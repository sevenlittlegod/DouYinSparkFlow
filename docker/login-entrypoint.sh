#!/bin/bash
set -euo pipefail
umask 077

export DISPLAY=:99
export PYTHONUNBUFFERED=1
mkdir -p /config
chmod 700 /config

children=()
cleanup() {
  trap - EXIT INT TERM
  if ((${#children[@]})); then
    kill "${children[@]}" 2>/dev/null || true
    wait "${children[@]}" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 &
children+=("$!")
for attempt in {1..50}; do
  [[ -S /tmp/.X11-unix/X99 ]] && break
  sleep 0.1
done
if [[ ! -S /tmp/.X11-unix/X99 ]]; then
  echo '[login] 无法启动图形显示。' >&2
  exit 1
fi
x11vnc -display "$DISPLAY" -localhost -rfbport 5900 -nopw -forever -shared >/dev/null 2>&1 &
children+=("$!")
websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 >/dev/null 2>&1 &
children+=("$!")
cd /app
python /app/docker/login.py &
children+=("$!")
wait "${children[-1]}"
