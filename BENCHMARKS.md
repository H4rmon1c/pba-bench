# Benchmarks

All runs were made with the **regtest-only** benchmark against a disposable
Bitcoin Core node. Nothing touched a public network.

## Environment

| | |
|---|---|
| Node | Bitcoin Core **v31.1.0** (`/Satoshi:31.1.0/`, RPC version 310100) |
| CPU | Intel(R) Xeon(R) CPU E5-2680 0 @ 2.70 GHz (16 physical / 32 logical) |
| OS | Linux 6.12 (x86_64) |
| RAM | 251 GiB |
| Validation threads | default (16 script threads) unless `-par=1` noted |

## Single runs

Raw results are in `results/*/results.json` and `results.csv`.

| Case | N | K | BIP-54 sigops | preimage (cache-aware) | wall | CPU | peak RSS | outcome |
|---|---|---|---|---|---|---|---|---|
| smoke | 10 | 2 | 20 | 5 KB | 0.004 s | 0.01 s | 54 MB | accepted |
| small (×3, median) | 500 | 6 | 3,000 | 10.4 MB | 0.036 s | 0.44 s | 54 MB | accepted |
| demo 3000×100 | 3,000 | 100 | 300,000 | 0.38 GB | 2.20 s | 32.5 s | 77 MB | accepted |
| demo 8500×100 | 8,500 | 100 | 850,000 | 2.99 GB | 6.24 s | 93.7 s | 106 MB | accepted |
| demo 8500×100 `-par=1` | 8,500 | 100 | 850,000 | 2.99 GB | **85.1 s** | 85.1 s | 162 MB | **accepted** |

The `-par=1` case is the single-threaded (weak-node) view: wall time equals CPU time.

## Scaling (quadratic hashing)

Sweeping the number of poison inputs at K=1 (from `results/sweep-N/results.json`):

| N | total sighash preimage | wall |
|---|---|---|
| 500 | 10.3 MB | 0.016 s |
| 1,000 | 41.1 MB | 0.069 s |
| 2,000 | 164.1 MB | 0.133 s |

Doubling N quadruples the preimage bytes (`N²` scaling), and wall time grows
superlinearly.

## Interpretation

* The **mechanism** (legacy sighash re-hashing) is reproduced and consensus-valid.
* The **scaling law** (`O(N²·K)` hashing) is measured directly.
* A single consensus-valid block reaches **~85 s single-threaded validation** —
  roughly 85,000× slower than a normal block.
* The single-block ceiling on this hardware (~850k sigops) is set by
  `MAX_SCRIPT_SIZE` (10 KB), `MAX_OPS_PER_SCRIPT` (201), and block weight (4M).
  See [docs/analysis.md](docs/analysis.md) and the README.
