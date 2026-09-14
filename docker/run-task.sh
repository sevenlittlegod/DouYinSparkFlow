#!/bin/bash
set -euo pipefail
umask 077
cd /app
mkdir -p /app/logs
# Manual and cron invocations use the same persistent lock, including across containers.
exec flock -n -E 75 /app/logs/task.lock python /app/docker/runtime.py run "$@"
