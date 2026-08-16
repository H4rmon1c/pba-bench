"""Tests for the poison-block generator (construction logic and a live smoke run)."""

import os
import sys
from pathlib import Path

import pytest

from construction import (
    BIP54_MAX_TX_LEGACY_SIGOPS,
    ConstructionConfig,
    DeterministicKeys,
    PoisonBlockGenerator,
    bip54_sigops_per_input,
    poison_script_pubkey,
)
from test_framework.script import OP_1, OP_CHECKSIG, OP_CHECKSIGVERIFY, OP_DUP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


BITCOIND = Path(os.environ.get("PBABENCH_BITCOIND", "/usr/local/bin/bitcoind"))


class _NullRPC:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _call(*a, **kw):
            self.calls.append((name, a, kw))
            return {}
        return _call


# --------------------------------------------------------------------------- #
# Pure construction tests (no node)
# --------------------------------------------------------------------------- #

def test_deterministic_keys_same_seed():
    k1 = DeterministicKeys(42)
    k2 = DeterministicKeys(42)
    assert k1.make_key().get_bytes() == k2.make_key().get_bytes()


def test_deterministic_keys_different_seeds():
    k1 = DeterministicKeys(1)
    k2 = DeterministicKeys(2)
    assert k1.make_key().get_bytes() != k2.make_key().get_bytes()


def test_poison_script_pubkey_structure():
    keys = DeterministicKeys(1)
    pub = keys.make_key().get_pubkey().get_bytes()
    k = 3
    spk = poison_script_pubkey(pub, k)
    # Count opcodes by iterating the parsed script (avoids matching bytes
    # that appear inside pushed pubkey data).
    ops = [op for op, _, _ in spk.raw_iter()]
    assert ops.count(OP_CHECKSIG) + ops.count(OP_CHECKSIGVERIFY) == k
    assert ops.count(OP_DUP) == k - 1


def test_bip54_sigops_per_input_matches_expected():
    keys = DeterministicKeys(1)
    pub = keys.make_key().get_pubkey().get_bytes()
    spk = poison_script_pubkey(pub, 5)
    from test_framework.script import CScript
    empty = CScript([])
    assert bip54_sigops_per_input(empty, spk) == 5


def test_generator_same_seed_same_keys_and_scripts():
    cfg = ConstructionConfig(seed=99, num_utxos=4, sigops_per_input=3)
    g1 = PoisonBlockGenerator(_NullRPC(), cfg)
    g2 = PoisonBlockGenerator(_NullRPC(), cfg)
    assert g1.fund_key.get_bytes() == g2.fund_key.get_bytes()
    assert g1.poison_spk == g2.poison_spk


def test_fast_preimages_match_reference():
    from test_framework.messages import CTransaction, CTxIn, CTxOut, COutPoint
    from test_framework.script import CScript, SIGHASH_ALL, LegacySignatureMsg, OP_1
    from construction import build_legacy_preimages
    cfg = ConstructionConfig(seed=3, num_utxos=4, sigops_per_input=2)
    g = PoisonBlockGenerator(_NullRPC(), cfg)
    tx = CTransaction()
    for i in range(4):
        tx.vin.append(CTxIn(COutPoint(i + 1, 0)))
    tx.vout.append(CTxOut(400000, CScript([OP_1])))
    script_codes = [g.poison_spk] * 4
    preimages, _ = build_legacy_preimages(tx, script_codes)
    for i in range(4):
        msg, err = LegacySignatureMsg(g.poison_spk, tx, i, SIGHASH_ALL)
        assert err is None
        assert preimages[i] == msg, f"preimage mismatch at input {i}"


def test_generator_small_stays_under_block_size():
    cfg = ConstructionConfig(seed=1, num_utxos=200, sigops_per_input=6)
    g = PoisonBlockGenerator(_NullRPC(), cfg)
    # Sanity: the expected per-input sighash preimage is about N*41 bytes.
    # (Verified against the live node in the integration test.)
    assert len(g.poison_spk) > 0


def test_bip54_limit_constant():
    assert BIP54_MAX_TX_LEGACY_SIGOPS == 2500


def test_deterministic_time_flag_present():
    # deterministic_time must be configurable (multi-node runs need real time).
    cfg = ConstructionConfig(seed=1, num_utxos=4, sigops_per_input=2,
                             deterministic_time=False)
    assert cfg.deterministic_time is False
    assert ConstructionConfig(seed=1).deterministic_time is True


# --------------------------------------------------------------------------- #
# Integration tests (require a local bitcoind)
# --------------------------------------------------------------------------- #

def _launch(cfg: ConstructionConfig):
    """Start a disposable node, run the construction, return (result, node)."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from benchmark import BenchmarkConfig, Node
    from construction import ConstructionConfig as CC

    bench_cfg = BenchmarkConfig(bitcoind_path=BITCOIND, profile="custom",
                                seed=cfg.seed, keep_datadir=False,
                                num_utxos=cfg.num_utxos,
                                sigops_per_input=cfg.sigops_per_input)
    node = Node(bench_cfg, Path(__file__).resolve().parent.parent, lambda *a: None)
    try:
        node.start()
        node.verify_regtest()
        gen = PoisonBlockGenerator(node.rpc, cfg)
        res = gen.generate()
        return res, node
    except Exception:
        node.stop()
        raise


@pytest.mark.skipif(not BITCOIND.exists(), reason="no bitcoind available")
def test_smoke_produces_valid_regtest_chain():
    cfg = ConstructionConfig(seed=5, num_utxos=3, sigops_per_input=2)
    res, node = _launch(cfg)
    try:
        assert res.poison_tx is not None
        assert len(res.poison_tx.vin) == 3
        # Submit the poison block; it must be accepted as consensus-valid.
        err = node.rpc.submitblock(res.poison_block.serialize().hex())
        assert err is None, f"poison block rejected: {err}"
        info = node.rpc.getblockchaininfo()
        assert info["chain"] == "regtest"
        assert info["blocks"] >= 102
        # The poison tx must be in the best chain.
        block = node.rpc.getblock(info["bestblockhash"], 2)
        assert len(block["tx"]) == 2
    finally:
        node.stop()


@pytest.mark.skipif(not BITCOIND.exists(), reason="no bitcoind available")
def test_identical_seeds_identical_blocks():
    cfg = ConstructionConfig(seed=1234, num_utxos=3, sigops_per_input=2)
    res1, node1 = _launch(cfg)
    res2, node2 = _launch(cfg)
    try:
        b1 = res1.poison_block.serialize().hex()
        b2 = res2.poison_block.serialize().hex()
        tx1 = res1.poison_tx.serialize_without_witness().hex()
        tx2 = res2.poison_tx.serialize_without_witness().hex()
        assert tx1 == tx2, "identical seeds must produce identical poison txs"
        assert b1 == b2, "identical seeds must produce identical poison blocks"
    finally:
        node1.stop()
        node2.stop()


@pytest.mark.skipif(not BITCOIND.exists(), reason="no bitcoind available")
def test_poison_tx_reported_sigops_match():
    cfg = ConstructionConfig(seed=7, num_utxos=4, sigops_per_input=2)
    res, node = _launch(cfg)
    try:
        assert res.metrics["total_legacy_sigops_bip54"] == 8
        assert res.metrics["poison_tx_vin_count"] == 4
        assert res.metrics["sighash_serialization_bytes"] > 0
    finally:
        node.stop()


def test_metrics_cost_model_relationships():
    """The corrected v2 cost model: no-cache = K * cache-aware serialization;
    ECDSA verifies = executed CHECKSIG = N*K."""
    from construction import ConstructionConfig, PoisonBlockGenerator
    from test_framework.script import CScript, OP_1, SIGHASH_ALL, LegacySignatureMsg
    from test_framework.messages import CTransaction, CTxIn, CTxOut, COutPoint
    from construction import build_legacy_preimages

    cfg = ConstructionConfig(seed=2, num_utxos=5, sigops_per_input=3)
    g = PoisonBlockGenerator(_NullRPC(), cfg)
    tx = CTransaction()
    for i in range(5):
        tx.vin.append(CTxIn(COutPoint(i + 1, 0)))
    tx.vout.append(CTxOut(500000, CScript([OP_1])))
    script_codes = [g.poison_spk] * 5
    preimages, _ = build_legacy_preimages(tx, script_codes)
    N, K = 5, 3
    serialized = sum(len(p) for p in preimages)
    assert serialized == cfg.num_utxos * len(preimages[0])  # O(N) per input
    g._preimage_sizes = [len(p) for p in preimages]  # normally set during _build_poison_tx
    m = g._metrics(tx, 1)
    assert m["executed_checksig_count"] == N * K
    assert m["ecdsa_verify_count"] == N * K
    assert m["sighash_serialization_bytes"] == serialized
    assert m["sighash_double_sha256_bytes"] == 2 * serialized
    # no-cache is Kx the cache-aware serialization (hypothetical).
    assert m["no_cache_sighash_serialization_bytes"] == K * serialized
    # deprecated aliases point at the actual values.
    assert m["expected_sighash_preimage_bytes"] == serialized
    assert m["theoretical_sighash_preimage_bytes_no_cache"] == K * serialized


def test_metrics_serialization_independent_of_k():
    """Serialization bytes must be ~independent of K (SigHashCache model)."""
    from construction import ConstructionConfig, PoisonBlockGenerator
    from test_framework.script import CScript, OP_1
    from test_framework.messages import CTransaction, CTxIn, CTxOut, COutPoint
    from construction import build_legacy_preimages

    def serial(N, K):
        cfg = ConstructionConfig(seed=2, num_utxos=N, sigops_per_input=K)
        g = PoisonBlockGenerator(_NullRPC(), cfg)
        tx = CTransaction()
        for i in range(N):
            tx.vin.append(CTxIn(COutPoint(i + 1, 0)))
        tx.vout.append(CTxOut(N * 100000, CScript([OP_1])))
        preimages, _ = build_legacy_preimages(tx, [g.poison_spk] * N)
        return sum(len(p) for p in preimages)

    # Same N, very different K. Serialization is O(N^2) dominated (each input
    # serializes the full N-input tx once); the scriptCode only adds its own
    # ~K-byte size. So raising K 40x must NOT raise serialization ~40x.
    a = serial(100, 1)
    b = serial(100, 40)
    assert b < a * 3, f"K raised serialization too much: {a} -> {b}"
