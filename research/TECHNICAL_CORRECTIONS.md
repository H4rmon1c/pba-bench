# Technical corrections to the pre-audit model

This file records the findings of the Phase 0 source audit that contradict or
refine the original documentation. Every claim here was verified against the
vendored Bitcoin Core source (`bitcoin-src/`) for the tested version (v31.1.0)
and, where noted, against direct measurement on the benchmark's own node.

## 1. The headline complexity `O(N²·K)` is wrong for v31.1.0

The original documentation claimed that "every `CHECKSIG` re-hashes the whole
transaction" and summarized the validation cost as `O(N²·K)` hashing work.

Two source facts contradict this:

### 1a. `SigHashCache` caches the SHA-256 midstate per input
`src/script/interpreter.h` (lines 255-270) and `src/script/interpreter.cpp`
(lines 1582-1685) define a per-`GenericTransactionSignatureChecker` cache of the
SHA-256 midstate keyed by `(hashType, scriptCode)`. A fresh checker (and thus a
fresh cache) is created for **every input** (`validation.cpp:2028`, inside
`CScriptCheck::operator()`). On a repeated `CHECKSIG` with the same `hashType`
and `scriptCode` *within the same input*, `SignatureHash` loads the cached
midstate and only finalizes:

```cpp
if (sighash_cache && sighash_cache->Load(nHashType, scriptCode, ss)) {
    ss << nHashType;
    return ss.GetHash();
}
```

So the full preimage is **serialized and fed through SHA-256 only once per
input** (on the input's first `CHECKSIG`). The remaining `K-1` repeated
`CHECKSIG`s in that input do not re-serialize or re-hash the transaction. The
`HashWriter` stores only the SHA-256 state, not the preimage bytes
(`src/hash.h:108`), so the cache copy is O(1).

**Consequence:** legacy sighash serialization + double-SHA-256 work is `O(N²)`,
independent of `K`.

### 1b. The signature cache is not written during block connection
`src/validation.cpp:2584` (inside `ConnectBlock`):

```cpp
bool fCacheResults = fJustCheck; /* Don't cache results if we're actually connecting blocks (still consult the cache, though) */
```

During real block connection (`submitblock` accepting the block), `fJustCheck`
is false, so `cacheSigStore=false`. In
`CachingTransactionSignatureChecker::VerifyECDSASignature`
(`src/script/sigcache.cpp:63`) with `store=false` the signature cache is
consulted (with erase) but **never populated**:

```cpp
if (m_signature_cache.Get(entry, !store)) return true;  // miss for a fresh block
if (!TransactionSignatureChecker::VerifyECDSASignature(vchSig, pubkey, sighash)) return false;
if (store) m_signature_cache.Set(entry);                // not executed during block connect
return true;
```

**Consequence:** every `CHECKSIG` performs a fresh ECDSA verification during
block validation. ECDSA work is `O(N·K)`, roughly 60-90 µs per verification on
the benchmark hardware.

### 1c. Measured confirmation
At N=2000 poison inputs, `-par=1`, on the benchmark's Xeon E5-2680:

| K | wall | delta vs K=1 | per extra CHECKSIG |
|---|---|---|---|
| 1  | 1.11 s | — | — |
| 10 | 2.42 s | +1.31 s | ~73 µs |
| 50 | 8.95 s | +7.84 s | ~80 µs |

If the cost were `O(N²·K)` (full re-hash every CHECKSIG), K=50 would be ~50×
K=1 (≈ 50-70 s). It is 8×. The per-extra-CHECKSIG cost (~75 µs) matches ECDSA
verification, not a re-serialization of the ~82 KB preimage. This is consistent
with "sighash serialization cached, ECDSA not cached."

### 1d. Corrected model
For the `scriptPubKey` vector on v31.1.0:

```
validation_cost  ≈  serialization + double-SHA-256 (O(N²))
                 +  ECDSA verification (O(N·K), ~70 µs each)
                 +  script-interpreter stack/loop overhead (O(N·K))
```

- The `O(N²)` term is set by `N` inputs each serializing/hashing an `O(N)`
  preimage once (the per-input `SigHashCache`).
- The `O(N·K)` term is set by one fresh ECDSA verification per `CHECKSIG`
  (the signature cache is not populated during block connection).
- The old `theoretical_sighash_preimage_bytes_no_cache = K ×` cache-aware
  preimage bytes describes a hypothetical implementation *without* the per-input
  midstate cache. It is a valid serialization bound for other/older
  implementations, but it does **not** describe what v31.1.0 hashes during block
  validation. It must be labeled as hypothetical.

This is the answer to "what does increasing K actually cost on modern Core after
SigHashCache is accounted for": it does not add serialization cost, but it adds a
full ECDSA verification per `CHECKSIG`.

## 2. `MAX_OPS_PER_SCRIPT` is a per-script budget, not a combined budget

`src/script/interpreter.cpp:2029-2034` evaluates the `scriptSig` and
`scriptPubKey` in **two separate `EvalScript` calls**, each initialising its own
`nOpCount = 0` (`interpreter.cpp:441`). The 201 non-push-operation limit
(`interpreter.cpp:462`) therefore applies to **each script independently**, not
to their sum.

For this construction the `scriptSig` is a single push (0 non-push ops) and the
`scriptPubKey` contributes `2K-1` non-push ops (`K-1` `OP_DUP` + `K-1`
`OP_CHECKSIGVERIFY` + `1` `OP_CHECKSIG`). The binding constraint is
`2K-1 ≤ 201`, i.e. `K ≤ 101`. The old text described the limit as "the
scriptSig + scriptPubKey may execute at most 201 non-push ops" — numerically it
happens to give the same K cap, but the wording is wrong and a reviewer would
object to it.

## 3. Other confirmed behaviors (unchanged)

- `MAX_SCRIPT_SIZE = 10000`: an output script > 10 KB is treated as unspendable
  (`src/script/script.h:564-566`) and is not stored in the UTXO set. Correct as
  documented.
- The poison transaction's own `scriptSig`s are push-only (no `CHECKSIG`
  opcodes), so the poison block stays under the per-block sigop cap
  (`MAX_BLOCK_SIGOPS_COST = 80000` weight units = 20000 legacy sigops,
  `src/consensus/tx_verify.cpp`). Correct as documented.
- BIP 54 (2,500 legacy sigops per non-coinbase tx, consensus) is not merged in
  v31.1.0; only the mempool-policy `CheckSigopsBIP54` exists
  (`src/policy/policy.cpp`). Correct as documented.

## 4. Measurement terminology

The original `propagate` code measured "propagation" as the interval from miner
`submitblock` return until the observer's active tip changed. That interval
includes the observer's block validation and tip activation, so it is not pure
P2P wire propagation. The corrected reports split this into
`miner_validation_seconds`, `post_miner_time_to_tip_seconds`, and
`time_to_tip_seconds`, and do not claim to have isolated pure wire transmission
without P2P instrumentation.
