"""Bilingual catalog (zh / en)."""
from __future__ import annotations
from typing import Dict

ZH: Dict[str, str] = {
    "welcome": "欢迎来到艾泽拉斯, {name}! | Welcome to Azeroth, {name}!",
    "registered": "角色 {name} ({cls}) 已创建, 等级 {level}. | Character {name} ({cls}) created, level {level}.",
    "moved": "{name} 移动到 {zone}({x},{y}). | {name} moved to {zone}({x},{y}).",
    "attack": "{actor} 对 {target} 施放 [{skill}], 造成 {dmg} 伤害. | {actor} casts [{skill}] on {target} for {dmg} dmg.",
    "attack_crit": "暴击! {actor} 对 {target} 施放 [{skill}], 造成 {dmg} 伤害! | CRIT! {actor} [{skill}] on {target} for {dmg}!",
    "heal": "{actor} 对 {target} 施放 [{skill}], 治疗 {amt}. | {actor} [{skill}] on {target} heals {amt}.",
    "miss": "{actor} 的 [{skill}] 未命中 {target}. | {actor} [{skill}] misses {target}.",
    "death_player": "{name} 倒下了... | {name} has fallen...",
    "death_mob": "{name} 被击败! | {name} defeated!",
    "loot_drop": "{name} 掉落: {items}. | {name} drops: {items}.",
    "level_up": "{name} 升级! Lv {level}. | {name} levels up! Lv {level}.",
    "guild_create": "公会 [{tag}] {name} 由 {leader} 创建. | Guild [{tag}] {name} founded by {leader}.",
    "guild_join": "{player} 加入公会 {guild}. | {player} joins {guild}.",
    "guild_kick": "{player} 被踢出公会 {guild}. | {player} kicked from {guild}.",
    "guild_war": "公会 {a} 向 {b} 宣战! | Guild {a} declares war on {b}!",
    "guild_ally": "公会 {a} 与 {b} 结盟. | Guild {a} allies with {b}.",
    "party_invite": "{leader} 邀请 {p} 加入队伍. | {leader} invites {p} to party.",
    "party_join": "{p} 加入队伍 (成员: {members}). | {p} joins party (members: {members}).",
    "dungeon_enter": "{party} 进入副本 [{dungeon}] 房间 [{room}]. | {party} enters dungeon [{dungeon}] room [{room}].",
    "boss_defeated": "首领 {boss} 被 {party} 击败! 战利品: {loot}. | Boss {boss} slain by {party}! Loot: {loot}.",
    "quest_accept": "{p} 接受任务 [{quest}]. | {p} accepts quest [{quest}].",
    "quest_complete": "{p} 完成任务 [{quest}], 奖励: +{gold}g +{xp}xp. | {p} completes [{quest}], reward +{gold}g +{xp}xp.",
    "gather": "{p} 在 [{node}] 采集获得 [{item}] x{qty}. | {p} gathers [{item}] x{qty} from [{node}].",
    "chat_world": "[世界] {sender}: {body} | [World] {sender}: {body}",
    "chat_party": "[队伍] {sender}: {body} | [Party] {sender}: {body}",
    "chat_guild": "[公会] {sender}: {body} | [Guild] {sender}: {body}",
    "err_no_target": "目标不存在. | Target not found.",
    "err_dead": "你已死亡. | You are dead.",
    "err_oob": "坐标越界. | Out of bounds.",
    "err_no_skill": "没有该技能. | No such skill.",
    "err_no_mp": "法力不足. | Not enough mana.",
    "err_not_leader": "只有队长能操作. | Only leader can do that.",
    "err_in_party": "已在队伍中. | Already in party.",
    "err_not_in_party": "未在队伍中. | Not in party.",
    "victory": "胜利! 副本通关. | Victory! Dungeon cleared.",
    "defeat": "失败... 副本重置. | Defeat... dungeon reset.",
}

EN: Dict[str, str] = {k: v.split("|", 1)[1].strip() if "|" in v else v for k, v in ZH.items()}
ZH_ONLY: Dict[str, str] = {k: v.split("|", 1)[0].strip() for k, v in ZH.items()}


def t(key: str, lang: str = "zh", **kwargs) -> str:
    """Get a translated string. If lang='zh', returns the bilingual 'zh | en' form by default.
    Use lang='zh_only' to get pure zh; lang='en' to get pure en."""
    if key not in ZH:
        return key
    if lang == "en":
        raw = EN.get(key, key)
    elif lang == "zh_only":
        raw = ZH_ONLY.get(key, key)
    else:
        raw = ZH.get(key, key)  # default: bilingual
    try:
        return raw.format(**kwargs)
    except (KeyError, IndexError):
        return raw


def name(name_zh: str, name_en: str, lang: str = "zh") -> str:
    """Entity name lookup helper. lang='zh' returns 'zh | en'; otherwise just that lang."""
    if lang == "en":
        return name_en
    return f"{name_zh} | {name_en}"
