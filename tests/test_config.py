"""Unit tests for construction config validation and CLI parsing."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from construction import ConstructionConfig, MAX_SCRIPT_SIZE, MAX_OPS_PER_SCRIPT
from safety import SafetyError


def test_config_valid_defaults():
    cfg = ConstructionConfig()
    cfg.validate()  # must not raise


def test_config_rejects_zero_utxos():
    with pytest.raises(ValueError):
        ConstructionConfig(num_utxos=0).validate()


def test_config_rejects_zero_sigops():
    with pytest.raises(ValueError):
        ConstructionConfig(sigops_per_input=0).validate()


def test_config_rejects_script_size_over_max():
    # K that makes the scriptPubKey exceed MAX_SCRIPT_SIZE (10,000 bytes).
    cfg = ConstructionConfig(sigops_per_input=500)
    with pytest.raises(SafetyError, match="MAX_SCRIPT_SIZE"):
        cfg.validate()


def test_config_rejects_ops_over_limit():
    # 2K-1 non-push ops in the scriptPubKey must stay <= MAX_OPS_PER_SCRIPT.
    cfg = ConstructionConfig(sigops_per_input=102)  # 2*102-1 = 203 > 201
    with pytest.raises(SafetyError, match="MAX_OPS_PER_SCRIPT"):
        cfg.validate()
    # 101 is the max for this construction (2*101-1 = 201 <= 201).
    ConstructionConfig(sigops_per_input=101).validate()


def test_config_rejects_absurd_total_sigops():
    cfg = ConstructionConfig(num_utxos=100000, sigops_per_input=100000)
    with pytest.raises(SafetyError):
        cfg.validate()


def test_cli_requires_confirm_for_custom(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import pba_bench
    rc = pba_bench.main(["benchmark", "--bitcoind", "/bin/true",
                         "--profile", "custom", "--num-utxos", "10",
                         "--sigops-per-input", "2"])
    assert rc == 2  # requires --confirm


def test_cli_parse_observer_par_list():
    from pba_bench import cmd_propagate
    import argparse
    # Ensure the propagate arg parsing accepts a comma list.
    p = argparse.ArgumentParser()
    p.add_argument("--observer-par", default="1")
    args = p.parse_args(["--observer-par", "1,2,4,8,0"])
    assert args.observer_par == "1,2,4,8,0"
