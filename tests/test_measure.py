"""Unit tests for measurement helpers: statistics, RPC probe classification,
and the resource guard."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from measure import (
    ProbeOutcome,
    ResourceGuard,
    _median,
    percentile,
    summarize,
)


def test_median_even_and_odd():
    assert _median([]) == 0.0
    assert _median([1, 2, 3]) == 2.0
    assert _median([1, 2, 3, 4]) == 2.5


def test_percentile():
    vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(vals, 50) == 5.5
    assert percentile(vals, 0) == 1
    assert percentile(vals, 100) == 10
    assert percentile([], 50) is None


def test_summarize_ignores_none():
    s = summarize([1, 2, None, 4])
    assert s["n"] == 3
    assert s["median"] == 2
    assert s["min"] == 1
    assert s["max"] == 4
    assert s["p25"] == 1.5
    assert s["p90"] == 3.6  # linear interpolation between 2 and 4 at 90th pct


def test_probe_outcome_classification():
    class FakeNodeDead:
        def alive(self):
            return False

    class FakeNodeAlive:
        def alive(self):
            return True

    # Node down -> node_shutdown
    assert ProbeOutcome.classify(RuntimeError("x"), False) == ProbeOutcome.NODE_SHUTDOWN
    # Timeout is reported by the caller (latency >= timeout), but connection
    # errors while alive classify as connection_error.
    assert ProbeOutcome.classify(OSError("reset"), True) == ProbeOutcome.CONNECTION_ERROR


def test_resource_guard_max_rss_fires():
    guard = ResourceGuard(os.getpid(), max_rss_mb=1, sample_interval=0.02)
    guard.start()
    time.sleep(0.1)
    guard.stop()
    v = guard.violation()
    assert v is not None
    assert "max_peak_rss_mb" in v


def test_resource_guard_max_wall_fires():
    guard = ResourceGuard(os.getpid(), max_wall_seconds=0, sample_interval=0.02)
    guard.start()
    time.sleep(0.1)
    guard.stop()
    v = guard.violation()
    assert v is not None
    assert "max_wall_seconds" in v


def test_resource_guard_no_limits_no_violation():
    guard = ResourceGuard(os.getpid(), max_rss_mb=None, max_wall_seconds=None)
    guard.start()
    time.sleep(0.05)
    guard.stop()
    assert guard.violation() is None


def test_resource_guard_on_violation_callback():
    called = []
    guard = ResourceGuard(os.getpid(), max_rss_mb=1, sample_interval=0.02,
                          on_violation=lambda: called.append(True))
    guard.start()
    time.sleep(0.1)
    guard.stop()
    assert called == [True]
