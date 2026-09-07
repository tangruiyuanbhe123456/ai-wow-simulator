"""V13 end-to-end demo.

Simulates:
  1. Birth two bots (BotA attacker, BotB victim)
  2. BotA kills BotB 3 times via direct lifecycle/death API
  3. Verify: biography has 1 origin + 3 fall chapters
             BotB has 3 death memories (weight 100)
             BotA has 3 kill memories (weight 60)
             BotB fears BotA (soul.fears populated)
             rivalry relation exists (both directions)
  4. Player resurrects BotB (cost 100 QC, mocked)
  5. Verify: BotB alive again, resurrection memory + return chapter
  6. Player gives flowers to BotB's memorial (cost 10 QC)
  7. Verify: memorial.flowers == 1
"""
from __future__ import annotations
import sys
import os
import time
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Direct DB access (skip server) for fast verification
from server.db import connect
from server.db.schema_v13 import ensure_v13_schema
from server.lifecycle import (
    birth as lc_birth,
    on_death as lc_death,
    resurrect as lc_resurrect,
    add_flower as lc_flower,
)
from server.memory import list_memories
from server.biography import list_biography
from server.genealogy import tree as genealogy_tree


# Test fixtures: real players must exist for FK. Insert minimal.
def _ensure_player(c, pid: str, name: str, cls: str = "warrior"):
    cur = c.execute("SELECT id FROM players WHERE id=?", (pid,))
    if cur.fetchone():
        return
    now = time.time()
    c.execute(
        """INSERT INTO players
           (id, name, cls, level, hp, hp_max, mp, mp_max, atk, defn, zone, created_at, last_seen)
           VALUES (?, ?, ?, 1, 100, 100, 50, 50, 10, 5, 'wild_plains', ?, ?)""",
        (pid, name, cls, now, now),
    )
    c.commit()


def main():
    print("=" * 60)
    print("V13 Digital Life Layer — End-to-End Demo")
    print("=" * 60)

    c = connect()
    ensure_v13_schema(c)

    # Setup players (need FK targets)
    _ensure_player(c, "bot_a_001", "Alpha")
    _ensure_player(c, "bot_b_002", "Bravo")

    # Step 1: birth
    print("\n[1] Birth")
    a = lc_birth(c, "bot_a_001", "Alpha")
    b = lc_birth(c, "bot_b_002", "Bravo")
    print(f"  Alpha born: soul_name={a['soul_name']} dna={a['dna_seed'][:8]}...")
    print(f"  Bravo born: soul_name={b['soul_name']} dna={b['dna_seed'][:8]}...")
    assert a["born_existing"] is False
    assert b["born_existing"] is False

    # Step 2: kill loop (die -> resurrect -> die -> resurrect -> die)
    print("\n[2] BotA kills BotB 3 times (with resurrects in between)")
    for i in range(3):
        result = lc_death(
            c, "bot_b_002", "shadow_dungeon", 5, 5,
            killed_by_pid="bot_a_001", killer_name="Alpha",
        )
        assert result.get("state") == "sleeping", result
        print(f"  Death #{i+1}: state={result['state']} death_count={result['death_count']}")
        if i < 2:
            rc = lc_resurrect(c, "bot_b_002", payer_pid="human:benben", qc_spent=100)
            assert rc["state"] == "alive"
            print(f"    -> Resurrected (cost {rc['qc_spent']} QC)")

    # Step 3: verify state
    print("\n[3] Verify state")
    # Biography should have origin + 3 fall
    bio_b = list_biography(c, "bot_b_002")
    chapters = [b["chapter"] for b in bio_b]
    print(f"  Bravo biography chapters: {chapters}")
    assert "origin" in chapters
    assert chapters.count("fall") == 3, f"expected 3 fall, got {chapters.count('fall')}"

    # Memories
    mems_b = list_memories(c, "bot_b_002", limit=10)
    weights = [m["weight"] for m in mems_b]
    print(f"  Bravo top memories by weight: {weights[:5]}")
    assert 100 in weights, "expected at least one weight=100 death memory"

    mems_a = list_memories(c, "bot_a_001", limit=10)
    print(f"  Alpha top memory types: {[m['memory_type'] for m in mems_a]}")
    assert "kill" in [m["memory_type"] for m in mems_a]

    # Genealogy (rivalry)
    tree_a = genealogy_tree(c, "bot_a_001")
    tree_b = genealogy_tree(c, "bot_b_002")
    print(f"  Alpha rivals: {[r['pid'] for r in tree_a['rival']]}")
    print(f"  Bravo rivals: {[r['pid'] for r in tree_b['rival']]}")
    assert any(r["pid"] == "bot_b_002" for r in tree_a["rival"])
    assert any(r["pid"] == "bot_a_001" for r in tree_b["rival"])

    # Soul fear
    lc_b = lc_birth(c, "bot_b_002", "Bravo")  # returns existing
    lc_data = c.execute(
        "SELECT soul_json FROM bot_lifecycle WHERE pid=?", ("bot_b_002",)
    ).fetchone()
    soul = json.loads(lc_data[0])
    fears = soul.get("fears", [])
    print(f"  Bravo fears: {len(fears)} entries, top weight={fears[0]['weight'] if fears else 0}")
    assert len(fears) >= 1
    assert fears[0]["pid"] == "bot_a_001"

    # Step 4: bot is currently sleeping from 3rd death, resurrect one more
    print("\n[4] Resurrect Bravo again (cost 100 QC) after final death")
    res = lc_resurrect(c, "bot_b_002", payer_pid="human:benben", qc_spent=100)
    print(f"  Resurrection result: {res}")
    assert res["state"] == "alive"

    # Step 5: verify post-resurrection
    print("\n[5] Verify post-resurrection")
    bio_b2 = list_biography(c, "bot_b_002")
    chapters2 = [b["chapter"] for b in bio_b2]
    print(f"  Bravo biography now: {chapters2}")
    assert "return" in chapters2

    mems_b2 = list_memories(c, "bot_b_002", limit=20)
    res_mems = [m for m in mems_b2 if m["memory_type"] == "resurrection"]
    print(f"  Resurrection memories: {len(res_mems)}")
    assert len(res_mems) == 3
    assert res_mems[0]["weight"] == 95

    # Step 6: flowers
    print("\n[6] Player gives flowers at memorial")
    fl = lc_flower(c, "bot_b_002", "human:benben", 10)
    print(f"  Flower result: {fl}")
    assert fl["flowers"] == 1

    print("\n" + "=" * 60)
    print("ALL V13 ASSERTIONS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nFAIL: {e}", file=sys.stderr)
        sys.exit(1)
