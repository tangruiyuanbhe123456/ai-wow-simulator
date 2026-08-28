"""World entities: zones, items, mobs, players, RNG helpers."""
from __future__ import annotations
import random
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional

from server.config import (
    PLAYER_BASE_HP_PER_LEVEL, PLAYER_BASE_ATK_PER_LEVEL,
    BOSS_BASE_HP, BOSS_BASE_ATK, MAX_LEVEL,
)


# ---- Zones ------------------------------------------------------------------

ZONES: List[Dict[str, Any]] = [
    {"id": "starter_village", "name_zh": "新手村", "name_en": "Starter Village",
     "x": 0, "y": 0, "size": 10, "level_min": 1, "level_max": 3},
    {"id": "wild_plains", "name_zh": "荒野平原", "name_en": "Wild Plains",
     "x": 5, "y": 5, "size": 20, "level_min": 2, "level_max": 6},
    {"id": "dark_forest", "name_zh": "黑暗森林", "name_en": "Dark Forest",
     "x": -5, "y": 5, "size": 20, "level_min": 4, "level_max": 9},
    {"id": "dragon_peaks", "name_zh": "巨龙之巅", "name_en": "Dragon Peaks",
     "x": 0, "y": -8, "size": 25, "level_min": 8, "level_max": 15},
    {"id": "shadow_dungeon", "name_zh": "暗影副本", "name_en": "Shadow Dungeon",
     "x": -10, "y": -10, "size": 15, "level_min": 10, "level_max": 18, "is_dungeon": True},
    {"id": "fire_citadel", "name_zh": "火焰堡垒", "name_en": "Fire Citadel",
     "x": 12, "y": -12, "size": 18, "level_min": 14, "level_max": 25, "is_dungeon": True},
]


def zone_by_id(zid: str) -> Optional[Dict[str, Any]]:
    for z in ZONES:
        if z["id"] == zid:
            return z
    return None


def zone_name(zid: str, lang: str = "zh") -> str:
    z = zone_by_id(zid)
    if not z:
        return zid
    n = z["name_zh"] if lang in ("zh", "zh_only") else z["name_en"]
    if lang == "zh":
        return f"{z['name_zh']} | {z['name_en']}"
    return n


# ---- Items / Loot -----------------------------------------------------------

ITEMS: Dict[str, Dict[str, Any]] = {
    "potion_minor": {"name_zh": "初级治疗药水", "name_en": "Minor Healing Potion", "type": "consumable", "heal": 30, "price": 5},
    "potion_health": {"name_zh": "治疗药水", "name_en": "Healing Potion", "type": "consumable", "heal": 80, "price": 15},
    "potion_mana": {"name_zh": "法力药水", "name_en": "Mana Potion", "type": "consumable", "mana": 50, "price": 15},
    "herb_silverleaf": {"name_zh": "银叶草", "name_en": "Silverleaf Herb", "type": "gather", "price": 3},
    "ore_copper": {"name_zh": "铜矿石", "name_en": "Copper Ore", "type": "gather", "price": 5},
    "ore_iron": {"name_zh": "铁矿石", "name_en": "Iron Ore", "type": "gather", "price": 12},
    "sword_iron": {"name_zh": "铁剑", "name_en": "Iron Sword", "type": "weapon", "atk": 6, "price": 40},
    "axe_steel": {"name_zh": "钢斧", "name_en": "Steel Axe", "type": "weapon", "atk": 9, "price": 90},
    "staff_mage": {"name_zh": "法师之杖", "name_en": "Mage Staff", "type": "weapon", "atk": 5, "mp": 20, "price": 80},
    "bow_hunters": {"name_zh": "猎人长弓", "name_en": "Hunter's Longbow", "type": "weapon", "atk": 7, "price": 70},
    "shield_iron": {"name_zh": "铁盾", "name_en": "Iron Shield", "type": "shield", "defn": 5, "price": 50},
    "armor_leather": {"name_zh": "皮甲", "name_en": "Leather Armor", "type": "armor", "defn": 3, "price": 30},
    "ring_power": {"name_zh": "力量之戒", "name_en": "Ring of Power", "type": "ring", "atk": 3, "price": 100},
    "amulet_healing": {"name_zh": "治疗护身符", "name_en": "Amulet of Healing", "type": "amulet", "mp": 15, "price": 90},
    "epic_blade": {"name_zh": "史诗之刃", "name_en": "Epic Blade", "type": "weapon", "atk": 15, "price": 500},
    "epic_robe": {"name_zh": "史诗长袍", "name_en": "Epic Robe", "type": "armor", "defn": 10, "price": 450},
}


def item_name(iid: str, lang: str = "zh") -> str:
    it = ITEMS.get(iid)
    if not it:
        return iid
    if lang == "en":
        return it["name_en"]
    if lang == "zh_only":
        return it["name_zh"]
    return f"{it['name_zh']} | {it['name_en']}"


def all_item_ids() -> List[str]:
    return list(ITEMS.keys())


# ---- Mob / Boss definitions -------------------------------------------------

MOB_TEMPLATES: List[Dict[str, Any]] = [
    {"name_zh": "草原狼", "name_en": "Plains Wolf", "kind": "mob", "level": 2, "hp": 40, "atk": 8, "defn": 1,
     "xp": 12, "gold": 3, "loot": [("potion_minor", 0.3), ("herb_silverleaf", 0.2)], "zone": "wild_plains"},
    {"name_zh": "森林蜘蛛", "name_en": "Forest Spider", "kind": "mob", "level": 4, "hp": 65, "atk": 12, "defn": 2,
     "xp": 25, "gold": 6, "loot": [("potion_health", 0.25), ("herb_silverleaf", 0.3)], "zone": "dark_forest"},
    {"name_zh": "石巨人", "name_en": "Stone Giant", "kind": "mob", "level": 7, "hp": 150, "atk": 18, "defn": 6,
     "xp": 60, "gold": 15, "loot": [("ore_iron", 0.4), ("armor_leather", 0.15)], "zone": "dark_forest"},
    {"name_zh": "小龙崽", "name_en": "Whelp", "kind": "mob", "level": 10, "hp": 220, "atk": 25, "defn": 8,
     "xp": 110, "gold": 30, "loot": [("ore_iron", 0.5), ("potion_mana", 0.3)], "zone": "dragon_peaks"},
]

BOSS_TEMPLATES: List[Dict[str, Any]] = [
    {"name_zh": "暗影领主", "name_en": "Shadow Lord", "level": 5, "hp": BOSS_BASE_HP, "atk": BOSS_BASE_ATK, "defn": 5,
     "xp": 200, "gold": 80, "loot": [("sword_iron", 0.5), ("shield_iron", 0.5), ("amulet_healing", 0.2)],
     "zone": "shadow_dungeon", "boss_room": "entrance", "skills": ["shadow_claw"]},
    {"name_zh": "火焰之王", "name_en": "Fire King", "level": 12, "hp": BOSS_BASE_HP * 2, "atk": BOSS_BASE_ATK * 2,
     "defn": 12, "xp": 500, "gold": 250, "loot": [("axe_steel", 0.4), ("staff_mage", 0.4), ("epic_blade", 0.1)],
     "zone": "fire_citadel", "boss_room": "throne", "skills": ["fire_nova"]},
    {"name_zh": "远古巨龙", "name_en": "Ancient Dragon", "level": 18, "hp": BOSS_BASE_HP * 4, "atk": BOSS_BASE_ATK * 3,
     "defn": 20, "xp": 1500, "gold": 800, "loot": [("epic_blade", 0.5), ("epic_robe", 0.5), ("ring_power", 0.3)],
     "zone": "fire_citadel", "boss_room": "summit", "skills": ["dragon_breath"]},
]


def gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def rng_pos(zone: Dict[str, Any]) -> tuple[int, int]:
    return (random.randint(zone["x"] - zone["size"] // 2, zone["x"] + zone["size"] // 2),
            random.randint(zone["y"] - zone["size"] // 2, zone["y"] + zone["size"] // 2))


def level_to_hp(level: int) -> int:
    return PLAYER_BASE_HP_PER_LEVEL * level + 20


def level_to_atk(level: int) -> int:
    return PLAYER_BASE_ATK_PER_LEVEL * level + 2


def xp_to_next(level: int) -> int:
    return int(80 * (1.35 ** (level - 1)))


# ---- Spawning ---------------------------------------------------------------

def spawn_world_mobs(conn) -> int:
    """Insert all mob/boss/gathering nodes into the DB on first boot."""
    import json as _json
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM mobs")
    if cur.fetchone()["c"] > 0:
        return 0
    inserted = 0

    # Mobs
    for tmpl in MOB_TEMPLATES:
        for _ in range(4):
            zone = zone_by_id(tmpl["zone"])
            x, y = rng_pos(zone)
            cur.execute("""INSERT INTO mobs (id,name,kind,level,hp,hp_max,atk,defn,zone,pos_x,pos_y,
                          xp_reward,gold_reward,loot_table,alive) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                        (gen_id("mob"), f"{tmpl['name_zh']}|{tmpl['name_en']}", "mob", tmpl["level"],
                         tmpl["hp"], tmpl["hp"], tmpl["atk"], tmpl["defn"], tmpl["zone"], x, y,
                         tmpl["xp"], tmpl["gold"], _json.dumps(tmpl["loot"], ensure_ascii=False)))
            inserted += 1

    # Bosses (one per room)
    for tmpl in BOSS_TEMPLATES:
        zone = zone_by_id(tmpl["zone"])
        x, y = rng_pos(zone)
        cur.execute("""INSERT INTO mobs (id,name,kind,level,hp,hp_max,atk,defn,zone,pos_x,pos_y,
                      xp_reward,gold_reward,loot_table,boss_room,alive) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (gen_id("boss"), f"{tmpl['name_zh']}|{tmpl['name_en']}", "boss", tmpl["level"],
                     tmpl["hp"], tmpl["hp"], tmpl["atk"], tmpl["defn"], tmpl["zone"], x, y,
                     tmpl["xp"], tmpl["gold"], _json.dumps(tmpl["loot"], ensure_ascii=False), tmpl["boss_room"]))
        inserted += 1

    # Gathering nodes
    gather_tmpl = [
        {"name_zh": "草药丛", "name_en": "Herb Bush", "level": 1, "zone": "wild_plains", "item": "herb_silverleaf"},
        {"name_zh": "矿脉", "name_en": "Ore Vein", "level": 3, "zone": "dark_forest", "item": "ore_copper"},
        {"name_zh": "铁矿脉", "name_en": "Iron Vein", "level": 6, "zone": "dark_forest", "item": "ore_iron"},
        {"name_zh": "龙晶矿脉", "name_en": "Dragon Crystal Vein", "level": 10, "zone": "dragon_peaks", "item": "ore_iron"},
    ]
    for tmpl in gather_tmpl:
        for _ in range(3):
            zone = zone_by_id(tmpl["zone"])
            x, y = rng_pos(zone)
            cur.execute("""INSERT INTO mobs (id,name,kind,level,hp,hp_max,atk,defn,zone,pos_x,pos_y,
                          xp_reward,gold_reward,loot_table,alive) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                        (gen_id("node"), f"{tmpl['name_zh']}|{tmpl['name_en']}", "gathering", tmpl["level"],
                         1, 1, 0, 0, tmpl["zone"], x, y, 0, 0,
                         _json.dumps([(tmpl["item"], 1.0)], ensure_ascii=False)))
            inserted += 1

    conn.commit()
    return inserted
