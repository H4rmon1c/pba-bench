# What pba-bench actually proves

A precise, honest statement of the claims this benchmark establishes, separates
from the claims it does not establish.

## 1. The claim, in one sentence

**Bitcoin's current consensus rules admit blocks that are fully valid but whose
validation is pathologically expensive — and we build one and measure it: a
consensus-valid block that takes ~85 seconds to validate single-threaded on a
mid-range Xeon (a normal block takes ~1 ms).**

## 2. What is directly demonstrated (measured)

1. **Existence of a poison block.** We construct a block and submit it to Bitcoin
   Core v31.1.0 with `submitblock`. The node **accepts it as consensus-valid**
   (it becomes the active chain tip). It is not a malformed or invalid block.

2. **The pathological cost is real and attributable to the mechanism.** The block
   contains a transaction with `N = 8500` inputs; each input's spent `scriptPubKey`
   executes `K = 100` signature checks. Every legacy `CHECKSIG` re-hashes the entire
   8500-input transaction (with the spent `scriptPubKey` as `scriptCode`). Total
   legacy sighash preimage bytes: **~2.99 GB**. Measured validation:
   **85 s single-threaded**, **~94 s CPU** (6.2 s wall on 16 parallel threads).

3. **The scaling law.** Varying only the number of inputs `N`, the total sighash
   preimage bytes grow as `N²` (10.3 → 41.1 → 164.1 MB for N = 500 → 1000 → 2000),
   and wall/CPU time grows superlinearly. This is the quadratic complexity that
   makes the attack scale.

4. **The consequence is local resource exhaustion.** During validation the node's
   validation threads are saturated; lightweight RPC calls issued while the block is
   validating block until it finishes. On a single-core or weak node the same block
   takes proportionally longer (the 85 s figure is already single-threaded).

## 3. The precise mechanism (verified against the source)

- Legacy (pre-SegWit) signature hashing serializes and double-SHA256s the *whole
  transaction* per signature check (`SignatureHash`). That serialization is
  `O(number of inputs)`.
- A poison transaction with `N` inputs each executing `K` `CHECKSIG`s therefore does
  `N·K` hashes of an `O(N)`-sized preimage ≈ `O(N²·K)` hashing work.
- Two caches exist but do **not** remove the cost: the per-input **sighash cache**
  skips re-*serialization* on repeated `CHECKSIG`s, and the global **signature cache**
  skips repeated ECDSA math, but **every `CHECKSIG` still re-hashes the full preimage**
  (`HashWriter::GetHash()` is not cached). The expensive step survives.
- The poison transaction's own inputs contain only signature pushes, so the poison
  *block* stays under the per-block sigop limit and is consensus-valid.

## 4. Why this is a valid proof without touching a public network

Block validity is a **local, deterministic** property of consensus rules. Regtest uses
the *identical* consensus code (script interpreter, sighash, sigop counting) as
mainnet. Therefore a block that is consensus-valid on regtest is consensus-valid on
mainnet, and its validation cost is a property of the algorithm, not the network. A
public-testnet run would add propagation-consequence measurements but would not make
the core finding "more real" — so we keep it regtest-only and avoid CPU-saturating
third-party nodes.

## 5. What this does NOT prove (read this too)

| Not proven | Why |
|---|---|
| Portland HODL's ~25-minute worst case, reproduced exactly | The exact generator is not public; and three consensus limits cap a *single* block (see §6). We reproduce the mechanism, the scaling, and a minute-scale (85 s) single block, not the 25-minute figure on this hardware. |
| Full peer-to-peer *topology* at scale | We demonstrate single-peer propagation on a small private loopback network (~97 s to a peer, stale tip, blocked RPC); the 0xB10C multi-peer signet topology is not reproduced at scale. |
| That BIP 54's consensus fix rejects the block | BIP 54 is not yet merged in v31.1.0; we record `bip54_would_reject` as an inference (850k sigops ≫ 2,500) and support a BIP-54 binary via `--bitcoind` for a live test. |
| The `scriptSig`/P2SH family of poison blocks | We reproduce the `scriptPubKey` family (the demonstrated worst case). |

## 6. An original finding: the consensus limits that bound a single block

While calibrating, we verified three consensus constraints that cap how expensive a
single poison block can be in current Core:

| Limit | Value | Consequence |
|---|---|---|
| `MAX_SCRIPT_SIZE` | 10,000 B | An output script >10 KB is *unspendable* and not stored in the UTXO set → caps the spent `scriptCode`. |
| `MAX_OPS_PER_SCRIPT` | 201 ops | Caps executed signature checks per input (~100 for our construction). |
| Block weight | 4,000,000 | Caps poison inputs `N` to ~8,500. |

Together these cap a single block at ~850k sigops ≈ 85 s single-threaded on this
hardware. Reaching minutes-to-hours would require a chain of such blocks, much slower
hardware (e.g. a Raspberry Pi), or a construction that defeats the sighash cache more
aggressively. This is a useful, honest bound on the single-block blast radius.

## 7. Reproduce it yourself

```bash
cd pba-bench
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python ./pba_bench.py benchmark --bitcoind "$(which bitcoind)" \
    --profile custom --num-utxos 8500 --sigops-per-input 100 --par 1 --confirm
```

Expected output: `outcome: accepted  wall=85s  cpu=85s`. Full commands, safety, and
all measured results are in the [README](../README.md) and
[BENCHMARKS.md](../BENCHMARKS.md).
