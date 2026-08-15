"""Multi-node block-propagation benchmark for pba-bench.

Demonstrates the *real-world consequences* of a poison block on a private,
loopback-only regtest network (the 0xB10C signet measurement, done locally):

  * a **miner** node builds the poison chain and broadcasts the poison block;
  * one or more **observer** nodes are peered with the miner over loopback P2P
    and relay blocks normally;
  * we measure how long the poison block takes to reach a peer (propagation +
    validation), how RPC latency balloons on a peer while it validates, and how
    a peer keeps working on a stale tip during validation.

Safety: every node is regtest, binds RPC and P2P to loopback only, peers only
with our own local nodes, and never touches a public network.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from benchmark import BenchmarkConfig, Node, _free_port
from construction import ConstructionConfig, PoisonBlockGenerator
from measure import timed_rpc
from safety import SafetyError


@dataclass
class PropagationConfig:
    seed: int = 1
    num_utxos: int = 2000          # poison inputs (N)
    sigops_per_input: int = 100    # CHECKSIG per input (K)
    observer_par: int = 1          # validation threads on observers (1 = slow single-thread)
    num_observers: int = 1
    miner_par: int = 0             # validation threads on the miner (0 = default)


def _block_on_tip(node: Node, tx) -> object:
    """Build a solved block containing ``tx`` on the node's current tip."""
    from test_framework.blocktools import create_block, create_coinbase
    height = node.rpc.getblockcount() + 1
    block = create_block(
        tmpl={"previousblockhash": node.rpc.getbestblockhash(),
              "curtime": node.rpc.getblockchaininfo()["time"] + 1, "height": height},
        coinbase=create_coinbase(height),
        txlist=[tx])
    block.solve()
    return block


class _RPCLatencyProbe:
    """Samples RPC latency + tip on a node from a background thread."""

    def __init__(self, node: Node):
        self.node = node
        self._stop = threading.Event()
        self._thread = None
        self._lats = []
        self._saw_poison = False
        self._pre_poison_tip = None

    def start(self, pre_poison_tip: str):
        self._pre_poison_tip = pre_poison_tip
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        # Use a dedicated RPC connection so we never share the measurement thread's
        # connection (which would corrupt its HTTP state).
        rpc = self.node._rpc_probe
        while not self._stop.is_set():
            try:
                t0 = time.perf_counter()
                tip = rpc.getbestblockhash()
                self._lats.append(time.perf_counter() - t0)
                if tip != self._pre_poison_tip:
                    self._saw_poison = True
            except Exception:
                pass
            self._stop.wait(0.02)

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if not self._lats:
            return {"rpc_probe_count": 0, "rpc_probe_max_seconds": 0.0,
                    "rpc_probe_median_seconds": 0.0,
                    "observer_tip_updated": self._saw_poison}
        s = sorted(self._lats)
        n = len(s)
        med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        return {
            "rpc_probe_count": n,
            "rpc_probe_max_seconds": round(max(s), 6),
            "rpc_probe_median_seconds": round(med, 6),
            "observer_tip_updated": self._saw_poison,
        }


class PropagationBenchmark:
    """Runs the multi-node propagation demo on a private regtest network."""

    def __init__(self, prop_cfg: PropagationConfig, bench_cfg: BenchmarkConfig,
                 workspace: Path, log):
        self.prop = prop_cfg
        self.bench = bench_cfg
        self.workspace = Path(workspace)
        self.log = log or (lambda *_: None)

    # -- helpers ----------------------------------------------------------- #
    def _observer_bench_cfg(self) -> BenchmarkConfig:
        bc = BenchmarkConfig(**vars(self.bench))
        bc.validation_threads = self.prop.observer_par
        return bc

    def _wait_sync(self, obs: Node, miner: Node, timeout: float = 120.0):
        target = miner.rpc.getblockcount()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if obs.rpc.getblockcount() >= target:
                    return
            except Exception:
                pass
            time.sleep(0.2)
        raise SafetyError("observer did not sync to miner's chain in time")

    def _measure_normal_propagation(self, miner: Node, obs: Node) -> dict:
        from test_framework.blocktools import create_block, create_coinbase
        height = miner.rpc.getblockcount() + 1
        block = create_block(
            tmpl={"previousblockhash": miner.rpc.getbestblockhash(),
                  "curtime": miner.rpc.getblockchaininfo()["time"] + 1, "height": height},
            coinbase=create_coinbase(height))
        block.solve()
        new_hash = block.hash_hex
        t_start = time.perf_counter()
        submit_res, submit_wall = timed_rpc(miner.rpc, "submitblock", block.serialize().hex())
        # Sanity: the miner must have connected it.
        if submit_res is not None:
            return {"baseline_propagation_seconds": None, "baseline_submit_wall_seconds": round(submit_wall, 6),
                    "baseline_block": new_hash,
                    "baseline_note": f"miner rejected baseline block: {submit_res}"}
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if obs.rpc.getbestblockhash() == new_hash:
                    return {"baseline_propagation_seconds": round(time.perf_counter() - t_start, 6),
                            "baseline_submit_wall_seconds": round(submit_wall, 6),
                            "baseline_block": new_hash}
            except Exception as e:
                return {"baseline_propagation_seconds": None,
                        "baseline_submit_wall_seconds": round(submit_wall, 6),
                        "baseline_block": new_hash, "baseline_note": f"observer RPC error: {e}"}
            time.sleep(0.05)
        return {"baseline_propagation_seconds": None, "baseline_submit_wall_seconds": round(submit_wall, 6),
                "baseline_block": new_hash,
                "baseline_note": f"peer did not observe block in 60s; miner_tip={miner.rpc.getbestblockhash()[:16]} obs_tip={obs.rpc.getbestblockhash()[:16]} peers={len(obs.rpc.getpeerinfo())}"}

    # -- main -------------------------------------------------------------- #
    def run(self) -> dict:
        prop = self.prop
        miner_p2p_port = _free_port()
        obs_nodes = []
        miner = Node(self.bench, self.workspace, self.log,
                     p2p_listen_port=miner_p2p_port, p2p_bind_host="127.0.0.1")
        try:
            miner.start(); miner.verify_regtest()
            self.log(f"miner up (p2p 127.0.0.1:{miner_p2p_port})")

            gen_cfg = ConstructionConfig(seed=prop.seed, num_utxos=prop.num_utxos,
                                         sigops_per_input=prop.sigops_per_input,
                                         deterministic_time=False)
            gen = PoisonBlockGenerator(miner.rpc, gen_cfg, self.log)
            res = gen.generate()   # mines prep chain on miner, returns poison tx + metrics
            poison_tx = res.poison_tx
            self.log(f"prep done; poison tx: {poison_tx.vin.__len__()} inputs, "
                     f"{res.metrics['total_legacy_sigops_bip54']} sigops")

            obs_bench_cfg = self._observer_bench_cfg()
            for i in range(prop.num_observers):
                obs = Node(obs_bench_cfg, self.workspace, self.log,
                           p2p_peers=[f"127.0.0.1:{miner_p2p_port}"])
                obs.start(); obs.verify_regtest()
                obs_nodes.append(obs)
                self.log(f"observer {i + 1} up (rpc {obs.port})")
                self._wait_sync(obs, miner)
                self.log(f"observer {i + 1} synced at height {obs.rpc.getblockcount()}")

            baseline = self._measure_normal_propagation(miner, obs_nodes[0])
            self.log(f"baseline normal-block propagation: {baseline['baseline_propagation_seconds']}s "
                     f"({baseline.get('baseline_note', '')})")
            self.log(f"miner peers={len(miner.rpc.getpeerinfo())} "
                     f"miner_tip={miner.rpc.getbestblockhash()[:16]} "
                     f"obs_tip={obs_nodes[0].rpc.getbestblockhash()[:16]} "
                     f"obs_peers={len(obs_nodes[0].rpc.getpeerinfo())}")

            # Build the poison block on the miner's CURRENT tip (after baseline),
            # then submit it and watch the observers.
            poison_block = _block_on_tip(miner, poison_tx)
            poison_hash = poison_block.hash_hex
            pre_poison_tip = miner.rpc.getbestblockhash()
            probe = _RPCLatencyProbe(obs_nodes[0]).start(pre_poison_tip)

            t0 = time.perf_counter()
            submit_result, submit_wall = timed_rpc(
                miner.rpc, "submitblock", poison_block.serialize().hex())
            miner_accepted = submit_result is None
            t_submit_ret = time.perf_counter() - t0   # miner finished (starts announcing)

            first_obs_time = None
            deadline = time.time() + 15 * 60
            while time.time() < deadline:
                if obs_nodes[0].rpc.getbestblockhash() == poison_hash:
                    first_obs_time = time.perf_counter() - t0
                    break
                time.sleep(0.05)
            probe_stats = probe.stop()

            after_miner = (first_obs_time - t_submit_ret) if first_obs_time else None
            self.log(f"poison submit accepted={miner_accepted} wall={submit_wall:.1f}s; "
                     f"reached observer in {first_obs_time}s "
                     f"(propagation+validation after miner finished: {after_miner}s)")
            self.log(f"observer RPC during validation: max={probe_stats['rpc_probe_max_seconds']:.2f}s")

            obs_results = [{"observer": i + 1,
                            "best_blockhash": obs.rpc.getbestblockhash(),
                            "blocks": obs.rpc.getblockcount()}
                           for i, obs in enumerate(obs_nodes)]

            return self._build_report(res, baseline, poison_hash,
                                      {"submit_accepted": miner_accepted,
                                       "submit_wall_seconds": round(submit_wall, 6),
                                       "propagation_to_observer_seconds":
                                           round(first_obs_time, 6) if first_obs_time else None,
                                       "propagation_after_miner_seconds":
                                           round(after_miner, 6) if after_miner is not None else None,
                                       "observer_saw_poison": first_obs_time is not None},
                                      probe_stats, obs_results)
        finally:
            for obs in obs_nodes:
                obs.stop()
            miner.stop()

    def _build_report(self, res, baseline, poison_hash, poison, probe, obs_results) -> dict:
        prop = self.prop
        m = res.metrics
        return {
            "run": {
                "profile": "propagation",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "seed": prop.seed,
            },
            "topology": {
                "num_observers": prop.num_observers,
                "observer_validation_threads": prop.observer_par,
                "peering": "loopback-only P2P between local regtest nodes",
            },
            "construction": {
                "num_utxos": m["num_utxos"],
                "sigops_per_input": m["sigops_per_input"],
                "total_legacy_sigops_bip54": m["total_legacy_sigops_bip54"],
                "poison_tx_vin_count": m["poison_tx_vin_count"],
                "poison_tx_size_bytes": m["poison_tx_size_bytes"],
                "poison_tx_weight": m["poison_tx_weight"],
                "poison_block": poison_hash,
            },
            "baseline": baseline,
            "poison": poison,
            "stale_tip_duration_seconds": poison.get("propagation_to_observer_seconds"),
            "observer_rpc_during_validation": probe,
            "observers": obs_results,
        }
