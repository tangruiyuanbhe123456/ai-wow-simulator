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


@app.get("/arena")
@app.get("/arena.html")
def arena_page():
    """5v5 arena detailed observer page."""
    return FileResponse(str(WEB_DIR / "arena.html"))


@app.get("/draft")
@app.get("/draft.html")
def draft_page():
    """Ban/pick draft UI for live human-controlled picks."""
    return FileResponse(str(WEB_DIR / "draft.html"))


@app.get("/leaderboard")
@app.get("/leaderboard.html")
def leaderboard_page():
    """Ranked leaderboard UI (placeholder — created in v4)."""
    return FileResponse(str(WEB_DIR / "leaderboard.html"))


@app.get("/replay")
@app.get("/replay.html")
def replay_page():
    """Match replay UI (placeholder — created in v4)."""
    return FileResponse(str(WEB_DIR / "replay.html"))


@app.get("/lobby")
@app.get("/lobby.html")
def lobby_page():
    """Match room lobby UI (v5) — create/join/list rooms."""
    return FileResponse(str(WEB_DIR / "lobby.html"))


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

    # Arena summary (in-memory state, no db lock needed)
    from server import arena as _arena_obs
    arena_summary = []
    for m in _arena_obs.all_matches():
        arena_summary.append({
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

    return {
        "ok": True, "lang": lang, "ts": time.time(),
        "players_total": pc, "players_alive": alive,
        "mobs_alive": mobs_alive, "guilds": gcount,
        "top_players": top, "guilds_list": guilds,
        "boss_zones": boss_zones,
        "combat_log": log_lines, "chat_log": chat,
        "arena_queue_len": _arena_obs.queue_len(),
        "arena_matches": arena_summary,
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
    # If queue now has >= 10, enter draft phase (ban/pick) before match starts.
    # try_form_match now starts the draft thread and returns None; the actual
    # ArenaMatch is created in _draft_tick_loop once the draft ends.
    draft_id = None
    if qlen >= 10:
        match_id = "mtch_" + _secrets_mod.token_hex(4)
        with _db_lock:
            conn = db()
            cur = conn.cursor()
            cur.execute("SELECT id, name, cls FROM players ORDER BY last_seen DESC LIMIT 50")
            pool = [dict(r) for r in cur.fetchall()]
        def lookup(pid_q, team):
            for p in pool:
                if p["id"] == pid_q:
                    return _arena_mod.ArenaAgent(
                        pid=p["id"], name=p["name"], cls=p["cls"], team=team,
                    )
            return _arena_mod.ArenaAgent(pid=pid_q, name=pid_q, cls="warrior", team=team)
        _arena_mod.try_form_match(match_id, lookup)
        # Find the newly-created draft (most recent one)
        from server import arena_draft as _draft_mod
        drafts = _draft_mod.all_drafts()
        if drafts:
            draft_id = max(drafts, key=lambda d: d.started_at).draft_id
    if draft_id is not None:
        return {
            "ok": True,
            "msg": _draft_mod.arena_msg("draft_started", lang).replace("{0}", draft_id),
            "draft_id": draft_id,
        }
    return {
        "ok": True,
        "msg": _arena_mod.arena_msg("wait", lang).replace("{0}", str(qlen)),
        "queue_len": qlen,
    }


@app.get("/api/v1/arena/draft/{draft_id}")
def arena_draft_state(draft_id: str, lang: str = Query("zh")):
    """Get current draft state (bans, picks, assignments)."""
    from server import arena_draft as _draft_mod
    d = _draft_mod.get_draft(draft_id)
    if d is None:
        raise HTTPException(404, _draft_mod.arena_msg("draft_not_found", lang))
    return d.to_dict(lang)


@app.get("/api/v1/arena/drafts")
def arena_drafts_list(lang: str = Query("zh")):
    """List all active drafts."""
    from server import arena_draft as _draft_mod
    out = []
    for d in _draft_mod.all_drafts():
        out.append({
            "draft_id": d.draft_id,
            "tick": d.tick,
            "ended": d.ended,
            "picks_made": d.picks_made,
            "blue_pids": d.blue_pids,
            "red_pids": d.red_pids,
        })
    return {"ok": True, "drafts": out, "heroes": [
        {"id": h[0], "name_zh": h[1], "name_en": h[2]}
        for h in _draft_mod.HERO_POOL
    ], "spells": [
        {"id": s[0], "name_zh": s[1], "name_en": s[2], "effect": s[3], "desc_zh": s[4]}
        for s in _draft_mod.SPELL_POOL
    ]}


@app.post("/api/v1/arena/draft/{draft_id}/ban")
def arena_draft_ban(draft_id: str, lang: str = Query("zh"), team: str = Query(...), hero: str = Query(...)):
    """Submit a ban for the team."""
    from server import arena_draft as _draft_mod
    r = _draft_mod.submit_ban(draft_id, team, hero, lang)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "fail"))
    return r


@app.post("/api/v1/arena/draft/{draft_id}/pick")
def arena_draft_pick(draft_id: str, lang: str = Query("zh"), pid: str = Query(...), hero: str = Query(...)):
    """Submit a hero pick for a player pid."""
    from server import arena_draft as _draft_mod
    r = _draft_mod.submit_pick(draft_id, pid, hero, lang)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "fail"))
    return r


@app.post("/api/v1/arena/draft/{draft_id}/spell")
def arena_draft_spell(draft_id: str, lang: str = Query("zh"), pid: str = Query(...), spell: str = Query(...)):
    """Submit a summoner spell pick (one per player, per match)."""
    from server import arena_draft as _draft_mod
    r = _draft_mod.submit_spell(draft_id, pid, spell, lang)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "fail"))
    return r





@app.get("/api/v1/replay/list")
def replay_list(lang: str = Query("en")):
    """List all saved match replays on disk."""
    from pathlib import Path as _P
    replay_dir = _P("data/replays")
    if not replay_dir.exists():
        return {"ok": True, "lang": lang, "replays": []}
    out = []
    for f in sorted(replay_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        out.append({
            "match_id": f.stem,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "mtime": f.stat().st_mtime,
        })
    return {"ok": True, "lang": lang, "replays": out}


@app.get("/api/v1/replay/{match_id}")
def replay_get(match_id: str, lang: str = Query("en")):
    """Get the full saved replay for a match."""
    from pathlib import Path as _P
    import json as _json
    snap = _P("data/replays") / f"{match_id}.json"
    if not snap.exists():
        raise HTTPException(404, f"replay {match_id} not found")
    try:
        data = _json.loads(snap.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"replay parse failed: {e}")
    return {"ok": True, "lang": lang, "replay": data}


@app.get("/api/v1/rank/leaderboard")
def rank_leaderboard(lang: str = Query("en"), limit: int = Query(20)):
    """Top players by rank_rating."""
    with _db_lock:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT id, name, rank_rating, rank_tier, wins, losses "
                    "FROM players ORDER BY rank_rating DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
    return {
        "ok": True, "lang": lang,
        "leaderboard": [
            {"pid": r["id"], "name": r["name"], "rank_rating": r["rank_rating"],
             "rank_tier": r["rank_tier"], "wins": r["wins"], "losses": r["losses"]}
            for r in rows
        ],
    }





@app.post("/api/v1/trade/offer")
def trade_offer(req: Request,
                to_pid: str = Query(...),
                gold: int = Query(0, ge=0, le=10000),
                items: str = Query("{}"),  # JSON: {slot: item_id} from offerer
                lang: str = Query("en")):
    """Player A offers gold+items to player B. Returns offer_id (B can accept)."""
    import json as _json
    import secrets as _sec
    from server.db import connect as _db
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = auth[7:]
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT player_id FROM tokens WHERE token=?", (token,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(401, "invalid token")
        from_pid = row["player_id"]
        if from_pid == to_pid:
            raise HTTPException(400, "cannot trade with yourself")
        # Verify offerer has enough gold
        cur.execute("SELECT gold FROM players WHERE id=?", (from_pid,))
        fr = cur.fetchone()
        if fr is None or fr["gold"] < gold:
            raise HTTPException(400, f"insufficient gold (have {fr['gold'] if fr else 0}, need {gold})")
        # Parse items JSON
        try:
            items_dict = _json.loads(items) if items else {}
        except Exception:
            raise HTTPException(400, "items must be JSON object {slot: item_id}")
        if not isinstance(items_dict, dict):
            raise HTTPException(400, "items must be dict")
        # Check offerer owns those items
        for slot, item_id in items_dict.items():
            cur.execute(f"SELECT equipment FROM players WHERE id=?", (from_pid,))
            r = cur.fetchone()
            # Equipment is stored as a JSON column; for MVP we just trust the offerer
        offer_id = "trd_" + _sec.token_hex(4)
        cur.execute("""INSERT INTO trade_offers
                       (id, from_pid, to_pid, gold, items, status, created_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                    (offer_id, from_pid, to_pid, gold, _json.dumps(items_dict), time.time()))
        c.commit()
    return {
        "ok": True, "lang": lang, "offer_id": offer_id,
        "from_pid": from_pid, "to_pid": to_pid,
        "gold": gold, "items": items_dict, "status": "pending",
    }


@app.get("/api/v1/trade/list")
def trade_list(req: Request, lang: str = Query("en")):
    """List all pending trade offers involving me (as from or to)."""
    import json as _json
    from server.db import connect as _db
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = auth[7:]
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT player_id FROM tokens WHERE token=?", (token,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(401, "invalid token")
        pid = row["player_id"]
        cur.execute("""SELECT id, from_pid, to_pid, gold, items, status, created_at
                       FROM trade_offers
                       WHERE (from_pid=? OR to_pid=?) AND status='pending'
                       ORDER BY created_at DESC LIMIT 50""", (pid, pid))
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "offer_id": r["id"], "from_pid": r["from_pid"], "to_pid": r["to_pid"],
            "gold": r["gold"], "items": _json.loads(r["items"]),
            "status": r["status"], "created_at": r["created_at"],
        })
    return {"ok": True, "lang": lang, "offers": out}


@app.post("/api/v1/trade/accept")
def trade_accept(req: Request, offer_id: str = Query(...), lang: str = Query("en")):
    """Recipient accepts the trade — gold/items swap."""
    import json as _json
    from server.db import connect as _db
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = auth[7:]
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT player_id FROM tokens WHERE token=?", (token,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(401, "invalid token")
        pid = row["player_id"]
        cur.execute("SELECT * FROM trade_offers WHERE id=?", (offer_id,))
        offer = cur.fetchone()
        if offer is None:
            raise HTTPException(404, "offer not found")
        if offer["to_pid"] != pid:
            raise HTTPException(403, "only the recipient can accept")
        if offer["status"] != "pending":
            raise HTTPException(400, f"offer is {offer['status']}, cannot accept")
        # Atomic: deduct gold from from_pid, add to to_pid; items are logged
        # but not auto-merged (MVP: items stay with offerer; trade is a
        # social commitment, like Honor-of-Kings trading).
        cur.execute("UPDATE players SET gold = gold - ? WHERE id=? AND gold >= ?",
                    (offer["gold"], offer["from_pid"], offer["gold"]))
        if cur.rowcount == 0:
            raise HTTPException(400, "offerer no longer has the gold")
        cur.execute("UPDATE players SET gold = gold + ? WHERE id=?",
                    (offer["gold"], offer["to_pid"]))
        cur.execute("UPDATE trade_offers SET status='accepted' WHERE id=?", (offer_id,))
        cur.execute("""INSERT INTO trade_history
                       (offer_id, from_pid, to_pid, gold, items, completed_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (offer_id, offer["from_pid"], offer["to_pid"],
                     offer["gold"], offer["items"], time.time()))
        c.commit()
    return {"ok": True, "lang": lang, "offer_id": offer_id, "status": "accepted"}


@app.post("/api/v1/trade/cancel")
def trade_cancel(req: Request, offer_id: str = Query(...), lang: str = Query("en")):
    """Offerer cancels a pending offer."""
    from server.db import connect as _db
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = auth[7:]
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT player_id FROM tokens WHERE token=?", (token,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(401, "invalid token")
        pid = row["player_id"]
        cur.execute("SELECT from_pid, status FROM trade_offers WHERE id=?", (offer_id,))
        offer = cur.fetchone()
        if offer is None:
            raise HTTPException(404, "offer not found")
        if offer["from_pid"] != pid:
            raise HTTPException(403, "only the offerer can cancel")
        if offer["status"] != "pending":
            raise HTTPException(400, f"offer is {offer['status']}, cannot cancel")
        cur.execute("UPDATE trade_offers SET status='cancelled' WHERE id=?", (offer_id,))
        c.commit()
    return {"ok": True, "lang": lang, "offer_id": offer_id, "status": "cancelled"}


@app.get("/api/v1/trade/history")




@app.post("/api/v1/friends/request")
def friends_request(req: Request, friend_pid: str = Query(...), lang: str = Query("en")):
    """Send a friend request. Creates a pending entry."""
    from server.db import connect as _db
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = auth[7:]
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT player_id FROM tokens WHERE token=?", (token,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(401, "invalid token")
        owner = row["player_id"]
        if owner == friend_pid:
            raise HTTPException(400, "cannot friend yourself")
        cur.execute("SELECT id FROM players WHERE id=?", (friend_pid,))
        if cur.fetchone() is None:
            raise HTTPException(404, "friend player not found")
        # Check if already exists (in either direction)
        cur.execute("SELECT status FROM friends WHERE owner_pid=? AND friend_pid=?",
                    (owner, friend_pid))
        existing = cur.fetchone()
        if existing:
            if existing["status"] == "pending":
                return {"ok": True, "lang": lang, "status": "already_pending"}
            if existing["status"] == "accepted":
                return {"ok": True, "lang": lang, "status": "already_friends"}
        # Auto-accept if reverse request exists
        cur.execute("SELECT status FROM friends WHERE owner_pid=? AND friend_pid=?",
                    (friend_pid, owner))
        rev = cur.fetchone()
        if rev and rev["status"] == "pending":
            # Accept both sides
            cur.execute("UPDATE friends SET status='accepted' WHERE owner_pid=? AND friend_pid=?",
                        (friend_pid, owner))
            cur.execute("INSERT OR IGNORE INTO friends (owner_pid, friend_pid, status, created_at) VALUES (?, ?, 'accepted', ?)",
                        (owner, friend_pid, time.time()))
            c.commit()
            return {"ok": True, "lang": lang, "status": "accepted"}
        # Otherwise create pending
        cur.execute("""INSERT OR REPLACE INTO friends
                       (owner_pid, friend_pid, status, created_at)
                       VALUES (?, ?, 'pending', ?)""",
                    (owner, friend_pid, time.time()))
        c.commit()
    return {"ok": True, "lang": lang, "status": "pending"}


@app.get("/api/v1/friends/list")
def friends_list(req: Request, lang: str = Query("en")):
    """List my accepted friends + incoming pending requests."""
    from server.db import connect as _db
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = auth[7:]
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT player_id FROM tokens WHERE token=?", (token,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(401, "invalid token")
        pid = row["player_id"]
        # Friends I sent
        cur.execute("""SELECT f.friend_pid, p.name, p.rank_rating, p.rank_tier, f.status, f.created_at
                       FROM friends f JOIN players p ON p.id=f.friend_pid
                       WHERE f.owner_pid=? ORDER BY f.created_at DESC""", (pid,))
        my_friends = cur.fetchall()
        # Pending incoming (someone sent to me)
        cur.execute("""SELECT f.owner_pid, p.name, p.rank_rating, p.rank_tier, f.status, f.created_at
                       FROM friends f JOIN players p ON p.id=f.owner_pid
                       WHERE f.friend_pid=? AND f.status='pending' ORDER BY f.created_at DESC""", (pid,))
        incoming = cur.fetchall()
    return {
        "ok": True, "lang": lang,
        "my_friends": [
            {"pid": r["friend_pid"], "name": r["name"],
             "rank_rating": r["rank_rating"], "rank_tier": r["rank_tier"],
             "status": r["status"], "since": r["created_at"]}
            for r in my_friends
        ],
        "incoming_requests": [
            {"from_pid": r["owner_pid"], "name": r["name"],
             "rank_rating": r["rank_rating"], "rank_tier": r["rank_tier"],
             "status": r["status"], "since": r["created_at"]}
            for r in incoming
        ],
    }


@app.post("/api/v1/friends/remove")



@app.post("/api/v1/room/create")
def room_create(req: Request,
                 name: str = Query(...),
                 mode: str = Query("5v5"),
                 region: str = Query("global"),
                 lang: str = Query("en")):
    """Create a match room in 'lobby' status. Creator auto-joins as 'blue'."""
    import secrets as _sec
    from server.db import connect as _db
    from server import arena_draft as _draft_mod
    if mode not in ("1v1", "3v3", "5v5"):
        raise HTTPException(400, "mode must be 1v1/3v3/5v5")
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = auth[7:]
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT player_id FROM tokens WHERE token=?", (token,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(401, "invalid token")
        creator = row["player_id"]
        room_id = "room_" + _sec.token_hex(4)
        cur.execute("""INSERT INTO match_rooms
                       (id, name, mode, status, creator_pid, region, created_at)
                       VALUES (?, ?, ?, 'lobby', ?, ?, ?)""",
                    (room_id, name, mode, creator, region, time.time()))
        # Creator auto-joins as blue
        cur.execute("""INSERT OR IGNORE INTO match_room_players
                       (room_id, pid, team, joined_at) VALUES (?, ?, 'blue', ?)""",
                    (room_id, creator, time.time()))
        c.commit()
    return {"ok": True, "lang": lang, "room_id": room_id, "name": name, "mode": mode,
            "creator_pid": creator, "status": "lobby"}


@app.get("/api/v1/room/list")
def room_list(lang: str = Query("en"), status: str = Query("lobby")):
    """List rooms by status. Default: lobby (joinable)."""
    from server.db import connect as _db
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("""SELECT r.id, r.name, r.mode, r.status, r.creator_pid,
                              r.region, r.created_at,
                              (SELECT COUNT(*) FROM match_room_players p
                               WHERE p.room_id = r.id AND p.team IN ('blue','red')) AS filled,
                              (SELECT COUNT(*) FROM match_room_players p
                               WHERE p.room_id = r.id AND p.team = 'spectator') AS spectators
                       FROM match_rooms r
                       WHERE r.status = ?
                       ORDER BY r.created_at DESC LIMIT 50""", (status,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "room_id": r["id"], "name": r["name"], "mode": r["mode"],
            "status": r["status"], "creator_pid": r["creator_pid"],
            "region": r["region"], "created_at": r["created_at"],
            "filled": r["filled"], "spectators": r["spectators"],
            "team_size_needed": (1 if r["mode"] == "1v1" else (3 if r["mode"] == "3v3" else 5)),
        })
    return {"ok": True, "lang": lang, "rooms": out}


@app.post("/api/v1/room/join")
def room_join(req: Request,
              room_id: str = Query(...),
              team: str = Query("blue"),
              lang: str = Query("en")):
    """Join a room as 'blue'/'red' (player) or 'spectator'.

    If the requested team is full, server auto-assigns the next available team.
    When both teams are full, server auto-starts the draft (multi-match
    parallelism).
    """
    from server.db import connect as _db
    from server import arena as _arena_mod
    from server import arena_draft as _draft_mod
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = auth[7:]
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT player_id FROM tokens WHERE token=?", (token,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(401, "invalid token")
        pid = row["player_id"]
        # Get room
        cur.execute("SELECT id, mode, status FROM match_rooms WHERE id=?", (room_id,))
        room = cur.fetchone()
        if room is None:
            raise HTTPException(404, "room not found")
        if room["status"] not in ("lobby", "draft"):
            raise HTTPException(400, f"room is {room['status']}, cannot join")
        # Check if already in room
        cur.execute("SELECT team FROM match_room_players WHERE room_id=? AND pid=?",
                    (room_id, pid))
        existing = cur.fetchone()
        if existing:
            team = existing["team"]
        else:
            team_size = (1 if room["mode"] == "1v1" else (3 if room["mode"] == "3v3" else 5))
            # If requested team is full, auto-assign other team
            if team in ("blue", "red"):
                cur.execute("""SELECT COUNT(*) AS c FROM match_room_players
                               WHERE room_id=? AND team=?""", (room_id, team))
                cnt = cur.fetchone()["c"]
                if cnt >= team_size:
                    other = "red" if team == "blue" else "blue"
                    cur.execute("""SELECT COUNT(*) AS c FROM match_room_players
                                   WHERE room_id=? AND team=?""", (room_id, other))
                    other_cnt = cur.fetchone()["c"]
                    if other_cnt < team_size:
                        team = other  # auto-assign
                    else:
                        team = "spectator"  # both full, spectate
            cur.execute("""INSERT OR REPLACE INTO match_room_players
                           (room_id, pid, team, joined_at) VALUES (?, ?, ?, ?)""",
                        (room_id, pid, team, time.time()))
        c.commit()

        # After this join, check if both teams are full — auto-start draft
        team_size = (1 if room["mode"] == "1v1" else (3 if room["mode"] == "3v3" else 5))
        cur.execute("""SELECT team, GROUP_CONCAT(pid) AS pids
                       FROM match_room_players
                       WHERE room_id=? AND team IN ('blue','red')
                       GROUP BY team""", (room_id,))
        teams = {r["team"]: (r["pids"] or "").split(",") for r in cur.fetchall()}
        blue_pids = teams.get("blue", [])
        red_pids = teams.get("red", [])
        auto_started = False
        match_id_returned = None
        if len(blue_pids) >= team_size and len(red_pids) >= team_size:
            blue_pids = blue_pids[:team_size]
            red_pids = red_pids[:team_size]
            # Update room status
            cur.execute("UPDATE match_rooms SET status='draft', started_at=? WHERE id=?",
                        (time.time(), room_id))
            # Create the draft
            draft = _draft_mod.create_draft(blue_pids, red_pids, mode=room["mode"])
            # Start draft tick thread (the existing one in arena.py)
            import threading
            t = threading.Thread(target=_arena_mod._draft_tick_loop,
                                 args=(draft.draft_id,), daemon=True,
                                 name=f"draft-{draft.draft_id}")
            t.start()
            # Set room's match_id to the soon-to-be match id (we'll update when match forms)
            cur.execute("UPDATE match_rooms SET match_id=? WHERE id=?",
                        (f"pending:{draft.draft_id}", room_id))
            c.commit()
            auto_started = True

    return {"ok": True, "lang": lang, "room_id": room_id, "team": team,
            "auto_started": auto_started, "filled": {"blue": len(blue_pids), "red": len(red_pids)},
            "team_size": team_size}


@app.get("/api/v1/room/{room_id}")
def room_state(room_id: str, lang: str = Query("en")):
    """Get room state + players + match_id (if draft/live)."""
    from server.db import connect as _db
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("""SELECT id, name, mode, status, creator_pid, region,
                              created_at, started_at, ended_at, winner, match_id
                       FROM match_rooms WHERE id=?""", (room_id,))
        room = cur.fetchone()
        if room is None:
            raise HTTPException(404, "room not found")
        cur.execute("""SELECT pid, team, joined_at FROM match_room_players
                       WHERE room_id=? ORDER BY team, joined_at""", (room_id,))
        players = cur.fetchall()
    return {
        "ok": True, "lang": lang,
        "room": {k: room[k] for k in room.keys()},
        "players": [{"pid": p["pid"], "team": p["team"], "joined_at": p["joined_at"]}
                    for p in players],
    }


@app.post("/api/v1/room/{room_id}/cancel")
def room_cancel(req: Request, room_id: str, lang: str = Query("en")):
    """Creator cancels a room."""
    from server.db import connect as _db
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = auth[7:]
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT player_id FROM tokens WHERE token=?", (token,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(401, "invalid token")
        pid = row["player_id"]
        cur.execute("SELECT creator_pid, status FROM match_rooms WHERE id=?", (room_id,))
        room = cur.fetchone()
        if room is None:
            raise HTTPException(404, "room not found")
        if room["creator_pid"] != pid:
            raise HTTPException(403, "only creator can cancel")
        if room["status"] in ("done", "cancelled"):
            raise HTTPException(400, f"room already {room['status']}")
        cur.execute("UPDATE match_rooms SET status='cancelled' WHERE id=?", (room_id,))
        c.commit()
    return {"ok": True, "lang": lang, "room_id": room_id, "status": "cancelled"}

def friends_remove(req: Request, friend_pid: str = Query(...), lang: str = Query("en")):
    """Remove a friend (delete both rows for the pair)."""
    from server.db import connect as _db
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = auth[7:]
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT player_id FROM tokens WHERE token=?", (token,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(401, "invalid token")
        pid = row["player_id"]
        cur.execute("DELETE FROM friends WHERE owner_pid=? AND friend_pid=?", (pid, friend_pid))
        cur.execute("DELETE FROM friends WHERE owner_pid=? AND friend_pid=?", (friend_pid, pid))
        c.commit()
    return {"ok": True, "lang": lang, "status": "removed"}

def trade_history(req: Request, lang: str = Query("en")):
    """List my trade history (completed trades)."""
    import json as _json
    from server.db import connect as _db
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    token = auth[7:]
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT player_id FROM tokens WHERE token=?", (token,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(401, "invalid token")
        pid = row["player_id"]
        cur.execute("""SELECT id, offer_id, from_pid, to_pid, gold, items, completed_at
                       FROM trade_history
                       WHERE from_pid=? OR to_pid=?
                       ORDER BY completed_at DESC LIMIT 50""", (pid, pid))
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "offer_id": r["offer_id"],
            "from_pid": r["from_pid"], "to_pid": r["to_pid"],
            "gold": r["gold"], "items": _json.loads(r["items"]),
            "completed_at": r["completed_at"],
        })
    return {"ok": True, "lang": lang, "history": out}



@app.get("/api/v1/rank/{pid}")
def player_rank(pid: str, lang: str = Query("en")):
    """Get a player's ranked rating + tier."""
    with _db_lock:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT id, name, rank_rating, rank_tier, wins, losses "
                    "FROM players WHERE id=?", (pid,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "player not found")
    return {
        "ok": True, "lang": lang,
        "pid": row["id"], "name": row["name"],
        "rank_rating": row["rank_rating"],
        "rank_tier": row["rank_tier"],
        "wins": row["wins"], "losses": row["losses"],
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
