# JK BMS Monitor — BLE test + MQTT publisher

Single-file monitor (`src/py/jk_bms_monitor.py`) that queries a JK (Jikong) smart BMS over Bluetooth LE with
[aiobmsble](https://github.com/patman15/aiobmsble) (pattern taken from
`examples/minimal.py`) and publishes the values to the local Mosquitto broker.

## Device

| item | value |
|------|-------|
| BLE MAC | `C8:47:80:53:27:81` |
| Serial / BLE name | `512251N28000371` |
| Model (read from BMS) | `JK-B1A8S20P`, fw 19.27 |

## Requirements

Already installed in `/home/pi/sdk/venv`: `aiobmsble` 0.33.0, `paho-mqtt` 2.1.0, `bleak`.
Mosquitto must listen on `127.0.0.1:1883` and the Bluetooth adapter must be up
(`bluetoothctl power on`).

## Run

```bash
cd /home/pi/projects/JK_BMS_Monitor2
python3 src/py/jk_bms_monitor.py                  # one read: print + publish
python3 src/py/jk_bms_monitor.py --interval 10    # continuous, every 10 s
python3 src/py/jk_bms_monitor.py --count 5 --interval 5
python3 src/py/jk_bms_monitor.py --no-mqtt        # BLE only
```

Verify the MQTT side:

```bash
mosquitto_sub -h 127.0.0.1 -t 'jk_bms/#' -v
```

## Values

voltage, current, power, balance_current, battery_level, cycle_charge, cycle_capacity,
design_capacity, cycles, battery_health, delta_voltage, cell_count, temperature (+ each
sensor), cell voltages (each cell), problem / problem_code, charge & discharge MOSFET state.

## MQTT layout (prefix `jk_bms`, all retained)

| topic | content |
|-------|---------|
| `jk_bms/data` | full sample as one JSON document |
| `jk_bms/<key>` | single value, e.g. `jk_bms/voltage`, `jk_bms/power` |
| `jk_bms/cell_voltages/<n>` | cell voltage n (0-based) |
| `jk_bms/temp/<n>`, `jk_bms/temp_<source>_<n>` | temperature sensor n |
| `jk_bms/availability` | `online` / `offline` (LWT) |

Environment overrides: `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASSWORD`, `MQTT_PREFIX`.

## Notes

* Current sign: positive = charging, negative = discharging (library convention);
  `power` is derived as `voltage * current`.
* The BMS device-info frame returns only the first 8 characters of the serial
  (`512251N2`); the full serial is the BLE advertised name, so the check accepts either.
