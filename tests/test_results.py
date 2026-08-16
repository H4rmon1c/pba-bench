"""Tests for result export (JSON/CSV) and schema completeness."""

import csv
import json
import os
import sys

import pytest

from schemas import (
    RESULT_FIELDS,
    SCHEMA_VERSION,
    csv_columns,
    flat_result,
    migrate_v1_result,
    validate_result,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sample_result():
    """A fully-populated result dict covering every required field."""
    result = {}
    for group, field, typ in RESULT_FIELDS:
        if field == "run_id":
            val = "test-run"
        elif field == "success":
            val = "accepted"
        elif typ == "int":
            val = 1
        elif typ == "float":
            val = 1.5
        else:
            val = "value"
        result.setdefault(group, {})[field] = val
    return result


def test_result_has_all_required_fields():
    result = _sample_result()
    for group, field, _ in RESULT_FIELDS:
        assert field in result[group], f"missing {group}.{field}"


def test_flat_result_covers_all_csv_columns():
    flat = flat_result(_sample_result())
    for col in csv_columns():
        assert col in flat


def test_csv_columns_are_grouped():
    cols = csv_columns()
    assert "provenance.node_version" in cols
    assert "construction.total_legacy_sigops_bip54" in cols
    assert "construction.sighash_serialization_bytes" in cols
    assert "measurement.validation_wall_seconds" in cols
    assert "measurement.rpc_probe_timeout_count" in cols
    assert "outcome.rejection_reason" in cols


def test_schema_version_present():
    assert SCHEMA_VERSION == "2.0.0"


def test_deprecated_v1_fields_are_aliased():
    # The v1 cache-aware preimage bytes map to the v2 serialization bytes.
    v1 = {
        "run": {},
        "construction": {"expected_sighash_preimage_bytes": 5050,
                         "theoretical_sighash_preimage_bytes_no_cache": 10100},
        "outcome": {}, "measurement": {}, "provenance": {}, "limits": {},
    }
    migrated = migrate_v1_result(v1)
    assert migrated["construction"]["sighash_serialization_bytes"] == 5050
    assert migrated["construction"]["no_cache_sighash_serialization_bytes"] == 10100
    assert migrated["run"]["schema_version"] == "1.0.0"


def test_validate_result_ok_for_wellformed():
    result = _sample_result()
    assert validate_result(result) == []


def test_validate_result_flags_missing_vector():
    result = _sample_result()
    result["construction"].pop("vector", None)
    assert any("construction.vector" in p for p in validate_result(result))


def test_export_roundtrip(tmp_path):
    from benchmark import export_results

    results = [_sample_result()]
    jp, cp = export_results(results, tmp_path)
    assert jp.exists() and cp.exists()

    # JSON round-trips.
    loaded = json.loads(jp.read_text())
    assert loaded[0]["run"]["run_id"] == "test-run"

    # CSV has header + 1 row and exactly the schema columns.
    with open(cp, newline="") as f:
        reader = list(csv.DictReader(f))
    assert reader[0]["run.run_id"] == "test-run"
    assert set(reader[0].keys()) == set(csv_columns())


def test_csv_ignores_unknown_fields(tmp_path):
    from benchmark import export_results

    result = _sample_result()
    result["construction"]["extra_unknown"] = 999  # not in schema
    jp, cp = export_results([result], tmp_path)
    with open(cp, newline="") as f:
        reader = list(csv.DictReader(f))
    assert "construction.extra_unknown" not in reader[0]
