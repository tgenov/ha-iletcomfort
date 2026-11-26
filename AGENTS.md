# AGENTS.md — context & hard-won knowledge for ha-iletcomfort

Guidance for AI agents (and humans) working on this repo. Read this **before** triaging issues or
changing the decoder. It records domain knowledge and, crucially, the **wrong turns we've already
made** so they aren't repeated.

---

## 1. What this integration is (scope)

A Home Assistant integration for **iLetComfort (ITS / BTRI) heat pumps** via the **Midea Dollin cloud**.

**It emulates the iLetComfort app** — it speaks the iLetComfort/Dollin **cloud protocol**, doing what
the app does. The protocol was originally reverse-engineered from the **iLetComfort iOS app** (see
[`tgenov/iletcomfort-cli`](https://github.com/tgenov/iletcomfort-cli)).

**Scope boundary is the cloud/app protocol layer, not the device.** A heat pump may be reachable by
several vendor apps (iLetComfort, SMARTHOME, …); each app uses its **own cloud protocol** even when the
heat pump shares the same underlying device protocol (Midea `0xC3`).

- **In-scope test:** does the device work in the **iLetComfort app**? If yes → in scope.
- **Do NOT add a second cloud/protocol.** We will not implement Midea **MSmartHome** (`*.appsmb.com`)
  support. A device that works *only* via a Midea/MSmartHome app (and not iLetComfort) → out of scope;
  point the user to a Midea integration (`midea_ac_lan`, `midea-local`).

---

## 2. Wrong turns we made — DO NOT REPEAT

These are real mistakes from the project history. Each cost a public retraction.

### 2a. "It's a Midea device, therefore out of scope" — WRONG
A reporter (KJRH-120L, #21/#5) shared a capture from the **SMARTHOME** app hitting `appsmb.com`. It was
concluded the device was "Midea-only / out of scope" and the issues were closed.
**That was false** — the reporter's **screenshots showed the unit working in the iLetComfort app too.**
- **Lesson:** A SMARTHOME / `appsmb.com` capture, or the device also working on Midea, does **not**
  prove out-of-scope. **Verify whether it works in the iLetComfort app first** (re-read the thread /
  screenshots). Only out of scope if it does **not** work via iLetComfort at all.

### 2b. "The app uses certificate pinning, so you need a rooted device" — WRONG
A reporter's Android proxy attempt showed an error and a `14005` response; it was concluded the app pins
its TLS cert and a rooted-Android + Frida bypass was needed.
**That was false:**
- The original protocol RE was done on **iPhone + mitmproxy with NO pinning** in the way.
- The reporter had **actually decrypted traffic** (a `bizGroup:msmart,datakey` request + JSON response
  came through the proxy in the clear). **True pinning shows only TLS handshake failures, never a
  decrypted request/response.**
- The real blocker was the **`14005` single-active-session** response (auth), not pinning.
- **Lesson:** Don't claim pinning unless you see handshake failures with **no** decrypted bodies. If a
  request/response decrypts, the proxy works. The known-good capture setup is **iPhone + mitmproxy**.

### 2c. Don't gate model decoding on a full/per-device serial
The original instinct to gate on the device serial was correctly rejected (targets one device). See §4.
**Lesson:** gate on the `sn8` **model code** (shared by all units of a model), default unknown → STANDARD.

### Meta-lesson
Both 2a and 2b came from **over-reading one new signal instead of reconciling it with facts already in
the thread.** Before asserting a root cause as settled — especially when closing an issue or telling a
user to do hard work — re-read the whole thread and check the new signal against what's already known.

---

## 3. Cloud protocol & error codes

- Auth/login + `list_appliances` happen first; the C3 status/sensor queries follow.
- **`code=1214` ("System error")**: the Dollin cloud **rejects our C3 hex command** for a device
  (raised in `ILetComfortClient.send_hex_command`). It is **not** auth, not offline, not a parsing bug,
  and **not proof of out-of-scope**. It means the command we send isn't what the cloud accepts for that
  model — the app sends something different. Fixing it requires capturing what the iLetComfort app sends.
- **Truncated C3 frame** (e.g. `aa,0b,c3,…,01,2e`): the cloud relays the command but the device returns
  a near-empty stub instead of a full status frame. Same family of "this model doesn't answer our
  standard query" as `1214` (see #5 vs #21).
- **`code=14005`**: token rejected / **single active session**. The cloud allows **one active login per
  account** — logging in from the app invalidates HA's token and vice versa (the "login war"). The
  integration auto-re-auths on `14005`/`12001`. Workaround for users: give HA its own account and share
  the device to it, or select the read-only **phone app coexistence** operation mode. That mode performs
  one account login during setup/restart to mint a session-independent MQTT certificate, then disables
  account polling and writes. Long-running processes rotate before the X.509 expiry (up to seven days
  early), retain
  the old connection and retry hourly after a mint failure, and re-authenticate only after the current
  token is actually rejected. An MQTT outage marks entities unavailable instead of starting polling.
  C3 has no dedicated `hbt` topic, but valid `dev` status frames have been observed from an idle/off
  AQUAPURA roughly every 15 minutes. The integration treats those frames as an end-to-end device-status
  heartbeat and allows up to 45 minutes for missed cycles before marking push stale; MQTT PINGREQ/PINGRESP
  remains transport-level liveness. **That cadence is not universal:** a `171H120F` Hyundai HPSI-Split
  (#129) sent no `0x01` pushes at all and only sporadic `0x04` (`C3EnergyBody`) change notifications,
  so the 45-minute watchdog expired on a healthy link in every test. The heartbeat is therefore gated
  per dialect (`ProtocolAdapter.periodic_status_push`, `False` for ATW). ATW availability follows MQTT
  connect/disconnect only. Unknown `sn8` → STANDARD keeps the heartbeat.
  On ATW units, phone-app mode only sees the optional subtypes from the setup poll; pushes carry 0x01
  and/or 0x04 only, if anything (#225). Cumulative counters (0x10 energy, 0x0A run hours) are kept on
  push, but live 0x10 telemetry (compressor frequency/running, pressures, EXV) is only available from the
  setup poll and goes unknown after the first push. ECO state reflects the setup poll until restart,
  because no push frame carries it (0x01 `body[3]` does not change with ECO, #139). Silent on/off
  follows the pushed 0x01 bit; the level comes from the setup 0x05 only while both agree.
  This is documented in `README.md` / `docs/TROUBLESHOOTING.md`. `14xxx` codes are the auth range.

---

## 4. Decoding, model variants & `sn8` profiles

Heat-pump models pack the status frame (C3 subtype `0x01`) at **different byte offsets**. The cloud
appliance metadata has **NO device-class field**: both an ATW and an ATA unit report
`applianceType="0xC3"` and `modelNumber="0"`. The **only** differentiator is **`sn8`** — the 8-char
model-code serial prefix (e.g. `171H120F` vs `171000AU`). However, #11 and #12 both report
`sn8=171000AU` with different frame semantics, so `sn8` is not proven to uniquely identify a layout.
The full per-device serial `sn` is **redacted** and never stored.

**Model-specific decoding is gated on an `sn8 → profile` table** (`custom_components/iletcomfort/model_profiles.py`):
- `resolve_profile(sn8)` → `ModelProfile.{STANDARD,ATW,AQUAPURA}`. **Unknown/missing sn8 → STANDARD
  (unchanged today's behavior).** So a new model can never be *corrupted* — worst case it doesn't get a
  profile yet. Caveat: `sn8` is assumed model-level (shared across units of a model); only ever confirmed
  on one unit per model so far. Do not select or extend a profile from `sn8` alone without corroborating
  frame evidence.
- `17100007` (`KJRH-120L2`) is a known C3 catalogue model code with no hardware reports or validated
  decode profile yet. It is intentionally absent from `_SN8_PROFILES` and defaults to STANDARD.

Key decode constants (`api.py`): base offset `d=1` (body[0] is the subtype byte);
`_temp_offset(raw) = raw - TEMP_OFFSET` with `TEMP_OFFSET=35`, returning `None` for
`SENSOR_DISCONNECTED=204`; `STATUS_MIN_BODY_LEN=6`.

### ATW profile (`sn8 171H120F`, Italtherm air-to-water, #22) — hardware-validated
Status `raw_body` (0-indexed; STANDARD misreads this 25-byte frame):
- `byte[8]`  = DHW setpoint, **direct °C** (`ATW_DHW_SETPOINT_INDEX`).
- `byte[9]`  = Zone-1 setpoint **×2** (0.5° resolution) → divide by 2.
- `byte[22]` = DHW tank current temp, **direct °C** (`ATW_DHW_TANK_TEMP_INDEX`).
- `byte[1]`  = flags (`ATW_FLAGS_INDEX`); bit `0x01` = space-heat demand (`ATW_SPACE_HEAT_DEMAND_BIT`).
- `byte[24]` = constant `0x80` flags/MSB byte — **NOT an error code** (STANDARD wrongly read it as 128).
- ATW sets `error_code=0` and `comp_running=None` (no confirmed running signal in any 0x01 layout; only 0x10 sets it).
- `decode_atw_status(body)` implements this. Uncertain HVAC mode/action is left conservative — validate
  on hardware before adding.
- **Sensor-template variant (#38, Galmet Prima 8GT):** a 75-byte `0x02` response containing only the
  subtype, 50 zeroes, and twelve `03,1e` pairs is a cloud placeholder, not live telemetry. The app can
  show live Zone-1 water/outdoor values that do not occur in that frame. Suppress template-derived
  temperatures and ODU current/voltage as unavailable, while retaining the status-derived Zone-1/DHW
  setpoints and DHW tank temperature. Gate on the frame structure as well as the ATW profile;
  never suppress or remap all `171H120F` sensor frames from `sn8` alone. The template also appears as
  38 bytes with constant `0x28` and no separator (Rotenso #124, Kaisai #123) and, on NØRDIS (#132),
  in Galmet's 75-byte shape with non-zero group payloads that read like `hh,00,hh,00` time slots — the
  app's live water/outdoor values appear nowhere in it. `atw_sensors_are_placeholder` therefore matches
  the structure: `02,00`, then only `03,<c>,x,x,x,x` groups sharing one `c` (≥ 6 groups, at most a
  single `00` between groups), plus the 38-byte active-schedule form (#128 Airwell Wellea M, #150
  Sinclair MSH-100IB-3/9) with a 6-bit enable mask in `body[1]` and six `[03|05, temp, sh, sm, eh, em]`
  slots carrying valid `HH:MM` clock bytes and uniform `03,<c>` idle slots.
- **27-byte status variant (#38 Galmet Prima 8GT, #132 NØRDIS Optimus Pro):** the same `sn8` also
  emits a 27-byte status layout. Galmet ends `e0,03,03`; two NØRDIS units end `60,04,03` and `40,05,00`
  and differ at `body[2]` (`0x15`/`0x17`). The shared signature is length 27 with
  `body[3:6] == a0,03,03`; every captured 25-byte frame has `0x90`/`0x50`/`0x68` at `body[3]`. Both
  NØRDIS captures match the app (Zone-1 Heating, target 33/35 °C at `body[6]`, DHW 50/60 °C, tank
  46/57–58 °C). In two same-moment Galmet captures, `body[6]` tracked the Zone-1 target directly (25/28 °C),
  while the Italtherm `body[9] / 2` field stayed at a false 24 °C. Gate this mapping on the status
  frame's structural signature, not `sn8` alone. When paired with the sensor template, climate current
  temperature is unavailable; do not relabel the separately exposed DHW tank temperature as Zone-1
  water temperature. Three controlled captures establish `body[1] & 0x01` as Zone-1 power for this
  layout: it is set (`0x05`) in both active-heating and compressor-stopped states, and clear (`0x04`)
  when Zone-1 is off. Decode that as Heat/Off only for this structural variant. The active and stopped
  frames were otherwise identical, so compressor running state and frequency are unavailable—never
  infer them from this bit or report false/0.

### `171H120F` frames match the standard Midea C3 basic body (`c3_basic.py`)
- Every `171H120F` status capture so far (Italtherm #22, Galmet #38, Rotenso Aquami #124 and a second
  Rotenso) is laid out like midea-local's `C3BasicBody` (`data_offset=1`): `[1]` power/flags (zone1
  `0x01`, zone2 `0x02`, DHW `0x04`, zone1 curve `0x08`, TBH `0x20`, fast DHW `0x40`), `[2]`
  capabilities (`0x10` = zone1 water-temperature control), `[3]` silent `0x02` / ECO `0x08`, `[4]`
  mode (3 heat, 2 cool), `[6]` zone1 water target, `[7]` zone2, `[8]` DHW target, `[9]` room target
  ×2, `[10..21]` limits, `[22]` tank, `[23]` error. `decode_c3_basic` decodes the raw fields into
  `ITSStatus.c3_basic`; `model_profiles.atw_c3_basic(sn8, status)` returns it only for the ATW profile.
- **Unified `C3BasicStatus` read decode (`171H120F`):** Cross-checking `midea-local`
  (`MideaC3Device.process_message`) against 10+ Dollin hardware captures (`#22`, `#38`, `#120`, `#125`,
  `#129`, `#132`, `#134`, `#143`, `#150`, `#151`) confirms all `C3BasicStatus` frames use `[1] & 0x01`
  as Zone-1 power (`[4]` Cool/Heat), and `body[2] & 0x10` (`zone1_water_temp`) to select `body[6]`
  (direct water target) vs `body[9]/2` (0.5 °C room target). Compressor state stays `None` in `0x01`
  and is overlaid by `0x10` when supported. Note: `#124` (`merol77a`, `[2]=0x15`) had one snapshot
  reporting 24 °C (`[9]/2`) while `[6]=40` (unconfirmed since Comment 7); both `zone1_water_target`
  and `room_target` remain exposed as climate attributes alongside `zone1_temp_mode`.
- The DHW water-heater entity (`water_heater.py`) is added only after a status frame for an ATW unit
  matched the layout (re-evaluated on coordinator updates, since `sn8` can be unknown at setup).
- **Subtype `0x10` live telemetry (`v0.21.0`, #120 / #139 / #142):** `171H120F` firmware splits on
  whether the Dollin cloud accepts a `0x10` query. Confirmed working on NØRDIS (#120) and Airwell
  Wellea Split (#139: outdoor, water inlet/outlet, compressor frequency, ODU voltage, total energy,
  condenser/evaporator/plate-HX/refrigerant temps, and high pressure in kPa). Rejected/unsupported
  on Rotenso (#143) and Midea M-Thermal 16 kW (#142 `@blahavl`, 25-byte `[2]=0x11` with 38-byte
  schedule template). `coordinator.py` probes `0x10` only when `0x02` is a placeholder and disables
  it for the session after the first `ApiError` or invalid frame. Compressor total run hours are not
  in `0x10`; they sit in subtype `0x0A` (`body[4..5]`, verified on NØRDIS #120) and are polled only
  when `0x10` succeeds, with their own single-probe `ApiError` session disable. The `0x10` energy
  counters (`total_kwh`, `total_thermal_kwh`) use the same sentinel filter as `0x04`
  (`atw.valid_energy_counter`: `0`/`0xFFFFFFFF` are never applied; the field keeps the base status
  value, i.e. unknown after a fresh poll/push decode, never `0`; #232).
- **Airwell Wellea Split (`sn8 171H120F`, #139) controlled Silent/ECO test:** 27-byte status with
  `body[2]=0x73`, `body[3]=0x90`, tail `e0,05,00` (does not match the `body[3:6] == a0,03,03`
  Galmet/NØRDIS signature). Both Silent Level 1 and Super Silent (Level 2) set `body[3] & 0x02`
  (`0x90 → 0x92`), confirming `0x01` only carries binary Silent ON/OFF (`super_silent_level` lives
  in `0x05`), while ECO left `body[3]=0x90` unchanged. Follow-up forced-mode captures (#139 C8)
  confirmed separate responses matching `midea-local` (`main`): **`0x05`** 10-byte Silent (`bit 0`
  ON/OFF, `bit 1` timer 1, `bit 3` Super Silent retained even when OFF, `body[2..9]` timer slots)
  and **`0x07`** 7-byte ECO (`bit 0` ON/OFF, `bit 1` timer, `body[2..6]` timer slots). We also
  retain the `0x05 <= 6B` ECO fallback (`midea-local` #300) in `apply_atw_silent` / `apply_atw_eco`.
- **Cross-checking `midea-local` before reverse-engineering `0xC3`:** Do **not** add `midea-local`
  as a runtime dependency (it couples to local LAN/MSmartHome and risks HA Core dependency clashes),
  but **do** cross-check [`midea-lan/midea-local`](https://github.com/midea-lan/midea-local)
  (`midealocal/devices/c3/message.py`) before reverse-engineering any `171H120F` subtype (`0x01`,
  `0x04`, `0x05`, `0x0A`, `0x10`) or `0xC3` SET command (`MessageSet`, `MessageSetECO`,
  `MessageSetSilent`). Treat `midea-local` as the candidate byte map and still verify against at
  least one real Dollin hardware capture before shipping.

### `171H120F` control: standard Midea C3 SET — hardware-validated on Rotenso Aquami (water) & Heiwa (#42, room), opt-in
- **Writes work with the standard Midea C3 SET**, not the 62-byte ITS frame (`build_c3_set`, `1214`):
  `c3_frame.build_c3_frame` (same AA header as the query), message type `0x02`, body `01 [power]
  [mode] [z1] [z2] [dhw] [room×2] [curve flags]`, direct °C (no +35). Accepted with `code=0`; the
  device answers with its full new status. Validated one at a time on a Rotenso Aquami (EU,
  2026-11-17), each re-queried and reverted, all in Heat mode with water-temperature control: DHW
  target, DHW power, Zone-1 water target, Zone-1 curve, Zone-1 power (from HA, confirmed in the
  app). The exact accepted frames are pinned in `tests/test_c3_basic.py`.
- Silent = SET body `05` + 9 bytes, ECO = `07` + 6 bytes; bit 0 of the first byte is on/off (and
  bit 3 is Super Silent for `0x05`, cleared explicitly when selecting Silent Level 1 via
  `ILetComfortMuteSelect`) and the rest is the unit's timer schedule, copied from a fresh `05`/`07`
  query (midea-local zeroes it). A reply shorter than the validated layout (silence 9 data bytes,
  ECO 5 + one accepted `00` pad) is rejected before any SET — never zero-fill a schedule.
- **Gating:** control is an options-flow opt-in (`CONF_C3_BASIC_CONTROL`, offered only for ATW units)
  because Italtherm (#22), Galmet (#38), and other `171H120F` frames also match the layout but are
  not validated for writes. Only the ATW dialect plans C3 SETs (other dialects refuse C3-only
  intents) and it never plans the legacy ITS frame; `c3_basic.apply_basic_changes` refuses mode changes, any Zone-1
  power-on/setpoint/curve change unless the unit is already in Heat (a Heat request must never power
  on Cool), water-target writes on room-mode units (and vice versa), and DHW/Fast-DHW writes when
  `cap_dhw` is False. Both water-temperature (`body[6]`) and room-temperature (`body[9]`, 0.5 °C)
  setpoint writes, `Fast DHW` (`switch.*_fast_dhw` when `cap_dhw`), and `select.*_silent_mode`
  (Off / Level 1 / Level 2) are routed through C3 basic/feature SET when opt-in is enabled.
- **One write path (#113):** entities send typed commands (`commands.py`) to
  `coordinator.async_send_command`, which asks the selected dialect's pure
  `ProtocolAdapter.plan_write(command, WriteContext)` for a `WritePlan` (`protocols/write.py`).
  Capability checks, ranges, refusals and frame encoding live in the dialect
  (`protocols/{standard,aquapura,atw,kjrh120l}.py`). A plan is static (KJRH-120L short writes) or
  read-modify-write: it names the `FreshRead` query and a pure `encode(fresh_body)`.
  `run_write_plan` does read → encode → send inside `coordinator.async_execute_command`, which
  handles the phone-app guard, a per-appliance `_write_lock` spanning the fresh read → SET (and any
  auth retry, which repeats the read), one re-auth, `HomeAssistantError` wrapping, and refresh on
  success only. `WriteRefused` is surfaced verbatim; `ApiError`/`ValueError` as "Heat pump command
  failed: …". The client is write transport only (`send_hex_command`). Frame builders live in
  `c3_basic.py` / `protocols/kjrh120l.py` / `c3_frame.py`; entities pass semantic commands only
  (no hex). `tests/test_write_planning.py` is the (dialect, state, command) → hex | refusal table.
- Query support on Rotenso (`171H120F`): `01`, `05`, `07` return real data; `02` returns the
  placeholder template; `04` zeros; `08`, `09`, `0A`, `10` are rejected with `1214` (whereas `0x10`
  and `0x0A` succeed on NØRDIS #120 and Airwell #139).

### KJRH-120L dual variant (`sn8 17100003`, #5 / #69) — hardware-validated reads & Zone-1 writes
- The same `sn8` covers pure-DHW and Zone-1 + DHW controllers. Gate the dual layout only when status
  `body[8] == 1` and `body[9] == 1`; pure-DHW captures have `0/0`.
- Dual layout: `body[12]` is the Zone-1 setpoint and `body[15]` is the DHW setpoint.
- Zone-1 is read-only in the climate entity. A field `0x08` write was tested on hardware and always
  forced 23 °C, regardless of the requested value, so do **not** use it.
- The app's Zone-1 setpoint write is field `0x04` (`00 04 01 <°C> ff`), confirmed by read-back. Run
  mode is field `0x03` (heat 3, cool 2; paired with the Zone-1 setpoint clamped into the destination
  mode's reported range) and Zone-1 power is field `0x00`. Implemented in PR #149 / v0.19.0 as
  dedicated Zone-1 Target (`number`), Mode (`select`), and Zone-1 Power (`switch`) entities gated on
  a latched `CONF_KJRH120L_ZONE1_CAPABLE` observation in `entry.data`; see
  `docs/KJRH120L_CONTROL_PROTOCOL.md`.
- Evidence limit on the gate above: on one dual unit, `body[8]`/`body[9]` follow the Zone-1/Zone-2
  power state, not a fixed hardware marker. With Zone 1 switched off they read `0/0`, so the
  unlatched `kjrh120l_has_zone1` check reads that unit as pure-DHW (the new Zone-1 entities use the
  latched flag to stay available across off states and restarts). Whether the issue #5 pure-DHW
  hardware lacks a zone is not known. See `docs/KJRH120L_SYSTEM_DECODE.md`.
- DHW uses a separate Number entity and the confirmed short field `0x07` write. Pure-DHW keeps its
  existing climate behavior.
- Outdoor ambient temperature (`t4_temp`) is decoded from `body[57] - 196` (v0.20.0, #156), live-
  verified on v0.22.0 (#142 `@keramys`: `0xca` → 6.0 °C matching app 6 °C). In `@keramys`'s Normal vs
  Silent Level 2 capture diff, `body[21]` and `body[31]` both flipped `0x00 → 0x01` (candidates
  only; not decoded yet) while `body[49]` remained `0xdd` (tank temp remains unmapped). `@keramys`'s
  working unit also reports `authStatus=0, activeStatus=0` in `appliance` metadata, so those zero
  flags alone do not explain `code=1214` (relevant to #121).

### AQUAPURA profile (`sn8 171000AU`, AQS Energie split HPWH, #12; discriminator collision under audit)
- The real water/tank temp is in `status.box_bottom_temp` (status byte[17], offset-decoded → e.g. 40 °C).
- The standard `sensors.twin_temp`/`twout_temp` (sensors bytes 25–26) are `0x23` null-fill → decode to 0
  (that was the "water temp = 0" bug).
- A second hardware variant with the same `sn8` publishes live inlet/outlet values while `th_temp` is a
  zero placeholder. Climate current temperature therefore keeps a nonzero `th_temp` when present and
  otherwise falls back to `twout_temp`, then `twin_temp`. Do not assume `sn8` alone distinguishes these
  AQUAPURA layouts.

### Entity routing (important)
- Entities read **only domain-named `HeatPumpState` properties** (`protocols/base.py`), never
  `ITSStatus`/`ITSSensors` wire fields (#111). Model-specific routing lives in the property
  (data-driven) or in a `ProtocolAdapter` method (e.g. `dhw_target_temp`). Add a property before an
  entity needs a new value.
- **Climate entities map a dialect `ClimateView` (#192):** `ProtocolAdapter.climate_view(status, ctx)` /
  `zone2_climate_view` (read as `coordinator.climate_view` / `coordinator.zone2_climate_view`) return
  modes, mode, target, step, min/max, supported features, availability and the user-facing
  `read_only_reason` / `temperature_refusal` (`protocols/views.py`). The regimes live in the dialects:
  legacy status-mode view (`legacy_climate_view`, STANDARD/AQUAPURA and ATW without a C3 layout),
  KJRH-120L pure-DHW vs dual read-only Zone-1 card (`kjrh120l_climate_view`), ATW C3 read-only vs
  opt-in control (`atw_climate_view`, `atw_zone2_climate_view`). `climate.py` holds no model logic:
  it raises the view's refusal before sending (so the phone-app guard order is unchanged) and passes
  the requested value.
- **Clamping and range refusals are dialect-owned (#113/#192):** `plan_write` clamps
  `SetTargetTemperatureCommand` to the climate view's bounds (STANDARD/AQUAPURA by mode, KJRH-120L
  20–70 °C, ATW C3 range), ATW `SetZoneTargetCommand(2)` to the Zone-2 view, ATW
  `SetDhwTargetCommand` to the reported DHW range (`atw_dhw_target_range`, else HA's 110–140 °F
  water-heater default the entity shows), and KJRH-120L `SetDhwTargetCommand` to 35–60 °C;
  `kjrh120l_zone1_target` refuses a Zone-1 target that is not a whole degree or outside the reported
  range. Entities never clamp. Non-finite service values are refused when the command is built
  (`entity.target_command`).
- **Which optional entities exist is dialect-owned (#192):** `ProtocolAdapter.entity_capabilities(status,
  ViewContext)` returns an `EntityCapabilities` (`protocols/views.py`): one `EntitySupport(exists,
  possible, available, writable)` each for Boost, Silent Mode (mute), Zone-2 climate, DHW water heater,
  the opt-in C3 switches, Zone-2 curve, Fast DHW and the KJRH-120L Zone-1 group. Read it as
  `coordinator.entity_capabilities` (`protocols.project_entity_capabilities(sn8, state, ctx)`); while
  `sn8` is unknown, `exists` comes from the STANDARD default and anything any dialect could expose stays
  `possible` (`profile_settled=False`). Platforms add entities with `entity.async_add_supported` and
  must not import `ModelProfile` or call `resolve_profile` / `get_adapter(...).profile`.
- **Boost and Silent Mode are hidden on the KJRH-120L (#192):** no write was ever captured for them
  (they sent DHW-OFF before #229 and refuse since). Because they used to be created for every model,
  `entity.async_remove_unsupported` removes exactly the `<code>_boost` / `<code>_mute` registry entries
  owned by the config entry when the dialect says they are not `possible`, at setup and, while `sn8`
  is unknown, on coordinator updates until the profile settles (so a late KJRH resolution also removes
  the live entities). Every other model keeps them with unchanged unique_ids. The helper only accepts
  names in `REMOVABLE_CAPABILITIES` (options-independent support); an option-dependent capability such
  as the C3 switches would delete entities and user customisations whenever the opt-in is turned off.
- `sensor.py`: **"Water Inlet Temperature"** ← `water_inlet_temp` (`twin_temp`); **"DHW Tank
  Temperature"** ← `dhw_tank_temp` (`th_temp`).
- **Unknown vs zero:** telemetry fields (`comp_frq`, `exv_drg`, `pressure_h/l`, `total_kwh`,
  `comp_total_run_hours`, sensor `tf/tp/th_temp`, `odu_*`, `water_flow`, `dc_*`) default to `None`
  and are set only by a decoder/overlay that actually carries them. Never default a telemetry field
  to `0`: TOTAL_INCREASING energy/run-hours reading 0 for unknown is a meter reset in HA statistics.
- `climate.py` `current_temperature` (`climate_current_temperature`) is **profile-aware**: `th_temp`
  for AQUAPURA and validated ATW layouts, unavailable for the ATW sensor-template variant, and
  `twin_temp` for STANDARD.
- For ATW/AQUAPURA the tank temp is routed into **`th_temp`** (so the correctly-named "DHW Tank
  Temperature" entity shows it). Do **not** route a tank reading into `twin_temp` (that mislabels "Water
  Inlet"). This was corrected after a reporter flagged it.

### Vendor `0xC3` plugin control schema (write path) — from the vendor's own bundle

Fetched with `scripts/fetch_plugin.py` (dev tool; usage in `CONTRIBUTING.md`). Bundle availability on
the **US tenant** (`appId 8010`), checked 2026-09-08:

| `sn8` | model | bundle |
|---|---|---|
| `17100007` | KJRH-120L2 | ✅ `0xC3` v1.0.65, 13.7 MB — 30 Weex screens, all `Mthermal*` (M-thermal) |
| `171H120F` | KJRH-120F/K (ATW) | ❌ `2200004` — product exists, **no plugin registered** |
| `17100003` | KJRH-120L | ❌ `2200004` |
| `171000AU` | AQUAPURA | ❌ `2200007` — not in this region's catalogue |

`2200004` did **not** budge under sweeps of `sdkVersion` (0/1/2018–2099 variants), `clienttype` (0–4),
`type` case (`0xc3`/`0XC3`), `language`, `firmwareVersion`, or `version`. The EU host with a US token
answers `9999`, so confirming whether the **EU** tenant registers those two needs an **EU account**.

**Crucial: the bundle contains NO byte offsets.** The plugin is JSON-level: screens call
`luaControl({params: {control_type, <field>: <value>}})` (reads use `luaQuery`), and the JSON→byte
conversion happens in the app's **native Lua** for `0xC3`, not in the JS. So the bundle is the source of
truth for **field names, value domains and semantics** — the frame bytes still need the Lua or a capture.
Do not expect this route to hand over a byte map.

The Lua is not obtainable from the cloud on this tenant (this answers @dzerik's open question in #48 —
it fails for a C3 tenant too): `/v1/device/status/lua/get` → `9999` for every body shape tried;
`/v1/product/upgrade/lua/get/latest` and `/v1/product/lua/get/latest` → `1800` with a **valid** token;
`type=lua`/`0xC3.lua`/`luaFile` on the plugin endpoint → `2200007`.

**`control_type` — the command-group selector.** The bundle carries it in two encodings (decimal in one
constants block, `0x20X0` in another):

| group | dec | hex |
|---|---|---|
| `base` | 1 | `0x2010` |
| `day_time` | 2 | `0x2020` |
| `holiday_away` | 4 | `0x2040` |
| `silence` | 5 | `0x2050` |
| `holiday_home` | 6 | `0x2060` |
| `eco` | 7 | `0x2070` |
| `install` | 8 | `0x2080` |
| `disinfect` | 9 | `0x2090` |
| `weeks_dhw` / `weeks_zone1` / `weeks_zone2` | — | `0x20D0` / `0x20E0` / `0x20F0` |
| `weeks_schedule` / `weeks_all_schedule` | 131 / 132 | — |
| `dhw_schedule_1..4` | 33537–33540 | `0x8301`–`0x8304` |
| `zone1_schedule_1..4` | 33553–33556 | `0x8311`–`0x8314` |
| `zone2_schedule_1..4` | 33569–33572 | `0x8321`–`0x8324` |

Note our `build_c3_set` writes `0x02` at `frame[9]` while the vendor's *base* control group is `1`/
`0x2010`. **Do not assume those are the same field** — that equivalence is unproven.

**The four writes #54 asked about — all under `control_type = base`:**

| write | field(s) | domain |
|---|---|---|
| Zone-1 setpoint | `zone1_temp_set` | `stringNumber`, bounded by `zone1_heat_min/max_set_temp` and `zone1_cool_min/max_set_temp` |
| Zone-2 setpoint | `zone2_temp_set` | as above with `zone2_*` bounds |
| Room setpoint | `room_temp_set` | bounded by `room_min/max_set_temp` |
| DHW setpoint | `dhw_temp_set` | bounded by `dhw_min/max_set_temp` |
| power | `zone1_power_state`, `zone2_power_state`, `dhw_power_state` | `"on"` / `"off"` — **strings, per zone, separate from mode** |
| mode | `run_mode_set` | `"auto"` / `"cool"` / `"heat"`; when auto, `runmode_under_auto` = `"cool"` / `"heat"` |
| weather-curve enable | `zone1_curve_state`, `zone2_curve_state` | `"on"` / `"off"` |
| weather-curve select | `zone1_curve_type`, `zone2_curve_type` | `1`–`8` presets, `9` = custom |

There is **no weather-curve *offset* field**. The custom curve (`type 9`) is edited as temperature points
through the same `zone1_temp_set`/`room_temp_set`/`dhw_temp_set` keys on the CurveCustom screen — so the
"curve offset" #54 assumed exists is not a key in the vendor's model.

Capability/supporting fields worth knowing: `heat_enable`, `cool_enable`, `dhw_enable`,
`double_zone_enable`, `zone1_temp_type` (`water_temperature_type` | `room_temperature_type`),
`zone1_terminal_type` (`fan_coil` | `floor_heat` | `radiatior` — the vendor's own typo),
`boostertbh_en`, `forcetbh_state`, `fastdhw_state`, `silence_on_state`, `eco_on_state`,
`holiday_on_state`, `holiday_on_type` (`0` off / `1` away / `2` home), `auto_min/max_set_temp`.

**What this does and does not unblock (#42 / #43 / #49):** it gives the vendor's semantic model —
booleans are the strings `"on"`/`"off"`, modes are strings, per-zone power is independent of mode, and
setpoint bounds are device-reported rather than hardcoded. It does **not** give the frame bytes, and
`/appliance/control/hexadecimal` takes hex. The remaining paths are the Lua, or an **iOS** capture (the
iOS app uses the HTTP endpoint and therefore sends already-encoded hex; Android is MQTT-only — §2b, #42).

### Android MQTT write investigation (#78)

The official Android package is `com.mbt.iletcomfort`. Version 1.7.0 contains AWS IoT, Eclipse Paho,
the `/midea/dev/` topic fragment, and the Midea OEM native libraries. Its application code is wrapped
by **KwProtect**: static JADX analysis exposes only `com.kiwivm.security.StubApplication` and the
loader, while the real classes are decrypted at runtime. Do not repeat static decompilation expecting
to find the publish call unless an unprotected older build becomes available.

The push client debug-logs a syntactically valid `commandHex` only when it cannot decode the message
as status, and only on the already-scoped appliance topic. Use that probe with `phone_app` mode while
making one official-app change at a time. If no line appears, the next step is a runtime capture of the
app's exact publish topic. Never widen the integration subscription to a broker wildcard: the broker
does not enforce per-appliance topic ACLs.

---

## 5. Code map

| File | Responsibility |
|------|----------------|
| `api.py` | `ILetComfortClient` (login, `list_appliances`, `send_hex_command`, `query_status`/`query_sensors`); frame build/parse (`build_c3_query`, `build_c3_set`, `parse_hex_response`, `extract_c3_body`); decoders `decode_its_status`/`decode_its_sensors` + dataclasses `ITSStatus`/`ITSSensors`; `_temp_offset`; `AuthError`/`ApiError`. |
| `error_codes.py` | Vendor C3 numeric `error_code` → panel code + description table; `sensor.py` exposes known entries as attributes while preserving the numeric sensor state. |
| `scripts/fetch_plugin.py` | **Dev tool, not shipped** — fetches the vendor per-model plugin bundle (`0xC3` = Weex JS) via the `version:"0.0.0"` downgrade on `/v1/product/upgrade/plugin/get/latest`. Source of truth for the control **schema** (not bytes) — see §4. |
| `scripts/c3_probe.py` | **Dev tool, not shipped, read-only** — sends C3 queries for `171H120F` units and decodes them (midea-local C3 layouts); reads only `ILET_EMAIL`/`ILET_PASSWORD`/`ILET_REGION` from `.env`. Cannot send SET frames. |
| `model_profiles.py` | `ModelProfile` enum, `_SN8_PROFILES` table, `resolve_profile`, `decode_atw_status`, `apply_profile_to_status`, `apply_profile_to_sensors`. **Add new model support here.** |
| `protocols/snapshot.py` | `build_snapshot(adapter, status, sensors, raw_frames, *, retained_subtypes=False, energy_notify=None)` — the **one** pure step that applies the profile sensors decode and every ATW overlay (0x10 → 0x0A → 0x05/0x07 when C3 basic → 0x04 last). `_poll`, `_apply_push_status` and `_apply_push_energy` and the corpus contract all use it (#225); never re-implement the overlay chain elsewhere. Pushes pass `retained_subtypes=True` (0x10 counters only, 0x05 reconciled with the 0x01 Silent bit) and the coordinator's sentinel-filtered 0x04 state as `energy_notify`; `raw_frames[0x04]` stays literal. Probing stays in the coordinator. |
| `c3_basic.py` | Standard Midea C3 basic status decode (`decode_c3_basic`, `C3BasicStatus`) and validated SET builders (`apply_basic_changes`, `build_basic_set`, `build_feature_set`) for `171H120F`; used by the ATW dialect's `plan_write`. |
| `c3_frame.py` | Low-level C3 framing (`build_c3_frame`, `build_c3_query`, `build_c3_set`, `parse_hex_response`, `extract_c3_body`, `_temp_offset`). |
| `water_heater.py` | DHW entity for ATW units whose status matches the C3 basic layout. |
| `coordinator.py` | `ILetComfortCoordinator` (`DataUpdateCoordinator[HeatPumpState]`): polling, re-auth, cache-fallback, offline Repair card. `coordinator.data` is the typed `HeatPumpState` returned by `build_snapshot` (`status`, `sensors`, `raw_frames`); there is no dict snapshot (#111). Caches `appliance_meta` (best-effort, never fatal) and exposes `sn8`; threads profile into decode. |
| `diagnostics.py` | Redacted snapshot: raw frames, decoded status/sensors, `sensors_temperature_scan` (per-byte `_temp_offset` map — use it to find a model's misplaced temp byte), and the `appliance` metadata block. `APPLIANCE_TO_REDACT = {owner, sn, name}` (keeps `applianceType`/`modelNumber`/`sn8`). |
| `climate.py` / `number.py` / `sensor.py` / `binary_sensor.py` / `switch.py` / `select.py` / `water_heater.py` | HA entities; they read domain-named `HeatPumpState` properties and the dialect projections (`coordinator.entity_capabilities`, `coordinator.climate_view`) only. `climate.py` is a thin `ClimateView` mapping. See §4 entity routing. |
| `protocols/base.py` | `HeatPumpState` (`coordinator.data`, domain-named read properties, unknown = `None`) and the `ProtocolAdapter` contract. |
| `protocols/views.py` | Pure, dialect-owned entity projections (#192): `ViewContext` (C3 opt-in, latched KJRH Zone 1), `EntityCapabilities` / `EntitySupport` (which optional entities exist, may appear, are available and writable), and `ClimateView` (+ `legacy_climate_view`, `NO_ZONE2_VIEW`, the climate refusal messages). |
| `entity.py` | `build_device_info`; `async_add_when_capable` / `async_add_supported` (add from the capability projection, keep listening while `possible`); `async_remove_unsupported` (remove stale registry entries the dialect never exposes); `target_command` (build a target command from an unclamped service value). |
| `config_flow.py` / `__init__.py` / `const.py` | Setup, entry, constants. |
| `tests/` | pytest suite; real captured frames are pinned as fixtures (e.g. issue-#11 frames for STANDARD regression). Build a test `coordinator.data` with `tests/snapshot_helpers.py::make_state(status, sensors, raw_frames, sn8=...)`, which runs the production `build_snapshot`. |

---

## 6. Diagnosing a "wrong/empty sensor" report (playbook)

1. Ask for a **diagnostics file** (Settings → Devices & Services → iLetComfort → ⋮ → Download
   diagnostics) **and** the **real value from the iLetComfort/BTRI app** at the same moment.
2. From diagnostics, read the `appliance` block (`sn8`!) and `sensors_temperature_scan`.
3. Cross-reference the app's real value against the scan / raw bytes to find the correct byte.
4. If it's model-specific, add/extend an `sn8` profile in `model_profiles.py` (default-safe), TDD against
   the real frames, keep STANDARD unchanged.
5. If setup fails before diagnostics exist (`1214` / truncated): get **debug logs**; the fix needs the
   command the **iLetComfort app** sends (mitmproxy capture — see §2b, no rooting assumptions).

---

## 7. Dev & release workflow

- **`main` is protected:** required status checks `pytest` + `Validate Conventional Commits
  title`, and `enforce_admins=true`. **No direct pushes** — they're rejected. Land changes via a PR
  (CI auto-runs, ~30s) and merge.
- **release-please** drives versioning from Conventional Commits on `main` (`feat:` → minor, `fix:`/
  `docs:` → patch/none). Its release PRs are **bot-authored, so CI does not fire** on them and
  `enforce_admins` blocks override → **close+reopen the release PR as a real user** to trigger checks,
  then merge. Repo **auto-merge is disabled**.
- **Conventional Commits** required (PR title is checked). Squash-merge keeps the conventional message on
  `main`.
- **TDD & local quality gates**: write failing tests first; run `pytest`, `ruff check .`, and
  `mypy custom_components/iletcomfort/` locally before pushing. Never break the STANDARD-decode
  regression tests or the entity-stability contract in `tests/test_hardware_corpus.py`.
- **Mandatory clean-context self-review before PR**: before opening any PR, run an independent
  code review of the full `git diff main...HEAD` in a **new subagent / clear context** to catch
  edge cases, push/poll parity gaps, redaction leaks, and test gaps before they burn a human
  review cycle.
- Tooling preference: `rg` / `fd` / `bat` over `grep` / `find` / `cat`.
- Distributed via **HACS**.

---

## 8. References

- Protocol RE / CLI: <https://github.com/tgenov/iletcomfort-cli> (captured via iLetComfort **iOS app** +
  mitmproxy).
- Out-of-scope (Midea MSmartHome) devices: <https://github.com/georgezhao2010/midea_ac_lan>,
  <https://github.com/midea-lan/midea-local>.
- `docs/C3_PROTOCOL_MATRIX.md` (Hardware-Verified Dollin vs. `midea-local` candidate matrix across `0x01`/`0x02`/`0x05`/`0x0A`/`0x10` and write dialects).
- `README.md` (scope, install, login-war note) and `docs/TROUBLESHOOTING.md`, `CONTRIBUTING.md`.
