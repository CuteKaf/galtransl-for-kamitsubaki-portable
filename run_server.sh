#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

log_file="project/watch.log"
pid_file="project/watch.pid"
conda_base="$(conda info --base 2>/dev/null || true)"
python_bin="${conda_base:+$conda_base/envs/galtransl/bin/python}"
python_bin="${python_bin:-$HOME/miniconda3/envs/galtransl/bin/python}"

mkdir -p "$(dirname "$log_file")"
touch "$log_file"

if [ ! -x "$python_bin" ]; then
  echo "[WATCH] missing python: $python_bin"
  exit 1
fi

if [ -f "$pid_file" ]; then
  existing_pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [ -n "${existing_pid:-}" ] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "[WATCH] already running (pid $existing_pid)"
    echo "[WATCH] log: $log_file"
    exit 0
  fi
  rm -f "$pid_file"
fi

existing="$(pgrep -af 'python -u headless_server.py watch' || true)"
if [ -n "$existing" ]; then
  echo "[WATCH] already running"
  echo "$existing"
  echo "[WATCH] log: $log_file"
  exit 0
fi

printf '\n[%s] starting headless watcher\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$log_file"
nohup "$python_bin" -u headless_server.py watch </dev/null >> "$log_file" 2>&1 &
watch_pid=$!

sleep 1
if ! kill -0 "$watch_pid" 2>/dev/null; then
  echo "[WATCH] failed to start"
  echo "[WATCH] log: $log_file"
  rm -f "$pid_file"
  exit 1
fi

echo "$watch_pid" > "$pid_file"

echo "[WATCH] started in background (pid $watch_pid)"
echo "[WATCH] log: $log_file"
echo "[WATCH] tail: tail -f $log_file"
