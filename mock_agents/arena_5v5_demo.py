"""5v5 arena demo: spawn 10 mock AI agents that queue for arena, then watch
the resulting match run to completion.

Each agent:
  1. POST /register → get token
  2. POST /arena/queue with Bearer token
When the 10th player joins, the server auto-starts a DRAFT (ban/pick phase).
The script then:
  - Reads the hero pool from /api/v1/arena/drafts
  - Submits bans (blue: mage_fire, red: warrior_dps) — distinct heroes
  - Submits picks (10 players choose allowed heroes via API)
When the draft ends, the server auto-builds the ArenaMatch from the
picks. The script polls /arena/match/<id> until ended, printing a summary.
"""
from __future__ import annotations
import argparse
import sys
import time
import urllib.request
import urllib.error
import json

from server.agent_sdk import connect   # uses the same SDK to get a Bearer token


def http_json(url, method="GET", headers=None, data=None, timeout=10.0,
              params=None):
    """Send an HTTP request. `params` is a list of (key, value) tuples for the
    query string (used because the draft endpoints take query params)."""
    if params:
        from urllib.parse import urlencode
        qs = urlencode(params)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{qs}"
    req = urllib.request.Request(url, method=method,
                                  headers={"Content-Type": "application/json",
                                           **(headers or {})})
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(req, data=body, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "body": e.read().decode("utf-8", "ignore")}


def run_arena_demo(base_url: str):
    classes = ["warrior", "mage", "priest", "hunter", "warrior",
               "warrior", "mage", "priest", "hunter", "warrior"]
    tokens = []
    print("=== 5v5 ARENA DEMO (with draft) / 5v5 王者 demo 含选秀 ===")
    print(f"Step 1: register 10 agents at {base_url}")
    for i, cls in enumerate(classes):
        import time as _t
        suffix = _t.strftime('%H%M%S')
        a = connect(base_url, f"PvPBot{suffix}_{i}_{cls[0].upper()}", cls)
        bearer = getattr(a, "token", None)
        tokens.append((a.player_id, a.name, cls, bearer))
        print(f"  ✓ {a.name} ({cls}) registered, pid={a.player_id}, token={bearer[:8] if bearer else 'NONE'}...")
        time.sleep(0.1)

    print("\nStep 2: each agent joins the 5v5 queue (auto-enters draft at 10)")
    tokens_with_bearer = tokens

    draft_id = None
    for i, (pid, name, cls, bearer) in enumerate(tokens_with_bearer):
        r = http_json(f"{base_url}/api/v1/arena/queue", method="POST",
                      headers={"Authorization": f"Bearer {bearer}"})
        if r.get("draft_id"):
            draft_id = r["draft_id"]
            print(f"  🎯 {name} triggered draft: {r.get('msg', '')[:60]}")
            print(f"     draft_id={draft_id}")
            break
        msg = r.get("msg", "?")
        print(f"  ✓ {name} queued: {msg[:60]}")
        time.sleep(0.1)

    if not draft_id:
        print("  ! No draft formed yet (should not happen with 10 agents)")
        return

    # Get hero pool
    print(f"\nStep 2.5: fetch hero + spell pool")
    pool_r = http_json(f"{base_url}/api/v1/arena/drafts")
    heroes = pool_r.get("heroes", [])
    spells = pool_r.get("spells", [])
    print(f"  hero pool ({len(heroes)} heroes)")
    print(f"  spell pool ({len(spells)} spells):")
    for s in spells:
        print(f"    - {s['id']:14s}  {s['name_zh']} | {s['name_en']} — {s.get('desc_zh','')}")

    # Submit bans
    print(f"\nStep 3: bans")
    blue_ban = "mage_fire"
    red_ban = "warrior_dps"
    r = http_json(f"{base_url}/api/v1/arena/draft/{draft_id}/ban",
                  method="POST",
                  params=[("lang", "zh"), ("team", "blue"), ("hero", blue_ban)])
    print(f"  blue bans {blue_ban}: ok={r.get('ok')}")
    r = http_json(f"{base_url}/api/v1/arena/draft/{draft_id}/ban",
                  method="POST",
                  params=[("lang", "zh"), ("team", "red"), ("hero", red_ban)])
    print(f"  red bans {red_ban}: ok={r.get('ok')}")

    # Submit picks — 10 players (5 blue + 5 red), each picks a hero
    # Banned: mage_fire (blue), warrior_dps (red). Pool = 12 - 2 = 10 unique → fits 5+5
    print(f"\nStep 4: picks (5+5 = 10 unique heroes from 12-hero pool minus 2 bans)")
    blue_pids = [t[0] for t in tokens[:5]]
    red_pids = [t[0] for t in tokens[5:]]
    blue_pick_plan = ["warrior_tank", "warrior_guard", "mage_ice", "priest_heal", "hunter_bow"]
    red_pick_plan = ["warrior_dps", "mage_fire", "mage_arcane", "priest_dark", "hunter_trap"]
    picks_made = 0
    for pid, hero in zip(blue_pids, blue_pick_plan):
        r = http_json(f"{base_url}/api/v1/arena/draft/{draft_id}/pick",
                      method="POST",
                      params=[("lang", "zh"), ("pid", pid), ("hero", hero)])
        if r.get("ok"):
            picks_made += 1
            print(f"  ✓ blue {pid[-8:]}... picks {hero} (total {picks_made}/10)")
        else:
            print(f"  ✗ blue {pid[-8:]}... picks {hero}: {r.get('error')}")
        time.sleep(0.05)
    for pid, hero in zip(red_pids, red_pick_plan):
        r = http_json(f"{base_url}/api/v1/arena/draft/{draft_id}/pick",
                      method="POST",
                      params=[("lang", "zh"), ("pid", pid), ("hero", hero)])
        if r.get("ok"):
            picks_made += 1
            print(f"  ✓ red  {pid[-8:]}... picks {hero} (total {picks_made}/10)")
        else:
            print(f"  ✗ red  {pid[-8:]}... picks {hero}: {r.get('error')}")
        time.sleep(0.05)
    print(f"  → picks_made={picks_made} (others will auto-pick if needed)")

    # Submit spell picks — each player picks 1 summoner spell
    print(f"\nStep 4.5: spell picks (1 per player)")
    spell_plan = ["flash", "heal", "ignite", "ghost", "exhaust"]   # blue picks
    spell_plan += ["barrier", "cleanse", "smite", "exhaust", "flash"]  # red picks
    spell_made = 0
    for pid, spell in zip(blue_pids + red_pids, spell_plan):
        r = http_json(f"{base_url}/api/v1/arena/draft/{draft_id}/spell",
                      method="POST",
                      params=[("lang", "zh"), ("pid", pid), ("spell", spell)])
        if r.get("ok"):
            spell_made += 1
            team = "blue" if pid in blue_pids else "red"
            print(f"  ✓ {team} {pid[-8:]}... picks spell [{spell}] ({spell_made}/10)")
        else:
            print(f"  ✗ {pid[-8:]}... picks spell [{spell}]: {r.get('error')}")
        time.sleep(0.05)

    # Step 5 — wait for draft to end + match to start
    print(f"\nStep 5: polling match state")
    match_id = None
    deadline = time.time() + 90
    while time.time() < deadline and match_id is None:
        matches = http_json(f"{base_url}/api/v1/arena/matches")
        if matches.get("matches"):
            match_id = matches["matches"][0]["match_id"]
            break
        time.sleep(0.5)

    if not match_id:
        print("  ! No match started within 90s — likely waiting for auto-fill timeout")
        return

    print(f"  🎯 match_id={match_id}")

    # Step 6 — watch the match
    print(f"\nStep 6: watching match {match_id} (max 120s)")
    deadline = time.time() + 120
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
            dragons = s.get("dragons", [])
            buffs = s.get("buffs", {})
            extra = ""
            if dragons:
                dragon_pos = ", ".join(f"{d['kind']}@{d['pos']}" for d in dragons)
                extra += f"  🐉 [{dragon_pos}]"
            if buffs:
                extra += f"  ⚡ {buffs}"
            print(f"  t={t:3d} blue={blue_alive}/5 hp={blue_hp:4d} kills={score_b}  "
                  f"red={red_alive}/5 hp={red_hp:4d} kills={score_r}{extra}")
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
            # Show equipment/spell/ult for the first agent on each team
            for a in s['blue'][:1] + s['red'][:1]:
                equip = ', '.join(f"{k}:{v}" for k, v in a['equipment'].items() if v) or 'none'
                print(f"   {a['team']} {a['name']}: gold={a.get('gold')} ult={a.get('ultimate')}(cd={a.get('ult_cd')}) spell={a.get('spell')}(used={a.get('spell_used')}) equip=[{equip}]")
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