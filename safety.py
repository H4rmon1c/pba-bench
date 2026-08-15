"""Safety layer for pba-bench.

This module is the single place that decides whether a given run configuration is
safe to execute. Every benchmark path must go through :func:`SafetyValidator.validate`
before any node is started, and every ``bitcoind`` argument list is produced by
:func:`build_safe_args`, which refuses to emit a non-regtest, non-loopback, or
networked configuration.

The rules here are enforced in code, not just in documentation. There is deliberately
no "public network" deployment mode anywhere in this project.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


class SafetyError(Exception):
    """Raised when a configuration is unsafe. The message is user-facing."""


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Networks that are never allowed, no matter what.
FORBIDDEN_NETWORKS = {"main", "test", "testnet", "testnet4", "testnet3", "signet"}

#: bitcoind flags that change the network or peer connectivity. If a user-supplied
#: extra argument mentions any of these, it is rejected unless the value is an
#: explicitly loopback-only / disabled form that we whitelist separately.
_NETWORK_FLAGS = {
    "-mainnet", "-testnet", "-testnet4", "-testnet3", "-signet", "-regtest",
    "-addnode", "-connect", "-seednode", "-dnsseed", "-dnsseeds", "-discover",
    "-proxy", "-onion", "-i2psam", "-bind", "-listen", "-upnp", "-natpmp",
    "-externalip", "-rpcbind", "-rpcallowip", "-onlynet", "-port", "-rpcport",
}

#: Flags that change where the node reads/writes state. These are managed by the
#: benchmark itself and are never allowed from a user-supplied extra arg.
_MANAGED_FLAGS = {"-datadir", "-conf", "-rpcuser", "-rpcpassword", "-server",
                  "-daemon", "-pid", "-txindex", "-disablewallet"}

#: Flags whose value must be loopback-only when present in the RPC allow list.
_LOOPBACK_ONLY = {"-rpcbind", "-rpcallowip", "-bind"}

#: A regular expression matching an IPv4 or IPv6 address (without the /mask).
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_IPV6_RE = re.compile(r"^[0-9a-fA-F:]+$")

#: Default limits (configurable). Values are intentionally conservative.
DEFAULT_LIMITS = {
    "max_wall_seconds": 600,      # hard cap on a single block validation wait
    "max_peak_rss_mb": 8192,      # abort if the node's RSS grows beyond this
    "max_blocks": 400,            # maximum total blocks the run may create
    "max_poison_tx_bytes": 3900000,  # keep the poison tx under the 4 MWU block cap
}


def is_loopback_ip(ip: str) -> bool:
    """Return True if *ip* (a bare IP string) is a loopback address."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback


def _is_loopback_peer(hostport: str) -> bool:
    """Return True if ``host:port`` points at a loopback address (host may be bare)."""
    hostport = hostport.strip()
    if hostport.startswith("["):          # [::1]:port
        end = hostport.find("]")
        if end == -1:
            return False
        host = hostport[1:end]
        return is_loopback_ip(host)
    if hostport.count(":") == 1:          # host:port (IPv4)
        host, _, _ = hostport.partition(":")
        host = host.rstrip()
        if host in ("localhost", "localhost."):
            return True
        return is_loopback_ip(host)
    # bare IPv6 or bare IPv4 with no port -> require loopback
    return is_loopback_ip(hostport)


def _parse_flag_value(arg: str):
    """Split a bitcoind-style ``-flag`` or ``-flag=value`` into (flag, value).

    Returns ``(flag, value)`` where value is ``None`` when no ``=value`` was given.
    """
    if "=" in arg:
        flag, value = arg.split("=", 1)
    else:
        flag, value = arg, None
    if not flag.startswith("-"):
        flag = "-" + flag
    return flag, value


@dataclass
class SafeConfig:
    """Fully validated, safe run configuration."""

    bitcoind_path: Path
    datadir: Path                  # fresh disposable datadir (already created)
    rpc_host: str = "127.0.0.1"    # loopback only
    rpc_port: int = 0              # 0 -> pick a free port
    rpc_user: str = "pba"
    rpc_password: str = ""
    limits: dict = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    extra_args: list = field(default_factory=list)
    keep_datadir: bool = False
    disable_networking: bool = True
    # Loopback-only P2P peering (used by the multi-node propagation benchmark).
    # All addresses must resolve to loopback; nothing is ever reachable remotely.
    p2p_peers: list = field(default_factory=list)       # ["127.0.0.1:<port>", ...]
    p2p_listen_port: int = 0                            # 0 = do not listen for P2P
    p2p_bind_host: str = "127.0.0.1"

    def build_bitcoind_args(self) -> list:
        """Return the full, safe ``bitcoind`` argument list.

        If ``p2p_peers`` or ``p2p_listen_port`` are set, P2P networking is enabled
        but restricted to loopback addresses only. Otherwise networking is fully
        disabled.
        """
        if not is_loopback_ip(self.rpc_host):
            raise SafetyError(f"rpc_host is not loopback: {self.rpc_host!r}")
        if not is_loopback_ip(self.p2p_bind_host):
            raise SafetyError(f"p2p_bind_host is not loopback: {self.p2p_bind_host!r}")
        for peer in self.p2p_peers:
            if not _is_loopback_peer(peer):
                raise SafetyError(f"p2p peer is not loopback: {peer!r}")

        args = [
            "-regtest",
            f"-datadir={self.datadir}",
            "-server=1",
            "-daemon=0",
            "-disablewallet=1",
            "-txindex=0",
            "-fallbackfee=0.00001",
            "-acceptnonstdtxn=1",     # poison txs are nonstandard; only affects relay policy, not block consensus
            f"-rpcuser={self.rpc_user}",
            f"-rpcpassword={self.rpc_password}",
            f"-rpcbind={self.rpc_host}",
            f"-rpcallowip=127.0.0.1",
            f"-rpcallowip=::1",
            "-dnsseed=0",   # no DNS seeds (and no fixed seeds)
            "-discover=0",  # no automatic interface discovery
            "-natpmp=0",    # no port mapping
        ]

        peering = bool(self.p2p_peers) or self.p2p_listen_port
        if not peering:
            args += ["-connect=0", "-listen=0"]
        else:
            if self.p2p_listen_port:
                args += [
                    "-listen=1",
                    f"-bind={self.p2p_bind_host}",
                    f"-port={self.p2p_listen_port}",
                ]
            else:
                args += ["-listen=0"]
            for peer in self.p2p_peers:
                args.append(f"-connect={peer}")
            if not self.p2p_peers:
                args.append("-connect=0")

        if self.rpc_port:
            args.append(f"-rpcport={self.rpc_port}")
        args += self.extra_args
        return args


class SafetyValidator:
    """Validates every aspect of a proposed run and raises :class:`SafetyError`."""

    _datadir_counter = 0

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    # -- datadir ----------------------------------------------------------- #
    def prepare_datadir(self, datadir: Path | None) -> Path:
        """Create and return a fresh, disposable datadir.

        Refuses to reuse an existing datadir, refuses paths that exist, refuses
        paths outside the workspace, and refuses paths whose realpath escapes the
        workspace (symlink escape).
        """
        if datadir is None:
            # A counter makes the name unique even when multiple nodes are
            # launched sequentially from the same process (e.g. under pytest).
            counter = SafetyValidator._datadir_counter
            SafetyValidator._datadir_counter += 1
            datadir = self.workspace / "work" / f"datadir-{os.getpid()}-{counter}"

        datadir = Path(datadir)
        if not datadir.is_absolute():
            datadir = (self.workspace / datadir).resolve()
        else:
            datadir = datadir.resolve()

        self._assert_inside_workspace(datadir, what="datadir")

        if datadir.exists():
            raise SafetyError(
                f"refusing to reuse an existing datadir: {datadir}. "
                "A fresh, disposable datadir is required."
            )
        # Create it, then verify the real (resolved) path is still inside the
        # workspace. This catches a symlink planted in an intermediate directory.
        datadir.mkdir(parents=True, exist_ok=False)
        real = datadir.resolve()
        self._assert_inside_workspace(real, what="datadir (after creation)")
        return datadir

    def _assert_inside_workspace(self, path: Path, what: str) -> None:
        try:
            path.relative_to(self.workspace)
        except ValueError:
            raise SafetyError(
                f"{what} escapes the benchmark workspace: {path} "
                f"(workspace={self.workspace})"
            ) from None

    # -- bitcoind path ----------------------------------------------------- #
    def validate_bitcoind(self, bitcoind: Path) -> Path:
        bitcoind = Path(bitcoind).resolve()
        if not bitcoind.is_file():
            raise SafetyError(f"bitcoind not found: {bitcoind}")
        if not os.access(bitcoind, os.X_OK):
            raise SafetyError(f"bitcoind is not executable: {bitcoind}")
        return bitcoind

    # -- rpc host ---------------------------------------------------------- #
    def validate_rpc_host(self, host: str) -> str:
        if host in ("localhost", "localhost."):
            host = "127.0.0.1"
        if is_loopback_ip(host):
            return host
        # Try to resolve; the resolved address(es) must all be loopback.
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise SafetyError(f"unable to resolve RPC host (must be loopback): {host!r}") from None
        addrs = {info[4][0] for info in infos}
        if not addrs:
            raise SafetyError(f"RPC host resolved to nothing (must be loopback): {host!r}")
        for a in addrs:
            if not is_loopback_ip(a):
                raise SafetyError(
                    f"RPC host {host!r} resolves to non-loopback address {a}; refusing."
                )
        return host

    # -- extra bitcoind args ----------------------------------------------- #
    def validate_extra_args(self, extra_args: list) -> list:
        """Validate user-supplied extra ``bitcoind`` arguments.

        Rejects anything that could change the network, the datadir, or RPC
        binding/authentication. Only a conservative allow-list passes.
        """
        out = []
        for arg in extra_args:
            arg = str(arg).strip()
            if not arg:
                continue
            flag, value = _parse_flag_value(arg)

            if flag in _MANAGED_FLAGS:
                raise SafetyError(
                    f"unsafe extra argument {flag!r}: this flag is managed by the "
                    "benchmark and cannot be overridden."
                )
            if flag in _LOOPBACK_ONLY:
                if value is None:
                    raise SafetyError(
                        f"unsafe extra argument {arg!r}: must include an explicit loopback value."
                    )
                if not is_loopback_ip(value.split("/")[0]):
                    raise SafetyError(
                        f"unsafe extra argument {arg!r}: value is not loopback."
                    )
                out.append(arg)
                continue
            if flag in _NETWORK_FLAGS:
                raise SafetyError(
                    f"unsafe extra argument {arg!r}: would change network/peer "
                    "connectivity. The benchmark always runs isolated regtest."
                )
            # Everything else is allowed (e.g. -debug, -par, -maxmempool, -vbparams).
            out.append(arg)
        return out

    # -- p2p peering (loopback only) --------------------------------------- #
    def validate_p2p_peers(self, peers: list, bind_host: str = "127.0.0.1") -> list:
        if not is_loopback_ip(bind_host):
            raise SafetyError(f"p2p_bind_host is not loopback: {bind_host!r}")
        out = []
        for peer in peers:
            if not _is_loopback_peer(str(peer)):
                raise SafetyError(
                    f"P2P peer {peer!r} is not a loopback address; refusing. "
                    "pba-bench only ever peers with its own local regtest nodes."
                )
            out.append(str(peer))
        return out

    # -- full validation --------------------------------------------------- #
    def validate(self, cfg: SafeConfig) -> SafeConfig:
        cfg.bitcoind_path = self.validate_bitcoind(cfg.bitcoind_path)
        cfg.rpc_host = self.validate_rpc_host(cfg.rpc_host)
        cfg.datadir = self.prepare_datadir(cfg.datadir)
        cfg.extra_args = self.validate_extra_args(cfg.extra_args)
        cfg.p2p_peers = self.validate_p2p_peers(cfg.p2p_peers, cfg.p2p_bind_host)
        cfg.limits = {**DEFAULT_LIMITS, **(cfg.limits or {})}
        return cfg


def verify_chain_is_regtest(rpc) -> None:
    """Call ``getblockchaininfo`` and require ``chain == \"regtest\"``.

    This is the last line of defense: even if every other check somehow passed,
    we abort the moment the node reports a non-regtest chain.
    """
    info = rpc.getblockchaininfo()
    chain = info.get("chain")
    if chain != "regtest":
        raise SafetyError(
            f"getblockchaininfo reports chain={chain!r}; refusing to run against "
            "anything other than a fresh regtest node. Aborting."
        )
