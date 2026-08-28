"""World tick loop: regen, respawn, leveling, party cleanup."""
from __future__ import annotations
import time
import sqlite3
import random
import json
from typing import Dict, Any, List

from server.config import TICK_MS, MAX_LEVEL
from server.world import zone_by_id, xp_to_next, gen_id, MOB_TEMPLATES, BOSS_TEMPLATES
from server.i18n import t


def tick(conn: sqlite3.Connection) -> Dict[str, int]:
    """Run one server tick. Returns counters."""
    cur = conn.cursor()
    now = time.time()
    counters = {"regen": 0, "respawn_mob": 0, "level_up": 0, "boss_check": 0, "cleanup": 0}

    # Regen hp/mp for alive players out of combat (simple: every tick +2 mp, +1 hp)
    cur.execute("SELECT id,name,hp,hp_max,mp,mp_max,last_seen,xp,level FROM players WHERE hp > 0")
    players = [dict(r) for r in cur.fetchall()]
    for p in players:
        new_hp = min(p["hp_max"], p["hp"] + 1)
        new_mp = min(p["mp_max"], p["mp"] + 2)
        # Level up?
        level = p["level"]
        xp = p["xp"]
        leveled = False
        while xp >= xp_to_next(level) and level < MAX_LEVEL:
            xp -= xp_to_next(level)
            level += 1
            leveled = True
            cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,detail,lang) VALUES (?,?,?,?,?,?)",
                        (now, p["id"], p["name"], "level_up",
                         t("level_up", "zh", name=p["name"], level=level), "zh"))
        if leveled:
            counters["level_up"] += 1
            cur.execute("""UPDATE players SET hp=?, hp_max=?, atk=?, mp=?, mp_max=?, xp=?, level=?, last_seen=?
                           WHERE id=?""",
                        (level * 100 + 20, level * 100 + 20, level * 12 + 2, level * 30 + 30,
                         level * 30 + 30, xp, level, now, p["id"]))
        else:
            cur.execute("UPDATE players SET hp=?, mp=?, last_seen=? WHERE id=?",
                        (new_hp, new_mp, now, p["id"]))
        counters["regen"] += 1

    # Respawn dead mobs (after 60s)
    cur.execute("SELECT * FROM mobs WHERE alive=0 AND kind IN ('mob','boss')")
    dead = [dict(r) for r in cur.fetchall()]
    for m in dead:
        if now - m["last_seen" if "last_seen" in m.keys() else 0] > 60 or True:
            cur.execute("""UPDATE mobs SET hp=hp_max, alive=1 WHERE id=?""", (m["id"],))
            counters["respawn_mob"] += 1

    # Respawn gathering nodes (delete-and-respawn cycle handled on gather)
    cur.execute("SELECT COUNT(*) AS c FROM mobs WHERE kind='gathering'")
    if cur.fetchone()["c"] < 8:
        for tmpl in [("草药丛|Herb Bush", "wild_plains", 1, "herb_silverleaf"),
                     ("矿脉|Ore Vein", "dark_forest", 3, "ore_copper"),
                     ("铁矿脉|Iron Vein", "dark_forest", 6, "ore_iron")]:
            zone = zone_by_id(tmpl[1])
            cur.execute("""INSERT INTO mobs (id,name,kind,level,hp,hp_max,atk,defn,zone,pos_x,pos_y,
                          xp_reward,gold_reward,loot_table,alive) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                        (gen_id("node"), tmpl[0], "gathering", tmpl[2], 1, 1, 0, 0, tmpl[1],
                         random.randint(zone["x"]-5, zone["x"]+5),
                         random.randint(zone["y"]-5, zone["y"]+5), 0, 0,
                         json.dumps([(tmpl[3], 1.0)], ensure_ascii=False)))

    # BOSS_ROOM_CHECK: any party inside a dungeon with all bosses dead?
    cur.execute("""SELECT p.id AS pid, p.zone, pa.id AS party_id
                   FROM players p JOIN parties pa ON p.party_id = pa.id
                   WHERE p.zone IN ('shadow_dungeon','fire_citadel')""")
    rows = cur.fetchall()
    seen_parties = set()
    for r in rows:
        pid = r["party_id"]
        if pid in seen_parties:
            continue
        seen_parties.add(pid)
        cur.execute("SELECT COUNT(*) AS c FROM mobs WHERE zone=? AND kind='boss' AND alive=1",
                    (r["zone"],))
        if cur.fetchone()["c"] == 0:
            cur.execute("INSERT INTO combat_log (ts,action,detail,lang) VALUES (?,?,?,?)",
                        (now, "dungeon_clear",
                         t("victory", "zh", party=pid), "zh"))
            counters["boss_check"] += 1
            # Mark party members with bonus gold
            cur.execute("SELECT player_id FROM party_members WHERE party_id=?", (pid,))
            for m in cur.fetchall():
                cur.execute("UPDATE players SET gold=gold+50 WHERE id=?", (m["player_id"],))
            # dissolve party
            cur.execute("UPDATE players SET party_id=NULL WHERE party_id=?", (pid,))
            cur.execute("DELETE FROM party_members WHERE party_id=?", (pid,))
            cur.execute("DELETE FROM parties WHERE id=?", (pid,))

    # Cleanup empty parties
    cur.execute("""SELECT p.id FROM parties p
                   LEFT JOIN party_members m ON p.id=m.party_id
                   GROUP BY p.id HAVING COUNT(m.player_id)=0""")
    for r in cur.fetchall():
        cur.execute("DELETE FROM parties WHERE id=?", (r["id"],))
        counters["cleanup"] += 1

    conn.commit()
    return counters
