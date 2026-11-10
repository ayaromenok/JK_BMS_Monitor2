#!/bin/sh
sudo cp ./jk_bms_monitor_py.service /etc/systemd/system/
sudo systemctl start jk_bms_monitor_py
sudo systemctl status jk_bms_monitor_py
sudo systemctl enable jk_bms_monitor_py