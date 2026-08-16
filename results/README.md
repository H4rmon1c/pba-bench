# Results

This directory holds benchmark results. Each run writes, into its own
timestamped subdirectory, at minimum:

* `results.json` — the machine-readable result (one entry per trial).
* `results.csv` — the same, flattened to a stable column layout.
* `manifest.json` — the reproducibility manifest (tool + node + hardware
  provenance, parameters, exact command, result-file SHA-256).
* `report.md` — a research markdown report (for `propagate` runs).

`benchmark`/`sweep`/`compare`/`propagate` each write a `*.json` (e.g.
`sweep.json`, `compare.json`, `propagation.json`) plus `manifest.json` and the
results.

## Layout

Suggested layout for contributed results (grouped by node binary so a future
reader can compare across versions and mitigations):

```
results/
├── core-31/          # e.g. Bitcoin Core v31.1.0 (vanilla)
│   └── <vector>-N<K>-<seed>/...
├── core-master/      # Bitcoin Core master build
├── bip54/            # a Consensus Cleanup (BIP 54) build
└── rpi5/             # a different hardware platform
```

The exact subdirectory name is not important; the `manifest.json` inside each
run is authoritative.

## Contributing results

To submit an independently reproduced result set:

1. Run pba-bench on your platform, e.g.:
   ```bash
   ./pba_bench.py benchmark --bitcoind /path/to/bitcoind --profile small --runs 3 \
       --outdir results/core-31/small-x3
   ./pba_bench.py sweep --bitcoind /path/to/bitcoind --axis k --fixed 3000 \
       --values 1,5,25,50 --runs 3 --outdir results/core-31/k-sweep
   ./pba_bench.py propagate --bitcoind /path/to/bitcoind --topology line \
       --observers 4 --observer-par 1 --num-utxos 3000 --sigops-per-input 100 \
       --confirm --outdir results/core-31/line-4obs
   ```
2. Keep the whole output subdirectory (JSON + CSV + manifest + report).
3. Open a PR (or paste the directory) with a short note stating: your platform,
   the exact bitcoind binary (path + SHA-256 are recorded in the manifest), and
   that the run was local/regtest-only.

Before merging, maintainers validate your files:

```bash
./pba_bench.py validate results/core-31/small-x3/results.json
```

This checks the result shape against the schema. The manifest records the schema
version that produced the file.

## Safety note

Every result here was produced by a **regtest-only, loopback-only** run against
disposable local nodes. Never commit RPC passwords or datadirs; `manifest.json`
deliberately contains no secrets.
