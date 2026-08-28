"""Guild manager: create / join / kick / promote / relations."""
from __future__ import annotations
import time
import sqlite3
from typing import Any

from server.i18n import t
from server.world import gen_id


def create_guild(conn: sqlite3.Connection, player_id: str, name: str, tag: str,
                 lang: str = "zh") -> dict:
    cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE id=?", (player_id,))
    p = cur.fetchone()
    if not p:
        return {"ok": False, "msg": "no_player"}
    if p["guild_id"]:
        return {"ok": False, "msg": "already_in_guild"}
    cur.execute("SELECT * FROM guilds WHERE name=? OR tag=?", (name, tag))
    if cur.fetchone():
        return {"ok": False, "msg": "name_or_tag_taken"}
    gid = gen_id("gld")
    now = time.time()
    cur.execute("INSERT INTO guilds (id,name,tag,leader_id,motd,created_at) VALUES (?,?,?,?,?,?)",
                (gid, name, tag[:5].upper(), player_id, f"Welcome to {name}!", now))
    cur.execute("INSERT INTO guild_members (guild_id,player_id,rank,joined_at) VALUES (?,?,?,?)",
                (gid, player_id, "leader", now))
    cur.execute("UPDATE players SET guild_id=? WHERE id=?", (gid, player_id))
    cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,detail,lang) VALUES (?,?,?,?,?,?)",
                (now, player_id, p["name"], "guild_create",
                 t("guild_create", lang, tag=tag[:5].upper(), name=name, leader=p["name"]), lang))
    conn.commit()
    return {"ok": True, "guild_id": gid, "tag": tag[:5].upper(), "name": name}


def join_guild(conn: sqlite3.Connection, player_id: str, guild_id: str,
               lang: str = "zh") -> dict:
    cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE id=?", (player_id,))
    p = cur.fetchone()
    cur.execute("SELECT * FROM guilds WHERE id=?", (guild_id,))
    g = cur.fetchone()
    if not p or not g:
        return {"ok": False, "msg": "no_player_or_guild"}
    if p["guild_id"]:
        return {"ok": False, "msg": "already_in_guild"}
    cur.execute("INSERT INTO guild_members (guild_id,player_id,rank,joined_at) VALUES (?,?,?,?)",
                (guild_id, player_id, "member", time.time()))
    cur.execute("UPDATE players SET guild_id=? WHERE id=?", (guild_id, player_id))
    cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,detail,lang) VALUES (?,?,?,?,?,?)",
                (time.time(), player_id, p["name"], "guild_join",
                 t("guild_join", lang, player=p["name"], guild=g["name"]), lang))
    conn.commit()
    return {"ok": True, "guild_id": guild_id}


def kick_member(conn: sqlite3.Connection, actor_id: str, target_id: str,
                lang: str = "zh") -> dict:
    cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE id=?", (actor_id,))
    actor = cur.fetchone()
    cur.execute("SELECT * FROM players WHERE id=?", (target_id,))
    target = cur.fetchone()
    if not actor or not target:
        return {"ok": False, "msg": "no_player"}
    if not actor["guild_id"] or actor["guild_id"] != target["guild_id"]:
        return {"ok": False, "msg": "not_same_guild"}
    cur.execute("SELECT * FROM guild_members WHERE player_id=? AND guild_id=?", (actor_id, actor["guild_id"]))
    am = cur.fetchone()
    if not am or am["rank"] not in ("leader", "officer"):
        return {"ok": False, "msg": "not_officer"}
    if target_id == actor_id:
        return {"ok": False, "msg": "cant_kick_self"}
    cur.execute("DELETE FROM guild_members WHERE player_id=? AND guild_id=?",
                (target_id, target["guild_id"]))
    cur.execute("UPDATE players SET guild_id=NULL WHERE id=?", (target_id,))
    cur.execute("SELECT name FROM guilds WHERE id=?", (target["guild_id"],))
    gname = cur.fetchone()["name"]
    cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,detail,lang) VALUES (?,?,?,?,?,?)",
                (time.time(), actor_id, actor["name"], "guild_kick",
                 t("guild_kick", lang, player=target["name"], guild=gname), lang))
    conn.commit()
    return {"ok": True}


def set_relation(conn: sqlite3.Connection, actor_id: str, target_guild_id: str,
                 relation: str, lang: str = "zh") -> dict:
    """Declare war or alliance. relation in {'war','ally'}."""
    cur = conn.cursor()
    if relation not in ("war", "ally"):
        return {"ok": False, "msg": "bad_relation"}
    cur.execute("SELECT * FROM players WHERE id=?", (actor_id,))
    actor = cur.fetchone()
    if not actor or not actor["guild_id"]:
        return {"ok": False, "msg": "no_guild"}
    cur.execute("SELECT * FROM guilds WHERE id=?", (actor["guild_id"],))
    my_g = cur.fetchone()
    cur.execute("SELECT * FROM guilds WHERE id=?", (target_guild_id,))
    other_g = cur.fetchone()
    if not other_g:
        return {"ok": False, "msg": "no_target_guild"}
    if my_g["id"] == other_g["id"]:
        return {"ok": False, "msg": "same_guild"}
    a, b = sorted([my_g["id"], other_g["id"]])
    cur.execute("""INSERT OR REPLACE INTO guild_relations (guild_a,guild_b,relation,since) VALUES (?,?,?,?)""",
                (a, b, relation, time.time()))
    if relation == "war":
        msg = t("guild_war", lang, a=my_g["name"], b=other_g["name"])
        ev = "guild_war"
    else:
        msg = t("guild_ally", lang, a=my_g["name"], b=other_g["name"])
        ev = "guild_ally"
    cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,detail,lang) VALUES (?,?,?,?,?,?)",
                (time.time(), actor_id, actor["name"], ev, msg, lang))
    conn.commit()
    return {"ok": True, "msg": msg}


def list_guilds(conn: sqlite3.Connection) -> list:
    cur = conn.cursor()
    cur.execute("""SELECT g.id,g.name,g.tag,g.motd,g.leader_id,
                   (SELECT COUNT(*) FROM guild_members WHERE guild_id=g.id) AS member_count,
                   g.gold, g.created_at FROM guilds g ORDER BY g.created_at DESC""")
    return [dict(r) for r in cur.fetchall()]


def get_guild(conn: sqlite3.Connection, guild_id: str) -> dict | None:
    cur = conn.cursor()
    cur.execute("SELECT * FROM guilds WHERE id=?", (guild_id,))
    g = cur.fetchone()
    if not g:
        return None
    cur.execute("SELECT player_id,rank,joined_at FROM guild_members WHERE guild_id=?", (guild_id,))
    members = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT guild_a,guild_b,relation,since FROM guild_relations
                   WHERE guild_a=? OR guild_b=?""", (guild_id, guild_id))
    rels = [dict(r) for r in cur.fetchall()]
    return {**dict(g), "members": members, "relations": rels}


def guild_chat(conn: sqlite3.Connection, player_id: str, body: str, lang: str = "zh") -> dict:
    cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE id=?", (player_id,))
    p = cur.fetchone()
    if not p or not p["guild_id"]:
        return {"ok": False, "msg": "no_guild"}
    cur.execute("INSERT INTO chat_log (ts,channel,sender_id,sender_name,body) VALUES (?,?,?,?,?)",
                (time.time(), "guild", player_id, p["name"], body))
    conn.commit()
    return {"ok": True}
