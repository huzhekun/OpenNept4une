# Power-loss recovery

When the power fails during a print, the printer saves where the print
stopped. After the power returns it offers to continue the print from that
point. It prints the line that was in progress again from its start, so there
is no gap. That segment may be slightly over-extruded.

The code is `img-config/plr/plr.py`, which only uses the Python standard
library. It builds on the power-loss monitor ([power-monitor.md](power-monitor.md)).

## How it works

### When the power fails

`power_monitor.sh` runs the hooks in `/etc/power_monitor.d` before it powers
off. `50-plr-checkpoint` links to `plr-checkpoint.sh`, which runs
`plr.py checkpoint`:

1. It asks Klipper (over `~/printer_data/comms/klippy.sock`) for the print
   state: `print_stats`, `virtual_sdcard`, `motion_report`, `toolhead`,
   `gcode_move`, `extruder`, `heater_bed`, `fan`, `bed_mesh`, `exclude_object`,
   `firmware_retraction` and `pause_resume`. The answer comes at Klipper's next
   status update, within 250 ms.
2. If a print is running, it sends an emergency stop straight away. The
   toolhead then stops within a few milliseconds of the position it reported,
   instead of moving on until the 24 V supply collapses. The stop also turns
   the heaters off.
3. It writes the answer to `~/printer_data/plr/checkpoint.json` and syncs it
   to the eMMC.

If no print is running, it saves nothing and leaves any earlier checkpoint in
place. On a Neptune 4 Max this takes about 0.4 s of the 5-6 s that the
super-capacitor keeps the board running.

### Finding the resume point

Klipper reads the G-code file up to about 2 s ahead of the motion, so the file
position in the checkpoint is past the point where the nozzle actually was.
At boot, `plr-prepare.service` runs `plr.py prepare` before Klipper starts.
It follows the file the way Klipper's `gcode_move` does (absolute and relative
modes, `G92`, arcs, firmware retraction with `G10`/`G11`) and records each move
near the file position. It then looks for the move that contains the toolhead's
live position from `motion_report`:

* The live X/Y must lie on the move's path (0.2 mm).
* The live Z must be near the move's Z (1 mm; the bed mesh and Z offset are in
  the live value).
* The live extruder position must lie within the move's extrusion, and agree
  with how far along the path the toolhead is. The extruder position is what
  tells apart the moves that pass the same spot, for example two layers, or a
  retraction followed by a travel.

Of the moves that fit, it takes the first one that hadn't finished. If none fit
with the extruder check, it falls back to the position alone and logs a
warning. If nothing fits at all, resuming isn't offered.

The file position is also checked against Klipper's `gcode_position`, which
catches a file that doesn't match what Klipper did (for example when a macro in
the file moved the toolhead).

If the print was paused when the power failed, it resumes at the file position
itself, because Klipper had finished every move before it.

Reading the file takes about 2 s on the printer for the last 512 KiB, plus
about 0.5 s per further MiB. The result is cached in the generated config, so
only the first boot after a power cut is delayed.

### The resume macros

`prepare` writes `~/printer_data/config/plr/plr.cfg`, which `printer.cfg`
includes with `[include plr/*.cfg]`. It contains:

| Section | Purpose |
|---|---|
| `[gcode_macro PLR_RESUME]` | Resumes the print. Its variables `checkpoint`, `filename` and `error` describe it. |
| `[gcode_macro PLR_DISCARD]` | Drops the recovery data. |
| `[delayed_gcode PLR_WATCH]` | Drops the recovery data if another print is started instead. |
| `[delayed_gcode PLR_SPEED]` | Restores the speed once the resumed layer is done (see step 7). |
| `[bed_mesh plr]` | The bed mesh that was active, usually the adaptive mesh from the print start (see below). |
| `[save_variables]` | Stores `plr_handled` in `~/printer_data/plr/variables.cfg`. Left out if the configuration already has a `[save_variables]` section; then that one is used. |

`PLR_RESUME`, `PLR_DISCARD` and starting another print each set
`plr_handled` to the checkpoint's id. On the next boot `prepare` then removes
`plr.cfg`. If the plan failed, `PLR_RESUME` only reports why, and
`PLR_DISCARD` still works.

The remaining layers have to be printed with the same bed mesh as the ones
already on the bed, or there is a step between them. The bed can't be probed
again because the part is in the way, and `BED_MESH_CALIBRATE ADAPTIVE=1`
doesn't save its mesh as a profile. The checkpoint holds the mesh Klipper had
in memory, though: `bed_mesh` reports the interpolated grid
(`mesh_matrix`) that it corrects Z with. `[bed_mesh plr]` stores that grid
with interpolation turned off (`algo: direct`, `mesh_pps: 0`), so loading it
gives the same correction, to the 6 decimals Klipper reports. Fade
(`fade_start`, `fade_end`) comes from `[bed_mesh]` as before.

`PLR_RESUME` does the following:

1. Clears the bed mesh and the G-code offset, and starts heating the bed.
2. Tells Klipper where the axes are with `SET_KINEMATIC_POSITION`. Z keeps its
   place without power (lead screws), and X/Y are homed later.
3. Heats the bed, then the nozzle, back to the print temperatures before
   anything moves, so that the nozzle comes free of the plastic it stopped in.
   The bed goes first so that the nozzle isn't hot against the print for
   longer than needed. Then it lifts the nozzle 5 mm.
4. Homes X and Y. Z isn't homed, because the probe would hit the print.
5. Restores the G-code offset (baby-stepping included) and loads the saved
   mesh.
6. At the back-left corner it waits for the nozzle, purges 30 mm (or the
   `PURGE` parameter) and retracts.
7. Restores the flow factor, velocity limits, pressure advance, firmware
   retraction settings and fan speed: first the values in the checkpoint, then
   the ones the file had set at the resume line. The rest of the layer the
   print resumes in is printed at 25% of the speed at the power loss (`M220`;
   the `SPEED` parameter changes it), for the new plastic to bond to the cooled
   layer below. `plr.py` finds where the next layer starts (the first extruding
   move at a higher Z, so z-hops don't count), and `PLR_SPEED` restores the
   speed when Klipper reaches it or the print ends, unless the speed was
   changed in the meantime.
8. Travels to where the resume line starts, lowers the nozzle to its Z, and
   sets the extruder up the way the file expects there: primed, or retracted
   by the slicer or with `G10`. Sets the E position, the absolute or relative
   modes and the feed rate.
9. Selects the file with `M23`, redefines the objects for `EXCLUDE_OBJECT`
   (`M23` clears them), excludes the objects that had been cancelled, and
   continues the object that was being printed. Then `M26` sets the file
   position and `M24` starts the print.

The touch screen asks whether to resume (see below). Without it, run
`PLR_RESUME` or `PLR_DISCARD` from Mainsail or Fluidd. `PLR_RESUME PURGE=0`
skips the purge, and `PLR_RESUME SPEED=100` resumes at full speed.

### Touch screen

The display connector
([huzhekun/openneptune_display_connector](https://github.com/huzhekun/openneptune_display_connector))
subscribes to `gcode_macro PLR_RESUME` and `save_variables`. While the
checkpoint hasn't been handled and no print is running, it shows the stock
"Power Loss Recovery" page (`continueprint`) once, with the file name or the
reason the print can't be resumed. Confirm runs `PLR_RESUME`, Cancel runs
`PLR_DISCARD`.

## Limits

* While the power is off, the nozzle rests hot against the print, and usually
  leaves a small blob there. It can't be retracted or lifted first: Klipper has
  sent the motion up to 2 s ahead to the mainboard, which only an emergency
  stop interrupts, and the motors lose power with the 24 V supply.
* Z isn't homed. When a stepper loses power, it settles to a full step, and
  when it's enabled again the driver may pull it to a different one. On the
  Max that's up to about 0.08 mm, which shows as a line in the print.
* The part has to stay stuck to the cooling bed. Large parts that warp or come
  off can't be continued.
* Only files printed from `~/printer_data/gcodes` (virtual SD card) can be
  resumed. The file must not be changed or deleted before resuming.
* G92 offsets for X, Y or Z in the print aren't supported.
* File names containing `#`, `{` or `}`, or ` ;`, can't be used in the
  generated macro, so those prints can't be resumed.
* `PLR_RESUME` marks the checkpoint handled before it moves anything. If the
  resume fails part way (a heater error, say), it can't be retried, because
  the saved Z position no longer matches the toolhead.
* Lines that Klipper turns into several moves of its own (macros) can't be
  matched. A cut inside a macro, such as `M600`, may not be resumable.

## Installing

`OpenNept4une.sh` sets this up as part of its fixes (also run on update):

* links `/etc/power_monitor.d/50-plr-checkpoint` to `plr-checkpoint.sh`
* installs and enables `plr-prepare.service` (oneshot, before `klipper.service`)
* creates `~/printer_data/plr` and `~/printer_data/config/plr`
* adds `[include plr/*.cfg]` to `printer.cfg` before its first section, if it's
  missing. New configurations from `printer-confs/base.cfg` have it already.

## Testing

Unit tests, including simulated cuts at random places in a real file:

```sh
python3 -m unittest img-config/plr/test_plr.py
PLR_TEST_GCODE=~/printer_data/gcodes/some_print.gcode PLR_TEST_CUTS=20 \
  python3 -m unittest img-config/plr/test_plr.py
```

On the printer, without cutting the power:

1. Start a print and let it run for a few layers.
2. `sudo /etc/power_monitor.d/50-plr-checkpoint` saves the checkpoint and
   stops the printer, as a power cut would. (`plr.py checkpoint --dry-run
   --output /tmp/cp.json` saves it without stopping.)
3. `python3 ~/OpenNept4une/img-config/plr/plr.py plan ~/printer_data/plr/checkpoint.json --cfg`
   shows the resume point and the generated macros.
4. Reboot (or run `plr.py prepare` and `FIRMWARE_RESTART`). The screen
   should offer to resume.
5. Confirm, and watch the first resumed lines.

The `prepare` log is in `journalctl -u plr-prepare`, the checkpoint log in
`journalctl -u power_monitor`.
