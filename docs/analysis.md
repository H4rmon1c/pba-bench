# pba-bench: technical analysis

This document explains the poison-block construction, separates what is *confirmed*
from what is *inferred* or *unknown*, and compares our observed results with the
published Portland HODL figures.

---

## 1. Confirmed consensus behavior (from Bitcoin Core source, v31.1.0)

These statements are verified against the Bitcoin Core source in this repository
(`src/script/interpreter.cpp`, `src/script/script.cpp`, `src/consensus/tx_verify.cpp`).

* **Legacy signature hashing re-serializes the whole transaction.** For a legacy
  (pre-SegWit) signature, `SignatureHash` serializes the entire transaction with every
  input's `scriptSig` emptied except the one being signed, which is replaced by the
  spent `scriptPubKey` (the `scriptCode`). The preimage is then hashed twice with
  SHA-256. So the cost of one legacy signature check is `O(transaction size)`.
* **The sighash preimage cache is per-input.** `GenericTransactionSignatureChecker`
  owns a small `SigHashCache` (keyed by `hashType` + `scriptCode`). It is constructed
  once per input, so multiple `CHECKSIG`s in the *same* input with the same
  `scriptCode` and `hashType` share one serialization. Different inputs have different
  `scriptCode`s, so each input pays its own full serialization.
* **Block sigop cap.** The consensus per-block sigop limit (`MAX_BLOCK_SIGOPS_COST =
  80,000` weight-units, i.e. 20,000 legacy sigops) counts `CHECKSIG`/`CHECKMULTISIG`
  in each transaction's `scriptSig` and `scriptPubKey` (and P2SH redeem scripts).
  It does **not** count the sigops in *spent* UTXOs' `scriptPubKey`s. A poison
  transaction whose own `scriptSigs` contain only signature pushes therefore
  contributes ~0 sigops to the block it is mined in, and is consensus-valid.
* **Block size cap.** The consensus block weight limit is 4,000,000 (weight =
  3×base size + total size). A non-witness block's base size is therefore capped at
  ~1,000,000 bytes. This bounds the number of inputs a single poison transaction can
  contain.
* **`MAX_SCRIPT_SIZE` (10,000 bytes) caps the scriptPubKey.** An output whose
  `scriptPubKey` is larger than `MAX_SCRIPT_SIZE` is treated as *unspendable*
  (`CScript::IsUnspendable`) and is **not stored in the UTXO set**. This bounds the
  size (and therefore the CHECKSIG count) of a poison UTXO's spent scriptCode to
  10,000 bytes. In our construction that is ~277 CHECKSIG ops per input.
* **`MAX_OPS_PER_SCRIPT` (201) caps executed operations.** The combined
  scriptSig + scriptPubKey may execute at most 201 non-push operations
  (`block-script-verify-flag-failed: Operation limit exceeded`). Our
  `DUP … CHECKSIGVERIFY` pattern uses two non-push ops per CHECKSIG, so it is capped
  at ~100 CHECKSIG ops per input — tighter than the 10,000-byte scriptPubKey limit.
  This is the binding constraint on `sigops_per_input` for this construction.
* **The signature cache collapses repeated signatures, not the sighash.** Bitcoin
  Core's per-input sighash cache and the global signature cache do not remove the
  expensive part: every `CHECKSIG` re-hashes the full legacy sighash preimage. The
  signature cache only skips the ECDSA math when the same `(sig, pubkey, sighash)` is
  seen again.

## 2. Relay and mining policy (not consensus)

These are mempool/mining-policy limits, not consensus rules. A poison block is
nonstandard but remains consensus-valid.

* **2,500 legacy sigops per transaction (policy).** Bitcoin Core's `policy.h`
  defines `MAX_TX_LEGACY_SIGOPS = 2500` and `policy.cpp::CheckSigopsBIP54` enforces
  it at mempool acceptance (and template building). It is *not* a block-consensus
  rule in v31.1.0.
* The poison transaction exceeds this, so it cannot be relayed or mined normally; it
  must be placed in a block by a miner (or by `submitblock` in our benchmark).

## 3. BIP 54 (the proposed consensus fix)

* BIP 54 (spec at https://bips.dev/54/) adds a *consensus* limit of 2,500 legacy
  sigops per non-coinbase transaction, counting `CHECKSIG`/`CHECKSIGVERIFY` as 1 and
  `CHECKMULTISIG`/`CHECKMULTISIGVERIFY` as 1..20 (20 unless immediately preceded by
  `OP_1`..`OP_16`), whether or not the op is executed.
* As of Bitcoin Core v31.1.0 the consensus rule is **not** merged (the deployment
  `consensuscleanup` and the `bad-txns-legacy-sigops` rejection are absent). It is
  implemented only as the mempool-policy check described above.
* A build with PR #35793 would reject the poison transaction with
  `bad-txns-legacy-sigops`. We record `outcome.bip54_would_reject` as an inference
  (reject iff BIP54-accounted sigops > 2500); testing the live rejection requires
  supplying such a binary via `--bitcoind`.

## 4. Published benchmark results (Portland HODL, OP_NEXT 2025)

From the public gist and secondary write-ups. **These are third-party claims; the
exact generator is not public.**

* `scriptPubKey`-based construction.
* ~22,000 preparation transactions, ~150 blocks of preparation data.
* ~4 million signature operations in total.
* ~25–29 minutes of validation on older Xeon hardware; ~11+ hours on Raspberry Pi-class
  hardware.

We could not obtain the exact generator, so we did **not** copy its details. We built
the smallest defensible reproduction from the consensus behavior above.

## 5. Our inferences

* The pathological cost is the **quadratic serialization** of legacy sighashes:
  `N` inputs, each serializing an `O(N)`-sized transaction preimage, gives `O(N²)`
  hashing work. We measure the total sighash preimage bytes and show they scale as
  `N²` (10.3 MB → 41.1 MB → 164.1 MB for N = 500 → 1000 → 2000).
* With the `OP_DUP <pub> CHECKSIGVERIFY ... <pub> CHECKSIG` construction (one
  signature satisfies K CHECKSIG ops per input), the two cost drivers are decoupled
  and total hashing scales as `N²·K`. This is what lets a single consensus-valid
  block reach minute-scale validation: `N` is bounded by the block size cap, `K` by
  the prep blocks' sigop cap.
* BIP 54's rationale explicitly calls out "CHECKSIG DROP CHECKSIG DROP..." redeem
  scripts as the pathological pattern it targets; our construction is the same
  class of script.
* We believe the reported "~4M signature operations" is the total sigop inventory
  created across the ~150 prep blocks (~150 × ~20,000/block ≈ 3M), not the number
  executed in a single block — a single block's size cap limits a poison transaction
  to ~10–50k executed sigops (fewer at higher K). We could not confirm this.

## 6. Details still unavailable or ambiguous

* The exact Portland generator (script layout, sigop distribution, number of inputs
  in the poison transaction) is not public.
* Whether the 25-minute figure was measured with the per-input sighash cache enabled
  is unknown; the cache materially reduces the `K` (sigops/input) factor while leaving
  the `N²` factor intact.
* The exact relationship between "~4M sigops" and the poison block's size is
  ambiguous given the block size cap.

## 7. Observed vs. Portland

| Quantity | Portland (published) | pba-bench (this run, v31.1.0, Xeon E5-2680) |
|---|---|---|
| Construction | `scriptPubKey` vector, ~22k prep txs, ~150 blocks | `scriptPubKey` vector (DUP/CHECKSIGVERIFY), 1 prep tx per block |
| Signature operations | ~4,000,000 (total) | 20 – 850,000 (per poison tx) |
| Validation time | ~25 min (Xeon); ~11 h (Pi) | **85 s single-threaded** / 6.2 s wall on 16 cores (N=8500, 850k sigops) |
| Scaling | reported "quadratic" | measured quadratic hashing (`N²` preimage bytes) |

We reproduce the **mechanism**, the **scaling law**, and now a **minute-scale
validation time on a single consensus-valid block** (85 s single-threaded). The gap
to Portland's 25 minutes is explained by three consensus limits we verified in the
current code that cap a single poison block's cost: `MAX_SCRIPT_SIZE` (10,000-byte
scriptPubKey), `MAX_OPS_PER_SCRIPT` (201 ops), and the block size/weight cap. With
those limits the largest single-block construction on this hardware is ~850k sigops ≈
85 s of single-threaded validation. Reaching Portland's 25 min would require a chain
of such blocks, slower hardware, or a construction that defeats the sighash cache
more aggressively.

## 8. What this does not reproduce (limitations)

* Portland's exact worst-case figures and generator.
* The `scriptSig`-family (P2SH) vector.
* Full peer-to-peer propagation *topology* (a large network of nodes) — we demonstrate
  single-peer propagation on a small private loopback network (see §10); the 0xB10C
  multi-peer signet measurement is not reproduced at scale.
* A live BIP 54 consensus rejection (needs a BIP 54 build).

## 10. Multi-node propagation (the `propagate` demo)

We also measure the *consequences* on a private loopback-only regtest network: a miner
peers with observer nodes over loopback P2P, and we time how a poison block reaches a
peer compared with a normal block. Measured (v31.1.0, Xeon E5-2680, `-par=1` observer):

| | normal block | poison (N=8500, 850k sigops) |
|---|---|---|
| propagation to peer | ~5 ms | ~97 s |
| peer tip stays stale | ~0 | ~97 s |
| peer RPC blocked (max) | — | ~30 s |

This is the network-level blast radius: a poison block stalls a peer's validation,
blocks its RPC, and keeps it working on a stale tip — the conditions that lead to
stale blocks and wasted mining work. It is the same phenomenon 0xB10C measured on
signet, reproduced locally and safely.

## 9. Security model

pba-bench **cannot** reach a public network:

* It always launches its own `bitcoind` subprocess with a fresh disposable regtest
  datadir and loopback-only RPC.
* It never connects to an existing node, never reuses a datadir, and never enables P2P
  networking (`-connect=0 -listen=0 -dnsseed=0 -discover=0`).
* No code path broadcasts transactions or blocks to public peers; there is no "public
  network" deployment mode to enable by accident or otherwise.
* The chain is forced to `-regtest` and verified at runtime via
  `getblockchaininfo.chain == "regtest"`; the run aborts otherwise.
* A prominent warning and the resolved datadir are printed before every run.
