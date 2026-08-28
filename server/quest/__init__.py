"""Quest templates and manager."""
from __future__ import annotations
import json
import time
import sqlite3
from typing import Dict, Any, List

from server.i18n import t
from server.world import gen_id, item_name


QUEST_TEMPLATES: List[Dict[str, Any]] = [
    {"id": "q_kill_wolves", "name_zh": "清除狼患", "name_en": "Cull the Wolves",
     "zone": "wild_plains", "level": 1, "target_kind": "mob", "target_zone": "wild_plains",
     "kills_required": 5, "reward_gold": 30, "reward_xp": 80,
     "narrative_zh": "平原上的狼群威胁村民, 请消灭5只草原狼.",
     "narrative_en": "Wolves threaten the village. Slay 5 Plains Wolves."},
    {"id": "q_gather_herbs", "name_zh": "采集银叶草", "name_en": "Gather Silverleaf",
     "zone": "wild_plains", "level": 1, "gather_item": "herb_silverleaf",
     "qty_required": 4, "reward_gold": 15, "reward_xp": 40,
     "narrative_zh": "炼金师需要4株银叶草.",
     "narrative_en": "The alchemist needs 4 Silverleaf herbs."},
    {"id": "q_kill_giant", "name_zh": "击退石巨人", "name_en": "Drive Back the Stone Giant",
     "zone": "dark_forest", "level": 4, "target_kind": "mob", "target_zone": "dark_forest",
     "kills_required": 2, "reward_gold": 80, "reward_xp": 250,
     "narrative_zh": "森林深处的石巨人正在毁坏村庄.",
     "narrative_en": "Stone Giants ravage the deep forest."},
    {"id": "q_dungeon_boss", "name_zh": "讨伐暗影领主", "name_en": "Slay the Shadow Lord",
     "zone": "shadow_dungeon", "level": 5, "target_kind": "boss", "target_zone": "shadow_dungeon",
     "kills_required": 1, "reward_gold": 200, "reward_xp": 600,
     "narrative_zh": "组队挑战暗影副本的暗影领主.",
     "narrative_en": "Party up and challenge the Shadow Lord in the Shadow Dungeon."},
]


def quest_name(qid: str, lang: str = "zh") -> str:
    for q in QUEST_TEMPLATES:
        if q["id"] == qid:
            if lang == "en":
                return q["name_en"]
            if lang == "zh_only":
                return q["name_zh"]
            return f"{q['name_zh']} | {q['name_en']}"
    return qid


def available_quests(level: int, zone: str, lang: str = "zh") -> List[Dict[str, Any]]:
    out = []
    for q in QUEST_TEMPLATES:
        if q["level"] <= level + 1 and q.get("zone") == zone:
            item = dict(q)
            item["name"] = quest_name(q["id"], lang)
            out.append(item)
    return out


def accept_quest(conn: sqlite3.Connection, player_id: str, template_id: str,
                 lang: str = "zh") -> dict:
    cur = conn.cursor()
    tmpl = next((q for q in QUEST_TEMPLATES if q["id"] == template_id), None)
    if not tmpl:
        return {"ok": False, "msg": "no_template"}
    cur.execute("SELECT * FROM quests WHERE player_id=? AND template_id=? AND state='active'",
                (player_id, template_id))
    if cur.fetchone():
        return {"ok": False, "msg": "already_active"}
    cur.execute("SELECT name FROM players WHERE id=?", (player_id,))
    p = cur.fetchone()
    qid = gen_id("quest")
    cur.execute("""INSERT INTO quests (id,player_id,template_id,state,progress,reward_gold,reward_xp,accepted_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (qid, player_id, template_id, "active", "{}", tmpl["reward_gold"], tmpl["reward_xp"], time.time()))
    cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,detail,lang) VALUES (?,?,?,?,?,?)",
                (time.time(), player_id, p["name"], "quest_accept",
                 t("quest_accept", lang, p=p["name"], quest=quest_name(template_id, lang)), lang))
    conn.commit()
    return {"ok": True, "quest_id": qid, "template_id": template_id}


def complete_quest(conn: sqlite3.Connection, player_id: str, quest_id: str,
                   lang: str = "zh") -> dict:
    cur = conn.cursor()
    cur.execute("SELECT * FROM quests WHERE id=? AND player_id=?", (quest_id, player_id))
    q = cur.fetchone()
    if not q:
        return {"ok": False, "msg": "no_quest"}
    if q["state"] != "active":
        return {"ok": False, "msg": "not_active"}
    tmpl = next((tq for tq in QUEST_TEMPLATES if tq["id"] == q["template_id"]), None)
    prog = json.loads(q["progress"] or "{}")
    ok = False
    if "kills_required" in tmpl:
        if prog.get("kills", 0) >= tmpl["kills_required"]:
            ok = True
    if "qty_required" in tmpl:
        cur.execute("SELECT SUM(qty) AS s FROM inventory WHERE player_id=? AND item_id=?",
                    (player_id, tmpl["gather_item"]))
        row = cur.fetchone()
        if (row["s"] or 0) >= tmpl["qty_required"]:
            ok = True
            # consume items
            cur.execute("""UPDATE inventory SET qty=qty-? WHERE player_id=? AND item_id=? AND qty>=?
                           ORDER BY id LIMIT 1""",
                        (tmpl["qty_required"], player_id, tmpl["gather_item"], tmpl["qty_required"]))
    if not ok:
        return {"ok": False, "msg": "objective_not_met"}
    cur.execute("UPDATE quests SET state='complete' WHERE id=?", (quest_id,))
    cur.execute("UPDATE players SET gold=gold+?, xp=xp+? WHERE id=?",
                (q["reward_gold"], q["reward_xp"], player_id))
    cur.execute("SELECT name FROM players WHERE id=?", (player_id,))
    pname = cur.fetchone()["name"]
    cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,detail,lang) VALUES (?,?,?,?,?,?)",
                (time.time(), player_id, pname, "quest_complete",
                 t("quest_complete", lang, p=pname, quest=quest_name(q["template_id"], lang),
                   gold=q["reward_gold"], xp=q["reward_xp"]), lang))
    conn.commit()
    return {"ok": True, "reward_gold": q["reward_gold"], "reward_xp": q["reward_xp"]}


def active_quests(conn: sqlite3.Connection, player_id: str, lang: str = "zh") -> list:
    cur = conn.cursor()
    cur.execute("SELECT * FROM quests WHERE player_id=? AND state='active'", (player_id,))
    out = []
    for r in cur.fetchall():
        d = dict(r)
        d["name"] = quest_name(d["template_id"], lang)
        tmpl = next((tq for tq in QUEST_TEMPLATES if tq["id"] == d["template_id"]), {})
        d["objective"] = tmpl
        out.append(d)
    return out
