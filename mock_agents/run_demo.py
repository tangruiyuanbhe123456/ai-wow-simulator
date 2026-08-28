"""Spawn 5 mock AI agents that auto-party and clear the dungeon."""
from __future__ import annotations
import argparse
import threading
import time
import random
import sys

from mock_agents.agents import SmartAgent, find_party_members, boss_in_zone


def run_agent(idx: int, base_url: str, cls: str, results: list):
    name = f"Bot{idx}_{cls[0].upper()}"
    log = []
    try:
        a = SmartAgent(base_url, name, cls)
        log.append(("REG", a.api.player_id))

        # Phase 1: warm up in starter village
        a.move_to_better_zone("wild_plains")
        # Find a mob and kill it for some XP
        for tick in range(15):
            s = a.api.state()
            mobs = [m for m in s["mobs"] if m["kind"] == "mob"]
            if mobs:
                r = a.api.action("attack", {"target_id": mobs[0]["id"],
                                              "skill_id": a.damage_skill})
                log.append(("ATK", r.get("msg", "?")[:80]))
                if not r.get("ok"):
                    break
                time.sleep(0.5)
            if not a.alive():
                # respawn
                try:
                    a.api.action("respawn", {})
                except Exception:
                    pass
                break

        # Phase 2: guild creation (only first two)
        if idx in (0, 1):
            tag = f"G{idx}"
            r = a.try_create_guild(f"Guild{idx}", tag)
            log.append(("GLD", r))

        # Phase 3: party-up (all 5 converge on one party led by Bot0)
        if idx == 0:
            r = a.form_party()
            log.append(("PTY", r))
        time.sleep(0.3)

        # Phase 4: move everyone into shadow_dungeon
        try:
            a.api.action("move", {"zone": "shadow_dungeon"})
        except Exception:
            pass
        time.sleep(0.5)

        # Phase 5: Bot0 invites others
        if idx == 0:
            s = a.api.state()
            others = [p["id"] for p in s["players_here"]
                      if p["id"] != a.api.player_id and not p.get("party_id")]
            for pid in others[:4]:
                a.invite(pid)
                time.sleep(0.2)
            log.append(("INV", len(others)))

        time.sleep(0.5)

        # Phase 6: leader sets target on boss
        if idx == 0:
            boss = boss_in_zone(a.api)
            if boss:
                r = a.api.action("party_target", {"kind": "boss", "target_id": boss["id"]})
                log.append(("TGT", r))
                # Attack the boss
                for _ in range(40):
                    if not a.alive():
                        break
                    if a.low_hp() and a.heal_skill:
                        try:
                            a.api.action("heal", {"target_id": a.api.player_id,
                                                  "skill_id": a.heal_skill})
                        except Exception:
                            pass
                    try:
                        a.api.action("attack", {"target_id": boss["id"],
                                                 "skill_id": a.damage_skill})
                    except Exception as e:
                        log.append(("ATK_ERR", str(e)[:60]))
                        break
                    time.sleep(0.4)
                # Check if boss is dead
                s = a.api.state()
                boss_still = any(m["id"] == boss["id"] and m["hp"] > 0 for m in s["mobs"])
                log.append(("BOSS_DEAD", not boss_still))
            else:
                log.append(("NO_BOSS", True))
        else:
            # Other members also attack the same boss for 25 ticks
            s = a.api.state()
            boss = next((m for m in s["mobs"] if m["kind"] == "boss"), None)
            if boss:
                for _ in range(25):
                    if not a.alive():
                        try:
                            a.api.action("respawn", {})
                        except Exception:
                            pass
                        break
                    if a.low_hp() and a.heal_skill:
                        try:
                            a.api.action("heal", {"target_id": a.api.player_id,
                                                  "skill_id": a.heal_skill})
                        except Exception:
                            pass
                    try:
                        a.api.action("attack", {"target_id": boss["id"],
                                                 "skill_id": a.damage_skill})
                    except Exception:
                        break
                    time.sleep(0.4)

        # Phase 7: PvP / guild war (optional)
        time.sleep(0.3)
        # Leader declares war on the other guild if exists
        if idx == 0:
            try:
                r = a.api.action("guild_list", {})
                guilds = r.get("guilds", []) if r else []
                others = [g for g in guilds if g["tag"] != "G0"]
                if others:
                    a.try_declare_war(others[0]["id"])
                    log.append(("WAR", others[0]["name"]))
            except Exception as e:
                log.append(("WAR_ERR", str(e)[:60]))

        log.append(("DONE", True))
    except Exception as e:
        log.append(("FATAL", repr(e)))
    results[idx] = log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()

    classes = ["warrior", "mage", "priest", "hunter", "warrior"]
    results: list = [None] * args.n
    threads = []
    for i in range(args.n):
        cls = classes[i % len(classes)]
        t = threading.Thread(target=run_agent, args=(i, args.url, cls, results), daemon=True)
        threads.append(t)
        t.start()
        time.sleep(0.2)  # stagger starts so registration order is deterministic

    # Wait up to 90s for all
    deadline = time.time() + 90
    while time.time() < deadline and any(r is None for r in results):
        time.sleep(0.5)
    # Force join
    for t in threads:
        t.join(timeout=2)

    # Print summary
    print("=" * 60)
    print("MOCK AGENTS RUN COMPLETE / 5 个假 AI 跑完")
    print("=" * 60)
    for i, log in enumerate(results):
        if log is None:
            print(f"Bot{i}: TIMEOUT")
            continue
        print(f"\n--- Bot{i} log ---")
        for ev, msg in log:
            print(f"  {ev}: {str(msg)[:120]}")
    sys.exit(0)


if __name__ == "__main__":
    main()
