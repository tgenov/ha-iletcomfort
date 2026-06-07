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
  the device to it (documented in `README.md` / `docs/TROUBLESHOOTING.md`). `14xxx` codes are the
  auth range.

---

## 4. Decoding, model variants & `sn8` profiles

Heat-pump models pack the status frame (C3 subtype `0x01`) at **different byte offsets**. The cloud
appliance metadata has **NO device-class field**: both an ATW and an ATA unit report
`applianceType="0xC3"` and `modelNumber="0"`. The **only** differentiator is **`sn8`** — the 8-char
model-code serial prefix (e.g. `171H120F` vs `171000AU`). The full per-device serial `sn` is **redacted**
and never stored.

**Model-specific decoding is gated on an `sn8 → profile` table** (`custom_components/iletcomfort/model_profiles.py`):
- `resolve_profile(sn8)` → `ModelProfile.{STANDARD,ATW,AQUAPURA}`. **Unknown/missing sn8 → STANDARD
  (unchanged today's behavior).** So a new model can never be *corrupted* — worst case it doesn't get a
  profile yet. Caveat: `sn8` is assumed model-level (shared across units of a model); only ever confirmed
  on one unit per model so far.

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
- ATW sets `error_code=0` and `comp_running=False` (no confirmed running signal in this short frame).
- `decode_atw_status(body)` implements this. Uncertain HVAC mode/action is left conservative — validate
  on hardware before adding.

### AQUAPURA profile (`sn8 171000AU`, AQS Energie split HPWH, #12)
- The real water/tank temp is in `status.box_bottom_temp` (status byte[17], offset-decoded → e.g. 40 °C).
- The standard `sensors.twin_temp`/`twout_temp` (sensors bytes 25–26) are `0x23` null-fill → decode to 0
  (that was the "water temp = 0" bug).

### Entity routing (important)
- `sensor.py`: **"Water Inlet Temperature"** ← `twin_temp`; **"DHW Tank Temperature"** ← `th_temp`.
- `climate.py` `current_temperature` is **profile-aware**: `th_temp` for ATW/AQUAPURA, `twin_temp` for
  STANDARD.
- For ATW/AQUAPURA the tank temp is routed into **`th_temp`** (so the correctly-named "DHW Tank
  Temperature" entity shows it). Do **not** route a tank reading into `twin_temp` (that mislabels "Water
  Inlet"). This was corrected after a reporter flagged it.

---

## 5. Code map

| File | Responsibility |
|------|----------------|
| `api.py` | `ILetComfortClient` (login, `list_appliances`, `send_hex_command`, `query_status`/`query_sensors`); frame build/parse (`build_c3_query`, `build_c3_set`, `parse_hex_response`, `extract_c3_body`); decoders `decode_its_status`/`decode_its_sensors` + dataclasses `ITSStatus`/`ITSSensors`; `_temp_offset`; `AuthError`/`ApiError`. |
| `model_profiles.py` | `ModelProfile` enum, `_SN8_PROFILES` table, `resolve_profile`, `decode_atw_status`, `apply_profile_to_status`, `apply_profile_to_sensors`. **Add new model support here.** |
| `coordinator.py` | `ILetComfortCoordinator`: polling, re-auth, cache-fallback, offline Repair card. Caches `appliance_meta` (best-effort, never fatal) and exposes `sn8`; threads profile into decode. |
| `diagnostics.py` | Redacted snapshot: raw frames, decoded status/sensors, `sensors_temperature_scan` (per-byte `_temp_offset` map — use it to find a model's misplaced temp byte), and the `appliance` metadata block. `APPLIANCE_TO_REDACT = {owner, sn, name}` (keeps `applianceType`/`modelNumber`/`sn8`). |
| `climate.py` / `sensor.py` / `binary_sensor.py` / `switch.py` / `select.py` | HA entities. See §4 for which field backs which entity. |
| `config_flow.py` / `__init__.py` / `const.py` / `entity.py` | Setup, entry, constants, base entity. |
| `tests/` | pytest suite; real captured frames are pinned as fixtures (e.g. issue-#11 frames for STANDARD regression). |

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

- **`main` is protected:** required status checks `pytest (Python 3.12)` + `Validate Conventional Commits
  title`, and `enforce_admins=true`. **No direct pushes** — they're rejected. Land changes via a PR
  (CI auto-runs, ~30s) and merge.
- **release-please** drives versioning from Conventional Commits on `main` (`feat:` → minor, `fix:`/
  `docs:` → patch/none). Its release PRs are **bot-authored, so CI does not fire** on them and
  `enforce_admins` blocks override → **close+reopen the release PR as a real user** to trigger checks,
  then merge. Repo **auto-merge is disabled**.
- **Conventional Commits** required (PR title is checked). Squash-merge keeps the conventional message on
  `main`.
- **TDD**: write failing tests first; run `pip install -r requirements_test.txt` then `python -m pytest -q`.
  Never break the STANDARD-decode regression tests.
- Tooling preference: `rg` / `fd` / `bat` over `grep` / `find` / `cat`.
- Distributed via **HACS**.

---

## 8. References

- Protocol RE / CLI: <https://github.com/tgenov/iletcomfort-cli> (captured via iLetComfort **iOS app** +
  mitmproxy).
- Out-of-scope (Midea MSmartHome) devices: <https://github.com/georgezhao2010/midea_ac_lan>,
  <https://github.com/midea-lan/midea-local>.
- `README.md` (scope, install, login-war note) and `docs/TROUBLESHOOTING.md`, `CONTRIBUTING.md`.
