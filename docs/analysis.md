# pba-bench: technical analysis

This document explains the worst-case-validation construction, the corrected cost
model, and separates *confirmed (from source)*, *measured*, *inferred*, and
*unknown* statements. It supersedes the earlier analysis that described the cost
as `O(N²·K)`.

---

## 1. Confirmed consensus behavior (from Bitcoin Core source, v31.1.0)

These statements are verified against the Bitcoin Core source in this repository
(`src/script/interpreter.cpp`, `src/script/sighash*`, `src/script/script.cpp`,
`src/validation.cpp`, `src/consensus/tx_verify.cpp`).

* **Legacy signature hashing serializes the whole transaction per input.**
  For a legacy (pre-SegWit) signature, `SignatureHash` serializes the transaction
  with every input's `scriptSig` emptied except the one being signed, which is
  replaced by the spent `scriptPubKey` (the `scriptCode`). The preimage is then
  double-SHA-256'd. The cost of one *first* `CHECKSIG` on an input is `O(transaction
  size)`.

* **The per-input `SigHashCache` caches the SHA-256 midstate.** Each input creates
  a fresh `GenericTransactionSignatureChecker` (and thus a fresh `SigHashCache`)
  (`validation.cpp:2028`). Within one input, repeated `CHECKSIG`s with the same
  `hashType` and `scriptCode` reuse the cached midstate (`interpreter.cpp:1582-1685`),
  so they do **not** re-serialize or re-hash the transaction. `HashWriter` stores
  only the SHA-256 state, so the cache copy is O(1).
  → **Legacy sighash serialization + hashing is `O(N²)`, independent of `K`.**

* **The signature cache is not populated during block connection.**
  `validation.cpp:2584`: `bool fCacheResults = fJustCheck;` — during real block
  connection `fJustCheck` is false, so `cacheSigStore=false` and the signature
  cache is consulted but never written (`sigcache.cpp`). Every `CHECKSIG` performs
  a fresh ECDSA verification during block validation.
  → **ECDSA verification is `O(N·K)`, one fresh verify per `CHECKSIG`.**

* **`MAX_OPS_PER_SCRIPT = 201` is a per-script budget.** `scriptSig` and
  `scriptPubKey` are evaluated in separate `EvalScript` calls
  (`interpreter.cpp:2029-2034`), each initialising its own `nOpCount = 0`
  (`interpreter.cpp:441`). It is **not** a combined budget.

* **`MAX_SCRIPT_SIZE = 10000`.** An output script > 10 KB is treated as
  unspendable (`script.h:564-566`) and not stored in the UTXO set.

* **Block sigop cap.** `MAX_BLOCK_SIGOPS_COST = 80000` weight units = 20000
  legacy sigops. The poison transaction's own `scriptSig`s are push-only (no
  `CHECKSIG` opcodes), so the poison block stays under the cap.

## 2. Measured cost model (v31.1.0, `-par=1`, Xeon E5-2680)

At N=2000 poison inputs, varying K:

| K | wall | per extra CHECKSIG |
|---|---|---|
| 1  | 1.11 s | — |
| 10 | 2.42 s | ~73 µs |
| 50 | 8.95 s | ~80 µs |

If the cost were `O(N²·K)` (full re-hash every CHECKSIG), K=50 would be ~50× K=1.
It is 8×, and the per-extra-CHECKSIG cost (~75 µs) matches ECDSA verification,
not a re-serialization of the ~82 KB preimage. This is consistent with the
source analysis: sighash serialization is cached per-input; ECDSA is not cached
during block connection.

## 3. Relay and mining policy (not consensus)

The 2,500-legacy-sigop-per-transaction limit (`MAX_TX_LEGACY_SIGOPS`,
`CheckSigopsBIP54` in `policy.cpp`) is mempool/template policy in v31.1.0, **not**
a block-consensus rule. A poison block is nonstandard but consensus-valid.

## 4. BIP 54 (the proposed consensus fix)

* BIP 54 adds a *consensus* limit of 2,500 legacy sigops per non-coinbase
  transaction. As of v31.1.0 it is **not** merged; only the mempool-policy check
  exists.
* A build with PR #35793 rejects the poison transaction with
  `bad-txns-legacy-sigops`. The project records `outcome.bip54_result` as
  `live` (a supplied binary actually rejected it), `inferred` (predicted from the
  sigop count, no binary tested), or `not_tested`.

## 5. Published benchmark results (Portland HODL, OP_NEXT 2025)

These are third-party claims; the exact generator is not public. We reproduce the
mechanism, the scaling law, and a minute-scale single block on this hardware, but
we do not claim to match Portland's absolute worst-case figures.

## 6. Propagation / network-consequence measurements

`propagate` peers a miner with observer nodes over loopback-only P2P. Terminology:

* `time_to_tip_seconds` — miner `submitblock` → observer active-tip transition.
* `miner_validation_seconds` — the miner's `submitblock` wall time.
* `post_miner_time_to_tip_seconds` — `time_to_tip` minus the miner's validation.

These are **not** pure wire propagation; they include observer validation and tip
activation. P2P wire transmission alone is not isolated without instrumentation.

**P2P event instrumentation (defensible subset).** Observer nodes run with
`-debug=net`; the benchmark parses their debug logs for `received block <hash>`
and `UpdateTip: new best=<hash> height=<n>` lines, recording log timestamps.
Limitation: these reflect when the node *logged* receipt/activation, not the
exact instant the block arrived on the wire, and log timestamps have 1-second
granularity unless `-logtimestamps`/`-logthreadnames` produce sub-second marks.
The project does not overclaim exact validation start/end timestamps.

**Line-topology measurement (hop compounding).** On a `MINER -> A -> B -> C`
line, each observer reaches the poison tip ~one validation delay after its
upstream, so end-to-end delay compounds across hops. This is a controlled,
reproducible demonstration on disposable local regtest nodes.

## 7. What this does not reproduce (limitations)

* Portland's exact worst-case figures and generator.
* The `scriptSig`/P2SH family (documented in `vectors/scriptsig.py`; not
  implemented because the exact generator is not public).
* Large-scale (hundreds-of-nodes) peer topology — we support star/line/tree up to
  modest node counts; methodology is prioritized over raw scale.
* Pure wire-propagation isolation without P2P instrumentation.

## 8. Security model

pba-bench cannot reach a public network: regtest-only, loopback-only, fresh
disposable datadirs, no DNS seeds, no peer discovery, no public-network mode, and
runtime verification of `getblockchaininfo.chain == "regtest"`.
