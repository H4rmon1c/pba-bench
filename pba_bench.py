#!/usr/bin/env python3
"""pba-bench: safe, reproducible Bitcoin Poison Block Attack benchmark.

Run ``./pba_bench.py --help`` for usage. The tool is regtest-only and never
touches any public network.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent


def _add_common(p):
    p.add_argument("--bitcoind", required=True, type=Path,
                   help="path to a bitcoind binary (e.g. /opt/bitcoin-core-29/bin/bitcoind)")
    p.add_argument("--seed", type=int, default=1, help="deterministic seed")
    p.add_argument("--outdir", type=Path, default=WORKSPACE / "results",
                   help="output directory for results")
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


def cmd_benchmark(args) -> int:
    from benchmark import BenchmarkConfig, run_benchmark

    cfg = BenchmarkConfig(
        bitcoind_path=args.bitcoind,
        profile=args.profile,
        runs=args.runs,
        seed=args.seed,
        outdir=args.outdir,
        keep_datadir=args.keep_datadir,
        confirm=args.confirm,
        validation_threads=args.par,
        warm_cold=args.warm_cold,
        rpc_host=args.rpc_host,
        extra_args=args.extra_arg,
    )
    if args.max_wall_seconds is not None: cfg.max_wall_seconds = args.max_wall_seconds
    if args.max_rss_mb is not None: cfg.max_peak_rss_mb = args.max_rss_mb
    if args.max_blocks is not None: cfg.max_blocks = args.max_blocks
    if args.max_poison_tx_bytes is not None: cfg.max_poison_tx_bytes = args.max_poison_tx_bytes
    if args.num_utxos is not None: cfg.num_utxos = args.num_utxos
    if args.sigops_per_input is not None: cfg.sigops_per_input = args.sigops_per_input
    if args.sweep_utxos: cfg.sweep_utxos = [int(x) for x in args.sweep_utxos.split(",")]
    if args.sweep_sigops: cfg.sweep_sigops = [int(x) for x in args.sweep_sigops.split(",")]

    # Require an explicit confirmation for larger profiles.
    from benchmark import PROFILE_CONFIRM_REQUIRED, PROFILE_DESCRIPTIONS
    if cfg.profile in PROFILE_CONFIRM_REQUIRED and not args.confirm:
        print(f"ERROR: profile '{cfg.profile}' requires --confirm.", file=sys.stderr)
        print(f"  {PROFILE_DESCRIPTIONS.get(cfg.profile, '')}", file=sys.stderr)
        return 2
    if cfg.profile == "custom" and not args.confirm:
        print("ERROR: --profile custom requires --confirm and explicit limits.", file=sys.stderr)
        return 2

    run_benchmark(cfg, WORKSPACE)
    return 0


def cmd_report(args) -> int:
    from report import generate_report
    md = generate_report(args.json, args.output)
    if args.output is None:
        print(md)
    else:
        print(f"report written: {args.output}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="pba-bench",
        description="Safe, reproducible Bitcoin Poison Block Attack benchmark (regtest only).")
    sub = p.add_subparsers(dest="command", required=True)

    pb = sub.add_parser("benchmark", help="run the benchmark")
    _add_common(pb)
    pb.add_argument("--profile", choices=["smoke", "small", "medium", "custom"], default="smoke")
    pb.add_argument("--runs", type=int, default=1)
    pb.add_argument("--confirm", action="store_true",
                    help="acknowledge running a larger/custom benchmark case")
    pb.add_argument("--num-utxos", type=int, default=None, help="override N (poison inputs)")
    pb.add_argument("--sigops-per-input", type=int, default=None, help="override K (CHECKSIG/input)")
    pb.add_argument("--vector", choices=["scriptpubkey"], default="scriptpubkey")
    pb.add_argument("--sweep-utxos", default=None,
                    help="comma-separated N values to sweep (one run each)")
    pb.add_argument("--sweep-sigops", default=None,
                    help="comma-separated K values to sweep (one run each)")
    pb.set_defaults(func=cmd_benchmark)

    pr = sub.add_parser("report", help="generate a markdown report from a results.json")
    pr.add_argument("json", type=Path)
    pr.add_argument("--output", type=Path, default=None)
    pr.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
