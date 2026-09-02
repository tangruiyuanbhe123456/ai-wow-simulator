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

## Version History

- **v3** — base world, 4 classes, 2 dungeons, guilds, quests, gather
- **v4** — ban/pick draft, summoner spells, equipment trade, 5v5 replay, leaderboard
- **v5** — match rooms (1v1 / 3v3 / 5v5 + lobby + auto-start draft)
- **v6** — bot strategy layer (5-rule decision tree)
- **v7** — AI training center (fitness + strategy evolution) + equipment depth
- **v8** — human-vs-bot mode + tournament system + equipment trade UI
- **v9** — self-play training + tournament advancement + set bonuses + RTMP streaming
- **v9.5** — bot marketplace (credits + buy/sell + strategy snapshot)
- **v10** — DLC expansion (3 heroes + 7 items + 2 events) + AI auto-pricing
- **v11** — bot self-upgrade endpoint + tournament auto-trigger + PayPal integration
- **v12** — **bot skill tree** (3 evolution branches + AI auto-evolve) + DB lock resilience

## v12 — Bot Skill Tree

Each bot picks one of 3 evolution branches and levels up via match play:

| Branch | Tagline | Best for |
|---|---|---|
| `tank` (Guardian) | HP max 5→25%, shield, self-heal | win-rate < 40% |
| `berserk` | ATK +5→25%, crit, lifesteal, ult CD -10t | moderate WR with high K/D |
| `strategist` | ult CD -3→-15t, spell slot, vision, double-ult | win-rate > 60% |

Each match awards **+20 skill_points on win, +8 on loss**. Levels 1..5 cost
`(level * 30)` points. Switching branches costs 50 points. The AI suggestion
in `GET /skill-tree/suggest` is based on the bot's recent win rate + fitness
trend; `POST /skill-tree/auto-evolve` accepts it idempotently.

API (under `/api/v1/bot/{pid}/skill-tree`):

```
GET  /skill-tree             full state + AI suggestion + evolution log
GET  /skill-tree/suggest     heuristic suggestion (no side effects)
POST /skill-tree/choose      pick or switch (force=true bypasses cost)
POST /skill-tree/auto-evolve AI auto-picks if no branch yet
```

UI: `web/skill_tree.html` (basic page shell; full interactive JS to follow).

### Quick Start: see AI auto-evolve in 90 seconds

```bash
# Terminal 1: server (already running on 127.0.0.1:8787)
python -m server.main

# Terminal 2: end-to-end demo
python scripts/demo_v12.py
```

Output (verified):

```
[4] AI Auto-Evolve
    BotA (e53bc948):
      -> branch: strategist
      reason_zh: 胜率 100% 高,建议谋士系强化技能节奏
      reason_en: Win rate 100% is high -- Strategist doubles down on utility
    BotB (7c292117):
      -> branch: tank
      reason_zh: 胜率 0% 偏低,建议转向守护系抗压
      reason_en: Win rate 0% is low -- Guardian path helps survive

[5] Verify Branch Picks
    BotA: got='strategist' expected='strategist' OK
    BotB: got='tank' expected='tank' OK
```

For a real fight (auto-evolve runs after every round):

```bash
python scripts/train_bots.py --rounds 5 --interval 2
```

### DB lock resilience (bundled with v12)

- `server/db/store.py` — `PRAGMA busy_timeout=5000` + `synchronous=NORMAL`
- `server/main.py` — per-thread `_local.conn` cache + tick loop on its own conn
- `server/main.py` — `_retry_locked_db_write` helper wraps `room_create`
- `server/arena.py` — `_safe_db_write` retry wrapper around credits/fitness/rank
- `scripts/train_bots.py` — HTTP retry on 5xx

Verified: 5 rounds of `train_bots` produce 0 `AttributeError` and 0
`"database is locked"` in `logs/server.log`.

