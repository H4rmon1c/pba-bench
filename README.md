# pba-bench — a safe, reproducible Bitcoin **Poison Block Attack** benchmark

A deterministic, **regtest-only** tool that builds a *poison block* — a consensus-valid
Bitcoin block that is pathologically slow to validate — submits it to its own freshly
launched, fully isolated `bitcoind`, and measures the damage.

It reproduces the mechanism, the scaling law, and a **minute-scale validation time on a
single consensus-valid block** (85 s single-threaded, 850,000 signature operations),
and it documents exactly which consensus limits bound the attack.

> A *poison block* (PBA / worst-case validation attack) is a block that is fully valid
> under Bitcoin's consensus rules but engineered so that validating it takes far longer
> than an ordinary block. This is a **classical algorithmic-complexity issue in legacy
> (pre-SegWit) signature hashing** — it is *not* a quantum attack.

---

## TL;DR — what exactly this proves

**It proves that Bitcoin's current consensus rules admit blocks whose validation is
pathologically expensive, and it measures how expensive.**

1. **Existence.** We construct and submit a block that Bitcoin Core **accepts as
   consensus-valid** yet takes **85 seconds to validate single-threaded** (a normal
   block takes ~1 ms — roughly a **85,000× slowdown**).
2. **The mechanism.** The block exploits legacy signature hashing, where every
   `CHECKSIG` re-hashes the *entire transaction*. A transaction with many inputs each
   executing many signature checks forces `O(N²·K)` hashing work.
3. **The scaling law.** We measure total hashed bytes growing quadratically in the
   number of inputs, and wall/CPU time growing superlinearly.

**It does *not* prove** (and we are explicit about this — see
[What it does not prove](#what-it-does-not-prove)):
- that the attack reaches Portland HODL's ~25-minute worst case on this hardware
  (three consensus limits cap a single block; see [Consensus constraints](#consensus-constraints)),
- that it can be triggered against a public network (it is regtest-only by design),
- that it disrupts block *propagation* between peers (that needs a multi-node setup).

---

## Headline result

Measured on Bitcoin Core **v31.1.0**, Intel Xeon E5-2680 (16 physical / 32 logical
cores), regtest:

| Case | Poison inputs (N) | CHECKSIG/input (K) | BIP-54 sigops | Sighash preimage | Wall | CPU | Outcome |
|---|---|---|---|---|---|---|---|
| `small` | 500 | 6 | 3,000 | 0.01 GB | 0.036 s | 0.44 s | accepted |
| demo | 3,000 | 100 | 300,000 | 0.38 GB | 2.2 s | 32.5 s | accepted |
| **demo, `-par=1`** | **8,500** | **100** | **850,000** | **2.99 GB** | **85.1 s** | **85.1 s** | **accepted** |

The `-par=1` case runs a single script-validation thread, which is what a weak or
single-core node sees; the same block validates in 6.2 s wall time on 16 parallel
cores while saturating ~94 s of CPU. Full results are in
[BENCHMARKS.md](BENCHMARKS.md) and under `results/`.

---

## Why regtest is a valid proof (network-independence)

Block validity is decided by consensus rules applied **locally** on each node. There is
no network oracle in block validation. Regtest uses the **identical consensus code**
(script interpreter, legacy sighash, sigop counting) as mainnet — the only differences
are chain parameters (difficulty, block spacing), which are irrelevant here. Therefore:

> A block that is consensus-valid on regtest is consensus-valid on mainnet.

The *poison* property (slow to validate) is a property of the validation algorithm, so
it is also network-independent. Running this against a public testnet would not make the
finding "more real" — it would only add propagation-consequence measurements at the cost
of CPU-saturating third-party nodes, which we deliberately do not do (see
[Safety](#safety) and [Security model](#security-model)).

---

## The construction (technical)

Two stages:

1. **Preparation.** We spend coinbases to create UTXOs whose *bare* `scriptPubKey` is
   a chain of `CHECKSIG`/`CHECKSIGVERIFY` ops that one signature satisfies:

   ```
   OP_DUP <pub> OP_CHECKSIGVERIFY  OP_DUP <pub> OP_CHECKSIGVERIFY  ...  <pub> OP_CHECKSIG
   ```

   with `scriptSig = <sig>`. There are `K` signature checks per UTXO.

2. **Poison transaction.** A single transaction spends `N` such UTXOs. To validate
   input *i*, the interpreter runs its `scriptPubKey`, and **every one of the K
   `CHECKSIG` ops** computes a legacy signature hash: it hashes the entire transaction
   (all `N` inputs) with only input *i*'s scriptSig replaced by the spent
   `scriptPubKey` as `scriptCode`. So validation does `N·K` hashes of an `O(N)`-sized
   preimage — roughly **`O(N²·K)` hashing work**.

Key details we verified against the source:
- **The sighash cache does not save you.** Bitcoin Core's per-input sighash cache
  skips the *serialization* on repeated `CHECKSIG`s, and the signature cache skips
  repeated *ECDSA math*, but **every `CHECKSIG` still re-hashes the full preimage**
  (`HashWriter::GetHash()` is not cached). The expensive part is not removed.
- The poison transaction's own inputs contain only signature pushes (no `CHECKSIG`
  opcodes), so the poison *block* stays under the per-block sigop limit — it is
  consensus-valid.

---

## Consensus constraints (why the attack is bounded — an original finding)

While calibrating the benchmark we verified three consensus limits that cap how
expensive a **single** poison block can be in current Core:

| Limit | Value | Effect |
|---|---|---|
| `MAX_SCRIPT_SIZE` | 10,000 bytes | An output script over 10 KB is treated as *unspendable* and **not stored in the UTXO set** — caps the spent `scriptCode` size. |
| `MAX_OPS_PER_SCRIPT` | 201 ops | The scriptSig + scriptPubKey may execute at most 201 non-push ops — caps `K` to ~100 for our construction. |
| Block weight | 4,000,000 | Caps the number of poison inputs `N` to ~8,500. |

Together these cap a single block at ~850k sigops ≈ **85 s single-threaded** on this
hardware. Reaching Portland's 25-minute figure would require a *chain* of such blocks,
slower hardware (e.g. a Raspberry Pi), or a construction that defeats the sighash cache
more aggressively. This is an honest, useful result: it bounds the real-world blast
radius of the single-block attack in current software.

---

## What it does not prove

* **It is not a byte-for-byte reproduction of Portland's demo.** The exact generator is
  not public (only high-level figures are). We built the smallest defensible
  reproduction from Bitcoin's consensus behavior and the public test vectors, and we do
  not claim to match Portland's absolute 25-minute number.
* **It does not test against any public network.** See [Safety](#safety).

## Multi-node consequences (the `propagate` demo)

The `propagate` subcommand peers 1–N observer nodes with a miner over **loopback-only
P2P** and measures the real-world consequences of a poison block, in the style of the
0xB10C signet study but fully local and harmless:

* **Propagation delay** — how long the poison block takes to reach a peer (vs a normal
  block). The delay is dominated by the peer's validation time.
* **RPC blocking on the peer** — lightweight RPC calls issued while the peer validates
  the poison block are delayed (up to ~the validation time).
* **Stale tip** — the peer cannot update its tip until it finishes validating, so it
  keeps working on (mining/extending) the pre-poison tip during validation.

Measured on v31.1.0 (Xeon E5-2680), a single `-par=1` observer:

| | normal block | poison block (N=3000, 300k sigops) |
|---|---|---|
| propagation to peer | ~5 ms | **~30 s** |
| peer RPC blocked (max) | — | ~28 s |
| peer tip stays stale | ~0 | **~30 s** (until validation completes) |

This demonstrates the network-level blast radius: a poison block stalls a peer's
validation, blocks its RPC, and keeps it working on a stale tip — the conditions that
lead to stale blocks and wasted mining work.

---

## Quick start

```bash
cd pba-bench
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # psutil, coincurve

# smoke: verifies construction + acceptance in a few seconds
.venv/bin/python ./pba_bench.py benchmark --bitcoind "$(which bitcoind)" --profile smoke

# small: measurable, low-impact (no confirmation)
.venv/bin/python ./pba_bench.py benchmark --bitcoind "$(which bitcoind)" --profile small --runs 3

# large demonstration: a consensus-valid block that takes ~85 s single-threaded
.venv/bin/python ./pba_bench.py benchmark --bitcoind "$(which bitcoind)" \
    --profile custom --num-utxos 8500 --sigops-per-input 100 --par 1 --confirm

# generate a markdown report from a results dir
.venv/bin/python ./pba_bench.py report results/demo-8500x100-par1/results.json

# one-command reproduction of the headline result (single-threaded AND parallel),
# saved under results/reproduce-<timestamp>/ with a printed summary
./scripts/reproduce.sh            # or: ./scripts/reproduce.sh /path/to/bitcoind

# multi-node propagation demo: build the poison block on a miner, peer observers
# over loopback P2P, and measure how long it takes the poison block to reach them
# and how it blocks their RPC / keeps them on a stale tip.
.venv/bin/python ./pba_bench.py propagate --bitcoind "$(which bitcoind)" \
    --num-utxos 3000 --sigops-per-input 100 --observer-par 1 --confirm
```

Cross-version comparison: pass a different `--bitcoind` binary (Core 29/30/31, Knots, a
BIP-54 build). Node version, subversion, CPU, RAM and kernel are recorded in every result.

---

## Profiles

| profile | N | K | total sigops | needs `--confirm` |
|---|---|---|---|---|
| `smoke` | 10 | 2 | 20 | no |
| `small` | 500 | 6 | 3,000 | no |
| `medium` | 2,500 | 4 | 10,000 | yes |
| `custom` | user | user | user | yes |

No full-strength case is the default. `medium`/`custom` require `--confirm`. Hard limits
(`--max-wall-seconds`, `--max-rss-mb`, `--max-blocks`, `--max-poison-tx-bytes`) abort a
run that would exceed them.

---

## What is measured

Per run: `submitblock` wall time, process CPU time, peak RSS, RPC latency during
validation, block size/weight, tx/input count, BIP-54 sigop count, expected sighash
preimage bytes (cache-aware and theoretical no-cache), and the outcome
(accepted / rejected / timeout / crash) with the exact rejection reason. Provenance:
node version & subversion, git commit (if available), CPU, RAM, kernel, config, and the
deterministic seed. Every run is written to both `results.json` and `results.csv`
incrementally; `--runs N` and the `report` subcommand give median/min/max.

## Safety & security model

Enforced in code (`safety.py`), not just documented:

* Chain forced to `-regtest` and verified via `getblockchaininfo.chain == "regtest"`.
* RPC binds to loopback only; non-loopback hosts are refused.
* Fresh disposable datadir per run; existing datadirs and symlink escapes are refused.
* P2P networking fully disabled (`-connect=0 -listen=0 -dnsseed=0 -discover=0`).
* No DNS seeds, no peer discovery, no outbound/inbound P2P, no public-network mode.
* `bitcoind` extra args are filtered; `--confirm` required for large/custom cases.
* A prominent warning and the resolved datadir are printed before every run.

The tool always launches **its own** `bitcoind` with a throwaway regtest datadir. It
never connects to an existing node, never reuses a datadir, and never broadcasts to a
public network. There is no "public network" deployment mode. See
[Security model](docs/analysis.md#security-model).

## Tests

```bash
.venv/bin/python -m pytest tests/ -q          # 65 tests
```

Covers the safety controls (mainnet/testnet/signet rejected, non-loopback RPC rejected,
existing-datadir / symlink-escape rejected, unsafe extra args rejected, cleanup stays in
the workspace), generator determinism and sigop accounting, preimage correctness, and
that the smoke profile produces a valid regtest chain.

## Repository layout

```
pba-bench/
├── pba_bench.py          # CLI (benchmark, propagate, report)
├── benchmark.py          # controller: launch node, measure, export
├── propagation.py        # multi-node propagation / consequence demo
├── construction.py       # deterministic poison-block generator
├── safety.py             # the safety layer (regtest/loopback/disposable only)
├── measure.py            # CPU/RSS/RPC-latency sampling during validation
├── provenance.py         # node/hardware/build info
├── schemas.py            # JSON/CSV result schema
├── report.py             # markdown report generator
├── scripts/reproduce.sh  # one-command headline reproduction
├── test_framework/       # vendored unchanged from Bitcoin Core v31.1.0 (MIT)
├── configs/safe-defaults.json
├── docs/analysis.md      # deep technical analysis, observed vs. Portland
├── docs/WHAT_THIS_PROVES.md   # precise, honest claim statement
├── research/             # curated primary-source notes (BIP 54, PR 35793, gists)
├── tests/                # safety, generator, results, propagation tests
├── results/              # sample JSON/CSV/report results
├── BENCHMARKS.md
└── README.md
```

## License

MIT — see [LICENSE](LICENSE). The vendored `test_framework/` is MIT-licensed Bitcoin
Core code, vendored unchanged.
