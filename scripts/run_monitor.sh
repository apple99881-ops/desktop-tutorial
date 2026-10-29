#!/bin/sh
# 매일 아침 감시 (cron 예: 50 5 * * 1-5 /경로/scripts/run_monitor.sh)
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
exec python3 -m uvis_monitor run >> logs/scheduler.log 2>&1
