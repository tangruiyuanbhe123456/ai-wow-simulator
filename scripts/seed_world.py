"""Seed world: re-populate mobs/bosses/gathering nodes.

Usage:
  python scripts/seed_world.py         # idempotent (no-op if mobs exist)
  python scripts/seed_world.py --force  # wipe + reseed
"""
from __future__ import annotations
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.db import connect, init_schema
from server.world import spawn_world_mobs, MOB_TEMPLATES, BOSS_TEMPLATES


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = connect()
    init_schema(conn)
    cur = conn.cursor()

    if args.force:
        cur.execute("DELETE FROM mobs")
        conn.commit()
        print("[seed] wiped mobs table")

    n = spawn_world_mobs(conn)
    cur.execute("SELECT kind, COUNT(*) FROM mobs GROUP BY kind")
    kinds = {r[0]: r[1] for r in cur.fetchall()}
    print(f"[seed] spawned {n} new entries. by kind: {kinds}")
    print(f"[seed] mob templates: {len(MOB_TEMPLATES)}, boss templates: {len(BOSS_TEMPLATES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
