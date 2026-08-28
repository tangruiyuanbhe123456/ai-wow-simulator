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


@dataclass
class ArenaAgent:
    pid: str          # player id (string from server)
    name: str         # display name
    cls: str          # class (warrior/mage/priest/hunter)
    team: str         # 'blue' or 'red'
    hp: int = 100
    hp_max: int = 100
    mp: int = 60
    atk: int = 14
    pos: tuple = (5, 25)   # starting x,y
    alive: bool = True
    kills: int = 0
    deaths: int = 0
    respawn_in: int = 0   # ticks until respawn


@dataclass
class Crystal:
    team: str
    hp: int = CRYSTAL_HP
    pos: tuple = field(default_factory=lambda: (1, 25))  # left or right edge


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
                "log": [self._log_view(t, m_zh, m_en, lang) for (t, m_zh, m_en) in self.log[-30:]],
            }

    def _agent_view(self, a: ArenaAgent) -> dict:
        return {
            "pid": a.pid, "name": a.name, "cls": a.cls, "team": a.team,
            "hp": a.hp, "hp_max": a.hp_max, "mp": a.mp, "atk": a.atk,
            "pos": list(a.pos), "alive": a.alive,
            "kills": a.kills, "deaths": a.deaths,
            "respawn_in": a.respawn_in,
        }

    def _log_view(self, t: int, m_zh: str, m_en: str, lang: str) -> dict:
        return {"tick": t, "msg": m_zh if lang == "zh" else m_en}

    def append_log(self, m_zh: str, m_en: str) -> None:
        with self.lock:
            self.log.append((self.tick, m_zh, m_en))
            # Keep last 200 events.
            if len(self.log) > 200:
                self.log = self.log[-200:]


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
    """If queue has ≥10 pids, pop first 10 and form a 5v5 match.

    `lookup_agent` is a callable(pid) -> ArenaAgent (caller provides
    the agent's name+class from the registered player). Returns None if
    fewer than 10 in queue.
    """
    with _lock:
        if len(_queue) < 10:
            return None
        pids = _queue[:10]
        del _queue[:10]

    blue_pids = pids[:5]
    red_pids = pids[5:10]
    blue = [lookup_agent(p, "blue") for p in blue_pids]
    red = [lookup_agent(p, "red") for p in red_pids]
    m = ArenaMatch(
        match_id=match_id,
        blue=blue,
        red=red,
        blue_crystal=Crystal(team="blue"),
        red_crystal=Crystal(team="red"),
        started_at=time.time(),
    )
    # Place agents at their team's side
    for i, a in enumerate(blue):
        a.pos = (5, 5 + i * 10)
    for i, a in enumerate(red):
        a.pos = (ARENA_W - 6, 5 + i * 10)
    with _lock:
        _active_matches[match_id] = m
    m.append_log(
        f"匹配开始! 蓝队 vs 红队 (10个 AI 集结) | Match starts! Blue vs Red (10 AIs queued)",
        f"Match starts! Blue vs Red (10 AIs queued)",
    )
    return m


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
      1. Respawn dead agents whose cooldown expired.
      2. Each alive agent attacks nearest enemy; melee swings only (no skills
         here — keeps MVP simple).
      3. If killer is in range of enemy crystal (within 3 cells of enemy base
         edge), damage crystal instead.
      4. Death events: killer gets a kill, victim dies, respawn timer set.
      5. Check crystal HP == 0 → match ends.
    """
    if m.ended:
        return
    rng = rng or random.Random()
    with m.lock:
        m.tick += 1
        tick_n = m.tick

    _respawn_step(m, tick_n)
    _combat_step(m, rng, tick_n)
    _check_crystals(m, tick_n)


def _respawn_step(m: ArenaMatch, tick_n: int) -> None:
    for a in m.blue + m.red:
        if not a.alive and a.respawn_in > 0:
            a.respawn_in -= 1
            if a.respawn_in <= 0:
                a.alive = True
                a.hp = a.hp_max
                a.mp = 60
                # respawn at home corner
                if a.team == "blue":
                    a.pos = (5, 5 + (m.blue.index(a)) * 10)
                else:
                    a.pos = (ARENA_W - 6, 5 + (m.red.index(a)) * 10)
                m.append_log(
                    f"{a.name} ({a.team}) 已复活 | {a.name} respawned",
                    f"{a.name} ({a.team}) respawned",
                )


def _combat_step(m: ArenaMatch, rng: random.Random, tick_n: int) -> None:
    """Each alive agent attacks nearest enemy (or enemy crystal in range)."""
    all_alive = [a for a in (m.blue + m.red) if a.alive]
    for a in all_alive:
        enemies = [e for e in all_alive if e.team != a.team]
        if not enemies:
            continue
        # Pick nearest enemy by Manhattan distance
        target = min(enemies, key=lambda e: abs(e.pos[0] - a.pos[0]) + abs(e.pos[1] - a.pos[1]))
        dist = abs(target.pos[0] - a.pos[0]) + abs(target.pos[1] - a.pos[1])
        # If far, step toward enemy (1 cell/tick toward them)
        if dist > 1:
            dx = (1 if target.pos[0] > a.pos[0] else (-1 if target.pos[0] < a.pos[0] else 0))
            dy = (1 if target.pos[1] > a.pos[1] else (-1 if target.pos[1] < a.pos[1] else 0))
            new_pos = (a.pos[0] + dx, a.pos[1] + dy)
            # bound to arena
            a.pos = (max(1, min(ARENA_W - 2, new_pos[0])),
                     max(1, min(ARENA_H - 2, new_pos[1])))
            continue  # movement only, no attack this tick
        # Melee range: hit target
        dmg = max(1, a.atk + rng.randint(0, 3))
        crit = rng.random() < 0.15
        if crit:
            dmg = int(dmg * 2)
        target.hp -= dmg
        if target.hp <= 0:
            target.alive = False
            target.hp = 0
            target.deaths += 1
            target.respawn_in = RESPAWN_TICKS
            a.kills += 1
            m.team_kills[a.team] += 1
            m.append_log(
                f"{a.name} ({a.team}) 击杀 {target.name} ({target.team}) 暴击={crit} 伤害={dmg} | {a.name} ({a.team}) killed {target.name} ({target.team}) crit={crit} dmg={dmg}",
                f"{a.name} ({a.team}) killed {target.name} ({target.team}) crit={crit} dmg={dmg}",
            )
        else:
            # small log for hit (every other tick to keep log readable)
            if tick_n % 2 == 0:
                m.append_log(
                    f"{a.name} 对 {target.name} 造成 {dmg} 伤害 {'暴击!' if crit else ''} | {a.name} hits {target.name} for {dmg} {'CRIT!' if crit else ''}",
                    f"{a.name} hits {target.name} for {dmg} {'CRIT!' if crit else ''}",
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