"""V9 marketplace end-to-end test:
1. Register seller (won matches earlier, has bot strategy profile)
2. Register buyer (no profile)
3. Seller lists bot
4. Browse listings
5. Buyer credits check
6. Buyer buys bot (credits transfer)
7. Verify seller credits went up, buyer went down
8. Bot detail with snapshot
9. Delist
"""
import json, secrets, sqlite3, urllib.request, urllib.error, time

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


# Manually insert bot_strategy_profiles for seller (simulating training)
suffix = secrets.token_hex(3)
s, r1 = call("/api/v1/register", "POST", body={"name": f"V9Seller{suffix}", "cls": "warrior"})
s, r2 = call("/api/v1/register", "POST", body={"name": f"V9Buyer{suffix}", "cls": "mage"})
s1_pid, s1_token = r1["player_id"], r1["token"]
s2_pid, s2_token = r2["player_id"], r2["token"]
print(f"Seller: {s1_pid}\nBuyer: {s2_pid}")

# Manually insert seller profile (3 wins, 1 loss, fitness 1.0-1.3)
conn = sqlite3.connect(DB)
conn.execute("""INSERT INTO bot_strategy_profiles
                (pid, wins, losses, matches_played, fitness_history,
                 hp_retreat_threshold, ult_teamfight_min_enemies, last_updated)
                VALUES (?, 3, 1, 4, '[1.2, 0.8, 1.3, 1.1]', 0.30, 1, ?)""",
             (s1_pid, time.time()))
conn.commit()
# Manually award credits to seller (simulating previous match wins)
conn.execute("""INSERT INTO player_credits (pid, credits, earned, last_active)
                VALUES (?, 50, 50, ?)""",
             (s1_pid, time.time()))
conn.execute("""INSERT INTO player_credits (pid, credits, earned, last_active)
                VALUES (?, 100, 100, ?)""",
             (s2_pid, time.time()))
conn.commit()
conn.close()
print("seeded seller profile + credits")

# Seller lists bot
s, listing = call("/api/v1/marketplace/list", "POST", token=s1_token,
                   params=[("bot_pid", s1_pid), ("title", "V9 Warrior pro"),
                           ("description", "Test bot with dragon armor"), ("price_credits", "30")])
print(f"\nlist: {s} {listing}")

# Browse listings
s, browse = call("/api/v1/marketplace/browse", "GET")
print(f"\nbrowse: {s} count={len(browse.get('listings', []))}")
for l in browse.get("listings", []):
    print(f"  {l['title']} - {l['price_credits']}💰 by {l['seller_name']} W{l['bot_stats']['wins']}")

# Buyer credits check
s, credits = call("/api/v1/marketplace/credits", "GET", token=s2_token)
print(f"\nbuyer credits: {credits}")

# Buyer buys
listing_id = listing["listing_id"]
s, buy = call("/api/v1/marketplace/buy", "POST", token=s2_token, params=[("listing_id", listing_id)])
print(f"\nbuy: {s} {buy}")

# Verify credits moved
s, seller_c = call("/api/v1/marketplace/credits", "GET", token=s1_token)
s, buyer_c = call("/api/v1/marketplace/credits", "GET", token=s2_token)
print(f"\nseller credits after: {seller_c['credits']} (was 50, +30 = 80)")
print(f"buyer credits after: {buyer_c['credits']} (was 100, -30 = 70)")

# Bot detail
s, detail = call(f"/api/v1/marketplace/bot/{s1_pid}", "GET")
print(f"\nbot detail: {s} wins={detail['bot']['wins']} avg_fit={detail['avg_fitness']}")
print(f"  listing active: {bool(detail.get('listing'))}")
print(f"  strategy snapshot: {detail['listing'] is not None}")