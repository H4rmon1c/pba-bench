# What pba-bench actually proves

A precise, honest statement of the claims this benchmark establishes, separated
from the claims it does not establish. Every quantity is labelled
**measured**, **derived/calculated**, **inferred**, or **external claim**.

## 1. The claim, in one sentence

**Bitcoin's current consensus rules admit blocks that are fully valid but whose
validation is pathologically expensive — and this project builds, submits, and
measures them against real `bitcoind` binaries, and compares how different node
versions, mitigations, and local peers experience them.**

## 2. What is directly measured

1. **Existence of a worst-case-validation block.** We construct a block and submit
   it to Bitcoin Core via `submitblock`. The node accepts it as consensus-valid (it
   becomes the active chain tip). It is not malformed or invalid. *(measured)*

2. **The pathological cost is real.** A block with `N` poison inputs, each spending
   a scriptPubKey that executes `K` CHECKSIG ops, validates slowly. At
   `N=8500, K=100` the block validated in ~85 s single-threaded on a Xeon E5-2680.
   *(measured)*

3. **The cost model is `O(N²)` serialization+hashing plus `O(N·K)` ECDSA**, not
   `O(N²·K)`. This is verified against source (the per-input `SigHashCache`
   collapses repeated identical `CHECKSIG`s within an input; the signature cache
   is not populated during block connection) and confirmed by measurement (at
   `N=2000`, raising K from 1 to 50 raises validation time ~8×, not ~50×). The
   `O(N²·K)` figure describes a hypothetical implementation without the midstate
   cache. *(measured + derived; see analysis.md)*

4. **The consequence is local resource exhaustion.** During validation, the node's
   validation threads are saturated; lightweight RPC calls block until validation
   finishes; the node works on a stale tip. *(measured)*

5. **Heterogeneous observers and topologies.** The same identical block is
   observed by nodes with different `-par` values and across star/line/tree
   loopback-only topologies; each observer gets an independent measurement
   context. On a line, time-to-tip compounds across hops. *(measured)*

## 3. The precise mechanism (verified against the source)

- Legacy signature hashing serializes and double-SHA-256s the whole transaction per
  input. The per-input `SigHashCache` caches the SHA-256 midstate, so repeated
  identical `CHECKSIG`s within an input do not re-serialize/re-hash.
  → serialization/hashing `O(N²)`.
- During block connection the signature cache is consulted but not populated, so
  every `CHECKSIG` does a fresh ECDSA verification. → ECDSA `O(N·K)`.
- The poison transaction's own inputs are push-only, so the poison block stays
  under the per-block sigop cap and is consensus-valid.

## 4. Why this is a valid proof without touching a public network

Block validity is a local, deterministic property of consensus rules. Regtest uses
the identical consensus code (script interpreter, sighash, sigop counting) as
mainnet. Therefore a block that is consensus-valid on regtest is consensus-valid on
mainnet, and its validation cost is a property of the algorithm, not the network. A
public-testnet run would add propagation-consequence measurements but would not make
the core finding "more real" — so we keep it regtest-only and avoid CPU-saturating
third-party nodes.

## 5. What this does NOT prove (read this too)

| Not proven | Why |
|---|---|
| Portland HODL's worst-case figure, reproduced exactly | The exact generator is not public; and consensus limits cap a *single* block. We reproduce the mechanism, the scaling, and a minute-scale single block, not the worst-case figure on this hardware. |
| Full peer-to-peer topology at scale | We support star/line/tree up to modest node counts; methodology is prioritized over raw scale. |
| That BIP 54's consensus fix rejects the block, unless tested | BIP 54 is not merged in v31.1.0. `compare --bip54 PATH` tests a real BIP54 binary and reports `live` if it rejects the block; otherwise the rejection is `inferred`. |
| The `scriptSig`/P2SH family | Documented in `vectors/scriptsig.py`; not implemented because the exact generator is not public. |
| Pure wire propagation isolation | `time_to_tip` includes observer validation and tip activation. P2P wire transmission alone is not isolated without instrumentation. |

## 6. An original finding: the consensus limits that bound a single block

Three consensus constraints cap how expensive a single poison block can be in
current Core:

| Limit | Value | Consequence |
|---|---|---|
| `MAX_SCRIPT_SIZE` | 10,000 B | An output script >10 KB is unspendable and not stored in the UTXO set → caps the spent `scriptCode`. |
| `MAX_OPS_PER_SCRIPT` | 201 ops **per script** | Caps executed signature checks per input (~101 for our construction, `2K-1 ≤ 201` in the scriptPubKey's own budget). |
| Block weight | 4,000,000 | Caps poison inputs `N` to ~8,500. |

Together these bound a single block on this hardware. Reaching minutes-to-hours
would require a chain of such blocks, much slower hardware, or a construction that
defeats the per-input midstate cache more aggressively.

## 7. Reproduce it yourself

```bash
# single-node, corrected cost model
.venv/bin/python ./pba_bench.py benchmark --bitcoind "$(which bitcoind)" \
    --profile custom --num-utxos 8500 --sigops-per-input 100 --par 1 --confirm

# what does K cost after SigHashCache? (sweep K at fixed N)
.venv/bin/python ./pba_bench.py sweep --bitcoind "$(which bitcoind)" \
    --axis k --fixed 2000 --values 1,5,25,50,100 --runs 3

# five independent observers, star
.venv/bin/python ./pba_bench.py propagate --bitcoind "$(which bitcoind)" \
    --observers 5 --observer-par 1 --topology star --num-utxos 3000 \
    --sigops-per-input 100 --confirm
```

Full commands and all measured results are in the [README](../README.md) and
[BENCHMARKS.md](../BENCHMARKS.md).
