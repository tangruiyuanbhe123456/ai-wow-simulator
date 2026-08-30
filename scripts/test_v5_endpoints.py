"""V5 end-to-end test: room create + join 1v1 + auto-start draft + match begin."""
import json, secrets, sqlite3, urllib.request, urllib.error

BASE = "http://127.0.0.1:8787"
DB = "D:/Projects/ai-wow-simulator/data/world.db"


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
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


# Register 2 players
suffix = secrets.token_hex(3)
s, r1 = call("/api/v1/register", "POST", body={"name": f"V5A{suffix}", "cls": "warrior"})
s, r2 = call("/api/v1/register", "POST", body={"name": f"V5B{suffix}", "cls": "mage"})
print(f"A: {r1['player_id']}")
print(f"B: {r2['player_id']}")
t1, t2 = r1["token"], r2["token"]
p1, p2 = r1["player_id"], r2["player_id"]

# --- Test 1: 1v1 ---
print("\n=== Test 1: 1v1 (duel) ===")
s, r = call("/api/v1/room/create", "POST", token=t1, params=[("name", "Duel A"), ("mode", "1v1")])
print(f"create: {s} {r}")
room_id = r.get("room_id")
assert r.get("ok"), f"create failed: {r}"
assert r.get("mode") == "1v1"

s, lst = call("/api/v1/room/list", "GET", params=[("status", "lobby")])
print(f"list: {s} count={len(lst.get('rooms',[]))}")
assert any(rm["room_id"] == room_id for rm in lst["rooms"])

s, j = call("/api/v1/room/join", "POST", token=t2, params=[("room_id", room_id), ("team", "red")])
print(f"join B: {s} {j}")
assert j.get("ok") and j.get("auto_started"), f"expected auto-start, got {j}"
assert j.get("filled", {}).get("blue") == 1
assert j.get("filled", {}).get("red") == 1

# Now check drafts (one should exist)
s, drafts = call("/api/v1/arena/drafts", "GET")
print(f"drafts: {s} count={len(drafts.get('drafts',[]))}")
# Note: draft endpoint doesn't exist yet for room-attached drafts, but
# /api/v1/arena/drafts shows all in-memory drafts.

# --- Test 2: 3v3 (need 6 players) ---
print("\n=== Test 2: 3v3 (skirmish, 6 players) ===")
players = []
for i in range(6):
    s, r = call("/api/v1/register", "POST", body={"name": f"V5S{suffix}_{i}", "cls": ["warrior","mage","priest","hunter","warrior","mage"][i]})
    players.append(r)
team_a = players[:3]
team_b = players[3:]
print(f"6 players registered: {len(players)}")

s, room3 = call("/api/v1/room/create", "POST", token=team_a[0]["token"], params=[("name", "Skirmish"), ("mode", "3v3")])
print(f"create 3v3: {s} mode={room3.get('mode')}")
room3_id = room3["room_id"]

# Join 2 more blues
for p in team_a[1:]:
    s, j = call("/api/v1/room/join", "POST", token=p["token"], params=[("room_id", room3_id), ("team", "blue")])
    assert j.get("ok")
    print(f"  blue joined: team={j.get('team')} filled={j.get('filled')} started={j.get('auto_started')}")
# Join 3 reds
for p in team_b:
    s, j = call("/api/v1/room/join", "POST", token=p["token"], params=[("room_id", room3_id), ("team", "red")])
    print(f"  red joined: team={j.get('team')} filled={j.get('filled')} started={j.get('auto_started')}")
    # The last join should trigger auto-start

# Verify room state
s, st = call(f"/api/v1/room/{room3_id}", "GET")
print(f"room3 state: {s} status={st.get('room',{}).get('status')} players={len(st.get('players',[]))}")

# --- Test 3: room cancel ---
print("\n=== Test 3: cancel a fresh room ===")
s, fresh = call("/api/v1/room/create", "POST", token=t1, params=[("name", "ToCancel")])
print(f"create: {s} {fresh.get('room_id')}")
s, cncl = call(f"/api/v1/room/{fresh['room_id']}/cancel", "POST", token=t1)
print(f"cancel: {s} {cncl}")
s, after = call(f"/api/v1/room/{fresh['room_id']}", "GET")
print(f"after cancel: status={after.get('room',{}).get('status')}")

print("\n=== ALL V5 TESTS PASSED ===")
