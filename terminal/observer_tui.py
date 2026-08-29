#!/usr/bin/env python
"""AI WoW Simulator — Terminal observer (rich TUI).
Polls /api/v1/observer/state and shows a live scrolling dashboard.

Usage:
  python -m terminal.observer_tui --lang en
  python -m terminal.observer_tui --url http://127.0.0.1:8787 --lang zh
"""
from __future__ import annotations
import argparse
import json
import time
import sys
import urllib.request

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich import box


def fetch(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def fetch_match(base_url: str, match_id: str, lang: str = "zh") -> dict:
    """Fetch detailed arena match state."""
    return fetch(f"{base_url}/api/v1/arena/match/{match_id}?lang={lang}")


def build_arena_panel(state: dict, base_url: str, lang: str) -> Panel:
    """Render a panel showing all active 5v5 arena matches.

    Shows match summary + dragons + towers + team buffs + (if exactly 1 match)
    live battle events.
    """
    is_en = (lang == "en")
    matches = state.get("arena_matches", []) or []
    ql = state.get("arena_queue_len", 0)

    # Match summary table
    table = Table(expand=True, box=box.SIMPLE, show_header=True, header_style="bold cyan")
    table.add_column("Match", width=20, style="white")
    table.add_column("Tick", width=5, justify="right", style="cyan")
    table.add_column("B♥", width=6, justify="right", style="cyan")
    table.add_column("B⚔", width=7, justify="right", style="red")
    table.add_column("R♥", width=6, justify="right", style="red")
    table.add_column("R⚔", width=7, justify="right", style="red")
    table.add_column("Status", width=12, style="yellow")

    if not matches:
        table.add_row(
            ("(no active matches)" if is_en else "(无活跃比赛)"),
            "-", "-", "-", "-", "-", f"Q:{ql}/10",
        )
    else:
        for m in matches:
            status = (f"🏆 {m['winner']}" if m["ended"] and m.get("winner")
                      else f"t={m['tick']}")
            table.add_row(
                m["match_id"][:18],
                str(m["tick"]),
                str(m["blue_crystal_hp"]),
                f"{m['blue_alive']}/5 ⚔{m['blue_kills']}",
                str(m["red_crystal_hp"]),
                f"{m['red_alive']}/5 ⚔{m['red_kills']}",
                status,
            )

    # If exactly 1 match is active, fetch detail and show towers + dragons + buffs + events
    detail_text = Text()
    if len(matches) == 1 and base_url:
        m = matches[0]
        detail = fetch_match(base_url, m["match_id"], lang)
        if detail.get("ok"):
            label = (f"[live events for {m['match_id']}]"
                     if is_en else f"[实时事件流 - {m['match_id']}]")
            detail_text.append("\n" + label + "\n", style="bold magenta")
            # Towers section
            towers = detail.get("towers") or []
            if towers:
                detail_text.append(
                    ("[towers]\n" if is_en else "[防御塔]\n"), style="bold yellow")
                for t in towers:
                    icon = "💥" if t["hp"] <= 0 else ("🗼" if t["kind"] == "outer" else "🏯")
                    lane_zh = {"top": "上", "mid": "中", "bot": "下"}[t["lane"]]
                    team_label = "B" if t["team"] == "blue" else "R"
                    line = (f"  {icon} {team_label}/{lane_zh}{t['kind'][0].upper()} "
                            f"{t['hp']:>3}/{t['hp_max']}\n")
                    style = ("red" if t["team"] == "blue" else "cyan")
                    if t["hp"] <= 0:
                        style = "dim"
                    detail_text.append(line, style=style)
            # Dragons + buffs
            dragons = detail.get("dragons") or []
            if dragons:
                detail_text.append(
                    ("[dragons]\n" if is_en else "[中立龙]\n"), style="bold yellow")
                for d in dragons:
                    detail_text.append(f"  🐉 {d['kind']}: {d['hp']}/{d['hp_max']}\n",
                                       style="yellow")
            buffs = detail.get("buffs") or {}
            if buffs:
                detail_text.append(
                    ("[buffs]\n" if is_en else "[团队 buff]\n"), style="bold yellow")
                for team, b in buffs.items():
                    color = "cyan" if team == "blue" else "red"
                    detail_text.append(
                        f"  ⚡ {team.upper()}: +{int(b['dmg_pct']*100)}% dmg "
                        f"({b['source']}, expires t={b['expires_at']})\n",
                        style=color,
                    )
            # Last events
            detail_text.append(
                ("\n[events]\n" if is_en else "\n[事件]\n"), style="bold magenta")
            for evt in (detail.get("log") or [])[-6:]:
                msg = evt.get("msg", "")[:80]
                if "击杀" in msg or "killed" in msg:
                    style = "red"
                elif "水晶" in msg or "crystal" in msg:
                    style = "yellow"
                elif "复活" in msg or "respawn" in msg:
                    style = "cyan"
                elif "推掉" in msg or "destroyed" in msg or "🐉" in msg:
                    style = "magenta"
                else:
                    style = "white"
                detail_text.append(f"  t={evt['tick']} {msg}\n", style=style)

    combo = Table.grid(padding=(0, 1))
    combo.add_row(table)
    if detail_text.plain:
        combo.add_row(detail_text)

    title = "5v5 Arena / 王者战场 (Q:" + str(ql) + "/10)"
    return Panel(combo, title=title, border_style="magenta", box=box.SIMPLE)


def build_layout(state: dict, lang: str, base_url: str = "") -> Layout:
    """Compose a rich Layout. Bilingual labels."""
    is_en = (lang == "en")
    title = "AI WoW Observer / AI 魔兽世界观战台"

    layout = Layout(name="root")
    layout.split(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )
    layout["left"].split(
        Layout(name="stats", size=7),
        Layout(name="players"),
        Layout(name="arena", size=14),
    )
    layout["right"].split(
        Layout(name="guilds", size=10),
        Layout(name="bosses", size=8),
        Layout(name="combat"),
    )

    # Header
    header = Text()
    header.append(title, style="bold yellow")
    header.append("   ")
    header.append(f"[{time.strftime('%H:%M:%S')}] ", style="dim")
    header.append(("Lang: " + lang), style="cyan")
    layout["header"].update(Panel(header, box=box.DOUBLE))

    # Stats
    if state.get("ok"):
        stats = Table.grid(padding=(0, 1))
        stats.add_column(style="cyan", justify="right")
        stats.add_column(style="white")
        stats.add_row("Players" if is_en else "玩家", f"{state.get('players_alive',0)}/{state.get('players_total',0)}")
        stats.add_row("Mobs" if is_en else "怪物", str(state.get('mobs_alive', 0)))
        stats.add_row("Guilds" if is_en else "公会", str(state.get('guilds', 0)))
        stats.add_row("Arena Q" if is_en else "匹配队列", str(state.get('arena_queue_len', 0)) + "/10")
        layout["stats"].update(Panel(stats, title="Stats / 世界", border_style="cyan"))
    else:
        layout["stats"].update(Panel(f"ERR: {state.get('error','?')}", title="Stats", border_style="red"))

    # Players
    pt = Table(expand=True, box=box.SIMPLE)
    pt.add_column("#", width=3, style="yellow")
    pt.add_column("Name" if is_en else "角色", style="white")
    pt.add_column("Cls" if is_en else "职业", width=8, style="cyan")
    pt.add_column("Lv", width=3, style="magenta", justify="right")
    pt.add_column("HP", width=12, style="red")
    pt.add_column("Zone" if is_en else "区域", style="green")
    for i, p in enumerate(state.get("top_players", []) or []):
        hp = f"{p['hp']}/{p['hp_max']}"
        pt.add_row(str(i+1), p["name"], p["cls"], str(p["level"]), hp, p["zone"])
    layout["players"].update(Panel(pt, title="Top / 排行", border_style="yellow"))

    # Guilds
    gt = Table(expand=True, box=box.SIMPLE)
    gt.add_column("Tag", width=6, style="bold yellow")
    gt.add_column("Name" if is_en else "公会", style="white")
    gt.add_column("#" if is_en else "成员", width=4, justify="right")
    for g in state.get("guilds_list", []) or []:
        gt.add_row(g["tag"], g["name"], str(g["members"]))
    layout["guilds"].update(Panel(gt, title="Guilds / 公会", border_style="magenta"))

    # Bosses
    bt = Table(expand=True, box=box.SIMPLE)
    bt.add_column("Dungeon" if is_en else "副本", style="white")
    bt.add_column("Alive" if is_en else "存活", justify="right")
    bz = state.get("boss_zones", {}) or {}
    for zid in ["shadow_dungeon", "fire_citadel"]:
        nm = "Shadow Dungeon" if is_en else "暗影副本 / Shadow Dungeon"
        if zid == "fire_citadel":
            nm = "Fire Citadel" if is_en else "火焰堡垒 / Fire Citadel"
        bt.add_row(nm, str(bz.get(zid, 0)))
    layout["bosses"].update(Panel(bt, title="Bosses / 首领", border_style="red"))

    # Combat log
    log_text = Text()
    for l in (state.get("combat_log", []) or [])[-12:]:
        ts = time.strftime("%H:%M:%S", time.localtime(l["ts"]))
        detail = l.get("detail") or f"{l.get('actor_name','')} -> {l.get('action','')} -> {l.get('target_name','')}"
        log_text.append(f"[{ts}] ", style="dim")
        log_text.append(detail + "\n")
    layout["combat"].update(Panel(log_text, title="Combat / 战报", border_style="green"))

    # Arena 5v5 panel
    layout["arena"].update(build_arena_panel(state, base_url, lang))

    # Footer
    layout["footer"].update(Panel(
        Text(f"Refresh 1s | Ctrl+C to quit / 退出 | URL: {state.get('_url','')} | Arena: /arena.html", style="dim"),
        box=box.SQUARE))

    return layout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    ap.add_argument("--lang", default="zh", choices=["zh", "en"])
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    console = Console()
    base_url = args.url
    url = f"{base_url}/api/v1/observer/state?lang={args.lang}"
    console.print(f"[cyan]Connecting to {url} ...[/cyan]")

    try:
        with Live(refresh_per_second=2, screen=True, console=console) as live:
            while True:
                s = fetch(url)
                s["_url"] = url
                try:
                    live.update(build_layout(s, args.lang, base_url))
                except Exception as ex:
                    console.print(f"[red]render error: {ex}[/red]")
                time.sleep(args.interval)
    except KeyboardInterrupt:
        console.print("[yellow]Bye / 再见[/yellow]")


if __name__ == "__main__":
    main()