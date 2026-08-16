# Benchmarks

All runs used the **regtest-only** benchmark against disposable Bitcoin Core
nodes. Nothing touched a public network. Quantities are labelled
**measured**, **derived**, or **inferred**.

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

| Case | N | K | CHECKSIG | serialMB* | wall | CPU | outcome |
|---|---|---|---|---|---|---|---|
| smoke | 10 | 2 | 20 | 0.005 | 0.004 s | 0.01 s | accepted |
| small (×3, median) | 500 | 6 | 3,000 | 10.4 | 0.036 s | 0.44 s | accepted |
| demo 3000×100 | 3,000 | 100 | 300,000 | 380 | 2.20 s | 32.5 s | accepted |
| demo 8500×100 | 8,500 | 100 | 850,000 | 2990 | 6.24 s | 93.7 s | accepted |
| demo 8500×100 `-par=1` | 8,500 | 100 | 850,000 | 2990 | **85.1 s** | 85.1 s | **accepted** |

\* serialMB = legacy sighash **serialization** bytes (cache-aware, derived), the
actual bytes Core v31.1.0 serializes and double-SHA-256s. This grows as `O(N²)`
and is **independent of K** because of the per-input `SigHashCache`.

## Cost model (corrected)

Validation cost on v31.1.0 is approximately
`O(N²)  serialization+hashing  +  O(N·K)  ECDSA`:

- **Serialization/hashing is `O(N²)`, independent of `K`.** The per-input
  `SigHashCache` caches the SHA-256 midstate, so the `K-1` repeated identical
  `CHECKSIG`s within one input do not re-serialize or re-hash.
- **ECDSA is `O(N·K)`.** The signature cache is not populated during block
  connection, so every `CHECKSIG` does a fresh ECDSA verify (~70 µs).

Measured (N=2000, `-par=1`):

| K | wall | per extra CHECKSIG |
|---|---|---|
| 1  | 1.11 s | — |
| 10 | 2.42 s | ~73 µs |
| 50 | 8.95 s | ~80 µs |

K=50 is ~8× slower than K=1, not ~50×. The per-extra-CHECKSIG cost matches ECDSA,
not a re-serialization. See [research/TECHNICAL_CORRECTIONS.md](research/TECHNICAL_CORRECTIONS.md).

## N scaling (quadratic serialization)

Sweeping N at K=1 (from `results/sweep-N/results.json`):

| N | sighash serialization | wall |
|---|---|---|
| 500 | 10.3 MB | 0.016 s |
| 1,000 | 41.1 MB | 0.069 s |
| 2,000 | 164.1 MB | 0.133 s |

Doubling N quadruples the serialization bytes (`N²`), and wall time grows
superlinearly. This is the quadratic serialization term.

## Multi-node propagation (`propagate`)

A miner builds the poison block and peers observers over loopback-only P2P.
`time_to_tip_seconds` spans miner submit → observer active-tip transition
(not pure wire propagation); `post_miner` subtracts the miner's validation.

### 3-observer line topology (`-par=1`)

| observer | upstream | time-to-tip | post-miner |
|---|---|---|---|
| 1 | miner | 0.20 s | 0.18 s |
| 2 | obs1 | 0.39 s | 0.37 s |
| 3 | obs2 | 0.57 s | 0.55 s |

End-to-end delay compounds across validation hops. *(measured)*

## Interpretation

* The **mechanism** (legacy sighash serialization `O(N²)` + per-CHECKSIG ECDSA
  `O(N·K)`) is reproduced and consensus-valid.
* The **scaling law** is measured, not assumed: `O(N²)` serialization is
  confirmed; the `K` term is ECDSA, not re-hashing.
* A single consensus-valid block reaches ~85 s single-threaded validation on this
  hardware.
* On a peered network, that block reaches observers only after their (slow)
  validation, keeping them on a stale tip with blocked RPC; delay compounds
  across a line topology.

The single-block ceiling on this hardware (~850k CHECKSIG) is set by
`MAX_SCRIPT_SIZE` (10 KB), `MAX_OPS_PER_SCRIPT` (201 per script), and block weight
(4M). See [docs/analysis.md](docs/analysis.md) and the [README](README.md).
