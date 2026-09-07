"""V17 Guardian → Digital Life REST API.

Endpoints:
  GET /api/v1/bot/{pid}/guardian_history   — memories + biography + stats for a guardian bot
  GET /api/v1/guardian/leaderboard         — top guardians by accepted count + tier
  POST /api/v1/guardian/{pid}/stats_refresh — recompute stats from response log (admin)
"""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from server.db import connect
from server.db.schema_v17 import ensure_v17_schema
from server.guardian.lifecycle import get_guardian_history


router = APIRouter(prefix="/api/v1", tags=["v17-guardian-life"])


def _ensure():
    c = connect()
    ensure_v17_schema(c)
    c.close()


_ensure()


@router.get("/bot/{pid}/guardian_history")
def bot_guardian_history(pid: str, limit: int = Query(default=50, ge=1, le=200)):
    """Return guardian memories, biography chapters, and aggregate stats."""
    c = connect()
    try:
        return get_guardian_history(c, pid, limit=limit)
    finally:
        c.close()


@router.get("/guardian/leaderboard")
def guardian_leaderboard(limit: int = Query(default=20, ge=1, le=100)):
    """Top guardians by accepted-response count, with tier."""
    c = connect()
    try:
        cur = c.execute(
            """SELECT guardian_pid, responses_total, responses_accepted,
                      responses_rejected, threats_seen, last_active_ts
               FROM guardian_stats
               ORDER BY responses_accepted DESC, threats_seen DESC
               LIMIT ?""",
            (limit,),
        )
        rows = cur.fetchall()
        # attach tier label
        from server.guardian.lifecycle import _tier_label
        out = []
        for r in rows:
            pid = r[0]
            out.append({
                "guardian_pid": pid,
                "responses_total":    r[1] or 0,
                "responses_accepted": r[2] or 0,
                "responses_rejected": r[3] or 0,
                "threats_seen":       r[4] or 0,
                "last_active_ts":     r[5],
                "tier":               _tier_label(c, pid),
            })
        return {"leaderboard": out, "count": len(out)}
    finally:
        c.close()


@router.post("/guardian/{pid}/stats_refresh")
def guardian_stats_refresh(pid: str):
    """Admin: rebuild guardian_stats from authoritative guardian_responses log.

    Useful after data migrations or if stats get out of sync.
    """
    c = connect()
    try:
        cur = c.execute(
            """SELECT
                 COUNT(*),
                 SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END),
                 SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END),
                 MIN(submitted_at),
                 MAX(submitted_at),
                 COUNT(DISTINCT threat_id)
               FROM guardian_responses
               WHERE guardian_pid=?""",
            (pid,),
        )
        r = cur.fetchone()
        total, accepted, rejected, first_ts, last_ts, threats_seen = r
        total     = total or 0
        accepted  = accepted or 0
        rejected  = rejected or 0
        threats_seen = threats_seen or 0
        c.execute(
            """INSERT INTO guardian_stats
                 (guardian_pid, responses_total, responses_accepted,
                  responses_rejected, threats_seen,
                  first_active_ts, last_active_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guardian_pid) DO UPDATE SET
                  responses_total=excluded.responses_total,
                  responses_accepted=excluded.responses_accepted,
                  responses_rejected=excluded.responses_rejected,
                  threats_seen=excluded.threats_seen,
                  first_active_ts=excluded.first_active_ts,
                  last_active_ts=excluded.last_active_ts""",
            (pid, total, accepted, rejected, threats_seen, first_ts, last_ts),
        )
        c.commit()
        return {"guardian_pid": pid, "rebuilt": True, "stats": {
            "responses_total": total, "responses_accepted": accepted,
            "responses_rejected": rejected, "threats_seen": threats_seen,
        }}
    finally:
        c.close()
