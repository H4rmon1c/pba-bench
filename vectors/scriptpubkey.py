"""The scriptPubKey worst-case validation vector.

This is the construction implemented by ``construction.py``: a single poison
transaction spends N UTXOs whose bare ``scriptPubKey`` is a chain of
``OP_DUP <pub> OP_CHECKSIGVERIFY ... <pub> OP_CHECKSIG``, satisfied by one
signature. First demonstrated publicly by Portland HODL; this is a defensible
reproduction of the mechanism (not a byte-for-byte copy, since the exact
generator is not public).
"""

from __future__ import annotations

from vectors.base import Vector


class ScriptPubKeyVector(Vector):
    name = "scriptpubkey"
    description = (
        "A poison transaction spends N UTXOs whose bare scriptPubKey runs K "
        "CHECKSIG/CHECKSIGVERIFY ops satisfied by a single signature."
    )
    implemented = True
    reproducible_from_public_info = True

    preparation_requirements = [
        "N coinbase-derived UTXOs with an identical bare scriptPubKey of "
        "OP_DUP <pub> CHECKSIGVERIFY ... <pub> CHECKSIG (K ops each)",
        "1 prep transaction per prep block (sigop-capped: K*utxos <= 20000/block)",
        "Poison transaction with N inputs, each spending one such UTXO",
    ]

    theoretical_counters = [
        "legacy sighash serialization + double-SHA256: O(N^2) (per-input "
        "SigHashCache collapses the K-1 repeated CHECKSIG within an input)",
        "ECDSA verification: O(N*K) (one fresh verify per CHECKSIG during block "
        "connect; the signature cache is not populated then)",
        "script interpreter stack/loop overhead: O(N*K)",
        "hypothetical no-midstate-cache serialization: O(N^2*K)",
    ]

    expected_properties = [
        "consensus-valid on regtest (and, by identical consensus code, on mainnet)",
        "poison tx's own scriptSigs are push-only -> poison block stays under the "
        "per-block sigop cap",
        "exceeds the 2500-legacy-sigop BIP 54 limit when N*K > 2500",
        "nonstandard (mempool policy) -> must be placed in a block via submitblock",
    ]

    safety_constraints = [
        "regtest-only, loopback-only, disposable datadir",
        "N bounded by block weight (~1 MB base size)",
        "K bounded by MAX_OPS_PER_SCRIPT (201 ops per script -> K <= 101)",
        "scriptPubKey <= MAX_SCRIPT_SIZE (10,000 bytes) to stay spendable",
    ]
