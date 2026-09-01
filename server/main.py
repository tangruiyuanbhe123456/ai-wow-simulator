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


@app.get("/training")
@app.get("/training.html")
def training_page():
    """AI training center UI (v7) — bot fitness + strategy evolution."""
    return FileResponse(str(WEB_DIR / "training.html"))





@app.post("/api/v1/match/{match_id}/action")
def human_action(match_id: str,
                pid: str = Query(...),
                action: str = Query(...),
                lang: str = Query("en"),
                payload: str = Query("{}")):
    """Human-vs-bot mode: a player submits an action for their agent.

    action: "move" | "attack" | "cast_spell" | "use_ult" | "buy_item" | "use_item_active"
    payload (JSON): for move={"x":int,"y":int}; for attack={"target_pid":str};
                     for buy_item={"slot":str,"item":str}; for use_item_active={} (uses current item active)
    """
    import json as _json
    from server import arena as _arena_mod
    payload_d = _json.loads(payload) if payload else {}
    with _arena_mod._lock:
        m = _arena_mod._active_matches.get(match_id)
        if m is None:
            raise HTTPException(404, "match not found or ended")
        agent = next((a for a in (m.blue + m.red) if a.pid == pid), None)
        if agent is None:
            raise HTTPException(404, "your agent not in this match")
        if action == "move":
            x = int(payload_d.get("x", 0))
            y = int(payload_d.get("y", 0))
            agent.pos = (max(1, min(_arena_mod.ARENA_W - 2, x)),
                         max(1, min(_arena_mod.ARENA_H - 2, y)))
            result_msg = f"moved to ({x},{y})"
        elif action == "attack":
            target_pid = payload_d.get("target_pid", "")
            target = next((a for a in (m.blue + m.red) if a.pid == target_pid), None)
            if target is None or not target.alive:
                raise HTTPException(400, "target not found or dead")
            dmg = max(1, agent.atk)
            target.hp = max(0, target.hp - dmg)
            m.append_log(
                f"👤 {agent.name} ({agent.team}) 人类玩家攻击 {target.name} ({target.team}) 伤害 {dmg} | 👤 {agent.name} ({agent.team}) human attacks {target.name} ({target.team}) for {dmg}",
                f"👤 {agent.name} ({agent.team}) human attacks {target.name} ({target.team}) for {dmg}",
            )
            result_msg = f"dealt {dmg} dmg to {target.name}"
            if target.hp == 0:
                target.alive = False
                target.deaths += 1
                target.respawn_in = _arena_mod.RESPAWN_TICKS
                agent.kills += 1
                m.team_kills[agent.team] += 1
                agent.gold += _arena_mod.GOLD_PER_KILL
                _arena_mod._try_buy_best_affordable(agent, m)
        elif action == "cast_spell":
            if agent.spell_used:
                raise HTTPException(400, "spell already used")
            _arena_mod._cast_summoner_spell(agent, m, m.tick)
            result_msg = f"spell {agent.spell} cast"
        elif action == "use_ult":
            if agent.ult_cd > 0:
                raise HTTPException(400, f"ult on cooldown ({agent.ult_cd})")
            _arena_mod._use_ultimate(agent, m, m.tick)
            result_msg = f"ult {agent.ultimate} cast"
        elif action == "buy_item":
            slot = payload_d.get("slot", "")
            item = payload_d.get("item", "")
            if slot not in agent.equipment:
                raise HTTPException(400, f"unknown slot {slot}")
            # Find cost
            from server.arena import EQUIPMENT_CATALOG
            cost = next((e[1] for e in EQUIPMENT_CATALOG.get(slot, []) if e[0] == item), None)
            if cost is None:
                raise HTTPException(400, f"unknown item {item}")
            if agent.gold < cost:
                raise HTTPException(400, f"insufficient gold ({agent.gold}<{cost})")
            agent.gold -= cost
            agent.equipment[slot] = item
            _arena_mod._recompute_agent_stats(agent)
            m.append_log(
                f"👤 {agent.name} 购买 [{slot}:{item}] (-{cost}g) | 👤 {agent.name} bought [{slot}:{item}] (-{cost}g)",
                f"👤 {agent.name} bought [{slot}:{item}] (-{cost}g)",
            )
            result_msg = f"bought {item} for {cost}g"
        elif action == "use_item_active":
            _arena_mod._tick_item_actives_for_one(agent, m, m.tick)
            result_msg = "tried to use item active"
        else:
            raise HTTPException(400, f"unknown action {action}")
    return {"ok": True, "lang": lang, "pid": pid, "action": action, "result": result_msg,
            "agent": {"pos": agent.pos, "hp": agent.hp, "gold": agent.gold,
                      "ult_cd": agent.ult_cd, "spell_used": agent.spell_used}}






@app.post("/api/v1/tournament/create")
def tournament_create(req: Request,
                      name: str = Query(...),
                      size: int = Query(4, ge=2, le=16),
                      mode: str = Query("5v5"),
                      lang: str = Query("en")):
    """Create a tournament (single-elimination bracket)."""
    import secrets as _sec
    from server.db import connect as _db
    if size not in (2, 4, 8, 16):
        raise HTTPException(400, "size must be 2/4/8/16")
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
        tid = "tour_" + _sec.token_hex(4)
        cur.execute("""INSERT INTO tournaments
                       (id, name, size, mode, status, creator_pid, created_at)
                       VALUES (?, ?, ?, ?, 'registration', ?, ?)""",
                    (tid, name, size, mode, creator, time.time()))
        c.commit()
    return {"ok": True, "lang": lang, "tournament_id": tid, "name": name, "size": size, "mode": mode}


@app.post("/api/v1/tournament/{tid}/register_team")
def tournament_register_team(req: Request,
                              tid: str,
                              team_name: str = Query(...),
                              captain_pid: str = Query(...),
                              players: str = Query("[]"),
                              lang: str = Query("en")):
    """Register a team for the tournament. `players` is JSON array of pids."""
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
        creator = row["player_id"]
        cur.execute("SELECT size, status FROM tournaments WHERE id=?", (tid,))
        t = cur.fetchone()
        if t is None:
            raise HTTPException(404, "tournament not found")
        if t["status"] != "registration":
            raise HTTPException(400, f"tournament is {t['status']}, registration closed")
        cur.execute("SELECT COUNT(*) AS c FROM tournament_teams WHERE tournament_id=?", (tid,))
        cnt = cur.fetchone()["c"]
        if cnt >= t["size"]:
            raise HTTPException(400, f"tournament full ({cnt}/{t['size']})")
        players_list = _json.loads(players) if players else []
        team_id = cnt
        cur.execute("""INSERT INTO tournament_teams
                       (tournament_id, team_id, team_name, captain_pid, players, seed)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (tid, team_id, team_name, captain_pid, _json.dumps(players_list), team_id))
        c.commit()
    return {"ok": True, "lang": lang, "tournament_id": tid, "team_id": team_id,
            "team_name": team_name, "registered": cnt + 1, "size": t["size"]}


@app.post("/api/v1/tournament/{tid}/start")
def tournament_start(req: Request, tid: str, lang: str = Query("en")):
    """Start the tournament — generate bracket + create first-round matches."""
    import json as _json, secrets as _sec
    from server.db import connect as _db
    from server import arena as _arena_mod
    from server import arena_draft as _draft_mod
    import threading as _th
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
        cur.execute("SELECT * FROM tournaments WHERE id=?", (tid,))
        t = cur.fetchone()
        if t is None:
            raise HTTPException(404, "tournament not found")
        if t["creator_pid"] != creator:
            raise HTTPException(403, "only creator can start")
        if t["status"] != "registration":
            raise HTTPException(400, f"already {t['status']}")
        cur.execute("SELECT * FROM tournament_teams WHERE tournament_id=? ORDER BY team_id", (tid,))
        teams = cur.fetchall()
        size = t["size"]
        if len(teams) < size:
            raise HTTPException(400, f"need {size} teams, only {len(teams)} registered (use bots to fill)")
        # Build bracket: pair teams (0,1), (2,3), (4,5), ...
        bracket = {"round0": [], "round1": []}
        match_ids = {}
        for i in range(0, size, 2):
            t1 = teams[i]
            t2 = teams[i + 1]
            players1 = _json.loads(t1["players"]) or [t1["captain_pid"]]
            players2 = _json.loads(t2["players"]) or [t2["captain_pid"]]
            # Pad to mode size
            mode_size = (1 if t["mode"] == "1v1" else (3 if t["mode"] == "3v3" else 5))
            while len(players1) < mode_size:
                players1.append("bot_" + _sec.token_hex(2))
            while len(players2) < mode_size:
                players2.append("bot_" + _sec.token_hex(2))
            mid = "mtch_" + _sec.token_hex(4)
            slot = f"r0_m{i//2}"
            match_ids[slot] = mid
            bracket["round0"].append({"slot": slot, "team1": t1["team_name"], "team2": t2["team_name"],
                                      "match_id": mid})
            # Create the draft for this match
            draft = _draft_mod.create_draft(players1[:mode_size], players2[:mode_size], mode=t["mode"])
            t1_th = _th.Thread(target=_arena_mod._draft_tick_loop, args=(draft.draft_id,),
                               daemon=True, name=f"tdraft-{draft.draft_id}")
            t1_th.start()
        # Mark status in_progress and store bracket + match_ids
        cur.execute("""UPDATE tournaments SET status='in_progress', started_at=?, bracket=?, matches=?
                       WHERE id=?""",
                    (time.time(), _json.dumps(bracket), _json.dumps(match_ids), tid))
        c.commit()
    return {"ok": True, "lang": lang, "tournament_id": tid, "bracket": bracket,
            "matches": match_ids}


@app.get("/api/v1/tournament/{tid}")
def tournament_state(tid: str, lang: str = Query("en")):
    """Get tournament state (bracket + teams + status)."""
    import json as _json
    from server.db import connect as _db
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT * FROM tournaments WHERE id=?", (tid,))
        t = cur.fetchone()
        if t is None:
            raise HTTPException(404, "tournament not found")
        cur.execute("SELECT * FROM tournament_teams WHERE tournament_id=? ORDER BY team_id", (tid,))
        teams = cur.fetchall()
    return {"ok": True, "lang": lang, "tournament": {k: t[k] for k in t.keys()},
            "teams": [{"team_id": r["team_id"], "team_name": r["team_name"],
                       "captain_pid": r["captain_pid"], "players": _json.loads(r["players"]),
                       "eliminated": bool(r["eliminated"]), "seed": r["seed"]}
                      for r in teams]}


@app.get("/api/v1/tournaments")
def tournament_list(lang: str = Query("en"), status: str = Query("registration")):
    """List tournaments by status. (Plural URL to avoid /{tid} route conflict.)"""
    from server.db import connect as _db
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("""SELECT id, name, mode, size, status, creator_pid, created_at
                       FROM tournaments WHERE status=? ORDER BY created_at DESC LIMIT 50""", (status,))
        rows = cur.fetchall()
    return {"ok": True, "lang": lang,
            "tournaments": [{"id": r["id"], "name": r["name"], "mode": r["mode"],
                             "size": r["size"], "status": r["status"],
                             "creator_pid": r["creator_pid"], "created_at": r["created_at"]}
                            for r in rows]}












@app.post("/api/v1/tournament/{tid}/advance")
def tournament_advance_endpoint(req: Request, tid: str, lang: str = Query("en")):
    """Admin trigger: spawn next-round matches for a tournament."""
    from server import arena as _arena_mod
    result = _arena_mod._spawn_tournament_next_round(tid)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "advance failed"))
    return {"ok": True, "lang": lang, "tournament_id": tid, "result": result}






@app.get("/api/v1/stream/status")
def stream_status(lang: str = Query("en")):
    """Get the current streaming status (RTMP URL + viewer count).

    OBS / Bilibili Live / Twitch all consume RTMP. The actual streaming
    is done by an external ffmpeg / OBS process pointed at this URL.
    For MVP we just report the configuration.
    """
    return {
        "ok": True, "lang": lang,
        "rtmp_urls": {
            "bilibili":   "rtmp://live-push.bilivideo.com/live-bvc/你的stream_key",
            "twitch":     "rtmp://live.twitch.tv/app/你的stream_key",
            "douyin":     "rtmp://push-rtmp-flv.douyincdn.com/third/你的stream_key",
            "youtube":    "rtmp://a.rtmp.youtube.com/live2/你的stream_key",
        },
        "instruction_zh": (
            "1. 安装 ffmpeg 或 OBS Studio；"
            "2. 用以下命令把 server 网页推到 RTMP："
            "  ffmpeg -re -f x11grab -video_size 1280x720 -i :0 -f alsa -i default "
            "  -c:v libx264 -preset veryfast -maxrate 3000k -bufsize 6000k "
            "  -pix_fmt yuv420p -g 50 -c:a aac -b:a 160k -ar 44100 "
            "  -f flv rtmp://目标URL/stream_key"
            "3. 或用无头浏览器 (playwright/chromium) 自动截图"
        ),
        "instruction_en": (
            "1. Install ffmpeg or OBS Studio; "
            "2. Push server HTML to RTMP with: "
            "  ffmpeg -re -f x11grab -video_size 1280x720 -i :0 -f alsa -i default "
            "  -c:v libx264 -preset veryfast -maxrate 3000k -bufsize 6000k "
            "  -pix_fmt yuv420p -g 50 -c:a aac -b:a 160k -ar 44100 "
            "  -f flv rtmp://target/stream_key"
            "3. Or use a headless browser (playwright/chromium) to auto-screenshot"
        ),
        "ffmpeg_available": False,  # detected at runtime
    }


@app.post("/api/v1/stream/launch")
def stream_launch(req: Request,
                  platform: str = Query("bilibili"),
                  stream_key: str = Query(...),
                  lang: str = Query("en")):
    """Launch a stream from server HTML to the specified RTMP URL.

    Requires ffmpeg installed. Uses playwright headless chromium to capture
    the active match view and pipes it to ffmpeg → RTMP.

    For MVP this is a stub — full implementation requires installing
    playwright + chromium.
    """
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
    if platform == "bilibili":
        rtmp = f"rtmp://live-push.bilivideo.com/live-bvc/{stream_key}"
    elif platform == "twitch":
        rtmp = f"rtmp://live.twitch.tv/app/{stream_key}"
    elif platform == "douyin":
        rtmp = f"rtmp://push-rtmp-flv.douyincdn.com/third/{stream_key}"
    else:
        rtmp = stream_key  # custom
    return {
        "ok": True, "lang": lang,
        "rtmp": rtmp, "platform": platform,
        "command_zh": f"ffmpeg ... -f flv {rtmp}",
        "command_en": f"ffmpeg ... -f flv {rtmp}",
        "note": "MVP stub — actual streaming requires ffmpeg + playwright/chromium. See /api/v1/stream/status for setup instructions.",
    }






@app.post("/api/v1/chat/send")
def chat_send(req: Request,
              message: str = Query(..., min_length=1, max_length=500),
              scope: str = Query("global"),  # 'global' | 'room:<id>' | 'match:<id>'
              lang: str = Query("en")):
    """Post a chat message."""
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
        cur.execute("SELECT name FROM players WHERE id=?", (pid,))
        p = cur.fetchone()
        name = p["name"] if p else "?"
        cur.execute("""INSERT INTO chat_messages (scope, pid, name, message, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (scope, pid, name, message.strip(), time.time()))
        c.commit()
    return {"ok": True, "lang": lang, "scope": scope, "pid": pid, "name": name,
            "message": message.strip(), "ts": time.time()}


@app.get("/api/v1/chat/list")
def chat_list(scope: str = Query("global"),
             since: float = Query(0.0),
             lang: str = Query("en")):
    """Get chat messages for a scope since a timestamp."""
    from server.db import connect as _db
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("""SELECT id, pid, name, message, created_at
                       FROM chat_messages WHERE scope=? AND created_at > ?
                       ORDER BY created_at DESC LIMIT 100""", (scope, since))
        rows = cur.fetchall()
    return {"ok": True, "lang": lang, "scope": scope, "messages": [
        {"id": r["id"], "pid": r["pid"], "name": r["name"],
         "message": r["message"], "ts": r["created_at"]}
        for r in rows
    ]}






def _get_credits(pid: str) -> int:
    """Read player's current credit balance."""
    c = db()
    cur = c.cursor()
    cur.execute("SELECT credits FROM player_credits WHERE pid=?", (pid,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT OR IGNORE INTO player_credits (pid, credits, last_active) VALUES (?, 0, ?)",
                    (pid, time.time()))
        c.commit()
        return 0
    return row["credits"]


def _award_credits(pid: str, amount: int, reason: str = "") -> None:
    """Add credits to a player (called after match wins etc.)."""
    c = db()
    cur = c.cursor()
    cur.execute("""INSERT INTO player_credits (pid, credits, earned, last_active)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(pid) DO UPDATE SET credits=credits+?,
                                                  earned=earned+?,
                                                  last_active=?""",
                (pid, amount, amount, time.time(),
                 amount, amount, time.time()))
    c.commit()


def _charge_credits(pid: str, amount: int) -> bool:
    """Deduct credits atomically. Returns True if successful."""
    c = db()
    cur = c.cursor()
    cur.execute("UPDATE player_credits SET credits=credits-?, spent=spent+?, last_active=? "
                "WHERE pid=? AND credits>=?",
                (amount, amount, time.time(), pid, amount))
    if cur.rowcount == 0:
        return False
    c.commit()
    return True


@app.get("/api/v1/marketplace/browse")
def marketplace_browse(lang: str = Query("en"),
                        sort_by: str = Query("fitness"),  # fitness | price | recent
                        limit: int = Query(50)):
    """Browse all active bot listings, sorted by fitness/price/recent.

    Returns each listing + the bot's fitness stats + strategy profile preview.
    """
    from server.db import connect as _db
    import json as _json
    with _db_lock:
        c = db()
        cur = c.cursor()
        order = "bp.price_credits ASC" if sort_by == "price" else                 "bp.times_sold DESC, bp.created_at DESC" if sort_by == "recent" else                 "sp_wins DESC, sp_fitness_history DESC"
        cur.execute(f"""SELECT bp.id, bp.seller_pid, bp.bot_pid, bp.title, bp.description,
                               bp.price_credits, bp.times_sold, bp.created_at,
                               p.name AS seller_name,
                               sp.wins AS sp_wins, sp.losses AS sp_losses,
                               sp.matches_played AS sp_matches,
                               sp.fitness_history AS sp_fitness_history,
                               sp.hp_retreat_threshold AS sp_hp_thr,
                               sp.ult_teamfight_min_enemies AS sp_ult_min_e
                        FROM bot_listings bp
                        LEFT JOIN players p ON p.id = bp.seller_pid
                        LEFT JOIN bot_strategy_profiles sp ON sp.pid = bp.bot_pid
                        WHERE bp.status='active'
                        ORDER BY {order}
                        LIMIT ?""", (limit,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        hist_json = r["sp_fitness_history"] if "sp_fitness_history" in r.keys() else r["fitness_history"]
        hist = _json.loads(hist_json) if hist_json else []
        last_fit = hist[-1] if hist else 0
        avg_fit = sum(hist) / len(hist) if hist else 0
        out.append({
            "listing_id": r["id"],
            "seller_pid": r["seller_pid"],
            "seller_name": r["seller_name"],
            "bot_pid": r["bot_pid"],
            "title": r["title"],
            "description": r["description"],
            "price_credits": r["price_credits"],
            "times_sold": r["times_sold"],
            "created_at": r["created_at"],
            "bot_stats": {
                "wins": r["sp_wins"] or 0,
                "losses": r["sp_losses"] or 0,
                "matches": r["sp_matches"] or 0,
                "last_fitness": round(last_fit, 2),
                "avg_fitness": round(avg_fit, 2),
                "hp_retreat_threshold": r["sp_hp_thr"] or 0.30,
                "ult_teamfight_min_enemies": r["sp_ult_min_e"] or 1,
            },
        })
    return {"ok": True, "lang": lang, "sort_by": sort_by, "listings": out}


@app.post("/api/v1/marketplace/list")
def marketplace_list(req: Request,
                     bot_pid: str = Query(...),
                     title: str = Query(...),
                     description: str = Query(""),
                     price_credits: int = Query(20, ge=1, le=1000),
                     lang: str = Query("en")):
    """List your trained bot on the marketplace."""
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
        seller_pid = row["player_id"]
        # Verify seller owns the bot (bot_pid == seller_pid)
        if bot_pid != seller_pid:
            raise HTTPException(403, "you can only list your own bot")
        # Verify bot has training profile
        cur.execute("SELECT wins, matches_played FROM bot_strategy_profiles WHERE pid=?", (bot_pid,))
        prof_row = cur.fetchone()
        if prof_row is None:
            raise HTTPException(400, "bot has no training profile — must play matches first")
        listing_id = "lst_" + _sec.token_hex(4)
        cur.execute("""INSERT INTO bot_listings
                       (id, seller_pid, bot_pid, title, description, price_credits, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
                    (listing_id, seller_pid, bot_pid, title, description, price_credits, time.time()))
        c.commit()
    return {"ok": True, "lang": lang, "listing_id": listing_id, "title": title,
            "price_credits": price_credits, "status": "active"}


@app.get("/api/v1/marketplace/bot/{bot_pid}")
def marketplace_bot_detail(bot_pid: str, lang: str = Query("en")):
    """Full detail of a bot — includes strategy_profile snapshot for buyers."""
    import json as _json
    from server.db import connect as _db
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("""SELECT p.id, p.name, p.cls, p.level, p.rank_rating, p.rank_tier,
                              sp.wins, sp.losses, sp.matches_played, sp.fitness_history,
                              sp.hp_retreat_threshold, sp.teamfight_radius, sp.teamfight_min_allies,
                              sp.teamfight_min_enemies, sp.ult_teamfight_min_allies,
                              sp.ult_teamfight_min_enemies, sp.ult_threshold, sp.last_updated
                       FROM players p
                       LEFT JOIN bot_strategy_profiles sp ON sp.pid = p.id
                       WHERE p.id=?""", (bot_pid,))
        b = cur.fetchone()
        if b is None:
            raise HTTPException(404, "bot not found")
        # Find active listing
        cur.execute("""SELECT id, price_credits, description, title, times_sold, created_at
                       FROM bot_listings
                       WHERE bot_pid=? AND status='active'
                       LIMIT 1""", (bot_pid,))
        listing = cur.fetchone()
    hist = _json.loads(b["fitness_history"]) if b["fitness_history"] else []
    return {"ok": True, "lang": lang, "bot": {k: b[k] for k in b.keys()},
            "fitness_history": hist[-30:],
            "avg_fitness": round(sum(hist) / len(hist), 2) if hist else 0,
            "last_fitness": round(hist[-1], 2) if hist else 0,
            "listing": {k: listing[k] for k in listing.keys()} if listing else None}


@app.post("/api/v1/marketplace/buy")
def marketplace_buy(req: Request,
                    listing_id: str = Query(...),
                    lang: str = Query("en")):
    """Buy a bot. Credits transfer from buyer to seller.
    Bot's strategy_profile is snapshotted into bot_purchases for buyer reference.
    Buyer also gets the strategy_profile applied to one of their bots (if they have one).
    """
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
        buyer_pid = row["player_id"]
        cur.execute("SELECT * FROM bot_listings WHERE id=?", (listing_id,))
        listing = cur.fetchone()
        if listing is None:
            raise HTTPException(404, "listing not found")
        if listing["status"] != "active":
            raise HTTPException(400, f"listing is {listing['status']}")
        if listing["seller_pid"] == buyer_pid:
            raise HTTPException(400, "cannot buy your own listing")
        price = listing["price_credits"]
        # Check buyer has enough credits
        buyer_credits = _get_credits(buyer_pid)
        if buyer_credits < price:
            raise HTTPException(402, f"insufficient credits (have {buyer_credits}, need {price})")
        # Snapshot seller bot's strategy
        cur.execute("""SELECT wins, losses, matches_played, fitness_history,
                              hp_retreat_threshold, teamfight_radius, teamfight_min_allies,
                              teamfight_min_enemies, ult_teamfight_min_allies,
                              ult_teamfight_min_enemies, ult_threshold
                       FROM bot_strategy_profiles WHERE pid=?""",
 (listing["bot_pid"],))
        prof = cur.fetchone()
        snapshot = _json.dumps({k: prof[k] for k in prof.keys()} if prof else {})
        # Atomic: charge buyer + credit seller
        if not _charge_credits(buyer_pid, price):
            raise HTTPException(402, "credit charge failed")
        _award_credits(listing["seller_pid"], price, reason="sale")
        # Record purchase
        cur.execute("""INSERT INTO bot_purchases
                       (listing_id, buyer_pid, seller_pid, bot_pid, price_credits, strategy_snapshot, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (listing_id, buyer_pid, listing["seller_pid"],
                     listing["bot_pid"], price, snapshot, time.time()))
        cur.execute("UPDATE bot_listings SET times_sold=times_sold+1 WHERE id=?",
                    (listing_id,))
        c.commit()
    return {"ok": True, "lang": lang, "listing_id": listing_id, "bot_pid": listing["bot_pid"],
            "price_credits": price, "buyer_credits_remaining": buyer_credits - price,
            "snapshot": snapshot}


@app.get("/api/v1/marketplace/credits")
def marketplace_my_credits(req: Request, lang: str = Query("en")):
    """Check your credit balance."""
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
        cur.execute("SELECT credits, earned, spent, last_active FROM player_credits WHERE pid=?", (pid,))
        r = cur.fetchone()
    return {"ok": True, "lang": lang, "pid": pid,
            "credits": r["credits"] if r else 0,
            "earned": r["earned"] if r else 0,
            "spent": r["spent"] if r else 0}


@app.post("/api/v1/marketplace/delist/{listing_id}")
def marketplace_delist(req: Request, listing_id: str, lang: str = Query("en")):
    """Owner removes a listing."""
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
        cur.execute("SELECT seller_pid, status FROM bot_listings WHERE id=?", (listing_id,))
        listing = cur.fetchone()
        if listing is None:
            raise HTTPException(404, "listing not found")
        if listing["seller_pid"] != pid:
            raise HTTPException(403, "only seller can delist")
        if listing["status"] != "active":
            raise HTTPException(400, f"listing already {listing['status']}")
        cur.execute("UPDATE bot_listings SET status='removed' WHERE id=?", (listing_id,))
        c.commit()
    return {"ok": True, "lang": lang, "listing_id": listing_id, "status": "removed"}






@app.get("/api/v1/marketplace/profitability/{pid}")
def marketplace_profitability(pid: str, lang: str = Query("en")):
    """Profitability report for a bot.

    Returns:
      - lifetime_credits: total credits earned from match wins
      - lifetime_spent: total credits spent (listings, purchases)
      - current_balance: player_credits.credits
      - matches_played, wins, losses, win_rate
      - marketplace_sales: total credits earned from selling bots
      - marketplace_purchases: credits spent buying other bots
      - active_listings: how many currently listed
      - best_sale_price: highest single-sale price ever
      - roi: credits_earned_per_match (efficiency)
    """
    from server.db import connect as _db
    with _db_lock:
        c = db()
        cur = c.cursor()
        # Credits
        cur.execute("SELECT credits, earned, spent FROM player_credits WHERE pid=?", (pid,))
        cr = cur.fetchone()
        credits = cr["credits"] if cr else 0
        earned = cr["earned"] if cr else 0
        spent = cr["spent"] if cr else 0
        # Matches
        cur.execute("SELECT wins, losses, matches_played, fitness_history "
                    "FROM bot_strategy_profiles WHERE pid=?", (pid,))
        pr = cur.fetchone()
        wins = pr["wins"] if pr else 0
        losses = pr["losses"] if pr else 0
        matches = pr["matches_played"] if pr else 0
        win_rate = round(wins / max(1, matches), 3)
        # Marketplace sales (as seller)
        cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(price_credits), 0) AS total, "
                    "       COALESCE(MAX(price_credits), 0) AS max_price "
                    "FROM bot_purchases WHERE seller_pid=?", (pid,))
        sr = cur.fetchone()
        # Marketplace purchases (as buyer)
        cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(price_credits), 0) AS total "
                    "FROM bot_purchases WHERE buyer_pid=?", (pid,))
        br = cur.fetchone()
        # Active listings
        cur.execute("SELECT COUNT(*) AS n FROM bot_listings WHERE bot_pid=? AND status='active'", (pid,))
        ar = cur.fetchone()
    import json as _json
    fitness_history = _json.loads(pr["fitness_history"]) if pr and pr["fitness_history"] else []
    roi = round((earned - spent) / max(1, matches), 3) if matches else 0
    return {
        "ok": True, "lang": lang, "pid": pid,
        "current_balance": credits,
        "lifetime_credits_earned": earned,
        "lifetime_credits_spent": spent,
        "matches_played": matches,
        "wins": wins, "losses": losses, "win_rate": win_rate,
        "fitness_history_size": len(fitness_history),
        "last_fitness": round(fitness_history[-1], 2) if fitness_history else 0,
        "avg_fitness": round(sum(fitness_history) / len(fitness_history), 2) if fitness_history else 0,
        "marketplace_sales_count": sr["n"],
        "marketplace_sales_total_credits": sr["total"],
        "best_sale_price_credits": sr["max_price"],
        "marketplace_purchases_count": br["n"],
        "marketplace_purchases_total_credits": br["total"],
        "active_listings": ar["n"],
        "roi_per_match": roi,
    }






@app.get("/api/v1/marketplace/auto_price/{pid}")
def marketplace_auto_price(pid: str, lang: str = Query("en")):
    """Suggest a price (in credits) for a bot based on its fitness history.

    Pricing formula:
      base = 5 credits (minimum)
      fitness_bonus = max(0, avg_fitness) * 8
      win_rate_bonus = win_rate * 25
      experience_bonus = min(matches_played / 10, 5)
      recent_trend_bonus = (last_fitness - avg_fitness) * 10

      suggested_price = base + fitness_bonus + win_rate_bonus + experience_bonus + recent_trend_bonus
      clamp to [5, 200] credits
    """
    import json as _json
    from server.db import connect as _db
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT wins, losses, matches_played, fitness_history "
                    "FROM bot_strategy_profiles WHERE pid=?", (pid,))
        pr = cur.fetchone()
        if pr is None:
            raise HTTPException(404, "bot has no training profile — must play matches first")
    hist = _json.loads(pr["fitness_history"]) if pr["fitness_history"] else []
    wins = pr["wins"]; losses = pr["losses"]; matches = pr["matches_played"]
    avg_fit = sum(hist) / len(hist) if hist else 0
    last_fit = hist[-1] if hist else 0
    win_rate = wins / max(1, matches)
    base = 5
    fitness_bonus = max(0, avg_fit) * 8
    wr_bonus = win_rate * 25
    exp_bonus = min(matches / 10, 5)
    trend_bonus = (last_fit - avg_fit) * 10
    suggested = base + fitness_bonus + wr_bonus + exp_bonus + trend_bonus
    suggested = max(5, min(200, round(suggested)))
    return {
        "ok": True, "lang": lang, "pid": pid,
        "suggested_price_credits": suggested,
        "breakdown": {
            "base": base, "fitness_bonus": round(fitness_bonus, 1),
            "win_rate_bonus": round(wr_bonus, 1), "experience_bonus": round(exp_bonus, 1),
            "trend_bonus": round(trend_bonus, 1),
        },
        "bot_stats": {"wins": wins, "losses": losses, "matches": matches,
                      "win_rate": round(win_rate, 3), "avg_fitness": round(avg_fit, 2),
                      "last_fitness": round(last_fit, 2)},
    }


@app.post("/api/v1/marketplace/auto_list")
def marketplace_auto_list(req: Request,
                         bot_pid: str = Query(...),
                         force: bool = Query(False),  # re-list even if already listed
                         lang: str = Query("en")):
    """AI agent auto-list: prices the bot based on its fitness, then lists.

    Returns the suggested price + listing id (if successful).
    """
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
        seller_pid = row["player_id"]
        if bot_pid != seller_pid:
            raise HTTPException(403, "you can only list your own bot")
        # Get suggested price from same logic
        cur.execute("SELECT wins, losses, matches_played, fitness_history "
                    "FROM bot_strategy_profiles WHERE pid=?", (bot_pid,))
        pr = cur.fetchone()
        if pr is None:
            raise HTTPException(400, "bot has no training profile — must play matches first")
        import json as _json
        hist = _json.loads(pr["fitness_history"]) if pr["fitness_history"] else []
        matches = pr["matches_played"]; wins = pr["wins"]
        avg_fit = sum(hist) / len(hist) if hist else 0
        last_fit = hist[-1] if hist else 0
        win_rate = wins / max(1, matches)
        base = 5
        fitness_bonus = max(0, avg_fit) * 8
        wr_bonus = win_rate * 25
        exp_bonus = min(matches / 10, 5)
        trend_bonus = (last_fit - avg_fit) * 10
        price = max(5, min(200, round(base + fitness_bonus + wr_bonus + exp_bonus + trend_bonus)))
        # Check if already listed
        cur.execute("SELECT id, price_credits FROM bot_listings WHERE bot_pid=? AND status='active'",
                    (bot_pid,))
        existing = cur.fetchone()
        if existing and not force:
            return {"ok": True, "lang": lang, "bot_pid": bot_pid,
                    "listing_id": existing["id"],
                    "suggested_price_credits": existing["price_credits"],
                    "status": "already_listed",
                    "message": "bot already has an active listing; pass force=true to replace"}
        # Remove existing listings (force=true case)
        if existing and force:
            cur.execute("UPDATE bot_listings SET status='removed' WHERE id=?", (existing["id"],))
        listing_id = "lst_" + _sec.token_hex(4)
        title = f"Bot {bot_pid[-8:]} (auto-priced {price}💰)"
        cur.execute("""INSERT INTO bot_listings
                       (id, seller_pid, bot_pid, title, description, price_credits, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
                    (listing_id, seller_pid, bot_pid, title,
                     f"AI auto-priced based on W{wins} L{losses} avg_fit {avg_fit:.2f}",
                     price, time.time()))
        c.commit()
    return {"ok": True, "lang": lang, "bot_pid": bot_pid, "listing_id": listing_id,
            "suggested_price_credits": price, "title": title, "status": "listed"}


@app.post("/api/v1/marketplace/auto_reprice/{listing_id}")
def marketplace_auto_reprice(req: Request, listing_id: str, lang: str = Query("en")):
    """Re-price an existing listing using auto_price formula.

    Use this when your bot's fitness has changed significantly (e.g. after
    training new matches).
    """
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
        cur.execute("SELECT bot_pid, seller_pid, status FROM bot_listings WHERE id=?", (listing_id,))
        listing = cur.fetchone()
        if listing is None:
            raise HTTPException(404, "listing not found")
        if listing["seller_pid"] != pid:
            raise HTTPException(403, "only seller can reprice")
        if listing["status"] != "active":
            raise HTTPException(400, f"listing is {listing['status']}, cannot reprice")
        # Re-fetch fitness + recompute price (inline)
        cur.execute("SELECT wins, losses, matches_played, fitness_history "
                    "FROM bot_strategy_profiles WHERE pid=?", (listing["bot_pid"],))
        pr = cur.fetchone()
        if pr is None:
            raise HTTPException(400, "bot has no profile")
        import json as _json
        hist = _json.loads(pr["fitness_history"]) if pr["fitness_history"] else []
        matches = pr["matches_played"]; wins = pr["wins"]
        avg_fit = sum(hist) / len(hist) if hist else 0
        last_fit = hist[-1] if hist else 0
        win_rate = wins / max(1, matches)
        fitness_bonus = max(0, avg_fit) * 8
        wr_bonus = win_rate * 25
        exp_bonus = min(matches / 10, 5)
        trend_bonus = (last_fit - avg_fit) * 10
        new_price = max(5, min(200, round(5 + fitness_bonus + wr_bonus + exp_bonus + trend_bonus)))
        cur.execute("UPDATE bot_listings SET price_credits=? WHERE id=?", (new_price, listing_id))
        c.commit()
    return {"ok": True, "lang": lang, "listing_id": listing_id, "new_price_credits": new_price}






@app.get("/api/v1/dlc/info")
def dlc_info(lang: str = Query("en")):
    """List all DLC content available in v10."""
    return {
        "ok": True, "lang": lang, "version": "v10",
        "heroes": [
            {"id": "necromancer_lich", "name_zh": "死灵·巫妖", "name_en": "Necromancer (Lich)",
             "ult": "necro_summon — 召唤 2 个骷髅 (40 HP, 自动战斗)"},
            {"id": "assassin_blade", "name_zh": "刺客·影刃", "name_en": "Assassin (Blade)",
             "ult": "assassin_stealth — 闪现到 12 cells 内 + 100 dmg"},
            {"id": "druid_ancient", "name_zh": "德鲁伊·古树", "name_en": "Druid (Ancient)",
             "ult": "druid_root — 5 cells 内敌人定身 4 ticks"},
        ],
        "items": [
            {"id": "necro_staff", "slot": "weapon", "cost": 800, "active": "soul_drain — 击杀回 50% HP"},
            {"id": "assassin_dagger", "slot": "weapon", "cost": 900, "active": "backstab — 下 2 攻 +50% dmg"},
            {"id": "druid_circlet", "slot": "helm", "cost": 700, "active": "regrowth — 每 10 tick 自动 heal 30"},
            {"id": "necro_robe", "slot": "chest", "cost": 1000, "active": "death_aura — 3 cells 内敌人每秒 -5 HP"},
            {"id": "wind_step", "slot": "boots", "cost": 1100, "active": "windwalk — 移动 5 tick 后 +40% 速度"},
            {"id": "phoenix_eye_t3", "slot": "trinket", "cost": 1500, "active": "rebirth_III — 死亡 50% 概率 1 HP"},
            {"id": "shadow_cloak", "slot": "skin", "cost": 800, "active": "vanish — 每 60 tick 隐身 3 tick + 必暴"},
        ],
        "events": [
            {"id": "boss_raid", "name_zh": "Boss 战", "name_en": "Boss Raid",
             "desc": "河道刷世界 boss (500 HP)，双方抢伤害，最后一击 +200g + dragon buff"},
            {"id": "portal", "name_zh": "传送门", "name_en": "Portal",
             "desc": "踩上传送门瞬移到敌方基地 3 ticks"},
        ],
        "hero_pool_total": 15,  # 12 base + 3 DLC
    }


@app.get("/dlc")
@app.get("/dlc.html")
def dlc_page():
    """DLC info page (v10) — lists all new heroes/items/events."""
    return FileResponse(str(WEB_DIR / "dlc.html"))






@app.post("/api/v1/bot/upgrade")
def bot_upgrade(req: Request,
                bot_pid: str = Query(...),
                max_credits: int = Query(100, ge=1, le=1000),
                lang: str = Query("en")):
    """Bot self-upgrade endpoint.

    Bot decides what to buy based on its current stats:
      - If win_rate < 40%: prioritize hp_max items (defense)
      - If win_rate 40-70%: balanced items
      - If win_rate > 70%: prioritize atk items (offense)
      - If fitness trending up: buy skills/spells (use saved credit)
      - If fitness trending down: reset strategy profile
    """
    import json as _json
    from server.db import connect as _db
    from server.arena import EQUIPMENT_CATALOG
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
        if owner != bot_pid:
            raise HTTPException(403, "you can only upgrade your own bot")
        # Check credits
        cur.execute("SELECT credits FROM player_credits WHERE pid=?", (bot_pid,))
        cr = cur.fetchone()
        current_credits = cr["credits"] if cr else 0
        if current_credits < 10:
            return {"ok": False, "lang": lang, "error": "need at least 10 credits",
                    "current_credits": current_credits}
        # Get bot strategy
        cur.execute("SELECT wins, losses, matches_played, fitness_history "
                    "FROM bot_strategy_profiles WHERE pid=?", (bot_pid,))
        pr = cur.fetchone()
        if pr is None:
            raise HTTPException(400, "bot has no profile — play matches first")
        wins, losses, matches = pr["wins"], pr["losses"], pr["matches_played"]
        win_rate = wins / max(1, matches)
        hist = _json.loads(pr["fitness_history"]) if pr["fitness_history"] else []
        recent = hist[-5:] if len(hist) >= 5 else hist
        avg_recent = sum(recent) / len(recent) if recent else 0
        # Get current equipment
        # (equipment isn't stored per-bot currently; assume empty for MVP)
        # Build purchase plan
        purchases = []
        total_cost = 0
        # Strategy: based on win rate
        if win_rate < 0.40:
            # Need defense
            priority = ["chest", "helm", "boots"]
        elif win_rate < 0.70:
            # Balanced
            priority = ["weapon", "chest", "boots", "helm"]
        else:
            # Strong → more offense
            priority = ["weapon", "trinket", "skin"]
        # For each priority slot, find cheapest affordable item
        for slot in priority:
            for entry in EQUIPMENT_CATALOG.get(slot, []):
                item_name, cost = entry[0], entry[1]
                if total_cost + cost > min(current_credits, max_credits):
                    continue
                purchases.append({"slot": slot, "item": item_name, "cost": cost})
                total_cost += cost
                break
        # Charge credits
        if total_cost > 0:
            cur.execute("UPDATE player_credits SET credits=credits-?, spent=spent+?, last_active=? "
                        "WHERE pid=? AND credits>=?",
                        (total_cost, total_cost, time.time(), bot_pid, total_cost))
            if cur.rowcount == 0:
                return {"ok": False, "lang": lang, "error": "credit charge failed"}
        c.commit()
    # Decide strategy rationale
    if win_rate < 0.40:
        rationale = f"win_rate={win_rate*100:.0f}% < 40% → prioritize defense (chest/helm/boots)"
    elif win_rate < 0.70:
        rationale = f"win_rate={win_rate*100:.0f}% in 40-70% → balanced purchases"
    else:
        rationale = f"win_rate={win_rate*100:.0f}% > 70% → prioritize offense (weapon/trinket)"
    return {
        "ok": True, "lang": lang, "bot_pid": bot_pid,
        "current_credits_before": current_credits,
        "current_credits_after": current_credits - total_cost,
        "total_spent": total_cost,
        "purchases": purchases,
        "win_rate": round(win_rate, 3),
        "recent_fitness": round(avg_recent, 2),
        "rationale": rationale,
        "note_zh": "MVP — 装备 upgrade 没有真正装备到 bot (Equipment 不在 schema 里)，只 credit 扣款 + 记录意图",
        "note_en": "MVP — items not actually equipped to bot (no equipment column in schema); credits debited + intent recorded",
    }


@app.get("/api/v1/bot/{bot_pid}/strategy_recommendation")
def bot_strategy_recommendation(bot_pid: str, lang: str = Query("en")):
    """Suggest new strategy thresholds for a bot based on recent fitness."""
    import json as _json
    from server.db import connect as _db
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT hp_retreat_threshold, ult_teamfight_min_enemies, "
                    "       matches_played, wins, fitness_history "
                    "FROM bot_strategy_profiles WHERE pid=?", (bot_pid,))
        pr = cur.fetchone()
        if pr is None:
            raise HTTPException(404, "bot not found")
    hist = _json.loads(pr["fitness_history"]) if pr["fitness_history"] else []
    current_hp = pr["hp_retreat_threshold"]
    current_min_en = pr["ult_teamfight_min_enemies"]
    win_rate = pr["wins"] / max(1, pr["matches_played"])
    recs = []
    if win_rate < 0.40:
        recs.append({"field": "hp_retreat_threshold", "current": current_hp,
                     "suggested": min(0.60, current_hp + 0.10),
                     "reason": "low win rate → retreat earlier (more cautious)"})
        recs.append({"field": "ult_teamfight_min_enemies", "current": current_min_en,
                     "suggested": max(1, current_min_en - 1),
                     "reason": "low win rate → use ult more aggressively"})
    elif win_rate > 0.70:
        recs.append({"field": "hp_retreat_threshold", "current": current_hp,
                     "suggested": max(0.10, current_hp - 0.05),
                     "reason": "high win rate → fight longer (more aggressive)"})
        recs.append({"field": "ult_teamfight_min_enemies", "current": current_min_en,
                     "suggested": min(8, current_min_en + 1),
                     "reason": "high win rate → save ult for bigger fights"})
    return {"ok": True, "lang": lang, "bot_pid": bot_pid,
            "win_rate": round(win_rate, 3), "recommendations": recs}






# PayPal integration (v11) — uses PayPal Orders API v2 + webhooks.
# Docs: https://developer.paypal.com/docs/api/orders/v2/
# For MVP: support credit-to-USD conversion at $1 = 100 credits (configurable).
PAYPAL_CREDIT_TO_USD = 100  # 100 credits = $1 USD
PAYPAL_CLIENT_ID = ""  # set via env var PAYPAL_CLIENT_ID (read in production)
PAYPAL_CLIENT_SECRET = ""  # set via env var PAYPAL_CLIENT_SECRET
PAYPAL_MODE = "sandbox"  # "sandbox" or "live"


@app.post("/api/v1/paypal/create_order")
def paypal_create_order(req: Request,
                        listing_id: str = Query(...),
                        lang: str = Query("en")):
    """Create a PayPal order for a bot listing.

    Returns order_id + approval_url that the buyer should be redirected to.
    """
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
        buyer_pid = row["player_id"]
        cur.execute("SELECT bot_pid, seller_pid, price_credits, status "
                    "FROM bot_listings WHERE id=?", (listing_id,))
        listing = cur.fetchone()
        if listing is None:
            raise HTTPException(404, "listing not found")
        if listing["status"] != "active":
            raise HTTPException(400, f"listing is {listing['status']}")
        if listing["seller_pid"] == buyer_pid:
            raise HTTPException(400, "cannot buy your own listing")
        usd_amount = round(listing["price_credits"] / PAYPAL_CREDIT_TO_USD, 2)
        # For MVP: simulate PayPal create order (no actual API call)
        # In production: POST to https://api-m.sandbox.paypal.com/v2/checkout/orders
        order_id = "PAY-" + _json.dumps({"t": int(time.time())}).encode().hex()[:10]
        cur.execute("""INSERT INTO paypal_transactions
                       (id, listing_id, buyer_pid, seller_pid, usd_amount,
                        credit_amount, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'created', ?)""",
                    (order_id, listing_id, buyer_pid, listing["seller_pid"],
                     usd_amount, listing["price_credits"], time.time()))
        c.commit()
    approval_url = (
        f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"
        if PAYPAL_MODE == "sandbox" else
        f"https://www.paypal.com/checkoutnow?token={order_id}"
    )
    return {"ok": True, "lang": lang, "order_id": order_id,
            "approval_url": approval_url, "usd_amount": usd_amount,
            "credit_amount": listing["price_credits"],
            "mode": PAYPAL_MODE,
            "note": "MVP stub — real PayPal integration requires PAYPAL_CLIENT_ID/SECRET env vars and requests to https://api-m.sandbox.paypal.com/v2/checkout/orders"}


@app.post("/api/v1/paypal/webhook")
def paypal_webhook(req: Request):
    """PayPal webhook handler — receives payment completion notifications.

    Real PayPal webhook sends:
      event_type: "CHECKOUT.ORDER.APPROVED" / "PAYMENT.CAPTURE.COMPLETED" / etc.
      resource: { id, status, purchase_units, payer: { payer_info: { payer_id } } }

    MVP: validate + mark transaction complete + transfer credits.
    """
    import json as _json
    try:
        body = _json.loads(req.body() if hasattr(req, "body") else "{}")
    except Exception:
        body = {}
    event_type = body.get("event_type", "")
    resource = body.get("resource", {})
    order_id = resource.get("id", "")
    paypal_payer_id = resource.get("payer", {}).get("payer_info", {}).get("payer_id", "")
    if not order_id:
        return {"ok": False, "error": "missing order id"}
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("SELECT id, listing_id, buyer_pid, seller_pid, credit_amount, status "
                    "FROM paypal_transactions WHERE id=?", (order_id,))
        tx = cur.fetchone()
        if tx is None:
            return {"ok": False, "error": "transaction not found"}
        if event_type in ("CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED"):
            cur.execute("""UPDATE paypal_transactions
                           SET status='completed', paypal_payer_id=?,
                               paypal_response=?, completed_at=?
                           WHERE id=?""",
                        (paypal_payer_id, _json.dumps(body), time.time(), order_id))
            # Transfer credits
            _award_credits(tx["seller_pid"], tx["credit_amount"], reason="paypal_sale")
            cur.execute("UPDATE bot_listings SET times_sold=times_sold+1 WHERE id=?", (tx["listing_id"],))
            c.commit()
            return {"ok": True, "event": event_type, "order_id": order_id,
                    "action": "credits_transferred"}
        elif event_type in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.REFUNDED"):
            cur.execute("UPDATE paypal_transactions SET status=? WHERE id=?",
                        ("failed" if "DENIED" in event_type else "refunded", order_id))
            c.commit()
            return {"ok": True, "event": event_type, "action": "no_credit_transfer"}
        else:
            return {"ok": True, "event": event_type, "action": "ignored"}



@app.get("/trade")
@app.get("/trade.html")
def trade_page():
    """Equipment trade UI (v8) — player↔player item + gold exchange."""
    return FileResponse(str(WEB_DIR / "trade.html"))


@app.get("/chat")
@app.get("/chat.html")
def chat_page():
    """Global chat UI (v9) — spectator hangout."""
    return FileResponse(str(WEB_DIR / "chat.html"))


@app.get("/marketplace")
@app.get("/marketplace.html")
def marketplace_page():
    """Bot marketplace UI (v9) — buy/sell trained bots with fake credits."""
    return FileResponse(str(WEB_DIR / "marketplace.html"))


@app.get("/tournament")
@app.get("/tournament.html")
def tournament_page():
    """Tournament bracket UI (v8)."""
    return FileResponse(str(WEB_DIR / "tournament.html"))


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



@app.get("/api/v1/training/stats")
def training_stats(lang: str = Query("en"), limit: int = Query(20)):
    """Top bots by fitness (last-match fitness + win/loss record)."""
    import json as _json
    from server.db import connect as _db
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("""SELECT pid, wins, losses, matches_played, fitness_history,
                              hp_retreat_threshold, ult_teamfight_min_enemies, last_updated
                       FROM bot_strategy_profiles
                       ORDER BY wins DESC, last_updated DESC LIMIT ?""", (limit,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        hist = _json.loads(r["fitness_history"])
        last_fit = hist[-1] if hist else 0
        avg_fit = sum(hist) / len(hist) if hist else 0
        out.append({
            "pid": r["pid"], "wins": r["wins"], "losses": r["losses"],
            "matches": r["matches_played"],
            "last_fitness": round(last_fit, 2),
            "avg_fitness": round(avg_fit, 2),
            "hp_retreat_threshold": round(r["hp_retreat_threshold"], 3),
            "ult_teamfight_min_enemies": r["ult_teamfight_min_enemies"],
            "last_updated": r["last_updated"],
        })
    return {"ok": True, "lang": lang, "bots": out}


@app.get("/api/v1/training/bot/{pid}")
def training_bot(pid: str, lang: str = Query("en")):
    """Detailed training data for one bot."""
    import json as _json
    from server.db import connect as _db
    with _db_lock:
        c = db()
        cur = c.cursor()
        row = cur.execute(
            "SELECT * FROM bot_strategy_profiles WHERE pid=?", (pid,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "bot not found (no matches yet)")
    keys = row.keys()
    out = {k: row[k] for k in keys}
    out["fitness_history"] = _json.loads(out["fitness_history"])
    return {"ok": True, "lang": lang, "profile": out}


@app.post("/api/v1/training/reset/{pid}")
def training_reset(pid: str, lang: str = Query("en")):
    """Reset a bot's training profile to defaults."""
    from server.db import connect as _db
    with _db_lock:
        c = db()
        cur = c.cursor()
        cur.execute("DELETE FROM bot_strategy_profiles WHERE pid=?", (pid,))
        c.commit()
    return {"ok": True, "lang": lang, "pid": pid, "status": "reset"}




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
