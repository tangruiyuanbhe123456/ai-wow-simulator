"""V8 end-to-end test: tournament create + register + start + match begin."""
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
        return e.code, json.loads(e.read().decode())


# Register 4 captains (each with 5 player pids for 5v5 mode)
suffix = secrets.token_hex(3)
teams = []
for i in range(4):
    s, r = call("/api/v1/register", "POST", body={"name": f"V8T{suffix}_{i}", "cls": "warrior"})
    captain_pid = r["player_id"]
    captain_token = r["token"]
    # Create 4 fake players per team (for 5v5)
    players = [captain_pid]
    for j in range(4):
        s, pr = call("/api/v1/register", "POST", body={"name": f"V8T{suffix}_{i}_{j}", "cls": "warrior"})
        players.append(pr["player_id"])
    teams.append({"name": f"Team {i+1}", "captain": captain_pid, "captain_token": captain_token, "players": players})

print(f"registered {len(teams)} teams")

# Tournament create (by team 0 captain)
s, tn = call("/api/v1/tournament/create", "POST", token=teams[0]["captain_token"],
             params=[("name", "V8 Cup"), ("size", "4"), ("mode", "5v5")])
print(f"tournament create: {s} {tn}")
tournament_id = tn["tournament_id"]

# Register teams
for i, t in enumerate(teams):
    s, reg = call(f"/api/v1/tournament/{tournament_id}/register_team", "POST",
                  token=t["captain_token"],
                  params=[("team_name", t["name"]),
                          ("captain_pid", t["captain"]),
                          ("players", json.dumps(t["players"]))])
    print(f"register team {i+1}: {s} slot={reg.get('team_id')}")

# List tournaments
s, lst = call("/api/v1/tournaments", "GET")
print(f"\ntournaments list: {s} count={len(lst.get('tournaments', []))}")

# Get tournament state
s, st = call(f"/api/v1/tournament/{tournament_id}", "GET")
print(f"tournament state: {s} status={st['tournament']['status']} teams={len(st['teams'])}")

# Start tournament
s, started = call(f"/api/v1/tournament/{tournament_id}/start", "POST",
                   token=teams[0]["captain_token"])
print(f"\ntournament start: {s}")
if started.get("ok"):
    print(f"  bracket: {started['bracket']}")
    print(f"  matches: {started['matches']}")
else:
    print(f"  ERR: {started}")

# Wait for matches to begin
import time
time.sleep(15)
matches_url = f"{BASE}/api/v1/arena/matches?lang=en"
s, mlist = call("/api/v1/arena/matches", "GET")
print(f"\nactive matches: {s} count={len(mlist.get('matches', []))}")
for m in mlist.get('matches', []):
    print(f"  {m['match_id']} t={m['tick']} winner={m.get('winner')}")

# Test human action endpoint
if mlist.get('matches'):
    mid = mlist['matches'][0]['match_id']
    # Get a player from that match
    s, md = call(f"/api/v1/arena/match/{mid}?lang=en")
    a_pid = md['blue'][0]['pid']
    print(f"\ntest human action on match {mid} player pid={a_pid}")
    s, ar = call(f"/api/v1/match/{mid}/action", "POST",
                  params=[("pid", a_pid), ("action", "move"),
                          ("payload", json.dumps({"x": 25, "y": 25}))])
    print(f"  move: {s} {ar}")
    s, ar2 = call(f"/api/v1/match/{mid}/action", "POST",
                   params=[("pid", a_pid), ("action", "cast_spell")])
    print(f"  cast_spell: {s} {ar2}")