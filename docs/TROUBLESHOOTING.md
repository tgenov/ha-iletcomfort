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

## 2. Download diagnostics (preferred)

This is the easiest way to capture everything in one file.

1. Go to **Settings → Devices & Services**.
2. Click **iLetComfort Heat Pump**, then your device.
3. Open the **⋮** (three-dot) menu and choose **Download diagnostics**.
4. Attach the downloaded `.json` file to your GitHub issue.

The file is **pre-redacted** — your email and password are removed. It contains
the raw frames (as hex), the fully decoded values, your region, and version
info.

## 3. Enable debug logging (fallback / live frames)

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

## 4. Read the ground-truth values from the app

Open the official **iLetComfort / BTRI** app and note what it shows for the
readings that are wrong in Home Assistant — water temperature, energy,
compressor on/off, and so on. **Screenshots taken at the same time as your
diagnostics/logs are ideal**, because they let the maintainer match a specific
raw frame to a specific real value.

## 5. What a good report contains

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
