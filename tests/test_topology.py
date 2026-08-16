"""Unit tests for the topology generator and observer-par normalization."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from propagation import PropagationConfig, TopologyNode, build_topology
from safety import SafetyError


def test_star_topology_edges():
    t = build_topology("star", 4, [1])
    assert t.kind == "star"
    assert t.num_observers == 4
    # Every observer connects directly to the miner (id 0).
    assert set((a, b) for a, b in t.edges) == {(0, 1), (0, 2), (0, 3), (0, 4)}
    # Every observer's upstream is the miner.
    for s in t.observer_specs:
        assert t._upstream[s.id] == [0]


def test_line_topology_edges():
    t = build_topology("line", 4, [1])
    # MINER->1, 1->2, 2->3, 3->4
    assert set((a, b) for a, b in t.edges) == {(0, 1), (1, 2), (2, 3), (3, 4)}
    assert t._upstream[1] == [0]
    assert t._upstream[2] == [1]
    assert t._upstream[3] == [2]
    assert t._upstream[4] == [3]


def test_tree_topology_edges():
    t = build_topology("tree", 6, [1])
    # MINER -> 1,2 ; 1 -> 3,4 ; 2 -> 5,6
    assert set((a, b) for a, b in t.edges) == {
        (0, 1), (0, 2), (1, 3), (1, 4), (2, 5), (2, 6)}
    assert t._upstream[1] == [0]
    assert t._upstream[2] == [0]
    assert t._upstream[3] == [1]
    assert t._upstream[5] == [2]


def test_unknown_topology_rejected():
    with pytest.raises(SafetyError):
        build_topology("mesh", 3, [1])


def test_zero_observers_rejected():
    with pytest.raises(SafetyError):
        build_topology("star", 0, [1])


def test_par_values_cycled_over_observers():
    t = build_topology("star", 3, [1, 2])
    pars = [s.par for s in t.observer_specs]
    assert pars == [1, 2, 1]  # cycled


def test_propagation_config_normalizes_par():
    assert PropagationConfig(observer_par=1).observer_par == [1]
    assert PropagationConfig(observer_par=[2, 4]).observer_par == [2, 4]
    assert PropagationConfig(observer_par="8").observer_par == [8]
    assert PropagationConfig(observer_par=[]).observer_par == [1]


def test_topology_node_label():
    assert TopologyNode(id=3, par=1).label() == "obs3"
