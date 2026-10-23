# Power-loss monitor

`img-config/power_monitor.sh` runs as `power_monitor.service` (root). The
board keeps running on its super-capacitor for a few seconds after the power
supply drops. The monitor uses that time to run hooks (for example to
checkpoint a print), sync, and power off cleanly.

## Power-sense pins by board

The pins differ between boards, so the script picks a profile from the model
in `/boot/.OpenNept4une.txt`. Set `PROFILE=plus-max` or `PROFILE=dual` to
override it.

| Profile | Models | Power lost when | Source |
|---|---|---|---|
| `plus-max` | N4 Plus, N4 Max | `gpiochip2` line 3 (GPIO2_A3, sysfs `gpio67`) goes active | Elegoo Plus/Max firmware APP 1.2.3.4 (`monitor_GPIO2_A3`) |
| `dual` | N4, N4 Pro | `gpiochip1` line 10 rises or line 19 falls | Elegoo N4 Pro firmware (2023), sysfs `gpio42` / `gpio51` |

Both profiles drive `gpiochip1` line 21 (`gpio53`, "super-capacitor charging")
high while running. The line stays high after the monitor exits.

### Measurements on a Neptune 4 Max (board v2.0)

* `gpiochip1` line 10 toggles about 42,000 times a second with the power on,
  and line 19 never changes during a power cut. Using them on the Max made the
  monitor fail at startup or shut down on noise.
* `gpio67` is inactive with the power on. When the supply fails, it bounces for
  about 2 ms and then stays active until power returns. The monitor has
  libgpiod v2 debounce it in the kernel (`gpiomon -p 50ms`), so it gets one
  edge 50 ms after the line settles.
* With the printer idle, the 24 V rail takes several seconds to collapse after
  the PSU is switched off. A 3 s switch-off doesn't register.
* After a loss is detected the super-capacitor keeps the board up for about
  5-6 s. In a test, `systemctl poweroff` had stopped the services, copied the
  RAM logs to the eMMC and flushed the journal 3.3 s after detection.

Elegoo's firmware confirms a loss with three readings about 35 ms apart, then
runs `sync`, waits 2 s and calls `shutdown -h now`.

## Behaviour

1. Drive the super-capacitor line high.
2. `plus-max`: if the loss line already reads active, log a warning and wait
   for it to clear, so a wrong pin can't cause a shutdown loop. Then watch both
   edges with one debounced `gpiomon`. Each rising edge is a verified loss.
   (libgpiod v1 has no debounce, so there the rising edge must not be followed
   by a falling one within `CONFIRM_SECONDS`.)
3. `dual`: wait for an edge on either line, then sample both lines
   `VERIFY_SAMPLES` times and require a majority to agree. Glitches loop
   back to monitoring.
4. On a verified loss, run every executable in `HOOK_DIR`
   (`/etc/power_monitor.d`) in name order, each under `HOOK_TIMEOUT` (2 s).
   Then run `sync` and `systemctl poweroff`, and keep holding the
   super-capacitor line until systemd stops the script.

Hooks run as root with nothing on stdin. Keep them well under a second: the
hold-up time also has to cover the sync. The power-loss recovery hook
(`50-plr-checkpoint`, see [power-loss-recovery.md](power-loss-recovery.md))
takes about 0.4 s.

## Settings

All settings are environment variables. Set them with a drop-in such as
`/etc/systemd/system/power_monitor.service.d/override.conf`:

| Variable | Default | Meaning |
|---|---|---|
| `PROFILE` | from the model | `plus-max` or `dual` |
| `DEBOUNCE_MS` | 50 | `plus-max` kernel debounce |
| `CONFIRM_SECONDS` | 0.1 | `plus-max` on libgpiod v1 |
| `VERIFY_SAMPLES`, `VERIFY_INTERVAL_MS` | 5, 40 | `dual` verification |
| `HOOK_DIR`, `HOOK_TIMEOUT` | `/etc/power_monitor.d`, 2 | hooks |
| `DRY_RUN` | false | watch the real pins, but only log what would happen |
| `DEBUG` | false | no GPIO access at all |
| `LOG_FILE` | (none) | extra log file, synced after every line |

## Testing

Test with a dry run first. Run it as a transient unit so it doesn't touch the
installed service:

```sh
sudo systemctl stop power_monitor
sudo systemd-run --unit=power-monitor-dryrun --collect \
  -E DRY_RUN=true -E LOG_FILE=/home/mks/power_monitor_dryrun.log \
  ~/OpenNept4une/img-config/power_monitor.sh
```

Switch the PSU off for about 15 s, then back on. The log should show one
"Power loss signalled", "verified" and "[DRY RUN] Would execute" sequence,
and later one "Power OK again". With `DRY_RUN` the board isn't shut down, so
it runs until the super-capacitor is empty.

To watch the installed service: `journalctl -u power_monitor -f`.
