"""FastAPI server with all REST endpoints."""
from __future__ import annotations
import time
import json
import sqlite3
import threading
import secrets
import logging
from typing import Any
from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from pydantic import BaseModel, Field
import uvicorn

from server.config import (
    HOST, PORT, TICK_MS, DB_PATH, WEB_DIR, LOG_FILE,
    DEFAULT_LANG, SUPPORTED_LANGS, MAX_LEVEL,
)
from server.db import connect, init_schema, row_to_dict, jdump, jload
from server.world import (
    spawn_world_mobs, zone_by_id, zone_name, item_name,
    gen_id, level_to_hp, level_to_atk, xp_to_next,
)
from server.i18n import t, name as i18n_name
from server.combat import SKILLS, skill_name, list_skills_for_class, perform_attack
from server.guild import (
    create_guild as g_create, join_guild as g_join, kick_member as g_kick,
    set_relation as g_set_relation, list_guilds, get_guild, guild_chat,
)
from server.quest import (
    QUEST_TEMPLATES, quest_name, available_quests, accept_quest,
    complete_quest, active_quests,
)
from server.tick import tick


# ---- App init --------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("wow")

app = FastAPI(title="AI WoW Simulator", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# DB connection (single-threaded via lock for simplicity in this scale)
_db_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
        init_schema(_conn)
    return _conn


# ---- Pydantic models -------------------------------------------------------

class RegisterReq(BaseModel):
    name: str
    cls: str = Field(..., pattern="^(warrior|mage|priest|hunter)$")


class ActionReq(BaseModel):
    action: str  # move / attack / cast / heal / gather / chat / party / guild / quest
    payload: dict[str, Any] = Field(default_factory=dict)


# ---- Helpers ---------------------------------------------------------------

def auth_player(authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization[7:].strip()
    with _db_lock:
        cur = db().cursor()
        cur.execute("SELECT player_id FROM tokens WHERE token=?", (token,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(401, "invalid token")
        cur.execute("SELECT * FROM players WHERE id=?", (row["player_id"],))
        return dict(cur.fetchone())


def get_lang(lang: str | None = None) -> str:
    if lang and lang in SUPPORTED_LANGS:
        return lang
    return DEFAULT_LANG


# ---- Routes: meta ----------------------------------------------------------

@app.get("/")
def root():
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/health")
def health():
    return {"ok": True, "ts": time.time(), "version": "1.0.0"}


@app.get("/api/v1/lang/{lang}")
def change_lang(lang: str, response: Response):
    if lang not in SUPPORTED_LANGS:
        raise HTTPException(400, "unsupported lang")
    response.set_cookie("wow_lang", lang, max_age=86400 * 365)
    return {"ok": True, "lang": lang}


# ---- Routes: auth ----------------------------------------------------------

@app.post("/api/v1/register")
def register(req: RegisterReq, lang: str = Query(DEFAULT_LANG)):
    with _db_lock:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM players WHERE name=?", (req.name,))
        if cur.fetchone():
            raise HTTPException(409, "name taken")
        pid = gen_id("p")
        token = secrets.token_hex(16)
        now = time.time()
        hp_max = level_to_hp(1)
        atk = level_to_atk(1)
        mp_max = 60  # L1 base mp; enough for several heals in a fight
        cur.execute("""INSERT INTO players (id,name,cls,level,xp,hp,hp_max,mp,mp_max,atk,defn,
                       zone,pos_x,pos_y,gold,guild_id,party_id,pvp_flag,created_at,last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid, req.name, req.cls, 1, 0, hp_max, hp_max, mp_max, mp_max,
                     atk, 2, "starter_village", 0, 0, 50, None, None, 0, now, now))
        cur.execute("INSERT INTO tokens (token,player_id,issued_at) VALUES (?,?,?)",
                    (token, pid, now))
        cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,detail,lang) VALUES (?,?,?,?,?,?)",
                    (now, pid, req.name, "register",
                     t("registered", lang, name=req.name, cls=req.cls, level=1), lang))
        conn.commit()
        return {"ok": True, "player_id": pid, "token": token, "name": req.name, "cls": req.cls}


# ---- Routes: state / observe -----------------------------------------------

@app.get("/api/v1/state")
def state(authorization: str = Header(...), lang: str = Query("zh")):
    p = auth_player(authorization)
    return _state_for_player(p, lang)


@app.get("/api/v1/observer/state")
def observer_state(lang: str = Query("zh")):
    """Public observer endpoint - no auth. Used by web UI and TUI."""
    return _world_snapshot(lang)


def _state_for_player(p: dict, lang: str) -> dict:
    with _db_lock:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM players WHERE id=?", (p["id"],))
        me = dict(cur.fetchone())
        cur.execute("SELECT * FROM zones_meta") if False else None
        # Players in same zone
        cur.execute("SELECT id,name,cls,level,hp,hp_max,atk,guild_id,party_id FROM players WHERE zone=?",
                    (me["zone"],))
        zone_players = [dict(r) for r in cur.fetchall()]
        # Mobs in same zone
        cur.execute("""SELECT id,name,kind,level,hp,hp_max,atk,defn,pos_x,pos_y,boss_room
                       FROM mobs WHERE zone=? AND alive=1""", (me["zone"],))
        mobs = [dict(r) for r in cur.fetchall()]
        # Inventory
        cur.execute("SELECT id,item_id,qty,equipped FROM inventory WHERE player_id=?", (me["id"],))
        inv = [dict(r) for r in cur.fetchall()]
        for it in inv:
            it["name"] = item_name(it["item_id"], lang)
        # Skills
        cur.execute("SELECT DISTINCT skill_id, COUNT(*) AS uses, MAX(ts) AS last_used FROM skills_used WHERE player_id=? GROUP BY skill_id",
                    (me["id"],))
        skill_use = [dict(r) for r in cur.fetchall()]
        # Party info
        party = None
        if me["party_id"]:
            cur.execute("SELECT * FROM parties WHERE id=?", (me["party_id"],))
            pr = cur.fetchone()
            if pr:
                cur.execute("SELECT player_id FROM party_members WHERE party_id=?", (me["party_id"],))
                members = [r["player_id"] for r in cur.fetchall()]
                party = {**dict(pr), "members": members}
        # Guild info
        guild = None
        if me["guild_id"]:
            guild = get_guild(conn, me["guild_id"])
        # Recent combat
        cur.execute("SELECT ts,actor_name,action,target_name,detail FROM combat_log ORDER BY id DESC LIMIT 10")
        recent = [dict(r) for r in cur.fetchall()]
        # Quests
        quests = active_quests(conn, me["id"], lang)

    me["name_disp"] = me["name"] if lang != "en" else me["name"]  # already 'name|cls' style? no, just name
    return {
        "ok": True, "lang": lang,
        "you": me,
        "zone": {"id": me["zone"], "name": zone_name(me["zone"], lang)},
        "players_here": zone_players,
        "mobs": mobs,
        "inventory": inv,
        "skill_use": skill_use,
        "party": party,
        "guild": guild,
        "quests": quests,
        "recent_combat": recent,
        "skills": [{"id": sid, "name": skill_name(sid, lang), "kind": SKILLS[sid].get("kind"),
                    "cost": SKILLS[sid].get("cost", 0)} for sid in list_skills_for_class(me["cls"])],
    }


def _world_snapshot(lang: str) -> dict:
    with _db_lock:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM players")
        pc = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM players WHERE hp > 0")
        alive = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM mobs WHERE alive=1 AND kind IN ('mob','boss')")
        mobs_alive = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM guilds")
        gcount = cur.fetchone()["c"]
        cur.execute("""SELECT id,name,cls,level,hp,hp_max,zone,guild_id,party_id FROM players
                       ORDER BY level DESC, xp DESC LIMIT 20""")
        top = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT ts,actor_name,action,target_name,detail FROM combat_log ORDER BY id DESC LIMIT 25")
        log_lines = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT ts,channel,sender_name,body FROM chat_log ORDER BY id DESC LIMIT 15")
        chat = [dict(r) for r in cur.fetchall()]
        cur.execute("""SELECT g.id,g.name,g.tag,(SELECT COUNT(*) FROM guild_members WHERE guild_id=g.id) AS members
                       FROM guilds g ORDER BY members DESC""")
        guilds = [dict(r) for r in cur.fetchall()]
        # Boss status per dungeon
        cur.execute("""SELECT zone, COUNT(*) AS alive_bosses FROM mobs
                       WHERE kind='boss' AND alive=1 GROUP BY zone""")
        boss_zones = {r["zone"]: r["alive_bosses"] for r in cur.fetchall()}
    return {
        "ok": True, "lang": lang, "ts": time.time(),
        "players_total": pc, "players_alive": alive,
        "mobs_alive": mobs_alive, "guilds": gcount,
        "top_players": top, "guilds_list": guilds,
        "boss_zones": boss_zones,
        "combat_log": log_lines, "chat_log": chat,
    }


# ---- Routes: action --------------------------------------------------------

@app.post("/api/v1/action")
def action(req: ActionReq, authorization: str = Header(...), lang: str = Query("zh")):
    p = auth_player(authorization)
    a = req.action.lower()
    pl = req.payload or {}

    with _db_lock:
        conn = db()
        cur = conn.cursor()

        if a == "move":
            x = int(pl.get("x", 0)); y = int(pl.get("y", 0))
            zone_id = pl.get("zone", p["zone"])
            z = zone_by_id(zone_id)
            if not z:
                raise HTTPException(400, "no zone")
            if abs(x - z["x"]) > z["size"] // 2 or abs(y - z["y"]) > z["size"] // 2:
                raise HTTPException(400, t("err_oob", lang))
            cur.execute("UPDATE players SET zone=?, pos_x=?, pos_y=?, last_seen=? WHERE id=?",
                        (zone_id, x, y, time.time(), p["id"]))
            conn.commit()
            return {"ok": True, "msg": t("moved", lang, name=p["name"], zone=zone_name(zone_id, lang), x=x, y=y)}

        if a == "attack" or a == "cast" or a == "heal":
            target_id = pl.get("target_id", "")
            skill_id = pl.get("skill_id", "")
            result = perform_attack(conn, p["id"], target_id, skill_id, lang)
            if not result["ok"]:
                raise HTTPException(400, result.get("msg", "fail"))
            return result

        if a == "gather":
            target_id = pl.get("target_id", "")
            # gathering nodes have 1 hp, the gather skill reduces them; we just resolve as 'attack' on gathering node
            return perform_attack(conn, p["id"], target_id, "mob_bite", lang)

        if a == "chat":
            body = str(pl.get("body", ""))[:200]
            if not body:
                raise HTTPException(400, "empty body")
            channel = pl.get("channel", "world")
            if channel == "guild" and p["guild_id"]:
                guild_chat(conn, p["id"], body, lang)
            else:
                cur.execute("INSERT INTO chat_log (ts,channel,sender_id,sender_name,body) VALUES (?,?,?,?,?)",
                            (time.time(), channel, p["id"], p["name"], body))
                conn.commit()
            return {"ok": True, "msg": t(f"chat_{channel}", lang, sender=p["name"], body=body)}

        if a == "party_create":
            if p["party_id"]:
                raise HTTPException(400, t("err_in_party", lang))
            pid = gen_id("party")
            cur.execute("INSERT INTO parties (id,leader_id,zone,created_at) VALUES (?,?,?,?)",
                        (pid, p["id"], p["zone"], time.time()))
            cur.execute("INSERT INTO party_members (party_id,player_id,joined_at) VALUES (?,?,?)",
                        (pid, p["id"], time.time()))
            cur.execute("UPDATE players SET party_id=? WHERE id=?", (pid, p["id"]))
            conn.commit()
            return {"ok": True, "party_id": pid}

        if a == "party_invite":
            target = pl.get("player_id", "")
            if not p["party_id"]:
                raise HTTPException(400, t("err_not_in_party", lang))
            cur.execute("SELECT * FROM parties WHERE id=? AND leader_id=?", (p["party_id"], p["id"]))
            if not cur.fetchone():
                raise HTTPException(400, t("err_not_leader", lang))
            cur.execute("UPDATE players SET party_id=? WHERE id=?", (p["party_id"], target))
            cur.execute("INSERT OR IGNORE INTO party_members (party_id,player_id,joined_at) VALUES (?,?,?)",
                        (p["party_id"], target, time.time()))
            cur.execute("SELECT name FROM players WHERE id=?", (target,))
            tname = cur.fetchone()["name"]
            cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,detail,lang) VALUES (?,?,?,?,?,?)",
                        (time.time(), p["id"], p["name"], "party_invite",
                         t("party_invite", lang, leader=p["name"], p=tname), lang))
            conn.commit()
            return {"ok": True, "msg": t("party_join", lang, p=tname, members=2)}

        if a == "party_leave":
            if not p["party_id"]:
                raise HTTPException(400, t("err_not_in_party", lang))
            cur.execute("DELETE FROM party_members WHERE party_id=? AND player_id=?", (p["party_id"], p["id"]))
            cur.execute("UPDATE players SET party_id=NULL WHERE id=?", (p["id"],))
            conn.commit()
            return {"ok": True}

        if a == "party_target":
            if not p["party_id"]:
                raise HTTPException(400, t("err_not_in_party", lang))
            cur.execute("SELECT * FROM parties WHERE id=? AND leader_id=?", (p["party_id"], p["id"]))
            if not cur.fetchone():
                raise HTTPException(400, t("err_not_leader", lang))
            kind = pl.get("kind"); tid = pl.get("target_id")
            cur.execute("UPDATE parties SET target_kind=?, target_id=? WHERE id=?", (kind, tid, p["party_id"]))
            conn.commit()
            return {"ok": True}

        if a == "party_move":
            if not p["party_id"]:
                raise HTTPException(400, t("err_not_in_party", lang))
            zid = pl.get("zone", p["zone"])
            z = zone_by_id(zid)
            if not z:
                raise HTTPException(400, "no zone")
            cur.execute("""UPDATE players SET zone=?, pos_x=?, pos_y=?, last_seen=?
                           WHERE party_id=?""", (zid, z["x"], z["y"], time.time(), p["party_id"]))
            cur.execute("UPDATE parties SET zone=? WHERE id=?", (zid, p["party_id"]))
            conn.commit()
            return {"ok": True, "msg": t("dungeon_enter", lang, party=p["party_id"], dungeon=zid, room="entrance")}

        if a == "guild_create":
            r = g_create(conn, p["id"], pl.get("name", ""), pl.get("tag", ""), lang)
            if not r["ok"]: raise HTTPException(400, r["msg"])
            return r
        if a == "guild_join":
            r = g_join(conn, p["id"], pl.get("guild_id", ""), lang)
            if not r["ok"]: raise HTTPException(400, r["msg"])
            return r
        if a == "guild_kick":
            r = g_kick(conn, p["id"], pl.get("target_id", ""), lang)
            if not r["ok"]: raise HTTPException(400, r["msg"])
            return r
        if a == "guild_declare_war":
            r = g_set_relation(conn, p["id"], pl.get("guild_id", ""), "war", lang)
            if not r["ok"]: raise HTTPException(400, r["msg"])
            return r
        if a == "guild_ally":
            r = g_set_relation(conn, p["id"], pl.get("guild_id", ""), "ally", lang)
            if not r["ok"]: raise HTTPException(400, r["msg"])
            return r
        if a == "guild_list":
            return {"ok": True, "guilds": list_guilds(conn)}
        if a == "guild_chat":
            r = guild_chat(conn, p["id"], pl.get("body", ""), lang)
            if not r["ok"]: raise HTTPException(400, r["msg"])
            return r

        if a == "quest_accept":
            r = accept_quest(conn, p["id"], pl.get("template_id", ""), lang)
            if not r["ok"]: raise HTTPException(400, r["msg"])
            return r
        if a == "quest_complete":
            r = complete_quest(conn, p["id"], pl.get("quest_id", ""), lang)
            if not r["ok"]: raise HTTPException(400, r["msg"])
            return r
        if a == "quest_list":
            return {"ok": True, "available": available_quests(p["level"], p["zone"], lang),
                    "active": active_quests(conn, p["id"], lang)}

        if a == "respawn":
            if p["hp"] > 0:
                raise HTTPException(400, "not dead")
            cur.execute("UPDATE players SET hp=hp_max, mp=mp_max WHERE id=?", (p["id"],))
            conn.commit()
            return {"ok": True, "msg": "respawned at starter village"}

        raise HTTPException(400, f"unknown action: {a}")


# ---- Routes: admin ---------------------------------------------------------

@app.post("/api/v1/admin/tick")
def admin_tick():
    with _db_lock:
        c = tick(db())
    return {"ok": True, "counters": c}


@app.post("/api/v1/admin/spawn")
def admin_spawn():
    """(re)spawn world mobs if empty."""
    with _db_lock:
        n = spawn_world_mobs(db())
    return {"ok": True, "spawned": n}


@app.get("/api/v1/zones")
def zones(lang: str = Query("zh")):
    from server.world import ZONES
    return {"ok": True, "zones": [{**z, "name": zone_name(z["id"], lang)} for z in ZONES]}


# ---- 5v5 Arena (Honor-of-Kings-inspired team battle) --------------------

import secrets as _secrets_mod
from server import arena as _arena_mod
# Per-match tick thread bookkeeping
_arena_tick_threads: dict[str, threading.Thread] = {}


def _arena_tick_loop(match_id: str):
    """Background loop that advances a single arena match by 1 tick/second.
    Stops when the match ends (winner set)."""
    import random as _r
    rng = _r.Random()
    while True:
        m = _arena_mod.get_match(match_id)
        if m is None:
            return
        if m.ended:
            return
        _arena_mod.tick_match(m, rng)
        time.sleep(1.0)


@app.post("/api/v1/arena/queue")
def arena_queue(req: Request, lang: str = Query("zh")):
    """Player joins 5v5 queue. Auto-forms match when 10 players queued.

    Auth: Bearer token (same as /action). The player must already be registered
    via /register (their token is in the Authorization header).
    """
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = auth[7:]
    # Look up player by token (tokens are stored in a separate `tokens` table,
    # not as a column on `players`).
    with _db_lock:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT player_id FROM tokens WHERE token=?", (token,))
        tok_row = cur.fetchone()
        if tok_row is None:
            raise HTTPException(401, "invalid token; register first via /api/v1/register")
        pid = tok_row["player_id"]
        cur.execute("SELECT id, name, cls FROM players WHERE id=?", (pid,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(401, "token valid but player not found")
    pid, name, cls = row["id"], row["name"], row["cls"]

    qlen = _arena_mod.enqueue(pid)
    # If queue now has >= 10, form a match.
    m = None
    if qlen >= 10:
        match_id = "mtch_" + _secrets_mod.token_hex(4)
        # We need full agent metadata. Pull all 10 from DB.
        with _db_lock:
            conn = db()
            cur = conn.cursor()
            cur.execute("SELECT id, name, cls FROM players ORDER BY last_seen DESC LIMIT 50")
            pool = [dict(r) for r in cur.fetchall()]
        # try_form_match pops the first 10 from the queue itself; we just need
        # to provide a lookup callable that maps pid → ArenaAgent metadata.
        def lookup(pid_q, team):
            for p in pool:
                if p["id"] == pid_q:
                    return _arena_mod.ArenaAgent(
                        pid=p["id"], name=p["name"], cls=p["cls"], team=team,
                    )
            # Fallback if lookup fails (player in queue but missing from pool)
            return _arena_mod.ArenaAgent(pid=pid_q, name=pid_q, cls="warrior", team=team)
        m = _arena_mod.try_form_match(match_id, lookup)
    if m is not None:
        # Start background tick thread
        t = threading.Thread(target=_arena_tick_loop, args=(m.match_id,),
                             daemon=True, name=f"arena-{m.match_id}")
        t.start()
        _arena_tick_threads[m.match_id] = t
        return {
            "ok": True,
            "msg": _arena_mod.arena_msg("match_started", lang).replace("{0}", m.match_id),
            "match_id": m.match_id,
        }
    return {
        "ok": True,
        "msg": _arena_mod.arena_msg("wait", lang).replace("{0}", str(qlen)),
        "queue_len": qlen,
    }


@app.get("/api/v1/arena/matches")
def arena_matches(lang: str = Query("zh")):
    """List all active arena matches (observer endpoint)."""
    out = []
    for m in _arena_mod.all_matches():
        d = m.to_dict(lang)
        out.append({
            "match_id": m.match_id,
            "tick": m.tick,
            "ended": m.ended,
            "winner": m.winner,
            "blue_alive": sum(1 for a in m.blue if a.alive),
            "red_alive": sum(1 for a in m.red if a.alive),
            "blue_crystal_hp": m.blue_crystal.hp,
            "red_crystal_hp": m.red_crystal.hp,
            "blue_kills": m.team_kills["blue"],
            "red_kills": m.team_kills["red"],
        })
    return {"ok": True, "matches": out, "queue_len": _arena_mod.queue_len()}


@app.get("/api/v1/arena/match/{match_id}")
def arena_match_state(match_id: str, lang: str = Query("zh")):
    m = _arena_mod.get_match(match_id)
    if m is None:
        raise HTTPException(404, _arena_mod.arena_msg("not_found", lang))
    return m.to_dict(lang)


# ---- Tick loop thread ------------------------------------------------------

def _tick_loop():
    log.info("tick loop start, %dms interval", TICK_MS)
    while True:
        try:
            with _db_lock:
                tick(db())
        except Exception as e:
            log.exception("tick error: %s", e)
        time.sleep(TICK_MS / 1000.0)


def start_background_tick():
    t = threading.Thread(target=_tick_loop, daemon=True, name="wow-tick")
    t.start()


@app.on_event("startup")
def on_startup():
    with _db_lock:
        init_schema(db())
        spawn_world_mobs(db())
    start_background_tick()
    log.info("AI WoW Simulator ready on %s:%d", HOST, PORT)


def main():
    """Entry point for `python -m server.main`."""
    uvicorn.run("server.main:app", host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
