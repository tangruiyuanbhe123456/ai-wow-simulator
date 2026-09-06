"""V14 Death Dispatcher.

Background loop that polls death_outbox and calls lifecycle.on_death()
for each pending row. Trigger-driven, so any code path that drops HP to 0
will be caught without changes to combat code.
"""
from __future__ import annotations
import time
import logging

from server.lifecycle import on_death

log = logging.getLogger("v14-death")

POLL_INTERVAL_SEC = 2.0
BATCH_SIZE = 50


def process_pending(conn, limit: int = BATCH_SIZE) -> int:
    """Process up to `limit` pending death outbox rows. Returns count processed."""
    cur = conn.execute(
        """SELECT id, pid, zone, pos_x, pos_y, last_damage_by,
                  last_damage_by_name, hp_before, created_at
           FROM death_outbox
           WHERE process_status='pending'
           ORDER BY created_at ASC
           LIMIT ?""",
        (limit,),
    )
    rows = cur.fetchall()
    processed = 0
    for r in rows:
        outbox_id, pid, zone, pos_x, pos_y, killer_pid, killer_name, hp_before, created_at = r
        try:
            # Only process if the bot is still alive (idempotency)
            cur2 = conn.execute(
                "SELECT state FROM bot_lifecycle WHERE pid=?", (pid,),
            )
            lc = cur2.fetchone()
            if lc and lc[0] == "sleeping":
                conn.execute(
                    """UPDATE death_outbox
                       SET process_status='skipped',
                           processed_at=?,
                           process_error='already_sleeping'
                       WHERE id=?""",
                    (time.time(), outbox_id),
                )
                conn.commit()
                continue

            result = on_death(
                conn, pid, zone or "unknown", pos_x or 0, pos_y or 0,
                killed_by_pid=killer_pid, killer_name=killer_name,
            )
            death_no = result.get("death_count") if "death_count" in result else None
            status = "processed" if "error" not in result else "failed"
            err = result.get("error") if "error" in result else None
            conn.execute(
                """UPDATE death_outbox
                   SET process_status=?, processed_at=?, process_error=?,
                       lifecycle_death_no=?
                   WHERE id=?""",
                (status, time.time(), err, death_no, outbox_id),
            )
            conn.commit()
            processed += 1
            log.info("death outbox %s: pid=%s status=%s", outbox_id, pid, status)
        except Exception as e:
            conn.execute(
                """UPDATE death_outbox
                   SET process_status='failed', processed_at=?, process_error=?
                   WHERE id=?""",
                (time.time(), str(e), outbox_id),
            )
            conn.commit()
            log.exception("death outbox %s failed: %s", outbox_id, e)
    return processed


def set_last_damage(conn, victim_pid: str, attacker_pid: str,
                    attacker_name: str) -> None:
    """Hint the next death's killer (best-effort).

    Combat code calls this whenever damage is applied; the trigger will
    only consume the most recent hint if the victim dies before the next
    damage event. Good enough for the 'death_count #N by killer' line.
    """
    cur = conn.execute(
        """SELECT id FROM death_outbox
           WHERE pid=? AND process_status='pending'
           ORDER BY created_at DESC LIMIT 1""",
        (victim_pid,),
    )
    row = cur.fetchone()
    if row:
        conn.execute(
            """UPDATE death_outbox
               SET last_damage_by=?, last_damage_by_name=?
               WHERE id=?""",
            (attacker_pid, attacker_name, row[0]),
        )
        conn.commit()
