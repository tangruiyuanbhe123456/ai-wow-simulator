"""5v5 arena mode — Honor-of-Kings-inspired team battle.

Each match:
  - Two teams of 5 agents (blue / red).
  - Each team has a Crystal (base). First to destroy the enemy crystal wins.
  - Per-tick: every agent chooses attack/move/skill; damage applies, respawn
    after death with cooldown. Crystal takes damage when the holder dies.

State is kept in-process (no DB persistence) because arena matches are
short-lived. Persisted on the server side via the api/v1/observer endpoint.

i18n: zh / en labels via i18n.arena_msg().
"""
from __future__ import annotations
import random
import time
from pathlib import Path as _Path
try:
    from server.config import DB_PATH as _DB_PATH_FROM_CONFIG
    DB_PATH = _DB_PATH_FROM_CONFIG
except Exception:
    DB_PATH = _Path("data/world.db")
import threading
from dataclasses import dataclass, field
from typing import Any


# Crystal HP (tunable). With ~10 dmg/tick from each agent in range, and 5 agents
# eventually reaching the enemy base, ~80 ticks (80s real time @ 1s tick)
# should be enough for one side to take it down.
CRYSTAL_HP = 300

# Respawn cooldown (real seconds). In tick count (assuming 1s tick) → 5.
RESPAWN_TICKS = 5

# Arena area: 50x50 logical grid, agents start at corners.
ARENA_W = 50
ARENA_H = 50

# Neutral objectives — Honor-of-Kings-style dragons.
# Younger dragon spawns at tick 30 in the river; whoever kills it gets +15% dmg for 25 ticks.
# Elder dragon spawns at tick 70; +25% dmg for 40 ticks. These stack.
DRAGON_SPAWN_TICKS = {"young": 30, "elder": 70}
DRAGON_HP = {"young": 400, "elder": 800}
DRAGON_REWARD = {"young": {"dmg_pct": 0.15, "duration": 25},
                 "elder": {"dmg_pct": 0.25, "duration": 40}}
BUFF_DURATION = 25  # default fallback if a key is missing

# 3-lane map (Honor-of-Kings-style). Each lane is a corridor from one team's
# base to the other. Agents are auto-assigned to a lane based on their index
# in the team roster (0→top, 1→mid, 2→bot, 3→top, 4→mid).
LANES = ["top", "mid", "bot"]
LANE_Y = {"top": 8, "mid": 25, "bot": 42}  # y coordinate of each lane corridor

# Towers — 2 per team per lane (outer + inner/高地). Inner is in front of
# crystal; outer is between bases. Push order: outer → inner → crystal.
TOWER_HP = {"outer": 200, "inner": 400}
TOWER_DMG_PER_TICK = 5  # when an agent is in tower's push range

# Equipment catalog — 6 slots × 3 tiers. Buying a piece grants permanent stat
# boosts (atk for weapon, hp_max for chest/helm, move bonus for boots, etc.).
# Equipment is bought with gold earned via kills (50g) / deaths (-10g tax).
# MVP: bots auto-buy the best available piece they can afford each tick.
EQUIPMENT_CATALOG = {
    # slot: [(name, cost, stats_dict, active_skill_id, active_desc)]
    "weapon": [
        ("rusty_blade",     200,  {"atk": 3},                       None, None),
        ("iron_sword",      500,  {"atk": 7},                       "blade_burst", "Next attack deals 2x dmg (30 tick cd)"),
        ("dragon_slayer",  1200,  {"atk": 15},                      "lifesteal",   "Lifesteal 30% for 5 ticks (45 tick cd)"),
    ],
    "helm": [
        ("cloth_cap",       150,  {"hp_max": 10},                   None, None),
        ("iron_helm",       400,  {"hp_max": 25},                   "guard",       "Shield 80 dmg for 6 ticks (40 tick cd)"),
        ("dragon_helm",     900,  {"hp_max": 60},                   "undying",     "Auto-revive once with 50% HP (one-time)"),
    ],
    "chest": [
        ("leather_vest",    150,  {"hp_max": 15},                   None, None),
        ("iron_plate",      450,  {"hp_max": 40},                   "thorns",      "Reflect 30% dmg for 8 ticks (35 tick cd)"),
        ("dragon_scale",   1000,  {"hp_max": 80},                   "immortal",    "First lethal hit deals 1 (one-time)"),
    ],
    "boots": [
        ("cloth_boots",     100,  {"atk": 4},                       None, None),
        ("swift_boots",     300,  {"atk": 2, "hp_max": 5},          "haste",       "+50% move speed for 10 ticks (25 tick cd)"),
        ("dragon_talons",   700,  {"atk": 4, "hp_max": 10},         "phase",       "Teleport 10 cells toward enemy (60 tick cd)"),
    ],
    "trinket": [
        ("lucky_charm",     200,  {"atk": 2, "hp_max": 10},         None, None),
        ("hero_medal",      500,  {"atk": 5, "hp_max": 20},         None, None),
        ("dragon_eye",     1100,  {"atk": 10, "hp_max": 50},        "time_warp",   "Slow enemies 50% within 8 cells 5 ticks (50 tick cd)"),
    ],
    "skin": [
        ("basic_skin",        0,  {},                                None, None),
        ("war_paint",        200,  {"atk": 4, "hp_max": 20},         "berserk",     "+30% atk for 6 ticks (40 tick cd)"),
        ("ascended_form",    600,  {"atk": 18, "hp_max": 80},        "rebirth",     "30% chance to survive death at 1 HP (one-time)"),
    ],
    # === DLC items (v10) ===
    "weapon": [
        ("necro_staff",      800,  {"atk": 22, "hp_max": 30},        "soul_drain",   "Kills heal 50% HP instantly (30 tick cd)"),
        ("assassin_dagger",  900,  {"atk": 30},                     "backstab",     "Next 2 attacks deal 50% more damage (25 tick cd)"),
    ],
    "helm": [
        ("druid_circlet",    700,  {"hp_max": 80, "atk": 8},        "regrowth",     "Heal 30 HP every 10 ticks passively (one-time setup)"),
    ],
    "chest": [
        ("necro_robe",      1000,  {"hp_max": 220},                 "death_aura",   "Deal 5 dmg/sec to enemies within 3 cells (passive)"),
    ],
    "boots": [
        ("wind_step",       1100,  {"atk": 12, "hp_max": 50},       "windwalk",     "After moving 5 ticks, +40% move speed (45 tick cd)"),
    ],
    "trinket": [
        ("phoenix_eye_t3",  1500,  {"atk": 35, "hp_max": 120},      "rebirth_III",  "On death: 50% chance to survive at 1 HP (improved from ascended_form)"),
    ],
    "skin": [
        ("shadow_cloak",     800,  {"atk": 25, "hp_max": 60},        "vanish",       "Every 60 ticks: become invisible for 3 ticks (next attack guaranteed crit)"),
    ],
}

# Per-hero ultimates — each class gets 1 ultimate ability, 60-tick cooldown
# after use. Triggered automatically when off-cooldown (bots always use
# when they can).
ULTIMATES = {
    # class -> (ult_id, zh_name, en_name, effect_func_name)
    "warrior":  ("warrior_charge",   "冲锋陷阵",  "Heroic Charge",   "Charge to nearest enemy (8 cells). Stun for 2 ticks."),
    "mage":     ("mage_meteor",      "陨石天降",  "Meteor Strike",   "Deal 80 dmg in 5-cell radius at nearest enemy position."),
    "priest":   ("priest_resurrect", "神圣复活",  "Divine Resurrection", "Revive any dead ally on the field (full HP, no respawn wait)."),
    "hunter":   ("hunter_snipe",     "致命狙击",  "Hunter's Snipe",  "Snipe lowest-HP enemy from any distance for 70 dmg + 1.5x crit."),
    # === DLC heroes (v10) ===
    "necromancer": ("necro_summon",   "亡者大军",  "Legion of the Dead", "Summon 2 skeleton minions (40 HP each, AI: attack nearest enemy)."),
    "assassin":    ("assassin_stealth","暗影突袭", "Shadow Strike",  "Teleport to enemy backline (12 cells), deal 100 dmg to lowest-HP target."),
    "druid":       ("druid_root",     "自然之力",  "Nature's Grasp", "Root all enemies within 5 cells for 4 ticks (they cannot move)."),
}

# Gold rewards
GOLD_PER_KILL = 50
GOLD_ON_DEATH = 0    # lose gold tax could be added in v2

# Ranked tier table — Honor-of-Kings-inspired.
# Rating ranges (inclusive lower bound, exclusive upper bound):
RANK_TIERS = [
    (0,    "青铜 I",   "Bronze I"),
    (200,  "青铜 II",  "Bronze II"),
    (400,  "白银 I",   "Silver I"),
    (600,  "白银 II",  "Silver II"),
    (800,  "黄金 I",   "Gold I"),
    (1000, "黄金 II",  "Gold II"),
    (1200, "铂金 I",   "Platinum I"),
    (1400, "铂金 II",  "Platinum II"),
    (1600, "钻石 I",   "Diamond I"),
    (1800, "钻石 II",  "Diamond II"),
    (2000, "星耀",     "Star"),
    (2200, "王者",     "King"),
]


def rating_to_tier(rating: int) -> str:
    """Return the tier name for the given rating."""
    tier_en = "Bronze I"
    for thr, _zh, en in RANK_TIERS:
        if rating >= thr:
            tier_en = en
    return tier_en


def apply_match_result(blue_pids: list, red_pids: list, winner: str) -> None:
    """Update each player's rank_rating / wins / losses after a 5v5 match.

    Uses simplified Elo: winner +25, loser -15. Plus shared w/l record.
    """
    from server.db import connect as _db_connect
    c = _db_connect()
    cur = c.cursor()
    for pid in blue_pids:
        delta = 25 if winner == "blue" else -15
        cur.execute("UPDATE players SET rank_rating = rank_rating + ?, "
                    "wins = wins + ?, losses = losses + ? WHERE id=?",
                    (delta, 1 if winner == "blue" else 0,
                     1 if winner == "red" else 0, pid))
    for pid in red_pids:
        delta = 25 if winner == "red" else -15
        cur.execute("UPDATE players SET rank_rating = rank_rating + ?, "
                    "wins = wins + ?, losses = losses + ? WHERE id=?",
                    (delta, 1 if winner == "red" else 0,
                     1 if winner == "blue" else 0, pid))
    # Recompute tiers for everyone
    cur.execute("SELECT id, rank_rating FROM players")
    for row in cur.fetchall():
        cur.execute("UPDATE players SET rank_tier=? WHERE id=?",
                    (rating_to_tier(row["rank_rating"]), row["id"]))
    c.commit()


@dataclass
class ArenaAgent:
    pid: str          # player id (string from server)
    name: str         # display name
    cls: str          # class (warrior/mage/priest/hunter)
    team: str         # 'blue' or 'red'
    lane: str = "mid"  # 'top'/'mid'/'bot' — auto-assigned at match creation
    hp: int = 100
    hp_max: int = 100
    mp: int = 60
    atk: int = 14
    pos: tuple = (5, 25)   # starting x,y
    alive: bool = True
    kills: int = 0
    deaths: int = 0
    respawn_in: int = 0   # ticks until respawn
    # Equipment system — 6 slots; each grants stat boosts. Bought with gold
    # earned via kills/death tickets. Cosmetic in MVP; affects atk/hp_max.
    gold: int = 500      # starting gold (enough for one tier-1 item)
    equipment: dict = field(default_factory=lambda: {
        "weapon": None, "helm": None, "chest": None,
        "boots": None, "trinket": None, "skin": None,
    })
    # Ultimate skill (per-hero) — loaded from ULTIMATES at match start.
    ultimate: str = ""        # ultimate_id, e.g. "warrior_charge"
    ult_cd: int = 0          # ticks until ready (0 = ready)
    # Summoner spell (chosen during draft)
    spell: str = ""          # spell_id, e.g. "flash"
    spell_used: bool = False  # one-shot per match
    # Status effects (from summoner spells or ultimates)
    shield_remaining: int = 0  # barrier absorb amount
    speed_boost_ticks: int = 0  # ghost remaining
    ignite_target_pid: str = ""  # who we're igniting
    ignite_ticks: int = 0
    weakened_target_pid: str = ""  # who we exhausted
    weakened_ticks: int = 0
    low_hp_smite_used: bool = False  # smite already used


@dataclass
class Crystal:
    team: str
    hp: int = CRYSTAL_HP
    pos: tuple = field(default_factory=lambda: (1, 25))  # left or right edge


@dataclass
class Tower:
    """A lane tower (outer or inner/高地)."""
    team: str         # which team's tower (i.e. defends this team's base)
    lane: str         # 'top' / 'mid' / 'bot'
    kind: str         # 'outer' / 'inner'
    hp: int = 0       # initialized in __post_init__ via TOWER_HP
    hp_max: int = 0
    pos: tuple = (0, 0)

    def __post_init__(self):
        self.hp_max = TOWER_HP.get(self.kind, 200)
        if self.hp == 0:
            self.hp = self.hp_max
        # x = team's side (1 for blue, ARENA_W-2 for red); y = LANE_Y[lane]
        x = 1 if self.team == "blue" else ARENA_W - 2
        y = LANE_Y[self.lane]
        # outer is at x=8 from blue side, x=42 from red side (mid-river)
        if self.kind == "outer":
            x = 14 if self.team == "blue" else ARENA_W - 15
        # inner is closer to base (in front of crystal)
        else:
            x = 6 if self.team == "blue" else ARENA_W - 7
        self.pos = (x, y)


@dataclass
class ArenaMatch:
    match_id: str
    blue: list = field(default_factory=list)   # list[ArenaAgent]
    red: list = field(default_factory=list)
    blue_crystal: Crystal = None
    red_crystal: Crystal = None
    started_at: float = 0.0
    tick: int = 0
    ended: bool = False
    winner: str | None = None   # 'blue' / 'red' / None
    log: list = field(default_factory=list)   # list[(t, msg_zh, msg_en)]
    team_kills: dict = field(default_factory=lambda: {"blue": 0, "red": 0})
    team_dmg_to_crystal: dict = field(default_factory=lambda: {"blue": 0, "red": 0})
    # Neutral objectives
    dragons: list = field(default_factory=list)   # active dragons in the arena
    events: list = field(default_factory=list)     # random events (ambush / airdrop / etc.)
    team_buffs: dict = field(default_factory=dict)  # {"blue"/"red": {"dmg_pct": 0.2, "expires_at": tick}}
    # 3-lane towers (6 towers total: 3 lanes × 2 kinds × 2 teams)
    towers: list = field(default_factory=list)    # list[Tower]
    lock: threading.Lock = field(default_factory=threading.Lock)

    def to_dict(self, lang: str = "zh") -> dict[str, Any]:
        with self.lock:
            return {
                "ok": True,
                "match_id": self.match_id,
                "lang": lang,
                "tick": self.tick,
                "started_at": self.started_at,
                "ended": self.ended,
                "winner": self.winner,
                "team_kills": dict(self.team_kills),
                "team_dmg_to_crystal": dict(self.team_dmg_to_crystal),
                "blue": [self._agent_view(a) for a in self.blue],
                "red": [self._agent_view(a) for a in self.red],
                "crystals": {
                    "blue": {"hp": self.blue_crystal.hp, "max": CRYSTAL_HP},
                    "red":  {"hp": self.red_crystal.hp,  "max": CRYSTAL_HP},
                },
                # Neutral objectives: dragons spawn at fixed ticks; whoever kills
                # them gets a team-wide damage buff for BUFF_DURATION ticks.
                "dragons": self.dragons,
                "buffs": dict(self.team_buffs),  # {team: {"dmg_pct": 0.2, "expires_at": tick}}
                # 3-lane towers (6 towers: blue outer/inner + red outer/inner × 3 lanes)
                "towers": [
                    {"team": t.team, "lane": t.lane, "kind": t.kind,
                     "hp": t.hp, "hp_max": t.hp_max, "pos": list(t.pos)}
                    for t in self.towers
                ],
                "log": [self._log_view(t, m_zh, m_en, lang) for (t, m_zh, m_en) in self.log[-30:]],
            }

    def _agent_view(self, a: ArenaAgent) -> dict:
        return {
            "pid": a.pid, "name": a.name, "cls": a.cls, "team": a.team,
            "lane": a.lane,
            "hp": a.hp, "hp_max": a.hp_max, "mp": a.mp, "atk": a.atk,
            "pos": list(a.pos), "alive": a.alive,
            "kills": a.kills, "deaths": a.deaths,
            "respawn_in": a.respawn_in,
            "gold": a.gold,
            "equipment": dict(a.equipment),
            "ultimate": a.ultimate,
            "ult_cd": a.ult_cd,
            "spell": a.spell,
            "spell_used": a.spell_used,
        }

    def _log_view(self, t: int, m_zh: str, m_en: str, lang: str) -> dict:
        return {"tick": t, "msg": m_zh if lang == "zh" else m_en}

    def append_log(self, m_zh: str, m_en: str) -> None:
        with self.lock:
            self.log.append((self.tick, m_zh, m_en))
            # Keep last 200 events.
            if len(self.log) > 200:
                self.log = self.log[-200:]


def _recompute_agent_stats(a: ArenaAgent) -> None:
    """Recompute a's atk and hp_max from base + equipment + buffs."""
    base_atk = 14
    base_hp_max = 100
    bonus_atk = 0
    bonus_hp = 0
    for slot, item_id in (a.equipment or {}).items():
        if not item_id:
            continue
        for entry in EQUIPMENT_CATALOG.get(slot, []):
            item_name = entry[0]
            if item_name == item_id:
                stats = entry[2]
                bonus_atk += stats.get("atk", 0)
                bonus_hp += stats.get("hp_max", 0)
                break
    # New max — preserve current HP ratio so the agent doesn't get a free heal
    ratio = (a.hp / a.hp_max) if a.hp_max else 1.0
    a.hp_max = base_hp_max + bonus_hp
    a.hp = max(1, int(a.hp_max * ratio))
    a.atk = base_atk + bonus_atk


def _try_buy_best_affordable(a: ArenaAgent, m: ArenaMatch) -> bool:
    """Bot AI: if agent has gold and an empty equipment slot, buy the best
    piece in that slot they can afford. Returns True if bought anything."""
    if not a.alive:
        return False
    bought = False
    for slot in list(a.equipment.keys()):
        if a.equipment[slot] is not None:
            continue  # already filled
        catalog = EQUIPMENT_CATALOG.get(slot, [])
        # Pick the most expensive item the agent can afford
        best = None
        for entry in catalog:
            item_name = entry[0]
            cost = entry[1]
            if a.gold >= cost and (best is None or cost > best[1]):
                best = (item_name, cost)
        if best is None:
            continue
        a.equipment[slot] = best[0]
        a.gold -= best[1]
        _recompute_agent_stats(a)
        m.append_log(
            f"🛒 {a.name} ({a.team}) 购买 [{slot}:{best[0]}] (-{best[1]}g, atk={a.atk} hp_max={a.hp_max}) | 🛒 {a.name} ({a.team}) bought [{slot}:{best[0]}] (-{best[1]}g, atk={a.atk} hp_max={a.hp_max})",
            f"🛒 {a.name} ({a.team}) bought [{slot}:{best[0]}] (-{best[1]}g, atk={a.atk} hp_max={a.hp_max})",
        )
        bought = True
    return bought


def _use_ultimate(a: ArenaAgent, m: ArenaMatch, tick_n: int) -> None:
    """Bot AI: trigger the agent's ultimate if off-cooldown.

    Each hero class has a unique ult with a powerful effect (high dmg / heal /
    stun / revive). After use, set a 60-tick cooldown.
    """
    if a.ult_cd > 0 or not a.alive or not a.ultimate:
        return
    ult_id = a.ultimate
    if ult_id == "warrior_charge":
        # Charge to nearest enemy (8 cells), stun them for 2 ticks.
        enemies = [e for e in (m.blue + m.red) if e.alive and e.team != a.team]
        if not enemies:
            return
        target = min(enemies, key=lambda e: abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]))
        dist = abs(target.pos[0] - a.pos[0]) + abs(target.pos[1] - a.pos[1])
        if dist > 12:
            return  # too far, save ult for closer fight
        # Apply stun (modeled as respawn timer of 2 ticks... actually use
        # a separate stun state — for simplicity we deal +30 dmg + knockback)
        dmg = 30 + a.atk
        target.hp -= dmg
        m.append_log(
            f"⚡ {a.name} ({a.team}) 大招 冲锋陷阵! 冲向 {target.name} ({target.team}) 伤害 {dmg} (cd=60) | ⚡ {a.name} ({a.team}) ULT Heroic Charge! Hits {target.name} for {dmg} (cd=60)",
            f"⚡ {a.name} ({a.team}) ULT Heroic Charge! Hits {target.name} for {dmg} (cd=60)",
        )
        if target.hp <= 0:
            target.alive = False
            target.deaths += 1
            target.respawn_in = RESPAWN_TICKS
            a.kills += 1
            m.team_kills[a.team] += 1
            a.gold += GOLD_PER_KILL
            _try_buy_best_affordable(a, m)
        a.ult_cd = 60
    elif ult_id == "mage_meteor":
        enemies = [e for e in (m.blue + m.red) if e.alive and e.team != a.team]
        if not enemies:
            return
        target = min(enemies, key=lambda e: abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]))
        if abs(target.pos[0] - a.pos[0]) + abs(target.pos[1] - a.pos[1]) > 20:
            return
        # 80 dmg to enemies in 5-cell radius
        import math as _math
        hit = [e for e in (m.blue + m.red) if e.alive and e.team != a.team
               and _math.hypot(e.pos[0] - target.pos[0], e.pos[1] - target.pos[1]) <= 5]
        for h in hit:
            h.hp -= 80
        m.append_log(
            f"⚡ {a.name} ({a.team}) 大招 陨石天降! 命中 {len(hit)} 人 各 80 伤害 (cd=60) | ⚡ {a.name} ({a.team}) ULT Meteor Strike! Hits {len(hit)} enemies for 80 each (cd=60)",
            f"⚡ {a.name} ({a.team}) ULT Meteor Strike! Hits {len(hit)} enemies for 80 each (cd=60)",
        )
        a.ult_cd = 60
    elif ult_id == "priest_resurrect":
        # Revive any dead ally on the field
        allies = [x for x in (m.blue if a.team == "blue" else m.red) if not x.alive]
        if not allies:
            return
        ally = allies[0]  # revive first dead ally
        ally.alive = True
        ally.hp = ally.hp_max
        ally.respawn_in = 0
        m.append_log(
            f"⚡ {a.name} ({a.team}) 大招 神圣复活! {ally.name} ({ally.team}) 满血复活 (cd=60) | ⚡ {a.name} ({a.team}) ULT Divine Resurrection! {ally.name} ({ally.team}) back at full HP (cd=60)",
            f"⚡ {a.name} ({a.team}) ULT Divine Resurrection! {ally.name} ({ally.team}) back at full HP (cd=60)",
        )
        a.ult_cd = 60
    elif ult_id == "hunter_snipe":
        # Snipe lowest-HP enemy from any distance
        enemies = [e for e in (m.blue + m.red) if e.alive and e.team != a.team]
        if not enemies:
            return
        target = min(enemies, key=lambda e: e.hp)
        dmg = int(70 * 1.5)  # 105 dmg (1.5x crit)
        target.hp -= dmg
        m.append_log(
            f"⚡ {a.name} ({a.team}) 大招 致命狙击! 命中 {target.name} ({target.team}) 105 伤害 (cd=60) | ⚡ {a.name} ({a.team}) ULT Snipe! Hits {target.name} ({target.team}) for 105 (cd=60)",
            f"⚡ {a.name} ({a.team}) ULT Snipe! Hits {target.name} ({target.team}) for 105 (cd=60)",
        )
        if target.hp <= 0:
            target.alive = False
            target.deaths += 1
            target.respawn_in = RESPAWN_TICKS
            a.kills += 1
            m.team_kills[a.team] += 1
            a.gold += GOLD_PER_KILL
            _try_buy_best_affordable(a, m)
        a.ult_cd = 60




def _cast_summoner_spell(a: ArenaAgent, m: ArenaMatch, tick_n: int) -> None:
    """Use the agent's summoner spell (one-shot per match).

    Each spell has a different effect triggered at the moment of use. The
    effect itself may be instant or a short DoT/buff. Bots auto-cast their
    spell when an opportunity arises (low HP → heal/smite, near enemy →
    ignite, low HP under attack → barrier, etc.).
    """
    if a.spell_used or not a.spell or not a.alive:
        return
    spell = a.spell
    a.spell_used = True
    enemies = [e for e in (m.blue + m.red) if e.alive and e.team != a.team]

    if spell == "heal":
        before = a.hp
        a.hp = min(a.hp_max, int(a.hp + a.hp_max * 0.40))
        m.append_log(
            f"💊 {a.name} ({a.team}) 召唤师 [治疗] 恢复 {a.hp - before} HP ({before}→{a.hp}) | 💊 {a.name} ({a.team}) SUMM Heal +{a.hp - before} HP ({before}→{a.hp})",
            f"💊 {a.name} ({a.team}) SUMM Heal +{a.hp - before} HP ({before}→{a.hp})",
        )
    elif spell == "barrier":
        a.shield_remaining = 60
        m.append_log(
            f"🛡️ {a.name} ({a.team}) 召唤师 [屏障] 吸收接下来 60 伤害 (8 tick) | 🛡️ {a.name} ({a.team}) SUMM Barrier absorbs next 60 dmg (8 ticks)",
            f"🛡️ {a.name} ({a.team}) SUMM Barrier absorbs next 60 dmg (8 ticks)",
        )
    elif spell == "flash":
        # Teleport up to 8 cells toward nearest enemy (or away from enemies)
        if enemies:
            target = min(enemies, key=lambda e: abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]))
            dx = (1 if target.pos[0] > a.pos[0] else (-1 if target.pos[0] < a.pos[0] else 0))
            dy = (1 if target.pos[1] > a.pos[1] else (-1 if target.pos[1] < a.pos[1] else 0))
            nx = max(1, min(ARENA_W - 2, a.pos[0] + dx * 8))
            ny = max(1, min(ARENA_H - 2, a.pos[1] + dy * 8))
            old_pos = a.pos
            a.pos = (nx, ny)
            m.append_log(
                f"⚡ {a.name} ({a.team}) 召唤师 [闪现] 移动 {abs(nx-old_pos[0]) + abs(ny-old_pos[1])} 格 | ⚡ {a.name} ({a.team}) SUMM Flash moved {abs(nx-old_pos[0]) + abs(ny-old_pos[1])} cells",
                f"⚡ {a.name} ({a.team}) SUMM Flash moved {abs(nx-old_pos[0]) + abs(ny-old_pos[1])} cells",
            )
    elif spell == "ignite" and enemies:
        target = min(enemies, key=lambda e: abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]))
        a.ignite_target_pid = target.pid
        a.ignite_ticks = 5
        m.append_log(
            f"🔥 {a.name} ({a.team}) 召唤师 [点燃] 烧 {target.name} ({target.team}) 80 伤害 / 5 tick | 🔥 {a.name} ({a.team}) SUMM Ignite burns {target.name} ({target.team}) 80 dmg over 5 ticks",
            f"🔥 {a.name} ({a.team}) SUMM Ignite burns {target.name} ({target.team}) 80 dmg over 5 ticks",
        )
    elif spell == "exhaust" and enemies:
        target = min(enemies, key=lambda e: abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]))
        a.weakened_target_pid = target.pid
        a.weakened_ticks = 10
        m.append_log(
            f"💨 {a.name} ({a.team}) 召唤师 [虚弱] 弱化 {target.name} ({target.team}) -50% 攻击 10 tick | 💨 {a.name} ({a.team}) SUMM Exhaust weakens {target.name} ({target.team}) -50% atk 10 ticks",
            f"💨 {a.name} ({a.team}) SUMM Exhaust weakens {target.name} ({target.team}) -50% atk 10 ticks",
        )
    elif spell == "ghost":
        a.speed_boost_ticks = 15
        m.append_log(
            f"👻 {a.name} ({a.team}) 召唤师 [幽灵疾步] 速度 +30% (15 tick) | 👻 {a.name} ({a.team}) SUMM Ghost +30% move 15 ticks",
            f"👻 {a.name} ({a.team}) SUMM Ghost +30% move 15 ticks",
        )
    elif spell == "smite" and enemies:
        # Instant-kill any enemy under 15% HP within 6 cells
        for e in enemies:
            dist = abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1])
            if dist <= 6 and e.hp / max(1, e.hp_max) < 0.15:
                e.hp = 0
                e.alive = False
                e.deaths += 1
                e.respawn_in = RESPAWN_TICKS
                a.kills += 1
                m.team_kills[a.team] += 1
                a.gold += GOLD_PER_KILL
                _try_buy_best_affordable(a, m)
                m.append_log(
                    f"💀 {a.name} ({a.team}) 召唤师 [晕跳] 处决 {e.name} ({e.team}) HP<15% | 💀 {a.name} ({a.team}) SUMM Smite executes {e.name} ({e.team}) (<15% HP)",
                    f"💀 {a.name} ({a.team}) SUMM Smite executes {e.name} ({e.team}) (<15% HP)",
                )
                return  # one-shot
    elif spell == "cleanse":
        # No-op in MVP (no debuffs to clean)
        m.append_log(
            f"✨ {a.name} ({a.team}) 召唤师 [净化] 解除自身控制 (MVP no-op) | ✨ {a.name} ({a.team}) SUMM Cleanse (MVP no-op)",
            f"✨ {a.name} ({a.team}) SUMM Cleanse (MVP no-op)",
        )


def _tick_spell_effects(m: ArenaMatch, tick_n: int) -> None:
    """Apply per-tick effects of ongoing spells: ignite DoT, speed buff,
    shield decay, exhaust tick, etc. Also auto-trigger spells on first
    eligible tick.
    """
    for a in m.blue + m.red:
        # V6: strict spell timing via _should_use_spell_now
        if _should_use_spell_now(a, m, tick_n):
            _cast_summoner_spell(a, m, tick_n)

        # Tick ongoing effects
        if a.ignite_ticks > 0:
            target = next((e for e in (m.blue + m.red) if e.pid == a.ignite_target_pid), None)
            if target and target.alive:
                dmg = 16  # 80 / 5 = 16/tick
                target.hp = max(0, target.hp - dmg)
                a.ignite_ticks -= 1
                m.append_log(
                    f"🔥 {target.name} 燃烧 -16 HP (剩 {a.ignite_ticks} tick) | 🔥 {target.name} burning -16 HP ({a.ignite_ticks} ticks left)",
                    f"🔥 {target.name} burning -16 HP ({a.ignite_ticks} ticks left)",
                )
                if target.hp == 0:
                    target.alive = False
                    target.deaths += 1
                    target.respawn_in = RESPAWN_TICKS
                    a.kills += 1
                    m.team_kills[a.team] += 1
                    a.gold += GOLD_PER_KILL
                    _try_buy_best_affordable(a, m)
                    m.append_log(
                        f"💀 {a.name} ({a.team}) 点燃处决 {target.name} | 💀 {a.name} ({a.team}) ignite executes {target.name}",
                        f"💀 {a.name} ({a.team}) ignite executes {target.name}",
                    )
            else:
                a.ignite_ticks = 0
        # Smite should already kill on cast; nothing to tick here
        if a.weakened_ticks > 0:
            a.weakened_ticks -= 1
        if a.speed_boost_ticks > 0:
            a.speed_boost_ticks -= 1
        if a.weakened_ticks > 0:
            a.weakened_ticks -= 1
        if a.speed_boost_ticks > 0:
            a.speed_boost_ticks -= 1
        # Shield decay happens on hit (see _combat_step)


def _spell_atk_modifier(a: ArenaAgent) -> float:
    """Return damage modifier (1.0 = no change) from active spell effects
    on this agent. Exhaust reduces atk; barrier absorbs separately."""
    if a.weakened_ticks > 0:
        return 0.5  # -50% atk
    return 1.0


def _shield_absorb(a, incoming: int) -> int:
    """Apply barrier shield to incoming damage; return the actual dmg taken."""
    if a is None:
        return incoming
    sr = getattr(a, "shield_remaining", 0)
    if sr and sr > 0 and incoming > 0:
        absorbed = min(sr, incoming)
        a.shield_remaining -= absorbed
        return incoming - absorbed
    return incoming




def _tick_ultimates(m: ArenaMatch, tick_n: int) -> None:
    """V6: Decrement all ultimates' cooldowns; trigger bot use when off-CD
    AND in a team-fight situation."""
    for a in m.blue + m.red:
        if a.ult_cd > 0:
            a.ult_cd = max(0, a.ult_cd - 1)
        if a.ult_cd == 0 and a.alive and _should_use_ult_now(a, m, tick_n):
            _use_ultimate(a, m, tick_n)


# ---------- module-level state: queue + active matches ----------

_queue: list[str] = []                       # waiting pids (in registration order)
_active_matches: dict[str, ArenaMatch] = {}
_lock = threading.Lock()


def queue_len() -> int:
    with _lock:
        return len(_queue)


def active_match_ids() -> list[str]:
    with _lock:
        return list(_active_matches.keys())


def enqueue(pid: str) -> int:
    """Add player to queue. Returns current queue length."""
    with _lock:
        if pid not in _queue:
            _queue.append(pid)
        return len(_queue)


def try_form_match(match_id: str, lookup_agent) -> ArenaMatch | None:
    """If queue has ≥10 pids, pop first 10 and form a 5v5 match via draft.

    `lookup_agent` is a callable(pid) -> ArenaAgent (caller provides
    the agent's name+class from the registered player). Returns None if
    fewer than 10 in queue.

    New: instead of jumping straight into a match, this now creates a
    ban/pick DRAFT (see arena_draft). The actual ArenaMatch is constructed
    later via form_match_from_draft() once the draft ends. For backwards
    compat with the 5v5_demo, callers should also support the legacy path.
    """
    with _lock:
        if len(_queue) < 10:
            return None
        pids = _queue[:10]
        del _queue[:10]

    # Lazy import to avoid circular
    from server import arena_draft as _draft_mod
    blue_pids = pids[:5]
    red_pids = pids[5:10]
    draft = _draft_mod.create_draft(blue_pids, red_pids)
    # Start a background tick thread for the draft
    import threading
    t = threading.Thread(target=_draft_tick_loop, args=(draft.draft_id,),
                         daemon=True, name=f"draft-{draft.draft_id}")
    t.start()
    # Don't create the ArenaMatch yet — that happens in _draft_tick_loop after
    # the draft ends. Return None; the API caller should poll /arena/draft/<id>
    # for status, then /arena/match/<match_id> once the match is up.
    return None


def form_match_from_draft(draft_id: str, lookup_agent) -> ArenaMatch | None:
    """Build the actual ArenaMatch once a draft has ended.

    Pulls hero assignments from the draft and overrides each agent's `cls`
    to match the picked hero's base class (warrior/mage/priest/hunter).
    """
    from server import arena_draft as _draft_mod
    d = _draft_mod.get_draft(draft_id)
    if d is None:
        return None
    # Auto-fill any missing picks before building
    _draft_mod.auto_fill_remaining(d)
    if d.picks_made < _draft_mod.PICKS_PER_TEAM * 2:
        return None

    blue_pids = d.blue_pids
    red_pids = d.red_pids

    def lookup(pid_q, team):
        ag = lookup_agent(pid_q, team)
        # Override cls from draft assignment if available
        hero_id = d.assignments.get(pid_q)
        if hero_id:
            ag.cls = _draft_mod.HERO_TO_BASE_CLASS.get(hero_id, ag.cls)
        return ag

    blue = [lookup(p, "blue") for p in blue_pids]
    red = [lookup(p, "red") for p in red_pids]

    # Load bot strategy profiles from DB (if any)
    _load_strategy_profiles(blue + red)

    # Auto-assign lanes by roster index: 0→top, 1→mid, 2→bot, 3→top, 4→mid
    for i, a in enumerate(blue):
        a.lane = LANES[i % 3]
    for i, a in enumerate(red):
        a.lane = LANES[i % 3]

    # Load per-agent ultimate (by base class) and summoner spell (from draft)
    for a in blue + red:
        ult_data = ULTIMATES.get(a.cls)
        if ult_data:
            a.ultimate = ult_data[0]
            a.ult_cd = 0  # ready immediately
        # Default spell if draft didn't set one
        spell_id = d.spells.get(a.pid, "heal")
        a.spell = spell_id
        a.spell_used = False
        # Initialize stats from base
        _recompute_agent_stats(a)

    # Build 6 towers (3 lanes × 2 teams × 2 kinds)
    towers = []
    for team in ("blue", "red"):
        for lane in LANES:
            for kind in ("outer", "inner"):
                towers.append(Tower(team=team, lane=lane, kind=kind))

    import secrets
    match_id = "mtch_" + secrets.token_hex(4)
    from server import arena_draft as _draft_for_mode
    m = ArenaMatch(
        match_id=match_id,
        blue=blue,
        red=red,
        blue_crystal=Crystal(team="blue"),
        red_crystal=Crystal(team="red"),
        started_at=time.time(),
        towers=towers,
    )
    # Tag match with its mode so observers know (1v1/3v3/5v5)
    m._mode = d.mode
    # Place agents at their team's side, on their lane's y
    for i, a in enumerate(blue):
        a.pos = (5, LANE_Y[a.lane])
    for i, a in enumerate(red):
        a.pos = (ARENA_W - 6, LANE_Y[a.lane])
    with _lock:
        _active_matches[match_id] = m

    d.append_log(
        f"⚔️ 比赛开始! 3 路推塔 (top/mid/bot) | ⚔️ Match starts! 3-lane push (top/mid/bot)",
        f"⚔️ Match starts! 3-lane push (top/mid/bot)",
    )
    return m


def _draft_tick_loop(draft_id: str) -> None:
    """Background thread: ticks the draft, when ended builds the ArenaMatch."""
    from server import arena_draft as _draft_mod
    d = _draft_mod.get_draft(draft_id)
    if d is None:
        return
    # Use a fresh lookup_agent callable — the arena_queue endpoint passes
    # its own, but we don't have it here. Build a default from players table.
    def lookup_agent(pid_q, team):
        # Lazy import to avoid circular
        from server.db import connect as _db_connect
        c = _db_connect()
        cur = c.cursor()
        cur.execute("SELECT id, name, cls FROM players WHERE id=?", (pid_q,))
        row = cur.fetchone()
        if row is None:
            return ArenaAgent(pid=pid_q, name=pid_q, cls="warrior", team=team)
        return ArenaAgent(pid=row["id"], name=row["name"], cls=row["cls"], team=team)

    while True:
        d = _draft_mod.get_draft(draft_id)
        if d is None:
            return
        # Auto-fill remaining picks proactively if either team is full but
        # the other team is still missing (i.e. picks_made >= PICKS_PER_TEAM).
        # This makes the demo fast — no need to wait for the 60-tick timeout.
        if not d.ended:
            _draft_mod.tick_draft(d)
            if (not d.ended
                and d.picks_made >= _draft_mod.PICKS_PER_TEAM):
                # One team is full; fill the other immediately.
                _draft_mod.auto_fill_remaining(d)
                if d.picks_made >= _draft_mod.PICKS_PER_TEAM * 2:
                    d.ended = True
                    d.append_log(
                        "⏰ 超时未选满, 服务器自动填满 | ⏰ Auto-fill complete (timeout)",
                        "⏰ Auto-fill complete (timeout)",
                    )
        if d.ended:
            # Build the match
            m = form_match_from_draft(draft_id, lookup_agent)
            if m is not None:
                # Start the match tick thread
                import threading as _thr
                t = _thr.Thread(target=_match_tick_loop, args=(m.match_id,),
                               daemon=True, name=f"arena-{m.match_id}")
                t.start()
            return
        time.sleep(1.0)






def _update_fitness(m: ArenaMatch, blue_pids: list, red_pids: list) -> None:
    """After a match, update each bot's fitness score and adjust strategy
    thresholds slightly toward "what worked".

    Fitness formula (per bot):
      win:    +1.0  + min(kills, 5) * 0.1  - deaths * 0.1
      loss:   -0.5  + kills * 0.1  - deaths * 0.05
      draw:   0  + (kills - deaths) * 0.1

    Strategy adjustment (small nudge, only for bots with ≥3 matches):
      if won: keep thresholds
      if lost: hp_retreat_threshold += 0.03 (more cautious)
                ult_teamfight_min_enemies += 1 (wait for more)
      clamp to [0.10, 0.60] for hp, [1, 8] for ult threshold
    """
    import sqlite3, json as _json
    try:
        c = sqlite3.connect(str(DB_PATH))
        cur = c.cursor()
        all_pids = list(blue_pids) + list(red_pids)
        for pid in all_pids:
            # Compute this bot's contribution
            agent = next((x for x in (m.blue + m.red) if x.pid == pid), None)
            if agent is None:
                continue
            won = (pid in (blue_pids if m.winner == "blue" else red_pids))
            kills, deaths = agent.kills, agent.deaths
            if won:
                fitness = 1.0 + min(kills, 5) * 0.1 - deaths * 0.1
                # Award marketplace credits to the bot's owner (v10)
                try:
                    _award_match_credits(pid, won=True)
                except Exception as e:
                    print(f"[credits] award failed: {e}")
            else:
                fitness = -0.5 + kills * 0.1 - deaths * 0.05
                # Small consolation credit for participation
                try:
                    _award_match_credits(pid, won=False)
                except Exception as e:
                    print(f"[credits] award failed: {e}")
            # Read current row (or insert defaults)
            row = cur.execute(
                "SELECT wins, losses, matches_played, fitness_history, "
                "hp_retreat_threshold, ult_teamfight_min_enemies "
                "FROM bot_strategy_profiles WHERE pid=?", (pid,)
            ).fetchone()
            if row is None:
                cur.execute("""INSERT INTO bot_strategy_profiles
                               (pid, wins, losses, matches_played, fitness_history, last_updated)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (pid,
                             1 if won else 0,
                             0 if won else 1,
                             1,
                             _json.dumps([fitness]),
                             time.time()))
            else:
                wins, losses, matches, hist_json, hp_thr, ult_min_en = row
                wins = wins + (1 if won else 0)
                losses = losses + (0 if won else 1)
                matches += 1
                hist = _json.loads(hist_json)
                hist.append(fitness)
                hist = hist[-30:]  # keep last 30
                # Strategy nudge after 3 matches
                if matches >= 3 and not won:
                    hp_thr = min(0.60, hp_thr + 0.03)
                    ult_min_en = min(8, ult_min_en + 1)
                cur.execute("""UPDATE bot_strategy_profiles SET
                               wins=?, losses=?, matches_played=?,
                               fitness_history=?,
                               hp_retreat_threshold=?,
                               ult_teamfight_min_enemies=?,
                               last_updated=? WHERE pid=?""",
                            (wins, losses, matches, _json.dumps(hist),
                             hp_thr, ult_min_en, time.time(), pid))
        c.commit()
        c.close()
    except Exception as e:
        print(f"[fitness update] failed: {e}")





# Item active skill effects (each item's active is identified by name)
ITEM_ACTIVE_DESCRIPTIONS = {}
for _slot, _items in EQUIPMENT_CATALOG.items():
    for _entry in _items:
        _name, _cost, _stats, _active_id, _active_desc = _entry
        if _active_id:
            ITEM_ACTIVE_DESCRIPTIONS[_active_id] = (_name, _active_desc)


def _agent_item_active(a: ArenaAgent) -> tuple:
    """Return (active_skill_id, item_name) if agent has an item with active."""
    for slot, item_id in a.equipment.items():
        if not item_id:
            continue
        for entry in EQUIPMENT_CATALOG.get(slot, []):
            name = entry[0]
            if name == item_id:
                active_id = entry[3]
                if active_id:
                    return (active_id, name)
    return (None, None)


def _tick_item_actives(m: ArenaMatch, tick_n: int) -> None:
    """Each tick, bots with item-actives may auto-trigger them based on
    simple conditions (low HP → undying/immortal, in teamfight → berserk,
    near enemies → blade_burst).
    """
    for a in m.blue + m.red:
        if not a.alive:
            continue
        active_id, item_name = _agent_item_active(a)
        # Ensure dicts exist on the agent (instance attrs)
        if not hasattr(a, "item_active_cd"):
            a.item_active_cd = {}
        if not hasattr(a, "active_buffs"):
            a.active_buffs = {}
        if not active_id or a.item_active_cd.get(active_id, 0) > 0:
            continue
        # Conditions per active skill
        state = _team_fight_state(a, m)
        trigger = False
        if active_id == "berserk" and state["near_enemies"] >= 1:
            trigger = True
        elif active_id == "blade_burst" and state["near_enemies"] >= 1:
            trigger = True
        elif active_id == "guard" and state["my_hp_pct"] < 0.40:
            trigger = True
        elif active_id == "haste" and state["near_enemies"] >= 2:
            trigger = True
        elif active_id == "phase" and state["near_enemies"] >= 1:
            trigger = True
        elif active_id == "thorns" and state["near_enemies"] >= 2:
            trigger = True
        elif active_id == "time_warp" and state["near_enemies"] >= 2:
            trigger = True
        elif active_id == "lifesteal" and state["my_hp_pct"] < 0.70:
            trigger = True
        if trigger:
            a.active_buffs[active_id] = tick_n + _item_active_duration(active_id)
            a.item_active_cd[active_id] = _item_active_cd_value(active_id)
            m.append_log(
                f"✨ {a.name} ({a.team}) 装备 [{item_name}] 触发 {active_id} ({ITEM_ACTIVE_DESCRIPTIONS[active_id][1]}) | "
                f"✨ {a.name} ({a.team}) item [{item_name}] triggers {active_id}",
                f"✨ {a.name} ({a.team}) item [{item_name}] triggers {active_id}",
            )


def _item_active_duration(active_id: str) -> int:
    return {
        "berserk": 6, "blade_burst": 1, "guard": 6, "haste": 10,
        "phase": 1, "thorns": 8, "time_warp": 5, "lifesteal": 5,
    }.get(active_id, 5)


def _item_active_cd_value(active_id: str) -> int:
    return {
        "berserk": 40, "blade_burst": 30, "guard": 40, "haste": 25,
        "phase": 60, "thorns": 35, "time_warp": 50, "lifesteal": 45,
    }.get(active_id, 30)


def _tick_item_cooldowns(m: ArenaMatch) -> None:
    """Decrement all item-active cooldowns each tick."""
    for a in m.blue + m.red:
        for active_id in list(a.item_active_cd.keys()):
            if a.item_active_cd[active_id] > 0:
                a.item_active_cd[active_id] -= 1
                if a.item_active_cd[active_id] <= 0:
                    del a.item_active_cd[active_id]





def _load_strategy_profiles(agents: list) -> None:
    """For each agent, load the bot_strategy_profiles row (if exists) and
    populate the agent's strategy fields (as instance attrs).
    Players without a profile keep defaults.
    """
    import sqlite3
    try:
        c = sqlite3.connect(str(DB_PATH))
        cur = c.cursor()
        for a in agents:
            row = cur.execute(
                "SELECT hp_retreat_threshold, teamfight_radius, teamfight_min_allies, "
                "teamfight_min_enemies, ult_teamfight_min_allies, ult_teamfight_min_enemies, "
                "ult_threshold FROM bot_strategy_profiles WHERE pid=?", (a.pid,)
            ).fetchone()
            if row is not None:
                (a.hp_retreat_threshold, a.teamfight_radius, a.teamfight_min_allies,
                 a.teamfight_min_enemies, a.ult_teamfight_min_allies,
                 a.ult_teamfight_min_enemies, a.ult_threshold) = row
        c.close()
    except Exception as e:
        print(f"[strategy profile] load failed: {e}")





# Random event types — added to the field at random intervals
EVENT_TYPES = [
    ("ambush",     "伏击",  "Ambush!",       5,   100,   {"hp": 50},      "A wild enemy scout appears in the jungle!"),
    ("airdrop",    "空投",  "Airdrop",       15,  300,   {"gold": 200},   "A supply crate falls — +200 gold for whoever reaches it first!"),
    ("wild_buff",  "野区符", "Wild Buff",    10,  60,    {"dmg_pct": 0.10, "duration": 30}, "A jungle spirit blesses the first player who finds it (+10% dmg 30 ticks)"),
    ("trap",       "陷阱",  "Trap",          8,   80,    {"dmg": 60},     "Hidden spike trap! Deals 60 dmg to the first player to step on it."),
    ("merchant",   "商人",  "Merchant",      12,  150,   {"random_item": "shadow_fang"}, "A wandering merchant appears — sells shadow_fang for 150g"),
    # === DLC events (v10) ===
    ("boss_raid",  "Boss 战", "Boss Raid",   8,   0,     {"boss_hp": 500, "boss_dmg": 30, "reward_per_hit": 5},
     "A world boss spawns in the river — both teams race to deal damage. Last hit gets +200g + dragon buff."),
    ("portal",     "传送门", "Portal",      6,   0,     {"teleport_to": "center"},
     "A glowing portal appears — stepping on it teleports you to the enemy base for 3 ticks."),
]


def _spawn_random_event(m: ArenaMatch, tick_n: int) -> None:
    """Every 15 ticks (after tick 10), 30% chance of a random event."""
    if tick_n < 10 or tick_n % 15 != 0:
        return
    import random as _r
    rng = _r.Random(tick_n + hash(m.match_id) % 1000)
    if rng.random() > 0.30:
        return
    kind, name_zh, name_en, pos_x_off, value, payload, desc = rng.choice(EVENT_TYPES)
    x = ARENA_W // 2 + rng.randint(-10, 10)
    y = rng.randint(5, ARENA_H - 5)
    pos = [x, y]
    m.events.append({
        "kind": kind, "name_zh": name_zh, "name_en": name_en,
        "pos": pos, "value": value, "payload": payload, "desc": desc,
        "spawn_tick": tick_n, "claimed_by": None, "claimed_tick": None,
    })
    m.append_log(
        f"🎲 随机事件 [{name_zh}] 在 ({x},{y}) 出现! {desc} | 🎲 Random event [{name_en}] at ({x},{y})!",
        f"🎲 Random event [{name_en}] at ({x},{y})!",
    )


def _process_event_claims(m: ArenaMatch, tick_n: int) -> None:
    """Agents within 2 cells of an event claim it."""
    for ev in m.events:
        if ev["claimed_by"]:
            continue
        candidates = [a for a in (m.blue + m.red) if a.alive]
        for a in candidates:
            if abs(a.pos[0] - ev["pos"][0]) + abs(a.pos[1] - ev["pos"][1]) > 2:
                continue
            ev["claimed_by"] = a.pid
            ev["claimed_tick"] = tick_n
            kind = ev["kind"]
            if kind == "ambush":
                a.hp = max(1, a.hp - 50)
                m.append_log(
                    f"⚔️ {a.name} ({a.team}) 遭遇伏击 -50 HP | ⚔️ {a.name} ({a.team}) ambushed -50 HP",
                    f"⚔️ {a.name} ({a.team}) ambushed -50 HP",
                )
            elif kind == "airdrop":
                a.gold += 200
                _try_buy_best_affordable(a, m)
                m.append_log(
                    f"📦 {a.name} ({a.team}) 拾取空投 +200g (gold={a.gold}) | 📦 {a.name} ({a.team}) airdrop +200g (gold={a.gold})",
                    f"📦 {a.name} ({a.team}) airdrop +200g (gold={a.gold})",
                )
            elif kind == "wild_buff":
                if not m.team_buffs.get(a.team) or m.team_buffs[a.team]["expires_at"] < tick_n + 30:
                    m.team_buffs[a.team] = {"dmg_pct": 0.10, "expires_at": tick_n + 30, "source": "wild_buff"}
                    m.append_log(
                        f"✨ {a.name} ({a.team}) 拾取野区符 全队 +10% 伤害 30 tick | ✨ {a.name} ({a.team}) wild buff team +10% dmg 30 ticks",
                        f"✨ {a.name} ({a.team}) wild buff team +10% dmg 30 ticks",
                    )
            elif kind == "trap":
                a.hp = max(1, a.hp - 60)
                m.append_log(
                    f"💥 {a.name} ({a.team}) 踩到陷阱 -60 HP | 💥 {a.name} ({a.team}) trap -60 HP",
                    f"💥 {a.name} ({a.team}) trap -60 HP",
                )
            elif kind == "merchant":
                if a.gold >= 150:
                    a.gold -= 150
                    a.equipment["weapon"] = "shadow_fang"
                    _recompute_agent_stats(a)
                    m.append_log(
                        f"🛒 {a.name} ({a.team}) 购买 [shadow_fang] (-150g) | 🛒 {a.name} ({a.team}) bought [shadow_fang] (-150g)",
                        f"🛒 {a.name} ({a.team}) bought [shadow_fang] (-150g)",
                    )
            break





def _tick_item_actives_for_one(a, m, tick_n):
    """Single-agent version of _tick_item_actives (for human action)."""
    if not a.alive:
        return
    active_id, item_name = _agent_item_active(a)
    if not hasattr(a, "item_active_cd"):
        a.item_active_cd = {}
    if not active_id or a.item_active_cd.get(active_id, 0) > 0:
        return
    state = _team_fight_state(a, m)
    trigger = False
    if active_id in ("berserk", "blade_burst") and state["near_enemies"] >= 1:
        trigger = True
    elif active_id == "guard" and state["my_hp_pct"] < 0.40:
        trigger = True
    elif active_id in ("haste", "thorns", "time_warp") and state["near_enemies"] >= 2:
        trigger = True
    elif active_id == "phase" and state["near_enemies"] >= 1:
        trigger = True
    elif active_id == "lifesteal" and state["my_hp_pct"] < 0.70:
        trigger = True
    if trigger:
        a.active_buffs = getattr(a, "active_buffs", {})
        a.active_buffs[active_id] = tick_n + _item_active_duration(active_id)
        a.item_active_cd[active_id] = _item_active_cd_value(active_id)
        m.append_log(
            f"✨ {a.name} ({a.team}) 装备 [{item_name}] 触发 {active_id} (人工) | ✨ {a.name} ({a.team}) item [{item_name}] triggers {active_id} (manual)",
            f"✨ {a.name} ({a.team}) item [{item_name}] triggers {active_id} (manual)",
        )





def _advance_tournament_bracket(match_id: str, winner: str) -> None:
    """If match_id is part of a tournament, advance the bracket.

    For MVP: tracks winners in the bracket. Actual next-round match
    creation requires player pids (we don't currently store them in
    tournaments.matches), so we defer that to a future trigger.
    """
    import json as _json
    try:
        from server.db import connect as _db
        c = _db()
        cur = c.cursor()
        cur.execute("SELECT id, bracket, status FROM tournaments WHERE matches LIKE ?",
                    (f'%"{match_id}"%',))
        t = cur.fetchone()
        if t is None:
            return
        tid = t["id"]
        bracket = _json.loads(t["bracket"])
        matches = _json.loads(t["matches"])
        # Find slot
        slot = None
        for k, v in matches.items():
            if v == match_id:
                slot = k
                break
        if slot is None or "_m" not in slot:
            return
        round_n = int(slot.split("_")[0][1:])
        round_key = f"round{round_n}"
        if round_key not in bracket:
            return
        for m in bracket[round_key]:
            if m["slot"] == slot:
                m["winner"] = winner
                break
        cur.execute("UPDATE tournaments SET bracket=? WHERE id=?",
                    (_json.dumps(bracket), tid))
        c.commit()
        # Check if all matches in this round have a winner
        all_done = all(m.get("winner") for m in bracket[round_key])
        if all_done and round_n > 0:
            print(f"[tournament {tid}] round {round_n} complete; awaiting admin trigger for next round")
    except Exception as ex:
        print(f"[tournament advance] failed: {ex}")


def _spawn_tournament_next_round(tournament_id: str) -> dict:
    """Admin endpoint helper: create next-round matches for a tournament.

    Looks at the bracket to find the next round to play (one where all
    prev-round winners are known). For each pair of winners, creates a
    fresh match (5v5/3v3/1v1) using the captain_pid from each team.
    Returns the new matches dict.
    """
    import json as _json, secrets as _sec, threading as _th
    from server import arena as _arena_mod
    from server import arena_draft as _draft_mod
    from server.db import connect as _db
    with _db_lock:
        c = _db()
        cur = c.cursor()
        cur.execute("SELECT bracket, matches, mode, size FROM tournaments WHERE id=?",
                    (tournament_id,))
        t = cur.fetchone()
        if t is None:
            return {"ok": False, "error": "tournament not found"}
        bracket = _json.loads(t["bracket"])
        mode = t["mode"]
        size = t["size"]
        # Find next round to play
        cur_round = 0
        for k in bracket.keys():
            rn = int(k.replace("round", ""))
            cur_round = max(cur_round, rn)
        cur_round += 1  # next round to create
        next_round_key = f"round{cur_round}"
        if cur_round * 2 > size:
            # Tournament is done
            cur.execute("UPDATE tournaments SET status='done', ended_at=? WHERE id=?",
                        (time.time(), tournament_id))
            c.commit()
            return {"ok": True, "status": "done"}
        # Find winners from previous round
        prev_key = f"round{cur_round - 1}"
        if prev_key not in bracket:
            return {"ok": False, "error": "no prev round data"}
        prev_matches = bracket[prev_key]
        if not all(m.get("winner") for m in prev_matches):
            return {"ok": False, "error": "previous round not finished"}
        # For each pair of winners, create match using team_teams captain_pid
        mode_size = (1 if mode == "1v1" else (3 if mode == "3v3" else 5))
        new_matches = _json.loads(t["matches"])
        new_bracket_round = []
        for i in range(0, len(prev_matches), 2):
            w1 = prev_matches[i]["winner"]
            w2 = prev_matches[i + 1]["winner"]
            # Find team_id by winner_team in tournament_teams
            cur.execute("""SELECT team_id, captain_pid, players FROM tournament_teams
                           WHERE tournament_id=? AND team_name=? LIMIT 1""",
                        (tournament_id, w1))
            t1 = cur.fetchone()
            cur.execute("""SELECT team_id, captain_pid, players FROM tournament_teams
                           WHERE tournament_id=? AND team_name=? LIMIT 1""",
                        (tournament_id, w2))
            t2 = cur.fetchone()
            if t1 is None or t2 is None:
                continue
            p1 = _json.loads(t1["players"]) or [t1["captain_pid"]]
            p2 = _json.loads(t2["players"]) or [t2["captain_pid"]]
            while len(p1) < mode_size:
                import secrets as _sec2
                p1.append("bot_" + _sec2.token_hex(2))
            while len(p2) < mode_size:
                p2.append("bot_" + _sec2.token_hex(2))
            new_slot = f"r{cur_round}_m{i//2}"
            mid = "mtch_" + _sec.token_hex(4)
            new_matches[new_slot] = mid
            new_bracket_round.append({"slot": new_slot, "match_id": mid,
                                      "team1": w1, "team2": w2})
            draft = _draft_mod.create_draft(p1[:mode_size], p2[:mode_size], mode=mode)
            t1_th = _th.Thread(target=_arena_mod._draft_tick_loop, args=(draft.draft_id,),
                               daemon=True, name=f"tdraft-{draft.draft_id}")
            t1_th.start()
        if next_round_key not in bracket:
            bracket[next_round_key] = []
        bracket[next_round_key] = new_bracket_round
        cur.execute("UPDATE tournaments SET bracket=?, matches=? WHERE id=?",
                    (_json.dumps(bracket), _json.dumps(new_matches), tournament_id))
        c.commit()
    return {"ok": True, "round": cur_round, "matches_created": len(new_bracket_round)}





# Set bonuses — when N+ items in a tier pattern are owned
SET_BONUSES = {
    # 3 件 dragon tier (helm/chest/boots) → 套装
    "dragon_armor": {
        "slots": [("helm", "dragon_helm"), ("chest", "dragon_scale"), ("boots", "dragon_talons")],
        "required": 3,
        "bonus": {"hp_max": 80, "atk": 10},
        "desc_zh": "龙鳞套装 3 件: 全属性 +hp80 +atk10",
        "desc_en": "Dragon set (3 pieces): +80 HP, +10 atk",
    },
    "fire_master": {
        "slots": [("weapon", "flame_blade"), ("chest", "iron_plate")],
        "required": 2,
        "bonus": {"atk_pct": 0.10},
        "desc_zh": "火焰大师 2 件: +10% 攻击",
        "desc_en": "Fire Master (2 pieces): +10% atk",
    },
    "lucky_striker": {
        "slots": [("weapon", "shadow_fang"), ("trinket", "dragon_eye")],
        "required": 2,
        "bonus": {"crit_pct": 0.10},  # extra 10% crit chance
        "desc_zh": "幸运打击 2 件: +10% 暴击",
        "desc_en": "Lucky Striker (2 pieces): +10% crit",
    },
}


def _compute_set_bonuses(a: ArenaAgent) -> tuple:
    """Check what set bonuses the agent qualifies for.

    Returns (set_name or None, bonus_dict, desc_text).
    """
    qualified = []
    for set_name, defn in SET_BONUSES.items():
        owned = sum(
            1 for slot, item in defn["slots"]
            if a.equipment.get(slot) == item
        )
        if owned >= defn["required"]:
            qualified.append((set_name, defn))
    if not qualified:
        return (None, {}, "")
    set_name, defn = qualified[0]
    desc = defn.get(f"desc_{('zh' if a.team else 'en')}", defn.get("desc_en", ""))
    return (set_name, defn["bonus"], desc)


def _apply_set_bonus_stats(bonus: dict, a: ArenaAgent) -> None:
    """Apply a set bonus to an agent's effective stats (called from _recompute_agent_stats)."""
    if bonus.get("hp_max"):
        a.hp_max += bonus["hp_max"]
        a.hp = max(1, int(a.hp_max * (a.hp / max(1, a.hp_max)) ))
    if bonus.get("atk"):
        a.atk += bonus["atk"]
    # Note: atk_pct / crit_pct are queried at combat time, not precomputed.


# Summoner + Ult combo bonuses — triggers when player has specific combo
SPELL_ULT_COMBOS = {
    # heal + priest_resurrect: emergency self-heal at death
    ("heal", "priest_resurrect"): {
        "bonus_zh": "神圣医疗: 死亡时自动 heal 30% HP (一次性)",
        "bonus_en": "Divine Heal: on death auto-restore 30% HP (one-time)",
        "trigger": "on_death_low_hp",
        "effect": {"auto_heal_pct": 0.30},
    },
    # flash + warrior_charge: double-tap blink + charge
    ("flash", "warrior_charge"): {
        "bonus_zh": "冲锋闪现: 冲锋距离 +50%, 命中伤害 +20%",
        "bonus_en": "Charge Flash: charge range +50%, dmg +20%",
        "trigger": "on_ult",
        "effect": {"charge_range_mult": 1.5, "ult_dmg_mult": 1.2},
    },
    # ignite + mage_meteor: meteor applies ignite stacks on all
    ("ignite", "mage_meteor"): {
        "bonus_zh": "燃烧陨石: meteor 范围 +3 cells, 每个敌人附加 5 tick ignite",
        "bonus_en": "Burning Meteor: +3 cell radius, each enemy gets 5-tick ignite",
        "trigger": "on_ult",
        "effect": {"radius_bonus": 3, "apply_ignite": 5},
    },
    # exhaust + hunter_snipe: snipe applies exhaust
    ("exhaust", "hunter_snipe"): {
        "bonus_zh": "虚弱狙击: 命中目标 atk -50% 持续 10 tick",
        "bonus_en": "Exhausting Snipe: target atk -50% for 10 ticks",
        "trigger": "on_ult",
        "effect": {"apply_exhaust_ticks": 10},
    },
    # ghost + warrior_charge: charge gets +movement speed during travel
    ("ghost", "warrior_charge"): {
        "bonus_zh": "幽灵冲锋: 冲锋过程不可阻挡 + 不触发敌方警觉",
        "bonus_en": "Ghost Charge: unstoppable, no enemy aggro during charge",
        "trigger": "on_ult",
        "effect": {"unstoppable": True},
    },
}


def _get_combo_for_agent(a: ArenaAgent) -> dict | None:
    """Return the combo bonus dict for this agent's (spell, ult), or None."""
    if not a.spell or not a.ultimate:
        return None
    key = (a.spell, a.ultimate)
    return SPELL_ULT_COMBOS.get(key)


def _check_set_bonus_event(a: ArenaAgent, m: ArenaMatch, tick_n: int) -> None:
    """When an agent's equipment changes (e.g. bought new item), log
    when they newly qualify for a set bonus.
    """
    prev = getattr(a, "_last_set", None)
    set_name, bonus, desc = _compute_set_bonuses(a)
    cur = set_name if set_name else None
    if cur != prev and cur is not None:
        m.append_log(
            f"🎁 {a.name} ({a.team}) 触发套装 [{cur}] {desc} | 🎁 {a.name} ({a.team}) triggers set [{cur}] {desc}",
            f"🎁 {a.name} ({a.team}) triggers set [{cur}] {desc}",
        )
    a._last_set = cur





def _check_set_bonuses(m: ArenaMatch, tick_n: int) -> None:
    """Periodically check if agents newly qualify for set bonuses."""
    for a in m.blue + m.red:
        _check_set_bonus_event(a, m, tick_n)



def _save_replay(m: ArenaMatch) -> None:
    """Persist the entire match history to data/replays/<match_id>.json.

    Each tick is a snapshot of agents, crystals, towers, dragons, buffs.
    This is the source of truth for the /replay.html playback.
    """
    import json as _json
    from pathlib import Path as _P
    replay_dir = _P("data/replays")
    replay_dir.mkdir(parents=True, exist_ok=True)
    snap_path = replay_dir / f"{m.match_id}.json"
    out = {
        "match_id": m.match_id,
        "started_at": m.started_at,
        "ended": m.ended,
        "winner": m.winner,
        "tick": m.tick,
        "blue": [
            {"pid": a.pid, "name": a.name, "cls": a.cls, "lane": a.lane,
             "kills": a.kills, "deaths": a.deaths, "gold": a.gold,
             "ultimate": a.ultimate, "ult_cd": a.ult_cd,
             "spell": a.spell, "spell_used": a.spell_used,
             "equipment": dict(a.equipment)}
            for a in m.blue
        ],
        "red": [
            {"pid": a.pid, "name": a.name, "cls": a.cls, "lane": a.lane,
             "kills": a.kills, "deaths": a.deaths, "gold": a.gold,
             "ultimate": a.ultimate, "ult_cd": a.ult_cd,
             "spell": a.spell, "spell_used": a.spell_used,
             "equipment": dict(a.equipment)}
            for a in m.red
        ],
        "log": [
            {"tick": t, "zh": m_zh, "en": m_en}
            for (t, m_zh, m_en) in m.log
        ],
    }
    snap_path.write_text(_json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def _match_tick_loop(match_id: str) -> None:
    """Background loop that advances a single arena match by 1 tick/second.
    When the match ends, applies rank-rating updates for all 10 players."""
    import random as _r
    rng = _r.Random()
    while True:
        m = get_match(match_id)
        if m is None:
            return
        if m.ended:
            # Apply rank-rating updates
            try:
                blue_pids = [a.pid for a in m.blue]
                red_pids = [a.pid for a in m.red]
                apply_match_result(blue_pids, red_pids, m.winner or "blue")
            except Exception as ex:
                print(f"[rank] failed to apply: {ex}")
            # Update bot fitness + nudge strategy
            try:
                _update_fitness(m, blue_pids, red_pids)
            except Exception as ex:
                print(f"[fitness] failed to apply: {ex}")
            # Save replay to disk
            try:
                _save_replay(m)
            except Exception as ex:
                print(f"[replay] failed to save: {ex}")
            return
        tick_match(m, rng)
        time.sleep(1.0)


def get_match(match_id: str) -> ArenaMatch | None:
    with _lock:
        return _active_matches.get(match_id)


def all_matches() -> list[ArenaMatch]:
    with _lock:
        return list(_active_matches.values())


def remove_match(match_id: str) -> None:
    with _lock:
        _active_matches.pop(match_id, None)


# ---------- per-tick simulation ----------

def tick_match(m: ArenaMatch, rng: random.Random | None = None) -> None:
    """Advance the match by one tick.

    Order per tick:
      0. Spawn neutral dragons if it's their spawn tick.
      1. Respawn dead agents whose cooldown expired.
      2. Each alive agent attacks nearest priority target (dragon > enemy >
         enemy crystal). Melee swings only (no skills here — keeps MVP simple).
      3. If killer is in range of enemy crystal, damage crystal.
      4. Death events: killer gets a kill, victim dies, respawn timer set.
         If a dragon dies, the killer's team gets a damage buff for X ticks.
      5. Check crystal HP == 0 → match ends.
    """
    if m.ended:
        return
    rng = rng or random.Random()
    with m.lock:
        m.tick += 1
        tick_n = m.tick

    _spawn_dragons(m, tick_n)
    _spawn_random_event(m, tick_n)
    _patrol_dragons(m)
    _respawn_step(m, tick_n)
    _periodic_shop_step(m, tick_n)
    _check_set_bonuses(m, tick_n)
    _expire_buffs(m, tick_n)
    _tick_spell_effects(m, tick_n)
    _tick_item_actives(m, tick_n)
    _tick_item_cooldowns(m)
    _tick_ultimates(m, tick_n)
    _process_event_claims(m, tick_n)
    _combat_step(m, rng, tick_n)
    _push_towers_step(m, tick_n)
    _check_crystals(m, tick_n)


def _spawn_dragons(m: ArenaMatch, tick_n: int) -> None:
    """Spawn a neutral dragon at its scheduled tick if not already active."""
    for kind, spawn_tick in DRAGON_SPAWN_TICKS.items():
        if tick_n != spawn_tick:
            continue
        if any(d["kind"] == kind for d in m.dragons):
            continue  # already alive
        m.dragons.append({
            "kind": kind,
            "hp": DRAGON_HP[kind],
            "hp_max": DRAGON_HP[kind],
            "pos": [ARENA_W // 2, ARENA_H // 2],  # river / center
            "last_hit_team": None,
        })
        zh = ("小龙" if kind == "young" else "大龙")
        en = ("Young Dragon" if kind == "young" else "Elder Dragon")
        m.append_log(
            f"🐉 {zh} 在河道刷新! ({DRAGON_HP[kind]} HP, 击杀全队 +{int(DRAGON_REWARD[kind]['dmg_pct']*100)}% 伤害 {DRAGON_REWARD[kind]['duration']} tick) | 🐉 {en} spawned in river! ({DRAGON_HP[kind]} HP, kill gives team +{int(DRAGON_REWARD[kind]['dmg_pct']*100)}% dmg for {DRAGON_REWARD[kind]['duration']} ticks)",
            f"🐉 {en} spawned in river! ({DRAGON_HP[kind]} HP, kill gives team +{int(DRAGON_REWARD[kind]['dmg_pct']*100)}% dmg for {DRAGON_REWARD[kind]['duration']} ticks)"
        )


def _patrol_dragons(m: ArenaMatch) -> None:
    """Dragons wander around the center river lane each tick (Honor-of-Kings).
    This makes the fight feel alive — agents have to chase a moving target.
    Dragons are slow (1 cell per 2 ticks) and stay within the river corridor.
    """
    import random as _r
    for d in m.dragons:
        # Use deterministic motion based on (kind, tick) so each dragon has a
        # unique wander path. Speed = 1 cell per 2 ticks.
        seed_val = (hash(d["kind"]) ^ (m.tick // 2)) & 0xFFFFFFFF
        rng = _r.Random(seed_val)
        dx = rng.choice([-1, 0, 0, 1])   # bias to stay
        dy = rng.choice([-1, 0, 0, 1])
        # Restrict to river corridor: x in [mid-river band], y anywhere
        x0, y0 = d["pos"]
        nx = max(ARENA_W // 2 - 5, min(ARENA_W // 2 + 5, x0 + dx))
        ny = max(5, min(ARENA_H - 5, y0 + dy))
        d["pos"] = [nx, ny]


def _expire_buffs(m: ArenaMatch, tick_n: int) -> None:
    """Drop buffs whose duration has expired."""
    expired = []
    for team, buff in list(m.team_buffs.items()):
        if buff.get("expires_at", 0) <= tick_n:
            expired.append(team)
            zh = "小龙" if buff["source"] == "young" else "大龙"
            m.append_log(
                f"{team}队的 {zh} buff 已结束 | {team} team's {buff['source']} dragon buff expired",
                f"{team} team's {buff['source']} dragon buff expired"
            )
    for team in expired:
        m.team_buffs.pop(team, None)


def _apply_buff(m: ArenaMatch, team: str, dragon_kind: str, tick_n: int) -> None:
    """Record (or stack) the team buff from killing a dragon."""
    reward = DRAGON_REWARD[dragon_kind]
    existing = m.team_buffs.get(team)
    if existing and existing.get("expires_at", 0) > tick_n:
        # Refresh + stack (Honor-of-Kings: elder buff replaces young)
        existing["dmg_pct"] = max(existing["dmg_pct"], reward["dmg_pct"])
        existing["expires_at"] = tick_n + reward["duration"]
        existing["source"] = dragon_kind
    else:
        m.team_buffs[team] = {
            "dmg_pct": reward["dmg_pct"],
            "expires_at": tick_n + reward["duration"],
            "source": dragon_kind,
        }




def _periodic_shop_step(m: ArenaMatch, tick_n: int) -> None:
    """Every 10 ticks, each alive agent tries to buy the best affordable item.
    This ensures bots gear up even if they haven't killed anything yet.
    """
    if tick_n % 10 != 0:
        return
    for a in m.blue + m.red:
        if not a.alive:
            continue
        if a.gold < 100:
            continue
        _try_buy_best_affordable(a, m)



def _respawn_step(m: ArenaMatch, tick_n: int) -> None:
    for a in m.blue + m.red:
        if not a.alive and a.respawn_in > 0:
            a.respawn_in -= 1
            if a.respawn_in <= 0:
                a.alive = True
                a.hp = a.hp_max
                a.mp = 60
                # respawn at home on their lane's y
                if a.team == "blue":
                    a.pos = (5, LANE_Y.get(a.lane, 25))
                else:
                    a.pos = (ARENA_W - 6, LANE_Y.get(a.lane, 25))
                m.append_log(
                    f"{a.name} ({a.team}/{a.lane}) 已复活 | {a.name} ({a.team}/{a.lane}) respawned",
                    f"{a.name} ({a.team}/{a.lane}) respawned",
                )




def _count_nearby(a: ArenaAgent, others: list, radius: int = 5) -> int:
    """Count how many of `others` are within `radius` cells of a (Manhattan)."""
    return sum(1 for o in others
               if abs(o.pos[0] - a.pos[0]) + abs(o.pos[1] - a.pos[1]) <= radius)


def _team_fight_state(a: ArenaAgent, m: ArenaMatch) -> dict:
    """Snapshot of the local battlefield around agent a.

    Returns counts of allies/enemies within 5 cells, plus whether a dragon
    is nearby. Used by the bot decision tree.
    """
    all_alive = [x for x in (m.blue + m.red) if x.alive]
    allies = [x for x in all_alive if x.team == a.team and x.pid != a.pid]
    enemies = [x for x in all_alive if x.team != a.team]
    near_allies = _count_nearby(a, allies, radius=5)
    near_enemies = _count_nearby(a, enemies, radius=5)
    dragon_near = False
    if m.dragons:
        dragon_near = any(abs(d["pos"][0] - a.pos[0]) + abs(d["pos"][1] - a.pos[1]) <= 6
                          for d in m.dragons)
    return {
        "near_allies": near_allies,
        "near_enemies": near_enemies,
        "dragon_near": dragon_near,
        "my_hp_pct": a.hp / max(1, a.hp_max),
        "ally_count_alive": len(allies) + 1,
        "enemy_count_alive": len(enemies),
    }


def _should_use_spell_now(a: ArenaAgent, m: ArenaMatch, tick_n: int) -> bool:
    """Strict spell timing: only fire when conditions make it count."""
    if a.spell_used or not a.spell or not a.alive:
        return False
    s = a.spell
    state = _team_fight_state(a, m)
    if s == "heal":
        # Was 40% (too eager). Now 20% (only when near death).
        return state["my_hp_pct"] < 0.20
    if s == "barrier":
        # Was 60%. Now 40% — only when in real danger.
        return state["my_hp_pct"] < 0.40 and state["near_enemies"] >= 1
    if s == "exhaust":
        # Only on a threat target that we are actually engaging.
        enemies = [e for e in (m.blue + m.red)
                   if e.alive and e.team != a.team
                   and abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]) <= 3
                   and e.hp / max(1, e.hp_max) > 0.20]
        return len(enemies) > 0
    if s == "ignite":
        # Only when an enemy is in melee range.
        return any(abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]) <= 3
                   for e in (m.blue + m.red) if e.alive and e.team != a.team)
    if s == "smite":
        # Only when an enemy is low and within execute range.
        return any(abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]) <= 6
                   and e.hp / max(1, e.hp_max) < 0.15
                   for e in (m.blue + m.red) if e.alive and e.team != a.team)
    if s == "flash":
        # Flash away when critical HP and enemies nearby.
        return state["my_hp_pct"] < 0.20 and state["near_enemies"] >= 1
    if s == "ghost":
        # Only at the start of the match (first 5 ticks).
        return tick_n <= 5
    if s == "cleanse":
        return False  # no debuffs in MVP
    return False


def _should_use_ult_now(a: ArenaAgent, m: ArenaMatch, tick_n: int) -> bool:
    """Strict ult timing: only fire during team fights (per-agent threshold)."""
    if a.ult_cd > 0 or not a.ultimate or not a.alive:
        return False
    state = _team_fight_state(a, m)
    min_a = getattr(a, "ult_teamfight_min_allies", 1)
    min_e = getattr(a, "ult_teamfight_min_enemies", 1)
    return (state["near_allies"] >= min_a
            and state["near_enemies"] >= min_e)


def _bot_think(a: ArenaAgent, m: ArenaMatch) -> dict:
    """The 5-rule decision tree (per-agent thresholds via a.*_threshold fields).

    Returns {"decision": str, "target": Agent|None, "reason": str}.
    """
    state = _team_fight_state(a, m)

    # Rule 1: RETREAT — low HP (per-agent threshold; default 0.30)
    hp_thr = getattr(a, "hp_retreat_threshold", 0.30)
    if state["my_hp_pct"] < hp_thr:
        return {"decision": "retreat", "target": None,
                "reason": f"HP {state['my_hp_pct']*100:.0f}% < {hp_thr*100:.0f}% — retreat"}

    # Rule 2: TEAMFIGHT — both teams within teamfight_radius
    tf_radius = getattr(a, "teamfight_radius", 5)
    tf_min_a = getattr(a, "teamfight_min_allies", 1)
    tf_min_e = getattr(a, "teamfight_min_enemies", 1)
    if (state["near_allies"] >= tf_min_a
            and state["near_enemies"] >= tf_min_e):
        return {"decision": "teamfight", "target": None,
                "reason": f"teamfight ({state['near_allies']}+{state['near_enemies']} within {tf_radius})"}

    # Rule 3: CONTEST — dragon is near and no big enemy force
    if state["dragon_near"] and state["near_enemies"] < 2:
        return {"decision": "contest", "target": None,
                "reason": f"dragon near (enemies={state['near_enemies']}, contestable)"}

    # Rule 4: PUSH — outer enemy tower in my lane is destroyed, push inner
    my_lane_towers = [t for t in m.towers
                      if t.team != a.team and t.lane == a.lane]
    outer_dead = all(t.hp <= 0 for t in my_lane_towers if t.kind == "outer")                  if any(t.kind == "outer" for t in my_lane_towers) else False
    if outer_dead:
        return {"decision": "push", "target": None,
                "reason": f"lane {a.lane} outer dead — push inner"}

    # Rule 5: FARM (default) — find nearest enemy and pressure them
    enemies = [e for e in (m.blue + m.red) if e.alive and e.team != a.team]
    if enemies:
        nearest = min(enemies, key=lambda e: abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]))
        return {"decision": "farm", "target": nearest,
                "reason": "default — nearest enemy"}

    # No enemies? Push lane.
    return {"decision": "push", "target": None, "reason": "no enemies — push lane"}




def _combat_step(m: ArenaMatch, rng: random.Random, tick_n: int) -> None:
    """Each alive agent attacks nearest priority target.

    Priority order: dragon (if any alive and in range) > enemy > enemy crystal.

    Damage formula: base_dmg * (1 + team_buff_dmg_pct) if team has an active buff.
    When a dragon dies, the killer's team gets a damage buff (see _apply_buff).
    """
    all_alive = [a for a in (m.blue + m.red) if a.alive]
    for a in all_alive:
        # 1. Find nearest alive dragon (any team can contest)
        dragon_target = None
        if m.dragons:
            dragon_target = min(m.dragons,
                                key=lambda d: abs(d["pos"][0] - a.pos[0]) + abs(d["pos"][1] - a.pos[1]))
        # 2. Find nearest enemy
        enemies = [e for e in all_alive if e.team != a.team]
        enemy_target = None
        if enemies:
            enemy_target = min(enemies,
                               key=lambda e: abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]))

        # === V6: bot decision tree replaces fixed "nearest" logic ===
        decision = _bot_think(a, m)
        # Log the decision so observers can see why the bot chose this action
        a._last_decision = (decision["decision"], decision["reason"])
        m.append_log(
            f"🧠 {a.name} ({a.team}/{a.lane}) 决策 [{decision['decision']}] {decision['reason']} | "
            f"🧠 {a.name} ({a.team}/{a.lane}) decides [{decision['decision']}] {decision['reason']}",
            f"🧠 {a.name} ({a.team}/{a.lane}) decides [{decision['decision']}] {decision['reason']}",
        )

        # Resolve target based on decision
        kind = None
        target = None
        if decision["decision"] == "retreat":
            # Move toward our crystal (backline)
            our_crystal = m.blue_crystal if a.team == "blue" else m.red_crystal
            tx, ty = our_crystal.pos
            kind = "retreat"
        elif decision["decision"] == "teamfight":
            # Move toward the closest enemy while staying with allies
            enemies = [e for e in all_alive if e.team != a.team]
            if enemies:
                target = min(enemies,
                             key=lambda e: abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]))
                tx, ty = target.pos
                kind = "enemy"
            else:
                tx, ty = m.blue_crystal.pos if a.team == "red" else m.red_crystal.pos
                kind = "push"
        elif decision["decision"] == "contest" and m.dragons:
            target = min(m.dragons,
                         key=lambda d: abs(d["pos"][0] - a.pos[0]) + abs(d["pos"][1] - a.pos[1]))
            tx, ty = target["pos"]
            kind = "dragon"
        elif decision["decision"] == "push":
            # Move toward the inner tower in my lane; if already down, move to crystal
            my_lane_towers = [t for t in m.towers
                              if t.team != a.team and t.lane == a.lane]
            target = None
            for t in my_lane_towers:
                if t.kind == "inner" and t.hp > 0:
                    target = t
                    break
            if target is None:
                # Inner already down → go for crystal
                their_crystal = m.red_crystal if a.team == "blue" else m.blue_crystal
                tx, ty = their_crystal.pos
                kind = "crystal"
            else:
                tx, ty = target.pos
                kind = "tower"
        else:  # "farm" or any fallback
            enemies = [e for e in all_alive if e.team != a.team]
            if enemies:
                target = min(enemies,
                             key=lambda e: abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]))
                tx, ty = target.pos
                kind = "enemy"
            else:
                continue  # nothing to do

        # Compute Manhattan distance to chosen target
        dist = abs(tx - a.pos[0]) + abs(ty - a.pos[1])

        # If far, step toward chosen target (1 cell/tick)
        if dist > 1:
            dx = (1 if tx > a.pos[0] else (-1 if tx < a.pos[0] else 0))
            dy = (1 if ty > a.pos[1] else (-1 if ty < a.pos[1] else 0))
            new_pos = (a.pos[0] + dx, a.pos[1] + dy)
            a.pos = (max(1, min(ARENA_W - 2, new_pos[0])),
                     max(1, min(ARENA_H - 2, new_pos[1])))
            continue  # movement only, no attack this tick

        # Melee range: hit target with team-buff-aware damage
        dmg = max(1, a.atk + rng.randint(0, 3))
        crit = rng.random() < 0.15
        if crit:
            dmg = int(dmg * 2)
        # Apply team buff (additive on dmg_pct)
        team_buff = m.team_buffs.get(a.team)
        if team_buff and team_buff.get("expires_at", 0) > tick_n:
            dmg = int(dmg * (1 + team_buff["dmg_pct"]))
        # Apply summoner spell effects (exhaust on attacker, barrier on target)
        dmg = int(dmg * _spell_atk_modifier(a))
        # Apply to target
        if kind == "dragon":
            target["hp"] -= dmg
            target["last_hit_team"] = a.team
            if target["hp"] <= 0:
                # Dragon slain — apply buff to killer's team + reward gold
                _apply_buff(m, a.team, target["kind"], tick_n)
                a.gold += 200  # dragon kill bonus
                dragon_zh = ("小龙" if target["kind"] == "young" else "大龙")
                dragon_en = ("Young Dragon" if target["kind"] == "young" else "Elder Dragon")
                m.append_log(
                    f"🐉 {a.name} ({a.team}) 击杀 {dragon_zh}! 团队获得 +{int(DRAGON_REWARD[target['kind']]['dmg_pct']*100)}% 伤害 buff ({DRAGON_REWARD[target['kind']]['duration']} tick) +200g | 🐉 {a.name} ({a.team}) slayed {dragon_en}! Team gets +{int(DRAGON_REWARD[target['kind']]['dmg_pct']*100)}% dmg buff ({DRAGON_REWARD[target['kind']]['duration']} ticks) +200g",
                    f"🐉 {a.name} ({a.team}) slayed {dragon_en}! Team gets +{int(DRAGON_REWARD[target['kind']]['dmg_pct']*100)}% dmg buff ({DRAGON_REWARD[target['kind']]['duration']} ticks) +200g",
                )
                # Remove from dragons list
                m.dragons[:] = [d for d in m.dragons if d["kind"] != target["kind"]]
            elif tick_n % 2 == 0:
                dragon_zh = ("小龙" if target["kind"] == "young" else "大龙")
                dragon_en = ("Young Dragon" if target["kind"] == "young" else "Elder Dragon")
                m.append_log(
                    f"⚔️ {a.name} ({a.team}) 对 {dragon_zh} 造成 {dmg} 伤害 {'暴击!' if crit else ''} | ⚔️ {a.name} hits {dragon_en} for {dmg} {'CRIT!' if crit else ''}",
                    f"⚔️ {a.name} hits {dragon_en} for {dmg} {'CRIT!' if crit else ''}",
                )
            continue  # dragon combat resolved; don't fall through to enemy code

        # Enemy target hit
        dmg = _shield_absorb(target, dmg)
        if dmg <= 0:
            continue
        target.hp -= dmg
        if target.hp <= 0:
            target.alive = False
            target.hp = 0
            target.deaths += 1
            target.respawn_in = RESPAWN_TICKS
            a.kills += 1
            m.team_kills[a.team] += 1
            # Gold rewards
            a.gold += GOLD_PER_KILL
            target.gold = max(0, target.gold + GOLD_ON_DEATH)
            m.append_log(
                f"{a.name} ({a.team}) 击杀 {target.name} ({target.team}) 暴击={crit} 伤害={dmg} +{GOLD_PER_KILL}g | {a.name} ({a.team}) killed {target.name} ({target.team}) crit={crit} dmg={dmg} +{GOLD_PER_KILL}g",
                f"{a.name} ({a.team}) killed {target.name} ({target.team}) crit={crit} dmg={dmg} +{GOLD_PER_KILL}g",
            )
            # Try to buy equipment with the gold
            _try_buy_best_affordable(a, m)
        else:
            if tick_n % 2 == 0:
                m.append_log(
                    f"{a.name} 对 {target.name} 造成 {dmg} 伤害 {'暴击!' if crit else ''} | {a.name} hits {target.name} for {dmg} {'CRIT!' if crit else ''}",
                    f"{a.name} hits {target.name} for {dmg} {'CRIT!' if crit else ''}",
                )


def _push_towers_step(m: ArenaMatch, tick_n: int) -> None:
    """3-lane tower push: an alive agent standing in range of an enemy tower
    damages that tower. Outer towers die before inner towers; when both
    are gone the agent can push the crystal (handled in _check_crystals).
    """
    PUSH_RANGE = 4  # cells
    # Only consider ENEMY towers (those defending the other base). Each tower
    # is "owned" by its defending team, so the OPPOSITE team can attack it.
    for t in m.towers:
        if t.hp <= 0:
            continue  # already destroyed
        attacker_team = "red" if t.team == "blue" else "blue"
        attackers = m.blue if attacker_team == "blue" else m.red
        # Find any alive attacker in range AND on the same lane
        in_range = [
            a for a in attackers
            if a.alive
            and a.lane == t.lane
            and abs(a.pos[0] - t.pos[0]) <= PUSH_RANGE
            and abs(a.pos[1] - t.pos[1]) <= PUSH_RANGE
        ]
        if not in_range:
            continue
        # Multiple agents in range stack damage
        total_dmg = TOWER_DMG_PER_TICK * len(in_range)
        t.hp -= total_dmg
        if t.hp <= 0:
            t.hp = 0
            lane_zh = {"top": "上路", "mid": "中路", "bot": "下路"}[t.lane]
            kind_zh = "外塔" if t.kind == "outer" else "高地塔"
            kind_en = "outer tower" if t.kind == "outer" else "inner tower"
            lane_en = t.lane
            m.append_log(
                f"🏰 {attacker_team}队 推掉 {lane_zh} {kind_zh}! | 🏰 {attacker_team} team destroyed {lane_en} {kind_en}!",
                f"🏰 {attacker_team} team destroyed {lane_en} {kind_en}!",
            )
        elif tick_n % 6 == 0:  # don't spam
            lane_zh = {"top": "上路", "mid": "中路", "bot": "下路"}[t.lane]
            kind_zh = "外塔" if t.kind == "outer" else "高地塔"
            kind_en = ("outer tower" if t.kind == "outer" else "inner tower")
            m.append_log(
                f"⚔️ {attacker_team}队 攻击 {lane_zh} {kind_zh} 伤害 {total_dmg} ({t.hp}/{t.hp_max}) | ⚔️ {attacker_team} team hits {t.lane} {kind_en} for {total_dmg} ({t.hp}/{t.hp_max})",
                f"⚔️ {attacker_team} team hits {t.lane} {kind_en} for {total_dmg} ({t.hp}/{t.hp_max})",
            )


def _check_crystals(m: ArenaMatch, tick_n: int) -> None:
    """If any agent is on enemy half, they damage the enemy crystal each tick
    (light damage; just to ensure crystal HP trends down and the match ends).
    """
    for a in m.blue + m.red:
        if not a.alive:
            continue
        enemy_crystal = m.red_crystal if a.team == "blue" else m.blue_crystal
        if a.team == "blue":
            in_enemy_half = a.pos[0] > ARENA_W // 2
        else:
            in_enemy_half = a.pos[0] < ARENA_W // 2
        if in_enemy_half:
            dmg = max(1, a.atk // 2 + rng_random_int(0, 3))   # module-level helper
            enemy_crystal.hp -= dmg
            m.team_dmg_to_crystal[a.team] += dmg
            if tick_n % 4 == 0:  # don't spam crystal hit logs
                m.append_log(
                    f"⚔️ {a.name} ({a.team}) 攻击 {a.team}水晶 伤害 {dmg} | ⚔️ {a.name} ({a.team}) attacks enemy crystal for {dmg}",
                    f"⚔️ {a.name} ({a.team}) attacks enemy crystal for {dmg}",
                )
            if enemy_crystal.hp <= 0:
                enemy_crystal.hp = 0
                if not m.ended:
                    m.ended = True
                    m.winner = a.team
                    m.append_log(
                        f"💥 {a.team}队 推爆 敌方水晶! 胜利! | 💥 {a.team} team destroys enemy crystal! VICTORY!",
                        f"💥 {a.team} team destroys enemy crystal! VICTORY!",
                    )


def rng_random_int(a: int, b: int) -> int:
    """Module-level helper for crystal damage (avoids `rng` shadowing issues)."""
    return random.randint(a, b)


# ---------- i18n ----------

def arena_msg(key: str, lang: str) -> str:
    """Bilingual strings for arena API responses."""
    msgs = {
        "queue_joined_zh": "已加入 5v5 匹配队列 ({0}/10)",
        "queue_joined_en": "Joined 5v5 queue ({0}/10)",
        "match_started_zh": "匹配成功! 比赛 ID: {0}",
        "match_started_en": "Match found! Match ID: {0}",
        "wait_zh": "队列中等待 ({0}/10), 请稍候...",
        "wait_en": "Waiting in queue ({0}/10), please hold...",
        "not_found_zh": "找不到这场比赛",
        "not_found_en": "Match not found",
    }
    return msgs.get(f"{key}_{lang}", msgs.get(f"{key}_zh", key))