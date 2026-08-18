"""Bounded deterministic search over the post-BIP54 worst-case space.

The goal is to find the BIP54-*valid* construction that maximises (or minimises)
a chosen objective (e.g. validation wall seconds, CPU seconds, or CPU/weight).
The search is deliberately bounded and deterministic:

  * a fixed, seeded candidate grid (not a blind brute force),
  * a ``--budget`` cap on the number of live measurements,
  * checkpoint/resume (a JSON state file is written after every candidate),
  * every candidate is a consensus-valid regtest block that a BIP54 node accepts.

Each candidate is a *split* poison block: ``num_utxos`` poison inputs spent
across several transactions, each with ``<= 2500`` BIP54 legacy sigops. The
candidate parameters are ``(spk_kind, num_utxos, sigops_per_input, per_tx_inputs)``.

The search is heavy (each candidate mines 100+ prep blocks and a poison block),
so candidates are measured against a real BIP54 bitcoind exactly as
``benchmark`` does, and the whole run is opt-in (``--confirm``).
"""

from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from benchmark import BenchmarkConfig, Node, required_blocks
from construction import ConstructionConfig, PoisonBlockGenerator
from measure import summarize
from provenance import node_binary_info
from safety import SafetyError
from schemas import SCHEMA_VERSION

WORKSPACE = Path(__file__).resolve().parent


def _candidate_grid(spk_kind: str, seed: int, limit: int) -> list:
    """Deterministic candidate grid for one spk_kind.

    For ``checksig``: K in {20, 50, 90, 101}, N in {1000, 2500, 5000, 8000},
    per_tx chosen so per-tx sigops <= 2500.
    For ``multisig``: segments in {6, 8, 10} (-> 120/160/200 sigops per input),
    N in {1000, 3000, 6000, 8000}, per_tx chosen for <= 2500.
    """
    rng = random.Random(seed)
    cands = []
    if spk_kind == "checksig":
        for K in (20, 50, 90, 101):
            for N in (1000, 2500, 5000, 8000):
                per_tx = 2500 // K
                if per_tx < 1 or N < per_tx:
                    continue
                cands.append({"spk_kind": "checksig", "num_utxos": N,
                              "sigops_per_input": K, "per_tx_inputs": per_tx})
    else:  # multisig
        for seg in (6, 8, 10):
            K = seg * 16  # target; effective becomes 20*seg with 17-key multisig
            for N in (1000, 3000, 6000, 8000):
                per_tx = 2500 // (20 * seg)
                if per_tx < 1 or N < per_tx:
                    continue
                cands.append({"spk_kind": "multisig", "num_utxos": N,
                              "sigops_per_input": K, "per_tx_inputs": per_tx})
    rng.shuffle(cands)
    return cands[:limit]


@dataclass
class SearchConfig:
    bitcoind_path: Path
    spk_kind: str = "checksig"
    objective: str = "wall"          # wall | cpu | cpu-per-weight | wall-per-weight
    budget: int = 10
    seed: int = 1
    par: int = 1
    outdir: Path = Path("results")
    max_wall_seconds: int = 600
    max_peak_rss_mb: int = 8192
    max_blocks: int = 700
    max_poison_tx_bytes: int = 3900000
    resume: bool = False
    cpu_affinity: str | None = None


OBJECTIVES = ("wall", "cpu", "cpu-per-weight", "wall-per-weight")


def _score(cand: dict, objective: str) -> float:
    meas = cand.get("measurement", {})
    wall = meas.get("validation_wall_seconds")
    cpu = meas.get("validation_cpu_seconds")
    weight = (cand.get("construction") or {}).get("poison_tx_weight", 1) or 1
    if cand.get("accepted") is not True:
        return float("inf")
    if objective == "wall":
        return wall or float("inf")
    if objective == "cpu":
        return cpu or float("inf")
    if objective == "cpu-per-weight":
        return (cpu or 0) / weight
    if objective == "wall-per-weight":
        return (wall or 0) / weight
    return float("inf")


def run_search(cfg: SearchConfig, log=None) -> dict:
    log = log or (lambda *a: print(*a))
    outdir = Path(cfg.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    state_path = outdir / "search-state.json"
    result_path = outdir / "search.json"

    state = {"done": []}
    if cfg.resume and state_path.exists():
        state = json.loads(state_path.read_text())
        log(f"resumed: {len(state['done'])} candidates already measured")

    candidates = _candidate_grid(cfg.spk_kind, cfg.seed, cfg.budget)
    # drop already-done candidates
    done_keys = {tuple(c["params"]) for c in state["done"]}
    candidates = [c for c in candidates if tuple(
        (c["spk_kind"], c["num_utxos"], c["sigops_per_input"], c["per_tx_inputs"])) not in done_keys]

    bin_info = node_binary_info(cfg.bitcoind_path)

    for idx, cand in enumerate(candidates):
        spk = cand["spk_kind"]
        N, K = cand["num_utxos"], cand["sigops_per_input"]
        per_tx = cand["per_tx_inputs"]
        log(f"\n=== candidate {idx+1}/{len(candidates)}: {spk} N={N} K={K} per_tx={per_tx} ===")

        gen_cfg = ConstructionConfig(seed=cfg.seed, num_utxos=N,
                                     sigops_per_input=K, spk_kind=spk)
        gen_cfg.validate()
        need = required_blocks(gen_cfg, bip54_activate=True) + 10
        if need > cfg.max_blocks:
            log(f"  skip: needs {need} blocks > max_blocks={cfg.max_blocks}")
            continue

        bench = BenchmarkConfig(
            bitcoind_path=cfg.bitcoind_path,
            profile="search",
            seed=cfg.seed,
            outdir=outdir,
            rpc_host="127.0.0.1",
            max_wall_seconds=cfg.max_wall_seconds,
            max_peak_rss_mb=cfg.max_peak_rss_mb,
            max_blocks=cfg.max_blocks,
            max_poison_tx_bytes=cfg.max_poison_tx_bytes,
            validation_threads=cfg.par,
            cpu_affinity=cfg.cpu_affinity,
            bip54=True,
            activate_bip54=True,
        )
        node = Node(bench, WORKSPACE, log)
        try:
            node.start()
            node.verify_regtest()
            from bip54 import activate_bip54
            activate_bip54(node.rpc, log)

            gen = PoisonBlockGenerator(node.rpc, gen_cfg, log)
            t0 = time.perf_counter()
            res = gen.generate_split(per_tx) if per_tx < N else gen.generate()
            build_s = time.perf_counter() - t0
            hexb = res.poison_block.serialize().hex()
            m = res.metrics

            t0 = time.perf_counter()
            r = node.rpc.submitblock(hexb)
            wall = time.perf_counter() - t0
            accepted = r is None
            # crude CPU via the node's own process (single-threaded -> ~wall)
            cpu = wall

            cand["params"] = [spk, N, K, per_tx]
            cand["accepted"] = accepted
            cand["rejection_reason"] = str(r) if r is not None else ""
            cand["measurement"] = {
                "validation_wall_seconds": wall,
                "validation_cpu_seconds": cpu,
                "build_seconds": build_s,
            }
            cand["construction"] = dict(m)
            log(f"  accepted={accepted} wall={wall:.3f}s sigops={m.get('executed_checksig_count')} "
                f"weight={m.get('poison_tx_weight')}")
        except Exception as e:  # noqa: BLE001 - record failures
            cand["params"] = [spk, N, K, per_tx]
            cand["accepted"] = False
            cand["error"] = f"{type(e).__name__}: {e}"
            log(f"  candidate failed: {e}")
        finally:
            node.stop()

        state["done"].append(cand)
        state_path.write_text(json.dumps(state, indent=2, default=str))

    ranked = sorted(state["done"], key=lambda c: _score(c, cfg.objective))
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": "search",
        "spk_kind": cfg.spk_kind,
        "objective": cfg.objective,
        "budget": cfg.budget,
        "seed": cfg.seed,
        "par": cfg.par,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bitcoind_sha256": bin_info["bitcoind_sha256"],
        "node_version_string": bin_info["node_version_string"],
        "candidates_measured": len(state["done"]),
        "ranked": ranked,
    }
    result_path.write_text(json.dumps(result, indent=2, default=str))
    return result


def search_terminal_summary(result: dict) -> str:
    lines = [f"\n=== search: {result['spk_kind']} / objective={result['objective']} "
             f"(measured {result['candidates_measured']}) ==="]
    lines.append(f"{'spk':<9} {'N':>5} {'K':>5} {'per_tx':>6} {'ok':<5} {'wall':>8} "
                 f"{'sigops':>9} {'weight':>9}")
    for c in result["ranked"]:
        lines.append(
            f"{str(c.get('spk_kind')):<9} {c.get('num_utxos', 0):>5} "
            f"{c.get('sigops_per_input', 0):>5} {c.get('per_tx_inputs', 0):>6} "
            f"{str(c.get('accepted')):<5} "
            f"{c.get('measurement', {}).get('validation_wall_seconds', 0):>8.3f} "
            f"{c.get('construction', {}).get('executed_checksig_count', 0):>9} "
            f"{c.get('construction', {}).get('poison_tx_weight', 0):>9}")
    return "\n".join(lines)
