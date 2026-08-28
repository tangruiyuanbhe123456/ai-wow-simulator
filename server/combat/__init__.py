"""Combat engine and skill definitions."""
from __future__ import annotations
import random
import json
import time
import sqlite3
from typing import Dict, Any, List, Tuple

from server.config import (
    BOSS_BASE_HP, BOSS_BASE_ATK, BOSS_CRIT_MULT, PARTY_BONUS_PER_MEMBER,
)
from server.world import (
    level_to_hp, level_to_atk, gen_id, item_name, zone_by_id,
)
from server.i18n import t, name as i18n_name


# ---- Skill catalog ----------------------------------------------------------

SKILLS: Dict[str, Dict[str, Any]] = {
    # Warrior
    "heroic_strike": {"name_zh": "英勇打击", "name_en": "Heroic Strike", "cls": "warrior",
                      "dmg_mult": 1.4, "cost": 0, "cd": 1, "kind": "melee"},
    "cleave": {"name_zh": "顺劈斩", "name_en": "Cleave", "cls": "warrior",
               "dmg_mult": 1.2, "cost": 5, "cd": 2, "kind": "melee", "aoe": True},
    "shield_block": {"name_zh": "盾牌格挡", "name_en": "Shield Block", "cls": "warrior",
                     "dmg_mult": 0.4, "cost": 8, "cd": 3, "kind": "buff", "buff_defn": 8},
    "rallying_cry": {"name_zh": "怒吼战吼", "name_en": "Rallying Cry", "cls": "warrior",
                     "dmg_mult": 0.0, "cost": 15, "cd": 5, "kind": "buff", "buff_atk": 6, "party": True},

    # Mage
    "fireball": {"name_zh": "火球术", "name_en": "Fireball", "cls": "mage",
                 "dmg_mult": 1.6, "cost": 12, "cd": 1, "kind": "spell"},
    "frostbolt": {"name_zh": "寒冰箭", "name_en": "Frostbolt", "cls": "mage",
                  "dmg_mult": 1.4, "cost": 10, "cd": 1, "kind": "spell"},
    "arcane_blast": {"name_zh": "奥术冲击", "name_en": "Arcane Blast", "cls": "mage",
                     "dmg_mult": 2.0, "cost": 20, "cd": 3, "kind": "spell"},
    "ice_block": {"name_zh": "寒冰屏障", "name_en": "Ice Block", "cls": "mage",
                  "dmg_mult": 0.0, "cost": 25, "cd": 8, "kind": "buff", "buff_invuln": 1},

    # Priest
    "holy_light": {"name_zh": "圣光术", "name_en": "Holy Light", "cls": "priest",
                   "dmg_mult": 0.0, "cost": 14, "cd": 1, "kind": "heal", "heal_mult": 1.2},
    "greater_heal": {"name_zh": "强效治疗术", "name_en": "Greater Heal", "cls": "priest",
                     "dmg_mult": 0.0, "cost": 25, "cd": 3, "kind": "heal", "heal_mult": 2.0},
    "shadow_word_pain": {"name_zh": "暗言术:痛", "name_en": "Shadow Word: Pain", "cls": "priest",
                         "dmg_mult": 0.9, "cost": 12, "cd": 2, "kind": "spell", "dot": 10},
    "prayer_of_healing": {"name_zh": "治疗祷言", "name_en": "Prayer of Healing", "cls": "priest",
                          "dmg_mult": 0.0, "cost": 30, "cd": 5, "kind": "heal", "heal_mult": 1.0, "party": True},

    # Hunter
    "auto_shot": {"name_zh": "自动射击", "name_en": "Auto Shot", "cls": "hunter",
                  "dmg_mult": 1.1, "cost": 0, "cd": 1, "kind": "ranged"},
    "aimed_shot": {"name_zh": "瞄准射击", "name_en": "Aimed Shot", "cls": "hunter",
                   "dmg_mult": 1.8, "cost": 10, "cd": 2, "kind": "ranged"},
    "multi_shot": {"name_zh": "多重射击", "name_en": "Multi-Shot", "cls": "hunter",
                   "dmg_mult": 1.2, "cost": 15, "cd": 3, "kind": "ranged", "aoe": True},
    "feign_death": {"name_zh": "假死", "name_en": "Feign Death", "cls": "hunter",
                    "dmg_mult": 0.0, "cost": 5, "cd": 6, "kind": "buff", "buff_invuln": 1},

    # Monster / boss skills
    "shadow_claw": {"name_zh": "暗影爪击", "name_en": "Shadow Claw", "dmg_mult": 1.2, "kind": "melee"},
    "fire_nova": {"name_zh": "火焰新星", "name_en": "Fire Nova", "dmg_mult": 1.0, "kind": "spell", "aoe": True},
    "dragon_breath": {"name_zh": "巨龙吐息", "name_en": "Dragon Breath", "dmg_mult": 1.4, "kind": "spell", "aoe": True},
    "mob_bite": {"name_zh": "撕咬", "name_en": "Bite", "dmg_mult": 1.0, "kind": "melee"},
}


def skill_name(sid: str, lang: str = "zh") -> str:
    s = SKILLS.get(sid)
    if not s:
        return sid
    if lang == "en":
        return s["name_en"]
    if lang == "zh_only":
        return s["name_zh"]
    return f"{s['name_zh']} | {s['name_en']}"


def list_skills_for_class(cls: str) -> List[str]:
    return [sid for sid, s in SKILLS.items() if s.get("cls") == cls]


# ---- Damage / heal formula --------------------------------------------------

def base_attack_damage(attacker_level: int, attacker_atk: int) -> int:
    """Pure physical swing without a skill multiplier."""
    return max(1, attacker_atk + random.randint(0, attacker_level))


def skill_damage(attacker_level: int, attacker_atk: int, skill: Dict[str, Any],
                 defender_defn: int, party_size: int = 1) -> int:
    raw = base_attack_damage(attacker_level, attacker_atk) * skill.get("dmg_mult", 1.0)
    raw *= (1 + PARTY_BONUS_PER_MEMBER * (party_size - 1))
    raw = max(1, raw - defender_defn * 0.5)
    if random.random() < 0.18:  # 18% crit
        raw *= BOSS_CRIT_MULT
    return int(raw)


def skill_heal(caster_level: int, caster_mp_max: int, skill: Dict[str, Any],
               party_size: int = 1) -> int:
    raw = caster_mp_max * 0.4 * skill.get("heal_mult", 1.0)
    raw *= (1 + PARTY_BONUS_PER_MEMBER * (party_size - 1))
    return int(raw)


# ---- Combat resolver --------------------------------------------------------

def perform_attack(conn: sqlite3.Connection, attacker_id: str, target_id: str,
                   skill_id: str, lang: str = "zh") -> Dict[str, Any]:
    """Run one attack/heal action. Mutates DB. Returns result dict."""
    cur = conn.cursor()
    now = time.time()

    cur.execute("SELECT * FROM players WHERE id=?", (attacker_id,))
    att = cur.fetchone()
    if not att:
        return {"ok": False, "msg": "no_attacker"}

    if att["hp"] <= 0:
        cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,detail,lang) VALUES (?,?,?,?,?,?)",
                    (now, att["id"], att["name"], "dead", t("err_dead", lang), lang))
        conn.commit()
        return {"ok": False, "msg": t("err_dead", lang)}

    skill = SKILLS.get(skill_id)
    if not skill:
        cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,detail,lang) VALUES (?,?,?,?,?,?)",
                    (now, att["id"], att["name"], "err", t("err_no_skill", lang), lang))
        conn.commit()
        return {"ok": False, "msg": t("err_no_skill", lang)}

    cost = skill.get("cost", 0)
    if att["mp"] < cost:
        cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,detail,lang) VALUES (?,?,?,?,?,?)",
                    (now, att["id"], att["name"], "err", t("err_no_mp", lang), lang))
        conn.commit()
        return {"ok": False, "msg": t("err_no_mp", lang)}

    # Determine target
    cur.execute("SELECT * FROM players WHERE id=?", (target_id,))
    t_player = cur.fetchone()
    t_mob = None
    if not t_player:
        cur.execute("SELECT * FROM mobs WHERE id=?", (target_id,))
        t_mob = cur.fetchone()

    if not t_player and not t_mob:
        return {"ok": False, "msg": t("err_no_target", lang)}

    # Party size for the attacker
    party_size = 1
    if att["party_id"]:
        cur.execute("SELECT COUNT(*) AS c FROM party_members WHERE party_id=?", (att["party_id"],))
        party_size = max(1, cur.fetchone()["c"])

    # Resolve
    dmg = 0
    heal = 0
    crit = False
    miss = False

    kind = skill.get("kind", "melee")

    if kind in ("melee", "spell", "ranged"):
        if random.random() < 0.05:
            miss = True
        else:
            if t_player:
                defender_defn = t_player["defn"]
            else:
                defender_defn = t_mob["defn"]
            dmg = skill_damage(att["level"], att["atk"], skill, defender_defn, party_size)
            crit = (dmg >= base_attack_damage(att["level"], att["atk"]) * skill.get("dmg_mult", 1.0) * BOSS_CRIT_MULT * 0.9)
            # Apply
            if t_player:
                new_hp = max(0, t_player["hp"] - dmg)
                cur.execute("UPDATE players SET hp=? WHERE id=?", (new_hp, t_player["id"]))
            else:
                new_hp = max(0, t_mob["hp"] - dmg)
                cur.execute("UPDATE mobs SET hp=? WHERE id=?", (new_hp, t_mob["id"]))
    elif kind == "heal":
        heal = skill_heal(att["level"], att["mp_max"], skill, party_size)
        if t_player:
            new_hp = min(t_player["hp_max"], t_player["hp"] + heal)
            cur.execute("UPDATE players SET hp=? WHERE id=?", (new_hp, t_player["id"]))
    elif kind == "buff":
        if skill.get("buff_invuln"):
            cur.execute("UPDATE players SET hp=? WHERE id=?", (min(att["hp_max"], att["hp"] + 50), att["id"]))
            heal = 50

    # Deduct MP
    cur.execute("UPDATE players SET mp=? WHERE id=?", (max(0, att["mp"] - cost), att["id"]))

    # Log skill use
    cur.execute("""INSERT INTO skills_used (player_id,skill_id,target_kind,target_id,damage,heal,ts)
                   VALUES (?,?,?,?,?,?,?)""",
                (att["id"], skill_id,
                 "player" if t_player else "mob",
                 target_id, dmg, heal, now))

    # Format target name
    if t_player:
        tgt_name = t_player["name"]
    else:
        tgt_name = t_mob["name"]

    actor_name = att["name"]
    skill_disp = skill_name(skill_id, lang)
    if miss:
        msg = t("miss", lang, actor=actor_name, target=tgt_name, skill=skill_disp)
    elif dmg > 0:
        if crit:
            msg = t("attack_crit", lang, actor=actor_name, target=tgt_name, skill=skill_disp, dmg=dmg)
        else:
            msg = t("attack", lang, actor=actor_name, target=tgt_name, skill=skill_disp, dmg=dmg)
    elif heal > 0:
        msg = t("heal", lang, actor=actor_name, target=tgt_name, skill=skill_disp, amt=heal)
    else:
        msg = f"{actor_name} -> {skill_disp} (no effect)"

    cur.execute("""INSERT INTO combat_log (ts,actor_id,actor_name,action,target_id,target_name,detail,lang)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (now, att["id"], actor_name, skill_id, target_id, tgt_name, msg, lang))

    # Death handling
    if t_player and t_player["hp"] - dmg <= 0:
        cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,target_id,target_name,detail,lang) VALUES (?,?,?,?,?,?,?,?)",
                    (now, None, None, "death", t_player["id"], t_player["name"], t("death_player", lang, name=t_player["name"]), lang))
    if t_mob and t_mob["hp"] - dmg <= 0:
        # Loot drop
        import json as _json
        loot_table = _json.loads(t_mob["loot_table"] or "[]")
        dropped = []
        for item_id, chance in loot_table:
            if random.random() < chance:
                cur.execute("INSERT INTO inventory (player_id,item_id,qty) VALUES (?,?,?)",
                            (att["id"], item_id, 1))
                dropped.append(item_name(item_id, lang))
        # XP / gold
        cur.execute("UPDATE players SET xp=xp+?, gold=gold+?, last_seen=? WHERE id=?",
                    (t_mob["xp_reward"], t_mob["gold_reward"], now, att["id"]))
        # Remove mob (or keep corpse for gathering nodes which have hp_max=1)
        if t_mob["kind"] == "gathering":
            cur.execute("DELETE FROM mobs WHERE id=?", (t_mob["id"],))
        else:
            cur.execute("UPDATE mobs SET alive=0 WHERE id=?", (t_mob["id"],))
        msg_drop = t("loot_drop", lang, name=tgt_name, items=", ".join(dropped) if dropped else "(none)")
        cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,target_id,target_name,detail,lang) VALUES (?,?,?,?,?,?,?,?)",
                    (now, att["id"], actor_name, "loot", t_mob["id"], tgt_name, msg_drop, lang))
        cur.execute("INSERT INTO combat_log (ts,actor_id,actor_name,action,target_id,target_name,detail,lang) VALUES (?,?,?,?,?,?,?,?)",
                    (now, None, None, "kill", t_mob["id"], tgt_name,
                     t("death_mob", lang, name=tgt_name), lang))

        # Quest progress
        cur.execute("SELECT * FROM quests WHERE player_id=? AND state='active'", (att["id"],))
        for q in cur.fetchall():
            if t_mob["kind"] in ("mob", "boss"):
                prog = json.loads(q["progress"] or "{}")
                prog["kills"] = prog.get("kills", 0) + 1
                cur.execute("UPDATE quests SET progress=? WHERE id=?", (json.dumps(prog), q["id"]))

    conn.commit()
    return {"ok": True, "msg": msg, "dmg": dmg, "heal": heal, "crit": crit, "miss": miss,
            "skill": skill_disp, "target": tgt_name}
