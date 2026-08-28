@echo off
REM AI WoW Simulator — Windows launcher
REM Usage: start.bat            (start server in background)
REM        start.bat mock       (start server + run 5 mock agents)
REM        start.bat tui        (start server + run TUI observer)
REM        start.bat web        (start server, open browser)
REM        start.bat test       (start server + run difficulty/e2e/guild checks)

setlocal
cd /d %~dp0

if not exist data mkdir data
if not exist logs mkdir logs

if "%1"=="mock" goto :mock
if "%1"=="tui" goto :tui
if "%1"=="web" goto :web
if "%1"=="test" goto :test

REM Default: just start the server in foreground
echo Starting AI WoW Simulator on http://127.0.0.1:8787
echo Press Ctrl+C to stop
python -m server.main
goto :eof

:mock
echo Starting server + 5 mock agents
start "wow-server" /B python -m server.main
timeout /t 3 /nobreak >nul
python -m mock_agents.run_demo --n 5
goto :eof

:tui
start "wow-server" /B python -m server.main
timeout /t 3 /nobreak >nul
python -m terminal.observer_tui --lang %2
goto :eof

:web
start "wow-server" /B python -m server.main
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8787/
goto :eof

:test
start "wow-server" /B python -m server.main
timeout /t 3 /nobreak >nul
python scripts\difficulty_check.py
python scripts\guild_cli.py smoke
python scripts\e2e_test.py
goto :eof
