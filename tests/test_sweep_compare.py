"""Unit tests for the sweep and compare drivers (pure logic, no node)."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compare import CompareConfig, load_manifest
from sweep import SweepConfig, _aggregate_point
from safety import SafetyError


def test_sweep_terminal_summary_renders():
    from sweep import sweep_terminal_summary
    result = {
        "axis": "k", "axis_name": "sigops_per_input",
        "fixed_name": "num_utxos", "fixed_value": 2000,
        "runs_per_point": 2, "values": [1, 2],
        "points": [
            {"axis_value": 1, "executed_checksig_count": 2000,
             "sighash_serialization_bytes": 1000, "outcome": {"accepted"},
             "validation_wall_seconds": {"median": 0.1, "min": 0.05, "max": 0.2},
             "validation_cpu_seconds": {"median": 0.1}},
        ],
    }
    text = sweep_terminal_summary(result)
    assert "axis=k" in text
    assert "2000" in text


def test_aggregate_point():
    from construction import ConstructionConfig
    gen_cfg = ConstructionConfig(seed=1, num_utxos=10, sigops_per_input=2)
    trials = [
        {"measurement": {"validation_wall_seconds": 0.1, "validation_cpu_seconds": 0.2,
                         "peak_rss_bytes": 1000},
         "outcome": {"success": "accepted", "block_hash": "a"},
         "construction": {"executed_checksig_count": 20,
                          "sighash_serialization_bytes": 100,
                          "no_cache_sighash_serialization_bytes": 200,
                          "poison_tx_size_bytes": 50, "poison_tx_weight": 200}},
        {"measurement": {"validation_wall_seconds": 0.3, "validation_cpu_seconds": 0.4,
                         "peak_rss_bytes": 1500},
         "outcome": {"success": "accepted", "block_hash": "b"},
         "construction": {"executed_checksig_count": 20,
                          "sighash_serialization_bytes": 100,
                          "no_cache_sighash_serialization_bytes": 200,
                          "poison_tx_size_bytes": 50, "poison_tx_weight": 200}},
    ]
    p = _aggregate_point(2, trials, gen_cfg)
    assert p["executed_checksig_count"] == 20
    assert p["validation_wall_seconds"]["median"] == 0.2
    assert p["validation_wall_seconds"]["max"] == 0.3
    assert p["trials"] == 2


def test_compare_manifest_roundtrip(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "binaries": [
            {"name": "core31", "path": "/opt/core31/bitcoind"},
            {"name": "bip54", "path": "/opt/core-bip54/bitcoind", "extra_args": ["-debug=1"]},
        ],
        "num_utxos": 500, "sigops_per_input": 10, "seed": 7,
        "validation_threads": 1,
    }))
    cfg = load_manifest(manifest)
    assert len(cfg.binaries) == 2
    assert cfg.binaries[0].name == "core31"
    assert cfg.binaries[1].extra_args == ["-debug=1"]
    assert cfg.num_utxos == 500
    assert cfg.seed == 7
    assert cfg.validation_threads == 1


def test_compare_manifest_missing_binaries_rejected():
    cfg = CompareConfig(binaries=[])
    assert cfg.binaries == []


def test_sweep_config_defaults():
    cfg = SweepConfig()
    assert cfg.axis == "k"
    assert cfg.fixed_value == 2000
    assert cfg.runs == 3
