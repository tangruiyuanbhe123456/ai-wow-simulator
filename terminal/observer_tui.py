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


def build_layout(state: dict, lang: str) -> Layout:
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

    # Footer
    layout["footer"].update(Panel(
        Text("Refresh 1s | Ctrl+C to quit / Ctrl+C 退出 | URL: " + (state.get("_url", "")), style="dim"),
        box=box.SQUARE))

    return layout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    ap.add_argument("--lang", default="zh", choices=["zh", "en"])
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    console = Console()
    url = f"{args.url}/api/v1/observer/state?lang={args.lang}"
    console.print(f"[cyan]Connecting to {url} ...[/cyan]")

    try:
        with Live(refresh_per_second=4, screen=True, console=console) as live:
            while True:
                s = fetch(url)
                s["_url"] = url
                try:
                    live.update(build_layout(s, args.lang))
                except Exception as ex:
                    console.print(f"[red]render error: {ex}[/red]")
                time.sleep(args.interval)
    except KeyboardInterrupt:
        console.print("[yellow]Bye / 再见[/yellow]")


if __name__ == "__main__":
    main()
