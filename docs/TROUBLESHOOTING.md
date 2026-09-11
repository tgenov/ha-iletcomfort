# Troubleshooting & reporting issues

This integration talks to your heat pump over a binary protocol (the "C3"
protocol) whose byte layout was reverse-engineered from a small number of real
devices. **Different brands and models pack the same readings at different
positions in the data.** That's the most common reason a sensor on your model
shows up as `0`, `Unknown`, or empty while it works fine on someone else's unit.

To add support for your model, the maintainer needs to line up two things:

1. The **raw data frames** your device sends, and
2. The **real values** for those same readings, read from the official app.

This guide shows how to collect both. Doing this once usually turns a "sensor X
is empty" report into a one-line fix.

---

## 1. Before you file

- **Check the device in the official app** (iLetComfort / BTRI). If a reading is
  also wrong or missing *there*, the problem is on the device or vendor-cloud
  side, not in this integration.
- **Look for a Repair card.** If Home Assistant shows a *"Heat pump appears
  offline"* Repair under **Settings → Devices & Services**, the integration has
  been getting empty responses and is showing the last known values. Wait for it
  to clear (it does so automatically when the device starts responding), or fix
  the device's connectivity, before reporting wrong values.
- **Rule out the "login war"** (next section) if the device only drops offline
  while Home Assistant is running, or if HA and the app seem to take turns
  working.

## 2. Heat pump keeps dropping offline (the "login war")

If the heat pump **drops offline the moment Home Assistant starts polling** — and
comes back when you stop the integration — or Home Assistant and the official app
seem to take turns working, you're hitting the vendor cloud's single-session
limit, not a bug in this integration.

**Why it happens.** The Midea Dollin cloud allows only **one active login per
account**. Each login issues a fresh token and immediately revokes the previous
one, so two clients can never be signed in at the same time. This integration
stores your credentials and **re-authenticates automatically** whenever its token
is rejected — which logs the other client (your phone) out. Open the app, and it
logs Home Assistant out. The two keep evicting each other, the device appears to
flap offline, and polls come back as empty `01` / `02` echo frames.

Home Assistant *on its own* is fine: it keeps its token and only re-authenticates
when the token is rejected. The conflict only appears when the **same account** is
signed in to both Home Assistant and the official app.

**Read-only alternative — leave the phone app connected.** In the integration's
options, select **Leave the phone app connected (read-only push)**. Home Assistant
logs in once during setup or restart to mint a session-independent MQTT
certificate, then stops account polling and blocks control commands. After HA has
loaded, sign back into the official app; real-time status continues over MQTT. If
Home Assistant stays running until the certificate nears its X.509 expiry, it
rotates the certificate before expiry, up to seven days early. Rotation tries the existing token first;
only a rejected token causes one credential login, which may briefly sign the phone
app out once per certificate lifetime. If renewal fails, HA keeps the still-valid
MQTT connection and retries an hour later. If the MQTT connection drops, HA marks
the entities unavailable rather than starting periodic account polling. Select
**HA is the primary client** to restore polling and control.

**Workaround — give Home Assistant its own account.** Use a second cloud account
for Home Assistant and share the heat pump to it, so each client gets its own
session:

1. On a **different phone or device**, create a second iLetComfort / BTRI cloud
   account with a separate email.
2. From your **primary** account (the one the heat pump is paired to), use the
   app's **share device** feature to share the heat pump with the new account.
3. Configure this integration with the **dedicated** account's credentials, and
   keep using your primary account on your own phone.

> **Important:** create the second account, and accept the share, on a *different*
> device than your primary account. Sharing to an account created on the same
> phone has been reported to fail.

## 3. Download diagnostics (preferred)

This is the easiest way to capture everything in one file.

1. Go to **Settings → Devices & Services**.
2. Click **iLetComfort Heat Pump**, then your device.
3. Open the **⋮** (three-dot) menu and choose **Download diagnostics**.
4. Attach the downloaded `.json` file to your GitHub issue.

The file is **pre-redacted** — your email and password are removed. It contains
the raw frames (as hex), the fully decoded values, your region, and version
info.

## 4. Enable debug logging (fallback / live frames)

Use this if the diagnostics download isn't available, or if a maintainer asks
for live frames over time.

**Option A — from the UI (no restart):**

1. **Settings → Devices & Services → iLetComfort Heat Pump**.
2. Click **Enable debug logging**.
3. Let it run for a couple of minutes (or reproduce the problem).
4. Click **Disable debug logging** — Home Assistant downloads a log file
   automatically. Attach it to your issue.

**Option B — via `configuration.yaml` (needs a restart):**

```yaml
logger:
  logs:
    custom_components.iletcomfort: debug
```

Restart Home Assistant, then find the relevant lines in **Settings → System →
Logs** (or in the `home-assistant.log` file). Look for lines like:

```
STATUS RAW: aa,01,...
SENSORS RAW: bb,02,...
```

Copy a few of each into your issue.

### Capturing a phone-app MQTT command

Only do this when a maintainer asks for an MQTT command capture. Select the
**Leave the phone app connected (read-only push)** operation mode, enable debug
logging with Option A above, and then make exactly one change in the official
phone app. Wait for the heat pump state to update before disabling debug
logging.

The useful line has this form:

```
MQTT unhandled scoped frame message_type='control' command_hex=aa...
```

If no such line appears, say so when attaching the log. That result is useful:
it means the phone app probably publishes commands on a different topic. The
probe listens only to your appliance's existing status topic; it never uses an
MQTT wildcard.

## 5. Read the ground-truth values from the app

Open the official **iLetComfort / BTRI** app and note what it shows for the
readings that are wrong in Home Assistant — water temperature, energy,
compressor on/off, and so on. **Screenshots taken at the same time as your
diagnostics/logs are ideal**, because they let the maintainer match a specific
raw frame to a specific real value.

## 6. What a good report contains

The [issue forms](https://github.com/tgenov/ha-iletcomfort/issues/new/choose)
ask for all of this — it's collected here so you know *why*:

- **Exact device brand & model** — tells us which layout you have.
- **Which entities are wrong/empty** — tells us which fields to look at.
- **Real values from the app** (screenshots) — the ground truth to map against.
- **Diagnostics file** (or `STATUS RAW:` / `SENSORS RAW:` log lines) — the raw
  bytes to map *from*.
- **Region, integration version, Home Assistant version** — context.

With the raw frames *and* the real values for your model, re-mapping a field is
usually straightforward. Without them, a report like "the water temperature is
always 0" can't be acted on. Thanks for taking the time to gather it!

## Unrecognized status mode when controlling a device

If a control action reports `Unrecognized status mode`, the integration could
not interpret the device's current mode and did not send a control command.
An unfamiliar model layout can otherwise cause an unrelated setting, such as
Boost, to change power or temperature. Readings may also be incorrect until
the model's layout is supported.

Download diagnostics and include the `appliance` metadata, raw frames, and the
corresponding values from the official app in your issue. Missing appliance
metadata is retried during polling; after a temporary cloud failure, wait for
another update and download diagnostics again. Do not change controls solely
to collect these diagnostics.

If the message says `Cannot preserve Auto mode`, a setpoint or feature change
could not preserve Auto with the known command encoding. An explicit change
to a supported operating mode remains possible.
