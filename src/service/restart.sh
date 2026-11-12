#!/bin/sh
sudo systemctl stop jk_bms_monitor_py
sudo systemctl start jk_bms_monitor_py
sudo systemctl status jk_bms_monitor_py