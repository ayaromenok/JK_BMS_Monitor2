#!/usr/bin/env python3
"""JK BMS monitor: read a JK (Jikong) BMS over BLE and publish values to MQTT.

Queries the BMS with aiobmsble, prints voltage, current, power, balance current,
battery level, cycle charge, temperatures and individual cell voltages, and sends
all of them to the local Mosquitto broker.

Based on the official aiobmsble example:
    https://github.com/patman15/aiobmsble/blob/main/examples/minimal.py

Usage:
    python3 src/py/jk_bms_monitor.py                 # single read, print + publish
    python3 src/py/jk_bms_monitor.py --interval 10   # keep reading every 10 s
    python3 src/py/jk_bms_monitor.py --no-mqtt       # BLE only (debug)
"""

import argparse
import asyncio
import json
import logging
import os
from typing import Any, Final

import paho.mqtt.client as mqtt
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from aiobmsble import BMSSample
from aiobmsble.bms.jikong_bms import BMS  # JK / Jikong smart BMS

# ------------------------------------------------------------------ config ---
BMS_ADDRESS: Final[str] = "C8:47:80:53:27:81"
BMS_SERIAL: Final[str] = "512251N28000371"
SCAN_TIMEOUT: Final[float] = 15.0

MQTT_HOST: Final[str] = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT: Final[int] = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER: Final[str | None] = os.getenv("MQTT_USER") or None
MQTT_PASS: Final[str | None] = os.getenv("MQTT_PASSWORD") or None
MQTT_PREFIX: Final[str] = os.getenv("MQTT_PREFIX", "jk_bms")

#: scalar values published as <prefix>/<key>
SCALARS: Final[tuple[str, ...]] = (
    "voltage",  # V
    "current",  # A (+ charging / - discharging)
    "power",  # W
    "balance_current",  # A
    "battery_level",  # %
    "cycle_charge",  # Ah
    "cycle_capacity",  # Wh
    "design_capacity",  # Ah
    "cycles",  # #
    "battery_health",  # %
    "delta_voltage",  # V
    "cell_count",  # #
    "temperature",  # degC (average reported by library)
    "problem",
    "problem_code",
    "chrg_mosfet",
    "dischrg_mosfet",
)

logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)
logger: logging.Logger = logging.getLogger("jk_bms_monitor")


# ------------------------------------------------------------------- mqtt ---
def mqtt_connect() -> mqtt.Client:
    """Connect to the local broker, set up availability LWT and return the client."""
    client: mqtt.Client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="jk-bms-test",
        protocol=mqtt.MQTTv311,
    )
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(f"{MQTT_PREFIX}/availability", "offline", qos=0, retain=True)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    client.publish(f"{MQTT_PREFIX}/availability", "online", qos=0, retain=True)
    return client


def mqtt_publish(client: mqtt.Client, data: dict[str, Any]) -> None:
    """Publish the flattened sample as retained topics (JSON blob + one topic per value)."""
    client.publish(f"{MQTT_PREFIX}/data", json.dumps(data), qos=0, retain=True)
    for key, value in data.items():
        if isinstance(value, (list, tuple)):
            for idx, item in enumerate(value):
                client.publish(
                    f"{MQTT_PREFIX}/{key}/{idx}", _to_str(item), qos=0, retain=True
                )
        else:
            client.publish(f"{MQTT_PREFIX}/{key}", _to_str(value), qos=0, retain=True)
    logger.info("published %d values to mqtt://%s:%s/%s/...", len(data), MQTT_HOST, MQTT_PORT, MQTT_PREFIX)


def _to_str(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


# --------------------------------------------------------------------- ble ---
def flatten(sample: BMSSample) -> dict[str, Any]:
    """Convert a BMS sample into plain, JSON/MQTT-friendly values."""
    data: dict[str, Any] = {k: sample[k] for k in SCALARS if k in sample}

    # temperatures: one entry per sensor, labelled by its source (battery_1, mosfet, ...)
    if temps := sample.get("temp_values"):
        data["temp"] = [round(float(t), 1) for t in temps]
        for idx, temp in enumerate(temps):
            data[f"temp_{temp.type.name.lower()}_{idx}"] = round(float(temp), 1)

    if cells := sample.get("cell_voltages"):
        data["cell_voltages"] = [round(v, 3) for v in cells]

    return data


def print_report(dev_info: dict[str, Any], data: dict[str, Any]) -> None:
    """Print a human readable report of one BMS response."""
    print("\n" + "=" * 60)
    print(
        f"JK BMS  {BMS_ADDRESS}  serial={dev_info.get('serial_number', 'n/a')}  "
        f"model={dev_info.get('model', 'n/a')}  fw={dev_info.get('sw_version', 'n/a')}"
    )
    print("-" * 60)
    for label, key, unit in (
        ("Pack voltage", "voltage", "V"),
        ("Current", "current", "A"),
        ("Power", "power", "W"),
        ("Balance current", "balance_current", "A"),
        ("Battery level", "battery_level", "%"),
        ("Cycle charge", "cycle_charge", "Ah"),
        ("Cycles", "cycles", "#"),
        ("Temperature", "temperature", "degC"),
        ("Delta voltage", "delta_voltage", "V"),
    ):
        if key in data:
            value = data[key]
            value = round(value, 2) if isinstance(value, float) else value
            print(f"{label:<17}: {value:>10} {unit}")

    if temps := data.get("temp"):
        print(f"{'Sensors':<17}: " + ", ".join(f"T{i + 1}={t} °C" for i, t in enumerate(temps)))

    cells = data.get("cell_voltages") or []
    if cells:
        print(f"{'Cell voltages':<17}: {len(cells)} cells, "
              f"min={min(cells):.3f} V  max={max(cells):.3f} V")
    else:
        print(f"{'Cell voltages':<17}: n/a")
    for row in range(0, len(cells), 8):
        chunk = ", ".join(f"{v:.3f}" for v in cells[row: row + 8])
        print(f"  [{row + 1:>2}-{row + len(cells[row: row + 8]):>2}] {chunk}")

    print(f"{'MOSFets':<17}: charge={'ON' if data.get('chrg_mosfet') else 'OFF'}, "
          f"discharge={'ON' if data.get('dischrg_mosfet') else 'OFF'}")
    print(f"{'Problem':<17}: {bool(data.get('problem'))} (code={data.get('problem_code', 0)})")
    print("=" * 60)


async def read_bms() -> tuple[dict[str, Any], dict[str, Any]]:
    """Scan for the JK BMS, query it once and return (device_info, flattened data)."""
    device: BLEDevice | None = await BleakScanner.find_device_by_address(
        BMS_ADDRESS, timeout=SCAN_TIMEOUT
    )
    if device is None:
        raise ConnectionError(f"BLE device {BMS_ADDRESS} not found (scan {SCAN_TIMEOUT:.0f} s)")
    logger.info("Found device: %s (%s)", device.name, device.address)

    async with BMS(ble_device=device) as bms:
        dev_info: dict[str, Any] = await bms.device_info()
        sample: BMSSample = await bms.async_update()

    # the BMS device-info frame carries only 8 chars of the serial,
    # the full one (BMS_SERIAL) is broadcast in the BLE advertised name
    serial: str = dev_info.get("serial_number", "")
    if BMS_SERIAL not in (device.name or "") and not BMS_SERIAL.startswith(serial):
        logger.warning("Serial mismatch: BMS reports '%s'/'%s', expected '%s'",
                       device.name, serial, BMS_SERIAL)
    dev_info["serial_number"] = BMS_SERIAL
    return dev_info, flatten(sample)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Query JK BMS via BLE and publish to MQTT")
    parser.add_argument("--interval", type=float, default=0.0,
                        help="seconds between reads (0 = single read)")
    parser.add_argument("--count", type=int, default=0,
                        help="stop after N reads (0 = unlimited)")
    parser.add_argument("--no-mqtt", action="store_true", help="do not publish to MQTT")
    args = parser.parse_args()

    client: mqtt.Client | None = None
    if not args.no_mqtt:
        try:
            client = mqtt_connect()
        except OSError as exc:
            logger.error("MQTT connect to %s:%s failed: %s", MQTT_HOST, MQTT_PORT, exc)

    reads: int = 0
    try:
        while True:
            try:
                dev_info, data = await read_bms()
                print_report(dev_info, data)
                if client is not None:
                    mqtt_publish(client, data)
            except (BleakError, TimeoutError, ValueError, ConnectionError) as exc:
                logger.error("BMS read failed: %s: %s", type(exc).__name__, exc)

            reads += 1
            if args.count and reads >= args.count:
                break
            if args.interval <= 0:
                break
            await asyncio.sleep(args.interval)
    finally:
        if client is not None:
            client.publish(f"{MQTT_PREFIX}/availability", "offline", qos=0, retain=True)
            client.loop_stop()
            client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
