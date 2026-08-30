"""Arena draft mode — Honor-of-Kings-inspired ban/pick phase.

When 10 players queue, instead of going straight to a match, they enter a
draft phase. Each side bans 1 hero and picks 5 (one per player). The picks
determine each agent's class (warrior/mage/priest/hunter or one of the
variants). If a player doesn't submit picks within DRAFT_TIMEOUT_TICKS, the
server auto-picks for them so the match can start.

Candidates (8 heroes, 2 variants per base class):
  warrior_tank, warrior_dps,
  mage_fire, mage_ice,
  priest_heal, priest_dark,
  hunter_bow, hunter_trap

Pick → player.cls mapping (server picks the closest base class when the
match starts; the variant tag is purely cosmetic in MVP).
"""
from __future__ import annotations
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any


HERO_POOL = [
    # 12 heroes — 3 per base class, so 5+5 picks always fit without dupes
    ("warrior_tank",  "战士·坦克",  "Warrior (Tank)"),
    ("warrior_dps",   "战士·狂战",  "Warrior (Berserker)"),
    ("warrior_guard", "战士·护卫",  "Warrior (Guard)"),
    ("mage_fire",     "法师·火焰",  "Mage (Fire)"),
    ("mage_ice",      "法师·冰霜",  "Mage (Ice)"),
    ("mage_arcane",   "法师·奥术",  "Mage (Arcane)"),
    ("priest_heal",   "牧师·治疗",  "Priest (Healer)"),
    ("priest_dark",   "牧师·暗影",  "Priest (Shadow)"),
    ("priest_holy",   "牧师·圣光",  "Priest (Holy)"),
    ("hunter_bow",    "猎人·弓",    "Hunter (Bow)"),
    ("hunter_trap",   "猎人·陷阱",  "Hunter (Trap)"),
    ("hunter_pet",    "猎人·宠物",  "Hunter (Pet)"),
]

# Map variant → base class (for existing /api/v1/action etc which expects base)
HERO_TO_BASE_CLASS = {
    "warrior_tank":  "warrior",
    "warrior_dps":   "warrior",
    "warrior_guard": "warrior",
    "mage_fire":     "mage",
    "mage_ice":      "mage",
    "mage_arcane":   "mage",
    "priest_heal":   "priest",
    "priest_dark":   "priest",
    "priest_holy":   "priest",
    "hunter_bow":    "hunter",
    "hunter_trap":   "hunter",
    "hunter_pet":    "hunter",
}

DRAFT_TIMEOUT_TICKS = 60  # 1 minute at 1s tick
PICKS_PER_TEAM = 5
BANS_PER_TEAM = 1

# Summoner spells — Honor-of-Kings-equivalent. Each player picks 1 (chosen
# during draft phase). Effects trigger during the match; tracked in
# ArenaAgent.spell. Cooldown-locked (1 use per match unless noted).
SPELL_POOL = [
    ("flash",      "闪现",     "Flash",       "blink_to_base_or_ally", "Teleport up to 8 cells (works only for self/ally)."),
    ("heal",       "治疗",     "Heal",        "heal_self",             "Restore 40% HP to self."),
    ("ignite",     "点燃",     "Ignite",      "burn_target",           "Deal 80 dmg over 5 ticks to nearest enemy."),
    ("exhaust",    "虚弱",     "Exhaust",     "weaken_target",         "Reduce nearest enemy's atk by 50% for 10 ticks."),
    ("ghost",      "幽灵疾步", "Ghost",       "speed_boost",           "+30% move speed for 15 ticks."),
    ("cleanse",    "净化",     "Cleanse",     "cleanse_debuffs",       "Remove all debuffs and cc from self."),
    ("barrier",    "屏障",     "Barrier",     "shield_self",           "Absorb 60 dmg for 8 ticks."),
    ("smite",      "晕跳",     "Smite",       "execute_low_hp",        "Instant-kill any enemy under 15% HP (within 6 cells)."),
]


@dataclass
class Draft:
    """One ban/pick session for 10 players (5 blue + 5 red)."""
    draft_id: str
    blue_pids: list = field(default_factory=list)
    red_pids: list = field(default_factory=list)
    # Banned heroes (hero_id strings)
    bans: dict = field(default_factory=lambda: {"blue": None, "red": None})
    # Picked heroes (in order of submission, hero_id strings)
    picks: dict = field(default_factory=lambda: {"blue": [], "red": []})
    # Which player pid chose which hero + which spell
    assignments: dict = field(default_factory=dict)   # pid -> hero_id
    spells: dict = field(default_factory=dict)        # pid -> spell_id
    started_at: float = 0.0
    tick: int = 0
    ended: bool = False
    picks_made: int = 0
    log: list = field(default_factory=list)  # list[(tick, msg_zh, msg_en)]
    lock: threading.Lock = field(default_factory=threading.Lock)

    def to_dict(self, lang: str = "zh") -> dict:
        with self.lock:
            return {
                "ok": True,
                "draft_id": self.draft_id,
                "lang": lang,
                "tick": self.tick,
                "started_at": self.started_at,
                "ended": self.ended,
                "picks_made": self.picks_made,
                "picks_required": PICKS_PER_TEAM * 2,  # 10 players
                "blue": {
                    "pids": list(self.blue_pids),
                    "ban": self.bans.get("blue"),
                    "picks": list(self.picks["blue"]),
                    "remaining": list(set(self.blue_pids) - {p for p, h in self.assignments.items() if h and HERO_TO_BASE_CLASS.get(h) in ("warrior", "mage", "priest", "hunter") and p in self.blue_pids}),
                },
                "red": {
                    "pids": list(self.red_pids),
                    "ban": self.bans.get("red"),
                    "picks": list(self.picks["red"]),
                },
"assignments": dict(self.assignments),
                "spells": dict(self.spells),
                "remaining_ticks": max(0, DRAFT_TIMEOUT_TICKS - self.tick),
                "log": [
                    {"tick": t, "msg": m_zh if lang == "zh" else m_en}
                    for (t, m_zh, m_en) in self.log[-20:]
                ],
            }

    def append_log(self, msg_zh: str, msg_en: str) -> None:
        with self.lock:
            self.log.append((self.tick, msg_zh, msg_en))
            if len(self.log) > 100:
                self.log = self.log[-100:]


# Module-level state
_drafts: dict[str, Draft] = {}
_lock = threading.Lock()


def get_draft(draft_id: str) -> Draft | None:
    with _lock:
        return _drafts.get(draft_id)


def all_drafts() -> list[Draft]:
    with _lock:
        return list(_drafts.values())


def create_draft(blue_pids: list, red_pids: list) -> Draft:
    """Create a new draft for the given 10 players (5 blue + 5 red)."""
    draft_id = "drft_" + secrets.token_hex(4)
    d = Draft(
        draft_id=draft_id,
        blue_pids=list(blue_pids),
        red_pids=list(red_pids),
        started_at=time.time(),
    )
    d.append_log(
        f"选秀开始! 蓝队 {len(blue_pids)} 人 vs 红队 {len(red_pids)} 人 | Draft started! Blue {len(blue_pids)} vs Red {len(red_pids)}",
        f"Draft started! Blue {len(blue_pids)} vs Red {len(red_pids)}",
    )
    with _lock:
        _drafts[draft_id] = d
    return d


def remove_draft(draft_id: str) -> None:
    with _lock:
        _drafts.pop(draft_id, None)


def submit_ban(draft_id: str, team: str, hero_id: str, lang: str = "zh") -> dict:
    """A team bans one hero (must not be already banned by the other team)."""
    d = get_draft(draft_id)
    if d is None or d.ended:
        return {"ok": False, "error": "draft not found or ended"}
    if team not in ("blue", "red"):
        return {"ok": False, "error": "team must be blue or red"}
    if d.bans[team] is not None:
        return {"ok": False, "error": f"team {team} already banned {d.bans[team]}"}
    valid_ids = [h[0] for h in HERO_POOL]
    if hero_id not in valid_ids:
        return {"ok": False, "error": f"unknown hero_id; choose from {valid_ids}"}
    other_team = "red" if team == "blue" else "blue"
    if d.bans.get(other_team) == hero_id:
        return {"ok": False, "error": "hero already banned by other team"}
    d.bans[team] = hero_id
    hero_zh = next(h[1] for h in HERO_POOL if h[0] == hero_id)
    hero_en = next(h[2] for h in HERO_POOL if h[0] == hero_id)
    d.append_log(
        f"{team}队 禁用 {hero_zh} ({hero_id}) | {team} team bans {hero_en} ({hero_id})",
        f"{team} team bans {hero_en} ({hero_id})",
    )
    return {"ok": True, "draft_id": draft_id, "team": team, "ban": hero_id}


def submit_spell(draft_id: str, pid: str, spell_id: str, lang: str = "zh") -> dict:
    """A player picks their summoner spell (one per player, per match)."""
    d = get_draft(draft_id)
    if d is None or d.ended:
        return {"ok": False, "error": "draft not found or ended"}
    if pid not in d.blue_pids + d.red_pids:
        return {"ok": False, "error": "player not in this draft"}
    valid_ids = [s[0] for s in SPELL_POOL]
    if spell_id not in valid_ids:
        return {"ok": False, "error": f"unknown spell_id; choose from {valid_ids}"}
    if pid in d.spells:
        return {"ok": False, "error": "you already picked a spell"}
    d.spells[pid] = spell_id
    spell_zh = next(s[1] for s in SPELL_POOL if s[0] == spell_id)
    spell_en = next(s[2] for s in SPELL_POOL if s[0] == spell_id)
    d.append_log(
        f"{pid} 选择召唤师技能 [{spell_zh}] ({spell_id}) | {pid} picked summoner [{spell_en}] ({spell_id})",
        f"{pid} picked summoner [{spell_en}] ({spell_id})",
    )
    return {"ok": True, "draft_id": draft_id, "pid": pid, "spell": spell_id}


def submit_pick(draft_id: str, pid: str, hero_id: str, lang: str = "zh") -> dict:
    """A player picks a hero for themselves.

    Must be the player's own turn (team picks_per_team order); must not pick
    a banned hero or one already chosen by another player; must not duplicate
    their own pick.
    """
    d = get_draft(draft_id)
    if d is None or d.ended:
        return {"ok": False, "error": "draft not found or ended"}
    if pid not in d.blue_pids + d.red_pids:
        return {"ok": False, "error": "player not in this draft"}
    team = "blue" if pid in d.blue_pids else "red"
    if pid in d.assignments:
        return {"ok": False, "error": "you already picked"}
    valid_ids = [h[0] for h in HERO_POOL]
    if hero_id not in valid_ids:
        return {"ok": False, "error": f"unknown hero_id; choose from {valid_ids}"}
    if hero_id in (d.bans["blue"], d.bans["red"]):
        return {"ok": False, "error": "hero is banned"}
    if hero_id in d.picks["blue"] or hero_id in d.picks["red"]:
        return {"ok": False, "error": "hero already picked by another player"}
    if len(d.picks[team]) >= PICKS_PER_TEAM:
        return {"ok": False, "error": f"team {team} has full roster ({PICKS_PER_TEAM} picks)"}

    d.assignments[pid] = hero_id
    d.picks[team].append(hero_id)
    d.picks_made += 1
    hero_zh = next(h[1] for h in HERO_POOL if h[0] == hero_id)
    hero_en = next(h[2] for h in HERO_POOL if h[0] == hero_id)
    d.append_log(
        f"{pid} ({team}) 选择 {hero_zh} ({hero_id}) [{d.picks_made}/{PICKS_PER_TEAM*2}] | {pid} ({team}) picked {hero_en} ({hero_id}) [{d.picks_made}/{PICKS_PER_TEAM*2}]",
        f"{pid} ({team}) picked {hero_en} ({hero_id}) [{d.picks_made}/{PICKS_PER_TEAM*2}]",
    )
    if d.picks_made >= PICKS_PER_TEAM * 2:
        d.ended = True
        d.append_log(
            f"选秀结束! 双方已选满 {PICKS_PER_TEAM} 英雄 | Draft complete! Both teams locked in",
            f"Draft complete! Both teams locked in",
        )
    return {"ok": True, "draft_id": draft_id, "pid": pid, "hero": hero_id, "picks_made": d.picks_made}


def auto_fill_remaining(d: Draft) -> None:
    """Auto-pick random allowed heroes for any unpicked players (timeout fallback).
    Also auto-assigns a default summoner spell (heal) for any player who
    didn't pick one."""
    if d.ended:
        return
    banned = set(filter(None, (d.bans["blue"], d.bans["red"])))
    chosen = set(d.picks["blue"]) | set(d.picks["red"])
    available = [h[0] for h in HERO_POOL if h[0] not in banned and h[0] not in chosen]
    import random as _r
    rng = _r.Random()
    rng.shuffle(available)

    for pid in d.blue_pids + d.red_pids:
        if pid in d.assignments:
            continue
        team = "blue" if pid in d.blue_pids else "red"
        if len(d.picks[team]) >= PICKS_PER_TEAM:
            continue
        if not available:
            break
        hero_id = available.pop(0)
        d.assignments[pid] = hero_id
        d.picks[team].append(hero_id)
        d.picks_made += 1
        hero_zh = next(h[1] for h in HERO_POOL if h[0] == hero_id)
        hero_en = next(h[2] for h in HERO_POOL if h[0] == hero_id)
        d.append_log(
            f"⏰ {pid} ({team}) 超时自动选 {hero_zh} | ⏰ {pid} ({team}) auto-picked {hero_en} (timeout)",
            f"⏰ {pid} ({team}) auto-picked {hero_en} (timeout)",
        )

    # Default spell = heal for players who didn't pick
    for pid in d.blue_pids + d.red_pids:
        if pid not in d.spells:
            d.spells[pid] = "heal"

    if d.picks_made >= PICKS_PER_TEAM * 2:
        d.ended = True


def tick_draft(d: Draft) -> None:
    """Advance the draft by one tick (called by background thread)."""
    if d.ended:
        return
    with d.lock:
        d.tick += 1
    # Auto-fill if past timeout
    if d.tick > DRAFT_TIMEOUT_TICKS:
        auto_fill_remaining(d)
        if d.picks_made >= PICKS_PER_TEAM * 2:
            d.ended = True


def arena_msg(key: str, lang: str) -> str:
    msgs = {
        "draft_started_zh": "选秀开始 (draft_id={0}), 每队 ban 1 选 5",
        "draft_started_en": "Draft started (draft_id={0}), each team bans 1, picks 5",
        "draft_wait_zh": "选秀中 ({0}/{1} picks), 等待 {2} 选英雄",
        "draft_wait_en": "Draft in progress ({0}/{1} picks), waiting for {2} to pick",
        "draft_done_zh": "选秀完成, 比赛开始!",
        "draft_done_en": "Draft complete, match starting!",
        "draft_not_found_zh": "选秀未找到",
        "draft_not_found_en": "Draft not found",
    }
    return msgs.get(f"{key}_{lang}", msgs.get(f"{key}_zh", key))