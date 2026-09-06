"""V13 Digital Life Layer REST API.

Endpoints (all under /api/v1/bot/{pid}/...):
  GET    /lifecycle            灵魂名/DNA/出生/死亡/灵魂属性
  GET    /memories             记忆流
  GET    /biography            生平传记
  GET    /genealogy            谱系树
  POST   /resurrect            复活(外部扣 Q 币后调用)
  POST   /memorial/flower      献花(Q 币赠予,入讣告)

Plus helper:
  POST   /lifecycle/birth      给现有 bot 触发 birth(通常 agent SDK connect 时自动调)
"""
from __future__ import annotations
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from server.db import connect
from server.db.schema_v13 import ensure_v13_schema
from server.db.schema_v14 import ensure_v14_schema
from server.lifecycle import (
    birth as lc_birth,
    get_lifecycle as lc_get,
    on_death as lc_death,
    resurrect as lc_resurrect,
    add_flower as lc_flower,
)
from server.memory import list_memories
from server.biography import list_biography
from server.genealogy import tree as genealogy_tree, add as add_relation
from server.wallet import (
    get_balance as qc_balance,
    topup as qc_topup,
    gift as qc_gift,
    spend_on_bot as qc_spend_on_bot,
    kyc_status as qc_kyc_status,
)
from server.death_dispatcher import process_pending as death_dispatch


router = APIRouter(prefix="/api/v1/bot", tags=["v13-life"])


# --- Ensure schema on import (idempotent) ----------------------------------

def _ensure():
    c = connect()
    ensure_v13_schema(c)
    c.close()


_ensure()


# --- Pydantic models -------------------------------------------------------

class BirthBody(BaseModel):
    player_name: str


class DeathBody(BaseModel):
    zone: str
    pos_x: int = 0
    pos_y: int = 0
    killed_by_pid: Optional[str] = None
    killer_name: Optional[str] = None


class ResurrectBody(BaseModel):
    payer_pid: str = "human:anonymous"
    qc_spent: int = 100


class FlowerBody(BaseModel):
    from_pid: str
    qc_amount: int = 10


class RelationBody(BaseModel):
    pid_b: str
    relation: str
    note: str = ""


# --- Endpoints --------------------------------------------------------------

@router.post("/{pid}/lifecycle/birth")
def ep_birth(pid: str, body: BirthBody):
    return lc_birth(connect(), pid, body.player_name)


# NOTE: /memorials must come BEFORE /{pid}/* routes — FastAPI matches
# by declaration order and would otherwise treat 'memorials' as a pid.
@router.get("/memorials")
def ep_memorials():
    """List every bot that has a tombstone OR is currently sleeping."""
    conn = connect()
    cur = conn.execute(
        """SELECT m.pid, m.death_no, m.ts, m.zone, m.flowers,
                  m.epitaph_zh, m.epitaph_en, l.soul_name, l.state
           FROM bot_memorials m
           LEFT JOIN bot_lifecycle l ON l.pid = m.pid
           ORDER BY m.ts DESC"""
    )
    return [
        {
            "pid": r[0],
            "death_no": r[1],
            "ts": r[2],
            "zone": r[3],
            "flowers": r[4],
            "epitaph_zh": r[5],
            "epitaph_en": r[6],
            "soul_name": r[7],
            "state": r[8] or "sleeping",
        }
        for r in cur.fetchall()
    ]


@router.get("/{pid}/lifecycle")
def ep_get_lifecycle(pid: str):
    lc = lc_get(connect(), pid)
    if not lc:
        raise HTTPException(404, "no_lifecycle")
    return lc


@router.post("/{pid}/lifecycle/death")
def ep_death(pid: str, body: DeathBody):
    return lc_death(
        connect(), pid, body.zone, body.pos_x, body.pos_y,
        killed_by_pid=body.killed_by_pid, killer_name=body.killer_name,
    )


@router.post("/{pid}/resurrect")
def ep_resurrect(pid: str, body: ResurrectBody):
    return lc_resurrect(connect(), pid, body.payer_pid, body.qc_spent)


@router.post("/{pid}/memorial/flower")
def ep_flower(pid: str, body: FlowerBody):
    return lc_flower(connect(), pid, body.from_pid, body.qc_amount)


@router.get("/{pid}/memories")
def ep_memories(pid: str, limit: int = Query(20, ge=1, le=200),
                min_weight: int = Query(0, ge=0, le=100)):
    return list_memories(connect(), pid, limit=limit, min_weight=min_weight)


@router.get("/{pid}/biography")
def ep_biography(pid: str):
    return list_biography(connect(), pid)


@router.get("/{pid}/genealogy")
def ep_genealogy(pid: str):
    return genealogy_tree(connect(), pid)


@router.post("/{pid}/relations")
def ep_add_relation(pid: str, body: RelationBody):
    return add_relation(connect(), pid, body.pid_b, body.relation, body.note)


# --- v14: QC Wallet + Ledger ------------------------------------------------

class TopupBody(BaseModel):
    player_pid: str
    qc_amount: int
    order_id: Optional[str] = None
    note: str = ""


class GiftBody(BaseModel):
    from_pid: str
    to_pid: str
    qc_amount: int


class SpendBody(BaseModel):
    from_pid: str
    qc_amount: int = 10


@router.get("/qc/balance/{pid}")
def ep_qc_balance(pid: str):
    return {"pid": pid, "balance": qc_balance(connect(), pid)}


@router.get("/qc/kyc/{pid}")
def ep_qc_kyc(pid: str):
    return qc_kyc_status(connect(), pid)


@router.post("/qc/topup")
def ep_qc_topup(body: TopupBody):
    """In production this is called by the PayPal/Stripe webhook on payment success."""
    return qc_topup(connect(), body.player_pid, body.qc_amount,
                    order_id=body.order_id, note=body.note)


@router.post("/qc/gift")
def ep_qc_gift(body: GiftBody):
    return qc_gift(connect(), body.from_pid, body.to_pid, body.qc_amount)


@router.post("/qc/spend/{bot_pid}/{direction}")
def ep_qc_spend(bot_pid: str, direction: str, body: SpendBody):
    """direction = resurrect | flower"""
    if direction not in ("resurrect", "flower"):
        raise HTTPException(400, "direction must be resurrect or flower")
    return qc_spend_on_bot(connect(), body.from_pid, bot_pid, body.qc_amount, direction)


# v14 trigger: outbox-driven death dispatch
# (called by background tick every POLL_INTERVAL_SEC)
def _death_tick():
    while True:
        try:
            time.sleep(2.0)
            death_dispatch(connect(), limit=20)
        except Exception:
            import logging
            logging.getLogger("v14-death").exception("death tick error")
            time.sleep(5.0)
