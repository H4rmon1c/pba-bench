# Changelog

## v1.0.0 (2026-08-14)

**pba-bench: a safe, reproducible Bitcoin Poison Block Attack benchmark.**

### What it does

Builds a consensus-valid *poison block* (pathologically slow to validate due to legacy
sighash re-hashing), submits it to a disposable regtest node, and measures the damage.
Regtest-only; loopback-only; never touches a public network.

### Highlights

- **Single-node validation:** a consensus-valid block with 8,500 inputs × 100 CHECKSIG
  (850,000 BIP-54 sigops) validates in **~85 s single-threaded** on a Xeon E5-2680 —
  ~85,000× slower than a normal block. Accepted by Bitcoin Core v31.1.0.
- **Quadratic scaling:** total sighash preimage bytes grow as `N²` with the number of
  inputs; validation time grows superlinearly.
- **Multi-node consequences (`propagate`):** the same block **propagates ~97 s to a
  peered node** (vs ~5 ms for a normal block), keeps the peer's tip stale for the whole
  validation, and blocks its RPC up to ~30 s.
- **Original finding:** verified the consensus limits that bound a single-block attack
  in current Core — `MAX_SCRIPT_SIZE` (10 KB), `MAX_OPS_PER_SCRIPT` (201), and block
  weight (4M) — which cap a single block at ~850k sigops on this hardware.

### Features

- `benchmark` — profiles (`smoke`/`small`/`medium`/`custom`), parameter sweeps, repeated
  trials, JSON+CSV export, deterministic seed.
- `propagate` — multi-node propagation / consequence demo on a private loopback network.
- `report` — markdown report generator.
- `scripts/reproduce.sh` — one-command headline reproduction.
- 74 automated tests (safety, generator, results, propagation).

### Safety

Chain forced to regtest and verified via RPC; loopback-only RPC and P2P; fresh
disposable datadir per run; no DNS seeds, no peer discovery, no public-network mode;
`--confirm` required for large/custom/propagation cases.

### Notes / limitations

- Not a byte-for-byte reproduction of Portland HODL's ~25-minute demo (exact generator
  not public; consensus limits cap a single block). Mechanism, scaling, and a
  minute-scale single block are reproduced.
- The `scriptSig`/P2SH vector and large-scale multi-peer topology are not implemented.
- A live BIP 54 rejection requires a BIP-54 build (v31.1.0 has it only as mempool
  policy); the tool records `bip54_would_reject` as an inference.
