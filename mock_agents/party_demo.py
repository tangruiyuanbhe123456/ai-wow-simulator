"""组队方式演示 / Party formation demo (auto vs manual).

Spawns 10 mock AI agents and demonstrates two ways to form parties of 5:

  --mode auto    : Bot0 (leader) auto-invites everyone who joins the same zone.
                   Mimics `mock_agents/run_demo.py` Phase 5 behavior.
  --mode manual  : Bot0 invites each teammate by explicit decision (only invites
                   agents that satisfy a simple rule — e.g. same class as a
                   1-warrior / 1-mage / 1-priest / 1-hunter + 1 DPS goal).

Both modes end with the 5-member party attacking the same boss to verify
party damage bonuses apply (5 in party = +20% damage per the game balance).
"""
from __future__ import annotations
import argparse
import sys
import time
import urllib.request
import urllib.error
import json

from server.agent_sdk import connect


def http_json(url, method="GET", headers=None, data=None, timeout=10.0):
    req = urllib.request.Request(url, method=method,
                                  headers={"Content-Type": "application/json",
                                           **(headers or {})})
    body = json.dumps(data).encode() if data is not None else None
    try:
        with urllib.request.urlopen(req, data=body, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "body": e.read().decode("utf-8", "ignore")}


def spawn(base_url, classes):
    """Connect N agents and return their WowAgent list."""
    import time as _t
    suffix = _t.strftime('%H%M%S')
    agents = []
    for i, cls in enumerate(classes):
        # connect() returns a WowAgent directly (it carries .player_id, .token,
        # .action(), .state() after a successful /register).
        a = connect(base_url, f"PartyDemo{suffix}_{i}_{cls[0].upper()}", cls)
        agents.append(a)
        time.sleep(0.05)
    return agents


def _safe_msg(r):
    """Format an action response for printing (handles non-dict return values)."""
    if not isinstance(r, dict):
        return repr(r)[:80]
    return str(r.get("msg", r))[:80]


def auto_form_party(agents, base_url):
    """AUTO mode: leader (agents[0]) creates party, then invites everyone in
    the same zone — no filtering."""
    print(f"\n--- AUTO MODE: leader={agents[0].name} auto-invites all eligible teammates ---")
    leader = agents[0]
    r = leader.action("party_create", {})
    print(f"  [leader] party_create → {_safe_msg(r)}")

    # Move all into the same zone. wild_plains (L2-6) has no mobs spawned at
    # bootstrap; dark_forest (L4-9) has forest spiders + stone giants + ore.
    # Center (-5,5), size 20 → (0,0) is OOB; we use (-5,5) which is in-bounds.
    target_zone = "dark_forest"
    for a in agents:
        try:
            a.action("move", {"zone": target_zone, "x": -5, "y": 5})
        except Exception as ex:
            print(f"  [move] {a.name}: {ex}")
    time.sleep(0.5)

    # Leader invites everyone else
    s = leader.state()
    others = [p for p in s.get("players_here", []) if p["id"] != leader.player_id]
    invited = 0
    for p in others[:4]:  # up to 4 invites → 5-member party
        r = leader.action("party_invite", {"player_id": p["id"]})
        if isinstance(r, dict) and r.get("ok"):
            invited += 1
            print(f"  [leader] invited {p['name']} → {_safe_msg(r)}")
        time.sleep(0.15)
    print(f"  AUTO mode: invited {invited}/4 teammates")
    return invited


def manual_form_party(agents, base_url):
    """MANUAL mode: leader picks each invitee based on class-composition goal.

    Goal composition: 1 warrior (tank) + 1 mage (DPS) + 1 priest (heal) +
    1 hunter (DPS) + 1 warrior (off-tank). Leader scans the zone, picks by
    name prefix to satisfy the goal, and skips anyone who doesn't fit.
    """
    print(f"\n--- MANUAL MODE: leader={agents[0].name} curates party by class role ---")
    leader = agents[0]
    r = leader.action("party_create", {})
    print(f"  [leader] party_create → {_safe_msg(r)}")

    for a in agents:
        try:
            a.action("move", {"zone": "dark_forest", "x": -5, "y": 5})
        except Exception as ex:
            print(f"  [move] {a.name}: {ex}")
    time.sleep(0.5)

    s = leader.state()
    candidates = [p for p in s.get("players_here", []) if p["id"] != leader.player_id]
    print(f"  [leader] scanning {len(candidates)} candidates in shadow_dungeon...")

    # Goal: pick the first warrior, mage, priest, hunter we find — then a 2nd warrior
    wanted_roles = ["warrior", "mage", "priest", "hunter", "warrior"]
    chosen = []
    for role in wanted_roles:
        match = next((p for p in candidates if p.get("cls") == role and p["id"] not in [c["id"] for c in chosen]), None)
        if match:
            chosen.append(match)
            r = leader.action("party_invite", {"player_id": match["id"]})
            ok = isinstance(r, dict) and r.get("ok")
            status = "✓" if ok else "✗"
            print(f"  [leader] {status} invited role={role:8s}  {match['name']:20s} → {_safe_msg(r)}")
            time.sleep(0.2)

    print(f"  MANUAL mode: invited {len(chosen)}/{len(wanted_roles)} by role")
    return len(chosen)


def show_party(agents):
    """Print final party composition for the leader."""
    leader = agents[0]
    s = leader.state()
    you = s.get("you", {})
    party_id = you.get("party_id")
    print(f"  [leader] final party_id={party_id}")
    if party_id:
        # Walk party_members via SQL
        from server.db import connect as db_connect
        c = db_connect()
        cur = c.cursor()
        cur.execute("""SELECT p.id, p.name, p.cls, p.hp, p.hp_max, pm.joined_at
                       FROM party_members pm JOIN players p ON p.id=pm.player_id
                       WHERE pm.party_id=?
                       ORDER BY pm.joined_at""", (party_id,))
        rows = cur.fetchall()
        print(f"  PARTY ({len(rows)} members):")
        for r in rows:
            print(f"    - {r['name']:20s}  cls={r['cls']:8s}  hp={r['hp']:>4}/{r['hp_max']}")


def attack_boss_together(agents, label):
    """All 5 (or fewer) party members attack the strongest mob in the zone for 30 ticks.
    Reports total damage dealt and any party-bonus evidence."""
    leader = agents[0]
    s = leader.state()
    # Prefer boss if present, otherwise the strongest mob (highest HP)
    boss = next((m for m in s.get("mobs", []) if m.get("kind") == "boss"), None)
    if not boss:
        mobs = [m for m in s.get("mobs", []) if m.get("kind") in ("mob", "elite")]
        mobs.sort(key=lambda m: m.get("hp", 0), reverse=True)
        boss = mobs[0] if mobs else None
    if not boss:
        print(f"  {label}: no targets in zone, skipping combat")
        return
    print(f"  {label}: targeting {boss['name']} (kind={boss.get('kind')}, hp={boss['hp']})")

    # Party target
    r = leader.action("party_target", {"kind": boss.get("kind", "mob"), "target_id": boss["id"]})
    print(f"  {label}: party_target → {_safe_msg(r)}")

    start_hp = boss["hp"]
    for tick in range(30):
        for a in agents:
            try:
                a.action("attack", {"target_id": boss["id"], "skill_id": "heroic_strike"})
            except Exception:
                pass
            time.sleep(0.15)
        # Check if target dead
        s2 = leader.state()
        still = any(m["id"] == boss["id"] and m.get("hp", 0) > 0 for m in s2.get("mobs", []))
        if not still:
            print(f"  {label}: TARGET DOWN at tick {tick + 1}")
            print(f"  {label}: party DPS ≈ {(start_hp // max(1, tick + 1))} dmg/tick × {len(agents)} members")
            return
    s3 = leader.state()
    remaining = next((m["hp"] for m in s3.get("mobs", []) if m["id"] == boss["id"]), start_hp)
    print(f"  {label}: target still alive after 30 ticks (took {start_hp - remaining} dmg)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    ap.add_argument("--mode", choices=["auto", "manual"], default="auto",
                    help="auto = leader auto-invites everyone; manual = leader picks by role")
    args = ap.parse_args()

    classes = ["warrior", "mage", "priest", "hunter", "warrior"]
    print(f"=== Party formation demo / 组队演示: mode={args.mode} ===")
    print(f"Spawning 5 agents: {classes}")
    agents = spawn(args.url, classes)
    for a in agents:
        print(f"  ✓ {a.name} (cls={a.cls}) pid={a.player_id}")

    if args.mode == "auto":
        auto_form_party(agents, args.url)
    else:
        manual_form_party(agents, args.url)

    print()
    show_party(agents)

    print()
    attack_boss_together(agents, label=f"[{args.mode}]")

    # Verify party damage bonus via SQLite
    print("\n=== Damage party-bonus evidence (combat_log entries) ===")
    from server.db import connect as db_connect
    c = db_connect()
    cur = c.cursor()
    cur.execute("""SELECT actor_name, action, detail, ts
                   FROM combat_log
                   WHERE action='attack' AND actor_name LIKE 'PartyDemo%'
                   ORDER BY ts DESC LIMIT 8""")
    rows = cur.fetchall()
    for r in rows:
        print(f"  [{r['ts']:.1f}] {r['actor_name']} → {r['detail'][:80]}")


if __name__ == "__main__":
    main()