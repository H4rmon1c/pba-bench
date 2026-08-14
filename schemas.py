"""Result schema for pba-bench.

Defines the ordered list of fields written to JSON and CSV, so that every run
carries the same provenance and measurement data regardless of which binary or
profile produced it.
"""

#: Ordered result fields. ``group`` is used for the JSON nesting; the flat CSV
#: uses ``group.field`` as the column name.
RESULT_FIELDS = [
    # -- provenance -------------------------------------------------------- #
    ("run", "run_id", "str"),
    ("run", "profile", "str"),
    ("run", "timestamp_utc", "str"),
    ("run", "seed", "int"),
    ("provenance", "node_version", "int"),
    ("provenance", "node_subversion", "str"),
    ("provenance", "node_version_string", "str"),
    ("provenance", "node_git_commit", "str"),
    ("provenance", "compiler", "str"),
    ("provenance", "build_type", "str"),
    ("provenance", "bitcoind_path", "str"),
    ("provenance", "kernel", "str"),
    ("provenance", "os_name", "str"),
    ("provenance", "machine", "str"),
    ("provenance", "cpu_model", "str"),
    ("provenance", "core_count", "int"),
    ("provenance", "physical_cores", "int"),
    ("provenance", "total_ram_bytes", "int"),
    ("provenance", "validation_threads", "int"),
    ("provenance", "warm_cold", "str"),
    # -- construction ------------------------------------------------------ #
    ("construction", "vector", "str"),
    ("construction", "num_utxos", "int"),
    ("construction", "sigops_per_input", "int"),
    ("construction", "num_prep_blocks", "int"),
    ("construction", "num_prep_transactions", "int"),
    ("construction", "total_legacy_sigops_bip54", "int"),
    ("construction", "poison_tx_vin_count", "int"),
    ("construction", "poison_tx_vout_count", "int"),
    ("construction", "poison_tx_size_bytes", "int"),
    ("construction", "poison_tx_weight", "int"),
    ("construction", "poison_block_size_bytes", "int"),
    ("construction", "poison_block_weight", "int"),
    ("construction", "expected_sighash_preimage_bytes", "int"),
    ("construction", "theoretical_sighash_preimage_bytes_no_cache", "int"),
    # -- outcome ----------------------------------------------------------- #
    ("outcome", "success", "str"),            # accepted|rejected|timeout|crash
    ("outcome", "rejection_reason", "str"),
    ("outcome", "block_hash", "str"),
    ("outcome", "block_height", "int"),
    ("outcome", "bip54_would_reject", "bool"),
    # -- measurements ------------------------------------------------------ #
    ("measurement", "baseline_wall_seconds", "float"),
    ("measurement", "validation_wall_seconds", "float"),
    ("measurement", "validation_cpu_seconds", "float"),
    ("measurement", "peak_rss_bytes", "int"),
    ("measurement", "rpc_probe_count", "int"),
    ("measurement", "rpc_probe_max_seconds", "float"),
    ("measurement", "rpc_probe_median_seconds", "float"),
    ("measurement", "block_tx_count", "int"),
    # -- limits in effect -------------------------------------------------- #
    ("limits", "max_wall_seconds", "int"),
    ("limits", "max_peak_rss_mb", "int"),
    ("limits", "max_blocks", "int"),
    ("limits", "max_poison_tx_bytes", "int"),
]


def flat_result(result: dict) -> dict:
    """Flatten a nested result dict into ``group.field`` keys for CSV."""
    out = {}
    for group, field, _ in RESULT_FIELDS:
        out[f"{group}.{field}"] = result.get(group, {}).get(field, "")
    return out


def csv_columns() -> list:
    return [f"{g}.{f}" for g, f, _ in RESULT_FIELDS]
