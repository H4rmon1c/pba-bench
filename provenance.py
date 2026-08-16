"""Collect provenance information about the node binary, host machine, and the
pba-bench tool itself.

All functions are best-effort and never raise: an unretrievable piece of
provenance is returned as an empty string / None rather than failing a run.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
from pathlib import Path

import psutil


def _run(cmd: list, timeout: int = 15) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return ""


def cpu_model() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def cpu_governor() -> str:
    """Return the CPU frequency governor if it can be read (Linux sysfs)."""
    paths = [
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
        "/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference",
    ]
    for p in paths:
        try:
            with open(p) as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            continue
    return ""


def binary_sha256(path: Path) -> str:
    """SHA-256 of a binary, or '' if it cannot be read."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def git_commit(repo: Path) -> str:
    """Best-effort HEAD commit of a git repository ('' if not a repo)."""
    out = _run(["git", "-C", str(repo), "rev-parse", "HEAD"])
    return out.strip()


def git_dirty(repo: Path) -> bool:
    """Best-effort dirty status of a git working tree."""
    out = _run(["git", "-C", str(repo), "status", "--porcelain"])
    return bool(out.strip())


def node_binary_info(bitcoind: Path) -> dict:
    """Best-effort version/compiler info from ``bitcoind --version``."""
    text = _run([str(bitcoind), "--version"])
    first_lines = [ln for ln in text.splitlines() if ln.strip()][:8]
    version_string = first_lines[0] if first_lines else "unknown"
    # Release builds do not embed a git commit; try to spot one if present.
    commit = ""
    for ln in first_lines:
        m = re.search(r"\b([0-9a-f]{40})\b", ln)
        if m:
            commit = m.group(1)
            break
    return {
        "node_version_string": version_string,
        "node_version_lines": first_lines,
        "node_git_commit": commit or "",
        "compiler": "",          # not exposed by release binaries; see RPC subversion
        "build_type": "",
        "bitcoind_sha256": binary_sha256(bitcoind),
    }


def node_rpc_info(rpc) -> dict:
    """Version info reported by the running node via RPC."""
    try:
        net = rpc.getnetworkinfo()
    except Exception as e:  # pragma: no cover - depends on node
        return {"node_version": None, "node_subversion": "", "rpc_info_error": str(e)}
    return {
        "node_version": net.get("version"),
        "node_subversion": net.get("subversion", ""),
    }


def host_info() -> dict:
    vm = psutil.virtual_memory()
    return {
        "kernel": platform.release(),
        "os_name": platform.system(),
        "machine": platform.machine(),
        "cpu_model": cpu_model(),
        "core_count": psutil.cpu_count(logical=True),
        "physical_cores": psutil.cpu_count(logical=False),
        "total_ram_bytes": vm.total,
        "cpu_governor": cpu_governor(),
    }


def current_process_affinity() -> str:
    """Comma-separated list of CPUs available to the current process ('' if unknown)."""
    try:
        aff = os.sched_getaffinity(0)
        return ",".join(str(c) for c in sorted(aff))
    except (AttributeError, OSError):
        return ""
