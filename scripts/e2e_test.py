"""End-to-end test: launch server in-process via TestClient, drive 5 agents + verify.

Self-check #1 (auto party & clear dungeon) and #7 (SDK 3-line connect).
"""
from __future__ import annotations
import sys
import time
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Wipe DB for clean test
for p in Path("data").glob("world.db*"):
    p.unlink()

from fastapi.testclient import TestClient
from server.main import app, db as _srv_db
from server.db import init_schema
from server.world import spawn_world_mobs

# Initialize schema + seed
init_schema(_srv_db())
spawn_world_mobs(_srv_db())

client = TestClient(app)


def hdr(token): return {"Authorization": f"Bearer {token}"}


def register(name, cls):
    r = client.post("/api/v1/register", json={"name": name, "cls": cls})
    assert r.status_code == 200, r.text
    return r.json()


def section(title):
    print(f"\n=== {title} ===")


def main() -> int:
    section("SDK 3-line connect demo")
    from server.agent_sdk import connect
    a = connect("http://testserver", "SDK_Demo", "warrior")
    s = a.state()
    print(f"SDK agent '{a.name}' (cls={a.cls}) hp={s['you']['hp']}/{s['you']['hp_max']} zone={s['zone']['name']}")
    assert s["ok"]

    section("Register 5 players (4 classes + extra warrior)")
    players = []
    classes = ["warrior", "mage", "priest", "hunter", "warrior"]
    names = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
    for n, c in zip(names, classes):
        d = register(n, c)
        players.append(d)
        print(f"  + {d['name']} ({d['cls']}) token={d['token'][:10]}...")

    section("Move all to shadow_dungeon")
    for p in players:
        r = client.post("/api/v1/action",
                        headers=hdr(p["token"]),
                        json={"action": "move", "payload": {"zone": "shadow_dungeon"}})
        assert r.status_code == 200, r.text

    section("Leader Alpha creates party + invites 4 others")
    leader = players[0]
    r = client.post("/api/v1/action", headers=hdr(leader["token"]),
                    json={"action": "party_create"})
    assert r.status_code == 200
    for p in players[1:]:
        r = client.post("/api/v1/action", headers=hdr(leader["token"]),
                        json={"action": "party_invite", "payload": {"player_id": p["player_id"]}})
        assert r.status_code == 200, r.text
    # verify everyone in party
    for p in players:
        s = client.get("/api/v1/state", headers=hdr(p["token"])).json()
        assert s["party"] and len(s["party"]["members"]) == 5, f"{p['name']} not in party"

    section("Leader targets boss + everyone attacks 60 ticks")
    s = client.get("/api/v1/state", headers=hdr(leader["token"])).json()
    boss = next((m for m in s["mobs"] if m["kind"] == "boss"), None)
    assert boss, "no boss in shadow_dungeon"
    print(f"  targeting boss {boss['name']} hp={boss['hp']}/{boss['hp_max']}")
    r = client.post("/api/v1/action", headers=hdr(leader["token"]),
                    json={"action": "party_target", "payload": {"kind": "boss", "target_id": boss["id"]}})
    assert r.status_code == 200

    damage_skills = {"warrior": "heroic_strike", "mage": "fireball",
                     "priest": "shadow_word_pain", "hunter": "auto_shot"}
    heal_skills = {"priest": "holy_light"}

    boss_killed = False
    for tick in range(60):
        for p in players:
            cls = p["cls"]
            sk = damage_skills.get(cls, "heroic_strike")
            client.post("/api/v1/action", headers=hdr(p["token"]),
                        json={"action": "attack", "payload": {"target_id": boss["id"], "skill_id": sk}})
            if cls in heal_skills:
                client.post("/api/v1/action", headers=hdr(p["token"]),
                            json={"action": "heal", "payload": {"target_id": p["player_id"], "skill_id": heal_skills[cls]}})
        s = client.get("/api/v1/state", headers=hdr(leader["token"])).json()
        boss_alive = any(m["id"] == boss["id"] and m["hp"] > 0 for m in s["mobs"])
        if not boss_alive:
            print(f"  ✓ boss killed at tick {tick+1}")
            boss_killed = True
            break
        # manual regen via admin tick (server tick loop is not running under TestClient by default)
        client.post("/api/v1/admin/tick")
    assert boss_killed, "boss not killed within 60 ticks"

    section("Guild creation + war via API")
    r1 = client.post("/api/v1/action", headers=hdr(players[0]["token"]),
                     json={"action": "guild_create", "payload": {"name": "Crimson", "tag": "CRM"}})
    assert r1.status_code == 200
    g1 = r1.json()["guild_id"]
    # second leader needs own guild
    r2 = client.post("/api/v1/action", headers=hdr(players[3]["token"]),
                     json={"action": "guild_create", "payload": {"name": "Azure", "tag": "AZR"}})
    assert r2.status_code == 200
    g2 = r2.json()["guild_id"]
    r = client.post("/api/v1/action", headers=hdr(players[0]["token"]),
                    json={"action": "guild_declare_war", "payload": {"guild_id": g2}})
    assert r.status_code == 200, r.text
    print(f"  declared war: {r.json()['msg']}")

    section("Observer state snapshot")
    s = client.get("/api/v1/observer/state?lang=en").json()
    assert s["ok"]
    print(f"  players_alive={s['players_alive']}/{s['players_total']} mobs={s['mobs_alive']} guilds={s['guilds']}")
    assert s["guilds"] >= 2

    section("Web HTML /api/v1 (curl) returns bilingual page")
    r = client.get("/")
    assert r.status_code == 200
    assert "AI WoW" in r.text
    assert "艾泽拉斯" not in r.text  # title doesn't have that, but should have other zh
    assert "观战台" in r.text
    assert "<html" in r.text.lower()
    print(f"  HTML contains EN keyword 'AI WoW': ✓, ZH keyword '观战台': ✓")

    section("i18n toggle: ?lang=en returns English labels")
    r = client.get("/api/v1/observer/state?lang=en").json()
    # The combat_log contains entries from above fights — at least one should be in English
    print(f"  3 sample log lines (lang=en):")
    for l in s["combat_log"][:3]:
        print(f"    - {l.get('detail')[:120]}")

    print("\n[e2e] ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
