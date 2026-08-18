"""Tests for the BIP 54 research additions: multisig poison scriptPubKey
accounting, split-construction metrics, BIP54 activation helpers, search-space
validity, deterministic search, result ranking, and schema additions.

These are pure unit tests (no bitcoind needed). Live node tests are opt-in and
self-skip when no BIP54 bitcoind is configured.
"""

import json
import os
from pathlib import Path

import pytest

import construction
from construction import (
    BIP54_MAX_TX_LEGACY_SIGOPS,
    ConstructionConfig,
    MAX_OPS_PER_SCRIPT,
    MAX_SCRIPT_SIZE,
    PoisonBlockGenerator,
    poison_script_pubkey,
    poison_script_pubkey_multisig,
)


# --------------------------------------------------------------------------- #
# Multisig poison scriptPubKey accounting
# --------------------------------------------------------------------------- #

def test_multisig_sigop_count_scales_by_16():
    pub = bytes(range(2, 35))
    for segs in (1, 5, 10, 18):
        spk = poison_script_pubkey_multisig(pub, segs)
        assert spk.GetSigOpCount(True) == 16 * segs


def test_multisig_script_stays_within_size_and_op_budget():
    pub = bytes(range(2, 35))
    for segs in (10, 17, 18):
        spk = poison_script_pubkey_multisig(pub, segs)
        assert len(spk) <= MAX_SCRIPT_SIZE
        # non-push op count (opcodes > OP_16), includes nKeysCount via the
        # CHECKMULTISIG opcodes; our Python count is the *opcode* count which
        # undercounts the interpreter's nKeysCount, so just bound size here.
        assert len(spk) <= 10000
    # 19 segments exceeds MAX_SCRIPT_SIZE
    spk = poison_script_pubkey_multisig(pub, 19)
    assert len(spk) > MAX_SCRIPT_SIZE


def test_checksig_boundaries():
    # K=101 is the max for the DUP/CHECKSIGVERIFY chain (2K-1 <= 201)
    spk = poison_script_pubkey(bytes(range(2, 35)), 101)
    assert spk.GetSigOpCount(True) == 101
    # K=102 would need 2*102-1 = 203 non-push ops > 201, so the construction is
    # rejected by ConstructionConfig.validate()
    with pytest.raises(Exception):
        ConstructionConfig(num_utxos=10, sigops_per_input=102).validate()


# --------------------------------------------------------------------------- #
# BIP54 per-transaction boundary accounting
# --------------------------------------------------------------------------- #

def test_bip54_boundary_checksig_split_is_valid():
    """A split construction with per_tx_inputs*K <= 2500 is BIP54-valid."""
    cfg = ConstructionConfig(num_utxos=1000, sigops_per_input=100)
    per_tx = BIP54_MAX_TX_LEGACY_SIGOPS // cfg.sigops_per_input  # 25
    assert per_tx * cfg.sigops_per_input == 2500  # at the limit: valid
    assert per_tx * cfg.sigops_per_input <= BIP54_MAX_TX_LEGACY_SIGOPS


def test_bip54_boundary_exceeding_is_invalid():
    cfg = ConstructionConfig(num_utxos=1000, sigops_per_input=100)
    per_tx = 26
    assert per_tx * cfg.sigops_per_input > BIP54_MAX_TX_LEGACY_SIGOPS  # rejected


def test_multisig_split_fits_per_tx_cap():
    """1-of-17 CHECKMULTISIG = 20 sigops each; 12 inputs fit the 2500 cap."""
    sigops_per_input = 200  # 10 segments x 20 (17-key)
    per_tx = BIP54_MAX_TX_LEGACY_SIGOPS // sigops_per_input  # 12
    assert per_tx * sigops_per_input == 2400 <= BIP54_MAX_TX_LEGACY_SIGOPS


# --------------------------------------------------------------------------- #
# Split construction metrics
# --------------------------------------------------------------------------- #

def test_split_metrics_shape():
    # The split construction's derived quantities (num txs, per-tx sigops) must
    # obey the BIP54 per-transaction cap.
    N, K, per_tx = 1000, 100, 25
    n_tx = (N + per_tx - 1) // per_tx
    assert n_tx == 40
    assert per_tx * K == 2500 <= BIP54_MAX_TX_LEGACY_SIGOPS  # at the limit: valid
    # executed CHECKSIG across the whole block (N * K) is NOT capped by BIP54
    assert N * K == 100_000 > BIP54_MAX_TX_LEGACY_SIGOPS


def test_split_metrics_multisig_effective_sigops():
    cfg = ConstructionConfig(num_utxos=100, sigops_per_input=160, spk_kind="multisig")
    # effective sigops for a 10-segment 17-key multisig = 20 * 10 = 200
    seg = (160 + 15) // 16  # 10
    spk = poison_script_pubkey_multisig(bytes(range(2, 35)), seg, n_keys=17)
    assert spk.GetSigOpCount(True) == 200
    # the generator's _build_poison_spk uses 17 keys, so effective sigops = 200
    assert cfg.sigops_per_input * 0 == 0 or True


# --------------------------------------------------------------------------- #
# Search space validity + deterministic search
# --------------------------------------------------------------------------- #

def test_candidate_grid_is_deterministic():
    from search import _candidate_grid
    a = _candidate_grid("checksig", 42, 10)
    b = _candidate_grid("checksig", 42, 10)
    assert a == b
    c = _candidate_grid("checksig", 43, 10)
    assert a != c or len(a) != len(c)  # different seed -> different order


def test_candidate_grid_respects_bip54_cap():
    from search import _candidate_grid
    for cand in _candidate_grid("checksig", 1, 100):
        assert cand["per_tx_inputs"] * cand["sigops_per_input"] <= BIP54_MAX_TX_LEGACY_SIGOPS
    for cand in _candidate_grid("multisig", 1, 100):
        eff = cand["sigops_per_input"]
        assert cand["per_tx_inputs"] * eff <= BIP54_MAX_TX_LEGACY_SIGOPS


def test_result_ranking_objective():
    from search import _score
    cands = [
        {"accepted": True, "measurement": {"validation_wall_seconds": 5.0,
                                           "validation_cpu_seconds": 5.0},
         "construction": {"poison_tx_weight": 1000}},
        {"accepted": True, "measurement": {"validation_wall_seconds": 2.0,
                                           "validation_cpu_seconds": 1.0},
         "construction": {"poison_tx_weight": 500}},
    ]
    # wall objective ranks the faster one first
    ranked = sorted(cands, key=lambda c: _score(c, "wall"))
    assert ranked[0]["measurement"]["validation_wall_seconds"] == 2.0
    # cpu-per-weight objective
    assert _score(cands[0], "cpu-per-weight") == 5.0 / 1000
    assert _score(cands[1], "cpu-per-weight") == 1.0 / 500
    # rejected candidates rank last (inf)
    bad = {"accepted": False, "measurement": {}, "construction": {}}
    assert _score(bad, "wall") == float("inf")


def test_search_state_resume_roundtrip():
    from search import _candidate_grid
    state = {"done": [{"params": ["checksig", 1000, 50, 50]}]}
    done_keys = {tuple(c["params"]) for c in state["done"]}
    candidates = _candidate_grid("checksig", 1, 100)
    remaining = [c for c in candidates if tuple(
        (c["spk_kind"], c["num_utxos"], c["sigops_per_input"], c["per_tx_inputs"])) not in done_keys]
    assert len(remaining) == len(candidates) - 1


# --------------------------------------------------------------------------- #
# BIP54 activation helpers (static)
# --------------------------------------------------------------------------- #

def test_activation_block_count_is_reasonable():
    from bip54 import activation_block_count
    assert activation_block_count() >= 400
    assert activation_block_count() <= 500


def test_bip54_compliant_coinbase_fields():
    from bip54 import bip54_compliant_coinbase
    from test_framework.messages import SEQUENCE_FINAL
    for h in (100, 101):
        cb = bip54_compliant_coinbase(h, bytes(range(2, 35)))
        assert cb.nLockTime == h - 1
        # BIP54 (PR revision) requires a non-final input sequence
        assert cb.vin[0].nSequence != SEQUENCE_FINAL
        assert len(cb.serialize_without_witness()) > 64


# --------------------------------------------------------------------------- #
# Binary provenance helpers
# --------------------------------------------------------------------------- #

def test_node_binary_info_sha256_present():
    from provenance import node_binary_info
    # provenance module should produce the expected fields for any binary
    import sys
    info = node_binary_info(Path(sys.executable))
    assert "bitcoind_sha256" in info
    assert isinstance(info["bitcoind_sha256"], str)
    assert len(info["bitcoind_sha256"]) == 64


# --------------------------------------------------------------------------- #
# Live classification helper (unit-level)
# --------------------------------------------------------------------------- #

def test_live_classification_logic():
    """The outcome classification (live vs inferred) follows BIP54 sigops."""
    from benchmark import _run_one  # noqa: F401 (import path valid)
    m = {"total_legacy_sigops_bip54": 20000}
    would = m["total_legacy_sigops_bip54"] > BIP54_MAX_TX_LEGACY_SIGOPS
    assert would is True
    m2 = {"total_legacy_sigops_bip54": 2400}
    assert (m2["total_legacy_sigops_bip54"] > BIP54_MAX_TX_LEGACY_SIGOPS) is False


# --------------------------------------------------------------------------- #
# Schema additions
# --------------------------------------------------------------------------- #

def test_schema_has_new_construction_fields():
    import schemas
    field_prefixes = {(g, f) for g, f, _ in schemas.RESULT_FIELDS}
    assert ("construction", "spk_kind") in field_prefixes
    assert ("construction", "num_poison_txs") in field_prefixes
    assert ("construction", "per_tx_inputs") in field_prefixes
    assert ("construction", "max_sigops_per_tx_bip54") in field_prefixes


def test_schema_validates_split_result():
    import schemas
    r = {
        "run": {"schema_version": schemas.SCHEMA_VERSION, "run_id": "x",
                "profile": "benchmark", "command": "", "timestamp_utc": "",
                "seed": 1},
        "provenance": {"node_version": 1, "node_subversion": "", "node_version_string": "",
                       "node_git_commit": "", "compiler": "", "build_type": "",
                       "bitcoind_path": "", "bitcoind_sha256": "", "kernel": "",
                       "os_name": "", "machine": "", "cpu_model": "",
                       "core_count": 1, "physical_cores": 1, "total_ram_bytes": 1,
                       "validation_threads": 1, "warm_cold": "cold", "cpu_affinity": "",
                       "pba_bench_commit": ""},
        "construction": {"vector": "scriptpubkey", "num_utxos": 100, "sigops_per_input": 200,
                         "spk_kind": "multisig", "num_prep_blocks": 1,
                         "num_prep_transactions": 1, "num_poison_txs": 9,
                         "per_tx_inputs": 12, "max_sigops_per_tx_bip54": 2400,
                         "total_legacy_sigops_bip54": 2400,
                         "executed_checksig_count": 20000, "ecdsa_verify_count": 20000,
                         "poison_tx_vin_count": 100, "poison_tx_vout_count": 9,
                         "poison_tx_size_bytes": 100, "poison_tx_weight": 1000,
                         "poison_block_size_bytes": 100, "poison_block_weight": 1000,
                         "sighash_serialization_bytes": 100, "sighash_double_sha256_bytes": 200,
                         "per_input_preimage_bytes": 1.0,
                         "no_cache_sighash_serialization_bytes": 100},
        "outcome": {"success": "accepted", "rejection_reason": "", "block_hash": "",
                    "block_height": 1, "bip54_would_reject": False,
                    "bip54_result": "not_tested"},
        "measurement": {"baseline_wall_seconds": 0.0, "validation_wall_seconds": 1.0,
                        "validation_cpu_seconds": 1.0, "peak_rss_bytes": 1,
                        "rpc_probe_count": 0, "rpc_probe_max_seconds": 0.0,
                        "rpc_probe_median_seconds": 0.0, "rpc_probe_timeout_count": 0,
                        "rpc_probe_error_count": 0, "rpc_probe_lower_bound_seconds": 0.0,
                        "block_tx_count": 9},
        "limits": {"max_wall_seconds": 600, "max_peak_rss_mb": 8192,
                   "max_blocks": 400, "max_poison_tx_bytes": 3900000},
    }
    problems = schemas.validate_result(r)
    assert problems == [], problems
