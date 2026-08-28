"""Guild CLI: exercise guild create / join / kick / war / ally without HTTP.

Usage:
  python scripts/guild_cli.py smoke     # run full sequence against local DB
  python scripts/guild_cli.py create <player_id> <name> <tag>
  ...
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.db import connect, init_schema
from server.guild import (
    create_guild as g_create, join_guild as g_join,
    kick_member as g_kick, set_relation as g_set_relation,
    list_guilds, get_guild,
)


def make_player(conn, name: str) -> str:
    cur = conn.cursor()
    import uuid
    pid = "p_" + uuid.uuid4().hex[:8]
    cur.execute("""INSERT INTO players (id,name,cls,level,xp,hp,hp_max,mp,mp_max,atk,defn,
                   zone,pos_x,pos_y,gold,created_at,last_seen) VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, name, "warrior", 1, 0, 120, 120, 30, 30, 14, 2,
                 "starter_village", 0, 0, 0, time.time(), time.time()))
    conn.commit()
    return pid


def smoke() -> int:
    # fresh DB
    for p in Path("data").glob("world.db*"):
        p.unlink()
    conn = connect()
    init_schema(conn)

    # 3 players: leader A, member B, member C
    a = make_player(conn, "Alice")
    b = make_player(conn, "Bob")
    c = make_player(conn, "Carol")

    print("[guild-smoke] create guild by Alice")
    r = g_create(conn, a, "Crimson", "CRM")
    print(f"  → {r}")
    assert r["ok"]

    print("[guild-smoke] Bob joins")
    r = g_join(conn, b, r["guild_id"])
    print(f"  → {r}")
    assert r["ok"]

    # Promote Bob to officer by direct DB update (no promote endpoint in API)
    cur = conn.cursor()
    cur.execute("UPDATE guild_members SET rank='officer' WHERE player_id=?", (b,))
    conn.commit()

    print("[guild-smoke] Carol joins")
    r = g_join(conn, c, r["guild_id"])
    print(f"  → {r}")
    assert r["ok"]

    # Create a second guild for war
    d = make_player(conn, "Dave")
    r2 = g_create(conn, d, "Azure", "AZR")
    print(f"[guild-smoke] second guild created by Dave: {r2}")
    assert r2["ok"]

    print("[guild-smoke] Alice declares war on Azure")
    r = g_set_relation(conn, a, r2["guild_id"], "war")
    print(f"  → {r}")
    assert r["ok"]

    print("[guild-smoke] Alice kicks Carol")
    r = g_kick(conn, a, c)
    print(f"  → {r}")
    assert r["ok"]

    print("[guild-smoke] Bob (officer) tries to declare alliance with Azure")
    r = g_set_relation(conn, b, r2["guild_id"], "ally")
    print(f"  → {r}")
    assert r["ok"], f"officer should be able to ally; got {r}"

    g = get_guild(conn, get_guild(conn, "")["id"] if False else list_guilds(conn)[0]["id"])
    print(f"[guild-smoke] guild state: {g['name']} members={len(g['members'])} relations={g['relations']}")

    print("\n[guild-smoke] PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("smoke")
    p_create = sub.add_parser("create")
    p_create.add_argument("player_id"); p_create.add_argument("name"); p_create.add_argument("tag")
    p_join = sub.add_parser("join")
    p_join.add_argument("player_id"); p_join.add_argument("guild_id")
    args = ap.parse_args()

    if args.cmd == "smoke":
        return smoke()
    conn = connect()
    init_schema(conn)
    if args.cmd == "create":
        r = g_create(conn, args.player_id, args.name, args.tag)
        print(r)
        return 0 if r["ok"] else 1
    if args.cmd == "join":
        r = g_join(conn, args.player_id, args.guild_id)
        print(r)
        return 0 if r["ok"] else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
