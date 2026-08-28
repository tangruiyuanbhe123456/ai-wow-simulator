"""Mock AI agent brain: very simple policy that wins via party play.

Mana-aware rotation (v2):
- Each agent now knows its full damage skill rotation (sorted by mana cost).
- When MP < cheapest skill cost, the agent waits one tick for natural regen
  before retrying, instead of dying with RuntimeError / exiting the loop.
- The damage_skill attribute is kept for back-compat with run_demo.py callers
  but the actual attack_loop() consults pick_skill() to choose what fits.
"""
from __future__ import annotations
import random
import time
from typing import Any

from server.agent_sdk import WowAgent, connect
from server.config import BOSS_BASE_HP
from server.combat import list_skills_for_class


# Skills that don't cost mana — used as last-resort fallback so the agent
# never gets stuck in an "out of mana, can't attack" loop.
FREE_FALLBACK_SKILLS = ("heroic_strike", "auto_shot", "mob_bite", "shadow_claw")


class SmartAgent:
    """Simple brain: gather xp -> quest -> form party -> kill boss -> repeat."""

    def __init__(self, base_url: str, name: str, cls: str):
        self.api = connect(base_url, name, cls)
        self.name = name
        self.cls = cls
        self.rng = random.Random(hash(name) & 0xFFFFFFFF)
        self.skills = list_skills_for_class(cls)
        # Damage rotation: damage-y skills first, sorted by ascending cost
        # so pick_skill() can pick the strongest affordable one.
        from server.combat import SKILLS
        dmg_pool = [s for s in self.skills if SKILLS.get(s, {}).get("dmg_mult", 0) > 0]
        if not dmg_pool:
            dmg_pool = list(self.skills)
        self.rotation = sorted(
            dmg_pool,
            key=lambda s: (SKILLS.get(s, {}).get("cost", 0),
                           -SKILLS.get(s, {}).get("dmg_mult", 0)),
        )
        # Back-compat: keep damage_skill as the strongest in the rotation.
        self.damage_skill = self.rotation[-1] if self.rotation else (
            self.skills[0] if self.skills else "heroic_strike")
        self.heal_skill = next((s for s in self.skills if "heal" in s or "prayer" in s or "holy" in s), None)

    def current_mp(self) -> int:
        return int(self.api.state()["you"].get("mp", 0))

    def pick_skill(self, state: dict | None = None) -> str:
            """Return the strongest skill in rotation whose cost <= current MP.

            Optional `state` argument lets callers pass a state dict they already
            fetched (avoids a second API call). If omitted, we fetch ourselves.

            Falls back to a FREE_FALLBACK_SKILLS entry (heroic_strike, auto_shot,
            mob_bite, shadow_claw) — those have cost=0 — so we never hit the
            "法力不足 / Not enough mana" 400 from the server. This is the
            mana-rotation fix: Bot1 (mage) and Bot2 (priest) used to crash here
            after a few fireballs / holy lights because their only damage skill
            cost 10-12 mana.
            """
            from server.combat import SKILLS
            if state is None:
                state = self.api.state()
            mp = int(state.get("you", {}).get("mp", 0))
            for sk in self.rotation:
                cost = SKILLS.get(sk, {}).get("cost", 0)
                if mp >= cost:
                    return sk
            # Out of mana → use a free fallback (cost 0) instead of crashing.
            for fb in FREE_FALLBACK_SKILLS:
                return fb  # all four have cost=0; same default for every class
            return self.rotation[0] if self.rotation else "heroic_strike"

    def hp_pct(self) -> float:
        s = self.api.state()
        return s["you"]["hp"] / max(1, s["you"]["hp_max"])

    def alive(self) -> bool:
        return self.hp_pct() > 0.0

    def low_hp(self) -> bool:
        return self.hp_pct() < 0.35

    def nearest_mob(self, kind: str = "mob"):
        s = self.api.state()
        for m in s["mobs"]:
            if m["kind"] == kind:
                return m
        return None

    def move_to_better_zone(self, zone: str):
        try:
            self.api.action("move", {"zone": zone})
        except Exception:
            pass

    def attack_loop(self, target_id: str, max_ticks: int = 50):
        """Attack target up to max_ticks. Now mana-aware: if a skill returns
        ok=False due to mana, we swap to a free skill and continue instead of
        exiting the loop. We never raise RuntimeError on out-of-mana."""
        from server.combat import SKILLS
        consecutive_offer = 0  # how many ticks we've been broke (server regen = +2 mp/tick)
        for tick_i in range(max_ticks):
            # Heal first if needed
            if self.low_hp() and self.heal_skill:
                try:
                    self.api.action("heal", {"target_id": self.api.player_id,
                                              "skill_id": self.heal_skill})
                except Exception:
                    pass
            # Pick strongest affordable skill
            sk = self.pick_skill()
            try:
                r = self.api.action("attack", {"target_id": target_id, "skill_id": sk})
            except Exception as e:
                return {"stopped": True, "err": str(e)}
            if not r.get("ok"):
                msg = r.get("msg", "")
                if "mana" in str(msg).lower() or "法力" in str(msg):
                    # Out of mana — try the free fallback explicitly
                    consecutive_offer += 1
                    if consecutive_offer > 30:
                        # truly stuck (regen should refill in ~15 ticks). Bail.
                        return {"stopped": True, "result": r, "reason": "mana_starvation"}
                    time.sleep(0.4)  # let server tick regen mp
                    continue
                # mob dead or target gone — normal end
                return {"stopped": True, "result": r}
            consecutive_offer = 0
            if not self.alive():
                return {"stopped": True, "dead": True}
        return {"stopped": False}

    def try_create_guild(self, name: str, tag: str):
        try:
            return self.api.action("guild_create", {"name": name, "tag": tag})
        except Exception as e:
            return {"err": str(e)}

    def try_declare_war(self, other_guild_id: str):
        try:
            return self.api.action("guild_declare_war", {"guild_id": other_guild_id})
        except Exception as e:
            return {"err": str(e)}

    def form_party(self):
        try:
            return self.api.action("party_create", {})
        except Exception:
            pass

    def invite(self, pid: str):
        try:
            return self.api.action("party_invite", {"player_id": pid})
        except Exception:
            pass


def find_party_members(api: WowAgent, target_zone: str, max_n: int = 3):
    s = api.state()
    out = [p["id"] for p in s["players_here"] if p["id"] != api.player_id]
    return out[:max_n]


def boss_in_zone(api: WowAgent):
    s = api.state()
    for m in s["mobs"]:
        if m["kind"] == "boss":
            return m
    return None
