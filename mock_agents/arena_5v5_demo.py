"""5v5 arena demo: spawn 10 mock AI agents that queue for arena, then watch
the resulting match run to completion.

Each agent:
  1. POST /register → get token
  2. POST /arena/queue with Bearer token
When the 10th player joins, the server auto-forms a 5v5 match.
The script then polls /arena/match/<id> until ended, printing a summary.
"""
from __future__ import annotations
import argparse
import sys
import time
import threading
import urllib.request
import urllib.error
import json

from server.agent_sdk import connect   # uses the same SDK to get a Bearer token


def http_json(url: str, method: str = "GET", headers: dict | None = None,
              data: dict | None = None, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, method=method,
                                  headers={"Content-Type": "application/json",
                                           **(headers or {})})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
    try:
        with urllib.request.urlopen(req, data=body, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "http_error": e.code, "body": e.read().decode("utf-8", "ignore")}


def run_arena_demo(base_url: str):
    classes = ["warrior", "mage", "priest", "hunter", "warrior",
               "warrior", "mage", "priest", "hunter", "warrior"]
    tokens = []
    print("=== 5v5 ARENA DEMO / 5v5 王者玩法 demo ===")
    print(f"Step 1: register 10 agents at {base_url}")
    for i, cls in enumerate(classes):
        # connect() returns a WowAgent directly (not SmartAgent); it carries
        # .token, .player_id, .name, .cls after a successful /register.
        # Use a unique name prefix to avoid collisions with NPCs spawned at
        # bootstrap time (seed_world may pre-create some "BotX_Y" agents).
        import time as _t
        suffix = _t.strftime('%H%M%S')
        a = connect(base_url, f"PvPBot{suffix}_{i}_{cls[0].upper()}", cls)
        bearer = getattr(a, "token", None)
        tokens.append((a.player_id, a.name, cls, bearer))
        print(f"  ✓ {a.name} ({cls}) registered, pid={a.player_id}, token={bearer[:8] if bearer else 'NONE'}...")
        time.sleep(0.1)

    print("\nStep 2: each agent joins the 5v5 queue")
    print("  (auto-forms a match when the 10th agent queues)")
    tokens_with_bearer = tokens  # already (pid, name, cls, token) tuples

    match_id = None
    for i, (pid, name, cls, bearer) in enumerate(tokens_with_bearer):
        r = http_json(f"{base_url}/api/v1/arena/queue", method="POST",
                      headers={"Authorization": f"Bearer {bearer}"})
        if r.get("match_id"):
            match_id = r["match_id"]
            print(f"  🎯 {name} triggered match formation: {r.get('msg', '')[:60]}")
            print(f"     match_id={match_id}")
            break
        msg = r.get("msg", "?")
        print(f"  ✓ {name} queued: {msg[:60]}")
        time.sleep(0.1)

    if not match_id:
        print("  ! No match formed yet (should not happen with 10 agents)")
        return

    # Step 3: poll the match until ended
    print(f"\nStep 3: watching match {match_id} (max 90s)")
    deadline = time.time() + 90
    last_tick = -1
    while time.time() < deadline:
        s = http_json(f"{base_url}/api/v1/arena/match/{match_id}?lang=zh")
        if not s.get("ok"):
            print(f"  ! poll failed: {s}")
            time.sleep(1)
            continue
        t = s["tick"]
        if t != last_tick:
            last_tick = t
            blue_hp = s["crystals"]["blue"]["hp"]
            red_hp = s["crystals"]["red"]["hp"]
            blue_alive = sum(1 for x in s["blue"] if x["alive"])
            red_alive = sum(1 for x in s["red"] if x["alive"])
            score_b = s["team_kills"]["blue"]
            score_r = s["team_kills"]["red"]
            print(f"  t={t:3d} blue={blue_alive}/5 hp={blue_hp:4d} kills={score_b}  "
                  f"red={red_alive}/5 hp={red_hp:4d} kills={score_r}")
            # Print last event
            if s["log"]:
                last_evt = s["log"][-1]
                print(f"         └ {last_evt['msg'][:100]}")
        if s.get("ended"):
            print(f"\n🏆 MATCH OVER! winner = {s['winner']} at tick {s['tick']}")
            print(f"   final: blue crystal={s['crystals']['blue']['hp']}, "
                  f"red crystal={s['crystals']['red']['hp']}")
            print(f"   kills: blue={s['team_kills']['blue']}, red={s['team_kills']['red']}")
            print(f"   crystal damage: blue={s['team_dmg_to_crystal']['blue']}, "
                  f"red={s['team_dmg_to_crystal']['red']}")
            return
        time.sleep(0.5)
    print("  ! Timed out waiting for match to end")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    args = ap.parse_args()
    run_arena_demo(args.url)


if __name__ == "__main__":
    main()