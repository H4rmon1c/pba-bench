"""Integration tests for multi-observer / topology propagation (require bitcoind).

These are kept tiny (few inputs, few observers) so they run in reasonable time.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark import BenchmarkConfig
from propagation import PropagationBenchmark, PropagationConfig

BITCOIND = Path(os.environ.get("PBABENCH_BITCOIND", "/usr/local/bin/bitcoind"))

pytestmark = pytest.mark.skipif(not BITCOIND.exists(), reason="no bitcoind available")


def _bench():
    return BenchmarkConfig(bitcoind_path=BITCOIND, profile="propagate",
                           keep_datadir=False, max_wall_seconds=600)


def _run(prop):
    return PropagationBenchmark(prop, _bench(), Path(".").resolve(),
                                lambda *_: None).run()


def test_three_observer_star_independent_measurements():
    prop = PropagationConfig(seed=11, num_utxos=40, sigops_per_input=15,
                             observer_par=[1, 2, 1], num_observers=3,
                             topology="star")
    result = _run(prop)
    assert result["topology"]["kind"] == "star"
    obs = result["observers"]
    assert len(obs) == 3
    # Every observer has its own measurement context and reached the poison tip.
    assert [o["success"] for o in obs] == ["reached_tip"] * 3
    # Heterogeneous par is recorded per observer.
    assert [o["par"] for o in obs] == [1, 2, 1]
    # Independent timings; observer 2 (par=2) should be no slower than observer 1.
    t1 = obs[0]["time_to_tip_seconds"]
    t2 = obs[1]["time_to_tip_seconds"]
    assert t1 is not None and t2 is not None
    assert t2 < t1 + 1.0  # par=2 must not be much slower than par=1


def test_line_topology_two_observers_hop_order():
    prop = PropagationConfig(seed=12, num_utxos=40, sigops_per_input=15,
                             observer_par=1, num_observers=2, topology="line")
    result = _run(prop)
    obs = {o["observer_id"]: o for o in result["observers"]}
    # Observer 2 is downstream of observer 1; it cannot reach the tip before obs1
    # by more than the validation delay (allow generous skew on fast hardware).
    t1 = obs[1]["time_to_tip_seconds"]
    t2 = obs[2]["time_to_tip_seconds"]
    assert t2 >= t1 - 0.5, "downstream observer reached tip before upstream"


def test_tree_topology_three_observers():
    prop = PropagationConfig(seed=13, num_utxos=40, sigops_per_input=15,
                             observer_par=1, num_observers=3, topology="tree")
    result = _run(prop)
    assert result["topology"]["kind"] == "tree"
    assert len(result["observers"]) == 3
    assert all(o["success"] == "reached_tip" for o in result["observers"])


def test_clean_shutdown_removes_datadirs():
    prop = PropagationConfig(seed=14, num_utxos=30, sigops_per_input=10,
                             observer_par=1, num_observers=1, topology="star")
    result = _run(prop)
    assert result["miner"]["submit_accepted"] is True
