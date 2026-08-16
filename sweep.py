"""Parameter-sweep driver for pba-bench.

Runs a deterministic poison-block construction across a range of ``N`` (poison
inputs) or ``K`` (CHECKSIG per input) while holding the other fixed, with
multiple repeated trials per data point, and reports per-point aggregate
statistics (median / min / max / p25 / p75 / stdev).

This is the tool for answering questions such as:

  * "What does increasing K actually cost on modern Bitcoin Core after the
    SigHashCache is accounted for?"  (sweep --axis k, fixed N)
  * "How does validation time scale with the number of inputs N?"  (sweep --axis n, fixed K)

The cost model (see construction._metrics and research/TECHNICAL_CORRECTIONS.md)
predicts: serialization/hashing O(N^2) (independent of K) plus ECDSA O(N*K).
This sweep measures the empirical curve rather than assuming it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from benchmark import (
    BenchmarkConfig,
    Node,
    _run_one,
    required_blocks,
)
from construction import ConstructionConfig, PoisonBlockGenerator
from measure import summarize
from safety import SafetyError
from schemas import SCHEMA_VERSION


@dataclass
class SweepConfig:
    bitcoind_path: Path = Path("/usr/local/bin/bitcoind")
    axis: str = "k"                     # "n" or "k"
    fixed_value: int = 2000             # value of the non-swept parameter
    values: list = field(default_factory=list)  # the swept values
    runs: int = 3                       # repeated trials per data point
    seed: int = 1
    outdir: Path = Path("results")
    keep_datadir: bool = False
    rpc_host: str = "127.0.0.1"
    max_wall_seconds: int = 600
    max_peak_rss_mb: int = 8192
    max_blocks: int = 400
    max_poison_tx_bytes: int = 3900000
    validation_threads: int = 0
    warm_cold: str = "cold"
    cpu_affinity: str | None = None
    extra_args: list = field(default_factory=list)


def _gen_cfg(axis: str, fixed: int, value: int, seed: int) -> ConstructionConfig:
    if axis == "n":
        return ConstructionConfig(seed=seed, num_utxos=value, sigops_per_input=fixed)
    if axis == "k":
        return ConstructionConfig(seed=seed, num_utxos=fixed, sigops_per_input=value)
    raise ValueError(f"axis must be 'n' or 'k', got {axis!r}")


def run_sweep(cfg: SweepConfig, workspace: Path, log=None, command: str = "") -> dict:
    log = log or (lambda *a: print(*a))
    workspace = Path(workspace)
    outdir = Path(cfg.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    axis_name = {"n": "num_utxos", "k": "sigops_per_input"}[cfg.axis]
    fixed_name = {"n": "sigops_per_input", "k": "num_utxos"}[cfg.axis]

    points = []
    for value in cfg.values:
        gen_cfg = _gen_cfg(cfg.axis, cfg.fixed_value, value, cfg.seed)
        gen_cfg.validate()
        need = required_blocks(gen_cfg)
        if need > cfg.max_blocks:
            log(f"skip {axis_name}={value}: requires {need} blocks > max_blocks={cfg.max_blocks}")
            continue

        trials = []
        for run_idx in range(1, cfg.runs + 1):
            bench = BenchmarkConfig(
                bitcoind_path=cfg.bitcoind_path,
                profile="custom",
                seed=cfg.seed,
                outdir=outdir,
                keep_datadir=cfg.keep_datadir,
                rpc_host=cfg.rpc_host,
                max_wall_seconds=cfg.max_wall_seconds,
                max_peak_rss_mb=cfg.max_peak_rss_mb,
                max_blocks=cfg.max_blocks,
                max_poison_tx_bytes=cfg.max_poison_tx_bytes,
                validation_threads=cfg.validation_threads,
                warm_cold=cfg.warm_cold,
                cpu_affinity=cfg.cpu_affinity,
                extra_args=list(cfg.extra_args),
            )
            node = Node(bench, workspace, log)
            try:
                node.start()
                node.verify_regtest()
                rid = f"sweep-{axis_name}-{value}-r{run_idx}"
                res = _run_one(node, gen_cfg, rid, "sweep", bench, log, command=command)
                res["sweep"] = {
                    "axis": cfg.axis,
                    "axis_value": value,
                    "num_utxos": gen_cfg.num_utxos,
                    "sigops_per_input": gen_cfg.sigops_per_input,
                    "run": run_idx,
                }
                trials.append(res)
            finally:
                node.stop()
        if not trials:
            continue
        points.append(_aggregate_point(value, trials, gen_cfg))

    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": "sweep",
        "axis": cfg.axis,
        "axis_name": axis_name,
        "fixed_name": fixed_name,
        "fixed_value": cfg.fixed_value,
        "values": list(cfg.values),
        "runs_per_point": cfg.runs,
        "seed": cfg.seed,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "command": command,
        "points": points,
    }
    _write_output(result, outdir)
    return result


def _aggregate_point(axis_value: int, trials: list, gen_cfg: ConstructionConfig) -> dict:
    wall = summarize([t["measurement"]["validation_wall_seconds"] for t in trials])
    cpu = summarize([t["measurement"]["validation_cpu_seconds"] for t in trials])
    rss = summarize([t["measurement"]["peak_rss_bytes"] for t in trials])
    r0 = trials[0]["construction"]
    return {
        "axis_value": axis_value,
        "num_utxos": gen_cfg.num_utxos,
        "sigops_per_input": gen_cfg.sigops_per_input,
        "executed_checksig_count": r0.get("executed_checksig_count", 0),
        "sighash_serialization_bytes": r0.get("sighash_serialization_bytes", 0),
        "no_cache_sighash_serialization_bytes": r0.get("no_cache_sighash_serialization_bytes", 0),
        "poison_tx_size_bytes": r0.get("poison_tx_size_bytes", 0),
        "poison_tx_weight": r0.get("poison_tx_weight", 0),
        "outcome": {t["outcome"]["success"] for t in trials},
        "validation_wall_seconds": wall,
        "validation_cpu_seconds": cpu,
        "peak_rss_bytes": rss,
        "trials": len(trials),
        "trial_hashes": [t.get("outcome", {}).get("block_hash", "") for t in trials],
    }


def _write_output(result: dict, outdir: Path) -> None:
    (outdir / "sweep.json").write_text(json.dumps(result, indent=2, default=str))


def sweep_terminal_summary(result: dict) -> str:
    """Human-readable summary of a sweep result."""
    lines = []
    lines.append(f"\n=== sweep axis={result['axis']} "
                 f"(fixed {result['fixed_name']}={result['fixed_value']}, "
                 f"{result['runs_per_point']} trial(s)/point) ===")
    lines.append(f"{result['axis_name']:>8}  {'checksig':>10}  "
                 f"{'serialMB':>9}  {'wall_med':>9}  {'wall_min':>9}  {'wall_max':>9}  "
                 f"{'cpu_med':>9}  outcome")
    for p in result["points"]:
        w, c = p["validation_wall_seconds"], p["validation_cpu_seconds"]
        lines.append(
            f"{p['axis_value']:>8}  {p['executed_checksig_count']:>10}  "
            f"{p['sighash_serialization_bytes'] / 1e6:>9.1f}  "
            f"{w.get('median', 0):>9.4f}  {w.get('min', 0):>9.4f}  {w.get('max', 0):>9.4f}  "
            f"{c.get('median', 0):>9.4f}  {sorted(p['outcome'])}"
        )
    lines.append("")
    return "\n".join(lines)
