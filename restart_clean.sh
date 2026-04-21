#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

./stop_server.sh
conda run --no-capture-output -n galtransl python -u headless_server.py reset
exec ./run_server.sh
