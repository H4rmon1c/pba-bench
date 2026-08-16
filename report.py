"""Markdown report generator for pba-bench result sets.

Produces research-grade markdown that separates *directly measured* from
*derived/calculated* and *inferred* claims, and includes provenance, topology,
exact command, limitations, and a safety statement (see Phase 13 / 17).
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

from schemas import migrate_v1_result


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


def _provenance_section(prov: dict, lines: list) -> None:
    lines.append("## Software and hardware provenance\n")
    lines.append(f"- Node: `{prov.get('node_version_string')}`  "
                 f"(RPC version {prov.get('node_version')}, "
                 f"subversion `{prov.get('node_subversion')}`)")
    if prov.get("node_git_commit"):
        lines.append(f"- Node git commit: `{prov['node_git_commit']}`")
    lines.append(f"- bitcoind path: `{prov.get('bitcoind_path')}`")
    if prov.get("bitcoind_sha256"):
        lines.append(f"- bitcoind SHA-256: `{prov['bitcoind_sha256']}`")
    lines.append(f"- pba-bench commit: `{prov.get('pba_bench_commit') or 'unknown'}`")
    lines.append(f"- CPU: {prov.get('cpu_model')} "
                 f"({prov.get('core_count')} logical / {prov.get('physical_cores')} "
                 f"physical cores)")
    lines.append(f"- OS: {prov.get('os_name')} {prov.get('kernel')} "
                 f"({prov.get('machine')})")
    lines.append(f"- RAM: {_fmt_bytes(prov.get('total_ram_bytes'))}")
    lines.append(f"- validation threads (-par): {prov.get('validation_threads')} "
                 f"(0 = node default)")
    lines.append(f"- CPU affinity: `{prov.get('cpu_affinity') or 'unset'}`")
    lines.append("")


def _construction_section(c: dict, lines: list, measured_header: bool = True) -> None:
    lines.append("## Construction\n")
    lines.append(f"- Vector: `{c.get('vector', 'scriptpubkey')}`")
    lines.append(f"- Poison inputs N: {c.get('num_utxos')}")
    lines.append(f"- CHECKSIG per input K: {c.get('sigops_per_input')}")
    lines.append(f"- Executed CHECKSIG: {c.get('executed_checksig_count')}")
    lines.append(f"- BIP 54-accounted legacy sigops: {c.get('total_legacy_sigops_bip54')} "
                 f"(BIP 54 limit: 2500)")
    lines.append(f"- Poison tx size: {_fmt_bytes(c.get('poison_tx_size_bytes'))}; "
                 f"weight {c.get('poison_tx_weight')} (limit 4,000,000)")
    lines.append(f"- Prep blocks: {c.get('num_prep_blocks')}")
    if measured_header:
        lines.append("\n**Measured-by-construction cost quantities (v31.1.0 model):**\n")
        lines.append(f"- Legacy sighash serialization bytes: "
                     f"{_fmt_bytes(c.get('sighash_serialization_bytes'))} "
                     f"(O(N^2); per-input SigHashCache collapses repeated CHECKSIG)")
        lines.append(f"- Legacy sighash double-SHA256 bytes: "
                     f"{_fmt_bytes(c.get('sighash_double_sha256_bytes'))}")
        lines.append(f"- ECDSA verifications: {c.get('ecdsa_verify_count')} "
                     f"(O(N*K); one fresh verify per CHECKSIG during block connect)")
        lines.append("\n**Hypothetical (NOT v31.1.0 behavior):**\n")
        lines.append(f"- No-cache sighash serialization bytes: "
                     f"{_fmt_bytes(c.get('no_cache_sighash_serialization_bytes'))} "
                     f"(what an implementation without the per-input midstate cache "
                     f"would serialize; does not describe v31.1.0)")
    lines.append("")


def _measurement_table(rs, lines: list) -> None:
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


def generate_report(results_json: Path, out_md: Path = None) -> str:
    data = json.loads(Path(results_json).read_text())

    # Dispatch by result kind.
    if isinstance(data, dict) and data.get("kind") == "propagation":
        md = propagation_markdown(data)
    elif isinstance(data, dict) and data.get("kind") == "compare":
        md = compare_markdown(data)
    elif isinstance(data, dict) and data.get("kind") == "sweep":
        md = sweep_markdown(data)
    else:
        md = benchmark_report_markdown(data)

    if out_md is not None:
        Path(out_md).write_text(md)
    return md


def benchmark_report_markdown(results) -> str:
    results = [migrate_v1_result(r) for r in results] if isinstance(results, list) else results
    results = results if isinstance(results, list) else results.get("results", [])
    lines = []
    lines.append("# pba-bench: worst-case block validation benchmark report\n")

    groups = defaultdict(list)
    for r in results:
        key = (r["construction"]["num_utxos"], r["construction"]["sigops_per_input"])
        groups[key].append(r)

    if results:
        _provenance_section(results[0]["provenance"], lines)

    lines.append("## Results per construction\n")
    for (n, k), rs in sorted(groups.items()):
        lines.append(f"### N={n} inputs, K={k} CHECKSIG/input\n")
        _construction_section(rs[0]["construction"], lines)
        _measurement_table(rs, lines)
        for r in rs:
            out = r["outcome"]
            lines.append(f"- Run `{r['run']['run_id']}`: **{out['success']}**"
                         + (f" (`{out['rejection_reason']}`)" if out['rejection_reason'] else ""))
        lines.append("")

    counts = defaultdict(int)
    for r in results:
        counts[r["outcome"]["success"]] += 1
    lines.append("## Overall outcome\n")
    for k, v in sorted(counts.items()):
        lines.append(f"- {k}: {v}")
    lines.append("")

    lines.append(_safety_statement())
    return "\n".join(lines)


def propagation_markdown(result: dict) -> str:
    lines = []
    lines.append("# pba-bench: multi-observer propagation report\n")
    lines.append(f"- Topology: `{result['topology']['kind']}` "
                 f"({result['topology']['num_observers']} observers)")
    edges = ", ".join(f"{a}->{b}" for a, b in result["topology"]["edges"])
    lines.append(f"- Edges: {edges}")
    lines.append(f"- Observer -par: {result['topology']['observer_par']}")
    lines.append("")
    _construction_section(result["construction"], lines)
    lines.append("## Miner\n")
    lines.append(f"- submit accepted: {result['miner']['submit_accepted']}")
    lines.append(f"- miner validation (seconds): "
                 f"{result['miner']['miner_validation_seconds']}")
    lines.append("")
    lines.append("## Per-observer (independent measurement context)\n")
    lines.append("| obs | par | upstream | time-to-tip (s) | post-miner (s) | "
                 "rpc-max (s) | rpc-timeouts | success |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for o in result["observers"]:
        lines.append(
            f"| {o['observer_id']} | {o['par']} | "
            f"{','.join(o['upstream_peers'])} | "
            f"{_fmt_seconds(o['time_to_tip_seconds'])} | "
            f"{_fmt_seconds(o['post_miner_time_to_tip_seconds'])} | "
            f"{o['rpc_probe_max_seconds']} | {o['rpc_probe_timeout_count']} | "
            f"{o['success']} |")
    lines.append("")
    agg = result.get("aggregate_time_to_tip_seconds", {})
    if agg.get("n"):
        lines.append("## Aggregate time-to-tip (seconds)\n")
        lines.append(f"- n={agg.get('n')}, min={agg.get('min')}, "
                     f"median={agg.get('median')}, p90={agg.get('p90')}, "
                     f"max={agg.get('max')}, stdev={agg.get('stdev')}")
        lines.append("")
    lines.append("## Terminology\n")
    lines.append("`time_to_tip_seconds` spans miner submit -> observer active-tip "
                 "transition and includes P2P transmission, the observer's block "
                 "validation, and tip activation. It is **not** pure wire "
                 "propagation. `post_miner_time_to_tip_seconds` subtracts the "
                 "miner's own validation. P2P wire transmission alone is not "
                 "isolated by this report.\n")
    lines.append(_safety_statement())
    return "\n".join(lines)


def compare_markdown(result: dict) -> str:
    lines = []
    lines.append("# pba-bench: cross-binary comparison report\n")
    lines.append(f"- construction: vector={result['vector']}, "
                 f"N={result['num_utxos']}, K={result['sigops_per_input']}, "
                 f"seed={result['seed']}, par={result['validation_threads']}")
    lines.append(f"- pba-bench commit: {result.get('pba_bench_commit') or 'unknown'}")
    lines.append("")
    lines.append("| binary | sha256 | outcome | wall-med (s) | cpu-med (s) | "
                 "bip54 | bip54-would-reject |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in result["rows"]:
        w = r["validation_wall_seconds"]
        lines.append(
            f"| {r['name']} | `{r['bitcoind_sha256'][:16]}...` | "
            f"{','.join(r['outcomes'])} | {w.get('median', '')} | "
            f"{r['validation_cpu_seconds'].get('median', '')} | "
            f"{','.join(r['bip54_results'])} | {r['bip54_would_reject']} |")
    lines.append("")
    lines.append("**Measured vs inferred:** a `live` BIP54 rejection means the "
                 "supplied BIP54 binary actually rejected the block with "
                 "`bad-txns-legacy-sigops`. `inferred` means no such binary was "
                 "tested and the rejection is predicted from the sigop count.\n")
    lines.append(_safety_statement())
    return "\n".join(lines)


def sweep_markdown(result: dict) -> str:
    lines = []
    axis_name = result["axis_name"]
    lines.append(f"# pba-bench: {axis_name}-sweep report\n")
    lines.append(f"- axis={result['axis']}, fixed {result['fixed_name']}="
                 f"{result['fixed_value']}, values={result['values']}, "
                 f"{result['runs_per_point']} trial(s)/point")
    lines.append("")
    lines.append("| value | checksig | serialMB | wall-med | wall-min | wall-max | cpu-med |")
    lines.append("|---|---|---|---|---|---|---|")
    for p in result["points"]:
        w, c = p["validation_wall_seconds"], p["validation_cpu_seconds"]
        lines.append(f"| {p['axis_value']} | {p['executed_checksig_count']} | "
                     f"{p['sighash_serialization_bytes'] / 1e6:.1f} | "
                     f"{w.get('median', '')} | {w.get('min', '')} | {w.get('max', '')} | "
                     f"{c.get('median', '')} |")
    lines.append("")
    lines.append("These are empirical measurements; no scaling law is inferred "
                 "beyond what the data support. See "
                 "research/TECHNICAL_CORRECTIONS.md for the v31.1.0 cost model.\n")
    lines.append("**Measurement note:** for sub-100 ms validations the process-CPU "
                 "time is unreliable because `/proc/<pid>/stat` CPU ticks are coarse "
                 "(~10 ms each); CPU figures are only trustworthy for runs lasting "
                 "well over 100 ms. Serialization bytes are deterministic (derived "
                 "from the construction) and exact.\n")
    lines.append(_safety_statement())
    return "\n".join(lines)


def _safety_statement() -> str:
    return ("\n---\n## Safety statement\n\n"
            "This run executed **regtest-only**, **loopback-only** disposable "
            "Bitcoin Core nodes with fresh datadirs. No public network, Tor, I2P, "
            "LAN, or reused datadir was contacted. The poison block was never "
            "broadcast outside the controlled local environment.\n")
