"""Behavior tests for the development-only semantic API extractor."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "extract_semantic_api.py"
    spec = importlib.util.spec_from_file_location("extract_semantic_api", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["extract_semantic_api"] = module
    spec.loader.exec_module(module)
    return module


extractor = _load_script()
FIXTURE = Path(__file__).parent / "fixtures" / "semantic_plugin"
MAIN_PROVENANCE = {"component": "module_58417e0f781b", "source": "module:58417e0f781b"}


def _main_at(line: int) -> dict[str, object]:
    return {**MAIN_PROVENANCE, "line": line}


def test_extracts_operations_constraints_merge_and_endpoints() -> None:
    catalogue = extractor.extract_catalogue(
        FIXTURE, model="TEST0001", plugin_version="2024032801"
    )

    assert catalogue["catalogue_version"] == 1
    assert catalogue["provenance"] == {
        "model_sn8": "TEST0001",
        "plugin_version": "2024032801",
        "source_kind": "unpacked_weex_plugin",
    }
    assert catalogue["operations"] == [
        {
            "direction": "control",
            "selector": {"name": "control_type", "value": "base"},
            "parameters": [
                    {
                        "name": "dhw_power_state",
                    },
                    {
                        "name": "dhw_temp_set",
                    },
            ],
            "state_merge": {
                "strategy": "object_assign",
                "sources": ["last.base"],
                "semantic_values_override_state": True,
            },
            "provenance": [
                _main_at(3),
                _main_at(4),
            ],
        },
        {
            "direction": "query",
            "selector": {"name": "query_type", "value": "base"},
            "parameters": [
                {
                    "name": "dhw_power_state",
                },
                {
                    "name": "dhw_temp_set",
                },
            ],
            "provenance": [
                _main_at(5)
            ],
        },
    ]
    assert catalogue["field_facts"] == [
        {
            "field": "dhw_power_state",
            "literal_values": ["off", "on"],
            "provenance": _main_at(7),
            "scope": "unlinked_literal_declaration",
        },
        {
            "field": "dhw_temp_set",
            "numeric_constraints": {"max": 65, "min": 20, "step": 1},
            "provenance": _main_at(6),
            "scope": "unlinked_literal_declaration",
        },
    ]
    assert catalogue["parser_endpoints"] == [
        {
            "direction": "decode",
            "path": "/v1/lua/decode",
            "provenance": _main_at(2),
        },
        {
            "direction": "encode",
            "path": "https://parser.example.invalid/v1/lua/encode",
            "provenance": _main_at(2),
        },
    ]


def test_dynamic_and_missing_expressions_are_explicitly_unresolved() -> None:
    catalogue = extractor.extract_catalogue(
        FIXTURE, model="TEST0001", plugin_version="1"
    )

    findings = {(item["kind"], item["field"]) for item in catalogue["unresolved"]}
    assert ("dynamic_selector", "control_type") in findings
    assert ("dynamic_parameter", "<computed>") in findings
    assert ("missing_params", "params") in findings
    assert all("value" not in item for item in catalogue["unresolved"])


def test_identical_inputs_produce_byte_identical_json() -> None:
    first = extractor.catalogue_json(
        extractor.extract_catalogue(FIXTURE, model="TEST0001", plugin_version="1")
    )
    second = extractor.catalogue_json(
        extractor.extract_catalogue(FIXTURE, model="TEST0001", plugin_version="1")
    )

    assert first == second
    assert first.endswith("\n")


def test_cli_accepts_entry_file_and_writes_catalogue(tmp_path: Path) -> None:
    output = tmp_path / "catalogue.json"

    result = extractor.main(
        [
            str(FIXTURE / "main.js"),
            "--model",
            "TEST0001",
            "--plugin-version",
            "1",
            "--output",
            str(output),
        ]
    )

    assert result == 0
    parsed = json.loads(output.read_text(encoding="utf-8"))
    assert parsed["provenance"]["model_sn8"] == "TEST0001"


def test_output_never_copies_sensitive_literals(tmp_path: Path) -> None:
    source = tmp_path / "secret.js"
    source.write_text(
        'const accessToken="SECRET";const packageUrl="https://x.invalid/a?X-Amz-Signature=SECRET";'
        'bridge.luaControl({params:{control_type:"base",dhw_temp_set:42}});',
        encoding="utf-8",
    )

    rendered = extractor.catalogue_json(
        extractor.extract_catalogue(source, model="TEST0001", plugin_version="1")
    )

    assert "SECRET" not in rendered
    assert "X-Amz-Signature" not in rendered
    assert "packageUrl" not in rendered


def test_rejects_a_full_device_serial_instead_of_persisting_it() -> None:
    try:
        extractor.extract_catalogue(
            FIXTURE, model="171000AU123456789", plugin_version="1"
        )
    except ValueError as err:
        assert "8-character sn8" in str(err)
    else:
        raise AssertionError("full device serial was accepted as model provenance")


def test_state_merge_reports_real_precedence(tmp_path: Path) -> None:
    source = tmp_path / "merge.js"
    source.write_text(
        'bridge.luaControl({params:Object.assign({},'
        '{control_type:"base",dhw_temp_set:42},last.base)});',
        encoding="utf-8",
    )

    catalogue = extractor.extract_catalogue(source, model="TEST0001", plugin_version="1")

    assert catalogue["operations"][0]["state_merge"] == {
        "strategy": "object_assign",
        "sources": ["last.base"],
        "semantic_values_override_state": False,
    }


def test_ambiguous_merge_is_unresolved_and_has_no_precedence_claim(tmp_path: Path) -> None:
    source = tmp_path / "merge.js"
    source.write_text(
        'bridge.luaControl({params:Object.assign({},'
        '{control_type:"base",dhw_temp_set:42},getState())});',
        encoding="utf-8",
    )

    catalogue = extractor.extract_catalogue(source, model="TEST0001", plugin_version="1")

    assert "state_merge" not in catalogue["operations"][0]
    assert catalogue["unresolved"][0]["kind"] == "dynamic_state_merge"


def test_endpoint_output_strips_all_url_credentials(tmp_path: Path) -> None:
    source = tmp_path / "endpoint.js"
    source.write_text(
        'const parser={encodeUrl:"https://user:pass@parser.example.invalid/encode?sig=SECRET#private"};',
        encoding="utf-8",
    )

    rendered = extractor.catalogue_json(
        extractor.extract_catalogue(source, model="TEST0001", plugin_version="1")
    )

    assert "SECRET" not in rendered
    assert "user" not in rendered
    assert "pass" not in rendered
    assert "sig=" not in rendered
    assert "https://parser.example.invalid/encode" in rendered
