#!/usr/bin/env bash
#
# reproduce.sh — independently reproduce pba-bench's headline result:
# a consensus-valid "poison block" that takes ~85 s to validate
# single-threaded on a mid-range Xeon (vs ~1 ms for a normal block).
#
# It runs two cases and writes their JSON/CSV/report under results/reproduce-<ts>/:
#   1. --par 1  : single script-validation thread (what a weak node sees)  -> ~85 s wall
#   2. default  : 16 parallel threads                                      -> ~6 s wall, ~94 s CPU
#
# Usage:
#   ./scripts/reproduce.sh [PATH_TO_BITCOIND]
#   PBABENCH_BITCOIND=/path/to/bitcoind ./scripts/reproduce.sh
#
# If no path is given it uses `which bitcoind`.
#
# Safety: regtest-only, disposable node, no public network. See README.md.
set -euo pipefail

cd "$(dirname "$0")/.."

# --- venv ---------------------------------------------------------------- #
if [[ ! -x .venv/bin/python ]]; then
    echo "creating virtualenv and installing dependencies..."
    python3 -m venv .venv
    .venv/bin/pip install --quiet -r requirements.txt
fi
PY=.venv/bin/python

# --- bitcoind ------------------------------------------------------------ #
BITCOIND="${PBABENCH_BITCOIND:-${1:-}}"
if [[ -z "$BITCOIND" ]]; then
    BITCOIND="$(command -v bitcoind || true)"
fi
if [[ -z "$BITCOIND" || ! -x "$BITCOIND" ]]; then
    echo "error: bitcoind not found. Pass a path or set PBABENCH_BITCOIND." >&2
    exit 1
fi
echo "using bitcoind: $BITCOIND"
"$BITCOIND" --version 2>/dev/null | head -1

TS="$(date +%Y%m%d-%H%M%S)"
OUT="results/reproduce-$TS"
mkdir -p "$OUT"

echo
echo "=== Case 1: single validation thread (--par 1) -> ~85 s wall ==="
"$PY" ./pba_bench.py benchmark --bitcoind "$BITCOIND" \
    --profile custom --num-utxos 8500 --sigops-per-input 100 \
    --par 1 --confirm --seed 41 --outdir "$OUT/par1"

echo
echo "=== Case 2: parallel validation (default) -> ~6 s wall, ~94 s CPU ==="
"$PY" ./pba_bench.py benchmark --bitcoind "$BITCOIND" \
    --profile custom --num-utxos 8500 --sigops-per-input 100 \
    --confirm --seed 41 --outdir "$OUT/par16"

# --- reports ------------------------------------------------------------- #
for d in par1 par16; do
    "$PY" ./pba_bench.py report "$OUT/$d/results.json" --output "$OUT/$d/report.md" \
        >/dev/null 2>&1 || true
done

echo
echo "================================================================"
echo " HEADLINE RESULT (saved under $OUT)"
echo "================================================================"
"$PY" - "$OUT" <<'PY'
import json, sys, glob
out = sys.argv[1]
for d in ("par1", "par16"):
    j = sorted(glob.glob(f"{out}/{d}/results.json"))[0]
    r = json.load(open(j))[0]
    c, m, o = r["construction"], r["measurement"], r["outcome"]
    par = r["provenance"]["validation_threads"]
    print(f"  validation threads (-par): {par}")
    print(f"  poison inputs x CHECKSIG : {c['num_utxos']} x {c['sigops_per_input']}")
    print(f"  executed CHECKSIG        : {c['executed_checksig_count']}")
    print(f"  sighash serialization MB : {c['sighash_serialization_bytes']/1e6:.1f}")
    print(f"  validation wall time     : {m['validation_wall_seconds']:.1f} s")
    print(f"  validation CPU time      : {m['validation_cpu_seconds']:.1f} s")
    print(f"  peak RSS                 : {m['peak_rss_bytes']/1e6:.0f} MB")
    print(f"  outcome                  : {o['success']}{' (' + o['rejection_reason'] + ')' if o['rejection_reason'] else ''}")
    print("  ------------------------------------------------------------")
print(f"\n  Node: {json.load(open(glob.glob(out+'/par1/results.json')[0]))[0]['provenance']['node_version_string']}")
PY

echo
echo "Results written to: $OUT"
echo "To compare against the committed sample: ls results/"
