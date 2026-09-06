#!/bin/bash
# Health check loop — runs every 5 minutes, checks server + bot activity + restarts if down.
# Called by start_all.sh as a background process.

set -e
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "[health $(date '+%H:%M:%S')] starting health check loop (5 min interval)..."

while true; do
    # Check 1: server health endpoint
    if ! curl -s --max-time 5 http://127.0.0.1:8787/health > /dev/null 2>&1; then
        echo "[health $(date '+%H:%M:%S')] ✗ server DOWN on 8787 — restarting"
        # Kill old server
        if [ -f logs/server.pid ]; then
            PID=$(cat logs/server.pid 2>/dev/null)
            if [ -n "$PID" ]; then
                taskkill //F //PID "$PID" 2>/dev/null || kill -9 "$PID" 2>/dev/null || true
            fi
        fi
        # Start new server
        nohup python -m uvicorn server.main:app --host 0.0.0.0 --port 8787 --log-level warning > logs/server.log 2>&1 &
        echo $! > logs/server.pid
        sleep 4
        if curl -s --max-time 5 http://127.0.0.1:8787/health > /dev/null 2>&1; then
            echo "[health $(date '+%H:%M:%S')] ✓ server restarted (pid $(cat logs/server.pid))"
        fi
    fi

    # Check 2: training loop still running?
    if [ -f logs/train.pid ]; then
        TPID=$(cat logs/train.pid)
        if ! kill -0 "$TPID" 2>/dev/null; then
            echo "[health $(date '+%H:%M:%S')] ✗ training loop down — restarting"
            nohup bash -c 'while true; do
                python scripts/train_bots.py --rounds 5 --interval 5 2>&1
                sleep 600
            done' > logs/train.log 2>&1 &
            echo $! > logs/train.pid
        fi
    fi

    # Check 3: log stats (every hour)
    HOUR=$(date '+%H')
    if [ "$HOUR" = "00" ]; then
        # Daily summary at midnight
        BOTS=$(curl -s "http://127.0.0.1:8787/api/v1/training/stats?limit=200" 2>/dev/null | python -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(len(d.get('bots', [])))
except: print(0)
")
        MATCHES=$(curl -s "http://127.0.0.1:8787/api/v1/arena/matches?lang=en" 2>/dev/null | python -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(len(d.get('matches', [])))
except: print(0)
")
        LISTINGS=$(curl -s "http://127.0.0.1:8787/api/v1/marketplace/browse?limit=100" 2>/dev/null | python -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(len(d.get('listings', [])))
except: print(0)
")
        echo "[health daily] $(date) bots=$BOTS matches=$MATCHES listings=$LISTINGS"
    fi

    sleep 300  # 5 min
done