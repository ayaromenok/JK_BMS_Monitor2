#!/bin/sh
sudo systemctl stop jk_bms_monitor_py
sudo systemctl status jk_bms_monitor_py
sudo systemctl disable jk_bms_monitor_py
sudo rm /etc/systemd/system/jk_bms_monitor_py.service
