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

# --- MQTT real-time push (issue #55) ---------------------------------------
# Opt-in, disabled by default. When enabled, the coordinator mints an app
# certificate and subscribes to the appliance's push topics; polling continues
# at a reduced backstop cadence and takes over fully if push drops.
CONF_ENABLE_MQTT_PUSH = "enable_mqtt_push"
DEFAULT_ENABLE_MQTT_PUSH = False

# AWS IoT MQTT broker (TLS, mutual-cert auth). Endpoint/port come from the cert
# response; this port is the documented default and a fallback.
MQTT_DEFAULT_PORT = 8883
MQTT_KEEPALIVE = 60

# Poll interval while push is confirmed alive. Push delivers on-change updates
# instantly; the C3 topic has no heartbeat (verified on hardware, issue #55),
# so a slow backstop poll still catches anything missed while disconnected.
PUSH_BACKSTOP_SCAN_INTERVAL = 900
