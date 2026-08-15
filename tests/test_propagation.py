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
    assert cfg.observer_par == 1
    assert cfg.num_observers == 1
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

    assert result["construction"]["total_legacy_sigops_bip54"] == 50 * 20
    assert result["poison"]["submit_accepted"] is True
    assert result["poison"]["observer_saw_poison"] is True
    assert result["poison"]["propagation_to_observer_seconds"] is not None
    # Normal block propagates much faster than the poison block.
    base = result["baseline"]["baseline_propagation_seconds"]
    poison = result["poison"]["propagation_to_observer_seconds"]
    assert base is not None and poison is not None
    assert poison > base, "poison block must propagate slower than a normal block"
    assert result["observer_rpc_during_validation"]["rpc_probe_count"] >= 0
    assert result["stale_tip_duration_seconds"] == poison
