# KJRH-120L status decode

Layout of the status frames of a KJRH-120L (`sn8 17100003`), matched against the vendor's decoded
`fullQuery` values. Evidence: an app capture session on 2026-11-12, and two full cloud snapshots of the
same unit, taken on 2026-11-12 and 2026-11-18. Anything resting on two samples is marked as a
candidate.

To reproduce, query each subtype with `ffff<ss><ss><ss>ff`, read `fullQuery` in the same moment, and
compare.

Frame: `aa,<len>,c3,00*6,<msgtype>,<sub>,fe,<body...>,<chk>`. Indexes below use `body = raw[10:-1]`
(what `extract_c3_body` returns), so `body[0]` is the subtype byte and `body[k]` is the k-th byte from
there.

## Subtype 0x01, basic status (write group `00`)

For write group `00`, field N lands at `body[8+N]`. The frames are in `KJRH120L_CONTROL_PROTOCOL.md`.

| body[k] | A | B | meaning | notes |
|---|---|---|---|---|
| 8 | 1 | 0 | Zone 1 power state | write field `0x00` |
| 9 | 1 | 0 | Zone 2 power state | write field `0x01` is acked and changes nothing |
| 10 | 1 | 1 | DHW power state | write field `0x02` |
| 11 | 3 | 3 | run mode | heat is 3, cool is 2. Write field `0x03` |
| 12 | 50 | 37 | Zone 1 setpoint, degrees C | write field `0x04` |
| 13 | 30 | 30 | Zone 2 setpoint, degrees C | field `0x05`, not exercised |
| 14 | 48 | 48 | room setpoint | byte / 2 gives degrees C, 24 in both. Field `0x06`, not exercised |
| 15 | 40 | 35 | DHW setpoint, degrees C | write field `0x07` |
| 30 | 1 | 0 | `time_set_state` | weekly timer master |
| 37 | 75 | 75 | `zone1_heat_max_set_temp` | limit |
| 38 | 35 | 35 | `zone1_heat_min_set_temp` | limit |
| 39 | 25 | 25 | `zone1_cool_max_set_temp` | limit |
| 40 | 5 | 5 | `zone1_cool_min_set_temp` | limit |
| 41 | 55 | 55 | `zone2_heat_max_set_temp` | limit |
| 47 | 70 | 70 | `dhw_max_set_temp` | limit |
| 48 | 20 | 20 | `dhw_min_set_temp` | limit |
| 53 | 6 | 6 | `zone1_curve_type` | weather curve index |
| 54 | 3 | 3 | `zone2_curve_type` | weather curve index |
| 84 to 88 | 26,11,12,13,53 | 26,11,18,8,57 | clock | year (20xx), month, day, hour, minute |

A is 2026-11-12 13:53 and B is 2026-11-18 08:57. The limits in rows 37 to 48 were identical in both
snapshots and equal `fullQuery`. The values at `body[42]` to `body[46]` (25, 25, 5, 60, 34) are
consistent with the remaining `fullQuery` limits (`zone2_heat_min` 25, `zone2_cool_max` 25,
`zone2_cool_min` 5, `room_max` 30 and `room_min` 17, both doubled), but that mapping rests on the values
only.

### Zone power and the dual-variant gate

`body[8]` and `body[9]` are the Zone 1 and Zone 2 power state, not a fixed hardware marker. On this
unit both dropped from 1 to 0 when Zone 1 was switched off, and `body[30]` dropped with the weekly
timer. `kjrh120l_has_zone1` reads `body[8:10] == 01 01` as the dual-variant marker, so this unit reads
as pure DHW whenever Zone 1 is off. The pure-DHW frames from issue #5 in the tests have
`body[8:10] == 00 00`, `body[11] == 03`, `body[12] == 0x41` and the same limit bytes. Whether that
hardware lacks a zone or had it switched off is not known.

### Tank and outdoor temperature

- **`body[57]` = `t4` + 196 (confirmed across four independent units):** Snapshot A (`0xd0` = 208 →
  12 °C), Snapshot B (`0xcc` = 204 → 8 °C), #142 Inventor MX290-10S (`0xce` = 206 → 10 °C, and
  live-verified on v0.22.0 at `0xca` = 202 → 6 °C matching the app), and #136 Mundoclima MUAMR-H14
  (`0xce` = 206 → 10 °C), plus diurnal warming in #119 (`0xcd` → 9 °C to `0xd9` → 21 °C). Decoded
  into `t4_temp` (Outdoor Ambient Temperature) by the integration.
- **`body[49]` = `tank_actual_temp` + 196 (candidate only):** matches Snapshots A/B (238 → 42 °C,
  240 → 44 °C), but issue #5's frame carries `0x13` (19) at `body[49]` and #142 stayed at `0xdd`
  across captures, so it is not decoded yet.

The older note that the tank temperature sits at `body[233]` of subtype 0x10 is retracted. That byte
was 78 in both snapshots while the tank went from 42 to 44.

## Real subtype 0x01 bodies

Both are full `body` values from the same unit. They contain no serial number. Values from the vendor
`fullQuery` taken at the same moment sit under each one.

Snapshot A, 2026-11-12 13:53:

```
01,fe,00,00,00,41,00,55,01,01,01,03,32,1e,30,28,00,00,00,00,00,00,01,01,01,00,00,00,00,00,01,00,00,00,01,02,02,4b,23,19,05,37,19,19,05,3c,22,46,14,ee,00,01,01,06,03,01,01,d0,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,30,ff,ff,ff,ff,00,00,00,01,00,00,00,00,00,1a,0b,0c,0d,35,00,00,00,00,ff
```

`zone1_power_state` on, `zone2_power_state` on, `dhw_power_state` on, `run_mode_set` heat,
`zone1_temp_set` 50, `zone2_temp_set` 30, `room_temp_set` 24, `dhw_temp_set` 40, `time_set_state` on,
`tank_actual_temp` 42, `t4` 12. Limits: zone1 heat 75/35 (max/min), zone1 cool 25/5, zone2 heat
55/25, dhw 70/20.

Snapshot B, 2026-11-18 08:57:

```
01,fe,00,00,00,41,00,55,00,00,01,03,25,1e,30,23,00,00,00,00,00,00,01,01,01,00,00,00,00,00,00,00,00,00,01,02,02,4b,23,19,05,37,19,19,05,3c,22,46,14,f0,00,01,01,06,03,01,01,cc,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,30,ff,ff,ff,ff,00,00,00,01,00,00,00,00,00,1a,0b,12,08,39,00,00,00,00,ff
```

`zone1_power_state` off, `zone2_power_state` off, `dhw_power_state` on, `run_mode_set` heat,
`zone1_temp_set` 37, `dhw_temp_set` 35, `time_set_state` off, `tank_actual_temp` 44, `t4` 8. All
limits unchanged from A. The Zone 2 setpoint (30) and room setpoint (24) in B are read from the body
bytes, not from `fullQuery`.

Bytes that differ between A and B: `body[8]`, `body[9]`, `body[12]`, `body[15]`, `body[30]`,
`body[49]`, `body[57]` and the clock bytes `body[86]` to `body[88]`.

Other boolean state flags sit around `body[21]`, `body[31]`, `body[34]` and `body[35]`. They did not
change between A and B. In #142 (Inventor MX290-10S), enabling Silent Level 2 flipped both `body[21]`
and `body[31]` from `0x00` to `0x01` (with `body[53]` changing `0x06 → 0x09`), making them candidates
for silent/super-silent state; they are not decoded yet.

## Other subtypes

| subtype | bytes | role |
|---|---|---|
| 0x02 | 99 | secondary sensors (water pressure and flow, versions, run hours), mostly 0 on this unit |
| 0x04 | 21 | zone and curve runtime block |
| 0x05 | 21 | short runtime block |
| 0x06 | 109 | holiday and away date block, where holiday writes appear to land |
| 0x07 | 16 | DHW and curve config, static |
| 0x08 | 154 | full config dump (limits, curves, timers), static |
| 0x09 | 16 | short status, static |
| 0x0a | 202 | extended config |
| 0x10 | 247 | live telemetry with many varying sensor bytes. The tank mapping from the older note is retracted, see above. The frame embeds the unit's serial number, so none is published here |
| 0x30 | 24 | flags block, static |
| 0x83 | 27 | weekly-timer summary |
| 0x84 | 399 | weekly-timer full grid |

The roles of subtypes 0x02 to 0x0a, 0x30, 0x83 and 0x84 come from their sizes and which bytes varied
during the 2026-11-12 capture. No subtype other than 0x01 was verified against `fullQuery` field by
field.

## Vendor `fullQuery` catalogue (2026-11-12, snapshot A)

The cloud's own decode, as returned for the 2026-11-12 snapshot.

Setpoints: `dhw_max_set_temp` 70, `dhw_min_set_temp` 20, `dhw_temp_set` 40, `room_max_set_temp` 30,
`room_min_set_temp` 17, `room_temp_set` 24, `zone1_cool_max_set_temp` 25, `zone1_cool_min_set_temp`
5, `zone1_heat_max_set_temp` 75, `zone1_heat_min_set_temp` 35, `zone1_temp_set` 50,
`zone2_cool_max_set_temp` 25, `zone2_cool_min_set_temp` 5, `zone2_heat_max_set_temp` 55,
`zone2_heat_min_set_temp` 25, `zone2_temp_set` 30.

Modes and enables: `boostertbh_en` on, `cool_enable` on, `dhw_enable` on, `dhw_power_state` on,
`double_zone_enable` off, `eco_on_state` off, `fastdhw_state` off, `forcetbh_state` off,
`heat_enable` on, `holiday_on_state` off, `protocol_newfunction_en` on, `remote_onoff` off,
`room_thermalen_state` off, `room_thermalmode_state` off, `run_mode_set` heat, `runmode_under_auto`
heat, `silence_on_state` off, `silence_set_state` off, `time_set_state` on, `zone1_cool_curve_state`
off, `zone1_curve_state` off, `zone1_heat_curve_state` off, `zone1_power_state` on,
`zone2_cool_curve_state` off, `zone2_curve_state` off, `zone2_heat_curve_state` off,
`zone2_power_state` on.

Curves and terminals: `zone1_curve_type` 6, `zone1_temp_type` water_temperature_type,
`zone1_terminal_type` radiatior (sic), `zone2_curve_type` 3, `zone2_temp_type`
water_temperature_type, `zone2_terminal_type` floor_heat.

Clock and misc: `config_code` 1, `day` 12, `error_code` 0, `error_code_str` /, `error_new_code` 0,
`hour` 13, `minute` 53, `month` 11, `t4` 12, `tank_actual_temp` 42, `version` 0.0.167, `year` 26.

Other: `HMIEnergyAnaSetEN` on, `SysEnergyAnaEN` on, `backupSensorOverAccuracy` 0,
`backupSensorRemainTime` 0, `backupSensorState` 0, `holiday_on_type` 0.

Note that `double_zone_enable` is off on a unit whose `zone2_power_state` is on and which has Zone 2
limits and setpoints. What `double_zone_enable` means for this hardware is not known.
