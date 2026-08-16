"""Tests for the multi-node propagation benchmark and its report schema."""

import json
import os
import sys
from pathlib import Path

import pytest

from construction import ConstructionConfig
from propagation import PropagationBenchmark, PropagationConfig

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BITCOIND = Path(os.environ.get("PBABENCH_BITCOIND", "/usr/local/bin/bitcoind"))


def test_propagation_config_defaults():
    cfg = PropagationConfig()
    assert cfg.observer_par == [1]
    assert cfg.num_observers == 1


def test_propagation_config_int_observer_par_normalized():
    # An int is accepted for backward compatibility and normalized to a list.
    cfg = PropagationConfig(observer_par=1)
    assert cfg.observer_par == [1]
    cfg2 = PropagationConfig(observer_par="4")
    assert cfg2.observer_par == [4]
    # The generator must use real time (not mocktime) in multi-node mode.
    gen = ConstructionConfig(seed=1, num_utxos=4, sigops_per_input=2,
                             deterministic_time=False)
    assert gen.deterministic_time is False


@pytest.mark.skipif(not BITCOIND.exists(), reason="no bitcoind available")
def test_propagation_smoke():
    """A tiny propagation run must reach the observer and report all key fields."""
    from benchmark import BenchmarkConfig

    bench = BenchmarkConfig(bitcoind_path=BITCOIND, profile="propagate",
                            keep_datadir=False, max_wall_seconds=600)
    prop = PropagationConfig(seed=3, num_utxos=50, sigops_per_input=20,
                             observer_par=1, num_observers=1)
    result = PropagationBenchmark(prop, bench, Path(".").resolve(), lambda *_: None).run()

    assert result["construction"]["executed_checksig_count"] == 50 * 20
    assert result["miner"]["submit_accepted"] is True
    assert result["miner"]["miner_validation_seconds"] is not None
    obs = result["observers"]
    assert len(obs) == 1
    assert obs[0]["time_to_tip_seconds"] is not None
    assert obs[0]["post_miner_time_to_tip_seconds"] is not None
    # The poison block must be slower than the normal-block baseline.
    base = result["baseline"].get("baseline_time_to_tip_seconds")
    poison = obs[0]["time_to_tip_seconds"]
    assert base is not None and poison is not None
    assert poison > base, "poison block must reach the observer slower than a normal block"
    assert obs[0]["rpc_probe_count"] >= 0
    # Per-observer fields required by the schema.
    for f in ("observer_id", "par", "node_version", "pre_poison_tip",
              "poison_block_hash", "final_chain_height", "success"):
        assert f in obs[0]
