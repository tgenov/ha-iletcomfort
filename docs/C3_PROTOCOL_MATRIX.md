# 0xC3 Protocol Matrix (`midea-local` vs. Dollin Hardware Corpus)

> **Policy:** [`midea-lan/midea-local`](https://github.com/midea-lan/midea-local) (`midealocal/devices/c3/message.py`) is used strictly as a **candidate protocol reference** — never as a runtime dependency (avoids LAN/MSmartHome coupling and Home Assistant Core dependency clashes). Every field shipped in `ha-iletcomfort` must be confirmed against at least one real Dollin hardware capture (`tests/corpus/`).

Legend:
- **Hardware-Verified (Dollin)** — matched directly against official app / unit service menu on real Dollin captures.
- **Candidate (`midea-local`)** — mapped from `midea-local` / `scripts/c3_probe.py`; requires live Dollin confirmation before exposing in entities or writes.
- **Dollin-Divergent** — Dollin cloud firmware differs from `midea-local` (must use structural/firmware-gated handling).

---

## 1. Subtype Overview (`body[0]`)

| Subtype | Role (`midea-local`) | Status in `ha-iletcomfort` | Dollin Findings & Notes |
|---|---|---|---|
| `0x01` | Basic status (`C3BasicBody`) | **Hardware-Verified (Dollin)** | Splits by `sn8` and frame structure: STANDARD ITS (`+35` offset), AQUAPURA (`box_bottom_temp` / inlet-outlet), KJRH-120L (95-byte custom layout), and `171H120F` (`C3BasicStatus` variants: Italtherm 25B, Rotenso 25B `[2]=0x15`, Galmet/NØRDIS 27B `[3:6]=a0,03,03`, Airwell 27B `[2]=0x73 [3]=0x90`). |
| `0x02` | Sensors / ECO / schedule | **Hardware-Verified (Dollin)** | STANDARD carries live temps (`+35` offset) & ODU V/A. On `171H120F` ATW units (#38, #120, #123, #124, #128, #150), returns 38B/75B schedule placeholders (`atw_sensors_are_placeholder`) — **Dollin-Divergent**. KJRH-120L `0x02` is static/suppressed. |
| `0x04` | Energy notify1 (`C3EnergyBody`) | **Hardware-Verified (MQTT push, Hyundai #129)** | HTTP polling returns a 9-byte stub on NØRDIS (#120) and Galmet (#118); over MQTT push, Hyundai `#129` emits unsolicited `notify1` `0x04` frames (`body[2..5]` `total_kwh`, `body[6..9]` `total_thermal_kwh`, `body[10]` `t4_temp`) handled in `mqtt.py` / `coordinator.py` to record push activity and overlay ATW energy/outdoor telemetry. These frames are change-driven, not periodic, so ATW push availability follows the MQTT link rather than a status heartbeat. |
| `0x05` | Silent / Super-Silent (+ `<= 6B` ECO fallback) | **Hardware-Verified (Rotenso & Airwell #139)** | Rotenso `0x05` Silent query/SET verified in opt-in C3Basic control; Airwell `#139` C8 verified 10B Silent (`bit 0` ON/OFF, `bit 1` Timer 1, `bit 3` Super Silent retained even when OFF; Timer 1/2 schedules in `[2..9]`). Some firmwares share `0x05` with ECO (`<= 6` bytes, `midea-local` #300); supported as fallback. |
| `0x07` | ECO mode + schedule | **Hardware-Verified (Rotenso & Airwell #139)** | Verified on Rotenso Aquami (`07 00 08 00 13 00` → 6-byte SET `07 <on> 08 00 13 00 00`) for opt-in C3Basic ECO control (`build_feature_set`), and Airwell `#139` C8 verified 7B ECO (`bit 0` ON/OFF, `bit 1` Timer enabled; schedules in `[2..6]`). Probed & decoded on `C3Basic` ATW units alongside `0x05`. |
| `0x09` | Disinfect query | **Dollin-Divergent / Rejected** | Returned `code=1214` on NØRDIS (#120). |
| `0x0A` | HMI parameters | **Hardware-Verified (`body[4..5]`)** | `body[4..5]` (`comp_total_run_hours`, uint16 BE) verified on NØRDIS #120 (`0x1c8f` = `7311 Hrs`). `body[3]` (`34 min` current run time) is Candidate. Polled only after `0x10` succeeds (`v0.23.0`). |
| `0x10` | Unit parameters | **Hardware-Verified (Selective)** | Live telemetry verified on NØRDIS (#120) and Airwell Wellea Split (#139); rejected (`code=1214`) on Rotenso (#143) & M-Thermal (#142 `@blahavl`). Gated on ATW `0x02` placeholder + single-probe `ApiError` session disable (`capabilities.py`). |

---

## 2. Subtype `0x01` (`C3BasicStatus`, `171H120F`, `c3_basic.py`)

0-indexed into `body` (`body[0] == 0x01`):

| Index | Field / Bits | Status | Notes |
|---|---|---|---|
| `1` | `0x01` zone1 power/demand, `0x02` zone2, `0x04` DHW, `0x08` curve1, `0x10` curve2, `0x20` TBH, `0x40` fast DHW | **Hardware-Verified (Unified)** | `bit 0` is Zone-1 Power and `body[4]` is Cool/Heat mode uniformly across all valid `C3BasicStatus` frames (`#22`, `#38`, `#120`, `#123`, `#124`, `#125`, `#127`, `#129`, `#134`, `#135`, `#138`, `#139`, `#141`, `#150`, `#151`). |
| `2` | Capabilities (`0x01` heat, `0x02` cool, `0x04` DHW, `0x08` double_zone, `0x10` zone1_water_temp, `0x20` zone2_water_temp) | **Hardware-Verified (Unified)** | `cap_dhw` gates `ILetComfortDhw`; `double_zone` gates `ILetComfortC3Zone2Climate` and `Zone 2 Weather Curve`; `zone1_water_temp` (`body[2] & 0x10`) selects `body[6]` water target vs `body[9]/2` room target (matching `midea-local`). |
| `3` | `0x02` Silent, `0x04` Holiday, `0x08` ECO | **Dollin-Divergent (ECO)** | Airwell #139 confirmed `body[3] & 0x02` toggles for both Silent L1 & L2 (`0x90 → 0x92`), while ECO left `body[3]=0x90` unchanged. |
| `4` | Operating mode (`2` Cool, `3` Heat; `1` Auto unverified) | **Hardware-Verified (`2`/`3`)** | Auto (`1`) intentionally ignored until hardware capture confirms semantics. |
| `6` | `zone1_target` (direct °C) | **Hardware-Verified (Unified water-mode)** | Selected whenever `body[2] & 0x10` (`zone1_water_temp`) is set (25B & 27B: Galmet #38, NØRDIS #120/#132, Kosner #134, Sinclair #150, Hyundai #129, Kaisai #123/#151, Rotenso #143). Single unconfirmed `#124` `merol77a` capture still exposes raw `room_target` in attributes. |
| `7` | `zone2_target` (direct °C) | **Hardware-Verified (Dual-Zone)** | Verified on Kaisai Arctic `#151` (`[2]=0x3f`, `25 °C`); exposed via dynamic `Zone 2` climate entity (`ILetComfortC3Zone2Climate`, gated on `double_zone`). |
| `8` | `dhw_target` (direct °C) | **Hardware-Verified (Dollin)** | Verified on Italtherm #22, Galmet #38, NØRDIS #132, Rotenso #124. |
| `9` | `room_target` (`raw / 2` °C) | **Hardware-Verified (Unified room-mode)** | Selected when `body[2] & 0x10` is clear (`0`: Italtherm #22, Inventor ATM16S #125, Heiwa #42 C14); 0.5 °C resolution. |
| `10..21` | Min/max limits (Zone-1/Zone-2/Room×2/DHW) | **Candidate (`midea-local`)** | Matches vendor schema bounds in `c3_basic.py`. |
| `22` | `dhw_tank_temp` (direct °C) | **Hardware-Verified (Dollin)** | Routed to `th_temp` ("DHW Tank Temperature") only when `cap_dhw`; no-DHW units fill it with a non-reading (`0xf1` #138; `0x19` #47 Dantex, #123, #124 "western", #139, #142), so the tank is unavailable. |
| `23` | `error_code` | **Hardware-Verified (`0`)** | 25B/27B tail `[24..26]` is flags/MSB (`0x80`/`0xe0`/`0x60`/`0x40`), never error code. |

---

## 3. Subtype `0x10` Unit Parameters (`protocols/atw.py`, `apply_atw_unit_params`)

Signed 8-bit °C values use `0x7F` as disconnected sentinel (`_ATW_UNIT_SENSOR_DISCONNECTED`).

| Index | Field | Scale / Encoding | Status | Source / Notes |
|---|---|---|---|---|
| `1` | `comp_frq` | Hz (`comp_running = frq > 0`) | **Hardware-Verified** | NØRDIS #120, Airwell #139 (`31 Hz`) |
| `3` | `fan_speed_rpm` | `raw * 10`, RPM | **Hardware-Verified (probe)** | Galmet #118 (`0x35 → 530 RPM` vs. Sinum `530 RPM`); fixed in `scripts/c3_probe.py` |
| `7` | `t3_temp` (Condenser / outdoor coil) | int8 °C | **Hardware-Verified** | NØRDIS #120, Airwell #139 |
| `8` | `t4_temp` (Outdoor ambient) | int8 °C | **Hardware-Verified** | NØRDIS #120, Airwell #139 (`7 °C`) |
| `9` | `tp_temp` (Plate HX / discharge) | int8 °C | **Hardware-Verified** | NØRDIS #120, Airwell #139 |
| `10` | `twin_temp` (Water inlet) | int8 °C | **Hardware-Verified** | NØRDIS #120, Airwell #139 (`44 °C`) |
| `11` | `twout_temp` (Water outlet) | int8 °C | **Hardware-Verified** | NØRDIS #120, Airwell #139 (`48 °C`) |
| `18..19` | `odu_voltage` | uint16 BE, V | **Hardware-Verified** | NØRDIS #120 (`237 V`), Airwell #139 (`221 V`) |
| `20..21` | `exv_drg` (`exv_steps`) | uint16 BE, steps | **Hardware-Verified** | NØRDIS #120 (`480`); exposed disabled-by-default (#169) |
| `34` | `t1_temp` (Total outlet water) | int8 °C | **Hardware-Verified** | NØRDIS #120 (`24 °C`) |
| `36..37` | `t2_temp`, `t2b_temp` | int8 °C | **Hardware-Verified** | NØRDIS #120 (`24 °C`), Airwell #139 |
| `38` | `th_temp` fallback (`T5` tank) | int8 °C | **Hardware-Verified** | Used when status `th_temp` is `None` |
| `43..44` | `pressure_h` | uint16 BE (kPa) | **Hardware-Verified** | NØRDIS #120 (`1220`), Airwell #139 (`2950`); kept unitless for Recorder compat |
| `50` | `dc_current` | uint8, A | **Hardware-Verified** | NØRDIS #120 (`0 A`); exposed disabled-by-default (#169) |
| `51` | `dc_voltage` | `raw * 10`, V | **Hardware-Verified** | NØRDIS #120 (`32 → 320 V`); exposed disabled-by-default (#169) |
| `52` | `tf_temp` (Module / refrigerant) | int8 °C | **Hardware-Verified** | NØRDIS #120, Airwell #139 |
| `55..56` | `water_flow` | uint16 BE ÷ 100, m³/h | **Hardware-Verified** | NØRDIS #120 (`61 → 0.61 m³/h`); exposed disabled-by-default (#169) |
| `67..70` | `total_kwh` | uint32 BE, kWh | **Hardware-Verified** | NØRDIS #120 (`6463`), Airwell #139 (`6549`), Galmet #118 (`1517` vs. Sinum `1517`) |
| `71..74` | `total_thermal_kwh` | uint32 BE, kWh | **Hardware-Verified** | Galmet #118 (`7759` vs. RS-485 Sinum `7760`); exposed disabled-by-default |
| `45..46` | `pressure_low` | uint16 BE | **Candidate (`midea-local`)** | Left unmapped (`2100` not on #120 display) |
| `75..86` | `instant_power`, etc. | uint32/uint16 BE | **Candidate (`midea-local`)** | Not yet hardware-validated or exposed |

---

## 4. Subtype `0x0A` HMI Parameters & Subtypes `0x05` / `0x07` (Silent / ECO)

- **`0x0A` (`apply_atw_hmi_params`, `protocols/atw.py`):**
  - `body[4..5]` (`comp_total_run_hours`, uint16 BE): **Hardware-Verified (Dollin)** on NØRDIS #120 (`0x1c8f` = `7311 Hrs`) and Airwell #139 (`0x0980` = `2432 Hrs`).
  - `body[3]` (current compressor run time, min): **Candidate (`#120` capture `0x22` = 34 min vs. service menu)**; not decoded in `scripts/c3_probe.py` or exposed in HA yet.
- **`0x05` / `0x07` (`apply_atw_silent`, `apply_atw_eco`, `protocols/atw.py`, #139 / #188):**
  - **Silence (`0x05`, 10B, Hardware-Verified Airwell #139):** `body[1] & 0x01` = Silent ON/OFF, `& 0x02` = Timer 1, `& 0x08` = Super Silent level (retained even when OFF); `body[2..9]` = Timer 1/2 `HH:MM` schedules.
  - **ECO (`0x07`, 7B, Hardware-Verified Airwell #139; or `0x05 <= 6B` fallback per `midea-local` #300):** `body[1] & 0x01` = `eco_on`, `& 0x02` = `eco_timer_on`; `body[2..6]` = schedule bytes.

---

## 5. Write Dialect Matrix (`coordinator.async_execute_command`)

| Family / `sn8` | Command Builder / Dialect | Status | Notes |
|---|---|---|---|
| **STANDARD / AQUAPURA** | 62-byte `build_c3_set` (`c3_frame.py`) | **Hardware-Verified** | Echoes status bytes; rejects unmapped modes cleanly. |
| **KJRH-120L (`17100003`)** | Short `00 <field> 01 <val> ff` (`protocols/kjrh120l.py`) | **Hardware-Verified** | Fields `0x00` (Zone-1 power), `0x02` (DHW power), `0x03` (run mode paired with `0x04`), `0x04` (Zone-1 setpoint), `0x07` (DHW setpoint). Field `0x08` must **never** be used (forced 23 °C on hardware). |
| **ATW (`171H120F`)** | `build_basic_set`, `build_feature_set` (`c3_basic.py`) | **Hardware-Verified (Rotenso Aquami water-mode & Heiwa #42 room-mode, opt-in)** | Opt-in C3Basic SET (`#42`, `#124`, `#139`, `#151`): DHW target/power/Fast DHW (`cap_dhw`-gated), Zone-1 water (`body[6]`) or room 0.5 °C (`body[9]`, verified on Heiwa #42) target/power/curve, Zone-2 water (`body[7]`) target/power/curve (`double_zone`-gated) in Heat mode, Silent (`0x05` Off / Level 1 clearing bit 3 / Level 2 Super Silent), and ECO (`0x07`) preserving schedules; refuses mode changes, Cool writes, and the legacy 62-byte ITS SET. |
