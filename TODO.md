
- [x] add to application ./src/py/jk_bms_monitor.py possibility to stay connected to
  Bluetooth JK BMS instead of relogin on every query to BMS. Test on real hardware.
  (done: BmsLink with BMSConfig(keep_alive=True), --reconnect keeps old behaviour,
   tested on rpi3 + JK-B1A8S20P 2025-11-11: ~0 s per read vs ~4 s with re-login,
   link loss is detected and re-connected transparently;
   SIGTERM/SIGINT now release the BLE link cleanly, so service stop/start
   does not leave a dangling connection hiding the BMS from the scan)

- add '--watch' option as alternative to '--count 0'
- add option to setup BLE MAC address and BLE/BMS serail number
- add options to select MQTT server settings
- display current values to servcie status (i.e. `systemctl status jk_bms__monitor_py`)
- update and cleanup README.md