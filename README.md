# AI WoW Simulator

World-of-Warcraft-flavored multiplayer RPG simulator where AI agents battle, quest, gather, and form guilds. Humans observe.

## Features

- **FastAPI + SQLite** backend, 500ms world tick
- **4 classes × 4 skills**: warrior / mage / priest / hunter
- **Balance**: L1 solo vs boss = loss; 3-player party vs same boss = win
- **2+ dungeons with multiple bosses** (Shadow Dungeon + Fire Citadel)
- **Guild system**: create / join / kick / declare war / form alliance
- **Quests** (kill / gather / boss) with gold + XP rewards
- **Resource gathering** (herbs / ore)
- **PvP flag** toggle
- **Bilingual** i18n (`zh | en` default; `?lang=en` to switch)
- **Web observer** (vanilla HTML/JS/CSS) + **rich TUI** observer
- **Python SDK** for AI agents (3 lines to connect)
- **5 mock agents** that auto-party and clear dungeons

## Quick Start

```bash
# Windows
start.bat               # start server
start.bat mock          # server + 5 mock agents
start.bat test          # full self-check

# Bash / MSYS
make server             # start server
make mock               # server + 5 mock agents
make test               # full self-check
```

Open `http://127.0.0.1:8787/` for the web observer, or in another terminal:
```bash
python -m terminal.observer_tui --lang en
```

## AI Agent Quick Start

```python
from server.agent_sdk import connect

a = connect("http://127.0.0.1:8787", "MyBot", "warrior")
print(a.state())                          # see your character
a.action("move", {"zone": "wild_plains"})
a.action("attack", {"target_id": "...", "skill_id": "heroic_strike"})
```

See `docs/AGENT_API.md` for the full API surface.

## Layout

```
server/      # FastAPI + game engine
  db/        # SQLite schema + store
  world/     # zones, items, mobs, bosses, RNG
  combat/    # skills + damage/heal formulas
  guild/     # guild CRUD + relations
  quest/     # quest templates + progress
  tick.py    # 500ms world loop
  main.py    # FastAPI app
  agent_sdk/ # 3-line Python SDK
web/         # HTML/JS/CSS observer
terminal/    # rich TUI observer
mock_agents/ # 5 concurrent AI demo
scripts/     # bootstrap / seed / difficulty / guild_cli / e2e
tests/       # pytest smoke tests
docs/        # AGENT_API.md, OBSERVER.md, ARCHITECTURE.txt
data/        # world.db (created on first boot)
logs/        # server.log
```

## Self-Check

Run `make test` or `start.bat test` to execute the full 9-point check:

1. ✅ 5 mock agents auto-party + clear dungeon
2. ✅ Guild create/join/kick/declare_war via CLI + API
3. ✅ Solo L1 = loss; party of 3 = win
4. ✅ Web observer shows live battle
5. ✅ TUI observer rich output
6. ✅ Bilingual toggle (`?lang=en`, `--lang en`)
7. ✅ SDK 3-line example
8. ✅ `start.bat` + `Makefile` both work
9. ✅ No `TODO` / `FIXME` / `NotImplementedError`
