"""The scriptSig / P2SH slow-validation vector (documented, not implemented).

The disclosed Portland HODL *scriptSig* family places the pathological CHECKSIG
chain in a P2SH redeem script spent by a poison transaction, so the executed
script (and its sigop contribution to the poison block's own accounting) lives in
the *spending* input rather than the spent output. This is the same class of
script as the BIP 54 rationale's "CHECKSIG DROP CHECKSIG DROP ..." redeem
scripts.

This vector is **not implemented** here for two reasons, stated plainly:

1. Portland HODL's exact generator is not public, so we cannot reproduce it
   byte-for-byte from public information.
2. Correctly building and safely verifying a P2SH slow-validation construction
   requires careful handling of the per-input SigHashCache interaction (a P2SH
   redeem script's repeated identical CHECKSIGs within one input are likewise
   collapsed by the midstate cache, so its cost model differs from a naive
   O(N^2*K)).

We deliberately do not invent Portland HODL's unpublished generator. This vector
documents the disclosed family so the architecture is ready when a safe,
publicly-verifiable construction is available.
"""

from __future__ import annotations

from vectors.base import Vector


class ScriptSigVector(Vector):
    name = "scriptsig"
    description = (
        "Disclosed P2SH/scriptSig slow-validation family: the CHECKSIG chain "
        "lives in a spent redeem script. NOT implemented (no public generator)."
    )
    implemented = False
    reproducible_from_public_info = False

    preparation_requirements = [
        "P2SH UTXOs whose redeem script is a CHECKSIG/DROP-style chain",
        "Poison transaction whose inputs spend those P2SH UTXOs with the "
        "pathological redeem script as the executed script",
        "Per-input SigHashCache interaction must be measured, not assumed",
    ]

    theoretical_counters = [
        "depends on the specific redeem-script layout; per-input midstate cache "
        "collapses repeated identical CHECKSIG within an input, so ECDSA is the "
        "K-scaling term, as in the scriptPubKey vector",
        "must be verified empirically against the actual construction",
    ]

    expected_properties = [
        "exercises P2SH (BIP 16) script execution on the spending input",
        "legacy sighash uses the redeem script as scriptCode",
        "exceeds BIP 54 legacy-sigop accounting when the redeem script has "
        "> 2500 CHECKSIG",
    ]

    safety_constraints = [
        "regtest-only, loopback-only, disposable datadir",
        "redeem script <= MAX_SCRIPT_SIZE and <= MAX_OPS_PER_SCRIPT to be valid",
        "must be reproduced from verifiable public information before enabling",
    ]
