"""Difficulty check: simulate 1v1 boss and 3v1 boss fights.

Self-check #3: solo L1 player MUST LOSE; party of 3 MUST WIN.

Uses server.combat.perform_attack directly with explicit party_size simulation.
"""
from __future__ import annotations
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Reproducible
random.seed(42)

from server.db import connect, init_schema
from server.world import spawn_world_mobs, level_to_hp, level_to_atk, gen_id, xp_to_next
from server.combat import perform_attack, SKILLS


def reset_world():
    """Wipe + reseed for clean slate. Caller owns the returned connection."""
    import gc, sqlite3
    # close any existing sqlite handles via gc
    gc.collect()
    # try to delete; on WinError 32 just truncate via a fresh connection
    for p in list(Path("data").glob("world.db*")):
        try:
            p.unlink()
        except PermissionError:
            pass
    conn = connect()
    init_schema(conn)
    spawn_world_mobs(conn)
    return conn


def make_player(conn, name: str, cls: str, level: int = 1) -> str:
    cur = conn.cursor()
    pid = gen_id("p")
    mp_max = 60 + level * 10  # matches server.main L1 baseline + per-level scaling
    cur.execute("""INSERT INTO players (id,name,cls,level,xp,hp,hp_max,mp,mp_max,atk,defn,
                   zone,pos_x,pos_y,gold,created_at,last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, name, cls, level, 0, level_to_hp(level), level_to_hp(level),
                 mp_max, mp_max, level_to_atk(level), 2, "starter_village", 0, 0, 0, time.time(), time.time()))
    conn.commit()
    return pid


def get_boss(conn, zone: str = "shadow_dungeon") -> dict:
    cur = conn.cursor()
    cur.execute("SELECT * FROM mobs WHERE zone=? AND kind='boss' AND alive=1 LIMIT 1", (zone,))
    return dict(cur.fetchone())


def get_player(conn, pid: str) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE id=?", (pid,))
    return dict(cur.fetchone())


def simulate_fight(conn, players: list, boss: dict, max_ticks: int = 200) -> dict:
    """Alternate attacks/heals. Boss attacks lowest-hp player each tick.

    Each player attacks every tick with damage_skill. When any player HP<35%
    of max, ALL healers cast heal (targeting lowest-HP ally).
    """
    cur = conn.cursor()
    bid = boss["id"]
    # Damage skill per class
    skill_map = {"warrior": "heroic_strike", "mage": "fireball",
                 "priest": "shadow_word_pain", "hunter": "auto_shot"}
    # Heal skill per class
    heal_map = {"priest": "holy_light"}

    for tick in range(max_ticks):
        # Regen mp/hp via server.tick (matches real gameplay)
        from server.tick import tick as do_tick
        do_tick(conn)

        # Check players' HP to decide heal-up first
        anyone_low = False
        for pl in players:
            cur.execute("SELECT hp, hp_max FROM players WHERE id=?", (pl["id"],))
            row = cur.fetchone()
            if row and row["hp"] > 0 and row["hp"] < row["hp_max"] * 0.4:
                anyone_low = True; break

        # Healer acts first if anyone low
        if anyone_low:
            for pl in players:
                if pl["cls"] in heal_map:
                    cur.execute("SELECT id FROM players WHERE id IN ({}) ORDER BY hp ASC LIMIT 1"
                                .format(",".join("?" * len(players))), [p["id"] for p in players])
                    tgt = cur.fetchone()
                    if tgt:
                        perform_attack(conn, pl["id"], tgt["id"], heal_map[pl["cls"]], "zh")

        # Players attack
        for pl in players:
            sk = skill_map.get(pl["cls"], "heroic_strike")
            perform_attack(conn, pl["id"], bid, sk, "zh")

        # Check boss
        cur.execute("SELECT hp, alive FROM mobs WHERE id=?", (bid,))
        brow = cur.fetchone()
        if brow and (brow["hp"] <= 0 or brow["alive"] == 0):
            return {"won": True, "ticks": tick + 1, "reason": "boss_dead"}

        # Check players alive
        alive_pids = []
        for pl in players:
            cur.execute("SELECT hp FROM players WHERE id=?", (pl["id"],))
            r = cur.fetchone()
            if r and r["hp"] > 0:
                alive_pids.append(pl["id"])
        if not alive_pids:
            return {"won": False, "ticks": tick + 1, "reason": "all_dead"}

        # Boss attacks lowest-hp player
        placeholders = ",".join("?" * len(alive_pids))
        cur.execute(f"SELECT id, hp FROM players WHERE id IN ({placeholders}) ORDER BY hp ASC LIMIT 1",
                    alive_pids)
        tgt = cur.fetchone()
        if tgt and brow and brow["hp"] > 0:
            from server.combat import base_attack_damage
            dmg = base_attack_damage(boss["level"], boss["atk"])
            cur.execute("UPDATE players SET hp=MAX(0, hp - ?) WHERE id=?", (dmg, tgt["id"]))

    return {"won": False, "ticks": max_ticks, "reason": "timeout"}


def main() -> int:
    conn = reset_world()

    # L1 warrior solo vs boss
    solo_id = make_player(conn, "SoloWar", "warrior", 1)
    boss = get_boss(conn, "shadow_dungeon")
    print(f"[diff] solo L1 warrior vs boss '{boss['name']}' (lvl {boss['level']}, hp {boss['hp']}/{boss['hp_max']}, atk {boss['atk']})")
    r = simulate_fight(conn, [{"id": solo_id, "cls": "warrior"}], boss, max_ticks=60)
    solo_won = r["won"]
    print(f"[diff]   → {'WIN' if solo_won else 'LOSE'} (ticks={r['ticks']}, reason={r['reason']})")
    assert not solo_won, "FAIL: solo L1 should not be able to beat a boss"

    # Reset world for party test
    conn = reset_world()
    p1 = make_player(conn, "P1War", "warrior", 1)
    p2 = make_player(conn, "P2Mag", "mage", 1)
    p3 = make_player(conn, "P3Pri", "priest", 1)
    # Make them a party
    cur = conn.cursor()
    party_id = gen_id("party")
    cur.execute("INSERT INTO parties (id,leader_id,zone,created_at) VALUES (?,?,?,?)",
                (party_id, p1, "shadow_dungeon", time.time()))
    for pid in (p1, p2, p3):
        cur.execute("INSERT INTO party_members (party_id,player_id,joined_at) VALUES (?,?,?)",
                    (party_id, pid, time.time()))
        cur.execute("UPDATE players SET party_id=? WHERE id=?", (party_id, pid))
    conn.commit()
    print(f"[diff] party of 3 (war/mage/priest L1) vs same boss")
    boss = get_boss(conn, "shadow_dungeon")
    r = simulate_fight(conn, [{"id": p1, "cls": "warrior"}, {"id": p2, "cls": "mage"}, {"id": p3, "cls": "priest"}],
                       boss, max_ticks=120)
    party_won = r["won"]
    print(f"[diff]   → {'WIN' if party_won else 'LOSE'} (ticks={r['ticks']}, reason={r['reason']})")
    assert party_won, "FAIL: party of 3 should beat the boss"

    print("\n[diff] PASS: solo L1 = loss, party of 3 = win")
    return 0


if __name__ == "__main__":
    sys.exit(main())
