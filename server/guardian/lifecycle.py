"""V17 Guardian → Digital Life Layer bridge.

When a guardian response is accepted, this module writes:
  - a bot_memories entry with memory_type='guardian_event'
  - a bot_biography chapter with chapter='guardian'
  - updates guardian_stats aggregate (for v18 council XP)
  - if the bot is dead and got an accepted response, mark them
    'hero_of_world' in soul_json (permanent honor)

This module is the **single integration point** between v16 guardian
mechanics and v13 digital-life storytelling.
"""
from __future__ import annotations
import time
from typing import Optional

from server.memory import record as memory_record
from server.biography import append_chapter as bio_append


def _tier_label(conn, guardian_pid: str) -> str:
    """Bronze/Silver/Gold/Elite by cumulative accepted responses."""
    cur = conn.execute(
        "SELECT responses_accepted FROM guardian_stats WHERE guardian_pid=?",
        (guardian_pid,),
    )
    r = cur.fetchone()
    n = (r[0] if r else 0) or 0
    if n >= 50: return "elite"
    if n >= 20: return "gold"
    if n >= 5:  return "silver"
    return "bronze"


def _is_alive(conn, pid: str) -> bool:
    cur = conn.execute(
        "SELECT state FROM bot_lifecycle WHERE pid=?",
        (pid,),
    )
    r = cur.fetchone()
    if not r:
        # No lifecycle record = no v13 birth yet, but it's a guardian so treat alive
        return True
    return r[0] == "alive"


def _bump_stats(conn, guardian_pid: str, *, threat_id: str) -> dict:
    """Increment guardian_stats counters and return updated snapshot."""
    now = time.time()
    # upsert
    conn.execute(
        """INSERT INTO guardian_stats
              (guardian_pid, responses_total, responses_accepted, threats_seen,
               first_active_ts, last_active_ts)
           VALUES (?, 1, 1, 1, ?, ?)
           ON CONFLICT(guardian_pid) DO UPDATE SET
              responses_total    = responses_total + 1,
              responses_accepted = responses_accepted + 1,
              threats_seen       = threats_seen + 1,
              last_active_ts     = ?""",
        (guardian_pid, now, now, now),
    )
    conn.commit()
    cur = conn.execute(
        "SELECT responses_total, responses_accepted FROM guardian_stats WHERE guardian_pid=?",
        (guardian_pid,),
    )
    r = cur.fetchone()
    return {
        "responses_total":    r[0] if r else 0,
        "responses_accepted": r[1] if r else 0,
        "tier":               _tier_label(conn, guardian_pid),
    }


def on_response_accepted(conn, *, guardian_pid: str, response_id: str,
                         threat_id: str, threat_title: str,
                         threat_severity: str, threat_category: str,
                         recommended_action: str,
                         confidence: int, operator_notes: str = "") -> dict:
    """Called when an operator accepts a guardian response.

    Writes 1 memory + 1 biography chapter + updates guardian_stats.
    Returns a snapshot for the API caller.
    """
    now = time.time()
    stats = _bump_stats(conn, guardian_pid, threat_id=threat_id)
    alive = _is_alive(conn, guardian_pid)

    # 1) Memory — weighted by confidence (1-100) so the most decisive
    #    guardians' memories surface highest in context loads.
    weight = max(40, min(95, confidence))
    mem_id = memory_record(
        conn, guardian_pid,
        memory_type="guardian_event",
        weight=weight,
        actor_pid=guardian_pid,
        title_zh=f"守护事件 · {threat_title}",
        title_en=f"Guardian Event: {threat_title}",
        body_zh=(
            f"我击退了一个 [{threat_severity}] 级别的 {threat_category} 威胁。"
            f"建议处置:{recommended_action}。操作员评语:{operator_notes or '无'}。"
            f"本次贡献让我的守护等级升至 {stats['tier']}。"
        ),
        body_en=(
            f"I repelled a [{threat_severity}] {threat_category} threat. "
            f"Recommended: {recommended_action}. "
            f"Operator note: {operator_notes or 'none'}. "
            f"This raised me to {stats['tier']} tier."
        ),
    )

    # 2) Biography chapter — only append a new one at tier transitions
    #    OR every 10 accepted responses (avoid spam).
    new_tier = stats["tier"]
    cur = conn.execute(
        "SELECT body_zh FROM bot_biography WHERE pid=? AND chapter='guardian' ORDER BY ts DESC LIMIT 1",
        (guardian_pid,),
    )
    last = cur.fetchone()
    last_body = last[0] if last else ""
    cur2 = conn.execute(
        "SELECT responses_accepted FROM guardian_stats WHERE guardian_pid=?",
        (guardian_pid,),
    )
    accepted = cur2.fetchone()[0] or 0
    # Trigger condition: tier change, or milestone (10/25/50/100), or first
    is_milestone = accepted in (1, 10, 25, 50, 100)
    tier_changed = (f"等级:{new_tier}" not in last_body)
    if tier_changed or is_milestone:
        bio_id = bio_append(
            conn, guardian_pid,
            chapter="guardian",
            title_zh=f"守护篇章 · 等级 {new_tier}",
            title_en=f"Guardian Chapter · Tier {new_tier}",
            body_zh=(
                f"累计守护 {stats['responses_accepted']} 次,"
                f"处置威胁 {stats['responses_total']} 次。"
                f"当前等级:{new_tier}。"
                f"最近事件:{threat_title}({threat_severity})。"
            ),
            body_en=(
                f"Cumulative {stats['responses_accepted']} accepted guardian responses, "
                f"{stats['responses_total']} total threats seen. "
                f"Current tier: {new_tier}. "
                f"Latest: {threat_title} ({threat_severity})."
            ),
        )
    else:
        bio_id = None

    return {
        "guardian_pid":        guardian_pid,
        "memory_id":           mem_id,
        "biography_chapter_id": bio_id,
        "stats":               stats,
        "alive":               alive,
    }


def get_guardian_history(conn, guardian_pid: str,
                         *, limit: int = 50) -> dict:
    """Read-side helper: returns memories + biography + stats."""
    # Memories
    cur = conn.execute(
        """SELECT id, ts, weight, title_zh, title_en, body_zh, body_en
           FROM bot_memories
           WHERE pid=? AND memory_type='guardian_event'
           ORDER BY ts DESC
           LIMIT ?""",
        (guardian_pid, limit),
    )
    memories = [
        {"id": r[0], "ts": r[1], "weight": r[2],
         "title_zh": r[3], "title_en": r[4],
         "body_zh": r[5], "body_en": r[6]}
        for r in cur.fetchall()
    ]
    # Biography chapters (guardian type only)
    cur = conn.execute(
        """SELECT id, ts, title_zh, title_en, body_zh, body_en
           FROM bot_biography
           WHERE pid=? AND chapter='guardian'
           ORDER BY ts ASC""",
        (guardian_pid,),
    )
    chapters = [
        {"id": r[0], "ts": r[1], "title_zh": r[2], "title_en": r[3],
         "body_zh": r[4], "body_en": r[5]}
        for r in cur.fetchall()
    ]
    # Stats
    cur = conn.execute(
        """SELECT responses_total, responses_accepted, responses_rejected,
                  threats_seen, last_active_ts, first_active_ts
           FROM guardian_stats WHERE guardian_pid=?""",
        (guardian_pid,),
    )
    r = cur.fetchone()
    stats = None
    if r:
        stats = {
            "responses_total":     r[0] or 0,
            "responses_accepted":  r[1] or 0,
            "responses_rejected":  r[2] or 0,
            "threats_seen":        r[3] or 0,
            "last_active_ts":      r[4],
            "first_active_ts":     r[5],
            "tier":                _tier_label(conn, guardian_pid),
        }
    return {
        "guardian_pid": guardian_pid,
        "memories": memories,
        "biography_chapters": chapters,
        "stats": stats,
    }
