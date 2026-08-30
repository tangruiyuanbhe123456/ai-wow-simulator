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
    "weapon": [
        ("rusty_blade",     200,  {"atk": 3}),
        ("iron_sword",      500,  {"atk": 7}),
        ("dragon_slayer",   1200, {"atk": 15}),
    ],
    "helm": [
        ("cloth_cap",       150,  {"hp_max": 10}),
        ("iron_helm",       400,  {"hp_max": 25}),
        ("dragon_helm",     900,  {"hp_max": 60}),
    ],
    "chest": [
        ("leather_vest",    150,  {"hp_max": 15}),
        ("iron_plate",      450,  {"hp_max": 40}),
        ("dragon_scale",    1000, {"hp_max": 80}),
],
    "boots": [
        ("cloth_boots",     100,  {"speed": 1}),   # speed not modeled; +atk as proxy
        ("swift_boots",     300,  {"speed": 2, "atk": 2}),
        ("dragon_talons",   700,  {"speed": 3, "atk": 4}),
    ],
    "trinket": [
        ("lucky_charm",     200,  {"atk": 2, "hp_max": 10}),
        ("hero_medal",      500,  {"atk": 5, "hp_max": 20}),
        ("dragon_eye",      1100, {"atk": 10, "hp_max": 50}),
    ],
    "skin": [
        ("basic_skin",      0,    {}),  # free / cosmetic only
        ("fancy_skin",      250,  {"atk": 1}),
        ("legendary_skin",  800,  {"atk": 3, "hp_max": 15}),
    ],
}

# Per-hero ultimates — each class gets 1 ultimate ability, 60-tick cooldown
# after use. Triggered automatically when off-cooldown (bots always use
# when they can).
ULTIMATES = {
    # class -> (ult_id, zh_name, en_name, effect_func_name)
    "warrior": ("warrior_charge",   "冲锋陷阵",  "Heroic Charge",   "Charge to nearest enemy (8 cells). Stun for 2 ticks."),
    "mage":    ("mage_meteor",      "陨石天降",  "Meteor Strike",   "Deal 80 dmg in 5-cell radius at nearest enemy position."),
    "priest":  ("priest_resurrect", "神圣复活",  "Divine Resurrection", "Revive any dead ally on the field (full HP, no respawn wait)."),
    "hunter":  ("hunter_snipe",     "致命狙击",  "Hunter's Snipe",  "Snipe lowest-HP enemy from any distance for 70 dmg + 1.5x crit."),
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
        for item_name, _cost, stats in EQUIPMENT_CATALOG.get(slot, []):
            if item_name == item_id:
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
        for item_name, cost, stats in catalog:
            if a.gold >= cost and (best is None or cost > best[1]):
                best = (item_name, cost, stats)
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
        # Auto-trigger spell (if not used yet) when conditions are right
        if not a.spell_used and a.spell and a.alive:
            sp = a.spell
            enemies = [e for e in (m.blue + m.red) if e.alive and e.team != a.team]
            if sp == "heal" and a.hp / max(1, a.hp_max) < 0.40:
                _cast_summoner_spell(a, m, tick_n)
            elif sp == "barrier" and a.hp / max(1, a.hp_max) < 0.60:
                _cast_summoner_spell(a, m, tick_n)
            elif sp == "smite" and enemies:
                for e in enemies:
                    dist = abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1])
                    if dist <= 6 and e.hp / max(1, e.hp_max) < 0.15:
                        _cast_summoner_spell(a, m, tick_n)
                        break
            elif sp == "ignite" and enemies:
                # Cast when an enemy is within 4 cells
                target = min(enemies, key=lambda e: abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]))
                if abs(target.pos[0] - a.pos[0]) + abs(target.pos[1] - a.pos[1]) <= 4:
                    _cast_summoner_spell(a, m, tick_n)
            elif sp == "exhaust" and enemies:
                target = min(enemies, key=lambda e: abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]))
                if abs(target.pos[0] - a.pos[0]) + abs(target.pos[1] - a.pos[1]) <= 3:
                    _cast_summoner_spell(a, m, tick_n)
            elif sp == "flash" and enemies:
                # Flash away when very low HP
                if a.hp / max(1, a.hp_max) < 0.20:
                    _cast_summoner_spell(a, m, tick_n)
            elif sp == "ghost" and tick_n == 5:
                # Cast at start of match for early mobility
                _cast_summoner_spell(a, m, tick_n)

        # Tick ongoing effects
        if a.ignite_ticks > 0:
            target = next((e for e in (m.blue + m.red) if e.pid == a.ignite_target_pid), None)
            if target and target.alive:
                target.hp = max(0, target.hp - 16)  # 80 / 5 = 16/tick
                a.ignite_ticks -= 1
                if target.hp == 0:
                    target.alive = False
                    target.deaths += 1
                    target.respawn_in = RESPAWN_TICKS
                    a.kills += 1
                    m.team_kills[a.team] += 1
                    a.gold += GOLD_PER_KILL
                    _try_buy_best_affordable(a, m)
            else:
                a.ignite_ticks = 0
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


def _shield_absorb(a: ArenaAgent, incoming: int) -> int:
    """Apply barrier shield to incoming damage; return the actual dmg taken."""
    if a.shield_remaining > 0 and incoming > 0:
        absorbed = min(a.shield_remaining, incoming)
        a.shield_remaining -= absorbed
        return incoming - absorbed
    return incoming




def _tick_ultimates(m: ArenaMatch, tick_n: int) -> None:
    """Decrement all ultimates' cooldowns; trigger bot use when off-CD."""
    for a in m.blue + m.red:
        if a.ult_cd > 0:
            a.ult_cd = max(0, a.ult_cd - 1)
        if a.ult_cd == 0 and a.alive:
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
    m = ArenaMatch(
        match_id=match_id,
        blue=blue,
        red=red,
        blue_crystal=Crystal(team="blue"),
        red_crystal=Crystal(team="red"),
        started_at=time.time(),
        towers=towers,
    )
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
    _patrol_dragons(m)
    _respawn_step(m, tick_n)
    _expire_buffs(m, tick_n)
    _tick_spell_effects(m, tick_n)
    _tick_ultimates(m, tick_n)
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

        # Choose closest by Manhattan distance (dragon or enemy); melee-only.
        candidates = []
        if dragon_target is not None:
            d_dist = abs(dragon_target["pos"][0] - a.pos[0]) + abs(dragon_target["pos"][1] - a.pos[1])
            candidates.append((d_dist, "dragon", dragon_target))
        if enemy_target is not None:
            e_dist = abs(enemy_target.pos[0] - a.pos[0]) + abs(enemy_target.pos[1] - a.pos[1])
            candidates.append((e_dist, "enemy", enemy_target))
        if not candidates:
            continue
        candidates.sort(key=lambda c: c[0])
        dist, kind, target = candidates[0]

        # If far, step toward chosen target (1 cell/tick)
        if dist > 1:
            if kind == "dragon":
                tx, ty = target["pos"]
            else:
                tx, ty = target.pos
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