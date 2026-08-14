"""Markdown report generator for pba-bench result sets."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path


def _fmt_bytes(n) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1000.0


def _fmt_seconds(s) -> str:
    return f"{s:.4f}" if s is not None else "n/a"


def _summarize(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return {}
    return {
        "n": len(vals),
        "median": statistics.median(vals),
        "min": min(vals),
        "max": max(vals),
        "mean": statistics.mean(vals),
        "stdev": statistics.stdev(vals) if len(vals) > 1 else 0.0,
    }


def generate_report(results_json: Path, out_md: Path = None) -> str:
    data = json.loads(Path(results_json).read_text())
    results = data if isinstance(data, list) else data.get("results", [])

    lines = []
    lines.append("# pba-bench: Poison Block Attack benchmark report\n")

    # Group by construction parameters for scaling presentation.
    groups = defaultdict(list)
    for r in results:
        key = (r["construction"]["num_utxos"], r["construction"]["sigops_per_input"])
        groups[key].append(r)

    # Header / environment summary.
    if results:
        r0 = results[0]
        prov = r0["provenance"]
        lines.append("## Environment\n")
        lines.append(f"- Node: `{prov['node_version_string']}`  (RPC version {prov['node_version']}, "
                     f"subversion `{prov['node_subversion']}`)")
        if prov["node_git_commit"]:
            lines.append(f"- Git commit: `{prov['node_git_commit']}`")
        lines.append(f"- bitcoind: `{prov['bitcoind_path']}`")
        lines.append(f"- CPU: {prov['cpu_model']} ({prov['core_count']} logical / "
                     f"{prov['physical_cores']} physical cores)")
        lines.append(f"- OS: {prov['os_name']} {prov['kernel']} ({prov['machine']})")
        lines.append(f"- RAM: {_fmt_bytes(prov['total_ram_bytes'])}")
        lines.append("")

    lines.append("## Results per construction\n")
    for (n, k), rs in sorted(groups.items()):
        lines.append(f"### N={n} inputs, K={k} CHECKSIG/input\n")
        c = rs[0]["construction"]
        lines.append(f"- Legacy sigops (BIP 54 accounting): **{c['total_legacy_sigops_bip54']}**"
                     f"  (BIP 54 limit: 2500)")
        lines.append(f"- Poison tx size: {_fmt_bytes(c['poison_tx_size_bytes'])}; "
                     f"weight {c['poison_tx_weight']} (limit 4,000,000)")
        lines.append(f"- Prep blocks: {c['num_prep_blocks']}")
        lines.append(f"- Expected sighash preimage bytes (cache-aware): "
                     f"{_fmt_bytes(c['expected_sighash_preimage_bytes'])}; "
                     f"theoretical no-cache: {_fmt_bytes(c['theoretical_sighash_preimage_bytes_no_cache'])}")
        lines.append("")

        wall = _summarize([r["measurement"]["validation_wall_seconds"] for r in rs])
        cpu = _summarize([r["measurement"]["validation_cpu_seconds"] for r in rs])
        rss = _summarize([r["measurement"]["peak_rss_bytes"] for r in rs])
        rpc_max = _summarize([r["measurement"]["rpc_probe_max_seconds"] for r in rs])

        lines.append("| metric | median | min | max | n |")
        lines.append("|---|---|---|---|---|")
        lines.append(f"| validation wall time (s) | {_fmt_seconds(wall.get('median'))} "
                     f"| {_fmt_seconds(wall.get('min'))} | {_fmt_seconds(wall.get('max'))} "
                     f"| {wall.get('n', 0)} |")
        lines.append(f"| validation CPU time (s) | {_fmt_seconds(cpu.get('median'))} "
                     f"| {_fmt_seconds(cpu.get('min'))} | {_fmt_seconds(cpu.get('max'))} "
                     f"| {cpu.get('n', 0)} |")
        lines.append(f"| peak RSS | {_fmt_bytes(rss.get('median'))} "
                     f"| {_fmt_bytes(rss.get('min'))} | {_fmt_bytes(rss.get('max'))} "
                     f"| {rss.get('n', 0)} |")
        lines.append(f"| RPC probe max latency (s) | {_fmt_seconds(rpc_max.get('median'))} "
                     f"| {_fmt_seconds(rpc_max.get('min'))} | {_fmt_seconds(rpc_max.get('max'))} "
                     f"| {rpc_max.get('n', 0)} |")
        lines.append("")

        for r in rs:
            out = r["outcome"]
            lines.append(f"- Run `{r['run']['run_id']}`: **{out['success']}**"
                         + (f" (`{out['rejection_reason']}`)" if out['rejection_reason'] else ""))
        lines.append("")

    # Outcome summary across all runs.
    counts = defaultdict(int)
    for r in results:
        counts[r["outcome"]["success"]] += 1
    lines.append("## Overall outcome\n")
    for k, v in sorted(counts.items()):
        lines.append(f"- {k}: {v}")
    lines.append("")

    md = "\n".join(lines)
    if out_md is not None:
        Path(out_md).write_text(md)
    return md
