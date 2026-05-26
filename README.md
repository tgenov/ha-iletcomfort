# iLetComfort Heat Pump Integration for Home Assistant

A custom Home Assistant integration for iLetComfort (ITS) heat pumps using the Midea Dollin cloud API.

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

## Contributing

This project uses [Conventional Commits](https://www.conventionalcommits.org/) and `release-please` to drive versioning, changelogs, and HACS releases. PR titles must follow the format (e.g. `feat: ...`, `fix: ...`) — see [CONTRIBUTING.md](CONTRIBUTING.md).
