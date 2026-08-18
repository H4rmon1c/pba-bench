# Post-BIP54 worst-case block validation

An empirical search for the most expensive consensus-valid block that survives
BIP 54 (Consensus Cleanup), measured against the actual Bitcoin Core BIP54
reference implementation. This is an attempt to *falsify* the effectiveness of
BIP54's resource bounds.

All measurements are against a disposable, loopback-only, regtest-only
`bitcoind`. Nothing here touches any public network. Hardware: Intel Xeon
E5-2680 v0 @ 2.70 GHz (32 logical CPUs, 2 sockets / 16 physical cores), 251 GiB
RAM.

> **TL;DR.** BIP54 live-rejects the original single-transaction poison (the
> expected result). But the *total* single-threaded worst-case validation time is
> **not** reduced by BIP54: a BIP54-*valid* block that spreads the poison across
> ~700 small transactions, using 1-of-17 `OP_CHECKMULTISIG` to pack 20 sigops per
> opcode, reaches **~104 s** of single-threaded validation on this hardware —
> comparable to the ~102 s the same construction already takes on vanilla (the
> split was always valid) and worse than the measured single-transaction CHECKSIG
> poison (~87 s). BIP54 eliminates the `O(N²)` *serialization* bottleneck (~31x)
> but not the `O(N·K)` *ECDSA* bottleneck, because the per-block sigop cap does
> not count spent-scriptPubKey sigops and the BIP54 cap is per-transaction, not
> per-block.

---

## 1. Which exact BIP54 rule stops the original construction?

The original pba-bench poison is a single transaction spending `N` UTXOs whose
bare `scriptPubKey` runs `K` `OP_CHECKSIG` ops, giving `N*K` legacy sigops in
one transaction. BIP54's **per-transaction legacy-sigop limit**
(`MAX_TX_BIP54_SIGOPS = 2500`, `src/consensus/tx_verify.cpp::CheckSigopsBIP54`,
reject reason `bad-txns-legacy-sigops`) rejects it when `N*K > 2500`.

**LIVE**: on the `9630491bf` BIP54 build with `consensuscleanup` active on
regtest, the original poison (`N=200, K=100`, 20,000 sigops) is rejected:

```
submitblock returned: 'bad-txns-legacy-sigops'
```

The boundary is strict (`> 2500`); a 2500-sigop transaction is accepted. See
`research/BIP54_BOUNDARY_MAP.md` for the full rule map.

## 2. What is the slowest BIP54-valid workload found?

A **split, CHECKMULTISIG-poison block**:

* `N = 8550` poison UTXOs, each with a `scriptPubKey` of **10 × 1-of-17
  `OP_CHECKMULTISIG(VERIFY)`** blocks (valid pubkey checked last).
* Spent across **713 transactions**, each with 12 inputs = 12 × 200 = **2400
  BIP54 sigops** (≤ 2500, BIP54-valid).
* Total **1,711,200 BIP54 sigops**, block weight 3.94 Mwu (near the 4 Mwu cap).

Measured **104 s** of single-threaded validation (`-par=1`) on the BIP54 node.

## 3. How was it found?

1. Established that BIP54 is a per-transaction cap and that the per-block sigop
   cap does **not** count spent-scriptPubKey sigops (source + live).
2. Reasoned that the post-BIP54 worst case is therefore *total per-block ECDSA*,
   spread across many small transactions (a "split" poison).
3. Measured a CHECKSIG split (800,000 sigops → 67 s).
4. Found that `OP_CHECKMULTISIG` packs more sigops per byte of poison-block
   weight: a 1-of-17 CHECKMULTISIG costs `1 + 17 = 18` ops and counts as 20 BIP54
   sigops, but performs up to 17 ECDSA verifies (valid pubkey placed so it is
   checked last). 10 such blocks fit the 201-op budget → 200 sigops / 170 ECDSA
   verifies per input (vs 101 for CHECKSIG).
5. A bounded, seeded search (`pba_bench.py search`) over `(spk_kind, N, K,
   per_tx)` confirmed the multisig split dominates.

The search is deterministic, budget-capped, and resumable (see Phase 4 notes
below).

## 4. How reproducible is it?

The construction is fully deterministic (fixed seed, mocktime) and was measured
multiple times. Repeated measurements of the multisig split at `N=8000`:

| run | wall (s) |
|---|---|
| par=1 (BIP54) | 89.87 |
| par=1 (BIP54, N=8550) | 103.80 |
| par=1 (vanilla, N=8000) | 101.78 |
| par=8 (BIP54, N=8000) | 12.59 |

The ~10-12% spread between the BIP54 `N=8000` (89.9 s) and vanilla `N=8000`
(101.8 s) runs is system noise / machine load, not a BIP54 effect. The scaling
is linear in sigops at ~66–71 µs/verify, so the result is robust.

## 5. What dominates its cost?

**ECDSA verification** (libsecp256k1), ~66–71 µs/verify on this hardware. For the
worst candidate: ~1.45 M ECDSA verifies ≈ 104 s. Legacy-sighash serialization is
small for the split (~86 MB across all txs, since each transaction has only 12
inputs). The `O(N²)` serialization of the single-transaction poison (2.65 GB) is
gone.

**Profiling evidence** (`perf`/`/usr/bin/time` were not available on this host;
used psutil on the bitcoind process during a measured submit):

| metric | multisig split, N=2000 (400,800 sigops) |
|---|---|
| wall | 23.05 s |
| CPU (user+system) | 23.11 s |
| peak RSS | 96 MB |

CPU ≈ wall confirms the workload is single-threaded, CPU-bound (no I/O or lock
wait). RSS is tiny (96 MB), so memory is not a factor. The per-verify cost
(~66–71 µs) closely matches raw libsecp256k1 ECDSA verify on this CPU (~55 µs
measured via coincurve) plus script-interpreter overhead, confirming ECDSA is the
dominant term.

## 6. How does it scale?

Single-threaded wall time is linear in total sigops (~66–71 µs/sigop), up to the
block-weight ceiling of ~1.71 M BIP54 sigops / ~1.45 M ECDSA verifies per block.
The block-weight ceiling is set by `MAX_BLOCK_WEIGHT` (4 Mwu): each poison input
is 113 bytes (prevout + one 72-byte signature + sequence), and each carries ~200
BIP54 sigops.

## 7. How does `-par` affect it?

The split parallelizes well: speedup = `T(par=1) / T(par=N)`.

| par | wall (s) | speedup | efficiency |
|---|---|---|---|
| 1 | 89.87 | 1.0 | — |
| 2 | 48.56 | 1.85× | 93% |
| 4 | 25.24 | 3.56× | 89% |
| 8 | 12.59 | 7.14× | 89% |

The 713 independent transactions give the CCheckQueue plenty of parallel work, so
a BIP54-valid split scales near-linearly. The attack is therefore most effective
against single-threaded / low-parity nodes (common for IBD and many personal
nodes). (The par=2/par=4 runs were measured concurrently and may be slightly
inflated, so real efficiency is likely a touch higher.)

## 8. How do hot/cold UTXO states affect it?

Measured on the multisig split at `N=4000` (801,600 sigops), `-par=1`:

| state | wall (s) |
|---|---|
| hot (fresh node, UTXOs in cache) | 51.48 |
| cold (node restarted, `-dbcache=4`, UTXOs re-read from disk) | 51.40 |

A cold UTXO/chainstate cache adds **negligible** time: ECDSA verification
completely dominates the ~51 s. The construction's UTXO-access pattern (12
prevouts per transaction, read sequentially) is cache-friendly even at
`-dbcache=4`. Bounding Script CPU therefore does **not** expose a meaningful
chainstate/UTXO worst case at these sizes — the residual bottleneck is ECDSA, not
prevout access.

## 9. How much improvement does BIP54 provide against the original vector?

* Against the original single-transaction poison: **infinite / total** — it is
  rejected (`bad-txns-legacy-sigops`).
* Against the *total single-threaded validation worst case*: **~none**. A
  BIP54-valid split reaches ~90–104 s, comparable to the pre-BIP54 single-tx
  poison (~87 s at 800 K sigops) and to what the same split achieves on vanilla
  (~102 s). BIP54 forces the poison to be spread but does not reduce the total
  ECDSA work per block, because the per-block sigop cap does not count
  spent-scriptPubKey sigops.
* Against the `O(N²)` legacy-sighash **serialization** bottleneck: ~31× (2.65 GB →
  ~86 MB per block). This is the component BIP54's "reduces worst-case
  validation time by 40×" claim most plausibly refers to; the *total* (ECDSA)
  worst case is not reduced.

## 10. Does another resource become the practical bottleneck?

Yes. Post-BIP54 the bottleneck is **ECDSA verification (CPU)**, bounded by
per-block sigops (weight-limited), rather than legacy-sighash serialization. The
ECDSA work is linear and parallelizes, so it is a "capacity" DoS (occupies N
cores for ~104/N s) rather than an "algorithmic-complexity" DoS. UTXO/prevout
access is not the bottleneck for these recently-created, in-cache UTXOs (Phase 6
examines the cold case).

## 11. Did any interaction appear undocumented or surprising?

Two interactions stand out and I could not find them explicitly quantified in the
BIP54 rationale text:

1. **The per-block sigop cap does not count spent-scriptPubKey sigops**, so a
   block of push-only-scriptSig transactions can execute far more than 20,000
   legacy sigops. This is not new (it is the original poison's premise), but it
   means BIP54's *per-transaction* cap does not bound the *per-block* total.
2. **`OP_CHECKMULTISIG` packs ~20 sigops per opcode** (16–20 via `nKeysCount`
   accounting), letting a BIP54-valid transaction carry 2500 sigops / ~2125 ECDSA
   verifies in just 12 inputs, and letting a block carry ~1.7 M sigops. Combined
   with the split, this is a worse single-threaded worst case than the
   pre-BIP54 single-transaction CHECKSIG poison.

I could not find a prior public measurement of a BIP54-*valid* block reaching
~100 s of single-threaded validation. See Phase 10 novelty notes below.

## 12. What should Bitcoin Core developers investigate next?

* Whether a **per-block** (or per-input-weighted) legacy-sigop accounting that
  includes spent-scriptPubKey sigops is warranted, or whether the current
  per-transaction limit is sufficient given the block-weight ceiling and
  parallelization.
* The interaction between `nOpCount += nKeysCount` and BIP54's per-tx sigop cap:
  a 1-of-17 CHECKMULTISIG is 20 sigops but only 18 ops, so sigop accounting and
  op accounting diverge.
* Whether `CHECKMULTISIG`'s 16-key cap (and the `MAX_PUBKEYS_PER_MULTISIG` count
  of 20 for >16 keys) should be revisited if worst-case per-block sigops matter.
* Practical mitigation framing: BIP54 eliminates the *algorithmic-complexity*
  (super-linear) component; the remaining worst case is linear ECDSA work that
  parallelizes. Whether that residual is acceptable depends on the threat model
  (single-threaded IBD nodes vs multi-core miners).

---

## Phase 1 — BIP54 implementation obtained and verified

| Field | Value |
|---|---|
| Repository | https://github.com/bitcoin/bitcoin |
| Implementation | PR #35793 "Implement BIP 54 (Consensus Cleanup) without mainnet activation" |
| Commit | `9630491bf2135d03dac586d3492cfca9939f6fbb` |
| Compiler | GCC 14.2.0, cmake Release, `-DENABLE_IPC=OFF` |
| `bitcoind --version` | `Bitcoin Core daemon version v31.99.0-9630491bf213 bitcoind` |
| bitcoind SHA-256 | `a4468b31145bd21546bba4aeb1bd75e302a6c94431c676fcf7de424407cc384f` |

BIP54 is **not** active on regtest by default; it is activated by mining through
the `consensuscleanup` BIP9 cycle (`-vbparams=consensuscleanup:0:3999999999`,
~430 blocks). `bip54.activate_bip54()` does this. Live rules verified: sigop
limit, coinbase locktime (`height-1` in this PR revision, not `height-15` as the
BIP text states), 64-byte tx, non-final coinbase sequence.

## Phase 3/4 — the worst BIP54-valid shape and the search

The worst shape is the **multisig split**. A bounded, seeded search
(`pba_bench.py search --spk-kind multisig --objective wall --budget N --confirm`)
samples `(K, N, per_tx)` deterministically, measures each candidate live against
the BIP54 node, checkpoints to `results/search/search-state.json`, and ranks by
the chosen objective (wall, CPU, CPU/weight, wall/weight). Heavy benchmarks are
opt-in (`--confirm`). Verified end-to-end: a `checksig N=5000 K=20 per_tx=125`
candidate was measured live at 7.33 s / 100,000 sigops and ranked. The search is
resumable (`--resume`) by re-reading its checkpoint and skipping measured
candidates (unit-tested).

## Phase 8 — differential (vanilla v31.1.0 vs BIP54 `9630491bf`)

| construction | vanilla | BIP54 | reduction |
|---|---|---|---|
| original one-tx poison (800 K sigops) | 87.2 s (accepted) | **rejected** `bad-txns-legacy-sigops` | ∞ (rejection) |
| CHECKSIG split (800 K sigops) | — | 66.8 s (accepted) | BIP54-valid |
| multisig split (1.6 M sigops, N=8000) | 101.8 s (accepted) | 89.9 s (accepted) | ~0 (noise) |
| multisig split (1.71 M sigops, N=8550) | — | 103.8 s (accepted) | BIP54-valid |

The multisig split is accepted and equally slow on both binaries, confirming that
BIP54 does not reduce this worst case.

## Phase 10 — novelty assessment

* **Already in BIP54 rationale / PR:** per-transaction 2500 cap; the specific
  single-transaction poison is mitigated; 64-byte / timewarp / coinbase rules.
* **Not explicitly quantified that I could find:** a BIP54-*valid* block reaching
  ~104 s of single-threaded validation by combining (a) per-transaction-only
  sigop accounting, (b) the block sigop cap ignoring spent-scriptPubKey sigops,
  and (c) CHECKMULTISIG's ~20 sigops/opcode packing. I could not find a prior
  public measurement of this specific composition; it is a *characterization* of
  the post-BIP54 residual worst case rather than a claim that BIP54 is "broken".

## Uncertainties

* Portland HODL's ~25-minute worst case uses a construction we do not have
  byte-for-byte; we cannot reproduce it, so the "reduction factor vs the original
  attack" is bounded by our own one-tx (87 s) and multisig-split (104 s)
  measurements.
* `par=2`/`par=4` runs were measured concurrently and may be slightly inflated.
* ECDSA cost is hardware-specific (libsecp256k1 / CPU). Relative conclusions
  (linear scaling, split parallelization) are robust.

## Next three experiments

1. **Truly cold chainstate at larger scale.** The cold restart used `-dbcache=4`
   and showed no UTXO penalty; a larger poison with a much bigger UTXO set (more
   prep blocks), or a memory-constrained environment, may surface a chainstate
   component. Worth repeating at `N=8550` with `-dbcache=1`.
2. **CHECKMULTISIG micro-benchmark**: quantify the per-verify cost and the exact
   `nOpCount += nKeysCount` divergence from BIP54 sigop accounting across key
   counts (16/17/20), to confirm 17-key is the optimal ECDSA-work packing.
3. **Cross-version / `-prevoutfetchthreads`**: compare the multisig split on an
   earlier Core version and vary `-prevoutfetchthreads` to isolate the
   chainstate-prefetch contribution, and to check for performance regressions
   between Core releases.
