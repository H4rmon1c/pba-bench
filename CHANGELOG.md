# Changelog

## v2.0.0 (unreleased) — worst-case validation & propagation research harness

The project evolved from a poison-block *reproduction* into a
**high-quality, reproducible Bitcoin worst-case block validation and propagation
research harness**. Key changes:

### Technical corrections
- **Corrected the headline cost model.** The old `O(N²·K)` claim is wrong for
  Bitcoin Core v31.1.0. The per-input `SigHashCache` caches the SHA-256 midstate,
  so repeated identical `CHECKSIG`s within an input do not re-serialize/re-hash →
  serialization/hashing is `O(N²)`, independent of `K`. The signature cache is not
  populated during block connection → every `CHECKSIG` does a fresh ECDSA verify →
  ECDSA is `O(N·K)`. See `research/TECHNICAL_CORRECTIONS.md`.
- **Corrected `MAX_OPS_PER_SCRIPT`.** It is a 201-op budget *per script* (scriptSig
  and scriptPubKey are evaluated separately), not a combined budget.
- Corrected the `sighash preimage` metrics: `sighash_serialization_bytes` (actual,
  cache-aware), `sighash_double_sha256_bytes`, and a clearly-labelled *hypothetical*
  `no_cache_sighash_serialization_bytes`.

### New capabilities
- `sweep` — N/K scaling sweeps with repeated trials and per-point aggregates
  (median/min/max/p25/p75/stdev).
- `propagate` — real multi-observer mode with an independent measurement context
  per observer; heterogeneous `--observer-par 1,2,4,8,0`; `star`/`line`/`tree`
  loopback-only topologies; per-observer timing, RPC latency/timeouts, CPU, RSS.
- `compare` — BIP 54 A/B (`--vanilla PATH --bip54 PATH`) and cross-version matrix
  (`--manifest`) with per-binary SHA-256/provenance; distinguishes `live` vs
  `inferred` BIP54 rejection.
- `validate` — validate externally-contributed result files.
- Vector plugin registry (`vectors/`) — scriptPubKey implemented; scriptSig/P2SH
  family documented but not implemented (no public generator).
- Reproducibility manifest (`manifest.json`) per run (tool/node/hardware
  provenance, exact command, result-file hashes; no secrets).
- Versioned result schema (`schemas.SCHEMA_VERSION = 2.0.0`) with v1 migration.
- Real resource limits: `--max-rss-mb` watchdog, `--max-blocks` refusal before
  construction, `--max-wall-seconds`, `--max-poison-tx-bytes`.
- Optional `--cpu-affinity`, meaningful `--warm-cold`, RPC probe censoring fix
  (timeouts recorded, not silently dropped).
- Research markdown reports with measured/derived/inferred separation.

### Safety
The safety model is unchanged or stronger: regtest-only, loopback-only, fresh
disposable datadirs, no public-network mode. Added tests for non-loopback IPv6,
external DNS/LAN peers, argument injection, and unsafe managed flags.

### Tests
Expanded to 125+ tests: config validation, corrected cost-model metrics, topology
generation, statistics, RPC censoring, resource guard, schema v2 + v1 migration,
plus tiny integration runs (multi-observer star/line/tree, heterogeneous par,
clean shutdown). CI added.

## v1.0.0 (2026-08-14)

**pba-bench: a safe, reproducible Bitcoin Poison Block Attack benchmark.**

- Single-node validation of a consensus-valid poison block (~85 s single-threaded
  at N=8500, K=100 on a Xeon E5-2680). **The `O(N²·K)` framing used here was
  corrected in v2.0.0.**
- Quadratic scaling demonstration; multi-node `propagate` demo; original finding
  on the consensus limits bounding a single block.
- Regtest-only; loopback-only; never touches a public network.
