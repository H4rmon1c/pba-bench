"""Cross-binary comparison driver for pba-bench.

Runs one *identical, deterministic* poison-block construction against multiple
bitcoind binaries and emits a comparable matrix with full provenance per binary.

Two entry points:

  * ``compare vanilla=<path> bip54=<path>``  -> BIP 54 A/B (vanilla vs Consensus
    Cleanup build), the highest-value mitigation-validation workflow.
  * ``compare --manifest core-builds.json``   -> arbitrary cross-version matrix
    (e.g. Core 29 / 30 / 31 / master / BIP54).

Every row records the exact binary (path, SHA-256, --version, RPC subversion),
the construction parameters, the measured outcome, and timing. A BIP54
rejection is reported as *live* only when the supplied binary actually rejects
the block with ``bad-txns-legacy-sigops``; otherwise it is marked *inferred*.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from benchmark import BenchmarkConfig, Node, _run_one
from construction import ConstructionConfig
from measure import summarize
from provenance import git_commit, node_binary_info
from safety import SafetyError
from schemas import SCHEMA_VERSION


@dataclass
class CompareBinary:
    name: str
    path: Path
    extra_args: list = field(default_factory=list)


@dataclass
class CompareConfig:
    binaries: list = field(default_factory=list)   # list[CompareBinary]
    vector: str = "scriptpubkey"
    num_utxos: int = 3000
    sigops_per_input: int = 100
    seed: int = 1
    runs: int = 1
    validation_threads: int = 0
    outdir: Path = Path("results")
    keep_datadir: bool = False
    rpc_host: str = "127.0.0.1"
    max_wall_seconds: int = 600
    max_peak_rss_mb: int = 8192
    max_blocks: int = 400
    max_poison_tx_bytes: int = 3900000
    cpu_affinity: str | None = None


def load_manifest(path: Path) -> CompareConfig:
    """Load a comparison manifest file."""
    data = json.loads(Path(path).read_text())
    binaries = []
    for b in data["binaries"]:
        binaries.append(CompareBinary(
            name=b.get("name", Path(b["path"]).name),
            path=Path(b["path"]),
            extra_args=list(b.get("extra_args", [])),
        ))
    cfg = CompareConfig(binaries=binaries)
    for k in ("vector", "num_utxos", "sigops_per_input", "seed", "runs",
              "validation_threads", "max_wall_seconds", "max_peak_rss_mb",
              "max_blocks", "max_poison_tx_bytes"):
        if k in data:
            setattr(cfg, k, data[k])
    if "outdir" in data:
        cfg.outdir = Path(data["outdir"])
    return cfg


def _run_binary(bin_: CompareBinary, cfg: CompareConfig, workspace: Path,
                log, command: str, run_idx: int) -> dict:
    gen_cfg = ConstructionConfig(seed=cfg.seed, num_utxos=cfg.num_utxos,
                                 sigops_per_input=cfg.sigops_per_input)
    gen_cfg.validate()
    bench = BenchmarkConfig(
        bitcoind_path=bin_.path,
        profile="compare",
        seed=cfg.seed,
        outdir=cfg.outdir,
        keep_datadir=cfg.keep_datadir,
        rpc_host=cfg.rpc_host,
        max_wall_seconds=cfg.max_wall_seconds,
        max_peak_rss_mb=cfg.max_peak_rss_mb,
        max_blocks=cfg.max_blocks,
        max_poison_tx_bytes=cfg.max_poison_tx_bytes,
        validation_threads=cfg.validation_threads,
        cpu_affinity=cfg.cpu_affinity,
        extra_args=list(bin_.extra_args),
    )
    node = Node(bench, workspace, log)
    try:
        node.start()
        node.verify_regtest()
        rid = f"compare-{_slug(bin_.name)}-r{run_idx}"
        res = _run_one(node, gen_cfg, rid, "compare", bench, log, command=command)
        return res
    finally:
        node.stop()


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "bin"


def run_compare(cfg: CompareConfig, workspace: Path, log=None,
                command: str = "") -> dict:
    log = log or (lambda *a: print(*a))
    workspace = Path(workspace)
    outdir = Path(cfg.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if not cfg.binaries:
        raise SafetyError("compare needs at least one binary (--manifest or key=path)")

    rows = []
    for bin_ in cfg.binaries:
        bin_.path = Path(bin_.path).resolve()
        if not bin_.path.is_file():
            raise SafetyError(f"bitcoind not found: {bin_.path}")
        log(f"\n=== binary: {bin_.name} ({bin_.path}) ===")
        bin_info = node_binary_info(bin_.path)
        log(f"  version: {bin_info['node_version_string'].strip()}")
        log(f"  sha256 : {bin_info['bitcoind_sha256'][:32]}...")

        trials = [_run_binary(bin_, cfg, workspace, log, command, i + 1)
                  for i in range(cfg.runs)]
        t0 = trials[0]
        wall = summarize([t["measurement"]["validation_wall_seconds"] for t in trials])
        cpu = summarize([t["measurement"]["validation_cpu_seconds"] for t in trials])
        rows.append({
            "name": bin_.name,
            "bitcoind_path": str(bin_.path),
            "bitcoind_sha256": bin_info["bitcoind_sha256"],
            "node_version_string": t0["provenance"]["node_version_string"],
            "node_subversion": t0["provenance"]["node_subversion"],
            "node_git_commit": t0["provenance"]["node_git_commit"],
            "outcomes": sorted({t["outcome"]["success"] for t in trials}),
            "rejection_reasons": sorted({t["outcome"]["rejection_reason"] for t in trials}),
            "bip54_results": sorted({t["outcome"]["bip54_result"] for t in trials}),
            "bip54_would_reject": t0["outcome"]["bip54_would_reject"],
            "validation_wall_seconds": wall,
            "validation_cpu_seconds": cpu,
            "construction": t0["construction"],
            "trials": trials,
        })

    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": "compare",
        "vector": cfg.vector,
        "num_utxos": cfg.num_utxos,
        "sigops_per_input": cfg.sigops_per_input,
        "seed": cfg.seed,
        "validation_threads": cfg.validation_threads,
        "runs": cfg.runs,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "command": command,
        "pba_bench_commit": git_commit(workspace),
        "rows": rows,
    }
    (outdir / "compare.json").write_text(json.dumps(result, indent=2, default=str))
    return result


def compare_terminal_summary(result: dict) -> str:
    lines = []
    lines.append("\n=== cross-binary comparison ===")
    lines.append(f"construction: vector={result['vector']} "
                 f"N={result['num_utxos']} K={result['sigops_per_input']} "
                 f"seed={result['seed']} par={result['validation_threads']}")
    lines.append("")
    lines.append(f"{'binary':<16} {'sha256':<20} {'outcome':<12} "
                 f"{'wall_med':>9} {'cpu_med':>9}  bip54")
    for r in result["rows"]:
        w = r["validation_wall_seconds"]
        lines.append(
            f"{r['name'][:15]:<16} {r['bitcoind_sha256'][:18]:<20} "
            f"{','.join(r['outcomes']):<12} "
            f"{w.get('median', 0):>9.4f} {r['validation_cpu_seconds'].get('median', 0):>9.4f}  "
            f"{','.join(r['bip54_results'])}"
        )
    lines.append("")
    return "\n".join(lines)
