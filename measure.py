"""Measurement helpers for pba-bench.

While a poison block is being validated (a blocking ``submitblock`` call), a
:class:`NodeMonitor` runs in a background thread and samples:

  * the bitcoind process CPU time (user + system),
  * the bitcoind peak resident set size,
  * the latency of lightweight RPC calls issued *during* validation (which
    demonstrates RPC unresponsiveness while the node is busy validating).

All timing uses ``time.perf_counter`` (monotonic).
"""

from __future__ import annotations

import threading
import time

import psutil


class NodeMonitor:
    """Samples node resource usage and RPC latency while a block validates.

    Usage::

        with NodeMonitor(pid, rpc_probe) as mon:
            result = rpc.submitblock(hexstr)   # blocking
            wall = mon.elapsed
        stats = mon.stats()
    """

    def __init__(self, pid: int, rpc_probe=None, sample_interval: float = 0.02):
        self.pid = pid
        self.rpc_probe = rpc_probe          # callable returning latency in seconds, or None
        self.sample_interval = sample_interval
        self._stop = threading.Event()
        self._thread = None
        self.elapsed = 0.0
        self._cpu_before = 0.0
        self._cpu_after = 0.0
        self._peak_rss = 0
        self._rpc_latencies = []
        self._proc = None

    def __enter__(self):
        try:
            self._proc = psutil.Process(self.pid)
            cpu = self._proc.cpu_times()
            self._cpu_before = cpu.user + cpu.system
            self._peak_rss = self._proc.memory_info().rss
        except (psutil.Error, ProcessLookupError):
            self._proc = None
        self._start = time.perf_counter()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.elapsed = time.perf_counter() - self._start
        if self._proc is not None:
            try:
                cpu = self._proc.cpu_times()
                self._cpu_after = cpu.user + cpu.system
            except (psutil.Error, ProcessLookupError):
                pass
        return False

    def _run(self):
        while not self._stop.is_set():
            if self._proc is not None:
                try:
                    rss = self._proc.memory_info().rss
                    if rss > self._peak_rss:
                        self._peak_rss = rss
                except (psutil.Error, ProcessLookupError):
                    pass
            if self.rpc_probe is not None:
                try:
                    lat = self.rpc_probe()
                    self._rpc_latencies.append(lat)
                except Exception:
                    pass  # node busy/unreachable during validation; ignore
            self._stop.wait(self.sample_interval)

    def stats(self) -> dict:
        return {
            "validation_wall_seconds": round(self.elapsed, 6),
            "validation_cpu_seconds": round(max(0.0, self._cpu_after - self._cpu_before), 6),
            "peak_rss_bytes": self._peak_rss,
            "rpc_probe_count": len(self._rpc_latencies),
            "rpc_probe_max_seconds": round(max(self._rpc_latencies, default=0.0), 6),
            "rpc_probe_median_seconds": round(_median(self._rpc_latencies), 6),
        }


def _median(values):
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def timed_rpc(proxy, method, *args, timeout=30):
    """Call an RPC method and return (result, wall_seconds)."""
    t0 = time.perf_counter()
    result = getattr(proxy, method)(*args)
    return result, time.perf_counter() - t0
