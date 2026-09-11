"""Validate the versioned semantic compatibility matrix's safety invariants."""

from __future__ import annotations

import json
from pathlib import Path


MATRIX_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "catalogues"
    / "semantic-compatibility-v1.json"
)
SUMMARY_PATH = MATRIX_PATH.parent / "17100007-1.0.65-extraction-summary.json"


def _matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def test_matrix_covers_every_known_model_and_unknown_standard() -> None:
    models = _matrix()["models"]

    assert {row["sn8"] for row in models} == {
        None,
        "171000AU",
        "171H120F",
        "17100003",
        "17100007",
    }
    assert next(row for row in models if row["sn8"] is None)["local_profile"] == "STANDARD"


def test_each_row_records_bundle_catalogue_probe_and_evidence_status() -> None:
    for row in _matrix()["models"]:
        assert row["regions"]
        assert set(row["bundle"]) == {"status", "version", "vendor_category"}
        assert set(row["parser_probe"]) == {"encode", "decode"}
        assert row["catalogue"]["status"]
        assert set(row["parser_endpoint"]) == {"status", "base_url", "encode_path", "decode_path"}
        assert row["evidence"]
        assert "semantic_operations" in row
        assert "type_selectors" in row
        assert "constraints" in row


def test_matrix_does_not_claim_cross_model_c3_equivalence_or_expose_private_data() -> None:
    text = MATRIX_PATH.read_text(encoding="utf-8")

    assert "A shared protocol must not be inferred from device type 0xC3." in text
    assert "accessToken" not in text
    assert "packageUrl" not in text
    assert "X-Amz-" not in text
    assert "commandHex" not in text


def test_unavailable_bundles_cannot_claim_parser_success() -> None:
    for row in _matrix()["models"]:
        if row["bundle"]["status"] == "unavailable":
            assert row["parser_probe"] == {"encode": "unavailable", "decode": "unavailable"}


def test_real_extraction_gap_is_explicit_not_equated_across_models() -> None:
    row = next(row for row in _matrix()["models"] if row["sn8"] == "17100007")

    assert row["catalogue"]["status"] == "extracted_unresolved"
    assert "28 control" in row["catalogue"]["reason"]
    assert row["type_selectors"] == ["none asserted; dynamic params"]


def test_real_extraction_summary_is_sanitized_and_reviewable() -> None:
    summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))

    assert summary["provenance"] == {
        "model_sn8": "17100007",
        "plugin_version": "1.0.65",
        "source_kind": "unpacked_weex_plugin",
    }
    assert summary["operations"] == []
    assert len(summary["unresolved"]) == 28
    assert {item["direction"] for item in summary["unresolved"]} == {"control"}
    assert all(item["provenance"]["source"].startswith("module:") for item in summary["unresolved"])
    text = SUMMARY_PATH.read_text(encoding="utf-8")
    assert "accessToken" not in text
    assert "X-Amz-" not in text
