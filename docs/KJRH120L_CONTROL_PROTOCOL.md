# KJRH-120L control protocol (captured 2026-11-12)

Captured by intercepting the official iLetComfort iOS app (TLS MITM, no cert
pinning) while performing each action once. All control goes through the Dollin
cloud:

```
POST /midea/open/business/v1/appliance/control/hexadecimal
{"applianceCode": "<code>", "command": "<hex>"}
```

## Frame format

- **Query:** `ffff<ss><ss><ss>ff` — read status subtype `<ss>`. The response
  `data` is the `aa …` status frame. The app reads subtypes 01, 02, 04, 05, 06,
  07, 08, 09, 10, 30, 83, 84 when opening the device.
- **Write:** one or more `<grp><field><len><value…>ff` frames **concatenated**
  into a single `command` string. `<grp>` selects the data group / subtype the
  field belongs to, `<field>` is the field id, `<len>` the value length in
  bytes, then the value bytes. All values seen were 1 byte.

Groups seen: `00` = basic state (subtype 0x01), `01` = weekly timer, `02` =
holiday / silent / away date ranges.

## Confirmed controls

| Action | Frame | Notes |
|---|---|---|
| **Run mode** | `00 03 01 <m> ff` | `m`: **heat=3, cool=2** (likely auto=1). App sends it with the matching zone-1 setpoint. |
| **Zone 1 setpoint** | `00 04 01 <°C> ff` | **Confirmed by read-back** (set 51 → app showed 51). This is the field upstream was missing; its wrong guess was 0x08. |
| **DHW setpoint** | `00 07 01 <°C> ff` | Already known upstream. |
| **Weekly timer master on/off** | `00 a8 01 <0/1> ff` | Drives `time_set_state`. |
| **Weekly timer data** | group `01`, `0x36`=days bitmask (0x7f=all 7), `0x37`=timer-1 enable, `0x38`–`0x4a`=per-slot time/temp (all 0 in the empty test). |
| **Holiday/away** | group `02`, `0x26`=enable(1/0), `0x27`–`0x29`=flags, `0x2a`–`0x2c`=start Y/M/D, `0x2d`–`0x2f`=end Y/M/D, `0x30`/`0x31`=setpoints. Captured 2026-11-13→11-20. |
| **Silent (with quiet-hours timer)** | group `02`, `0x3a`=enable, `0x3b`–`0x45`=period start/end times. |
| **Away date-range variant** | group `02`, `0x4e`=enable, `0x4f`–`0x54`=start/end Y/M/D, `0x58`=flag. |

Single-field toggles also seen (exact meaning not labelled during capture):
`00/0x46`, `00/0x4b`, `00/0x82` — each sent as `00 <field> 01 <0/1> ff`.

## Example batched command (set mode heat + zone-1 50)

```
00030103ff00040132ff
```

See `CONTROL_CAPTURE.txt` for the raw timestamped capture.

## Additional captures (2026-11-12, second session)

| Action | Frame | Notes |
|---|---|---|
| **Power** | `00 02 01 <0/1> ff` | The app's "power off" sent `0x02=0`. Upstream already uses this for DHW on/off; on this unit it toggled `remote_onoff`/`dhw_power`. `remote_onoff=off` is the normal idle state (zones + DHW stay "on"). |
| **TBH (tank backup heater) / Fast DHW** | `00 0b 01 <0/1> ff` | Both the "TBH" and "Fast DHW" buttons produced `0x0b`; a controlled cloud toggle of `0x0b` did not flip `fastdhw_state` or `forcetbh_state` (likely because the unit was powered off at the time / session race). Needs one more clean capture to split the two. |
| **Weekly timer master** | `00 a8 01 <0/1> ff` | Drives `time_set_state`. |

### Control reliability note

The vendor cloud allows **one active session per account**. When the iLetComfort
phone app is open and logged in, it repeatedly re-logs in and invalidates any
other session, so cloud control commands sent from Home Assistant (or a script)
in that window return `code 0` but may not reach the appliance. Reliable control
needs HA to be the sole session (close the phone app, or use a separate
iLetComfort account for HA). This is the same single-session constraint the
upstream integration documents for status.
