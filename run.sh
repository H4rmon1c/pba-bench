#!/usr/bin/env bash
# Convenience wrapper. Usage:
#   ./run.sh smoke [BITCOIND_PATH]
#   ./run.sh small 3 [BITCOIND_PATH]
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv/bin/python"
if [[ ! -x "$VENV" ]]; then
    echo "creating virtualenv and installing deps..."
    python3 -m venv .venv
    .venv/bin/pip install --quiet -r requirements.txt
fi

PROFILE="${1:-smoke}"
RUNS="${2:-1}"
BITCOIND="${3:-/usr/local/bin/bitcoind}"

exec .venv/bin/python ./pba_bench.py benchmark \
    --bitcoind "$BITCOIND" --profile "$PROFILE" --runs "$RUNS"
