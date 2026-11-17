"""Constants for the iLetComfort integration."""

DOMAIN = "iletcomfort"
PLATFORMS = [
    "climate",
    "sensor",
    "switch",
    "select",
    "binary_sensor",
    "number",
    "water_heater",
]

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

# --- Operation modes / MQTT real-time push (issue #55) ---------------------
# Legacy v0.9-v0.11 option. True migrates at runtime to phone-app mode and is
# removed the next time the options form is saved.
CONF_ENABLE_MQTT_PUSH = "enable_mqtt_push"
DEFAULT_ENABLE_MQTT_PUSH = False

# HA-primary is the default and preserves the original polling + write behavior.
# Phone-app mode uses session-independent certificate push only: no periodic
# account polling and no writes that could evict the official app's session.
CONF_OPERATION_MODE = "operation_mode"
OPERATION_MODE_HA_PRIMARY = "ha_primary"
OPERATION_MODE_PHONE_APP = "phone_app"
DEFAULT_OPERATION_MODE = OPERATION_MODE_HA_PRIMARY

# Opt-in standard Midea C3 control for sn8 171H120F units whose status frame
# matches the C3 basic layout. Hardware-validated on a Rotenso Aquami only, so
# it stays off until the owner confirms their unit behaves the same way.
CONF_C3_BASIC_CONTROL = "c3_basic_control"
DEFAULT_C3_BASIC_CONTROL = False

# AWS IoT MQTT broker (TLS, mutual-cert auth). Endpoint/port come from the cert
# response; this port is the documented default and a fallback.
MQTT_DEFAULT_PORT = 8883
MQTT_KEEPALIVE = 60

# C3 devices publish full status snapshots while idle, commonly around every
# 15 minutes. Captures also show occasional missed cycles, so require three
# missed cycles before treating an otherwise-connected push channel as stale.
MQTT_STATUS_STALE_AFTER = 45 * 60
