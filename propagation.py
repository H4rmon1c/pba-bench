"""Multi-node, multi-observer block-propagation benchmark for pba-bench.

Demonstrates the real-world consequences of a poison block on a private,
loopback-only regtest network, in the style of the 0xB10C signet measurement but
done fully locally:

  * a **miner** node builds the poison chain and broadcasts the poison block;
  * one or more **observer** nodes (in a configurable loopback-only topology)
    validate and accept it;
  * every observer gets an *independent* measurement context: its own probe
    thread and RPC connection, per-observer tip-transition timing, RPC latency,
    CPU, peak RSS, and topology position.

Every observer may run a different validation-thread count (``-par``), so one
identical poison block can be observed by heterogeneous nodes
(``--observer-par 1,2,4,8,0``).

Topologies (all loopback-only, among disposable local regtest nodes):
  * star : MINER -- every observer directly
  * line : MINER -> A -> B -> C -> ...
  * tree : balanced binary tree rooted at MINER

Safety: every node is regtest, binds RPC and P2P to loopback only, peers only
with our own local nodes, and never touches a public network.

Measurement terminology
-----------------------
The interval from miner ``submitblock`` to an observer's active-tip transition is
``time_to_tip_seconds``. It includes the miner's own validation and the
observer's P2P receive, validation, and tip activation; it is *not* pure wire
propagation. ``post_miner_time_to_tip_seconds`` subtracts the miner's
validation. See research/TECHNICAL_CORRECTIONS.md.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from benchmark import BenchmarkConfig, Node, _free_port
from construction import ConstructionConfig, PoisonBlockGenerator
from measure import ResourceGuard, timed_rpc, summarize
from provenance import git_commit, node_binary_info, node_rpc_info
from safety import SafetyError
from schemas import SCHEMA_VERSION

# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #

@dataclass
class TopologyNode:
    """One observer in a topology."""
    id: int
    par: int                       # validation threads for this observer
    upstream: list = field(default_factory=list)   # [(host, port)] of parents
    downstream_ports: list = field(default_factory=list)  # ports we should listen on? handled externally

    def label(self) -> str:
        return f"obs{self.id}"


@dataclass
class Topology:
    kind: str                      # star | line | tree
    observer_specs: list           # list[TopologyNode]
    # edges: list of (src_id, dst_id); src_id 0 = miner, 1..n = observers
    edges: list = field(default_factory=list)

    @property
    def num_observers(self) -> int:
        return len(self.observer_specs)


def _ports(n: int) -> list:
    return [_free_port() for _ in range(n)]


def build_topology(kind: str, num_observers: int,
                   par_values: list) -> Topology:
    """Build a loopback-only topology of ``num_observers`` observers.

    ``par_values`` is a list of validation-thread counts; it is cycled over the
    observers (one value per observer).
    """
    if num_observers < 1:
        raise SafetyError("propagation needs at least 1 observer")
    specs = [
        TopologyNode(id=i + 1, par=par_values[(i) % len(par_values)])
        for i in range(num_observers)
    ]

    # Default: every observer's upstream peer is the miner (id 0). Line/tree
    # override the intermediate observers to point at their parent observer.
    upstream = {s.id: [0] for s in specs}

    if kind == "star":
        # MINER -> every observer (direct). Observers have no children.
        edges = [(0, s.id) for s in specs]
    elif kind == "line":
        # MINER -> A -> B -> C -> ...
        edges = [(0, 1)] + [(i, i + 1) for i in range(1, num_observers)]
        for s in specs:
            if s.id > 1:
                upstream[s.id] = [s.id - 1]
    elif kind == "tree":
        # balanced binary tree rooted at MINER:
        #   MINER -> obs1, obs2 ; obs1 -> obs3, obs4 ; obs2 -> obs5, obs6 ; ...
        # Parent of 1-based observer id i is (i-1)//2 (0 = miner).
        edges = []
        for s in specs:
            parent = (s.id - 1) // 2
            edges.append((parent, s.id))
            upstream[s.id] = [parent]
    else:
        raise SafetyError(f"unknown topology {kind!r}; choose star|line|tree")

    topo = Topology(kind=kind, observer_specs=specs, edges=edges)
    topo._upstream = upstream
    return topo


# --------------------------------------------------------------------------- #
# Per-observer measurement
# --------------------------------------------------------------------------- #

class _ObserverProbe:
    """Independently measures one observer while the poison block propagates.

    Runs in its own thread with its own RPC connection so that probe threads
    never share HTTP connections and never serialize on one another.
    """

    def __init__(self, node: Node, probe_timeout: float = 60.0,
                 sample_interval: float = 0.02):
        self.node = node
        self.probe_timeout = probe_timeout
        self.sample_interval = sample_interval
        self._stop = threading.Event()
        self._thread = None
        self._samples = []            # list of (outcome, latency, lower_bound)
        self._tip_seen = None
        self._pre_poison_tip = None
        self._poison_hash = None
        self._peak_rss = 0

    def start(self, pre_poison_tip: str, poison_hash: str):
        self._pre_poison_tip = pre_poison_tip
        self._poison_hash = poison_hash
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        rpc = self.node._rpc_probe
        while not self._stop.is_set():
            t0 = time.perf_counter()
            outcome = "ok"
            lat = 0.0
            tip = None
            try:
                tip = rpc.getbestblockhash()
                lat = time.perf_counter() - t0
            except Exception:
                lat = time.perf_counter() - t0
                if not self.node.alive():
                    outcome = "node_shutdown"
                else:
                    outcome = "timeout" if lat >= self.probe_timeout else "error"
            self._samples.append((outcome, lat,
                                  self.probe_timeout if outcome == "timeout" else 0.0))
            if self._tip_seen is None and tip == self._poison_hash:
                self._tip_seen = time.perf_counter()
            self._stop.wait(self.sample_interval)

    def stop(self, timeout: float = 3.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        return self.stats()

    def stats(self) -> dict:
        ok = [lat for outcome, lat, _ in self._samples if outcome == "ok"]
        timeouts = sum(1 for o, _, _ in self._samples if o == "timeout")
        errors = sum(1 for o, _, _ in self._samples if o in ("error", "node_shutdown"))
        return {
            "rpc_probe_count": len(self._samples),
            "rpc_probe_max_seconds": round(max(ok, default=0.0), 6),
            "rpc_probe_median_seconds": round(_median(ok), 6),
            "rpc_probe_timeout_count": timeouts,
            "rpc_probe_error_count": errors,
            "rpc_probe_lower_bound_seconds": self.probe_timeout,
        }


def _median(values):
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


# --------------------------------------------------------------------------- #
# Log event extraction (defensible subset of P2P instrumentation)
# --------------------------------------------------------------------------- #

_UPDATE_TIP_RE = re.compile(r"UpdateTip: new best=([0-9a-fA-F]+).* height=(\d+)")
_RECV_BLOCK_RE = re.compile(r"received block\s+([0-9a-fA-F]+)")

def parse_block_log_events(logfile: Path) -> dict:
    """Best-effort extraction of block-receipt / tip-transition events from a
    bitcoind debug log.

    Bitcoin Core logs ``UpdateTip: new best=<hash> ... height=<n>`` on every tip
    transition (always) and ``received block <hash>`` (with -debug=net). We parse
    those and return their wall-clock timestamps. This is a *defensible subset*
    of P2P instrumentation: it does NOT isolate the exact instant the block
    arrived on the wire, only when the node logged receipt and when it activated
    the new tip. See docs/analysis.md for the limitation.
    """
    events = {"received_block_entries": [], "tip_transitions": []}
    try:
        text = logfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return events
    for line in text.splitlines():
        ts = line[: line.find("]") + 1] if "]" in line else ""
        for m in _UPDATE_TIP_RE.finditer(line):
            events["tip_transitions"].append({
                "log_ts": ts, "block_hash": m.group(1), "height": int(m.group(2))})
        for m in _RECV_BLOCK_RE.finditer(line):
            events["received_block_entries"].append({
                "log_ts": ts, "block_hash": m.group(1)})
    return events


# --------------------------------------------------------------------------- #
# The benchmark
# --------------------------------------------------------------------------- #

@dataclass
class PropagationConfig:
    seed: int = 1
    num_utxos: int = 2000          # poison inputs (N)
    sigops_per_input: int = 100    # CHECKSIG per input (K)
    observer_par: list = field(default_factory=lambda: [1])
    num_observers: int = 1
    miner_par: int = 0             # validation threads on the miner (0 = default)
    topology: str = "star"         # star | line | tree
    debug_net: bool = True         # add -debug=net to observers (for log events)

    def __post_init__(self):
        # Accept either an int (all observers) or a list (heterogeneous).
        if isinstance(self.observer_par, int):
            self.observer_par = [self.observer_par]
        self.observer_par = [int(x) for x in self.observer_par]
        if not self.observer_par:
            self.observer_par = [1]


def _block_on_tip(node: Node, tx) -> object:
    from test_framework.blocktools import create_block, create_coinbase
    height = node.rpc.getblockcount() + 1
    block = create_block(
        tmpl={"previousblockhash": node.rpc.getbestblockhash(),
              "curtime": node.rpc.getblockchaininfo()["time"] + 1, "height": height},
        coinbase=create_coinbase(height),
        txlist=[tx])
    block.solve()
    return block


class PropagationBenchmark:
    def __init__(self, prop_cfg: PropagationConfig, bench_cfg: BenchmarkConfig,
                 workspace: Path, log):
        self.prop = prop_cfg
        self.bench = bench_cfg
        self.workspace = Path(workspace)
        self.log = log or (lambda *_: None)

    def _observer_bench_cfg(self, par: int) -> BenchmarkConfig:
        bc = BenchmarkConfig(**vars(self.bench))
        bc.validation_threads = par
        return bc

    def _wait_sync(self, obs: Node, target_height: int, timeout: float = 300.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if obs.rpc.getblockcount() >= target_height:
                    return
            except Exception:
                pass
            time.sleep(0.2)
        raise SafetyError(
            f"observer {obs.port} did not sync to height {target_height} in time "
            f"(height={obs.rpc.getblockcount()})")

    def _measure_baseline(self, miner: Node, first_obs: Node) -> dict:
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
        if submit_res is not None:
            return {"baseline_note": f"miner rejected baseline block: {submit_res}",
                    "baseline_submit_wall_seconds": round(submit_wall, 6)}
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if first_obs.rpc.getbestblockhash() == new_hash:
                    return {"baseline_time_to_tip_seconds": round(time.perf_counter() - t_start, 6),
                            "baseline_submit_wall_seconds": round(submit_wall, 6),
                            "baseline_block": new_hash}
            except Exception:
                time.sleep(0.05)
            time.sleep(0.05)
        return {"baseline_note": "peer did not observe normal block in 60s",
                "baseline_submit_wall_seconds": round(submit_wall, 6)}

    def run(self) -> dict:
        prop = self.prop
        log = self.log
        topo = build_topology(prop.topology, prop.num_observers, list(prop.observer_par))

        # Assign P2P ports: each observer that has children needs to listen.
        obs_ports = _ports(topo.num_observers)   # obs id i (1-based) -> obs_ports[i-1]
        miner_port = _free_port()

        # Determine each observer's upstream peer addresses.
        # _upstream maps obs id -> [parent obs id (1-based) or 0=miner]
        upstream = getattr(topo, "_upstream", {s.id: [0] for s in topo.observer_specs})

        obs_nodes = {}
        miner = None
        try:
            miner = Node(self.bench, self.workspace, log,
                         p2p_listen_port=miner_port, p2p_bind_host="127.0.0.1")
            miner.start(); miner.verify_regtest()
            log(f"miner up (p2p 127.0.0.1:{miner_port})")

            gen_cfg = ConstructionConfig(seed=prop.seed, num_utxos=prop.num_utxos,
                                         sigops_per_input=prop.sigops_per_input,
                                         deterministic_time=False)
            from benchmark import required_blocks
            if required_blocks(gen_cfg) > self.bench.max_blocks:
                raise SafetyError(
                    f"construction (N={gen_cfg.num_utxos}, K={gen_cfg.sigops_per_input}) "
                    f"requires {required_blocks(gen_cfg)} blocks, exceeding "
                    f"max_blocks={self.bench.max_blocks}. Refusing before constructing.")
            gen = PoisonBlockGenerator(miner.rpc, gen_cfg, log)
            res = gen.generate()
            poison_tx = res.poison_tx
            log(f"prep done; poison tx: {poison_tx.vin.__len__()} inputs, "
                f"{res.metrics['executed_checksig_count']} executed CHECKSIG")
            target_height = miner.rpc.getblockcount()

            # Launch observers in their topology positions.
            for spec in topo.observer_specs:
                parents = upstream.get(spec.id, [0])
                peers = [f"127.0.0.1:{miner_port if p == 0 else obs_ports[p - 1]}"
                         for p in parents]
                bc = self._observer_bench_cfg(spec.par)
                extra = list(bc.extra_args)
                if prop.debug_net:
                    extra = extra + ["-debug=net", "-logtimestamps=1"]
                bc.extra_args = extra
                node = Node(bc, self.workspace, log,
                            p2p_peers=peers,
                            p2p_listen_port=obs_ports[spec.id - 1],
                            p2p_bind_host="127.0.0.1")
                node.start(); node.verify_regtest()
                obs_nodes[spec.id] = node
                log(f"observer {spec.id} up (par={spec.par}, p2p listen "
                    f"127.0.0.1:{obs_ports[spec.id - 1]}, upstream={peers})")

            # Sync every observer to the miner's full chain.
            for spec in topo.observer_specs:
                self._wait_sync(obs_nodes[spec.id], target_height)
            log("all observers synced to miner's chain")

            first_obs = obs_nodes[topo.observer_specs[0].id]
            baseline = self._measure_baseline(miner, first_obs)
            log(f"baseline normal-block time-to-tip: "
                f"{baseline.get('baseline_time_to_tip_seconds')}s")

            # Build + submit the poison block, measure every observer.
            poison_block = _block_on_tip(miner, poison_tx)
            poison_hash = poison_block.hash_hex
            pre_poison_tip = miner.rpc.getbestblockhash()

            probes = {}
            for spec in topo.observer_specs:
                pr = _ObserverProbe(obs_nodes[spec.id])
                pr.start(pre_poison_tip, poison_hash)
                probes[spec.id] = pr

            guard = ResourceGuard(miner._proc.pid,
                                  max_rss_mb=self.bench.max_peak_rss_mb,
                                  max_wall_seconds=self.bench.max_wall_seconds,
                                  on_violation=miner.terminate)
            guard.start()
            t0 = time.perf_counter()
            submit_result, submit_wall = timed_rpc(
                miner.rpc, "submitblock", poison_block.serialize().hex())
            miner_accepted = submit_result is None
            guard.stop()
            t_submit_ret_ts = time.perf_counter()   # miner finished (starts announcing)

            # Wait until the deepest observer (or any observer) reaches the tip,
            # up to a generous deadline.
            deadline = time.time() + 20 * 60
            while time.time() < deadline:
                reached = [sid for sid, pr in probes.items()
                           if pr._tip_seen is not None]
                if reached and all(pr._tip_seen is not None for pr in probes.values()):
                    break
                time.sleep(0.1)
            for sid, pr in probes.items():
                pr.stop()

            obs_results = []
            for spec in topo.observer_specs:
                node = obs_nodes[spec.id]
                probe = probes[spec.id]
                tip_seen = probe._tip_seen
                obs_results.append(self._observer_record(
                    spec, node, probe, tip_seen, t0, t_submit_ret_ts, poison_hash,
                    pre_poison_tip, upstream))

            return self._build_report(res, baseline, poison_hash, poison_hash,
                                      {"submit_accepted": miner_accepted,
                                       "miner_validation_seconds": round(submit_wall, 6)},
                                      obs_results, topo, upstream, miner)
        finally:
            for node in obs_nodes.values():
                node.stop()
            if miner is not None:
                miner.stop()

    def _observer_record(self, spec, node: Node, probe: _ObserverProbe,
                         tip_seen, t0, t_submit_ret_ts, poison_hash,
                         pre_poison_tip, upstream) -> dict:
        rpc_info = _safe_call(node.rpc, "getnetworkinfo", {})
        info = _safe_call(node.rpc, "getblockchaininfo", {})
        parents = upstream.get(spec.id, [0])
        rec = {
            "observer_id": spec.id,
            "par": spec.par,
            "node_version": rpc_info.get("version"),
            "node_subversion": rpc_info.get("subversion", ""),
            "upstream_peers": ["miner" if p == 0 else f"obs{p}" for p in parents],
            "downstream_peers": _downstream(spec.id, upstream),
            "pre_poison_tip": pre_poison_tip,
            "poison_block_hash": poison_hash,
            "time_to_tip_seconds": round(tip_seen - t0, 6) if tip_seen else None,
            "post_miner_time_to_tip_seconds": round(tip_seen - t_submit_ret_ts, 6) if tip_seen else None,
            "stale_tip_duration_seconds": round(tip_seen - t0, 6) if tip_seen else None,
            "final_chain_height": info.get("blocks"),
            "success": "reached_tip" if tip_seen else "timeout",
            "errors_or_disconnects": probe.stats()["rpc_probe_error_count"],
            **probe.stats(),
        }
        return rec

    def _build_report(self, res, baseline, poison_hash, block_hash, miner,
                      obs_results, topo, upstream, miner_node) -> dict:
        prop = self.prop
        m = res.metrics
        # Per-observer time-to-tip for aggregation.
        ttt = [o["time_to_tip_seconds"] for o in obs_results if o["time_to_tip_seconds"] is not None]
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "propagation",
            "run": {
                "profile": "propagation",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "seed": prop.seed,
                "pba_bench_commit": git_commit(Path(__file__).resolve().parent),
            },
            "topology": {
                "kind": topo.kind,
                "num_observers": topo.num_observers,
                "edges": [[src, dst] for src, dst in topo.edges],
                "observer_par": [s.par for s in topo.observer_specs],
                "peering": "loopback-only P2P between disposable local regtest nodes",
            },
            "construction": {
                "vector": self.bench.vector,
                "num_utxos": m["num_utxos"],
                "sigops_per_input": m["sigops_per_input"],
                "num_prep_blocks": m.get("num_prep_blocks"),
                "executed_checksig_count": m["executed_checksig_count"],
                "ecdsa_verify_count": m.get("ecdsa_verify_count"),
                "sighash_serialization_bytes": m["sighash_serialization_bytes"],
                "sighash_double_sha256_bytes": m.get("sighash_double_sha256_bytes"),
                "no_cache_sighash_serialization_bytes": m.get("no_cache_sighash_serialization_bytes"),
                "per_input_preimage_bytes": m.get("per_input_preimage_bytes"),
                "total_legacy_sigops_bip54": m["total_legacy_sigops_bip54"],
                "poison_tx_vin_count": m["poison_tx_vin_count"],
                "poison_tx_size_bytes": m["poison_tx_size_bytes"],
                "poison_tx_weight": m["poison_tx_weight"],
                "poison_block": poison_hash,
            },
            "baseline": baseline,
            "miner": miner,
            "observers": obs_results,
            "aggregate_time_to_tip_seconds": summarize(ttt),
        }


def _downstream(obs_id, upstream):
    return sorted(p for p, parents in upstream.items() if obs_id in parents)


def _safe_call(rpc, method, default):
    try:
        return getattr(rpc, method)()
    except Exception:
        return default
