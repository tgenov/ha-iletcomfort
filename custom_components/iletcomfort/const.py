"""Constants for the iLetComfort integration."""

DOMAIN = "iletcomfort"
PLATFORMS = ["climate", "sensor", "switch", "select", "binary_sensor"]

CONF_APPLIANCE_CODE = "appliance_code"
CONF_REGION = "region"

REGION_US = "us"
REGION_EU = "eu"
REGION_URLS = {
    REGION_US: "https://us.dollin.net",
    REGION_EU: "https://eu.dollin.net",
}
DEFAULT_REGION = REGION_US

DEFAULT_SCAN_INTERVAL = 60
