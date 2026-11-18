# KJRH-120L control protocol

Captured on 2026-11-12 and 2026-11-14 by intercepting the official iLetComfort iOS app (TLS
interception, no certificate pinning) while performing each action once. All control goes through the
Dollin cloud:

```
POST /midea/open/business/v1/appliance/control/hexadecimal
{"applianceCode": "<code>", "command": "<hex>"}
```

This page documents the protocol only. The integration does not send any of these frames yet.

## Evidence levels

- Confirmed means the app sent the frame, the cloud acked it with code 0, and the new value showed up in
  the status frame and in the vendor `fullQuery` afterwards.
- Captured, not validated means the frame was recorded from the app and acked, but no read-back
  showed the effect. Do not build on these without a controlled test.

## Frame format

- Query: `ffff<ss><ss><ss>ff` reads status subtype `<ss>`. The response `data` is the `aa ...` status
  frame. The app reads subtypes 01, 02, 04, 05, 06, 07, 08, 09, 10, 30, 83 and 84 when it opens the
  device.
- Write: one or more `<grp><field><len><value...>ff` frames concatenated into a single `command`
  string. `<grp>` selects the data group the field belongs to, `<field>` is the field id, `<len>` the
  value length in bytes, then the value bytes. All values seen were 1 byte.

Groups seen: `00` is basic state (status subtype 0x01), `01` is the weekly timer, `02` is holiday,
silent and away date ranges.

For group `00`, field N lands at status `body[8+N]`, with `body = raw[10:-1]` and `body[0]` the subtype
byte (see `KJRH120L_SYSTEM_DECODE.md`). This holds for fields `0x00` to `0x07`. It was not checked for
higher field ids.

## Confirmed by read-back

| Action | Frame | Status byte | Notes |
|---|---|---|---|
| Zone 1 power | `00 00 01 <0/1> ff` | `body[8]` | Captured 2026-11-14. `zone1_power_state` and `zone2_power_state` follow, DHW is untouched. |
| DHW power | `00 02 01 <0/1> ff` | `body[10]` | The app's DHW on/off button. |
| Run mode | `00 03 01 <m> ff` | `body[11]` | `m`: heat is 3, cool is 2. Auto is not captured. |
| Zone 1 setpoint | `00 04 01 <C> ff` | `body[12]` | Degrees C. Set 51, 63 and 50, each read back. Upstream tried field `0x08` here and it always forced 23 C. |
| DHW setpoint | `00 07 01 <C> ff` | `body[15]` | Set 39, read back. |

The app always sends the mode together with the Zone 1 setpoint. Both frames go in one command string:

```
00030103ff00040132ff   mode heat, Zone 1 setpoint 50
00030102ff00040132ff   mode cool, Zone 1 setpoint 50
```

Field `0x01` is acked but changes nothing: `00010100ff`. It is the Zone 2 power byte (`body[9]`), but
the write has no effect on this unit. `zone2_power_state` follows Zone 1 power instead.

## Captured, not validated by read-back

| Action | Frame | Notes |
|---|---|---|
| Fast DHW and TBH (tank backup heater) | `00 0b 01 <0/1> ff` | Both the "TBH" and "Fast DHW" buttons sent `0x0b`. A controlled cloud toggle of `0x0b` did not change `fastdhw_state` or `forcetbh_state`. The unit may have been powered off at the time, or the session may have raced the phone app. Unresolved. This frame is not in `KJRH120L_CONTROL_CAPTURE.txt`. |
| Weekly timer master on/off | `00 a8 01 <0/1> ff` | The vendor `time_set_state` followed it in the 2026-11-12 session. |
| Weekly timer data | group `01`: `0x36` day bitmask (`0x7f` is all 7 days), `0x37` timer 1 enable, `0x38` to `0x4a` per-slot time and temperature | All slot values were 0 in the empty test. Slot layout is unknown. |
| Holiday | group `02`: `0x26` enable (1/0), `0x27` to `0x29` flags, `0x2a` to `0x2c` start Y/M/D, `0x2d` to `0x2f` end Y/M/D, `0x30` and `0x31` setpoints | The captured range ran from 2026-11-13 to 2026-11-20. Fields `0x27` to `0x29` are unknown (the app sent 1 for all three). Both setpoints were 25, and what they set is unknown. |
| Silent with quiet hours | group `02`: `0x3a` enable, `0x3b` to `0x45` period start and end times | Layout of the time bytes not worked out. |
| Away date range | group `02`: `0x4e` enable, `0x4f` to `0x54` start and end Y/M/D, `0x58` flag | |

Single-field toggles also seen, never labelled during the capture: `00/0x46`, `00/0x4b` and `00/0x82`.
Each was sent as `00 <field> 01 <0/1> ff`.

## Raw capture

`KJRH120L_CONTROL_CAPTURE.txt` holds the hex commands and timings of the 2026-11-12 session, plus the
Zone 1 power and DHW power frames from 2026-11-14. It contains no account, token, appliance code or
serial number.

## Control reliability

The vendor cloud allows one active session per account. While the iLetComfort phone app is open and
logged in, it re-logs in repeatedly and invalidates other sessions. Commands sent from Home Assistant
or a script in that window return code 0 but may not reach the appliance. For reliable control, make
Home Assistant the only session: close the phone app, or give Home Assistant a separate iLetComfort
account. The upstream integration documents the same constraint for status reads.
