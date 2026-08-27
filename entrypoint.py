#!/usr/bin/env python3
"""
entrypoint.py

The one thing that differs between the two ways this image gets run:

- Plain container (`docker run`, or your own compose file): config
  arrives as real environment variables you set directly -- global
  settings as flat vars (MQTT_HOST etc.), per-inverter settings as a JSON
  array in INVERTERS_JSON (see opendtu_fronius_meter.py's module
  docstring for its shape). Nothing for this script to do -- it falls
  straight through to main().
- Home Assistant Supervisor App (add-on): config arrives as JSON at
  /data/options.json, built from whatever the user filled in on the App's
  Configuration tab (shaped by config.yaml's `options`/`schema`). Supervisor
  does NOT set environment variables for these -- so this script translates
  options.json's global keys into the same flat env vars, and its
  `inverters` list-of-dicts option into INVERTERS_JSON, then falls through
  to the exact same main().

Either way, opendtu_fronius_meter.py itself never needs to know which mode
it's running in -- it only ever reads environment variables, exactly as it
always has for standalone container use.
"""

import json
import os

OPTIONS_PATH = "/data/options.json"

# Maps a global config.yaml option key to the environment variable
# GlobalConfig reads. Keep this in sync with config.yaml's top-level
# `options`/`schema` block whenever one changes. Per-inverter keys (the
# `inverters` list's own sub-fields) are handled separately below, since
# they all collapse into a single INVERTERS_JSON env var rather than one
# env var each.
GLOBAL_OPTION_TO_ENV = {
    "mqtt_host": "MQTT_HOST",
    "mqtt_port": "MQTT_PORT",
    "mqtt_username": "MQTT_USERNAME",
    "mqtt_password": "MQTT_PASSWORD",
    "update_interval_seconds": "UPDATE_INTERVAL_SECONDS",
    "log_level": "LOG_LEVEL",
    "tcp_listen_port": "TCP_LISTEN_PORT",
    "meter_manufacturer": "METER_MANUFACTURER",
    "meter_model": "METER_MODEL",
    "meter_serial": "METER_SERIAL",
    "meter_device_address": "METER_DEVICE_ADDRESS",
}


def apply_supervisor_options():
    """If running as a Supervisor App, translate /data/options.json into
    the environment variables opendtu_fronius_meter.py expects. No-op
    (and safe to call) when that file doesn't exist, e.g. as a plain
    container, where config already arrives as real env vars."""
    if not os.path.exists(OPTIONS_PATH):
        return
    with open(OPTIONS_PATH) as f:
        options = json.load(f)
    for option_key, env_key in GLOBAL_OPTION_TO_ENV.items():
        # Only options the user actually set/left non-empty in the App's
        # Configuration UI get translated -- anything absent falls back to
        # opendtu_fronius_meter.py's own os.environ.get(..., default).
        value = options.get(option_key)
        if value not in (None, ""):
            os.environ[env_key] = str(value)
    # `inverters` is a list of dicts (one per OpenDTU inverter feeding the
    # combined meter, each with its own topics/phase -- see config.yaml).
    # opendtu_fronius_meter.py reads this whole list as one JSON blob
    # rather than as individual env vars, since its length is variable.
    os.environ["INVERTERS_JSON"] = json.dumps(options.get("inverters") or [])


if __name__ == "__main__":
    apply_supervisor_options()
    import opendtu_fronius_meter
    opendtu_fronius_meter.main()
