"""Mock AI agent brain: very simple policy that wins via party play."""
from __future__ import annotations
import random
import time
from typing import Any

from server.agent_sdk import WowAgent, connect
from server.config import BOSS_BASE_HP
from server.combat import list_skills_for_class


class SmartAgent:
    """Simple brain: gather xp -> quest -> form party -> kill boss -> repeat."""

    def __init__(self, base_url: str, name: str, cls: str):
        self.api = connect(base_url, name, cls)
        self.name = name
        self.cls = cls
        self.rng = random.Random(hash(name) & 0xFFFFFFFF)
        self.skills = list_skills_for_class(cls)
        self.damage_skill = next((s for s in self.skills if s in
            ("heroic_strike", "fireball", "frostbolt", "shadow_word_pain",
             "aimed_shot", "auto_shot", "cleave", "multi_shot")), self.skills[0])
        self.heal_skill = next((s for s in self.skills if "heal" in s or "prayer" in s or "holy" in s), None)

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
        for _ in range(max_ticks):
            if self.low_hp() and self.heal_skill:
                try:
                    self.api.action("heal", {"target_id": self.api.player_id,
                                              "skill_id": self.heal_skill})
                except Exception:
                    pass
            try:
                r = self.api.action("attack", {"target_id": target_id,
                                                "skill_id": self.damage_skill})
            except Exception as e:
                return {"stopped": True, "err": str(e)}
            if not r.get("ok"):
                # mob might be dead or target gone
                return {"stopped": True, "result": r}
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
