#!/bin/bash
# AI WoW Simulator — 一键启动脚本
# 用法：./scripts/start_all.sh
# 启动：server (port 8787) + train_bots cron loop + health_check
# 你只要跑一次，然后它会一直在跑

set -e
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

mkdir -p logs

echo "===================================="
echo "AI WoW Simulator - Starting..."
echo "Project dir: $PROJECT_DIR"
echo "Time: $(date)"
echo "===================================="

# 1. Start server in background
if curl -s http://127.0.0.1:8787/health > /dev/null 2>&1; then
    echo "[server] already running on 8787"
else
    echo "[server] starting on 0.0.0.0:8787..."
    nohup python -m uvicorn server.main:app --host 0.0.0.0 --port 8787 --log-level warning > logs/server.log 2>&1 &
    echo $! > logs/server.pid
    sleep 4
    if curl -s http://127.0.0.1:8787/health > /dev/null 2>&1; then
        echo "[server] ✓ started (pid $(cat logs/server.pid))"
    else
        echo "[server] ✗ failed to start - check logs/server.log"
        exit 1
    fi
fi

# 2. Start training bot loop in background (every 10 min, 5 rounds each)
if [ -f logs/train.pid ] && kill -0 $(cat logs/train.pid) 2>/dev/null; then
    echo "[train] already running (pid $(cat logs/train.pid))"
else
    echo "[train] starting training loop (every 10 min, 5 rounds each)..."
    nohup bash -c 'while true; do
        echo "[train] === Round start at $(date) ==="
        python scripts/train_bots.py --rounds 5 --interval 5 2>&1
        echo "[train] === Round end, sleeping 10 min ==="
        sleep 60
    done' > logs/train.log 2>&1 &
    echo $! > logs/train.pid
    echo "[train] ✓ started (pid $(cat logs/train.pid))"
fi

# 3. Start health check loop (every 5 min)
if [ -f logs/health.pid ] && kill -0 $(cat logs/health.pid) 2>/dev/null; then
    echo "[health] already running (pid $(cat logs/health.pid))"
else
    echo "[health] starting health check loop (every 5 min)..."
    nohup bash "$PROJECT_DIR/scripts/health_check.sh" > logs/health.log 2>&1 &
    echo $! > logs/health.pid
    echo "[health] ✓ started (pid $(cat logs/health.pid))"
fi

echo ""
echo "===================================="
echo "✓ All systems running"
echo "  Server:    http://127.0.0.1:8787"
echo "  Training:  10 min cycle (5 rounds each)"
echo "  Health:    5 min cycle"
echo ""
echo "View logs:    tail -f logs/server.log"
echo "             tail -f logs/train.log"
echo "             tail -f logs/health.log"
echo ""
echo "Stop:        ./scripts/stop_all.sh"
echo "===================================="