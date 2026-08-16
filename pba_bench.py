#!/usr/bin/env python3
"""pba-bench: a safe, reproducible Bitcoin worst-case block validation and
propagation benchmark suite.

Run ``./pba_bench.py --help`` for usage. The tool is regtest-only, loopback-only,
and never touches any public network.

Subcommands
-----------
benchmark      single-node validation benchmark
sweep          sweep N (inputs) or K (CHECKSIG/input) with repeated trials
propagate      multi-observer propagation over a loopback-only topology
compare        cross-binary comparison (BIP 54 A/B or a --manifest matrix)
report         render a markdown report from a results.json
validate       validate an externally-contributed results file
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent


def _add_common(p):
    p.add_argument("--bitcoind", type=Path,
                   help="path to a bitcoind binary (e.g. /opt/bitcoin-core-29/bin/bitcoind)")
    p.add_argument("--seed", type=int, default=1, help="deterministic seed")
    p.add_argument("--outdir", type=Path, default=None,
                   help="output directory for results (default: results/<kind>-<ts>)")
    p.add_argument("--keep-datadir", action="store_true",
                   help="keep the disposable datadir after the run (default: delete)")
    p.add_argument("--rpc-host", default="127.0.0.1", help="loopback RPC host (default 127.0.0.1)")
    p.add_argument("--max-wall-seconds", type=int, default=None)
    p.add_argument("--max-rss-mb", type=int, default=None)
    p.add_argument("--max-blocks", type=int, default=None)
    p.add_argument("--max-poison-tx-bytes", type=int, default=None)
    p.add_argument("--extra-arg", action="append", default=[],
                   help="extra bitcoind argument, e.g. --extra-arg -debug=1 (safety-filtered)")
    p.add_argument("--par", type=int, default=0, metavar="THREADS",
                   help="script validation threads (-par); 0 = node default")
    p.add_argument("--warm-cold", choices=["cold", "warm"], default="cold",
                   help="run against a freshly-started node (cold) or after warmup (warm)")
    p.add_argument("--cpu-affinity", default=None, metavar="CPUS",
                   help="pin each node to CPUs, e.g. '0' or '0,2' (optional)")


def _default_outdir(kind: str):
    import time
    return WORKSPACE / "results" / f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}"


def _validate_vector(name: str):
    from vectors import get_vector
    v = get_vector(name)
    if not v.implemented:
        print(f"ERROR: vector '{name}' is documented but not implemented "
              f"({v.description})", file=sys.stderr)
        sys.exit(2)
    return v


def cmd_benchmark(args) -> int:
    from benchmark import BenchmarkConfig, run_benchmark

    _validate_vector(args.vector)
    bitcoind = args.bitcoind or Path("/usr/local/bin/bitcoind")
    cfg = BenchmarkConfig(
        bitcoind_path=bitcoind,
        profile=args.profile,
        runs=args.runs,
        seed=args.seed,
        outdir=args.outdir or _default_outdir("benchmark"),
        keep_datadir=args.keep_datadir,
        confirm=args.confirm,
        validation_threads=args.par,
        warm_cold=args.warm_cold,
        rpc_host=args.rpc_host,
        extra_args=args.extra_arg,
        cpu_affinity=args.cpu_affinity,
    )
    if args.max_wall_seconds is not None: cfg.max_wall_seconds = args.max_wall_seconds
    if args.max_rss_mb is not None: cfg.max_peak_rss_mb = args.max_rss_mb
    if args.max_blocks is not None: cfg.max_blocks = args.max_blocks
    if args.max_poison_tx_bytes is not None: cfg.max_poison_tx_bytes = args.max_poison_tx_bytes
    if args.num_utxos is not None: cfg.num_utxos = args.num_utxos
    if args.sigops_per_input is not None: cfg.sigops_per_input = args.sigops_per_input
    if args.sweep_utxos: cfg.sweep_utxos = [int(x) for x in args.sweep_utxos.split(",")]
    if args.sweep_sigops: cfg.sweep_sigops = [int(x) for x in args.sweep_sigops.split(",")]

    from benchmark import PROFILE_CONFIRM_REQUIRED, PROFILE_DESCRIPTIONS
    if cfg.profile in PROFILE_CONFIRM_REQUIRED and not args.confirm:
        print(f"ERROR: profile '{cfg.profile}' requires --confirm.", file=sys.stderr)
        print(f"  {PROFILE_DESCRIPTIONS.get(cfg.profile, '')}", file=sys.stderr)
        return 2
    if cfg.profile == "custom" and not args.confirm:
        print("ERROR: --profile custom requires --confirm and explicit limits.", file=sys.stderr)
        return 2

    run_benchmark(cfg, WORKSPACE, command=" ".join(sys.argv))
    return 0


def cmd_sweep(args) -> int:
    from sweep import SweepConfig, run_sweep, sweep_terminal_summary

    if not args.values:
        print("ERROR: sweep requires --values, e.g. --values 1,2,5,10,25,50", file=sys.stderr)
        return 2
    cfg = SweepConfig(
        bitcoind_path=args.bitcoind or Path("/usr/local/bin/bitcoind"),
        axis=args.axis,
        fixed_value=args.fixed,
        values=[int(v) for v in args.values.split(",")],
        runs=args.runs,
        seed=args.seed,
        outdir=args.outdir or _default_outdir("sweep"),
        keep_datadir=args.keep_datadir,
        rpc_host=args.rpc_host,
        validation_threads=args.par,
        cpu_affinity=args.cpu_affinity,
        extra_args=args.extra_arg,
    )
    if args.max_wall_seconds is not None: cfg.max_wall_seconds = args.max_wall_seconds
    if args.max_rss_mb is not None: cfg.max_peak_rss_mb = args.max_rss_mb
    if args.max_blocks is not None: cfg.max_blocks = args.max_blocks
    if args.max_poison_tx_bytes is not None: cfg.max_poison_tx_bytes = args.max_poison_tx_bytes
    result = run_sweep(cfg, WORKSPACE, command=" ".join(sys.argv))
    print(sweep_terminal_summary(result))
    print(f"sweep results written: {cfg.outdir / 'sweep.json'}")
    return 0


def cmd_propagate(args) -> int:
    from benchmark import BenchmarkConfig
    from propagation import PropagationBenchmark, PropagationConfig

    if not args.confirm:
        print("ERROR: --profile propagate requires --confirm (it builds a poison block "
              "and measures its effect on peered local regtest nodes).", file=sys.stderr)
        return 2

    par_values = [int(x) for x in args.observer_par.split(",")] if args.observer_par else [1]
    num_obs = args.observers or len(par_values)
    if num_obs < 1:
        print("ERROR: need at least 1 observer", file=sys.stderr)
        return 2

    bench = BenchmarkConfig(
        bitcoind_path=args.bitcoind or Path("/usr/local/bin/bitcoind"),
        profile="propagate",
        seed=args.seed,
        outdir=args.outdir or _default_outdir("propagate"),
        keep_datadir=args.keep_datadir,
        rpc_host=args.rpc_host,
        extra_args=args.extra_arg,
        max_wall_seconds=args.max_wall_seconds or 3600,
        vector=args.vector,
        cpu_affinity=args.cpu_affinity,
    )
    if args.max_rss_mb is not None: bench.max_peak_rss_mb = args.max_rss_mb
    if args.max_blocks is not None: bench.max_blocks = args.max_blocks

    prop = PropagationConfig(
        seed=args.seed,
        num_utxos=args.num_utxos or 2000,
        sigops_per_input=args.sigops_per_input or 100,
        observer_par=par_values,
        num_observers=num_obs,
        miner_par=args.miner_par,
        topology=args.topology,
    )

    result = PropagationBenchmark(prop, bench, WORKSPACE, print).run()
    _write_propagation_output(result, bench.outdir)
    print(_propagation_summary(result))
    return 0


def _write_propagation_output(result: dict, outdir: Path):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "propagation.json").write_text(json.dumps(result, indent=2, default=str))
    from report import propagation_markdown
    (outdir / "report.md").write_text(propagation_markdown(result))


def _propagation_summary(result: dict) -> str:
    lines = []
    lines.append("\n=== propagation: "
                 f"{result['topology']['kind']} topology, "
                 f"{result['topology']['num_observers']} observer(s) ===")
    lines.append(f"construction: N={result['construction']['num_utxos']} "
                 f"K={result['construction']['sigops_per_input']} "
                 f"({result['construction']['executed_checksig_count']} executed CHECKSIG)")
    lines.append(f"poison block: {result['construction']['poison_block'][:16]}...")
    lines.append(f"miner accepted: {result['miner']['submit_accepted']} "
                 f"(miner validation {result['miner']['miner_validation_seconds']:.3f}s)")
    lines.append("")
    lines.append("observer   par   upstream     time-to-tip   post-miner   rpc-max   rpc-timeouts")
    for o in result["observers"]:
        ttt = o["time_to_tip_seconds"]
        pm = o["post_miner_time_to_tip_seconds"]
        lines.append(
            f"{o['observer_id']:<9} {o['par']:<5} "
            f"{','.join(o['upstream_peers']):<13} "
            f"{'%.3f' % ttt if ttt is not None else 'n/a':<12} "
            f"{'%.3f' % pm if pm is not None else 'n/a':<11} "
            f"{o['rpc_probe_max_seconds']:<8.3f} {o['rpc_probe_timeout_count']}")
    agg = result.get("aggregate_time_to_tip_seconds", {})
    if agg.get("n"):
        lines.append("")
        lines.append(f"aggregate time-to-tip (s): min={agg.get('min')} "
                     f"median={agg.get('median')} p90={agg.get('p90')} "
                     f"max={agg.get('max')} n={agg.get('n')}")
    lines.append("")
    return "\n".join(lines)


def cmd_compare(args) -> int:
    from compare import (
        CompareBinary, CompareConfig, compare_terminal_summary,
        load_manifest, run_compare,
    )

    if args.manifest:
        cfg = load_manifest(args.manifest)
    else:
        binaries = []
        for spec in args.binary:
            name, _, path = spec.partition("=")
            if not path:
                print(f"ERROR: --binary must be NAME=PATH, got {spec!r}", file=sys.stderr)
                return 2
            binaries.append(CompareBinary(name=name, path=Path(path)))
        if args.vanilla:
            binaries.append(CompareBinary(name="vanilla", path=Path(args.vanilla)))
        if args.bip54:
            binaries.append(CompareBinary(name="bip54", path=Path(args.bip54)))
        cfg = CompareConfig(binaries=binaries)
        cfg.vector = args.vector
        cfg.num_utxos = args.num_utxos or 3000
        cfg.sigops_per_input = args.sigops_per_input or 100
        cfg.seed = args.seed
        cfg.validation_threads = args.par
        cfg.outdir = args.outdir or _default_outdir("compare")
        cfg.cpu_affinity = args.cpu_affinity

    if not cfg.binaries:
        print("ERROR: compare needs --binary NAME=PATH, or --manifest FILE, "
              "or --vanilla/--bip54 paths.", file=sys.stderr)
        return 2

    result = run_compare(cfg, WORKSPACE, command=" ".join(sys.argv))
    print(compare_terminal_summary(result))
    print(f"comparison written: {cfg.outdir / 'compare.json'}")
    return 0


def cmd_report(args) -> int:
    from report import generate_report
    md = generate_report(args.json, args.output)
    if args.output is None:
        print(md)
    else:
        print(f"report written: {args.output}")
    return 0


def cmd_validate(args) -> int:
    from schemas import validate_result
    data = json.loads(Path(args.json).read_text())
    results = data if isinstance(data, list) else data.get("rows", data.get("results", [data]))
    problems = []
    for r in results:
        problems.extend(validate_result(r))
    if problems:
        print(f"INVALID: {len(problems)} problem(s):")
        for p in problems[:40]:
            print(f"  - {p}")
        return 1
    print(f"OK: {len(results)} result(s) validate against schema {args.schema or 'current'}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="pba-bench",
        description="Safe, reproducible Bitcoin worst-case block validation and "
                    "propagation benchmark (regtest only).")
    sub = p.add_subparsers(dest="command", required=True)

    pb = sub.add_parser("benchmark", help="single-node validation benchmark")
    _add_common(pb)
    pb.add_argument("--profile", choices=["smoke", "small", "medium", "custom"], default="smoke")
    pb.add_argument("--runs", type=int, default=1)
    pb.add_argument("--confirm", action="store_true",
                    help="acknowledge running a larger/custom benchmark case")
    pb.add_argument("--num-utxos", type=int, default=None, help="override N (poison inputs)")
    pb.add_argument("--sigops-per-input", type=int, default=None, help="override K (CHECKSIG/input)")
    pb.add_argument("--vector", choices=["scriptpubkey"], default="scriptpubkey")
    pb.add_argument("--sweep-utxos", default=None, help="comma-separated N values (one run each)")
    pb.add_argument("--sweep-sigops", default=None, help="comma-separated K values (one run each)")
    pb.set_defaults(func=cmd_benchmark)

    ps = sub.add_parser("sweep", help="sweep N or K with repeated trials per point")
    _add_common(ps)
    ps.add_argument("--axis", choices=["n", "k"], default="k")
    ps.add_argument("--fixed", type=int, default=2000,
                    help="value of the non-swept parameter (default 2000)")
    ps.add_argument("--values", default=None,
                    help="comma-separated swept values, e.g. 1,2,5,10,25,50")
    ps.add_argument("--runs", type=int, default=3, help="repeated trials per data point")
    ps.set_defaults(func=cmd_sweep)

    pp = sub.add_parser("propagate",
                        help="multi-observer propagation over a loopback-only topology")
    _add_common(pp)
    pp.add_argument("--confirm", action="store_true",
                    help="acknowledge running the multi-node propagation demo")
    pp.add_argument("--num-utxos", type=int, default=None, help="poison inputs (N), default 2000")
    pp.add_argument("--sigops-per-input", type=int, default=None, help="CHECKSIG/input (K), default 100")
    pp.add_argument("--observer-par", default="1",
                    help="comma-separated validation threads per observer, e.g. 1,2,4,8,0")
    pp.add_argument("--miner-par", type=int, default=0,
                    help="validation threads on the miner (0 = default)")
    pp.add_argument("--observers", type=int, default=None,
                    help="number of observers (default: number of --observer-par values)")
    pp.add_argument("--topology", choices=["star", "line", "tree"], default="star")
    pp.add_argument("--vector", choices=["scriptpubkey"], default="scriptpubkey")
    pp.set_defaults(func=cmd_propagate)

    pc = sub.add_parser("compare", help="cross-binary comparison (BIP54 A/B or manifest)")
    _add_common(pc)
    pc.add_argument("--manifest", type=Path, default=None,
                    help="path to a JSON manifest of binaries + parameters")
    pc.add_argument("--binary", action="append", default=[],
                    help="NAME=PATH, repeatable")
    pc.add_argument("--vanilla", default=None, help="path to a vanilla bitcoind")
    pc.add_argument("--bip54", default=None, help="path to a BIP54 bitcoind build")
    pc.add_argument("--num-utxos", type=int, default=None)
    pc.add_argument("--sigops-per-input", type=int, default=None)
    pc.add_argument("--vector", choices=["scriptpubkey"], default="scriptpubkey")
    pc.set_defaults(func=cmd_compare)

    pr = sub.add_parser("report", help="generate a markdown report from a results.json")
    pr.add_argument("json", type=Path)
    pr.add_argument("--output", type=Path, default=None)
    pr.set_defaults(func=cmd_report)

    pv = sub.add_parser("validate", help="validate an externally-contributed results file")
    pv.add_argument("json", type=Path)
    pv.add_argument("--schema", default=None)
    pv.set_defaults(func=cmd_validate)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
