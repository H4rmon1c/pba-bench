"""Deterministic poison-block construction for the pba-bench benchmark.

Reproduces the *scriptPubKey* family of worst-case legacy validation blocks
(first demonstrated publicly by Portland HODL).

Construction
------------
A poison transaction spends ``num_utxos`` UTXOs. Each UTXO has a *bare*
scriptPubKey of the form::

    OP_DUP <pub> OP_CHECKSIGVERIFY  OP_DUP <pub> OP_CHECKSIGVERIFY  ...  <pub> OP_CHECKSIG

i.e. ``sigops_per_input`` ``CHECKSIG``/``CHECKSIGVERIFY`` ops. Because each op
``OP_DUP``s the signature, the whole script is satisfied by a *single* signature:
::

    scriptSig: <sig>

This decouples the two cost drivers:

  * ``num_utxos`` (N) is bounded by the poison block's ~1 MB base-size cap
    (each input adds one 71-byte signature).
  * ``sigops_per_input`` (K) is bounded by the prep blocks' per-block sigop cap
    (each UTXO's scriptPubKey adds K sigops to its prep block).

For legacy (pre-SegWit) signature hashing, the spent ``scriptPubKey`` is used as
the ``scriptCode`` for the sighash. The cost model is described precisely in
:func:`PoisonBlockGenerator._metrics`; in short, Bitcoin Core v31.1.0 serializes
and double-SHA256s each input's O(N)-sized preimage once (the per-input
``SigHashCache`` collapses repeated identical ``CHECKSIG``s within an input), so
serialization/hashing is O(N^2), while each ``CHECKSIG`` still performs a fresh
ECDSA verification (the signature cache is not populated during block
connection), so signature verification is O(N*K).

The builder is optimized: it uses one shared key, computes legacy sighash
preimages with C-speed bytes splicing, and signs with coincurve (C-backed
secp256k1) when available, falling back to the pure-Python signer.
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from test_framework.blocktools import create_block, create_coinbase
from test_framework.key import ECKey, ECPubKey
from test_framework.messages import (
    COutPoint,
    CTransaction,
    CTxIn,
    CTxOut,
    hash256,
)
from test_framework.script import (
    CScript,
    OP_1,
    OP_CHECKSIG,
    OP_CHECKSIGVERIFY,
    OP_DUP,
    SIGHASH_ALL,
    LegacySignatureMsg,
)
from test_framework.script_util import key_to_p2pk_script

from safety import SafetyError

COIN = 100_000_000
#: Consensus block sigop cost cap (legacy sigops * 4 <= 80_000).
MAX_BLOCK_SIGOPS_COST = 80_000
#: Maximum block weight (consensus).
MAX_BLOCK_WEIGHT = 4_000_000
#: Number of confirmations before a coinbase is spendable.
COINBASE_MATURITY = 100
#: BIP 54 per-transaction legacy sigop limit.
BIP54_MAX_TX_LEGACY_SIGOPS = 2500
#: Consensus max script size. A scriptPubKey larger than this is treated as
#: unspendable and its output is NOT stored in the UTXO set, so a poison UTXO's
#: scriptPubKey (the spent scriptCode) must stay at or below this size.
MAX_SCRIPT_SIZE = 10000
#: Consensus max non-push operations per *single* script (interpreter.cpp
#: enforces it with a fresh nOpCount=0 for each EvalScript call). The scriptSig
#: and scriptPubKey are evaluated in separate EvalScript calls, so each gets its
#: own 201-op budget; it is NOT a combined budget. Our DUP/CHECKSIGVERIFY pattern
#: contributes 2K-1 non-push ops to the scriptPubKey, so K is capped at
#: (MAX_OPS_PER_SCRIPT+1)/2.
MAX_OPS_PER_SCRIPT = 201


def script_pubkey_size(k: int) -> int:
    """Serialized size of the DUP/CHECKSIGVERIFY scriptPubKey for ``k`` CHECKSIG ops."""
    if k < 1:
        return 0
    return 36 * (k - 1) + 35 if k > 1 else 35


class DeterministicKeys:
    """Generates ECKeys deterministically from a seed."""

    def __init__(self, seed: int):
        self._rng = random.Random(seed)

    def make_key(self) -> ECKey:
        while True:
            secret = self._rng.randbytes(32)
            k = ECKey()
            k.set(secret, True)
            if k.is_valid:
                return k


# --------------------------------------------------------------------------- #
# Signing (coincurve if available, else pure-Python)
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - depends on optional dependency
    from coincurve import PrivateKey as _CCPrivateKey
    _HAVE_COINCURVE = True
except Exception:  # pragma: no cover
    _CCPrivateKey = None
    _HAVE_COINCURVE = False


def _der_verify(sig_der, pubkey_bytes, msg_hash):
    """Verify a DER ECDSA signature using coincurve (if available)."""
    if not _HAVE_COINCURVE:
        return True
    from coincurve import PublicKey
    return PublicKey(pubkey_bytes).verify(sig_der, msg_hash, hasher=None)


def sign_der(secret_bytes: bytes, msg_hash: bytes) -> bytes:
    """Return a DER-encoded, low-S ECDSA signature over ``msg_hash``.

    Uses coincurve when available (fast), otherwise the pure-Python signer.
    """
    if _HAVE_COINCURVE:
        return _CCPrivateKey(secret_bytes).sign(msg_hash, hasher=None)
    k = ECKey()
    k.set(secret_bytes, True)
    return k.sign_ecdsa(msg_hash, rfc6979=True)


@dataclass
class ConstructionConfig:
    """Parameters of one poison-block construction."""

    seed: int = 1
    num_utxos: int = 10            # N: number of poison inputs / prep UTXOs
    sigops_per_input: int = 2      # K: executed CHECKSIG ops per input's scriptPubKey
    utxo_value_sats: int = 100_000
    change_script: str = "op_true"
    max_block_weight: int = MAX_BLOCK_WEIGHT
    max_prep_blocks: int = 0
    #: Use a seed-derived mocktime for fully deterministic blocks. Disable for
    #: multi-node runs (setmocktime suppresses new-block relay to peers).
    deterministic_time: bool = True

    def validate(self) -> None:
        if self.num_utxos < 1:
            raise ValueError("num_utxos must be >= 1")
        if self.sigops_per_input < 1:
            raise ValueError("sigops_per_input must be >= 1")
        if script_pubkey_size(self.sigops_per_input) > MAX_SCRIPT_SIZE:
            raise SafetyError(
                f"sigops_per_input={self.sigops_per_input} makes the scriptPubKey "
                f"({script_pubkey_size(self.sigops_per_input)} bytes) exceed "
                f"MAX_SCRIPT_SIZE ({MAX_SCRIPT_SIZE}); the output would be unspendable. "
                f"Maximum is {MAX_SCRIPT_SIZE // 36} CHECKSIG ops."
            )
        # Each CHECKSIG in the DUP/CHECKSIGVERIFY pattern contributes two non-push
        # ops (one OP_DUP plus one CHECKSIG/CHECKSIGVERIFY) to the scriptPubKey's
        # own 201-op budget. MAX_OPS_PER_SCRIPT applies per script, not to the
        # combined scriptSig+scriptPubKey.
        if 2 * self.sigops_per_input - 1 > MAX_OPS_PER_SCRIPT:
            raise SafetyError(
                f"sigops_per_input={self.sigops_per_input} would exceed "
                f"MAX_OPS_PER_SCRIPT ({MAX_OPS_PER_SCRIPT}) in the scriptPubKey; "
                f"the block would be rejected. Maximum for this construction is "
                f"{(MAX_OPS_PER_SCRIPT + 1) // 2}."
            )
        # Sanity cap: a single poison block can hold at most ~1 MB of signatures
        # and each UTXO's scriptPubKey is bounded by the prep block sigop cap, so
        # ~200M sigops is the theoretical ceiling for one block. Beyond that the
        # request is certainly a mistake. The real guards are the block-size and
        # per-block sigop caps plus --confirm and the --max-* limits.
        if self.num_utxos * self.sigops_per_input > 200_000_000:
            raise SafetyError(
                f"requested {self.num_utxos * self.sigops_per_input} legacy sigops in one "
                "transaction; refusing to build an unrealistically large case."
            )


# --------------------------------------------------------------------------- #
# Scripts
# --------------------------------------------------------------------------- #

def poison_script_pubkey(pubkey_bytes: bytes, k: int) -> CScript:
    """Build the bare scriptPubKey for one poison UTXO with ``k`` CHECKSIG ops.

    ``pubkey_bytes`` is a 33-byte compressed public key. The script is::

        OP_DUP <pub> OP_CHECKSIGVERIFY  OP_DUP <pub> OP_CHECKSIGVERIFY ...  <pub> OP_CHECKSIG

    which is satisfied by a single signature (each ``OP_DUP`` copies it).
    """
    ops: list = []
    for _ in range(k - 1):
        ops += [OP_DUP, pubkey_bytes, OP_CHECKSIGVERIFY]
    ops += [pubkey_bytes, OP_CHECKSIG]
    return CScript(ops)


def _change_script(kind: str, fund_pub: ECPubKey) -> CScript:
    if kind == "op_true":
        return CScript([OP_1])
    if kind == "p2pk":
        return key_to_p2pk_script(fund_pub.get_bytes())
    raise ValueError(f"unknown change_script kind: {kind!r}")


# --------------------------------------------------------------------------- #
# Sigop accounting (matches Bitcoin Core policy.cpp CheckSigopsBIP54)
# --------------------------------------------------------------------------- #

def bip54_sigops_per_input(script_sig, script_pubkey: CScript) -> int:
    n = CScript(script_sig).GetSigOpCount(True)
    n += script_pubkey.GetSigOpCount(CScript(script_sig))
    return n


# --------------------------------------------------------------------------- #
# Fast legacy sighash preimages
# --------------------------------------------------------------------------- #

def _varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xFD" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xFE" + n.to_bytes(4, "little")
    return b"\xFF" + n.to_bytes(8, "little")


def _ser_string(data: bytes) -> bytes:
    return _varint(len(data)) + bytes(data)


def _legacy_preimage_for(tx: CTransaction, script_codes: list, i: int,
                         base: bytes, head_len: int, in_off: int) -> bytes:
    """Return the SIGHASH_ALL legacy preimage for input ``i``.

    ``base`` is the full tx serialization with every input's scriptSig empty.
    ``head_len`` is the byte length of everything before the inputs array and
    ``in_off`` the offset of each input (all empty inputs are 41 bytes).
    """
    empty_len = 41
    start = head_len + i * empty_len
    code_in = (tx.vin[i].prevout.serialize()
               + _ser_string(script_codes[i])
               + tx.vin[i].nSequence.to_bytes(4, "little"))
    return base[:start] + code_in + base[start + empty_len:]


def build_legacy_preimages(tx: CTransaction, script_codes: list,
                           hashtype: int = SIGHASH_ALL) -> tuple:
    """Efficiently build every input's legacy sighash preimage.

    Returns ``(preimages, base_serialized)``. This is O(N^2) total bytes but uses
    C-speed bytes splicing rather than per-input Python object serialization, and
    matches Bitcoin Core's legacy ``SignatureHash`` for SIGHASH_ALL.
    """
    N = len(tx.vin)
    head = tx.version.to_bytes(4, "little") + _varint(N)
    empty_ins = []
    for vin in tx.vin:
        empty_ins.append(vin.prevout.serialize() + b"\x00"
                         + vin.nSequence.to_bytes(4, "little"))
    tail = _varint(len(tx.vout))
    for vout in tx.vout:
        tail += vout.serialize()
    tail += tx.nLockTime.to_bytes(4, "little")
    tail += hashtype.to_bytes(4, "little")
    base = head + b"".join(empty_ins) + tail
    head_len = len(head)
    preimages = [
        _legacy_preimage_for(tx, script_codes, i, base, head_len, 0)
        for i in range(N)
    ]
    return preimages, base


# --------------------------------------------------------------------------- #
# The generator
# --------------------------------------------------------------------------- #

@dataclass
class ConstructionResult:
    prep_blocks: list = field(default_factory=list)
    prep_transactions: list = field(default_factory=list)
    poison_tx: Optional[CTransaction] = None
    poison_block: object = None
    metrics: dict = field(default_factory=dict)


class PoisonBlockGenerator:
    """Builds the full poison-chain on a fresh regtest node via RPC."""

    def __init__(self, rpc, cfg: ConstructionConfig, log=None):
        self.rpc = rpc
        self.cfg = cfg
        self.cfg.validate()
        self.log = log or (lambda *_: None)
        self._clock = None
        self._preimage_sizes = None
        self.keys = DeterministicKeys(cfg.seed)
        self.fund_key = self.keys.make_key()
        self.fund_secret = self.fund_key.get_bytes()       # shared secret
        self.fund_pub = self.fund_key.get_pubkey()
        self.pub_bytes = self.fund_pub.get_bytes()
        self.fund_script = key_to_p2pk_script(self.pub_bytes)
        # One shared scriptPubKey for every poison UTXO (same public key).
        self.poison_spk = poison_script_pubkey(self.pub_bytes, cfg.sigops_per_input)
        # Populated by generate(); initialized so metrics are callable standalone.
        self.prep_transactions = []
        self.prep_blocks = []

    # -- helpers ----------------------------------------------------------- #
    def _init_clock(self):
        if self.cfg.deterministic_time:
            # Deterministic wall clock so identical seeds produce identical blocks.
            base = 1_700_000_000 + (self.cfg.seed % 1_000_000)
            self.rpc.setmocktime(base + 100_000)
            self._clock = base
        else:
            # Real time: required for multi-node runs (setmocktime suppresses
            # new-block relay to peers).
            self._clock = None

    def _tmpl(self):
        if self.cfg.deterministic_time:
            self._clock += 1
            curtime = self._clock
        else:
            curtime = max(int(time.time()), (self._clock or 0) + 1)
            self._clock = curtime
        return {
            "previousblockhash": self.rpc.getbestblockhash(),
            "curtime": curtime,
            "height": self.rpc.getblockcount() + 1,
            "version": 4,
        }

    def _mine_empty_block(self):
        height = self.rpc.getblockcount() + 1
        block = create_block(tmpl=self._tmpl(),
                             coinbase=create_coinbase(height, pubkey=self.pub_bytes))
        block.solve()
        self._submit(block)

    def _submit(self, block, expect=None) -> None:
        res = self.rpc.submitblock(block.serialize().hex())
        if res is not None:
            raise SafetyError(
                f"internal block rejected while preparing (expected {expect}): {res}")

    def _coinbase_txid(self, height: int) -> str:
        return self.rpc.getblock(self.rpc.getblockhash(height), 1)["tx"][0]

    # -- main entry -------------------------------------------------------- #
    def generate(self) -> ConstructionResult:
        cfg = self.cfg
        N, K = cfg.num_utxos, cfg.sigops_per_input
        self._init_clock()

        sigops_per_prep_block = MAX_BLOCK_SIGOPS_COST // 4  # 20,000 legacy
        # Reserve headroom for the block's own coinbase (a P2PK output = 1 sigop)
        # and a safety margin, so the prep block stays under the per-block cap.
        reserve = 400
        max_utxos_per_prep_block = max(1, (sigops_per_prep_block - reserve) // K)
        n_prep_blocks = (N + max_utxos_per_prep_block - 1) // max_utxos_per_prep_block

        if cfg.max_prep_blocks and n_prep_blocks > cfg.max_prep_blocks:
            raise SafetyError(
                f"construction needs {n_prep_blocks} prep blocks, exceeding "
                f"max_prep_blocks={cfg.max_prep_blocks}.")

        self.log("mining %d baseline blocks (coinbase maturity)..." % COINBASE_MATURITY)
        for _ in range(COINBASE_MATURITY):
            self._mine_empty_block()

        prep_blocks, prep_txs = [], []
        utxo_idx = 0
        for b in range(n_prep_blocks):
            start, end = utxo_idx, min(utxo_idx + max_utxos_per_prep_block, N)
            height = self.rpc.getblockcount() + 1
            fund_height = height - COINBASE_MATURITY
            coinbase_txid = self._coinbase_txid(fund_height)
            fund_block_hash = self.rpc.getblockhash(fund_height)
            prev_tx = _tx_from_hex(self.rpc.getrawtransaction(coinbase_txid, 0, fund_block_hash))

            prep_tx = CTransaction()
            prep_tx.vin.append(CTxIn(COutPoint(prev_tx.txid_int, 0)))
            for _ in range(start, end):
                prep_tx.vout.append(CTxOut(cfg.utxo_value_sats, self.poison_spk))
            sighash, err = _legacy_sighash(self.fund_script, prep_tx, 0)
            assert err is None
            sig = sign_der(self.fund_secret, sighash) + bytes([SIGHASH_ALL])
            prep_tx.vin[0].scriptSig = CScript([sig])

            block = create_block(tmpl=self._tmpl(),
                                 coinbase=create_coinbase(height, pubkey=self.pub_bytes),
                                 txlist=[prep_tx])
            block.solve()
            self._submit(block)
            prep_blocks.append(block)
            prep_txs.append(prep_tx)
            utxo_idx = end
            self.log("mined prep block %d/%d (utxos %d..%d)" % (b + 1, n_prep_blocks, start + 1, end))

        self.prep_blocks = prep_blocks
        self.prep_transactions = prep_txs

        poison_tx = self._build_poison_tx()
        height = self.rpc.getblockcount() + 1
        poison_block = create_block(tmpl=self._tmpl(),
                                    coinbase=create_coinbase(height, pubkey=self.pub_bytes),
                                    txlist=[poison_tx])
        poison_block.solve()
        return ConstructionResult(prep_blocks=prep_blocks, prep_transactions=prep_txs,
                                  poison_tx=poison_tx, poison_block=poison_block,
                                  metrics=self._metrics(poison_tx, n_prep_blocks))

    # -- poison tx --------------------------------------------------------- #
    def _build_poison_tx(self) -> CTransaction:
        cfg = self.cfg
        N, K = cfg.num_utxos, cfg.sigops_per_input

        outpoints = []
        for block in self.prep_blocks:
            for tx in block.vtx[1:]:
                for vout in range(len(tx.vout)):
                    outpoints.append((tx, vout))
        assert len(outpoints) == N

        tx = CTransaction()
        for ptx, vout in outpoints:
            tx.vin.append(CTxIn(COutPoint(ptx.txid_int, vout)))
        tx.vout.append(CTxOut(N * cfg.utxo_value_sats, _change_script(cfg.change_script, self.fund_pub)))

        script_codes = [self.poison_spk] * N
        preimages, _ = build_legacy_preimages(tx, script_codes)
        self._preimage_sizes = [len(p) for p in preimages]

        for i in range(N):
            sighash = hash256(preimages[i])
            sig = sign_der(self.fund_secret, sighash) + bytes([SIGHASH_ALL])
            tx.vin[i].scriptSig = CScript([sig])
        return tx

    # -- metrics ----------------------------------------------------------- #
    #
    # Cost model (v31.1.0, verified against source and by measurement):
    #
    #  * Each input serializes + double-SHA256s its O(N)-sized legacy sighash
    #    preimage ONCE. The per-input SigHashCache (interpreter.cpp:1582) caches
    #    the SHA-256 midstate keyed by (hashType, scriptCode), so the K-1
    #    repeated CHECKSIGs inside the same input reuse the midstate and do NOT
    #    re-serialize or re-hash the transaction. -> serialization/hashing O(N^2).
    #
    #  * Each CHECKSIG still performs a fresh ECDSA verification, because the
    #    signature cache is consulted but NOT populated during block connection
    #    (validation.cpp:2584, cacheSigStore=fJustCheck=false). -> ECDSA O(N*K).
    #
    #  * script interpreter stack/loop overhead is O(N*K) but cheap.
    #
    def _metrics(self, poison_tx: CTransaction, n_prep_blocks: int) -> dict:
        cfg = self.cfg
        N, K = cfg.num_utxos, cfg.sigops_per_input
        sizes = self._preimage_sizes or []

        total_sigops = N * K
        # Actual serialization performed by Core v31.1.0 during validation: each
        # of the N inputs serializes its preimage once (SigHashCache collapses
        # the K-1 repeated CHECKSIGs within an input). Independent of K.
        sighash_serialized = sum(sizes)
        # Each preimage is fed through SHA-256 twice (double-SHA256).
        sighash_double_sha256 = 2 * sighash_serialized
        # Hypothetical serialization if the per-input midstate cache did not
        # exist (e.g. older implementations / the python test framework's
        # LegacySignatureHash). This does NOT describe v31.1.0 block validation.
        no_cache_serialized = sum(sz * K for sz in sizes)
        # Every CHECKSIG does a fresh ECDSA verification during block connection.
        ecdsa_verifies = total_sigops

        tx_bytes = len(poison_tx.serialize_without_witness())
        m = {
            "num_utxos": N,
            "sigops_per_input": K,
            "num_prep_blocks": n_prep_blocks,
            "num_prep_transactions": len(self.prep_transactions),
            "total_legacy_sigops_bip54": total_sigops,
            "executed_checksig_count": total_sigops,
            "ecdsa_verify_count": ecdsa_verifies,
            "poison_tx_vin_count": len(poison_tx.vin),
            "poison_tx_vout_count": len(poison_tx.vout),
            "poison_tx_size_bytes": tx_bytes,
            "poison_tx_weight": _weight(poison_tx),
            # Measured-by-construction quantities (what Core actually does).
            "sighash_serialization_bytes": sighash_serialized,
            "sighash_double_sha256_bytes": sighash_double_sha256,
            "per_input_preimage_bytes": sighash_serialized / N if N else 0,
            # Hypothetical no-cache serialization (NOT v31.1.0 behavior).
            "no_cache_sighash_serialization_bytes": no_cache_serialized,
            # Deprecated aliases (kept for compatibility; see RESEARCH note).
            "expected_sighash_preimage_bytes": sighash_serialized,
            "theoretical_sighash_preimage_bytes_no_cache": no_cache_serialized,
        }
        return m


def _weight(tx: CTransaction) -> int:
    return len(tx.serialize_without_witness()) * 3 + len(tx.serialize())


def _tx_from_hex(hexstr: str) -> CTransaction:
    from test_framework.messages import tx_from_hex
    return tx_from_hex(hexstr)


def _legacy_sighash(script_code, tx: CTransaction, n_in: int):
    """Return the legacy sighash for one input (matches Core's SignatureHash)."""
    from test_framework.script import LegacySignatureHash
    return LegacySignatureHash(script_code, tx, n_in, SIGHASH_ALL)
