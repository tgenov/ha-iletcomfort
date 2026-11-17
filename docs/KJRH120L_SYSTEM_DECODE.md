# KJRH-120L — full system decode

Automatically derived by correlating 47 raw status-frame snapshots (captured from the app + cloud queries) against the vendor's own decoded `fullQuery` values. No manual activity required to reproduce: query each subtype with `ffff<ss><ss><ss>ff` and read `fullQuery`, then match values.

Frame: `aa,<len>,c3,00*6,<msgtype>,<sub>,fe,<body…>,<chk>`. Below, `body[0]` = the subtype byte; `body[k]` is the k-th byte from there.

## Subtype 0x01 — basic status (write target group `00`)

| body[k] | now | varies | field | notes |
|---|---|---|---|---|
| body[11] | 3 | ✓ | **run_mode** | heat=3, cool=2 — written via SET field 0x03 |
| body[12] | 50 | ✓ | **zone1_temp_set** | °C — SET field 0x04 |
| body[13] | 30 | ✓ | **zone2_temp_set** | °C |
| body[14] | 48 |  | **room_temp_set** | value = byte/2 (°C, 0.5° res) |
| body[15] | 40 | ✓ | **dhw_temp_set** | °C — SET field 0x07 |
| body[37] | 75 |  | **zone1_heat_max_set_temp** | °C limit |
| body[38] | 35 |  | **zone1_heat_min_set_temp** | °C limit |
| body[39] | 25 |  | **zone1_cool_max_set_temp / zone2_heat_min** | °C limit |
| body[40] | 5 |  | **zone1_cool_min_set_temp** | °C limit |
| body[41] | 55 |  | **zone2_heat_max_set_temp** | °C limit |
| body[47] | 70 |  | **dhw_max_set_temp** | °C limit |
| body[48] | 20 |  | **dhw_min_set_temp** | °C limit |
| body[53] | 6 | ✓ | **zone1_curve_type** | weather-curve index |
| body[54] | 3 | ✓ | **zone2_curve_type** | weather-curve index |
| body[84] | 26 |  | **year** | RTC (20xx) |
| body[85] | 11 |  | **month** | RTC |
| body[86] | 12 |  | **day** | RTC |
| body[87] | 13 |  | **hour** | RTC |
| body[88] | 53 | ✓ | **minute** | RTC |

Additional **boolean state flags** at body[21, 30, 31, 34, 35] (change with silent/holiday/timer/eco/power toggles; these carry `remote_onoff`, `time_set_state`, `silence_*`, `*_power_state`, enables). Exact bit per flag needs a single on/off diff each.

Clock is body[84..88] = Y,M,D,h,m (now 26,11,12,13,53).

## Other subtypes (role)

| subtype | bytes | role |
|---|---|---|
| 0x02 | 99 | secondary sensors (water pres/flow, versions, run-hours) — mostly 0 on this unit |
| 0x04 | 21 | zone/curve runtime block (varying: body[8,12-17]) |
| 0x05 | 21 | short runtime block (varying body[8,10]) |
| 0x06 | 109 | holiday/away date block — holiday writes land here (varying body[8-14,18]) |
| 0x07 | 16 | DHW/curve config (static) |
| 0x08 | 154 | full config dump (limits, curves, timers — 154 B, static) |
| 0x09 | 16 | short status (static) |
| 0x0a | 202 | extended config (202 B) |
| 0x10 | 247 | live telemetry — tank temp at body[233]=val+35, + many varying sensor bytes |
| 0x30 | 24 | flags block (static) |
| 0x83 | 27 | weekly-timer summary (27 B) |
| 0x84 | 399 | weekly-timer full grid (399 B = 7 days × slots) |

## Complete state catalog (vendor `fullQuery`, 67 fields)

This is the cloud's own full decode — every controllable/observable value. `sensor.*` in HA already exposes the useful ones.

**Setpoints:** `dhw_max_set_temp`=70, `dhw_min_set_temp`=20, `dhw_temp_set`=40, `room_max_set_temp`=30, `room_min_set_temp`=17, `room_temp_set`=24, `zone1_cool_max_set_temp`=25, `zone1_cool_min_set_temp`=5, `zone1_heat_max_set_temp`=75, `zone1_heat_min_set_temp`=35, `zone1_temp_set`=50, `zone2_cool_max_set_temp`=25, `zone2_cool_min_set_temp`=5, `zone2_heat_max_set_temp`=55, `zone2_heat_min_set_temp`=25, `zone2_temp_set`=30

**Modes/enables:** `boostertbh_en`=on, `cool_enable`=on, `dhw_enable`=on, `dhw_power_state`=on, `double_zone_enable`=off, `eco_on_state`=off, `fastdhw_state`=off, `forcetbh_state`=off, `heat_enable`=on, `holiday_on_state`=off, `protocol_newfunction_en`=on, `remote_onoff`=off, `room_thermalen_state`=off, `room_thermalmode_state`=off, `run_mode_set`=heat, `runmode_under_auto`=heat, `silence_on_state`=off, `silence_set_state`=off, `time_set_state`=on, `zone1_cool_curve_state`=off, `zone1_curve_state`=off, `zone1_heat_curve_state`=off, `zone1_power_state`=on, `zone2_cool_curve_state`=off, `zone2_curve_state`=off, `zone2_heat_curve_state`=off, `zone2_power_state`=on

**Curves/terminals:** `zone1_curve_type`=6, `zone1_temp_type`=water_temperature_type, `zone1_terminal_type`=radiatior, `zone2_curve_type`=3, `zone2_temp_type`=water_temperature_type, `zone2_terminal_type`=floor_heat

**Clock/misc:** `config_code`=1, `day`=12, `error_code`=0, `error_code_str`=/, `error_new_code`=0, `hour`=13, `minute`=53, `month`=11, `t4`=12, `tank_actual_temp`=42, `version`=0.0.167, `year`=26

**Other:** `HMIEnergyAnaSetEN`=on, `SysEnergyAnaEN`=on, `backupSensorOverAccuracy`=0, `backupSensorRemainTime`=0, `backupSensorState`=0, `holiday_on_type`=0
