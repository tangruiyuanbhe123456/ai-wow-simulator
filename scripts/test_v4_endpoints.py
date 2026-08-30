import json, secrets, sqlite3, urllib.request, urllib.error

BASE = "http://127.0.0.1:8788"
DB = "D:/Projects/ai-wow-simulator/data/world.db"

def call(path, method="GET", token=None, body=None, params=None):
    full = BASE + path
    if params:
        from urllib.parse import urlencode
        full += ("?" if "?" not in full else "&") + urlencode(params)
    data_bytes = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(full, method=method, data=data_bytes)
    req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

suffix = secrets.token_hex(3)
s, r1 = call("/api/v1/register", "POST", body={"name": f"V4A{suffix}", "cls": "warrior"})
s, r2 = call("/api/v1/register", "POST", body={"name": f"V4B{suffix}", "cls": "mage"})
print(f"A: {r1['player_id']}")
print(f"B: {r2['player_id']}")
t1, t2 = r1["token"], r2["token"]
p1, p2 = r1["player_id"], r2["player_id"]

conn = sqlite3.connect(DB)
def gold(pid): return conn.execute("SELECT gold FROM players WHERE id=?", (pid,)).fetchone()[0]
print(f"\nstart: A={gold(p1)} B={gold(p2)}")

# Trade: A offers 10g to B (small enough that A has it)
s, off = call("/api/v1/trade/offer", "POST", token=t1, params=[("to_pid", p2), ("gold", "10"), ("items", "{}")])
print(f"\ntrade/offer: {s} {off}")
oid = off.get("offer_id")

s, lst = call("/api/v1/trade/list", "GET", token=t2)
print(f"trade/list: {s} count={len(lst.get('offers',[]))}")

if oid:
    s, acc = call("/api/v1/trade/accept", "POST", token=t2, params=[("offer_id", oid)])
    print(f"trade/accept: {s} {acc}")
    print(f"\nafter: A={gold(p1)} B={gold(p2)} (A -10, B +10 expected)")

    s, hist = call("/api/v1/trade/history", "GET", token=t1)
    print(f"trade/history: {s} count={len(hist.get('history',[]))}")

# Friends
print("\n--- Friends ---")
s, fr = call("/api/v1/friends/request", "POST", token=t1, params=[("friend_pid", p2)])
print(f"A->B: {s} {fr}")
s, fr2 = call("/api/v1/friends/request", "POST", token=t2, params=[("friend_pid", p1)])
print(f"B->A: {s} {fr2}")
s, fl = call("/api/v1/friends/list", "GET", token=t2)
print(f"B friends: {s} count={len(fl.get('my_friends',[]))}")
for f in fl.get("my_friends", []):
    print(f"  {f['name']} status={f['status']}")
conn.close()
