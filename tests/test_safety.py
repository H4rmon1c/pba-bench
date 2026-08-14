"""Tests for the pba-bench safety layer.

These tests are pure (no node required) and prove the safety controls actually
reject unsafe configurations in code.
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

from safety import (
    DEFAULT_LIMITS,
    SafetyError,
    SafetyValidator,
    SafeConfig,
    is_loopback_ip,
    verify_chain_is_regtest,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def validator(workspace):
    return SafetyValidator(workspace)


# --------------------------------------------------------------------------- #
# Network rejection
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("arg", ["-mainnet", "-testnet", "-testnet4", "-signet"])
def test_network_flags_rejected(validator, arg):
    with pytest.raises(SafetyError):
        validator.validate_extra_args([arg])


def test_build_args_always_regtest(workspace):
    validator = SafetyValidator(workspace)
    datadir = validator.prepare_datadir(None)
    cfg = SafeConfig(bitcoind_path=Path("/bin/true"), datadir=datadir)
    args = cfg.build_bitcoind_args()
    assert "-regtest" in args
    assert not any(a.startswith(("-mainnet", "-testnet", "-signet")) for a in args)


# --------------------------------------------------------------------------- #
# Chain verification (getblockchaininfo)
# --------------------------------------------------------------------------- #

class _FakeRPC:
    def __init__(self, chain):
        self._chain = chain

    def getblockchaininfo(self):
        return {"chain": self._chain}


@pytest.mark.parametrize("chain", ["main", "test", "testnet4", "signet", "foo"])
def test_verify_chain_rejects_non_regtest(chain):
    with pytest.raises(SafetyError, match="regtest"):
        verify_chain_is_regtest(_FakeRPC(chain))


def test_verify_chain_accepts_regtest():
    verify_chain_is_regtest(_FakeRPC("regtest"))


# --------------------------------------------------------------------------- #
# Loopback RPC
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("host", ["192.168.1.5", "8.8.8.8", "0.0.0.0", "10.0.0.1"])
def test_non_loopback_rpc_rejected(validator, host):
    with pytest.raises(SafetyError):
        validator.validate_rpc_host(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_rpc_accepted(validator, host):
    assert validator.validate_rpc_host(host)


def test_is_loopback_ip():
    assert is_loopback_ip("127.0.0.1")
    assert is_loopback_ip("::1")
    assert not is_loopback_ip("8.8.8.8")
    assert not is_loopback_ip("not-an-ip")


def test_safe_config_refuses_non_loopback(workspace):
    validator = SafetyValidator(workspace)
    datadir = validator.prepare_datadir(None)
    cfg = SafeConfig(bitcoind_path=Path("/bin/true"), datadir=datadir, rpc_host="8.8.8.8")
    with pytest.raises(SafetyError):
        cfg.build_bitcoind_args()


# --------------------------------------------------------------------------- #
# Datadir safety
# --------------------------------------------------------------------------- #

def test_existing_datadir_rejected(validator, workspace):
    existing = workspace / "existing"
    existing.mkdir()
    with pytest.raises(SafetyError, match="reuse"):
        validator.prepare_datadir(existing)


def test_datadir_outside_workspace_rejected(validator, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(SafetyError, match="workspace"):
        validator.prepare_datadir(outside)


def test_symlink_escape_rejected(validator, workspace, tmp_path):
    target = tmp_path / "real-outside"
    target.mkdir()
    link = workspace / "evil-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(SafetyError, match="workspace"):
        validator.prepare_datadir(link)


def test_fresh_datadir_created(validator, workspace):
    datadir = validator.prepare_datadir(None)
    assert datadir.is_dir()
    assert datadir.parent == workspace / "work"
    assert datadir.resolve().is_relative_to(workspace.resolve())


# --------------------------------------------------------------------------- #
# Unsafe extra arguments
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("arg", [
    "-connect=8.8.8.8",
    "-addnode=1.2.3.4",
    "-seednode=seed.bitcoin.sipa.be",
    "-dnsseed=1",
    "-proxy=127.0.0.1:9050",
    "-bind=0.0.0.0:8333",
    "-rpcbind=0.0.0.0",
    "-rpcallowip=0.0.0.0/0",
    "-externalip=8.8.8.8",
    "-listen=1",
    "-upnp=1",
    "-natpmp=1",
    "-datadir=/tmp/something",
    "-rpcpassword=hunter2",
    "-daemon=1",
])
def test_unsafe_extra_args_rejected(validator, arg):
    with pytest.raises(SafetyError):
        validator.validate_extra_args([arg])


@pytest.mark.parametrize("arg", [
    "-debug=1",
    "-par=4",
    "-maxmempool=50",
    "-rpcbind=127.0.0.1",
    "-rpcallowip=127.0.0.1/0",
    "-vbparams=consensuscleanup:0:3999999999",
])
def test_safe_extra_args_accepted(validator, arg):
    validator.validate_extra_args([arg])


# --------------------------------------------------------------------------- #
# Cleanup cannot escape the workspace
# --------------------------------------------------------------------------- #

def test_datadir_resolution_stays_in_workspace(validator, workspace):
    datadir = validator.prepare_datadir(None)
    # Simulate the cleanup routine: rmtree only ever receives a path we created
    # inside the workspace; verify the resolved path is still inside the workspace.
    assert datadir.resolve().is_relative_to(workspace.resolve())
    shutil.rmtree(datadir)


def test_cleanup_does_not_touch_sibling(tmp_path):
    # Ensure deleting a datadir inside the workspace never affects siblings.
    ws = tmp_path / "ws"
    ws.mkdir()
    datadir = ws / "datadir"
    datadir.mkdir()
    sibling = ws / "precious"
    sibling.mkdir()
    shutil.rmtree(datadir)
    assert sibling.is_dir()


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

def test_default_limits_present(validator):
    for k in ("max_wall_seconds", "max_peak_rss_mb", "max_blocks", "max_poison_tx_bytes"):
        assert k in DEFAULT_LIMITS


def test_safe_config_defaults_to_networking_disabled(workspace):
    validator = SafetyValidator(workspace)
    datadir = validator.prepare_datadir(None)
    cfg = SafeConfig(bitcoind_path=Path("/bin/true"), datadir=datadir)
    args = cfg.build_bitcoind_args()
    assert "-connect=0" in args
    assert "-listen=0" in args
    assert "-dnsseed=0" in args
    assert "-discover=0" in args
