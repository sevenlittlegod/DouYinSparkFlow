#!/bin/bash
set -euo pipefail
umask 077
cd /app
python /app/docker/runtime.py prepare
echo '[docker] Configuration validated; waiting for the daily scheduled run.'
exec cron -f
