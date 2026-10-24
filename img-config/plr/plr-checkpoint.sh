#!/bin/sh
# power_monitor hook (installed in /etc/power_monitor.d): save the running
# print for power-loss recovery and stop the motion.
exec /usr/bin/python3 -I "$(dirname "$(readlink -f "$0")")/plr.py" checkpoint
