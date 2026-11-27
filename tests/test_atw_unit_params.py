"""Tests for ATW (sn8 171H120F) subtype 0x10 live telemetry (issue #120)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import (
    ApiError,
    ILetComfortClient,
    build_c3_query,
    decode_its_sensors,
)
from custom_components.iletcomfort.climate import ILetComfortClimate
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DOMAIN,
    REGION_EU,
)
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator
from custom_components.iletcomfort.protocols import get_adapter
from custom_components.iletcomfort.protocols.atw import (
    apply_atw_hmi_params,
    apply_atw_unit_params,
    decode_atw_status,
)
from custom_components.iletcomfort.protocols.kjrh120l import decode_kjrh120l_status
from custom_components.iletcomfort.protocols.profile import ATW_SN8, KJRH120L_SN8, ModelProfile
from tests.test_dialect_profiles import (
    ATW_PLACEHOLDER_SENSORS_BODY,
    NORDIS_ATW_STATUS_FRAMES,
)
from tests.snapshot_helpers import make_state


def _bytes(csv: str) -> bytes:
    return bytes(int(x, 16) for x in csv.split(","))


# Sanitized first 96 bytes of real subtype 0x10 response from NØRDIS Optimus
# Pro (sn8 171H120F, issue #120), cross-checked against 22 service-menu values.
NORDIS_UNIT_PARAMS_0X10 = _bytes(
    "10,00,03,00,02,00,00,0e,0e,19,18,18,7f,00,00,00,00,00,00,ed,01,e0,08,01,"
    "00,00,00,00,04,00,07,40,00,68,18,19,18,18,23,19,7f,7f,1e,04,c4,08,34,13,"
    "02,00,00,20,15,1c,ff,00,3d,00,00,00,00,00,00,00,00,00,3d,00,00,19,3f,00,"
    "00,5e,f8,00,00,19,3f,00,00,5e,e9,00,05,00,00,00,00,4b,70,00,00,00,10,41"
)

# Real subtype 0x0A HMI parameters prefix from NØRDIS Optimus Pro (issue #120):
# body[4..5] = 0x1c, 0x8f → 7311 compressor total run hours.
NORDIS_HMI_PARAMS_0X0A = _bytes(
    "0a,38,00,22,1c,8f,1c,47,3f,e1,00,94,00,20,00,00"
)


def _c3_frame(body: bytes) -> str:
    header = bytes([0xAA, 0x00, 0xC3, 0, 0, 0, 0, 0, 0, 0x03])
    return (header + body + b"\x00").hex()


def test_apply_atw_unit_params_decodes_verified_issue120_fields():
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    sensors = get_adapter(ModelProfile.ATW).decode_sensors(
        decode_its_sensors(ATW_PLACEHOLDER_SENSORS_BODY),
        status,
    )

    out_status, out_sensors = apply_atw_unit_params(
        NORDIS_UNIT_PARAMS_0X10, status, sensors
    )

    assert out_status.comp_frq == 0
    assert out_status.comp_running is False
    assert out_status.exv_drg == 480
    assert out_status.pressure_h == 1220
    assert out_status.total_kwh == 6463

    assert out_sensors.t3_temp == 14.0
    assert out_sensors.t4_temp == 14.0
    assert out_sensors.tp_temp == 25.0
    assert out_sensors.twin_temp == 24.0
    assert out_sensors.twout_temp == 24.0
    assert out_sensors.odu_voltage == 237
    assert out_sensors.t1_temp == 24.0
    assert out_sensors.t2_temp == 24.0
    assert out_sensors.t2b_temp == 24.0
    assert out_sensors.dc_current == 0
    assert out_sensors.dc_voltage == 320
    assert out_sensors.tf_temp == 21.0
    assert out_sensors.water_flow == 61
    # Existing status-derived DHW tank temperature is preserved.
    assert out_sensors.th_temp == 46.0
    assert out_sensors.raw_body == NORDIS_UNIT_PARAMS_0X10


def test_apply_atw_unit_params_signed_negative_disconnected_and_running():
    body = bytearray(NORDIS_UNIT_PARAMS_0X10)
    body[1] = 42  # compressor running at 42 Hz
    body[8] = 0xFB  # -5 °C (signed int8)
    body[7] = 0x7F  # disconnected sentinel
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    sensors = get_adapter(ModelProfile.ATW).decode_sensors(
        decode_its_sensors(ATW_PLACEHOLDER_SENSORS_BODY),
        status,
    )

    out_status, out_sensors = apply_atw_unit_params(bytes(body), status, sensors)

    assert out_status.comp_frq == 42
    assert out_status.comp_running is True
    assert out_sensors.t4_temp == -5.0
    assert out_sensors.t3_temp is None


def test_apply_atw_unit_params_ignores_short_or_wrong_subtype():
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    sensors = get_adapter(ModelProfile.ATW).decode_sensors(
        decode_its_sensors(ATW_PLACEHOLDER_SENSORS_BODY),
        status,
    )

    for bad in (b"", NORDIS_UNIT_PARAMS_0X10[:20], b"\x02" + NORDIS_UNIT_PARAMS_0X10[1:]):
        out_status, out_sensors = apply_atw_unit_params(bad, status, sensors)
        assert out_status is status
        assert out_sensors is sensors


def test_client_query_atw_unit_params_sends_0x10_and_validates():
    client = ILetComfortClient(api_base="https://eu.dollin.net")
    with patch.object(
        client, "send_hex_command", return_value=_c3_frame(NORDIS_UNIT_PARAMS_0X10)
    ) as mock_send:
        body = client.query_atw_unit_params("APPL1")

    mock_send.assert_called_once_with("APPL1", build_c3_query(0x10))
    assert body == NORDIS_UNIT_PARAMS_0X10


def test_client_query_atw_unit_params_rejects_short_reply():
    client = ILetComfortClient(api_base="https://eu.dollin.net")
    with (
        patch.object(client, "send_hex_command", return_value=_c3_frame(b"\x10\x00")),
        pytest.raises(ApiError, match="0x10"),
    ):
        client.query_atw_unit_params("APPL1")


def test_climate_uses_live_water_outlet_when_0x10_telemetry_present():
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    sensors = get_adapter(ModelProfile.ATW).decode_sensors(
        decode_its_sensors(ATW_PLACEHOLDER_SENSORS_BODY),
        status,
    )
    status, sensors = apply_atw_unit_params(NORDIS_UNIT_PARAMS_0X10, status, sensors)

    coord = MagicMock()
    coord.appliance_code = "APPL1"
    coord.sn8 = ATW_SN8
    coord.data = make_state(
        status,
        sensors,
        {
            0x01: status.raw_body,
            0x02: ATW_PLACEHOLDER_SENSORS_BODY,
            0x10: NORDIS_UNIT_PARAMS_0X10,
        },
        sn8=coord.sn8,
    )
    climate = ILetComfortClimate(coord)

    assert climate.current_temperature == 24.0


def _atw_coordinator(hass: HomeAssistant) -> tuple[ILetComfortCoordinator, MagicMock]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ATW",
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_EU,
        },
        version=2,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient") as cls:
        coord = ILetComfortCoordinator(hass, entry)
    coord.appliance_meta = {"applianceCode": "APPL1", "sn8": ATW_SN8}
    client = cls.return_value
    client.query_status.return_value = decode_atw_status(
        NORDIS_ATW_STATUS_FRAMES["issue132"]
    )
    client.query_sensors.return_value = decode_its_sensors(
        ATW_PLACEHOLDER_SENSORS_BODY
    )
    return coord, client


async def test_coordinator_polls_0x10_on_atw_placeholder_and_populates_sensors(
    hass: HomeAssistant,
):
    coord, client = _atw_coordinator(hass)
    client.query_atw_unit_params.return_value = NORDIS_UNIT_PARAMS_0X10

    with patch("custom_components.iletcomfort.coordinator.asyncio.sleep", new=AsyncMock()):
        data = await coord._poll()

    client.query_atw_unit_params.assert_called_once_with("APPL1")
    assert coord._atw_unit_params_supported is True
    assert data.sensors.t4_temp == 14.0
    assert data.sensors.twout_temp == 24.0
    assert data.status.total_kwh == 6463


async def test_coordinator_disables_0x10_after_1214_and_keeps_placeholder_safe(
    hass: HomeAssistant,
):
    """Rotenso-like 171H120F units that reject 0x10 with 1214 only probe once."""
    coord, client = _atw_coordinator(hass)
    client.query_atw_unit_params.side_effect = ApiError("Send command failed: code=1214")

    with patch("custom_components.iletcomfort.coordinator.asyncio.sleep", new=AsyncMock()):
        data1 = await coord._poll()
        data2 = await coord._poll()

    assert client.query_atw_unit_params.call_count == 1
    assert client.query_atw_hmi_params.call_count == 0
    assert coord._atw_unit_params_supported is False
    assert data1.sensors.t4_temp is None
    assert data1.sensors.th_temp == 46.0
    assert data2.sensors.t4_temp is None


def test_apply_atw_hmi_params_decodes_verified_issue120_run_hours():
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    out_status = apply_atw_hmi_params(NORDIS_HMI_PARAMS_0X0A, status)
    assert out_status.comp_total_run_hours == 7311


def test_apply_atw_hmi_params_ignores_short_or_wrong_subtype():
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    for bad in (b"", NORDIS_HMI_PARAMS_0X0A[:5], b"\x10" + NORDIS_HMI_PARAMS_0X0A[1:]):
        assert apply_atw_hmi_params(bad, status) is status


def test_client_query_atw_hmi_params_sends_0x0a_and_validates():
    client = ILetComfortClient(api_base="https://eu.dollin.net")
    with patch.object(
        client, "send_hex_command", return_value=_c3_frame(NORDIS_HMI_PARAMS_0X0A)
    ) as mock_send:
        body = client.query_atw_hmi_params("APPL1")

    mock_send.assert_called_once_with("APPL1", build_c3_query(0x0A))
    assert body == NORDIS_HMI_PARAMS_0X0A


def test_client_query_atw_hmi_params_rejects_short_reply():
    client = ILetComfortClient(api_base="https://eu.dollin.net")
    with (
        patch.object(client, "send_hex_command", return_value=_c3_frame(b"\x0a\x00")),
        pytest.raises(ApiError, match="0x0A"),
    ):
        client.query_atw_hmi_params("APPL1")


async def test_coordinator_polls_0x0a_after_0x10_and_populates_run_hours(
    hass: HomeAssistant,
):
    coord, client = _atw_coordinator(hass)
    client.query_atw_unit_params.return_value = NORDIS_UNIT_PARAMS_0X10
    client.query_atw_hmi_params.return_value = NORDIS_HMI_PARAMS_0X0A

    with patch("custom_components.iletcomfort.coordinator.asyncio.sleep", new=AsyncMock()):
        data = await coord._poll()

    client.query_atw_hmi_params.assert_called_once_with("APPL1")
    assert coord._atw_hmi_params_supported is True
    assert data.status.comp_total_run_hours == 7311
    assert data.status.total_kwh == 6463


async def test_coordinator_disables_0x0a_after_api_error_while_keeping_0x10(
    hass: HomeAssistant,
):
    coord, client = _atw_coordinator(hass)
    client.query_atw_unit_params.return_value = NORDIS_UNIT_PARAMS_0X10
    client.query_atw_hmi_params.side_effect = ApiError("Send command failed: code=1214")

    with patch("custom_components.iletcomfort.coordinator.asyncio.sleep", new=AsyncMock()):
        data1 = await coord._poll()
        data2 = await coord._poll()

    assert client.query_atw_unit_params.call_count == 2
    assert client.query_atw_hmi_params.call_count == 1
    assert coord._atw_unit_params_supported is True
    assert coord._atw_hmi_params_supported is False
    assert data1.status.comp_total_run_hours is None
    assert data2.status.total_kwh == 6463


def test_extended_0x10_sensor_descriptions_disabled_by_default_and_scaled():
    """Verified 0x10 sensors expose scaled values without enabling on all models (#169)."""
    from custom_components.iletcomfort.sensor import SENSOR_DESCRIPTIONS

    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    sensors = get_adapter(ModelProfile.ATW).decode_sensors(
        decode_its_sensors(ATW_PLACEHOLDER_SENSORS_BODY),
        status,
    )
    status, sensors = apply_atw_unit_params(NORDIS_UNIT_PARAMS_0X10, status, sensors)
    data = make_state(
        status,
        sensors,
        {
            0x01: status.raw_body,
            0x02: ATW_PLACEHOLDER_SENSORS_BODY,
            0x10: NORDIS_UNIT_PARAMS_0X10,
        },
        sn8=ATW_SN8,
    )

    by_key = {desc.key: desc for desc in SENSOR_DESCRIPTIONS}
    for key, expected in (
        ("water_flow", 0.61),
        ("dc_voltage", 320),
        ("dc_current", 0),
        ("exv_steps", 480),
    ):
        desc = by_key[key]
        assert desc.entity_registry_enabled_default is False
        assert desc.value_fn(data) == expected


def test_extended_0x10_sensors_return_none_without_0x10_frame():
    """Non-0x10 models (AQUAPURA, KJRH-120L, STANDARD, ATW placeholder) must return None."""
    from custom_components.iletcomfort.api import decode_its_status
    from custom_components.iletcomfort.sensor import SENSOR_DESCRIPTIONS
    from tests.test_dialect_profiles import (
        ISSUE_11_SENSORS_BODY,
        ISSUE_11_STATUS_BODY,
        KJRH120L_SENSORS_BODY,
        KJRH120L_STATUS_BODY,
    )


    by_key = {desc.key: desc for desc in SENSOR_DESCRIPTIONS}
    cases = [
        (
            None,
            decode_its_status(ISSUE_11_STATUS_BODY),
            decode_its_sensors(ISSUE_11_SENSORS_BODY),
        ),
        (
            KJRH120L_SN8,
            decode_kjrh120l_status(KJRH120L_STATUS_BODY),
            decode_its_sensors(KJRH120L_SENSORS_BODY),
        ),
        (
            ATW_SN8,
            decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"]),
            decode_its_sensors(ATW_PLACEHOLDER_SENSORS_BODY),
        ),
    ]
    for sn8, st, sens in cases:
        data = make_state(st, sens, {0x01: st.raw_body, 0x02: sens.raw_body}, sn8=sn8)
        for key in ("water_flow", "dc_voltage", "dc_current", "exv_steps", "total_thermal_energy"):
            assert by_key[key].value_fn(data) is None

    # Even if cached sensors.raw_body starts with 0x10 (degraded sensors +
    # transient 0x10 failure), a raw_frames dict without 0x10 is authoritative.
    st = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    sens = decode_its_sensors(ATW_PLACEHOLDER_SENSORS_BODY)
    _, cached_0x10_sens = apply_atw_unit_params(NORDIS_UNIT_PARAMS_0X10, st, sens)
    degraded_data = make_state(
        st,
        cached_0x10_sens,
        {0x01: st.raw_body, 0x02: ATW_PLACEHOLDER_SENSORS_BODY},
    )
    for key in ("water_flow", "dc_voltage", "dc_current", "exv_steps", "total_thermal_energy"):
        assert by_key[key].value_fn(degraded_data) is None


async def test_coordinator_sensors_cache_fallback_preserves_0x02_and_keeps_0x10_active(
    hass: HomeAssistant,
):
    """A transient query_sensors failure after 0x10 must not overwrite raw_frames[0x02]."""
    coord, client = _atw_coordinator(hass)
    client.query_atw_unit_params.return_value = NORDIS_UNIT_PARAMS_0X10
    client.query_atw_hmi_params.return_value = NORDIS_HMI_PARAMS_0X0A

    with patch("custom_components.iletcomfort.coordinator.asyncio.sleep", new=AsyncMock()):
        coord.data = await coord._poll()
        client.query_sensors.side_effect = ApiError("transient sensors timeout")
        data2 = await coord._poll()

    assert data2.raw_frames[0x02].startswith(b"\x02\x00\x03\x1e")
    assert 0x10 in data2.raw_frames
    assert 0x0A in data2.raw_frames
    assert client.query_atw_unit_params.call_count == 2


async def test_coordinator_transient_api_error_after_0x10_supported_retries_next_poll(
    hass: HomeAssistant,
):
    """Once 0x10 is proven supported, a transient ApiError must not disable it."""
    coord, client = _atw_coordinator(hass)
    client.query_atw_unit_params.side_effect = [
        NORDIS_UNIT_PARAMS_0X10,
        ApiError("transient code=1214"),
        NORDIS_UNIT_PARAMS_0X10,
    ]
    client.query_atw_hmi_params.return_value = NORDIS_HMI_PARAMS_0X0A

    with patch("custom_components.iletcomfort.coordinator.asyncio.sleep", new=AsyncMock()):
        await coord._poll()
        await coord._poll()
        data3 = await coord._poll()

    assert coord.capabilities.atw_unit_params.supported is True
    assert client.query_atw_unit_params.call_count == 3
    assert data3.status.total_kwh == 6463


def test_atw_status_without_0x10_leaves_cumulative_counters_none():
    """ATW 0x01 never carries energy, run hours, or pressures; never emit a fake 0 (#118/#134)."""
    st = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    assert st.total_kwh is None
    assert st.total_thermal_kwh is None
    assert st.comp_total_run_hours is None
    assert st.pressure_h is None
    assert st.pressure_l is None


def test_galmet_issue118_0x10_decodes_total_thermal_energy_and_sensor():
    """Galmet Prima 8GT (#118) 200-byte 0x10 verified against RS-485 Sinum."""
    galmet_0x10 = bytes.fromhex(
        "10 1d 03 35 02 01 00 fe 00 22 1a 1e 7f 00 00 00 00 02 00 ec 00 60 08 "
        "01 00 00 00 00 04 00 87 50 20 68 1d 19 1c 1e 2c 19 7f 7f 1e 07 62 08 "
        "34 fe 02 1e 03 25 0e 1d ff 00 57 00 01 90 00 00 00 00 00 00 35 00 00 "
        "05 ed 00 00 1e 4f 00 00 05 ec 00 00 1e 4f 00 4e 01 aa"
    )
    st = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    sens = decode_its_sensors(ATW_PLACEHOLDER_SENSORS_BODY)
    out_st, out_sens = apply_atw_unit_params(galmet_0x10, st, sens)

    assert out_st.comp_frq == 29
    assert out_st.comp_running is True
    assert out_st.total_kwh == 1517
    assert out_st.total_thermal_kwh == 7759
    assert out_sens.t4_temp == 0.0
    assert out_sens.twin_temp == 26.0
    assert out_sens.twout_temp == 30.0
    assert out_sens.t1_temp == 29.0
    assert out_sens.water_flow == 87

    from custom_components.iletcomfort.sensor import SENSOR_DESCRIPTIONS

    by_key = {desc.key: desc for desc in SENSOR_DESCRIPTIONS}
    thermal_desc = by_key["total_thermal_energy"]
    assert thermal_desc.entity_registry_enabled_default is False
    data_with_0x10 = make_state(out_st, out_sens, {0x10: galmet_0x10})
    assert thermal_desc.value_fn(data_with_0x10) == 7759
    assert thermal_desc.value_fn(make_state(out_st, out_sens, {})) is None


async def test_coordinator_transient_0x10_failure_uses_cached_body_without_zero_reset(
    hass: HomeAssistant,
):
    """A transient 0x10 error/short reply must reuse cached 0x10 instead of dropping to 0/None (#118)."""
    coord, client = _atw_coordinator(hass)
    client.query_atw_unit_params.side_effect = [
        NORDIS_UNIT_PARAMS_0X10,
        ApiError("transient cloud timeout"),
        b"\x10\x00",  # truncated reply
    ]
    client.query_atw_hmi_params.side_effect = [
        NORDIS_HMI_PARAMS_0X0A,
        ApiError("transient hmi error"),
        NORDIS_HMI_PARAMS_0X0A,
    ]

    with patch("custom_components.iletcomfort.coordinator.asyncio.sleep", new=AsyncMock()):
        data1 = await coord._poll()
        data2 = await coord._poll()
        data3 = await coord._poll()

    for data in (data1, data2, data3):
        assert data.status.total_kwh == 6463
        assert data.status.comp_total_run_hours == 7311
        assert data.sensors.twin_temp == 24.0
        assert data.sensors.t4_temp == 14.0
        assert 0x10 in data.raw_frames
        assert 0x0A in data.raw_frames
