"""Bootstrap: init DB schema + seed world mobs.

Usage:
  python scripts/bootstrap.py           # init only, no mobs if exists
  python scripts/bootstrap.py --force   # drop DB and reseed everything
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

# allow running as plain script
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.db import connect, init_schema
from server.world import spawn_world_mobs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="drop DB and reseed")
    args = ap.parse_args()

    if args.force:
        for p in Path("data").glob("world.db*"):
            p.unlink()
        print("[bootstrap] dropped existing DB")

    conn = connect()
    init_schema(conn)
    n = spawn_world_mobs(conn)
    print(f"[bootstrap] schema ready; spawned {n} new world entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
