# iLetComfort Heat Pump Integration for Home Assistant

A custom Home Assistant integration for iLetComfort (ITS) heat pumps using the Midea Dollin cloud API.

## What this integration is

This integration **emulates the iLetComfort app**: it talks to the iLetComfort (Midea **Dollin**) cloud using the same protocol the app does. If your heat pump works in the **iLetComfort / BTRI app**, it's the right target for this integration.

Some heat pumps can *also* be controlled by other vendor apps — for example **SMARTHOME** (the Midea **MSmartHome** cloud). Those apps use a **different cloud protocol**, even though the heat pump itself may speak the same protocol underneath. This integration does **not** emulate those other clouds. If your device works *only* in a Midea/MSmartHome-style app and not in iLetComfort, a Midea-focused integration such as [midea_ac_lan](https://github.com/georgezhao2010/midea_ac_lan) or [midea-local](https://github.com/midea-lan/midea-local) is the better fit.

## Installation

### HACS (Recommended)

1. Open Home Assistant and go to **HACS** > **Integrations**.
2. Click the three-dot menu in the top right and select **Custom repositories**.
3. Add `https://github.com/tgenov/ha-iletcomfort` with category **Integration**.
4. Click **Add**, then search for "iLetComfort Heat Pump" in HACS and install it.
5. Restart Home Assistant.

### Manual

1. Download or clone this repository.
2. Copy the `custom_components/iletcomfort` folder into your Home Assistant `config/custom_components/` directory.
3. Restart Home Assistant.

## Configuration

1. Go to **Settings** > **Devices & Services** > **Add Integration**.
2. Search for "iLetComfort Heat Pump".
3. Enter your iLetComfort account email and password.
4. The integration will automatically discover your heat pump appliance.

## Using alongside the official app

The vendor cloud allows only **one active login per account**, so Home Assistant and
the official iLetComfort / BTRI app signed in to the *same* account will keep logging
each other out — the heat pump appears to flap offline. Give Home Assistant its own
account and share the device to it; see
[Heat pump keeps dropping offline (the "login war")](docs/TROUBLESHOOTING.md#2-heat-pump-keeps-dropping-offline-the-login-war)
for the full walkthrough.

## Troubleshooting & reporting issues

If a sensor reads `0`/empty, the integration errors, or something else
misbehaves, please read the [Troubleshooting guide](docs/TROUBLESHOOTING.md)
first. It explains how to download a redacted **diagnostics** file and enable
**debug logging** — the data needed to act on most reports.

When you're ready, open an issue via the
[issue forms](https://github.com/tgenov/ha-iletcomfort/issues/new/choose); they
prompt for exactly what's required. Empty/wrong sensor values are almost always
a per-model data-layout difference, so reports must include your **exact device
model**, the **real values from the official app**, and the **diagnostics file**.

## Contributing

This project uses [Conventional Commits](https://www.conventionalcommits.org/) and `release-please` to drive versioning, changelogs, and HACS releases. PR titles must follow the format (e.g. `feat: ...`, `fix: ...`) — see [CONTRIBUTING.md](CONTRIBUTING.md).
