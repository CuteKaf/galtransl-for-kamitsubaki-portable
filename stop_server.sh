#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

pid_file="project/watch.pid"

if [ -f "$pid_file" ]; then
  watch_pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [ -n "${watch_pid:-}" ] && kill -0 "$watch_pid" 2>/dev/null; then
    echo "[STOP] stopping watcher pid: $watch_pid"
    kill "$watch_pid" || true
  fi
  rm -f "$pid_file"
fi

pids="$(ps -eo pid=,args= | awk '
  /headless_server\.py watch/ ||
  /headless_server\.py once/ ||
  /whisper-cli/ ||
  /llama-server/ ||
  /\/ffmpeg\/ffmpeg/ {
    print $1
  }
')"

if [ -z "$pids" ]; then
  echo "[STOP] no matching processes"
  exit 0
fi

echo "[STOP] stopping processes: $pids"
kill $pids || true
sleep 2

still_running="$(ps -o pid= -p $pids 2>/dev/null | xargs || true)"
if [ -n "$still_running" ]; then
  echo "[STOP] force killing: $still_running"
  kill -9 $still_running || true
fi

echo "[STOP] done"
