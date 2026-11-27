"""sn8 model code from the cloud field or the unit serial (#260)."""

from __future__ import annotations

import logging

import pytest

from custom_components.iletcomfort.protocols import appliance_sn8, sn8_from_serial
from tests.test_entity_capabilities import (  # noqa: F401 - fixture
    ROTENSO,
    _entry,
    _setup,
    mock_client,
)

# Synthetic serial in the Midea layout: <0000><C3><3 chars><sn8><rest>.
SYNTHETIC_SERIAL = "0000C3310171H120F000000000TESTXYZ"


def test_sn8_from_serial_reads_characters_9_to_16() -> None:
    assert sn8_from_serial(SYNTHETIC_SERIAL) == "171H120F"
    assert sn8_from_serial(" " + SYNTHETIC_SERIAL + " ") == "171H120F"


@pytest.mark.parametrize(
    "serial",
    [
        None,
        42,
        "",
        "0000C3310171H120",  # too short for sn[9:17]
        "0000AC310171H120F000000000TESTXYZ",  # not a C3 device type
        "0000C3310171H-20F000000000TESTXYZ",  # not alphanumeric
        "0000C3310171h120f000000000TESTXYZ",  # lower-case code
        "a1b2c37f9e0d4c5b6a7f8e9d0c1b2a3f",  # lower-case hex (encrypted form)
    ],
)
def test_sn8_from_serial_rejects_other_values(serial) -> None:
    assert sn8_from_serial(serial) is None


def test_appliance_sn8_prefers_the_cloud_field() -> None:
    assert appliance_sn8({"sn8": "171000AU", "sn": SYNTHETIC_SERIAL}) == (
        "171000AU",
        "cloud",
    )


@pytest.mark.parametrize("field", [None, ""])
def test_appliance_sn8_falls_back_to_the_serial(field) -> None:
    meta = {"sn": SYNTHETIC_SERIAL}
    if field is not None:
        meta["sn8"] = field
    assert appliance_sn8(meta) == ("171H120F", "serial")


@pytest.mark.parametrize("meta", [None, {}, {"sn8": "", "sn": "**REDACTED**"}])
def test_appliance_sn8_unknown(meta) -> None:
    assert appliance_sn8(meta) == (None, "unknown")


async def test_coordinator_uses_serial_sn8_and_logs_selection_once(
    hass, mock_client, caplog  # noqa: F811
):
    entry = _entry(hass)
    with caplog.at_level(logging.INFO):
        await _setup(hass, mock_client, entry, "171H120F", ROTENSO)
        coordinator = hass.data["iletcomfort"][entry.entry_id]
        # Same unit, but the cloud now sends an empty sn8 field.
        meta = {"applianceCode": "APPL1", "sn8": "", "sn": SYNTHETIC_SERIAL}
        mock_client.list_appliances.return_value = [meta]
        coordinator.appliance_meta = dict(meta)
        assert coordinator.sn8 == "171H120F"
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert caplog.text.count("sn8=171H120F (from cloud); using the atw protocol dialect") == 1
    assert caplog.text.count("sn8=171H120F (from serial); using the atw protocol dialect") == 1
    assert SYNTHETIC_SERIAL not in caplog.text
    assert "APPL1" not in caplog.text


async def test_coordinator_logs_unknown_model_code(hass, mock_client, caplog):  # noqa: F811
    entry = _entry(hass)
    with caplog.at_level(logging.INFO):
        await _setup(hass, mock_client, entry, None, ROTENSO)
        coordinator = hass.data["iletcomfort"][entry.entry_id]
        coordinator.appliance_meta = {"applianceCode": "APPL1"}
        await coordinator.async_refresh()

    assert "model code (sn8) unknown; using the standard protocol dialect" in caplog.text


async def test_setup_with_empty_cloud_sn8_selects_dialect_from_serial(
    hass, mock_client, caplog  # noqa: F811
):
    """Real path: list_appliances returns an empty sn8 and a serial."""
    from custom_components.iletcomfort.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = _entry(hass)
    with caplog.at_level(logging.INFO):
        mock_client.list_appliances.return_value = [
            {"applianceCode": "APPL1", "sn8": "", "sn": SYNTHETIC_SERIAL}
        ]
        from tests.test_entity_capabilities import _status_for

        mock_client.query_status.return_value = _status_for("171H120F", ROTENSO)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = hass.data["iletcomfort"][entry.entry_id]
        calls_after_setup = mock_client.list_appliances.call_count
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.sn8 == "171H120F"
    assert coordinator.sn8_source == "serial"
    # A derived code keeps the metadata lookup running, so a cloud value
    # that arrives later still wins.
    assert mock_client.list_appliances.call_count > calls_after_setup
    assert "sn8=171H120F (from serial); using the atw protocol dialect" in caplog.text
    assert SYNTHETIC_SERIAL not in caplog.text

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["protocol"]["resolved_profile"] == "atw"
    assert diag["protocol"]["resolved_sn8"] == "171H120F"
    assert diag["protocol"]["sn8_source"] == "serial"
    assert SYNTHETIC_SERIAL not in str(diag)


def test_cloud_sn8_with_spaces_is_kept_as_before() -> None:
    """The cloud field is used as sent (no new stripping behaviour)."""
    assert appliance_sn8({"sn8": "  ", "sn": SYNTHETIC_SERIAL}) == ("  ", "cloud")
