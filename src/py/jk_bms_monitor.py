#!/usr/bin/env python3
"""JK BMS monitor: read a JK (Jikong) BMS over BLE and publish values to MQTT.

Queries the BMS with aiobmsble, prints voltage, current, power, balance current,
battery level, cycle charge, temperatures and individual cell voltages, and sends
all of them to the local Mosquitto broker.

Based on the official aiobmsble example:
    https://github.com/patman15/aiobmsble/blob/main/examples/minimal.py

The BLE connection is established once and kept open, so repeated reads do not
have to redo connect + GATT discovery + notification setup on every cycle.

Usage:
    python3 src/py/jk_bms_monitor.py                 # single read, print + publish
    python3 src/py/jk_bms_monitor.py --interval 10   # keep reading every 10 s
    python3 src/py/jk_bms_monitor.py --no-mqtt       # BLE only (debug)
    python3 src/py/jk_bms_monitor.py --interval 10 --reconnect   # old behaviour:
                                                   # disconnect after each read
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
from typing import Any, Final, Self

import paho.mqtt.client as mqtt
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from aiobmsble import BMSConfig, BMSSample
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


def _install_stop_handlers(stop: asyncio.Event) -> None:
    """Stop the read loop on SIGINT/SIGTERM so the BLE link is released cleanly.

    Without this, a SIGTERM (what systemd sends on service stop) kills the
    process without running the cleanup, leaving a dangling BLE connection on
    the controller. The BMS then keeps holding the link, stops advertising
    and the next start cannot find it in the scan.
    """
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)


async def find_bms_device() -> BLEDevice:
    """Scan for the JK BMS and return the found BLE device."""
    device: BLEDevice | None = await BleakScanner.find_device_by_address(
        BMS_ADDRESS, timeout=SCAN_TIMEOUT
    )
    if device is None:
        raise ConnectionError(f"BLE device {BMS_ADDRESS} not found (scan {SCAN_TIMEOUT:.0f} s)")
    logger.info("Found device: %s (%s)", device.name, device.address)
    return device


class BmsLink:
    """BLE link to the BMS that stays connected between reads.

    The connection (link setup, GATT discovery, notification setup and the
    "BMS ready" handshake) is established once by the first `read()` and then
    reused by all following reads (`BMSConfig(keep_alive=True)`), instead of a
    full re-login for every sample.  A link that got dropped by the BMS or the
    adapter is re-established transparently by the next read.

    Use it as an async context manager so the link is always closed again::

        async with BmsLink() as link:
            data = flatten(await link.read())
    """

    def __init__(self, keep_alive: bool = True) -> None:
        self._keep_alive: Final[bool] = keep_alive
        self._stack: Final[contextlib.AsyncExitStack] = contextlib.AsyncExitStack()
        self._device: BLEDevice | None = None
        self._bms: BMS | None = None
        self._entered: bool = False
        #: device info, read once when the link is established
        self.dev_info: dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        """True while the BLE connection to the BMS is up."""
        return self._bms is not None and self._bms.is_connected

    # ------------------------------------------------------ context manager ---
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # --------------------------------------------------------------- public ---
    async def read(self) -> BMSSample:
        """Return one fresh sample, connecting / reconnecting as needed."""
        bms: BMS = await self._ensure()
        return await bms.async_update()

    async def reset(self) -> None:
        """Force-close a possibly half-dead link; next `read()` scans again."""
        await self.close(reset=True)
        self._device = None
        self.dev_info = {}

    async def close(self, reset: bool = False) -> None:
        """Disconnect the BMS (idempotent); `reset` also drops stale links."""
        bms, self._bms = self._bms, None
        if bms is None:
            return
        try:
            if reset:
                await bms.disconnect(reset=True)
            if self._entered:
                self._entered = False
                await self._stack.aclose()  # leaves `async with BMS(...)` -> disconnect
            elif bms.is_connected:
                await bms.disconnect()
        except (BleakError, TimeoutError, OSError) as exc:
            logger.warning("BMS disconnect failed: %s: %s", type(exc).__name__, exc)

    # ------------------------------------------------------------ internals ---
    async def _ensure(self) -> BMS:
        """Return a BMS instance with an open connection, creating it on demand."""
        bms = self._bms
        if bms is not None:
            if not self.connected and self._keep_alive:
                logger.info("BMS link lost - reconnecting %s", BMS_ADDRESS)
            # a dropped link is re-established by `async_update()` itself
            return bms

        self._device = await find_bms_device()
        bms = BMS(
            ble_device=self._device,
            config=BMSConfig(keep_alive=self._keep_alive),
        )
        try:
            if self._keep_alive:
                # entering the context connects and keeps the link up until close()
                bms = await self._stack.enter_async_context(bms)
                self._entered = True
            self._bms = bms
            await self._read_dev_info(bms)
        except (BleakError, TimeoutError, ValueError, ConnectionError):
            await self.reset()
            raise

        logger.info(
            "Connected to BMS %s - %s",
            BMS_ADDRESS,
            "link stays open between reads" if self._keep_alive else "re-login on every read",
        )
        return bms

    async def _read_dev_info(self, bms: BMS) -> None:
        """Read (and sanity-check) the device information once per connection."""
        try:
            dev_info: dict[str, Any] = await bms.device_info()
        except (BleakError, TimeoutError, ValueError) as exc:
            logger.warning("device info unavailable: %s: %s", type(exc).__name__, exc)
            return

        # the BMS device-info frame carries only 8 chars of the serial,
        # the full one (BMS_SERIAL) is broadcast in the BLE advertised name
        name: str = (self._device.name if self._device else "") or ""
        serial: str = dev_info.get("serial_number", "")
        if BMS_SERIAL not in name and not BMS_SERIAL.startswith(serial):
            logger.warning("Serial mismatch: BMS reports '%s'/'%s', expected '%s'",
                           name, serial, BMS_SERIAL)
        dev_info["serial_number"] = BMS_SERIAL
        self.dev_info = dev_info


async def main() -> None:
    parser = argparse.ArgumentParser(description="Query JK BMS via BLE and publish to MQTT")
    parser.add_argument("--interval", type=float, default=0.0,
                        help="seconds between reads (0 = single read)")
    parser.add_argument("--count", type=int, default=0,
                        help="stop after N reads (0 = unlimited)")
    parser.add_argument("--reconnect", action="store_true",
                        help="disconnect after each read instead of staying "
                             "connected (slower, for debugging)")
    parser.add_argument("--no-mqtt", action="store_true", help="do not publish to MQTT")
    args = parser.parse_args()

    client: mqtt.Client | None = None
    if not args.no_mqtt:
        try:
            client = mqtt_connect()
        except OSError as exc:
            logger.error("MQTT connect to %s:%s failed: %s", MQTT_HOST, MQTT_PORT, exc)

    reads: int = 0
    errors: int = 0
    stop: asyncio.Event = asyncio.Event()
    _install_stop_handlers(stop)
    try:
        async with BmsLink(keep_alive=not args.reconnect) as link:
            while not stop.is_set():
                try:
                    data: dict[str, Any] = flatten(await link.read())
                    errors = 0
                    print_report(link.dev_info, data)
                    if client is not None:
                        mqtt_publish(client, data)
                except (BleakError, TimeoutError, ValueError, ConnectionError) as exc:
                    errors += 1
                    logger.error("BMS read failed (#%d consecutive): %s: %s",
                                 errors, type(exc).__name__, exc)
                    # the link may be half-dead: close it cleanly, the next
                    # iteration re-scans and re-connects from scratch
                    await link.reset()

                reads += 1
                if args.count and reads >= args.count:
                    break
                if args.interval <= 0:
                    break
                try:
                    # wait for the interval, but wake up immediately on a stop signal
                    await asyncio.wait_for(stop.wait(), timeout=args.interval)
                except asyncio.TimeoutError:
                    pass
    finally:
        if stop.is_set():
            logger.info("signal received - shutting down")
        if client is not None:
            client.publish(f"{MQTT_PREFIX}/availability", "offline", qos=0, retain=True)
            client.loop_stop()
            client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
