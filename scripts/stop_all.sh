#!/bin/bash
# Stop all background processes
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

for name in server train health; do
    PIDFILE="logs/${name}.pid"
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE")
        if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
            echo "[stop] killing $name (pid $PID)"
            taskkill //F //PID "$PID" 2>/dev/null || kill -9 "$PID" 2>/dev/null || true
        fi
        rm -f "$PIDFILE"
    fi
done

# Also kill anything on port 8787
PID=$(powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8787 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess" 2>/dev/null | tr -d '\r\n')
if [ -n "$PID" ] && [ "$PID" -gt 0 ] 2>/dev/null; then
    echo "[stop] killing port 8787 pid $PID"
    taskkill //F //PID "$PID" 2>/dev/null || true
fi

echo "[stop] all stopped"