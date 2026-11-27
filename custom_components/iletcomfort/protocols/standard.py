"""STANDARD protocol dialect (default C3 layout, #110)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..api import (
    MODE_COOL,
    MODE_HEAT,
    MODE_MAP,
    MODE_OFF,
    MODE_WATERPUMP,
    QUERY_TO_SET_MODE,
    TEMP_RANGES,
    ITSSensors,
    ITSStatus,
    decode_its_status,
)
from ..c3_basic import C3BasicStatus
from ..c3_frame import build_c3_query, build_c3_set
from ..cloud import ApiError
from ..commands import (
    HeatPumpCommand,
    SetBoostCommand,
    SetDhwPowerCommand,
    SetDhwTargetCommand,
    SetEcoModeCommand,
    SetFastDhwCommand,
    SetHvacModeCommand,
    SetPowerCommand,
    SetSilentModeCommand,
    SetTargetTemperatureCommand,
    SetZoneCurveCommand,
    SetZonePowerCommand,
    SetZoneRunModeCommand,
    SetZoneTargetCommand,
)
from .kjrh120l import zone1_run_mode_setpoint
from .probes import OptionalProbe
from .profile import ModelProfile
from .views import (
    LEGACY_BOOST_MUTE,
    NO_ZONE2_VIEW,
    ClimateView,
    EntityCapabilities,
    ViewContext,
    legacy_climate_view,
)
from .write import (
    C3_CONTROL_DISABLED,
    KJRH120L_ONLY_ZONE1,
    FreshRead,
    WriteContext,
    WritePlan,
    WriteRefused,
)

# HA HVAC-mode intent → legacy ITS SET mode byte.
LEGACY_HVAC_TO_SET_MODE: dict[str, int] = {
    "off": MODE_OFF,
    "heat": MODE_HEAT,
    "cool": MODE_COOL,
    "fan_only": MODE_WATERPUMP,
}


def default_climate_temp_bounds(set_mode: int | None) -> tuple[float, float]:
    """Return default ``(min_temp, max_temp)`` for a SET ``set_mode``."""
    if set_mode is not None and set_mode in TEMP_RANGES:
        return float(TEMP_RANGES[set_mode][0]), float(TEMP_RANGES[set_mode][1])
    return 10.0, 40.0


def encode_legacy_its_set(
    status_body: bytes,
    *,
    mode: int | None = None,
    temperature: int | None = None,
    boost: bool | None = None,
    mute: int | None = None,
    power_on: bool = False,
    last_on_state: tuple[int, int] | None = None,
) -> str:
    """Encode the legacy 62-byte ITS SET from a fresh status body (pure).

    Echo bytes come from ``status_body``; the requested change is merged in,
    the temperature range is validated, and ``build_c3_set`` builds the frame.
    Raises ``ApiError`` for an unrecognized status layout or an Auto mode that
    cannot be preserved, and ``ValueError`` for an out-of-range temperature.
    """
    status = decode_its_status(bytearray(status_body))

    # An unknown mode can mean this model uses a different status layout.
    # Even an explicit mode override cannot make its echoed bytes safe.
    if status.mode not in (*QUERY_TO_SET_MODE, 3):
        raise ApiError(
            f"Unrecognized status mode {status.mode}; refusing to send a "
            "control command using an unvalidated status layout"
        )
    current_set_mode = QUERY_TO_SET_MODE.get(status.mode)

    # Handle power_on: restore last on-state
    if power_on and last_on_state is not None:
        if mode is None:
            mode = last_on_state[0]
        if temperature is None:
            temperature = last_on_state[1]

    eff_mode = mode if mode is not None else current_set_mode
    if eff_mode is None:
        raise ApiError(
            "Cannot preserve Auto mode in a control command; "
            "select an explicit operating mode first"
        )
    # Use active mode setpoint (t5s_def, offset-decoded) not DHW target (set_temperature)
    if temperature is not None:
        eff_temp = temperature
    elif status.t5s_def is not None:
        eff_temp = int(status.t5s_def)
    else:
        eff_temp = status.set_temperature
    temp_explicitly_set = temperature is not None

    eff_ctrl_flag = 0x00
    eff_mute_level = 0x00
    if boost is True:
        eff_ctrl_flag = 0x02
    elif mute is not None and mute != 0:
        eff_ctrl_flag = 0x01
        eff_mute_level = 0x00 if mute == 1 else 0x01

    if temp_explicitly_set and eff_mode in TEMP_RANGES and eff_mode != MODE_OFF:
        temp_min, temp_max = TEMP_RANGES[eff_mode]
        if not (temp_min <= eff_temp <= temp_max):
            mode_name = {v: k for k, v in MODE_MAP.items()}.get(eff_mode, "unknown")
            raise ValueError(
                f"Temperature {eff_temp}C out of range for {mode_name} mode "
                f"(allowed: {temp_min}-{temp_max}C)"
            )

    return build_c3_set(
        mode=eff_mode,
        temperature=eff_temp,
        status_body=bytearray(status_body),
        mute_level=eff_mute_level,
        ctrl_flag=eff_ctrl_flag,
    )


def _legacy_plan(ctx: WriteContext, **change: Any) -> WritePlan:
    """Plan a legacy ITS SET: fresh 0x01 read (echo bytes) → ``encode_legacy_its_set``."""
    last_on_state = ctx.last_on_state

    def _encode(body: bytes) -> str:
        return encode_legacy_its_set(body, last_on_state=last_on_state, **change)

    return WritePlan.read_modify_write(
        # The legacy path never validated the reply's subtype echo; keep that.
        FreshRead(0x01, build_c3_query(0x01), require_echo=False),
        _encode,
        description=f"legacy_its {change}",
    )


def plan_legacy_its_write(command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
    """Plan ``command`` for dialects that use the legacy 62-byte ITS SET.

    Shared by STANDARD and AQUAPURA. C3-basic-only intents are refused with the
    same message as an ATW unit without the opt-in; Zone 1 writes are
    KJRH-120L-only.
    """
    if isinstance(command, SetPowerCommand):
        if command.on:
            return _legacy_plan(ctx, power_on=True)
        return _legacy_plan(ctx, mode=MODE_OFF)
    if isinstance(command, SetHvacModeCommand):
        return _legacy_plan(ctx, mode=LEGACY_HVAC_TO_SET_MODE[command.mode])
    if isinstance(command, SetTargetTemperatureCommand):
        # Clamp to the bounds the climate entity shows for the current mode.
        temp = legacy_climate_view(ctx.status, default_climate_temp_bounds).clamp(
            command.temperature
        )
        return _legacy_plan(ctx, temperature=int(temp))
    if isinstance(command, SetBoostCommand):
        return _legacy_plan(ctx, boost=command.enabled)
    if isinstance(command, SetSilentModeCommand) and command.level is not None:
        return _legacy_plan(ctx, mute=command.level)
    if isinstance(command, SetDhwPowerCommand):
        return _legacy_plan(ctx, mode=MODE_HEAT if command.on else MODE_OFF)
    if isinstance(command, SetDhwTargetCommand):
        return _legacy_plan(ctx, temperature=int(command.temperature))
    if isinstance(command, (SetZonePowerCommand, SetZoneTargetCommand)):
        if command.zone == 1:
            raise ApiError(KJRH120L_ONLY_ZONE1)
        raise WriteRefused(C3_CONTROL_DISABLED)
    if isinstance(command, SetZoneRunModeCommand):
        zone1_run_mode_setpoint(ctx, command.mode)
        raise ApiError(KJRH120L_ONLY_ZONE1)
    if isinstance(
        command,
        (SetSilentModeCommand, SetEcoModeCommand, SetFastDhwCommand, SetZoneCurveCommand),
    ):
        raise WriteRefused(C3_CONTROL_DISABLED)
    raise WriteRefused(f"Unsupported command intent: {command!r}")


class StandardAdapter:
    """Default dialect adapter for unknown/standard C3 heat pumps."""

    profile = ModelProfile.STANDARD
    periodic_status_push = True
    accepts_energy_notify = False
    offers_c3_basic_control = False
    latches_zone1_capability = False

    def build_query(self, subtype: int) -> str:
        return build_c3_query(subtype)

    def read_status(self, send: Callable[[str], str]) -> ITSStatus | None:
        return None

    def read_sensors(self, send: Callable[[str], str]) -> ITSSensors | None:
        return None

    def decode_status(self, status: ITSStatus) -> ITSStatus:
        return status

    def decode_sensors(
        self, sensors: ITSSensors, status: ITSStatus
    ) -> ITSSensors:
        return sensors

    def sensors_placeholder(self, raw_0x02: bytes) -> bool | None:
        return None

    def optional_probes(
        self, status: ITSStatus, raw_frames: Mapping[int, bytes]
    ) -> tuple[OptionalProbe, ...]:
        return ()

    def apply_overlays(
        self,
        status: ITSStatus,
        sensors: ITSSensors | None,
        raw_frames: Mapping[int, bytes],
        *,
        retained_subtypes: bool,
        energy_notify: bytes | None,
    ) -> tuple[ITSStatus, ITSSensors | None]:
        return status, sensors

    def observed_zone1(self, status: ITSStatus) -> bool:
        return False

    def describe_variant(self, status: ITSStatus | None) -> str:
        return "standard"

    def climate_current_temperature(self, sensors: ITSSensors | None) -> float | None:
        return sensors.twin_temp if sensors is not None else None

    def climate_temp_bounds(
        self, status: ITSStatus | None, set_mode: int | None
    ) -> tuple[float, float]:
        return default_climate_temp_bounds(set_mode)

    def is_kjrh120l_dual(
        self, status: ITSStatus | None, zone1_latched: bool
    ) -> bool:
        return False

    def c3_basic(self, status: ITSStatus | None) -> C3BasicStatus | None:
        return None

    def dhw_target_temp(self, status: ITSStatus) -> float | None:
        # No DHW-target entity reads the legacy layout; not exposed.
        return None

    def entity_capabilities(
        self, status: ITSStatus | None, ctx: ViewContext
    ) -> EntityCapabilities:
        return EntityCapabilities(boost=LEGACY_BOOST_MUTE, mute=LEGACY_BOOST_MUTE)

    def climate_view(self, status: ITSStatus | None, ctx: ViewContext) -> ClimateView:
        return legacy_climate_view(status, default_climate_temp_bounds)

    def zone2_climate_view(
        self, status: ITSStatus | None, ctx: ViewContext
    ) -> ClimateView:
        return NO_ZONE2_VIEW

    def plan_write(self, command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
        return plan_legacy_its_write(command, ctx)
