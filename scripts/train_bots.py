#!/usr/bin/env python3
"""AI self-play training script — run this as a cron service.

Usage:
  python scripts/train_bots.py --rounds 10 --bots 5v5
  python scripts/train_bots.py --rounds 100 --interval 30  # every 30s

What it does:
  1. Auto-registers N bots with the server (warrior/mage/priest/hunter)
  2. Each "round" spawns a 5v5 (or 3v3 / 1v1) match between two teams
  3. Waits for match to complete (polls /api/v1/arena/matches)
  4. Reads the resulting bot_strategy_profiles to track fitness evolution
  5. Prints a summary of top bots by fitness
  6. Loops — every `interval` seconds, starts a new round

This is the "exercising muscle" of v7's training system — without it,
the bot_strategy_profiles table stays empty.
"""
import json
import time
import urllib.request
import urllib.error
import secrets
import argparse
import sqlite3
from pathlib import Path


BASE = "http://127.0.0.1:8787"
DB_PATH = "D:/Projects/ai-wow-simulator/data/world.db"


def call(path, method="GET", token=None, body=None, params=None):
    full = BASE + path
    if params:
        from urllib.parse import urlencode
        full += ("?" if "?" not in full else "&") + urlencode(params)
    data_bytes = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(full, method=method, data=data_bytes)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def register_bot(cls):
    """Register a new bot with the server. Returns (pid, token)."""
    suffix = secrets.token_hex(3)
    name = f"TrainBot_{cls}_{suffix}"
    s, r = call("/api/v1/register", "POST", body={"name": name, "cls": cls})
    if s != 200:
        raise RuntimeError(f"register failed: {s} {r}")
    return r["player_id"], r["token"]


def create_room(mode, creator_token):
    """Create a match_room and return room_id."""
    s, r = call("/api/v1/room/create", "POST", token=creator_token,
                 params=[("name", f"Training {secrets.token_hex(2)}"),
                         ("mode", mode)])
    if s != 200:
        raise RuntimeError(f"room/create failed: {s} {r}")
    return r["room_id"]


def join_room(room_id, team, token):
    s, r = call("/api/v1/room/join", "POST", token=token,
                 params=[("room_id", room_id), ("team", team)])
    return s, r


def wait_for_match_to_end(match_id, timeout=180):
    """Poll the match endpoint until ended=True or timeout."""
    start = time.time()
    while time.time() - start < timeout:
        s, r = call(f"/api/v1/arena/match/{match_id}", "GET")
        if s == 200 and r.get("ended"):
            return r
        time.sleep(2)
    return None


def read_training_leaderboard():
    """Read bot fitness stats from DB."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""SELECT pid, wins, losses, matches_played, fitness_history,
                              hp_retreat_threshold, ult_teamfight_min_enemies
                       FROM bot_strategy_profiles
                       ORDER BY wins DESC LIMIT 10""")
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        return []


def main():
    parser = argparse.ArgumentParser(description="AI self-play training")
    parser.add_argument("--rounds", type=int, default=5, help="number of training rounds")
    parser.add_argument("--interval", type=int, default=15, help="seconds between rounds")
    parser.add_argument("--mode", default="5v5", choices=["1v1", "3v3", "5v5"])
    parser.add_argument("--bots-per-team", type=int, default=None,
                        help="override team size (1=1v1, 3=3v3, 5=5v5)")
    args = parser.parse_args()

    team_size = args.bots_per_team or {"1v1": 1, "3v3": 3, "5v5": 5}[args.mode]
    print(f"[train_bots] starting: rounds={args.rounds} interval={args.interval}s mode={args.mode} team_size={team_size}")
    print(f"[train_bots] target server: {BASE}")
    print(f"[train_bots] db: {DB_PATH}")

    # Health check
    s, h = call("/health")
    if s != 200:
        print(f"[train_bots] server unreachable: {s}")
        return 1
    print(f"[train_bots] server OK: {h}")

    # Register bots (re-use across rounds so fitness accumulates)
    print(f"\n[train_bots] registering {team_size * 2} bots...")
    bots = []
    for i in range(team_size * 2):
        cls = ["warrior", "mage", "priest", "hunter"][i % 4]
        try:
            pid, token = register_bot(cls)
            bots.append({"pid": pid, "token": token, "cls": cls,
                         "team": "blue" if i < team_size else "red"})
            print(f"  ✓ bot {i+1}: {pid} ({cls}) team={bots[-1]['team']}")
        except Exception as e:
            print(f"  ✗ bot {i+1}: {e}")
    if len(bots) < team_size * 2:
        print(f"[train_bots] only got {len(bots)} bots, need {team_size * 2}")
        return 1

    # Run rounds
    for round_n in range(1, args.rounds + 1):
        print(f"\n=== Round {round_n}/{args.rounds} ===")
        # Pick creator (round-robin)
        creator = bots[(round_n - 1) % len(bots)]
        try:
            room_id = create_room(args.mode, creator["token"])
            print(f"  created room {room_id}")
        except Exception as e:
            print(f"  ✗ create_room: {e}")
            time.sleep(args.inter_interval)
            continue

        # All bots join
        match_started = False
        for bot in bots:
            s, j = join_room(room_id, bot["team"], bot["token"])
            if j.get("auto_started"):
                match_started = True
        print(f"  match_started={match_started}")

        if not match_started:
            # Wait briefly for bots to fill
            time.sleep(5)
            for bot in bots:
                s, j = join_room(room_id, bot["team"], bot["token"])
                if j.get("auto_started"):
                    match_started = True

        # Wait for the room to spawn a match via draft
        print(f"  waiting for match to spawn from room...")
        match_id = None
        for attempt in range(20):
            s, drafts = call("/api/v1/arena/drafts")
            if drafts.get("drafts"):
                # Find draft for this room (no direct mapping in MVP; just take any active)
                match_id = None  # we'll discover via matches endpoint
                break
            time.sleep(1)

        # Wait for an active match
        wait_start = time.time()
        while time.time() - wait_start < 60:
            s, ml = call("/api/v1/arena/matches", "GET")
            for m in ml.get("matches", []):
                if not m.get("ended"):
                    match_id = m["match_id"]
                    break
            if match_id:
                break
            time.sleep(2)

        if not match_id:
            print(f"  ✗ no match spawned, skipping")
            time.sleep(args.inter_interval)
            continue

        print(f"  watching match {match_id}...")
        result = wait_for_match_to_end(match_id, timeout=180)
        if result:
            print(f"  ✓ match ended: winner={result.get('winner')} tick={result['tick']}")
        else:
            print(f"  ✗ match timed out")

        # Show leaderboard so far
        lb = read_training_leaderboard()
        if lb:
            print(f"\n  training leaderboard (top 5):")
            for row in lb[:5]:
                pid_short = row[0][-8:]
                print(f"    {pid_short} W={row[1]} L={row[2]} matches={row[3]}")

        if round_n < args.rounds:
            print(f"  sleeping {args.inter_interval}s...")
            time.sleep(args.inter_interval)

    print(f"\n[train_bots] done — {args.rounds} rounds completed")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())