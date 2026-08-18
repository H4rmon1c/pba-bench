"""Result schema for pba-bench.

Defines the ordered list of fields written to JSON and CSV, so that every run
carries the same provenance and measurement data regardless of which binary or
profile produced it.

The schema is versioned: bump :data:`SCHEMA_VERSION` whenever the set or
semantics of fields changes. Every result manifest records the schema version
that produced it so downstream tools can validate against the right shape.
"""

#: Version of this result schema. Bump on any field addition/removal/rename.
SCHEMA_VERSION = "2.0.0"

#: Ordered result fields. ``group`` is used for the JSON nesting; the flat CSV
#: uses ``group.field`` as the column name.
RESULT_FIELDS = [
    # -- schema ------------------------------------------------------------ #
    ("run", "schema_version", "str"),
    ("run", "run_id", "str"),
    ("run", "profile", "str"),
    ("run", "command", "str"),
    ("run", "timestamp_utc", "str"),
    ("run", "seed", "int"),
    # -- provenance -------------------------------------------------------- #
    ("provenance", "node_version", "int"),
    ("provenance", "node_subversion", "str"),
    ("provenance", "node_version_string", "str"),
    ("provenance", "node_git_commit", "str"),
    ("provenance", "compiler", "str"),
    ("provenance", "build_type", "str"),
    ("provenance", "bitcoind_path", "str"),
    ("provenance", "bitcoind_sha256", "str"),
    ("provenance", "kernel", "str"),
    ("provenance", "os_name", "str"),
    ("provenance", "machine", "str"),
    ("provenance", "cpu_model", "str"),
    ("provenance", "core_count", "int"),
    ("provenance", "physical_cores", "int"),
    ("provenance", "total_ram_bytes", "int"),
    ("provenance", "validation_threads", "int"),
    ("provenance", "warm_cold", "str"),
    ("provenance", "cpu_affinity", "str"),
    ("provenance", "pba_bench_commit", "str"),
    # -- construction ------------------------------------------------------ #
    ("construction", "vector", "str"),
    ("construction", "num_utxos", "int"),
    ("construction", "sigops_per_input", "int"),
    ("construction", "spk_kind", "str"),
    ("construction", "num_prep_blocks", "int"),
    ("construction", "num_prep_transactions", "int"),
    ("construction", "num_poison_txs", "int"),
    ("construction", "per_tx_inputs", "int"),
    ("construction", "max_sigops_per_tx_bip54", "int"),
    ("construction", "total_legacy_sigops_bip54", "int"),
    ("construction", "executed_checksig_count", "int"),
    ("construction", "ecdsa_verify_count", "int"),
    ("construction", "poison_tx_vin_count", "int"),
    ("construction", "poison_tx_vout_count", "int"),
    ("construction", "poison_tx_size_bytes", "int"),
    ("construction", "poison_tx_weight", "int"),
    ("construction", "poison_block_size_bytes", "int"),
    ("construction", "poison_block_weight", "int"),
    # -- cost model quantities (measured-by-construction) ------------------ #
    ("construction", "sighash_serialization_bytes", "int"),
    ("construction", "sighash_double_sha256_bytes", "int"),
    ("construction", "per_input_preimage_bytes", "float"),
    # -- hypothetical no-cache quantities (NOT v31.1.0 behavior) ----------- #
    ("construction", "no_cache_sighash_serialization_bytes", "int"),
    # -- outcome ----------------------------------------------------------- #
    ("outcome", "success", "str"),            # accepted|rejected|timeout|crash
    ("outcome", "rejection_reason", "str"),
    ("outcome", "block_hash", "str"),
    ("outcome", "block_height", "int"),
    ("outcome", "bip54_would_reject", "bool"),
    ("outcome", "bip54_result", "str"),       # live|inferred|not_tested
    # -- measurements ------------------------------------------------------ #
    ("measurement", "baseline_wall_seconds", "float"),
    ("measurement", "validation_wall_seconds", "float"),
    ("measurement", "validation_cpu_seconds", "float"),
    ("measurement", "peak_rss_bytes", "int"),
    ("measurement", "rpc_probe_count", "int"),
    ("measurement", "rpc_probe_max_seconds", "float"),
    ("measurement", "rpc_probe_median_seconds", "float"),
    ("measurement", "rpc_probe_timeout_count", "int"),
    ("measurement", "rpc_probe_error_count", "int"),
    ("measurement", "rpc_probe_lower_bound_seconds", "float"),
    ("measurement", "block_tx_count", "int"),
    # -- limits in effect -------------------------------------------------- #
    ("limits", "max_wall_seconds", "int"),
    ("limits", "max_peak_rss_mb", "int"),
    ("limits", "max_blocks", "int"),
    ("limits", "max_poison_tx_bytes", "int"),
]

#: Fields that still exist in v1.0.0 results but were renamed/superseded in
#: v2.0.0. Reading v1 results is supported by :func:`migrate_v1_result`.
DEPRECATED_FIELDS = {
    ("construction", "expected_sighash_preimage_bytes"): "sighash_serialization_bytes",
    ("construction", "theoretical_sighash_preimage_bytes_no_cache"): "no_cache_sighash_serialization_bytes",
}


def flat_result(result: dict) -> dict:
    """Flatten a nested result dict into ``group.field`` keys for CSV."""
    out = {}
    for group, field, _ in RESULT_FIELDS:
        out[f"{group}.{field}"] = result.get(group, {}).get(field, "")
    return out


def csv_columns() -> list:
    return [f"{g}.{f}" for g, f, _ in RESULT_FIELDS]


def migrate_v1_result(result: dict) -> dict:
    """Upgrade a v1.0.0 result dict in place to the v2 schema.

    v1 results used ``expected_sighash_preimage_bytes`` (the cache-aware sum of
    per-input preimage serialization) and
    ``theoretical_sighash_preimage_bytes_no_cache``. These map onto the v2
    fields ``sighash_serialization_bytes`` and
    ``no_cache_sighash_serialization_bytes`` respectively. New fields that a v1
    run cannot supply are left absent (CSV will render them as empty).
    """
    if result.get("run", {}).get("schema_version"):
        return result
    result.setdefault("run", {})["schema_version"] = "1.0.0"
    for (group, old), new in DEPRECATED_FIELDS.items():
        g = result.setdefault(group, {})
        if old in g and new not in g:
            g[new] = g[old]
    return result


def validate_result(result: dict) -> list:
    """Return a list of problems for a result dict, or [] if it is well-formed.

    Checks the presence of every schema field and a few invariants. Used to
    validate externally-contributed result files (see `results/README.md`).
    """
    problems = []
    for group, field, typ in RESULT_FIELDS:
        value = result.get(group, {}).get(field)
        if value is None or value == "":
            continue  # optional for imported/v1 results
        if typ == "int" and not isinstance(value, int):
            problems.append(f"{group}.{field}: expected int, got {type(value).__name__}")
        elif typ == "float" and not isinstance(value, (int, float)):
            problems.append(f"{group}.{field}: expected float, got {type(value).__name__}")
    if not result.get("construction", {}).get("vector"):
        problems.append("construction.vector: missing")
    return problems
