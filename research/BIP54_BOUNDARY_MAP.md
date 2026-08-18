# BIP 54 (Consensus Cleanup) — consensus-boundary map

Every BIP 54 rule is mapped to its implementation in the tested Bitcoin Core
build, its regtest behaviour, and the resource it bounds. Boundaries are
labelled **LIVE ACCEPTED** / **LIVE REJECTED** (observed with a real
BIP54-enabled binary) or **STATICALLY INFERRED** (read from source only).

## Tested binary (BIP54 reference implementation)

| Field | Value |
|---|---|
| Repository | https://github.com/bitcoin/bitcoin |
| Implementation | Bitcoin Core PR #35793 "Implement BIP 54 (Consensus Cleanup) without mainnet activation" |
| Branch / ref | `refs/pull/35793/head` |
| Commit SHA | `9630491bf2135d03dac586d3492cfca9939f6fbb` |
| Build config | cmake Release, `-DBUILD_BITCOIN_WALLET=OFF -DENABLE_IPC=OFF`, gcc 14.2.0 |
| Compiler | GCC 14.2.0 (Debian 14.2.0-19), C++20 |
| `bitcoind --version` | `Bitcoin Core daemon version v31.99.0-9630491bf213 bitcoind` |
| binary SHA-256 (bitcoind) | `a4468b31145bd21546bba4aeb1bd75e302a6c94431c676fcf7de424407cc384f` |

Activation on regtest: BIP9 deployment `consensuscleanup`, forced with
`-vbparams=consensuscleanup:0:3999999999`. It is **not** active by default on
regtest; it must be mined through the versionbits cycle (~430 blocks) as
`bip54.activate_bip54()` does.

> **Note on the coinbase locktime rule:** the BIP 54 *text* says the coinbase
> `nLockTime` must equal `height - 15`, but the PR #35793 implementation at
> `9630491bf` enforces `nLockTime == height - 1` (`src/validation.cpp`,
> `bad-cb-locktime`). This research characterises the *implementation*.

## Rule map

| BIP54 rule | source file:function | constant | reject reason | regtest behaviour | resource bounded | status |
|---|---|---|---|---|---|---|
| Per-transaction legacy sigop limit | `src/consensus/tx_verify.cpp` `CheckSigopsBIP54` (called from `CheckTxInputs` with `enforce_bip54`) | `MAX_TX_BIP54_SIGOPS = 2500` (`src/consensus/consensus.h:44`) | `bad-txns-legacy-sigops` | >2500 legacy sigops in one non-coinbase tx is rejected; 2500 exactly is accepted | executed ECDSA / sigops per transaction | **LIVE** (see below) |
| Timewarp (first block of retarget period) | `src/validation.cpp` (timestamp check) | `MAX_TIMEWARP_BIP54 = 2*60*60` | `time-timewarp-attack` | first block of a retarget period must be within 2h of the previous block | timestamp manipulation / difficulty | STATICALLY INFERRED |
| Murch–Zawy (negative difficulty interval) | `src/validation.cpp` | — | `time-negative-interval` | retarget-period elapsed time cannot be negative | timestamp manipulation | STATICALLY INFERRED |
| Coinbase nLockTime | `src/validation.cpp` (~line 4205) | `nLockTime == height - 1` (PR revision) | `bad-cb-locktime` | coinbase nLockTime must equal height-1 | duplicate-txid (BIP30) | **LIVE** (activation blocks use it) |
| Coinbase non-final sequence | `src/validation.cpp` | `nSequence != SEQUENCE_FINAL` | `bad-cb-sequence` | coinbase input must not be final | duplicate-txid (BIP30) | STATICALLY INFERRED |
| 64-byte transactions invalid | `src/validation.cpp` (`b1ec5a6f4`) | witness-stripped size == 64 | `bad-txns-size` | 64-byte tx rejected (incl. coinbase) | Merkle-tree node ambiguity | STATICALLY INFERRED |

## Sigop-limit boundary (LIVE)

The per-transaction legacy-sigop limit is the rule most relevant to pathological
validation cost. `CheckSigopsBIP54` counts, per input, `scriptSig.GetSigOpCount`
plus the *spent* `scriptPubKey.GetSigOpCount` (BIP16-accurate: every `CHECKSIG`
= 1; `OP_1..OP_16 CHECKMULTISIG` = 1..16; all other `CHECKMULTISIG` = 20), and
rejects the transaction once the running total exceeds 2500.

| sigops in one tx | construction | node result | classification |
|---|---|---|---|
| 2500 | 25 P2PK inputs, K=100 CHECKSIG each | accepted | **LIVE ACCEPTED** |
| 2500 | 12 inputs, 1-of-17 CHECKMULTISIG (200 sigops each) | accepted | **LIVE ACCEPTED** |
| >2500 | original poison: N=200 × K=100 = 20,000 in one tx | rejected `bad-txns-legacy-sigops` | **LIVE REJECTED** |
| 2525 | 25 P2PK inputs, K=101 (exceeds ops limit anyway) | rejected | STATICALLY INFERRED |

The boundary is *strictly* `> 2500` (2500 is accepted). Verified live on the
`9630491bf` build.

## Key architectural facts (source-verified)

1. **The per-block sigop cap does not count spent-scriptPubKey sigops.**
   `ConnectBlock` (`src/validation.cpp:2585`) accumulates
   `GetTransactionSigOpCost` = `GetLegacySigOpCount(tx)` (the block's own
   scriptSigs and vout scriptPubKeys) + P2SH + witness. A poison transaction
   whose scriptSigs are push-only therefore contributes ~0 to the block sigop
   cap no matter how many `CHECKSIG`/`CHECKMULTISIG` are in the *spent*
   prevouts. So the 20,000-legacy-sigop block cap does **not** bound the total
   executed ECDSA work of a poison block.
2. **BIP54 is a per-transaction limit (2500), not a per-block limit.** It
   forbids concentrating >2500 sigops in one transaction, but says nothing about
   the *sum* across many transactions in one block.
3. **`OP_CHECKMULTISIG` adds `nKeysCount` to `MAX_OPS_PER_SCRIPT`.**
   `src/script/interpreter.cpp:1129` (`nOpCount += nKeysCount`). A 1-of-17
   `CHECKMULTISIG` costs 1 (opcode) + 17 (keys) = 18 ops and counts as 20 BIP54
   sigops, but performs up to 17 ECDSA verifies (valid pubkey checked last).
4. **BIP54 sigop accounting is `GetSigOpCount(fAccurate=true)`** (16 for
   `OP_16 CHECKMULTISIG`), while the block sigop cap uses
   `GetSigOpCount(fAccurate=false)` (20 for every CHECKMULTISIG). This matters
   for prep-block sizing.

## Consequences for the worst case (see POST_BIP54_WORST_CASE.md)

- The original single-transaction poison (`N*K > 2500`) is stopped live.
- A BIP54-*valid* block can still execute ~1.6M legacy sigops (spread across
  ~700 small transactions), reaching ~90 s of single-threaded validation on the
  benchmark hardware — *worse* than the pre-BIP54 single-transaction poison.
- The mechanism is the combination of facts 1 and 2: the per-tx cap is not a
  per-block bound, and `CHECKMULTISIG` packs ~20 sigops per opcode.
