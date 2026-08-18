"""Controller for pba-bench.

Launches a fresh, isolated regtest ``bitcoind``, builds a poison block with the
deterministic construction, measures the blocking ``submitblock`` call, records
provenance and outcome, and writes JSON + CSV results.
"""

from __future__ import annotations

import csv
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from test_framework.authproxy import AuthServiceProxy, JSONRPCException

import construction
from construction import ConstructionConfig, PoisonBlockGenerator
from measure import NodeMonitor, ResourceGuard, timed_rpc
from provenance import (
    current_process_affinity,
    git_commit,
    host_info,
    node_binary_info,
)
from safety import (
    DEFAULT_LIMITS,
    SafetyError,
    SafetyValidator,
    SafeConfig,
    verify_chain_is_regtest,
)
from schemas import SCHEMA_VERSION, csv_columns, flat_result

# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #

PROFILES = {
    # name: (num_utxos, sigops_per_input, needs_confirm, description)
    "smoke": construction.ConstructionConfig(num_utxos=10, sigops_per_input=2),
    "small": construction.ConstructionConfig(num_utxos=500, sigops_per_input=6),
    "medium": construction.ConstructionConfig(num_utxos=2500, sigops_per_input=4),
}

PROFILE_CONFIRM_REQUIRED = {"medium"}
PROFILE_DESCRIPTIONS = {
    "smoke": "verifies construction + acceptance in seconds (no --confirm needed)",
    "small": "measurable but low-impact validation case (no --confirm needed)",
    "medium": "larger case; requires --confirm",
}


def profile_config(profile: str, overrides: dict) -> ConstructionConfig:
    if profile == "custom":
        cfg = ConstructionConfig()
    elif profile in PROFILES:
        cfg = ConstructionConfig(**vars(PROFILES[profile]))
    else:
        raise SafetyError(
            f"unknown profile {profile!r}; choose from {sorted(PROFILES)} or use --custom"
        )
    for k, v in overrides.items():
        if v is not None and hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclass
class BenchmarkConfig:
    bitcoind_path: Path
    profile: str = "smoke"
    runs: int = 1
    seed: int = 1
    outdir: Path = Path("results")
    keep_datadir: bool = False
    confirm: bool = False
    validation_threads: int = 0          # 0 = node default (-par)
    warm_cold: str = "cold"              # cold|warm
    rpc_host: str = "127.0.0.1"
    max_wall_seconds: int = DEFAULT_LIMITS["max_wall_seconds"]
    max_peak_rss_mb: int = DEFAULT_LIMITS["max_peak_rss_mb"]
    max_blocks: int = DEFAULT_LIMITS["max_blocks"]
    max_poison_tx_bytes: int = DEFAULT_LIMITS["max_poison_tx_bytes"]
    extra_args: list = field(default_factory=list)
    cpu_affinity: str | None = None   # e.g. "0" or "0,2"; None = inherit
    bip54: bool = False               # pass -vbparams to activate BIP54 on regtest
    activate_bip54: bool = False      # mine through the BIP9 cycle before construction
    # construction overrides
    num_utxos: int | None = None
    sigops_per_input: int | None = None
    vector: str = "scriptpubkey"
    spk_kind: str = "checksig"        # "checksig" | "multisig"
    per_tx_inputs: int = 0            # split poison block into N-input transactions
    sweep_utxos: list = field(default_factory=list)   # e.g. [100, 200, 400]
    sweep_sigops: list = field(default_factory=list)  # e.g. [1, 2, 4]


# --------------------------------------------------------------------------- #
# Node lifecycle
# --------------------------------------------------------------------------- #

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Node:
    """Owns a freshly launched, isolated (or loopback-peered) regtest bitcoind."""

    def __init__(self, cfg: BenchmarkConfig, workspace: Path, log,
                 *, p2p_peers: list | None = None,
                 p2p_listen_port: int = 0,
                 p2p_bind_host: str = "127.0.0.1"):
        self.cfg = cfg
        self.log = log
        self.port = _free_port()
        self.password = secrets.token_hex(16)
        validator = SafetyValidator(workspace)
        self.datadir = validator.prepare_datadir(None)
        self.safe_cfg = SafeConfig(
            bitcoind_path=cfg.bitcoind_path,
            datadir=self.datadir,
            rpc_host=cfg.rpc_host,
            rpc_port=self.port,
            rpc_user="pba",
            rpc_password=self.password,
            limits={
                "max_wall_seconds": cfg.max_wall_seconds,
                "max_peak_rss_mb": cfg.max_peak_rss_mb,
                "max_blocks": cfg.max_blocks,
                "max_poison_tx_bytes": cfg.max_poison_tx_bytes,
            },
            keep_datadir=cfg.keep_datadir,
            p2p_peers=p2p_peers or [],
            p2p_listen_port=p2p_listen_port,
            p2p_bind_host=p2p_bind_host,
        )
        self.safe_cfg.p2p_peers = validator.validate_p2p_peers(
            self.safe_cfg.p2p_peers, p2p_bind_host)
        extra = list(cfg.extra_args)
        if cfg.validation_threads:
            extra.append(f"-par={cfg.validation_threads}")
        if cfg.bip54:
            from bip54 import VB_PARAMS
            extra.append(f"-vbparams={VB_PARAMS}")
        self.safe_cfg.extra_args = validator.validate_extra_args(extra)
        self._proc = None
        self.rpc = None
        self._rpc_probe = None
        self.logfile = self.datadir / "bitcoind.log"

    # -- lifecycle --------------------------------------------------------- #
    def start(self):
        args = [str(self.safe_cfg.bitcoind_path)] + self.safe_cfg.build_bitcoind_args()
        self.log("launching bitcoind with args:")
        self.log("  " + " ".join(args))
        self._proc = subprocess.Popen(
            args, stdout=open(self.logfile, "ab"), stderr=subprocess.STDOUT
        )
        url = (f"http://{self.safe_cfg.rpc_user}:{self.safe_cfg.rpc_password}"
               f"@{self.safe_cfg.rpc_host}:{self.port}")
        # Do not reuse HTTP connections: if a request races node startup, a reused
        # connection is left in a "Request-sent" state and fails forever.
        # The main RPC client must outlive the longest possible block validation,
        # otherwise submitblock times out mid-validation (which we report as
        # "timeout", not rejection). The probe client is short.
        self.rpc = AuthServiceProxy(url, timeout=30 + self.cfg.max_wall_seconds)
        self._rpc_probe = AuthServiceProxy(url, timeout=60)
        self.rpc.reuse_http_connections = False
        self._rpc_probe.reuse_http_connections = False
        self._apply_affinity()
        self._wait_ready()

    def _apply_affinity(self):
        """Pin this node's process to the configured CPUs (optional)."""
        if not self.cfg.cpu_affinity:
            return
        try:
            cpus = [int(c) for c in self.cfg.cpu_affinity.split(",") if c.strip() != ""]
        except ValueError:
            raise SafetyError(
                f"invalid --cpu-affinity {self.cfg.cpu_affinity!r}; expected "
                "comma-separated CPU indices, e.g. '0' or '0,2'"
            ) from None
        if not cpus:
            raise SafetyError("--cpu-affinity must name at least one CPU")
        try:
            os.sched_setaffinity(self._proc.pid, cpus)
        except (AttributeError, OSError) as e:
            raise SafetyError(f"could not set CPU affinity to {cpus}: {e}") from None

    def _wait_ready(self, timeout: float = 120.0):
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            if self._proc.poll() is not None:
                tail = self._read_log_tail()
                raise SafetyError(
                    f"bitcoind exited during startup (code {self._proc.returncode}).\n{tail}"
                )
            try:
                self.rpc.getblockchaininfo()
                return
            except Exception as e:
                last = str(e)
                time.sleep(0.2)
        raise SafetyError(f"bitcoind did not become ready within {timeout}s: {last}")

    def _read_log_tail(self, n: int = 40) -> str:
        try:
            lines = self.logfile.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return ""

    def stop(self):
        for attr in ("rpc", "_rpc_probe"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.__del__()
                except Exception:
                    pass
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=15)
        if self.safe_cfg.datadir.exists() and not self.cfg.keep_datadir:
            shutil.rmtree(self.safe_cfg.datadir, ignore_errors=True)

    # -- safety re-verification at runtime ---------------------------------- #
    def verify_regtest(self):
        verify_chain_is_regtest(self.rpc)

    def probe_latency(self) -> float:
        """Return the wall time of one trivial RPC call (for the monitor).

        Raises on timeout/error so the monitor can classify the sample.
        """
        t0 = time.perf_counter()
        self._rpc_probe.getblockcount()
        return time.perf_counter() - t0

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def terminate(self):
        """Hard-terminate the node (used by resource-limit enforcement)."""
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Result collection
# --------------------------------------------------------------------------- #

class RunResult(dict):
    pass


def _run_one(node: Node, gen_cfg: ConstructionConfig, run_id: str, profile: str,
             cfg: BenchmarkConfig, log, command: str = "") -> RunResult:
    if cfg.activate_bip54:
        from bip54 import activate_bip54
        log("activating BIP54 (consensuscleanup) on the disposable regtest node...")
        activate_bip54(node.rpc, log)

    gen = PoisonBlockGenerator(node.rpc, gen_cfg, log)

    # Baseline: time submission of a normal (empty) block for comparison.
    # In "warm" mode we additionally submit an extra empty block first, so the
    # node's caches/threads have done one more block of warmup before the
    # measured submit. "cold" submits the baseline block and the poison block on
    # a freshly-started node.
    if cfg.warm_cold == "warm":
        _baseline_submit(node)
    baseline_wall = _baseline_submit(node)

    log("building poison construction (prep)...")
    if cfg.per_tx_inputs and cfg.per_tx_inputs < gen_cfg.num_utxos:
        res = gen.generate_split(cfg.per_tx_inputs)
    else:
        res = gen.generate()

    poison_tx = res.poison_tx
    poison_block = res.poison_block
    m = res.metrics

    if poison_tx.serialize_without_witness().__len__() > cfg.max_poison_tx_bytes:
        raise SafetyError(
            f"poison tx size {m['poison_tx_size_bytes']} bytes exceeds "
            f"max_poison_tx_bytes={cfg.max_poison_tx_bytes}. Reduce num_utxos/sigops_per_input."
        )
    if m["poison_tx_weight"] > construction.MAX_BLOCK_WEIGHT:
        raise SafetyError(
            f"poison tx weight {m['poison_tx_weight']} exceeds MAX_BLOCK_WEIGHT; "
            "this block could not be consensus-valid."
        )

    log(f"poison tx: {m['poison_tx_vin_count']} inputs, "
        f"{m['executed_checksig_count']} executed CHECKSIG, "
        f"{m['total_legacy_sigops_bip54']} BIP54 sigops, "
        f"{m['poison_tx_size_bytes']} bytes, weight {m['poison_tx_weight']}")
    log(f"sighash serialization bytes (v31.1.0, cache-aware): "
        f"{m['sighash_serialization_bytes']}")

    # --- measured submit --------------------------------------------------- #
    block_hex = poison_block.serialize().hex()
    height = node.rpc.getblockcount() + 1

    def do_submit():
        return node.rpc.submitblock(block_hex)

    # ResourceGuard enforces --max-rss-mb / --max-wall-seconds as real limits:
    # on violation it hard-terminates the disposable node and records why.
    guard = ResourceGuard(
        node._proc.pid,
        max_rss_mb=cfg.max_peak_rss_mb,
        max_wall_seconds=cfg.max_wall_seconds,
        on_violation=node.terminate,
    )
    outcome = {"success": "accepted", "rejection_reason": ""}
    with NodeMonitor(node._proc.pid, node.probe_latency,
                     probe_timeout=60) as mon:
        guard.start()
        try:
            rpc_res = do_submit()
            # submitblock returns None on success, or the reject-reason string.
            if rpc_res is not None:
                outcome["success"] = "rejected"
                outcome["rejection_reason"] = str(rpc_res)
        except JSONRPCException as e:
            if isinstance(e.error, dict) and e.error.get("code") == -344:
                # submitblock RPC timed out: validation is taking longer than the
                # configured limit. This is itself evidence of the attack.
                outcome["success"] = "timeout"
                outcome["rejection_reason"] = f"submitblock exceeded RPC timeout: {e.error.get('message')}"
            else:
                outcome["success"] = "rejected"
                outcome["rejection_reason"] = str(e.error)
        except Exception as e:
            outcome["success"] = "crash"
            outcome["rejection_reason"] = f"{type(e).__name__}: {e}"
        guard.stop()
    mon_stats = mon.stats()

    # A resource-limit violation takes precedence over whatever the RPC reported.
    violation = guard.violation()
    if violation:
        outcome["success"] = "aborted"
        outcome["rejection_reason"] = f"resource limit: {violation}"

    # After submit: confirm chain state (only if the node is still alive).
    block_hash, block_meta = "", {}
    try:
        if outcome["success"] == "accepted" and node.alive():
            info = node.rpc.getblockchaininfo()
            block_hash = info.get("bestblockhash", "")
            block_meta = node.rpc.getblock(block_hash, 2)
    except Exception:
        pass

    wall = mon_stats["validation_wall_seconds"]
    if outcome["success"] == "accepted" and wall >= cfg.max_wall_seconds:
        outcome["success"] = "timeout"
        outcome["rejection_reason"] = f"validation exceeded max_wall_seconds={cfg.max_wall_seconds}"

    # --- provenance -------------------------------------------------------- #
    rpc_info = _safe_call(node.rpc, "getnetworkinfo", {}) if node.alive() else {}
    bin_info = node_binary_info(cfg.bitcoind_path)
    hw = host_info()
    pba_repo = Path(__file__).resolve().parent
    result = RunResult()
    result["run"] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "profile": profile,
        "command": command,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": gen_cfg.seed,
    }
    result["provenance"] = {
        "node_version": rpc_info.get("version"),
        "node_subversion": rpc_info.get("subversion", ""),
        "node_version_string": bin_info["node_version_string"],
        "node_git_commit": bin_info["node_git_commit"],
        "compiler": bin_info["compiler"],
        "build_type": bin_info["build_type"],
        "bitcoind_path": str(cfg.bitcoind_path),
        "bitcoind_sha256": bin_info["bitcoind_sha256"],
        "kernel": hw["kernel"],
        "os_name": hw["os_name"],
        "machine": hw["machine"],
        "cpu_model": hw["cpu_model"],
        "core_count": hw["core_count"],
        "physical_cores": hw["physical_cores"],
        "total_ram_bytes": hw["total_ram_bytes"],
        "validation_threads": cfg.validation_threads,  # 0 = node default (-par)
        "warm_cold": cfg.warm_cold,
        "cpu_affinity": cfg.cpu_affinity or current_process_affinity(),
        "pba_bench_commit": git_commit(pba_repo),
    }
    result["construction"] = dict(m)
    result["construction"]["vector"] = cfg.vector
    try:
        from vectors import get_vector
        result["construction"]["vector_metadata"] = get_vector(cfg.vector).as_dict()
    except Exception:
        pass
    result["construction"]["poison_block_size_bytes"] = block_meta.get("size", 0) or len(poison_block.serialize())
    result["construction"]["poison_block_weight"] = block_meta.get("weight", 0) or (len(poison_block.serialize()) * 4)
    result["outcome"] = {
        "success": outcome["success"],
        "rejection_reason": outcome["rejection_reason"],
        "block_hash": block_hash,
        "block_height": height,
    }
    # BIP 54: distinguish a live rejection (a BIP54 binary actually rejected the
    # block) from an inference (no such binary was tested).
    bip54_sigops = m.get("total_legacy_sigops_bip54", 0)
    would = bip54_sigops > construction.BIP54_MAX_TX_LEGACY_SIGOPS
    if would and outcome["success"] == "rejected" and "legacy-sigops" in outcome["rejection_reason"]:
        result["outcome"]["bip54_result"] = "live"
    elif would:
        result["outcome"]["bip54_result"] = "inferred"
    else:
        result["outcome"]["bip54_result"] = "not_tested"
    result["outcome"]["bip54_would_reject"] = would
    result["measurement"] = {
        "baseline_wall_seconds": baseline_wall,
        "validation_wall_seconds": mon_stats["validation_wall_seconds"],
        "validation_cpu_seconds": mon_stats["validation_cpu_seconds"],
        "peak_rss_bytes": mon_stats["peak_rss_bytes"],
        "rpc_probe_count": mon_stats["rpc_probe_count"],
        "rpc_probe_max_seconds": mon_stats["rpc_probe_max_seconds"],
        "rpc_probe_median_seconds": mon_stats["rpc_probe_median_seconds"],
        "rpc_probe_timeout_count": mon_stats["rpc_probe_timeout_count_exact"],
        "rpc_probe_error_count": mon_stats["rpc_probe_error_count"],
        "rpc_probe_lower_bound_seconds": mon_stats["rpc_probe_lower_bound_seconds"],
        "block_tx_count": len(block_meta.get("tx", [])) if block_meta else 0,
    }
    result["limits"] = {
        "max_wall_seconds": cfg.max_wall_seconds,
        "max_peak_rss_mb": cfg.max_peak_rss_mb,
        "max_blocks": cfg.max_blocks,
        "max_poison_tx_bytes": cfg.max_poison_tx_bytes,
    }
    log(f"outcome: {outcome['success']} wall={wall:.3f}s cpu={mon_stats['validation_cpu_seconds']:.3f}s "
        f"peak_rss={mon_stats['peak_rss_bytes']/1e6:.1f}MB")
    return result


def _baseline_submit(node: Node) -> float:
    """Submit a normal empty block and return its wall-clock validation time."""
    from test_framework.blocktools import create_block, create_coinbase
    height = node.rpc.getblockcount() + 1
    block = create_block(
        tmpl={
            "previousblockhash": node.rpc.getbestblockhash(),
            "curtime": node.rpc.getblockchaininfo()["time"] + 1,
            "height": height,
        },
        coinbase=create_coinbase(height),
    )
    block.solve()
    try:
        _, wall = timed_rpc(node.rpc, "submitblock", block.serialize().hex())
        return wall
    except Exception:
        return 0.0


def _safe_call(rpc, method, default):
    try:
        return getattr(rpc, method)()
    except Exception:
        return default


# --------------------------------------------------------------------------- #
# Result export
# --------------------------------------------------------------------------- #

def export_results(results: list, outdir: Path) -> tuple:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / "results.json"
    csv_path = outdir / "results.csv"

    json_path.write_text(json.dumps(results, indent=2, default=str))

    cols = csv_columns()
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(flat_result(r))
    return json_path, csv_path


def required_blocks(gen_cfg: ConstructionConfig, bip54_activate: bool = False) -> int:
    """Total blocks a construction mines, including the poison block.

    Used to enforce ``--max-blocks`` *before* constructing anything.
    """
    from construction import COINBASE_MATURITY, MAX_BLOCK_SIGOPS_COST
    # Use the inaccurate (block-sigop-cap) count so multisig prep is sized right.
    cost = 20 * ((gen_cfg.sigops_per_input + 15) // 16) if gen_cfg.spk_kind == "multisig" \
        else gen_cfg.sigops_per_input
    sigops_per_prep_block = MAX_BLOCK_SIGOPS_COST // 4
    reserve = 400
    max_utxos_per_prep_block = max(1, (sigops_per_prep_block - reserve) // cost)
    n_prep_blocks = (gen_cfg.num_utxos + max_utxos_per_prep_block - 1) // max_utxos_per_prep_block
    total = COINBASE_MATURITY + n_prep_blocks + 1
    if bip54_activate:
        from bip54 import activation_block_count
        total += activation_block_count()
    return total


# --------------------------------------------------------------------------- #
# Main benchmark driver
# --------------------------------------------------------------------------- #

def run_benchmark(cfg: BenchmarkConfig, workspace: Path, log=None,
                  command: str = "") -> Path:
    log = log or (lambda *a: print(*a))
    validator = SafetyValidator(workspace)
    cfg.bitcoind_path = validator.validate_bitcoind(cfg.bitcoind_path)

    _print_warning(cfg)

    outdir = Path(cfg.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    run_id = f"{cfg.profile}-{int(time.time())}"

    all_results = []

    # Build the list of construction configs to run (supports one-at-a-time sweeps).
    gen_configs = _build_gen_configs(cfg)
    log(f"running {len(gen_configs)} construction config(s) x {cfg.runs} run(s)")

    # Enforce --max-blocks before constructing anything: refuse any config that
    # would need more blocks than the limit.
    for g_idx, gen_cfg in enumerate(gen_configs):
        need = required_blocks(gen_cfg, bip54_activate=cfg.activate_bip54)
        if need > cfg.max_blocks:
            raise SafetyError(
                f"construction (N={gen_cfg.num_utxos}, K={gen_cfg.sigops_per_input}) "
                f"requires {need} blocks, exceeding max_blocks={cfg.max_blocks}. "
                "Refusing before constructing."
            )

    for g_idx, gen_cfg in enumerate(gen_configs):
        for run_idx in range(1, cfg.runs + 1):
            rid = f"{run_id}-c{g_idx + 1}-r{run_idx}"
            node = Node(cfg, workspace, log)
            try:
                node.start()
                node.verify_regtest()
                log(f"[{rid}] datadir={node.datadir}")
                res = _run_one(node, gen_cfg, rid, cfg.profile, cfg, log, command=command)
                all_results.append(res)
                # Export incrementally so partial results survive interruption.
                json_path, csv_path = export_results(all_results, outdir)
                log(f"partial results: {json_path}, {csv_path}")
            finally:
                node.stop()

    json_path, csv_path = export_results(all_results, outdir)

    # Reproducibility manifest.
    try:
        from manifest import build_manifest, write_manifest
        first = all_results[0] if all_results else {}
        mf = build_manifest(
            workspace=workspace,
            bitcoind=cfg.bitcoind_path,
            command=command,
            vector=cfg.vector,
            num_utxos=first.get("construction", {}).get("num_utxos", cfg.num_utxos or 0),
            sigops_per_input=first.get("construction", {}).get("sigops_per_input",
                                                               cfg.sigops_per_input or 0),
            seed=cfg.seed,
            topology=None,
            observer_config={"validation_threads": cfg.validation_threads,
                             "warm_cold": cfg.warm_cold},
            validation_threads=cfg.validation_threads,
            node_args=cfg.extra_args,
            result_files=[json_path, csv_path],
        )
        write_manifest(mf, outdir)
    except Exception as e:  # pragma: no cover - manifest must never break a run
        log(f"warning: could not write manifest: {e}")

    log(f"results written: {json_path}, {csv_path}")
    return outdir


def _build_gen_configs(cfg: BenchmarkConfig) -> list:
    base = profile_config(cfg.profile, {
        "num_utxos": cfg.num_utxos,
        "sigops_per_input": cfg.sigops_per_input,
        "spk_kind": cfg.spk_kind,
    })
    configs = []
    if cfg.sweep_utxos:
        for n in cfg.sweep_utxos:
            configs.append(ConstructionConfig(seed=cfg.seed, num_utxos=n,
                                              sigops_per_input=base.sigops_per_input,
                                              spk_kind=base.spk_kind))
    elif cfg.sweep_sigops:
        for k in cfg.sweep_sigops:
            configs.append(ConstructionConfig(seed=cfg.seed, num_utxos=base.num_utxos,
                                              sigops_per_input=k,
                                              spk_kind=base.spk_kind))
    else:
        configs.append(ConstructionConfig(seed=cfg.seed, num_utxos=base.num_utxos,
                                          sigops_per_input=base.sigops_per_input,
                                          spk_kind=base.spk_kind))
    return configs


def _print_warning(cfg: BenchmarkConfig) -> None:
    line = "=" * 74
    print("\n" + line)
    print("pba-bench: Bitcoin worst-case block validation benchmark (REG TEST ONLY)")
    print(line)
    print("This tool launches its OWN disposable regtest node, fully isolated:")
    print("  - chain is forced to regtest and verified via getblockchaininfo")
    print("  - RPC binds to loopback only; P2P networking disabled (-connect=0 -listen=0)")
    print("  - fresh disposable datadir; no existing node is touched")
    print("  - no transactions or blocks are ever broadcast to public peers")
    print()
    print(f"  bitcoind : {cfg.bitcoind_path}")
    print(f"  profile  : {cfg.profile}  ({PROFILE_DESCRIPTIONS.get(cfg.profile, '')})")
    print(f"  datadir  : <created fresh under workspace>/work/datadir-<pid>-<counter>")
    print(f"  limits   : max_wall={cfg.max_wall_seconds}s "
          f"max_rss={cfg.max_peak_rss_mb}MB max_blocks={cfg.max_blocks} "
          f"max_poison_tx_bytes={cfg.max_poison_tx_bytes}")
    print(line + "\n")
