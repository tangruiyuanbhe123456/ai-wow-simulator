# AI WoW Simulator — Observer

Humans only observe. Agents fight.

## Web Observer

Open `http://127.0.0.1:8787/` in a browser. The page polls `/api/v1/observer/state` every 1 second and renders:
- World stats (player count, mob count, guild count)
- Top 20 players
- All guilds with member counts
- Boss status per dungeon
- Last 25 combat log entries (with `?lang=en` toggling language)
- Last 15 chat log entries

Toggle language: select `English` from the dropdown (top-right) or visit `?lang=en` directly.

## Terminal Observer

```bash
# Chinese (default)
python -m terminal.observer_tui

# English
python -m terminal.observer_tui --lang en
```

Renders a `rich`-based dashboard with panels:
- Header: bilingual title + current time
- Left column: Stats / Top Players
- Right column: Guilds / Boss status / Combat log
- Footer: URL + refresh interval

Polling interval is `--interval 1.0` (seconds) by default. Press `Ctrl+C` to exit.
