# AI WoW Simulator - Build Plan

## Architecture (locked)
- **Server**: Python 3.14 + FastAPI + SQLite
- **AI API**: HTTP REST + Bearer Token (`/api/v1/action`, `/api/v1/state`)
- **Observer Web**: Plain HTML + JS + CSS (no framework)
- **Observer TUI**: Python + rich
- **Tick**: World tick loop @ 500ms default
- **Persistence**: SQLite + JSON snapshots
- **i18n**: zh-CN (default) / en-US (toggle via `?lang=en` web, `--lang en` tui)

## Module Dependency Order
1. `server/db/` — SQLite schema, persistence (NO deps)
2. `server/i18n/` — bilingual text catalog (NO deps)
3. `server/world/` — entities (player/mob/npc/boss/item/quest/zone), tick loop, RNG → depends on db
4. `server/combat/` — skills, damage formula, aggro, crit, status effects → depends on world
5. `server/guild/` — guild CRUD, ranks, declare_war/alliance, invites, kick → depends on world, db
6. `server/quest/` — quest templates, objective tracking, reward tables → depends on world, db
7. `server/api/` — FastAPI routes (auth, action, state, guild, world snapshot, observer stream) → depends on all above
8. `server/agent_sdk/` — Python SDK for AI agents (3-line connect)
9. `server/observer/` — server-side broadcast (SSE-style polling endpoints for web/tui)
10. `web/` — observer HTML page (static, polls `/api/v1/observer/state`)
11. `terminal/` — observer TUI (rich, polls server)
12. `mock_agents/` — 5 fake AI agents running concurrent scenarios (party, dungeon, war)
13. `tests/` — pytest smoke tests per module
14. `scripts/` — bootstrap, reseed, e2e runner

## Game Design Constraints
- 4 starting classes: warrior / mage / priest / hunter (each with 4+ skills)
- Level 1 player vs boss → guaranteed loss (damage formula tuned so 1v1 boss is unwinnable)
- 3-player party vs same boss → guaranteed win (tuned damage / heal curves)
- Boss drops loot scaled to party size
- Guild: create/join/invite/kick/promote/declare_war/form_alliance
- 2+ dungeons with multiple boss rooms
- Resource: gold, herbs, ore (gathering nodes)
- PvP flag toggle

## Self-Check (9 items)
1. Mock 5 AI agents auto-party + clear dungeon
2. Guild create/join/kick/declare_war via CLI + API
3. Solo L1 vs boss = loss; party of 3 = win
4. Web observer shows live battle (curl HTML)
5. TUI observer rich output
6. Bilingual toggle (web ?lang=en, tui --lang)
7. AI SDK 3-line example runs
8. start.bat + Makefile both work
9. grep TODO/FIXME/NotImplementedError returns empty

## File Layout
```
D:\Projects\ai-wow-simulator\
  start.bat
  Makefile
  README.md
  requirements.txt
  server\__init__.py
  server\main.py
  server\config.py
  server\db\{__init__,schema.py,store.py}
  server\i18n\{__init__,catalog.py}
  server\world\{__init__,entities.py,tick.py,zone.py,loot.py,gathering.py}
  server\combat\{__init__,skills.py,engine.py,balance.py}
  server\guild\{__init__,manager.py}
  server\quest\{__init__,templates.py,manager.py}
  server\api\{__init__,auth.py,action.py,state.py,guild.py,observer.py,admin.py}
  server\observer\{__init__,broadcaster.py}
  server\agent_sdk\{__init__,client.py}
  web\index.html
  web\app.js
  web\style.css
  terminal\observer_tui.py
  mock_agents\run_demo.py
  mock_agents\agents.py
  scripts\bootstrap.py
  scripts\e2e_test.py
  scripts\difficulty_check.py
  scripts\guild_cli.py
  scripts\seed_world.py
  tests\test_smoke.py
  data\seed.json
  logs\server.log
  docs\AGENT_API.md
  docs\OBSERVER.md
  docs\ARCHITECTURE.txt
```
