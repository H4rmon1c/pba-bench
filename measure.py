"""Measurement helpers for pba-bench.

While a poison block is being validated (a blocking ``submitblock`` call), a
:class:`NodeMonitor` runs in a background thread and samples:

  * the bitcoind process CPU time (user + system),
  * the bitcoind peak resident set size,
  * the outcome + latency of lightweight RPC calls issued *during* validation
    (which demonstrates RPC unresponsiveness while the node is busy validating).

All timing uses ``time.perf_counter`` (monotonic).

RPC probe results are classified so that censored samples (requests that timed
out or errored while the node was busy validating) are *recorded*, never silently
discarded. See :class:`RPCSample`.
"""

from __future__ import annotations

import threading
import time

import psutil

from test_framework.authproxy import JSONRPCException


class ProbeOutcome:
    """Classification of a single RPC probe result."""

    OK = "ok"                        # RPC returned a response; latency is valid
    TIMEOUT = "timeout"              # request exceeded the probe timeout
    CONNECTION_ERROR = "connection_error"  # socket refused/reset etc.
    NODE_SHUTDOWN = "node_shutdown"  # the node process exited
    OTHER_ERROR = "other_error"      # any other exception

    @classmethod
    def classify(cls, exc: Exception, node_alive: bool) -> str:
        if not node_alive:
            return cls.NODE_SHUTDOWN
        if isinstance(exc, JSONRPCException):
            # An RPC-level error (e.g. the server is busy). Still a valid signal
            # that the node is unresponsive, but not a transport timeout.
            return cls.OTHER_ERROR
        return cls.CONNECTION_ERROR


class RPCSample:
    """One recorded RPC probe."""

    __slots__ = ("outcome", "latency_seconds", "lower_bound_seconds")

    def __init__(self, outcome: str, latency_seconds: float,
                 lower_bound_seconds: float):
        self.outcome = outcome
        self.latency_seconds = latency_seconds
        self.lower_bound_seconds = lower_bound_seconds

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "latency_seconds": round(self.latency_seconds, 6),
            "lower_bound_seconds": self.lower_bound_seconds,
        }


class _ProbingThread:
    """Background sampler of node resource usage and RPC latency.

    Each call to ``rpc_probe()`` is expected to either return the wall latency of
    a successful call (float seconds) or raise. Exceptions are classified and
    recorded rather than dropped.
    """

    def __init__(self, pid: int, rpc_probe, sample_interval: float = 0.02,
                 probe_timeout: float = 60.0, node_is_alive=None):
        self.pid = pid
        self.rpc_probe = rpc_probe
        self.sample_interval = sample_interval
        self.probe_timeout = probe_timeout
        self.node_is_alive = node_is_alive or (lambda: True)
        self._stop = threading.Event()
        self._thread = None
        self._samples = []
        self._peak_rss = 0
        self._proc = None
        self._lock = threading.Lock()

    def start(self):
        try:
            self._proc = psutil.Process(self.pid)
            self._peak_rss = self._proc.memory_info().rss
        except (psutil.Error, ProcessLookupError):
            self._proc = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 3.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return self

    def _run(self):
        while not self._stop.is_set():
            if self._proc is not None:
                try:
                    rss = self._proc.memory_info().rss
                    if rss > self._peak_rss:
                        self._peak_rss = rss
                except (psutil.Error, ProcessLookupError):
                    pass
            self._probe_once()
            self._stop.wait(self.sample_interval)

    def _probe_once(self):
        if self.rpc_probe is None:
            return
        t0 = time.perf_counter()
        try:
            self.rpc_probe()
            lat = time.perf_counter() - t0
            sample = RPCSample(ProbeOutcome.OK, lat, 0.0)
        except Exception as exc:
            lat = time.perf_counter() - t0
            outcome = ProbeOutcome.classify(exc, self.node_is_alive())
            sample = RPCSample(outcome, lat, self.probe_timeout if outcome == ProbeOutcome.TIMEOUT else 0.0)
        with self._lock:
            self._samples.append(sample)

    def stats(self) -> dict:
        with self._lock:
            samples = list(self._samples)
        ok = [s.latency_seconds for s in samples if s.outcome == ProbeOutcome.OK]
        return {
            "rpc_probe_count": len(samples),
            "rpc_probe_ok_count": len(ok),
            "rpc_probe_timeout_count": sum(1 for s in samples if s.outcome == ProbeOutcome.TIMEOUT),
            "rpc_probe_error_count": sum(1 for s in samples if s.outcome not in (ProbeOutcome.OK, ProbeOutcome.TIMEOUT)),
            "rpc_probe_timeout_count_exact": sum(1 for s in samples if s.outcome == ProbeOutcome.TIMEOUT),
            "rpc_probe_max_seconds": round(max(ok, default=0.0), 6),
            "rpc_probe_median_seconds": round(_median(ok), 6),
            "rpc_probe_lower_bound_seconds": self.probe_timeout,
            "rpc_probe_samples": [s.as_dict() for s in samples],
        }


class NodeMonitor:
    """Samples node resource usage and RPC latency while a block validates.

    Usage::

        with NodeMonitor(pid, rpc_probe) as mon:
            result = rpc.submitblock(hexstr)   # blocking
            wall = mon.elapsed
        stats = mon.stats()

    ``rpc_probe`` may be a callable returning float-seconds (successful call) or
    raising (timed out / errored). Both outcomes are recorded.
    """

    def __init__(self, pid: int, rpc_probe=None, sample_interval: float = 0.02,
                 probe_timeout: float = 60.0):
        self.pid = pid
        self.probe_timeout = probe_timeout
        self._thread = _ProbingThread(
            pid, rpc_probe, sample_interval=sample_interval,
            probe_timeout=probe_timeout, node_is_alive=lambda: self._alive())
        self.elapsed = 0.0
        self._start = 0.0
        self._cpu_before = 0.0
        self._cpu_after = 0.0

    def _alive(self) -> bool:
        if self._thread._proc is None:
            return True
        try:
            return self._thread._proc.is_running()
        except psutil.Error:
            return True

    def __enter__(self):
        self._thread.start()   # sets _thread._proc; must happen before CPU capture
        proc = self._thread._proc
        if proc is not None:
            try:
                cpu = proc.cpu_times()
                self._cpu_before = cpu.user + cpu.system
            except psutil.Error:
                pass
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self._thread.stop()
        self.elapsed = time.perf_counter() - self._start
        proc = self._thread._proc
        if proc is not None:
            try:
                cpu = proc.cpu_times()
                self._cpu_after = cpu.user + cpu.system
            except psutil.Error:
                pass
        return False

    def stats(self) -> dict:
        s = self._thread.stats()
        s["validation_wall_seconds"] = round(self.elapsed, 6)
        s["validation_cpu_seconds"] = round(max(0.0, self._cpu_after - self._cpu_before), 6)
        s.setdefault("peak_rss_bytes", self._thread._peak_rss)
        return s


def _median(values):
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


class ResourceGuard:
    """Enforces hard resource limits on a running bitcoind.

    Polls the node's RSS and the elapsed wall time from a background thread. When
    a limit is exceeded it calls ``on_violation`` (typically to terminate the
    node) and records the reason. This makes advertised limits real: exceeding
    ``max_rss_mb`` aborts the disposable node instead of silently continuing.
    """

    def __init__(self, pid: int, max_rss_mb: int | None = None,
                 max_wall_seconds: int | None = None,
                 sample_interval: float = 0.2,
                 on_violation=None):
        self.pid = pid
        self.max_rss_mb = max_rss_mb
        self.max_wall_seconds = max_wall_seconds
        self.sample_interval = sample_interval
        self.on_violation = on_violation
        self._stop = threading.Event()
        self._thread = None
        self._violation = None
        self._lock = threading.Lock()
        self._start = time.perf_counter()

    def start(self):
        self._start = time.perf_counter()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return self.violation()

    def violation(self):
        with self._lock:
            return self._violation

    def elapsed(self) -> float:
        return time.perf_counter() - self._start

    def _run(self):
        proc = None
        try:
            proc = psutil.Process(self.pid)
        except (psutil.Error, ProcessLookupError):
            proc = None
        while not self._stop.is_set():
            reason = None
            if self.max_rss_mb is not None and proc is not None:
                try:
                    rss = proc.memory_info().rss
                    if rss > self.max_rss_mb * 1024 * 1024:
                        reason = (f"max_peak_rss_mb={self.max_rss_mb} exceeded "
                                  f"(rss={rss / 1e6:.1f}MB)")
                except (psutil.Error, ProcessLookupError):
                    pass
            if reason is None and self.max_wall_seconds is not None:
                if self.elapsed() > self.max_wall_seconds:
                    reason = (f"max_wall_seconds={self.max_wall_seconds} exceeded "
                              f"(elapsed={self.elapsed():.1f}s)")
            if reason is not None:
                with self._lock:
                    self._violation = reason
                if self.on_violation is not None:
                    try:
                        self.on_violation()
                    except Exception:
                        pass
                return
            self._stop.wait(self.sample_interval)


def timed_rpc(proxy, method, *args, timeout=30):
    """Call an RPC method and return (result, wall_seconds)."""
    t0 = time.perf_counter()
    result = getattr(proxy, method)(*args)
    return result, time.perf_counter() - t0


def percentile(values, p: float):
    """Linear-interpolation percentile (0 <= p <= 100)."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1 if f + 1 < len(vals) else f
    return vals[f] + (vals[c] - vals[f]) * (k - f)


def summarize(values) -> dict:
    """Robust summary of a list of numbers (ignoring None)."""
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0}
    s = sorted(vals)
    import statistics
    return {
        "n": len(vals),
        "min": s[0],
        "p25": percentile(s, 25),
        "median": _median(s),
        "p75": percentile(s, 75),
        "p90": percentile(s, 90),
        "max": s[-1],
        "mean": statistics.mean(s),
        "stdev": statistics.stdev(s) if len(s) > 1 else 0.0,
    }
