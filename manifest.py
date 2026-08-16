"""Reproducibility manifest generation.

Every run writes a ``manifest.json`` next to its results containing everything a
second researcher needs to recreate the run: tool and node provenance, hardware,
parameters, exact command, and result-file hashes. Secrets (RPC passwords) are
never included.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from provenance import git_commit, git_dirty, host_info, node_binary_info
from schemas import SCHEMA_VERSION


def _file_sha256(path: Path) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def build_manifest(*, workspace: Path, bitcoind: Path, command: str,
                   vector: str, num_utxos: int, sigops_per_input: int,
                   seed: int, topology: str | None, observer_config,
                   validation_threads: int, node_args: list,
                   result_files: list) -> dict:
    """Build a reproducibility manifest dict.

    ``result_files`` is a list of Paths whose SHA-256 should be recorded.
    """
    hw = host_info()
    bin_info = node_binary_info(bitcoind)
    workspace = Path(workspace)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "manifest",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pba_bench": {
            "commit": git_commit(workspace),
            "git_dirty": git_dirty(workspace),
            "benchmark_schema_version": SCHEMA_VERSION,
            "command": command,
        },
        "software": {
            "bitcoind_path": str(bitcoind),
            "bitcoind_sha256": bin_info["bitcoind_sha256"],
            "node_version_string": bin_info["node_version_string"],
            "node_git_commit": bin_info["node_git_commit"],
        },
        "hardware": {
            "os_name": hw["os_name"],
            "kernel": hw["kernel"],
            "machine": hw["machine"],
            "cpu_model": hw["cpu_model"],
            "core_count": hw["core_count"],
            "physical_cores": hw["physical_cores"],
            "total_ram_bytes": hw["total_ram_bytes"],
            "cpu_governor": hw.get("cpu_governor", ""),
        },
        "parameters": {
            "vector": vector,
            "num_utxos": num_utxos,
            "sigops_per_input": sigops_per_input,
            "seed": seed,
            "validation_threads": validation_threads,
            "topology": topology,
            "observer_config": observer_config,
            "node_args": list(node_args),
        },
        "result_files": {
            str(p.relative_to(workspace) if p.is_relative_to(workspace) else p): _file_sha256(p)
            for p in result_files
        },
    }


def write_manifest(manifest: dict, outdir: Path) -> Path:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, default=str))
    return path
