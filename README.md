# pba-bench — a safe, reproducible Bitcoin worst-case block validation and propagation benchmark suite

A deterministic, **regtest-only**, **loopback-only** research harness for measuring
and understanding *worst-case (pathological) block validation* in Bitcoin. It
builds consensus-valid blocks that are pathologically slow to validate, measures
them against real `bitcoind` binaries, compares how different node versions and
mitigations (e.g. BIP 54 / Consensus Cleanup) treat them, and measures how
validation delay propagates across controlled local networks.

This is not an attack tool and not a demo aimed at scaring people. It exists to
give Core developers, protocol researchers, BIP authors, node implementers, and
performance/security researchers **exact, reproducible constructions, defensible
measurements, cross-version and cross-mitigation comparisons, and raw evidence**
for protocol discussion.

> A *worst-case validation block* is a block that is fully valid under Bitcoin's
> consensus rules but engineered so that validating it takes far longer than an
> ordinary block. This is a **classical algorithmic-complexity issue in legacy
> (pre-SegWit) signature hashing and signature verification** — it is *not* a
> quantum attack.

---

## Safety model (unchanged and enforced)

pba-bench **cannot** reach a public network. The safety layer (`safety.py`) is
the project's strongest feature and is enforced in code, not just documented:

* Chain forced to `-regtest` and verified at runtime via
  `getblockchaininfo.chain == "regtest"` (abort otherwise).
* RPC binds to loopback only; non-loopback hosts/IPv6 are refused.
* Fresh disposable datadir per run; existing datadirs, reused datadirs, and
  symlink escapes are refused.
* P2P networking is fully disabled by default (`-connect=0 -listen=0
  -dnsseed=0 -discover=0`). When the multi-node topology mode is used, P2P is
  enabled **only** over loopback addresses between disposable local regtest
  nodes.
* No DNS seeds, no peer discovery, no Tor/I2P/LAN/public peers, no public-network
  mode.
* `bitcoind` extra args are filtered; managed flags (`-datadir`, `-conf`,
  `-rpcuser`, …) and network flags are rejected.
* Hard resource limits (`--max-wall-seconds`, `--max-rss-mb`, `--max-blocks`,
  `--max-poison-tx-bytes`) are enforced, not merely recorded (see
  [Resource limits](#resource-limits)).
* `--confirm` is required for large/custom/propagation cases.

There is deliberately **no** way to enable a public-network mode.

---

## What this measures, and the honest distinction between claim types

This project separates **directly measured**, **derived/calculated**, and
**inferred** claims. They are never mixed.

| Type | Example |
|---|---|
| **Directly measured** | this block was accepted; validation took X seconds; observer Y reached the new tip after Z seconds; RPC max latency was W seconds |
| **Derived/calculated** | theoretical legacy-sighash serialization bytes; executed CHECKSIG count; BIP-54 sigop count |
| **Inferred** | a mitigation "would reject" the block when no supporting binary was actually tested |
| **External claim** | Portland HODL reported approximately N minutes |

Results and reports label every quantity with its type.

---

## The technical model (corrected)

The construction (the *scriptPubKey* vector, `vectors/scriptpubkey.py`) is:

1. **Preparation.** We spend coinbases to create UTXOs whose bare `scriptPubKey`
   is a chain of `CHECKSIG`/`CHECKSIGVERIFY` ops that one signature satisfies:

   ```
   OP_DUP <pub> OP_CHECKSIGVERIFY  OP_DUP <pub> OP_CHECKSIGVERIFY  ...  <pub> OP_CHECKSIG
   ```

   with `scriptSig = <sig>`. There are `K` signature checks per UTXO.

2. **Poison transaction.** A single transaction spends `N` such UTXOs. Each input
   pays the *same* signature (via `OP_DUP`).

The **corrected** cost model for validating this block on Bitcoin Core v31.1.0
(verified against source and by measurement — see
[research/TECHNICAL_CORRECTIONS.md](research/TECHNICAL_CORRECTIONS.md)):

* **Legacy sighash serialization + double-SHA-256: `O(N²)`, independent of `K`.**
  Bitcoin Core's per-input `SigHashCache` (interpreter.cpp) caches the SHA-256
  midstate keyed by `(hashType, scriptCode)` for each input. The `K-1` repeated
  identical `CHECKSIG`s *within one input* reuse that midstate and do **not**
  re-serialize or re-hash the transaction. Each of the `N` inputs serializes and
  hashes its `O(N)`-sized preimage **once**.

* **ECDSA signature verification: `O(N·K)`, one fresh verify per `CHECKSIG`.**
  During actual block connection the signature cache is *consulted but not
  populated* (`validation.cpp:2584`), so every `CHECKSIG` performs a fresh ECDSA
  verification (~60–90 µs each on the benchmark hardware).

* **Script interpreter stack/loop overhead: `O(N·K)`, but cheap.**

So the empirical validation cost is approximately
`O(N²)  (serialization+hashing)  +  O(N·K)  (ECDSA)`. The old headline
`O(N²·K)` described a hypothetical implementation *without* the per-input
midstate cache; it does **not** describe what v31.1.0 hashes during block
validation. The project keeps the no-cache quantity as a clearly-labelled
*hypothetical* counter.

### `MAX_OPS_PER_SCRIPT` (201) is a per-script budget

Bitcoin Core evaluates `scriptSig` and `scriptPubKey` in **two separate
`EvalScript` calls**, each with its own 201 non-push-op budget
(interpreter.cpp). It is **not** a combined scriptSig + scriptPubKey budget.
For this construction the `scriptPubKey` alone contributes `2K-1` non-push ops,
so `K ≤ 101`.

---

## Commands

```bash
# single-node validation benchmark
./pba_bench.py benchmark --bitcoind /opt/core31/bin/bitcoind --profile small --runs 3

# empirical scaling: sweep K at fixed N (answer: what does K cost after SigHashCache?)
./pba_bench.py sweep --bitcoind /opt/core31/bin/bitcoind --axis k \
    --fixed 2000 --values 1,2,5,10,25,50,75,100 --runs 3

# empirical scaling: sweep N at fixed K
./pba_bench.py sweep --bitcoind /opt/core31/bin/bitcoind --axis n \
    --fixed 10 --values 500,1000,2000 --runs 3

# five actually-independently-measured observers (star)
./pba_bench.py propagate --bitcoind /opt/core31/bin/bitcoind \
    --observers 5 --observer-par 1 --topology star --num-utxos 3000 \
    --sigops-per-input 100 --confirm

# the same block observed by heterogeneous nodes (par 1,2,4,8,default)
./pba_bench.py propagate --bitcoind /opt/core31/bin/bitcoind \
    --observer-par 1,2,4,8,0 --topology star --confirm

# multi-hop line topology
./pba_bench.py propagate --bitcoind /opt/core31/bin/bitcoind \
    --topology line --observers 8 --observer-par 1 --confirm

# BIP 54 A/B: the same deterministic construction against vanilla vs a Consensus
# Cleanup build
./pba_bench.py compare --vanilla /opt/core31/bin/bitcoind \
    --bip54 /opt/core-bip54/bin/bitcoind --num-utxos 3000 --sigops-per-input 100

# cross-version matrix from a manifest
./pba_bench.py compare --manifest configs/core-builds.json

# BIP54-active worst-case benchmark: activate BIP54 on a disposable regtest node,
# build the split (multi-transaction) poison, measure single-threaded validation
./pba_bench.py benchmark --bitcoind /opt/core-bip54/bin/bitcoind \
    --profile custom --num-utxos 8000 --sigops-per-input 100 \
    --per-tx-inputs 25 --bip54-active --activate-bip54 --par 1 --confirm

# CHECKMULTISIG-poison variant (packs ~200 sigops/input, the worst found)
./pba_bench.py benchmark --bitcoind /opt/core-bip54/bin/bitcoind \
    --profile custom --num-utxos 8000 --sigops-per-input 200 \
    --spk-kind multisig --per-tx-inputs 12 --bip54-active --activate-bip54 --par 1 --confirm

# bounded deterministic search for the post-BIP54 worst case (opt-in, heavy)
./pba_bench.py search --bitcoind /opt/core-bip54/bin/bitcoind \
    --spk-kind multisig --objective wall --budget 10 --par 1 --confirm \
    --max-blocks 700

# validate an externally-contributed result file
./pba_bench.py validate results/core-31/small-x3/results.json

# render a markdown report from any results file
./pba_bench.py report results/benchmark-.../results.json
```

Every run writes `results.json` + `results.csv` + `manifest.json` (and a
`report.md` for propagation runs) into a timestamped subdirectory of
`--outdir`. The result schema is versioned (`schemas.SCHEMA_VERSION`, currently
`2.0.0`).

---

## Single-node benchmark

`benchmark` launches a fresh, isolated regtest `bitcoind`, builds the
deterministic poison construction, and measures the blocking `submitblock` call.

Per run it records: `submitblock` wall time, process CPU time, peak RSS,
RPC latency during validation (with timeout/error counts — censored samples are
recorded, never silently dropped), block size/weight, executed CHECKSIG count,
ECDSA-verify count, legacy-sighash serialization bytes (cache-aware) and the
hypothetical no-cache counter, the outcome (accepted / rejected / timeout /
crash / aborted) with the exact reason, and full provenance (node version &
subversion, binary SHA-256, git commits, CPU, RAM, kernel, governor, affinity,
`-par`, seed, exact command).

Profiles:

| profile | N | K | total CHECKSIG | needs `--confirm` |
|---|---|---|---|---|
| `smoke` | 10 | 2 | 20 | no |
| `small` | 500 | 6 | 3,000 | no |
| `medium` | 2,500 | 4 | 10,000 | yes |
| `custom` | user | user | user | yes |

---

## Propagation / network-consequence experiments

`propagate` builds the poison block on a **miner** node and measures how a
controlled, loopback-only topology of **observer** nodes experiences it. Every
observer gets an *independent* measurement context: its own probe thread and RPC
connection, per-observer tip-transition timing, RPC latency, CPU, peak RSS, and
topology position. Observers may run different `-par` values, so one identical
block is observed by heterogeneous nodes.

Topologies (all loopback-only, among disposable local regtest nodes):

* `star` : `MINER -- every observer` (direct).
* `line` : `MINER -> A -> B -> C -> ...` — measures whether end-to-end delay
  compounds across validation hops.
* `tree` : a balanced binary tree rooted at `MINER`.

### Measurement terminology

* `time_to_tip_seconds` — miner `submitblock` → observer's active tip becomes the
  poison block. Includes P2P transmission, the observer's validation, and tip
  activation. **Not** pure wire propagation.
* `miner_validation_seconds` — the miner's own `submitblock` wall time.
* `post_miner_time_to_tip_seconds` — `time_to_tip` minus the miner's validation.

The reports distinguish miner validation, P2P announcement/transmission,
observer reconstruction/request behavior, observer validation, and the active-tip
transition, and they do **not** claim to isolate pure wire transmission without
P2P instrumentation. A defensible subset of P2P event instrumentation (parsing
the observer debug logs for `received block` and `UpdateTip` events) is
included; see `docs/analysis.md` for its limitations.

---

## Cross-binary comparison (BIP 54 and cross-version)

`compare` runs one *identical, deterministic* construction against multiple
`bitcoind` binaries and emits a comparable matrix with full provenance (path,
SHA-256, `--version`, RPC subversion, git commit when available).

* `compare --vanilla PATH --bip54 PATH` — the BIP 54 A/B workflow (vanilla vs a
  Consensus Cleanup build). A rejection is reported as **`live`** only when the
  supplied BIP54 binary actually rejects the block with
  `bad-txns-legacy-sigops`; otherwise it is marked **`inferred`**.
* `compare --manifest core-builds.json` — an arbitrary cross-version matrix
  (e.g. Core 29/30/31/master/BIP54), for spotting performance regressions.

The same construction runs against every binary in the matrix. `configs/core-builds.json`
is a template.

---

## Post-BIP54 worst-case research

The `--bip54-active` / `--activate-bip54` flags, the `--spk-kind`/`--per-tx-inputs`
construction knobs, and the `search` subcommand support measuring the *post-BIP54*
worst case (see `research/POST_BIP54_WORST_CASE.md` and
`research/BIP54_BOUNDARY_MAP.md`).

Key results (measured on the BIP54 reference implementation, Bitcoin Core PR
#35793 commit `9630491bf`, and vanilla v31.1.0, on a Xeon E5-2680):

* The original single-transaction poison is **live-rejected** by BIP54 with
  `bad-txns-legacy-sigops`.
* A BIP54-*valid* "split" block (the poison spread across ~700 small
  transactions, using 1-of-17 `OP_CHECKMULTISIG` to pack 20 sigops/opcode)
  reaches ~104 s of single-threaded validation — comparable to the pre-BIP54
  poison. BIP54 eliminates the `O(N²)` sighash-serialization bottleneck (~31×)
  but not the total per-block ECDSA work, because the per-block sigop cap does
  not count spent-scriptPubKey sigops and BIP54's cap is per-transaction.

Safety is unchanged: everything stays regtest-only, loopback-only, disposable.

---

## Resource limits (real, not decorative)

| Option | Behavior |
|---|---|
| `--max-wall-seconds` | aborts the run and reports `timeout` if a single validation exceeds it |
| `--max-rss-mb` | a watchdog hard-terminates the disposable node if its RSS exceeds this; the run is recorded as `aborted` with the reason |
| `--max-blocks` | refuses to construct (before building anything) a case that would mine more blocks than this |
| `--max-poison-tx-bytes` | refuses a poison transaction larger than this |

`--cpu-affinity 0,2` optionally pins each node to specific CPUs; the affinity is
recorded in provenance. `--warm-cold warm` submits an extra warmup block before
the measured block; `cold` is a freshly-started node. All runs are on a warmed
node in practice because the construction itself mines 100+ prep blocks first.

---

## What this does / does not prove

**It proves** (directly measured): Bitcoin Core's current consensus rules admit
blocks whose validation is pathologically expensive, and the project builds,
submits, and measures such blocks; and it quantifies the cost model
(`O(N²)` serialization+hashing + `O(N·K)` ECDSA) and how the same block
experiences heterogeneous local peers and moves across controlled topologies.

**It does not prove**:
* a byte-for-byte reproduction of Portland HODL's worst case (the exact
  generator is not public);
* that any *public* network is disrupted (regtest-only by design);
* a live BIP 54 rejection unless a real BIP 54 binary was tested.

See [docs/WHAT_THIS_PROVES.md](docs/WHAT_THIS_PROVES.md) for the precise claim
statement.

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q          # 125+ tests
```

Covers: the safety controls (mainnet/testnet/signet/testnet4 rejected,
non-loopback RPC and IPv6 rejected, external DNS / LAN peers rejected, existing
datadir / symlink-escape rejected, unsafe and injected extra args rejected,
cleanup stays in the workspace), generator determinism, sigop accounting, the
corrected cost-model metrics, topology generation, percentile/statistic
calculations, timeout/censored RPC samples, resource-limit enforcement, the
result schema and v1 migration, and tiny integration runs (multi-observer star,
line and tree topologies, heterogeneous `-par`, clean shutdown).

CI runs the pure unit/safety suite on every push; the heavy bitcoind integration
smoke is opt-in.

## Repository layout

```
pba-bench/
├── pba_bench.py          # CLI (benchmark, sweep, propagate, compare, report, validate)
├── benchmark.py          # single-node controller, resource limits, export, manifest
├── propagation.py        # multi-observer, heterogeneous, topologies (star/line/tree)
├── sweep.py              # N/K scaling sweeps with per-point aggregates
├── compare.py            # cross-binary / BIP54 comparison matrix
├── construction.py       # deterministic poison-block generator + corrected cost metrics
├── vectors/              # vector plugin registry (scriptpubkey implemented; scriptsig documented)
├── safety.py             # the safety layer (regtest/loopback/disposable only)
├── measure.py            # CPU/RSS/RPC sampling, RPC censoring, resource guard
├── provenance.py         # node/hardware/tool provenance, binary SHA-256
├── schemas.py            # versioned JSON/CSV result schema + v1 migration
├── manifest.py           # reproducibility manifest
├── report.py             # research markdown reports (benchmark/sweep/compare/propagation)
├── scripts/reproduce.sh  # one-command headline reproduction
├── configs/core-builds.json   # cross-version comparison manifest template
├── test_framework/       # vendored unchanged from Bitcoin Core v31.1.0 (MIT)
├── docs/                 # analysis, precise claim statement
├── research/             # primary-source notes + TECHNICAL_CORRECTIONS.md
├── tests/                # unit, safety, and tiny integration tests
├── results/              # results + contribution guide
└── .github/workflows/ci.yml
```

## License

MIT — see [LICENSE](LICENSE). The vendored `test_framework/` is MIT-licensed
Bitcoin Core code, vendored unchanged.
