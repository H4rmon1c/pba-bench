"""BIP 54 (Consensus Cleanup) activation and boundary helpers.

pba-bench targets a disposable, loopback-only regtest node. To measure the
*post-BIP54* worst case we must activate the ``consensuscleanup`` BIP9 deployment
on that node (BIP 54 is not active on regtest by default). This module mines
through the deployment cycle with BIP54-compliant coinbases and exposes the live
boundary classifications used by the research harness.

Reference implementation: Bitcoin Core PR #35793 ("Implement BIP 54 (Consensus
Cleanup) without mainnet activation"), commit 9630491bf2135d03dac586d3492cfca9939f6fbb.
"""

from __future__ import annotations

from test_framework.blocktools import NULL_OUTPOINT, create_block, create_coinbase, \
    script_BIP34_coinbase_height
from test_framework.key import ECKey
from test_framework.messages import MAX_SEQUENCE_NONFINAL

#: BIP 54 per-transaction legacy-sigop consensus limit (MAX_TX_BIP54_SIGOPS).
BIP54_MAX_TX_SIGOPS = 2500
#: The BIP9 deployment name in Bitcoin Core for BIP 54.
DEPLOYMENT_NAME = "consensuscleanup"
#: Default activation override for regtest (start at 0, no timeout).
VB_PARAMS = f"{DEPLOYMENT_NAME}:0:3999999999"
#: The block version bit used by the last pre-BIP54 blocks (BIP34-style).
VERSIONBITS_LAST_OLD_BLOCK_VERSION = 0x20000000
#: Regtest retarget period (blocks).
REGTEST_RETARGET_PERIOD = 144


def _default_pubkey() -> bytes:
    k = ECKey()
    k.set(bytes(range(32)), True)
    return k.get_pubkey().get_bytes()


def bip54_compliant_coinbase(height: int, pubkey: bytes | None = None):
    """A coinbase that satisfies the BIP 54 rules (PR #35793 revision):

      * ``nLockTime == height - 1`` (the PR implements height-1, not the BIP
        text's height-15),
      * non-final input sequence (``nSequence != 0xffffffff``),
      * serialized size > 64 bytes (a P2PK output keeps it well above 64 B).
    """
    if pubkey is None:
        pubkey = _default_pubkey()
    coinbase = create_coinbase(height, pubkey=pubkey)
    coinbase.nLockTime = height - 1
    coinbase.vin[0].nSequence = MAX_SEQUENCE_NONFINAL
    return coinbase


def _mine(node, count: int, version=VERSIONBITS_LAST_OLD_BLOCK_VERSION, pubkey=None):
    """Mine ``count`` BIP54-compliant blocks of the given version."""
    height = node.getblockcount() + 1
    prev = node.getbestblockhash()
    prev_time = node.getblockheader(prev)["time"]
    for _ in range(count):
        block = create_block(tmpl={
            "previousblockhash": prev, "height": height,
            "curtime": prev_time + 1, "version": version,
        }, coinbase=bip54_compliant_coinbase(height, pubkey))
        block.solve()
        res = node.submitblock(block.serialize().hex())
        if res is not None:
            raise RuntimeError(f"activation block rejected: {res}")
        prev_time += 1
        prev = block.hash_hex
        height += 1


def is_bip54_active(node) -> bool:
    d = node.getdeploymentinfo()
    return (d["deployments"].get(DEPLOYMENT_NAME, {}).get("active") is True)


def activate_bip54(node, log=None) -> bool:
    """Mine to activate the ``consensuscleanup`` deployment. Idempotent.

    Returns True if the deployment is active afterward. Uses the same cycle as
    Bitcoin Core's ``feature_bip54.py`` functional test.
    """
    log = log or (lambda *a: None)
    if is_bip54_active(node):
        log("consensuscleanup already active")
        return True
    cc = node.getdeploymentinfo()["deployments"][DEPLOYMENT_NAME]["bip9"]
    log(f"consensuscleanup status: {cc['status']}")

    # Reach the end of the current retarget period, then one more block so the
    # versionbits state is evaluated against a parent whose MTP >= start_time.
    n = REGTEST_RETARGET_PERIOD - node.getblockcount() % REGTEST_RETARGET_PERIOD - 1
    _mine(node, n)
    cc = node.getdeploymentinfo()["deployments"][DEPLOYMENT_NAME]["bip9"]
    if cc["status"] != "started":
        _mine(node, 1)
        cc = node.getdeploymentinfo()["deployments"][DEPLOYMENT_NAME]["bip9"]
    if cc["status"] != "started":
        raise RuntimeError(f"expected consensuscleanup started, got {cc['status']}")

    bit = cc["bit"]
    threshold = cc["statistics"]["threshold"]
    signal = (1 << 29) | (1 << bit)
    _mine(node, threshold, version=signal)
    _mine(node, REGTEST_RETARGET_PERIOD - threshold + 1)
    cc = node.getdeploymentinfo()["deployments"][DEPLOYMENT_NAME]["bip9"]
    if cc["status"] != "locked_in":
        raise RuntimeError(f"expected locked_in, got {cc['status']}")
    _mine(node, REGTEST_RETARGET_PERIOD)
    cc = node.getdeploymentinfo()["deployments"][DEPLOYMENT_NAME]["bip9"]
    log(f"consensuscleanup final status: {cc['status']}")
    if cc["status"] != "active":
        raise RuntimeError(f"failed to activate BIP54: {cc['status']}")
    return True


def activation_block_count() -> int:
    """Blocks mined to activate BIP54 from a fresh regtest chain (height 0)."""
    return (REGTEST_RETARGET_PERIOD - 1) + 1 + REGTEST_RETARGET_PERIOD \
        + (REGTEST_RETARGET_PERIOD - 144 + 1) + REGTEST_RETARGET_PERIOD + 1
