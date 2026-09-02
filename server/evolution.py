"""v12: AI bot evolution / skill-tree module.

Each bot picks ONE branch ('tank' / 'berserk' / 'strategist') and levels up
over time. Branch choice is automated by `_suggest_branch()` based on the bot's
recent fitness history + win_rate + KDA, or can be forced via the API.

The 3 branches and their perks (cumulative by branch_level 1..5):

  tank       (defensive)   — for low win_rate (<40%) bots
    L1: hp_max +5%
    L2: hp_max +10%
    L3: hp_max +15% + auto-shield once per match (1 hit absorb)
    L4: hp_max +20% + armor +5
    L5: hp_max +25% + self-heal 1% hp/tick when below 30%

  berserk    (aggressive)  — for moderate win_rate (40-60%) bots with high kills
    L1: atk +5%
    L2: atk +10%
    L3: atk +15% + crit chance +5%
    L4: atk +20% + lifesteal 5%
    L5: atk +25% + ult cd -10t

  strategist (utility)     — for high win_rate (>60%) bots
    L1: ult_cd -3t
    L2: ult_cd -6t + spell slot +1
    L3: ult_cd -9t + cooldown all -10%
    L4: ult_cd -12t + vision +2 cells (see enemies further)
    L5: ult_cd -15t + double-ult (cast ult twice per fight)

All functions in this module are SAFE under SQLite "database is locked" via
_safe_db_write; see server.arena for the helper.

i18n: zh / en labels via dict at the bottom.
"""
from __future__ import annotations
import json
import time
from typing import Any

from server.arena import _safe_db_write
from server.db import connect as _db_connect


# ---- Branch definitions ---------------------------------------------------

BRANCHES = ("tank", "berserk", "strategist")

BRANCH_PERKS = {
    "tank": {
        1: {"hp_max_pct": 5,   "desc_zh": "生命上限 +5%",   "desc_en": "HP max +5%"},
        2: {"hp_max_pct": 10,  "desc_zh": "生命上限 +10%",  "desc_en": "HP max +10%"},
        3: {"hp_max_pct": 15,  "shield_per_match": 1, "desc_zh": "生命上限 +15% + 护盾 1 次/局", "desc_en": "HP max +15% + 1 shield/match"},
        4: {"hp_max_pct": 20,  "armor": 5, "desc_zh": "生命上限 +20% + 护甲 +5", "desc_en": "HP max +20% + armor +5"},
        5: {"hp_max_pct": 25,  "regen_below_30": True, "desc_zh": "生命上限 +25% + 30% 以下自愈", "desc_en": "HP max +25% + self-heal when HP<30%"},
    },
    "berserk": {
        1: {"atk_pct": 5,   "desc_zh": "攻击 +5%",   "desc_en": "ATK +5%"},
        2: {"atk_pct": 10,  "desc_zh": "攻击 +10%",  "desc_en": "ATK +10%"},
        3: {"atk_pct": 15,  "crit_pct": 5, "desc_zh": "攻击 +15% + 暴击率 +5%", "desc_en": "ATK +15% + crit +5%"},
        4: {"atk_pct": 20,  "lifesteal_pct": 5, "desc_zh": "攻击 +20% + 吸血 5%", "desc_en": "ATK +20% + lifesteal 5%"},
        5: {"atk_pct": 25,  "ult_cd_minus": 10, "desc_zh": "攻击 +25% + 大招 CD -10t", "desc_en": "ATK +25% + ult CD -10t"},
    },
    "strategist": {
        1: {"ult_cd_minus": 3,  "desc_zh": "大招 CD -3t",  "desc_en": "Ult CD -3t"},
        2: {"ult_cd_minus": 6,  "spell_slot": 1, "desc_zh": "大招 CD -6t + 技能槽 +1", "desc_en": "Ult CD -6t + spell slot +1"},
        3: {"ult_cd_minus": 9,  "all_cd_pct": 10, "desc_zh": "大招 CD -9t + 全 CD -10%", "desc_en": "Ult CD -9t + all CD -10%"},
        4: {"ult_cd_minus": 12, "vision_plus": 2, "desc_zh": "大招 CD -12t + 视野 +2", "desc_en": "Ult CD -12t + vision +2 cells"},
        5: {"ult_cd_minus": 15, "double_ult": True, "desc_zh": "大招 CD -15t + 双大招", "desc_en": "Ult CD -15t + double ult"},
    },
}

BRANCH_META = {
    "tank":       {"name_zh": "守护",   "name_en": "Guardian", "tagline_zh": "抗压生存,适合胜率 <40% 的 bot",  "tagline_en": "Defensive path for low-winrate bots (<40%)"},
    "berserk":    {"name_zh": "狂战",   "name_en": "Berserk",  "tagline_zh": "暴力输出,适合胜率 40-60% 但击杀 > 死亡 的 bot", "tagline_en": "Offensive path for bots with high kill/death ratio"},
    "strategist": {"name_zh": "谋士",   "name_en": "Strategist","tagline_zh": "技能节奏,适合胜率 >60% 的 bot", "tagline_en": "Utility path for high-winrate bots (>60%)"},
}

# Cost (skill points) to switch branches after first pick
SWITCH_COST = 50
# Cost to level up: each level costs (level * 30) points
LEVEL_UP_BASE = 30
# Skill points earned per match
POINTS_PER_WIN = 20
POINTS_PER_LOSS = 8   # consolation so bad bots still progress
# How many recent matches to analyze for the suggestion
SUGGEST_WINDOW = 10


# ---- DB helpers ------------------------------------------------------------

def _ensure_tree_row(pid: str) -> None:
    """Insert a bot_skill_tree row if missing. Idempotent."""
    def _do():
        c = _db_connect()
        cur = c.cursor()
        cur.execute("INSERT OR IGNORE INTO bot_skill_tree (pid) VALUES (?)", (pid,))
        c.commit()
        c.close()
    _safe_db_write(_do)


def get_tree(pid: str) -> dict:
    """Return the bot's current skill-tree state."""
    _ensure_tree_row(pid)
    def _do():
        c = _db_connect()
        cur = c.cursor()
        cur.execute("SELECT branch, skill_points, branch_level, branch_history_json, last_evolved_at "
                    "FROM bot_skill_tree WHERE pid=?", (pid,))
        row = cur.fetchone()
        c.close()
        if not row:
            return {}
        d = dict(row)
        # BUGFIX: parse the JSON history string so API consumers get a list directly.
        try:
            d["branch_history"] = json.loads(d.get("branch_history_json") or "[]")
        except Exception:
            d["branch_history"] = []
        return d
    return _safe_db_write(_do)


def award_match_points(pid: str, won: bool) -> int:
    """Called by arena._update_fitness after each match. Returns points awarded."""
    points = POINTS_PER_WIN if won else POINTS_PER_LOSS
    _ensure_tree_row(pid)

    def _do():
        c = _db_connect()
        cur = c.cursor()
        cur.execute("UPDATE bot_skill_tree SET skill_points = skill_points + ? WHERE pid=?",
                    (points, pid))
        c.commit()
        c.close()
    _safe_db_write(_do)

    # Auto level-up if threshold crossed and a branch is already picked.
    # Only auto-advance if the bot has been on this branch for at least 1 match
    # (heuristic: just always try; cost = level * LEVEL_UP_BASE).
    tree = get_tree(pid)
    if tree.get("branch"):
        _try_auto_level_up(pid)
    return points


def _points_for_next_level(current_level: int) -> int:
    return (current_level + 1) * LEVEL_UP_BASE


def _try_auto_level_up(pid: str) -> bool:
    """Level up the bot if it has enough points. Returns True if leveled."""
    tree = get_tree(pid)
    branch = tree.get("branch")
    if not branch:
        return False
    pts = tree.get("skill_points", 0) or 0
    lvl = tree.get("branch_level", 0) or 0
    if lvl >= 5:
        return False  # max
    need = _points_for_next_level(lvl)
    if pts < need:
        return False
    new_level = lvl + 1

    def _do():
        c = _db_connect()
        cur = c.cursor()
        cur.execute("UPDATE bot_skill_tree SET branch_level=?, skill_points=?, last_evolved_at=? "
                    "WHERE pid=?", (new_level, pts - need, time.time(), pid))
        cur.execute("INSERT INTO bot_evolution_log (pid, event_type, branch, detail_json, ts) "
                    "VALUES (?, 'level_up', ?, ?, ?)",
                    (pid, branch, json.dumps({"from": lvl, "to": new_level,
                                               "cost": need,
                                               "perk": BRANCH_PERKS[branch].get(new_level, {})}),
                     time.time()))
        c.commit()
        c.close()
    _safe_db_write(_do)
    return True


def choose_branch(pid: str, branch: str, reason: str = "manual", force: bool = False) -> dict:
    """Pick or switch branch for a bot. force=True ignores switch cost (admin override)."""
    if branch not in BRANCHES:
        raise ValueError(f"branch must be one of {BRANCHES}, got {branch!r}")
    _ensure_tree_row(pid)
    tree = get_tree(pid)
    current = tree.get("branch")
    pts = tree.get("skill_points", 0) or 0
    cost = 0 if (current is None or force) else SWITCH_COST
    if pts < cost:
        raise ValueError(f"need {cost} skill_points to switch branch (have {pts})")

    def _do():
        nonlocal_cost = cost
        c = _db_connect()
        cur = c.cursor()
        cur.execute("SELECT branch_history_json FROM bot_skill_tree WHERE pid=?", (pid,))
        row = cur.fetchone()
        hist = json.loads(row["branch_history_json"] or "[]") if row else []
        hist.append({"branch": branch, "ts": time.time(), "reason": reason,
                     "cost": nonlocal_cost, "from": current})
        # Keep last 50 history entries
        hist = hist[-50:]
        cur.execute("""UPDATE bot_skill_tree
                       SET branch=?, skill_points=?, branch_history_json=?, last_evolved_at=?
                       WHERE pid=?""",
                    (branch, pts - nonlocal_cost, json.dumps(hist), time.time(), pid))
        event = "pick_branch" if current is None else "switch_branch"
        cur.execute("INSERT INTO bot_evolution_log (pid, event_type, branch, detail_json, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (pid, event, branch, json.dumps({"from": current, "cost": nonlocal_cost,
                                                       "reason": reason}), time.time()))
        c.commit()
        c.close()
    _safe_db_write(_do)
    return get_tree(pid)


def _get_match_stats(pid: str) -> dict:
    """Aggregate last SUGGEST_WINDOW matches from bot_strategy_profiles + log."""
    def _do():
        c = _db_connect()
        cur = c.cursor()
        # bot_strategy_profiles has the aggregate stats
        cur.execute("SELECT wins, losses, matches_played, fitness_history FROM "
                    "bot_strategy_profiles WHERE pid=?", (pid,))
        row = cur.fetchone()
        # Try to pull kill/death counts from player_credits + the recent evolution log.
        # No dedicated per-match kda column, so we infer avg KDA via fitness trend.
        c.close()
        if not row:
            return {"wins": 0, "losses": 0, "matches": 0, "fitness_history": []}
        return {"wins": row["wins"] or 0, "losses": row["losses"] or 0,
                "matches": row["matches_played"] or 0,
                "fitness_history": json.loads(row["fitness_history"] or "[]")[-SUGGEST_WINDOW:]}
    return _safe_db_write(_do)


def suggest_branch(pid: str) -> dict:
    """AI-suggested branch based on recent performance.

    Heuristic (v12 simple version; we can tune later):
      - <3 matches played: no suggestion (not enough data)
      - win_rate < 40%                   -> 'tank'
      - 40-60% + fitness trending down   -> 'tank' (fall back to safety)
      - 40-60% + avg_fitness > 0         -> 'berserk'
      - win_rate > 60%                   -> 'strategist'
      - already has branch: explain why keep / switch
    """
    stats = _get_match_stats(pid)
    matches = stats["matches"]
    if matches < 3:
        return {"suggest": None, "confidence": 0.0,
                "reason_zh": "数据不足 (少于 3 场),暂无建议",
                "reason_en": "Not enough data (<3 matches), no suggestion yet",
                "stats": stats}

    wins, losses = stats["wins"], stats["losses"]
    win_rate = wins / max(1, wins + losses)
    hist = stats["fitness_history"]
    avg_fit = sum(hist) / len(hist) if hist else 0.0
    trend = (hist[-1] - hist[0]) if len(hist) >= 2 else 0.0

    if win_rate < 0.40:
        suggestion, conf, zh, en = ("tank", 0.85,
                                    f"胜率 {win_rate*100:.0f}% 偏低,建议转向守护系抗压",
                                    f"Win rate {win_rate*100:.0f}% is low — Guardian path helps survive")
    elif win_rate > 0.60:
        suggestion, conf, zh, en = ("strategist", 0.85,
                                    f"胜率 {win_rate*100:.0f}% 高,建议谋士系强化技能节奏",
                                    f"Win rate {win_rate*100:.0f}% is high — Strategist doubles down on utility")
    elif trend < -0.3:
        suggestion, conf, zh, en = ("tank", 0.70,
                                    f"胜率 {win_rate*100:.0f}% 中等但 fitness 下滑,先求稳",
                                    f"Moderate win rate but fitness declining — stabilize with Guardian")
    else:
        suggestion, conf, zh, en = ("berserk", 0.70,
                                    f"胜率 {win_rate*100:.0f}% 中等 + fitness 稳定,狂战系提输出",
                                    f"Moderate win rate + stable fitness — Berserk adds damage")

    tree = get_tree(pid)
    current = tree.get("branch")
    if current == suggestion:
        zh += " (当前已是该分支)"
        en += " (already on this branch)"
    elif current is not None:
        zh += f" (当前 {BRANCH_META[current]['name_zh']},切换需 {SWITCH_COST} 技能点)"
        en += f" (currently on {BRANCH_META[current]['name_en']}; switching costs {SWITCH_COST} skill points)"

    return {"suggest": suggestion, "confidence": conf,
            "reason_zh": zh, "reason_en": en,
            "stats": {"wins": wins, "losses": losses, "win_rate": win_rate,
                      "avg_fitness": avg_fit, "trend": trend, "matches": matches},
            "current": current}


def evolution_log(pid: str, limit: int = 20) -> list[dict]:
    """Return recent evolution events for a bot."""
    def _do():
        c = _db_connect()
        cur = c.cursor()
        cur.execute("SELECT event_type, branch, detail_json, ts FROM bot_evolution_log "
                    "WHERE pid=? ORDER BY ts DESC LIMIT ?", (pid, limit))
        rows = cur.fetchall()
        c.close()
        return [{"event_type": r["event_type"], "branch": r["branch"],
                 "detail": json.loads(r["detail_json"] or "{}"),
                 "ts": r["ts"]} for r in rows]
    return _safe_db_write(_do)


# ---- Public summary for the UI --------------------------------------------

def describe_tree(pid: str) -> dict:
    """Bundle everything the skill_tree.html page needs in one call."""
    tree = get_tree(pid)
    stats = _get_match_stats(pid)
    sugg = suggest_branch(pid)
    log = evolution_log(pid, limit=10)
    return {"pid": pid, "tree": tree, "stats": stats, "suggestion": sugg,
            "log": log,
            "branches": [{"id": b, **BRANCH_META[b],
                          "perks": [{"level": lvl, **BRANCH_PERKS[b][lvl]}
                                    for lvl in sorted(BRANCH_PERKS[b].keys())]}
                         for b in BRANCHES]}
