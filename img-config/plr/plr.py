#!/usr/bin/env python3
"""Power-loss recovery (PLR) for OpenNept4une.

checkpoint  Run by power_monitor.sh (as a hook in /etc/power_monitor.d) right
            after a power cut is detected. Reads the print state from
            Klipper's API socket, emergency-stops the motion and stores the
            state in <printer_data>/plr/checkpoint.json.
prepare     Run at boot before Klipper starts. If there is a checkpoint that
            hasn't been resumed or discarded, it finds where the print stopped
            and writes <printer_data>/config/plr/plr.cfg with PLR_RESUME and
            PLR_DISCARD macros. Otherwise it removes that file.
plan        Print the resume plan for a checkpoint (for testing).

The print resumes from the start of the G-code line that was executing when
the power failed, so that line is printed again in full. That line is found
by matching Klipper's live toolhead and extruder position against the moves
read from the file, rather than trusting the file position, which runs up to
a couple of seconds ahead of the motion.

Only the Python standard library is used.
"""

import argparse
import collections
import configparser
import copy
import json
import math
import os
import re
import socket
import sys
import time

DATA_DIR = os.environ.get("PLR_DATA_DIR", "/home/mks/printer_data")
CHECKPOINT_VERSION = 1
# Klipper objects saved with the checkpoint
QUERY_OBJECTS = (
    "print_stats", "virtual_sdcard", "motion_report", "toolhead", "gcode_move",
    "extruder", "heater_bed", "fan", "bed_mesh", "exclude_object",
    "firmware_retraction", "pause_resume",
)
# Moves kept while reading the file. Klipper queues about 2 s of motion, which
# with short arc and infill segments can still be a few hundred lines.
MOVE_WINDOW = 20000
# Moves are only recorded (and state snapshots taken) this close to the file
# position, which makes reading the rest of the file faster
WINDOW_BYTES = 512 * 1024
SNAPSHOT_LINES = 2000
TOL_XY = 0.2     # mm, live position to a move's path
TOL_E = 0.02     # mm of filament
TOL_Z = 1.0      # mm, covers the bed mesh and z offset
TOL_ALONG = 0.5  # mm, position along a move from XY vs from the extruder
PURGE_LENGTH = 30.0
LIFT = 5.0
# Below this the checkpoint has no usable nozzle temperature
MIN_EXTRUDE_TEMP = 170.0
# Speed (percent of the speed at the power loss) for the rest of the layer the
# print resumes in, and how far ahead the next layer is looked for
RESUME_SPEED = 25
NEXT_LAYER_BYTES = 8 * 1024 * 1024


class NotStarted(Exception):
    """The power failed before the print had printed anything."""


class PlanError(Exception):
    pass


def paths(data_dir=DATA_DIR):
    return {
        "socket": os.path.join(data_dir, "comms", "klippy.sock"),
        "state_dir": os.path.join(data_dir, "plr"),
        "checkpoint": os.path.join(data_dir, "plr", "checkpoint.json"),
        # Power-loss recovery is off while this exists (the screen's setting)
        "disabled": os.path.join(data_dir, "plr", "disabled"),
        "variables": os.path.join(data_dir, "plr", "variables.cfg"),
        "config_dir": os.path.join(data_dir, "config"),
        "cfg": os.path.join(data_dir, "config", "plr", "plr.cfg"),
    }


def log(msg):
    print("plr: " + msg, flush=True)


# ===== Checkpoint (power_monitor hook) =====

class KlippyClient:
    def __init__(self, path, timeout=1.5):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.buf = b""
        self.next_id = 1

    def send(self, method, params=None):
        req_id = self.next_id
        self.next_id += 1
        msg = {"id": req_id, "method": method, "params": params or {}}
        self.sock.sendall(json.dumps(msg).encode() + b"\x03")
        return req_id

    def response(self, req_id):
        while True:
            while b"\x03" in self.buf:
                raw, self.buf = self.buf.split(b"\x03", 1)
                msg = json.loads(raw)
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(msg["error"].get("message", msg["error"]))
                    return msg["result"]
            data = self.sock.recv(65536)
            if not data:
                raise RuntimeError("Klipper closed the connection")
            self.buf += data


def write_atomic(path, text, owner_of=None):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if owner_of is not None and os.geteuid() == 0:
        st = os.stat(owner_of)
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)
    dfd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def cmd_checkpoint(args):
    p = paths(args.data_dir)
    if os.path.exists(p["disabled"]):
        log("power-loss recovery is turned off, nothing to save")
        return 0
    try:
        client = KlippyClient(p["socket"])
        result = client.response(client.send(
            "objects/query", {"objects": {o: None for o in QUERY_OBJECTS}}))
    except (OSError, RuntimeError, ValueError) as e:
        log("can't query Klipper: %s" % e)
        return 1
    received = time.monotonic()
    status = result["status"]
    state = status.get("print_stats", {}).get("state")
    sd = status.get("virtual_sdcard", {})
    printing = state == "printing" and sd.get("is_active")
    if not printing and state != "paused":
        log("no print running (state %s), nothing to save" % state)
        return 0
    # Stop the motion now, so the position read above is (within a few ms)
    # where the toolhead stops. Otherwise it keeps moving until the 24 V rail
    # collapses. This also turns the heaters off.
    estop_delay = None
    if printing and not args.dry_run:
        req = client.send("emergency_stop")
        estop_delay = time.monotonic() - received
        try:
            client.sock.settimeout(0.3)
            client.response(req)
        except (OSError, RuntimeError, ValueError):
            pass
    file_path = sd.get("file_path") or ""
    try:
        st = os.stat(file_path)
        file_info = {"file_size": st.st_size, "file_mtime": st.st_mtime}
    except OSError:
        file_info = {}
    checkpoint = {
        "version": CHECKPOINT_VERSION,
        "id": int(time.time() * 1000),
        "time": time.time(),
        "state": state,
        "filename": status["print_stats"].get("filename"),
        "file_path": file_path,
        "file_position": sd.get("file_position"),
        "eventtime": result.get("eventtime"),
        "estop_delay": estop_delay,
        "dry_run": bool(args.dry_run),
        "status": status,
    }
    checkpoint.update(file_info)
    if not os.path.isdir(p["state_dir"]):
        os.makedirs(p["state_dir"])
        if os.geteuid() == 0:
            dst = os.stat(args.data_dir)
            os.chown(p["state_dir"], dst.st_uid, dst.st_gid)
    target = args.output or p["checkpoint"]
    write_atomic(target, json.dumps(checkpoint), owner_of=args.data_dir)
    log("saved %s at byte %s (%s)%s" % (
        checkpoint["filename"], checkpoint["file_position"], state,
        "" if estop_delay is None else ", emergency stop after %.0f ms" % (estop_delay * 1000)))
    return 0


# ===== G-code file model =====

Move = collections.namedtuple(
    "Move", "offset line x0 y0 z0 x1 y1 z1 e0 e1 arc")


def _num(s):
    try:
        return float(s)
    except ValueError:
        return None


TRAD_RE = re.compile(r"([A-Z])([^A-Z]*)")
# Lines that aren't G0-G3 moves, and the E values of moves (for fast_forward)
NON_MOVE_RE = re.compile(rb"^[ \t]*(?![Gg]0*[0-3](?![0-9.]))[A-Za-z][^\n]*", re.M)
MOVE_RE = re.compile(rb"^[ \t]*[Gg]0*[0-3](?![0-9.])[^;\n]*", re.M)
MOVE_E_RE = re.compile(rb"^[ \t]*[Gg]0*[0-3](?![0-9.])[^;\n]*?[ \t][Ee]([-+]?[0-9.]+)", re.M)
TRAD_CMD_RE = re.compile(r"^[GMT]\d")


MOVE_CMDS = {"G0": "G0", "G1": "G1", "G2": "G2", "G3": "G3",
             "g0": "G0", "g1": "G1", "g2": "G2", "g3": "G3"}


def split_command(line):
    """Return (command, params, comment) like Klipper parses a line."""
    code, sep, comment = line.partition(";")
    words = code.split()
    # Fast path for the usual "G1 X1 Y2 E.3" moves
    if words and words[0] in MOVE_CMDS:
        try:
            params = {w[0].upper(): w[1:] for w in words[1:]}
            if all(v and v[0] in "-+.0123456789" for v in params.values()):
                return MOVE_CMDS[words[0]], params, comment if sep else None
        except IndexError:
            pass
    code = code.strip()
    if not code:
        return None, None, comment if sep else None
    if code[0] == "N" and code[1:2].isdigit():
        code = code.split(None, 1)[1] if " " in code else ""
        if not code:
            return None, None, comment if sep else None
    upper = code.upper()
    if TRAD_CMD_RE.match(upper):
        parts = TRAD_RE.findall(upper.replace(" ", ""))
        cmd = parts[0][0] + parts[0][1]
        try:
            cmd = cmd[0] + str(int(float(cmd[1:]))) if "." not in cmd else cmd
        except ValueError:
            pass
        params = {k: v for k, v in parts[1:]}
        return cmd, params, comment if sep else None
    words = code.split(None, 1)
    cmd = words[0].upper()
    params = {}
    if len(words) > 1:
        for m in re.finditer(r"(\S+?)=(\"[^\"]*\"|'[^']*'|\S*)", words[1]):
            params[m.group(1).upper()] = m.group(2).strip("\"'")
    return cmd, params, comment if sep else None


class GcodeModel:
    """Follows the state Klipper's gcode_move keeps while reading a file."""

    def __init__(self, retract_length=0.0, unretract_extra=0.0, window=MOVE_WINDOW):
        self.pos = [0.0, 0.0, 0.0]
        self.e = 0.0           # G-code E coordinate (as Klipper reports it)
        self.cum_e = 0.0       # all extruder motion, incl. firmware retraction
        self.f = None
        self.abs_xyz = True
        self.abs_e = True
        self.plane = 17
        self.fw_retracted = False
        self.slicer_retract = 0.0
        self.retract_length = retract_length
        self.unretract_extra = unretract_extra
        self.layer = 0
        self.layer_z = None
        self.obj = None
        self.defines = collections.OrderedDict()
        self.state_lines = collections.OrderedDict()
        self.velocity = collections.OrderedDict()
        self.start_temps = {}
        self.moves = collections.deque(maxlen=window)
        self.record_from = 0
        self.last_g92_e = -1   # offset of the last G92 that set E
        self.line = 0

    def snapshot(self):
        return {"pos": list(self.pos), "e": self.e, "cum_e": self.cum_e}

    def copy(self):
        """Copy of the state, without the recorded moves."""
        new = copy.copy(self)
        new.pos = list(self.pos)
        for name in ("defines", "state_lines", "velocity"):
            setattr(new, name, collections.OrderedDict(getattr(self, name)))
        new.start_temps = dict(self.start_temps)
        new.moves = collections.deque(maxlen=1)
        return new

    def feed(self, text, offset):
        self.line += 1
        cmd, params, comment = split_command(text)
        if comment is not None and cmd is None:
            c = comment.strip()
            if c == "LAYER_CHANGE":
                self.layer += 1
            elif c.startswith("Z:"):
                self.layer_z = _num(c[2:])
            return
        if cmd is None:
            return
        if cmd in ("G0", "G1", "G2", "G3"):
            self._move(cmd, params, offset)
        elif cmd == "G90":
            self.abs_xyz = True
            self.abs_e = True
        elif cmd == "G91":
            self.abs_xyz = False
            self.abs_e = False
        elif cmd == "M82":
            self.abs_e = True
        elif cmd == "M83":
            self.abs_e = False
        elif cmd == "G92":
            for i, a in enumerate("XYZ"):
                if a in params and _num(params[a]) is not None:
                    self.pos[i] = _num(params[a])
            if "E" in params and _num(params["E"]) is not None:
                self.e = _num(params["E"])
                self.last_g92_e = offset
            if not params:
                self.pos = [0.0, 0.0, 0.0]
                self.e = 0.0
        elif cmd in ("G17", "G18", "G19"):
            self.plane = int(cmd[1:])
        elif cmd == "G10":
            if not self.fw_retracted:
                self._e_only(offset, -self.retract_length)
                self.fw_retracted = True
        elif cmd == "G11":
            if self.fw_retracted:
                self._e_only(offset, self.retract_length + self.unretract_extra)
                self.fw_retracted = False
        elif cmd == "SET_RETRACTION":
            if _num(params.get("RETRACT_LENGTH", "")) is not None:
                self.retract_length = _num(params["RETRACT_LENGTH"])
            if _num(params.get("UNRETRACT_EXTRA_LENGTH", "")) is not None:
                self.unretract_extra = _num(params["UNRETRACT_EXTRA_LENGTH"])
            self.state_lines["SET_RETRACTION"] = text.split(";")[0].strip()
        elif cmd in ("M106", "M107"):
            fan = params.get("P", "0")
            self.state_lines["fan" + fan] = text.split(";")[0].strip()
        elif cmd == "SET_VELOCITY_LIMIT":
            self.velocity.update(params)
        elif cmd in ("M204", "SET_PRESSURE_ADVANCE", "G21"):
            self.state_lines[cmd] = text.split(";")[0].strip()
        elif cmd in ("M104", "M109"):
            if _num(params.get("S", "")) is not None:
                self.start_temps["extruder"] = _num(params["S"])
        elif cmd in ("M140", "M190"):
            if _num(params.get("S", "")) is not None:
                self.start_temps["bed"] = _num(params["S"])
        elif cmd in ("PRINT_START", "START_PRINT"):
            for key, name in (("EXTRUDER_TEMP", "extruder"), ("EXTRUDER", "extruder"),
                              ("HOTEND_TEMP", "extruder"), ("BED_TEMP", "bed"), ("BED", "bed")):
                if _num(params.get(key, "")) is not None:
                    self.start_temps[name] = _num(params[key])
        elif cmd == "EXCLUDE_OBJECT_DEFINE":
            self.defines[params.get("NAME", "")] = text.split(";")[0].strip()
        elif cmd == "EXCLUDE_OBJECT_START":
            self.obj = params.get("NAME")
        elif cmd == "EXCLUDE_OBJECT_END":
            self.obj = None

    def fast_forward(self, path, end):
        """Bring the state to byte offset end (a line start) without
        following every move. Only the moves' final position and the E
        coordinate matter there, and recorded moves start at end anyway.
        Returns False if the file needs to be read in full instead.
        """
        with open(path, "rb") as f:
            data = f.read(end)
        for m in NON_MOVE_RE.finditer(data):
            self.feed(m.group().decode("utf-8", "replace"), m.start())
            if not self.abs_xyz:
                return False   # relative moves add up
        self.line = data.count(b"\n")
        self.layer = data.count(b"\n;LAYER_CHANGE") + data.startswith(b";LAYER_CHANGE")
        z = data.rfind(b"\n;Z:")
        if z >= 0:
            self.layer_z = _num(data[z + 4:data.find(b"\n", z + 1)].decode().strip())
        # Last value of each axis, from the moves at the end
        need = {"X", "Y", "Z", "F"}
        found = {}
        last_e = None
        chunk = 65536
        while need and chunk < 2 * end:
            start = max(0, end - chunk)
            for m in reversed(list(MOVE_RE.finditer(data, start))):
                cmd, params, _ = split_command(m.group().decode("utf-8", "replace"))
                for a in list(need):
                    if a in params and _num(params[a]) is not None:
                        found[a] = _num(params[a])
                        need.discard(a)
                if last_e is None and "E" in params and _num(params["E"]) is not None:
                    last_e = (m.start(), _num(params["E"]))
                if not need:
                    break
            if start == 0:
                break
            chunk *= 4
        for i, a in enumerate("XYZ"):
            self.pos[i] = found.get(a, self.pos[i])
        self.f = found.get("F", self.f)
        if self.abs_e:
            if last_e is not None and last_e[0] > self.last_g92_e:
                self.e = last_e[1]
        else:
            self.e += sum(float(v) for v in MOVE_E_RE.findall(data, self.last_g92_e + 1))
        return True

    def _e_only(self, offset, de):
        x, y, z = self.pos
        e0 = self.cum_e
        self.cum_e += de
        if offset >= self.record_from:
            self.moves.append(Move(offset, self.line, x, y, z, x, y, z, e0, self.cum_e, None))

    def _move(self, cmd, params, offset):
        start = list(self.pos)
        new = list(self.pos)
        for i, a in enumerate("XYZ"):
            v = _num(params[a]) if a in params else None
            if v is not None:
                new[i] = v if self.abs_xyz else new[i] + v
        de = 0.0
        if "E" in params and _num(params["E"]) is not None:
            v = _num(params["E"])
            de = v - self.e if self.abs_e else v
            self.e = v if self.abs_e else self.e + v
        if "F" in params and _num(params["F"]) is not None:
            self.f = _num(params["F"])
        moved_xy = new[0] != start[0] or new[1] != start[1]
        arc = None
        if cmd in ("G2", "G3"):
            moved_xy = True
        if cmd in ("G2", "G3") and self.plane == 17 and offset >= self.record_from:
            i = _num(params.get("I", "0")) or 0.0
            j = _num(params.get("J", "0")) or 0.0
            cx, cy = start[0] + i, start[1] + j
            r = math.hypot(start[0] - cx, start[1] - cy)
            a0 = math.atan2(start[1] - cy, start[0] - cx)
            a1 = math.atan2(new[1] - cy, new[0] - cx)
            if cmd == "G3":
                sweep = (a1 - a0) % (2 * math.pi)
                if sweep < 1e-9:
                    sweep = 2 * math.pi
            else:
                sweep = -((a0 - a1) % (2 * math.pi))
                if sweep > -1e-9:
                    sweep = -2 * math.pi
            arc = (cx, cy, r, a0, sweep)
        if de < 0:
            self.slicer_retract -= de
        elif de > 0:
            self.slicer_retract = 0.0 if moved_xy else max(0.0, self.slicer_retract - de)
        e0 = self.cum_e
        self.cum_e += de
        self.pos = new
        if offset >= self.record_from and (de or moved_xy or new[2] != start[2]):
            self.moves.append(Move(offset, self.line, start[0], start[1], start[2],
                                   new[0], new[1], new[2], e0, self.cum_e, arc))


def iter_lines(path, stop=None, start=0):
    """Yield (offset, text) for each line, like virtual_sdcard counts bytes."""
    offset = start
    with open(path, "rb") as f:
        f.seek(start)
        for raw in f:
            if stop is not None and offset >= stop:
                return
            yield offset, raw.rstrip(b"\r\n").decode("utf-8", "replace")
            offset += len(raw)


def line_start(path, pos):
    """Offset of the first line starting at or after pos."""
    if pos <= 0:
        return 0
    with open(path, "rb") as f:
        f.seek(pos - 1)
        f.readline()
        return f.tell()


def path_distance(m, px, py):
    """Distance from (px, py) to the move's XY path, and how far along it is."""
    if m.arc is not None:
        cx, cy, r, a0, sweep = m.arc
        d_center = math.hypot(px - cx, py - cy)
        ang = math.atan2(py - cy, px - cx)
        delta = (ang - a0) % (2 * math.pi) if sweep > 0 else (a0 - ang) % (2 * math.pi)
        if delta <= abs(sweep):
            return abs(d_center - r), delta / abs(sweep)
        d0 = math.hypot(px - m.x0, py - m.y0)
        d1 = math.hypot(px - m.x1, py - m.y1)
        return (d0, 0.0) if d0 < d1 else (d1, 1.0)
    dx, dy = m.x1 - m.x0, m.y1 - m.y0
    seg = dx * dx + dy * dy
    if seg == 0:
        return math.hypot(px - m.x0, py - m.y0), 0.0
    t = max(0.0, min(1.0, ((px - m.x0) * dx + (py - m.y0) * dy) / seg))
    return math.hypot(px - (m.x0 + t * dx), py - (m.y0 + t * dy)), t


def path_length(m):
    if m.arc is not None:
        return abs(m.arc[4]) * m.arc[2]
    return math.hypot(m.x1 - m.x0, m.y1 - m.y0)


def locate(moves, px, py, pz, pe, use_e=True):
    """Index of the move being executed at the live position, or None.

    pe is the live extruder position on the model's cum_e scale. Of all moves
    that fit, the earliest one the toolhead hasn't finished yet is chosen.
    """
    fits = []
    for idx, m in enumerate(moves):
        d, t = path_distance(m, px, py)
        if d > TOL_XY:
            continue
        if not (min(m.z0, m.z1) - TOL_Z <= pz <= max(m.z0, m.z1) + TOL_Z):
            continue
        de = m.e1 - m.e0
        if use_e:
            if abs(de) > 1e-6:
                te = (pe - m.e0) / de
                if te < -TOL_E / abs(de) or te > 1 + TOL_E / abs(de):
                    continue
                # Along a move the extruder and the toolhead progress together
                if abs(te - t) * path_length(m) > TOL_ALONG:
                    continue
                t = te
            elif abs(pe - m.e0) > TOL_E:
                continue
        fits.append((idx, t))
    if not fits:
        return None
    for idx, t in fits:
        if t < 0.999:
            return idx
    return fits[-1][0]


# ===== Plan =====

def _fmt(v):
    s = "%.4f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def load_checkpoint(path):
    with open(path) as f:
        cp = json.load(f)
    if cp.get("version") != CHECKPOINT_VERSION:
        raise PlanError("checkpoint version %s not supported" % cp.get("version"))
    return cp


def first_print_move(path, end):
    """Offset of the first extruding XY move, or None if there's none up to
    end."""
    for offset, text in iter_lines(path):
        if offset > end:
            return None
        cmd, params, _ = split_command(text)
        if cmd in ("G1", "G2", "G3") and ("X" in params or "Y" in params):
            e = _num(params.get("E", "")) if "E" in params else None
            if e is not None and e > 0:
                return offset
    return None


def plan(cp, gcode_path=None):
    """Work out where and how to resume. Returns a dict."""
    st = cp["status"]
    path = gcode_path or cp["file_path"]
    if not path or not os.path.exists(path):
        raise PlanError("the G-code file %s no longer exists" % (cp.get("filename") or path))
    if gcode_path is None and "file_size" in cp:
        fst = os.stat(path)
        if fst.st_size != cp["file_size"] or abs(fst.st_mtime - cp["file_mtime"]) > 1:
            raise PlanError("the G-code file changed since the power loss")
    gm = st["gcode_move"]
    fw = st.get("firmware_retraction", {})
    fp = cp["file_position"]
    paused = cp["state"] == "paused"
    first = first_print_move(path, fp)
    if first is None or fp <= first:
        # Klipper hadn't read a print move yet, so it was still in the start
        # G-code (heating, homing, probing): there's nothing to continue
        raise NotStarted("the power failed before printing started (file position %d, "
                         "first print move %s)" % (fp, "none" if first is None else first))
    ef = gm.get("extrude_factor", 1.0) or 1.0

    # Pass 1: read up to and including the line at the file position
    def new_model():
        return GcodeModel(fw.get("retract_length", 0.0), fw.get("unretract_extra_length", 0.0))
    model = new_model()
    window = line_start(path, fp - WINDOW_BYTES)
    model.record_from = window
    start = 0
    if window > 0:
        if model.fast_forward(path, window):
            start = window
        else:
            model = new_model()
            model.record_from = window
    snapshots = []    # (offset of the next line, model state before it)
    before = after = None
    fp_line = None
    for offset, text in iter_lines(path, start=start):
        if offset >= window and (offset == window or model.line % SNAPSHOT_LINES == 0):
            snapshots.append((offset, model.copy()))
        if offset == fp:
            before = model.snapshot()
            fp_line = model.line + 1
        elif offset > fp:
            break
        model.feed(text, offset)
        if offset == fp:
            after = model.snapshot()
    if before is None:
        raise PlanError("file position %d isn't the start of a line" % fp)
    if after is None:
        after = model.snapshot()

    # Which of the two states Klipper's gcode_move had reached
    gpos = gm["gcode_position"]

    def mismatch(s):
        return max(abs(s["pos"][i] - gpos[i]) for i in range(3)) + abs(s["e"] - gpos[3])
    # Ties (E-only lines like G10) are usual: Klipper normally waits inside a
    # line's move, after gcode_move has taken it in
    refs = sorted((after, before), key=mismatch)
    ref = refs[0]
    warnings = []
    if mismatch(ref) > 0.01:
        warnings.append("the file doesn't match Klipper's position at the file position "
                        "(off by %.3f); a macro may have moved the toolhead" % mismatch(ref))
    moves = list(model.moves)
    base = [gm["position"][i] - gpos[i] for i in range(3)]
    homing = gm["homing_origin"]
    if max(abs(base[i] - homing[i]) for i in range(3)) > 0.001:
        raise PlanError("the print used G92 offsets for X/Y/Z, which resume doesn't support")

    if paused:
        resume_offset = fp
        resume_line = fp_line
        live = st["toolhead"]["position"]
        method = "paused"
    else:
        live = st["motion_report"]["live_position"]
        px, py, pz = live[0] - base[0], live[1] - base[1], live[2] - base[2]
        method = "position+extruder"
        for r in refs:
            # Extruder motion still queued in Klipper when the state was read
            pe = r["cum_e"] - (gm["position"][3] - live[3]) / ef
            idx = locate(moves, px, py, pz, pe)
            if idx is not None:
                break
        if idx is None:
            idx = locate(moves, px, py, pz, pe, use_e=False)
            method = "position"
            warnings.append("matched the live position without the extruder position")
        if idx is None:
            raise PlanError("couldn't find the live position %.3f, %.3f, %.3f in the last "
                            "%d moves before byte %d" % (px, py, pz, len(moves), fp))
        resume_offset = moves[idx].offset
        resume_line = moves[idx].line

    # Pass 2: the state just before the resume line, from the last snapshot
    start, model2 = 0, None
    for offset, snap in snapshots:
        if offset <= resume_offset:
            start, model2 = offset, snap
    if model2 is None:
        model2 = GcodeModel(fw.get("retract_length", 0.0), fw.get("unretract_extra_length", 0.0),
                            window=1)
    for offset, text in iter_lines(path, stop=resume_offset, start=start):
        model2.feed(text, offset)
    next_layer = next_layer_offset(path, model2, resume_offset)
    if next_layer is None:
        warnings.append("the next layer isn't within %d MiB; resuming at full speed"
                        % (NEXT_LAYER_BYTES // (1024 * 1024)))
    return {
        "checkpoint": cp,
        "path": path,
        "method": method,
        "warnings": warnings,
        "resume_offset": resume_offset,
        "resume_line": resume_line,
        "fp_line": fp_line,
        "bytes_back": fp - resume_offset,
        "live": live,
        "state": model2,
        "next_layer_offset": next_layer,
    }


def next_layer_offset(path, model, start):
    """Offset of the line that starts the layer after the one at start.

    That is the first extruding XY move above the Z of the first one from
    start, so z-hops and travels don't count. Returns the file size if the
    print ends first, and None if it isn't within NEXT_LAYER_BYTES.
    """
    m = model.copy()
    m.record_from = 0
    last = None
    layer_z = None
    for offset, text in iter_lines(path, start=start):
        if offset - start > NEXT_LAYER_BYTES:
            return None
        m.feed(text, offset)
        mv = m.moves[-1] if m.moves else None
        if mv is last:
            continue
        last = mv
        if mv.e1 - mv.e0 > 1e-6 and (mv.arc or (mv.x0, mv.y0) != (mv.x1, mv.y1)):
            if layer_z is None:
                layer_z = mv.z1
            elif mv.z1 > layer_z + 1e-3:
                return offset
    return os.path.getsize(path)


def describe(p):
    s = p["state"]
    return ("resume at line %d (byte %d, %d bytes before the file position, line %s); "
            "layer %d, Z %s, start X%s Y%s Z%s, matched by %s" % (
                p["resume_line"], p["resume_offset"], p["bytes_back"], p["fp_line"],
                s.layer, _fmt(s.layer_z) if s.layer_z is not None else "?",
                _fmt(s.pos[0]), _fmt(s.pos[1]), _fmt(s.pos[2]), p["method"])
            + ("" if p.get("next_layer_offset") is None
               else "; next layer at byte %d" % p["next_layer_offset"]))


# ===== Klipper config =====

UNSAFE_CFG = re.compile(r"[#{}]|\s;")


def resume_gcode(p):
    """The G-code lines PLR_RESUME runs."""
    cp = p["checkpoint"]
    st = cp["status"]
    s = p["state"]
    th = st["toolhead"]
    gm = st["gcode_move"]
    fw = st.get("firmware_retraction")
    ext = st.get("extruder", {})
    live = p["live"]
    bed_t = st.get("heater_bed", {}).get("target") or 0.0
    ext_t = ext.get("target") or 0.0
    if cp["state"] == "paused" or ext_t < MIN_EXTRUDE_TEMP:
        # PAUSE lowers the nozzle temperature; use the file's
        ext_t = s.start_temps.get("extruder", ext_t)
        bed_t = bed_t or s.start_temps.get("bed", 0.0)
    if ext_t < MIN_EXTRUDE_TEMP:
        raise PlanError("no nozzle temperature to resume with")
    zmax = th["axis_maximum"][2]
    lift_z = min(live[2] + LIFT, zmax)
    park_x = th["axis_minimum"][0] + 5
    park_y = th["axis_maximum"][1] - 5
    retract = (fw or {}).get("retract_length") or 1.0
    homing = gm["homing_origin"]
    mesh = st.get("bed_mesh", {})
    sx, sy, sz = s.pos
    g = []
    a = g.append
    a("M117 Resuming print")
    a("BED_MESH_CLEAR")
    a("SET_GCODE_OFFSET X=0 Y=0 Z=0")
    a("M140 S%s" % _fmt(bed_t))
    # The axes keep their place without power, except X/Y that are homed below
    a("SET_KINEMATIC_POSITION X=%s Y=%s Z=%s" % (_fmt(live[0]), _fmt(live[1]), _fmt(live[2])))
    # Back to the print temperatures before anything moves, so the nozzle comes
    # free of the plastic it stopped in. The bed first, so that the nozzle
    # isn't hot against the print for longer than needed.
    if bed_t > 0:
        a("M190 S%s" % _fmt(bed_t))
    a("M109 S%s" % _fmt(ext_t))
    a("G90")
    a("G1 Z%s F600" % _fmt(lift_z))
    a("G28 X Y")
    a("SET_GCODE_OFFSET X=%s Y=%s Z=%s" % (_fmt(homing[0]), _fmt(homing[1]), _fmt(homing[2])))
    if mesh.get("profile_name"):
        a("BED_MESH_PROFILE LOAD=plr")
    a("G1 X%s Y%s F6000" % (_fmt(park_x), _fmt(park_y)))
    a("M109 S%s" % _fmt(ext_t))
    a("M83")
    a("G1 E{params.PURGE|default(%s)|float} F200" % _fmt(PURGE_LENGTH))
    a("G1 E-%s F2400" % _fmt(retract))
    a("G91")
    for dx in (20, -20, 20, -20):
        a("G1 X%d F12000" % dx)
    a("G90")
    # Settings at the time of the power loss, then the file's at the resume line
    # Slower for the rest of this layer: PLR_SPEED restores the speed at the next
    speed = gm.get("speed_factor", 1.0) * 100
    slow = p.get("next_layer_offset") is not None
    if slow:
        a("{%% set slow = %s * params.SPEED|default(%d)|float / 100 %%}" % (_fmt(speed), RESUME_SPEED))
        a("SET_GCODE_VARIABLE MACRO=PLR_RESUME VARIABLE=slow VALUE={slow}")
        a("M220 S{slow}")
    else:
        a("M220 S%s" % _fmt(speed))
    a("M221 S%s" % _fmt(gm.get("extrude_factor", 1.0) * 100))
    a("SET_VELOCITY_LIMIT VELOCITY=%s ACCEL=%s SQUARE_CORNER_VELOCITY=%s" % (
        _fmt(th["max_velocity"]), _fmt(th["max_accel"]), _fmt(th["square_corner_velocity"])))
    if "pressure_advance" in ext:
        a("SET_PRESSURE_ADVANCE ADVANCE=%s SMOOTH_TIME=%s" % (
            _fmt(ext["pressure_advance"]), _fmt(ext.get("smooth_time", 0.04))))
    if fw:
        a("SET_RETRACTION RETRACT_LENGTH=%s RETRACT_SPEED=%s UNRETRACT_EXTRA_LENGTH=%s "
          "UNRETRACT_SPEED=%s" % (_fmt(fw["retract_length"]), _fmt(fw["retract_speed"]),
                                  _fmt(fw["unretract_extra_length"]), _fmt(fw["unretract_speed"])))
    fan = st.get("fan", {}).get("speed")
    if fan is not None:
        a("M106 S%s" % _fmt(fan * 255))
    for key, line in s.state_lines.items():
        if key != "SET_RETRACTION":
            a(line)
    if s.velocity:
        a("SET_VELOCITY_LIMIT " + " ".join("%s=%s" % kv for kv in s.velocity.items()))
    # Travel to where the resume line starts and set the extruder up the way
    # the file left it there
    a("G1 X%s Y%s F12000" % (_fmt(sx), _fmt(sy)))
    a("G1 Z%s F600" % _fmt(sz))
    prime = retract - s.slicer_retract
    if abs(prime) > 1e-6:
        a("G1 E%s F2400" % _fmt(prime))
    if s.fw_retracted:
        a("G10")
    a("M82" if s.abs_e else "M83")
    a("G92 E%s" % _fmt(s.e))
    if s.f:
        a("G1 F%s" % _fmt(s.f))
    if not s.abs_xyz:
        a("G91")
    if s.plane != 17:
        a("G%d" % s.plane)
    a("M23 %s" % cp["filename"])
    # M23 clears the object list
    excluded = st.get("exclude_object", {}).get("excluded_objects", [])
    defines = list(s.defines.values())
    if any(UNSAFE_CFG.search(d) for d in defines):
        p["warnings"].append("object names can't be put in the macro; cancelling objects "
                             "won't work for the rest of the print")
    else:
        g.extend(defines)
        for name in excluded:
            a("EXCLUDE_OBJECT NAME=%s" % name)
        if s.obj:
            a("EXCLUDE_OBJECT_START NAME=%s" % s.obj)
    a("M26 S%d" % p["resume_offset"])
    a("M117 Printing")
    a("M24")
    if slow:
        a("UPDATE_DELAYED_GCODE ID=PLR_SPEED DURATION=1")
    return g


def speed_section(p):
    """[delayed_gcode PLR_SPEED], which restores the speed at the next layer.

    It acts once Klipper has read up to that layer (or the print has ended),
    unless the speed was changed in the meantime.
    """
    if p.get("next_layer_offset") is None:
        return []
    speed = _fmt(p["checkpoint"]["status"]["gcode_move"].get("speed_factor", 1.0) * 100)
    return ["[delayed_gcode PLR_SPEED]",
            "gcode:",
            "  {% set slow = printer[\"gcode_macro PLR_RESUME\"].slow|float %}",
            "  {%% if printer.print_stats.state in (\"printing\", \"paused\")"
            " and printer.virtual_sdcard.file_position < %d %%}" % p["next_layer_offset"],
            "    UPDATE_DELAYED_GCODE ID=PLR_SPEED DURATION=0.5",
            "  {% elif (printer.gcode_move.speed_factor * 100 - slow)|abs < 0.01 %}",
            "    M220 S%s" % speed,
            "    RESPOND MSG=\"Power-loss recovery: speed back to %s%%\"" % speed,
            "  {% endif %}",
            ""]


def mesh_section(st):
    """Profile with the bed mesh that was active, as Klipper had it in memory.

    The interpolated mesh from the status is saved with interpolation turned
    off ("direct"), so loading it gives the same mesh without probing again or
    knowing how it was interpolated. This also covers the meshes that
    BED_MESH_CALIBRATE ADAPTIVE=1 makes, which aren't saved as profiles.
    """
    mesh = st.get("bed_mesh", {})
    name = mesh.get("profile_name")
    if not name:
        return []
    grid = mesh.get("mesh_matrix") or [[]]
    lo, hi = mesh.get("mesh_min"), mesh.get("mesh_max")
    if (len(grid) < 2 or len(grid[0]) < 2 or any(len(row) != len(grid[0]) for row in grid)
            or not lo or not hi or hi[0] <= lo[0] or hi[1] <= lo[1]):
        raise PlanError("bed mesh %s wasn't saved with the checkpoint" % name)
    out = ["[bed_mesh plr]", "version: 1", "points:"]
    for row in grid:
        out.append("  " + ", ".join("%.6f" % v for v in row))
    params = [("min_x", lo[0]), ("max_x", hi[0]), ("min_y", lo[1]), ("max_y", hi[1]),
              ("x_count", len(grid[0])), ("y_count", len(grid)),
              ("mesh_x_pps", 0), ("mesh_y_pps", 0), ("algo", "direct"), ("tension", 0.2)]
    out += ["%s: %s" % kv for kv in params]
    return out


def find_save_variables(config_dir, own_cfg):
    """Filename of the user's [save_variables], if the config has one."""
    for root, dirs, files in os.walk(config_dir):
        for name in files:
            path = os.path.join(root, name)
            if not name.endswith(".cfg") or os.path.abspath(path) == os.path.abspath(own_cfg):
                continue
            try:
                with open(path, errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            m = re.search(r"^\[save_variables\][^\[]*?^filename\s*[:=]\s*(\S+)", text, re.M | re.S)
            if m:
                return os.path.expanduser(m.group(1))
    return None


def read_handled(var_file):
    if not var_file or not os.path.exists(var_file):
        return None
    cfg = configparser.ConfigParser()
    try:
        cfg.read(var_file)
        return int(cfg.get("Variables", "plr_handled"))
    except (configparser.Error, ValueError):
        return None


def build_cfg(cp, p, own_variables):
    """Text of plr.cfg. p is a plan, or a PlanError to report."""
    cid = cp["id"]
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(cp["time"]))
    filename = cp.get("filename") or ""
    out = ["# Power-loss recovery for the print interrupted at %s." % when,
           "# Written by OpenNept4une img-config/plr/plr.py at boot, removed once",
           "# the print has been resumed or discarded. checkpoint=%d" % cid, ""]
    if own_variables:
        out += ["[save_variables]", "filename: " + own_variables, ""]
    handled = ("{%% set handled = printer.save_variables.variables.plr_handled|default(0) "
               "== %d %%}" % cid)
    mark = "SAVE_VARIABLE VARIABLE=plr_handled VALUE=%d" % cid
    error = None
    if isinstance(p, PlanError):
        error = str(p)
    elif UNSAFE_CFG.search(filename):
        error = "the file name can't be used in a macro: " + filename
    if error is None:
        try:
            lines = resume_gcode(p)
            out += mesh_section(cp["status"]) + [""] + speed_section(p)
        except PlanError as e:
            error = str(e)
    safe_name = re.sub(r"[#{}\"]", "_", filename)
    out += ["[gcode_macro PLR_RESUME]",
            "description: Resume the print interrupted by the power loss",
            "variable_checkpoint: %d" % cid,
            "variable_filename: %s" % json.dumps(safe_name),
            "variable_error: %s" % json.dumps(re.sub(r"[#{}\"]", "_", error or "")),
            "variable_slow: 0",
            "gcode:",
            "  " + handled,
            "  {% if handled %}",
            "    {action_raise_error(\"Nothing to resume\")}",
            "  {% endif %}"]
    if error is not None:
        out.append("  {action_raise_error(\"Can't resume: %s\")}" % re.sub(r"[#{}\"]", "_", error))
    else:
        out += ["  {% if printer.print_stats.state in (\"printing\", \"paused\") %}",
                "    {action_raise_error(\"A print is running\")}",
                "  {% endif %}",
                "  " + mark]
        out += ["  " + line for line in lines]
    out += ["",
            "[gcode_macro PLR_DISCARD]",
            "description: Discard the print interrupted by the power loss",
            "gcode:",
            "  " + mark,
            "  RESPOND MSG=\"Power-loss recovery data discarded\"",
            "",
            "# Starting another print discards the recovery data too",
            "[delayed_gcode PLR_WATCH]",
            "initial_duration: 5",
            "gcode:",
            "  " + handled,
            "  {% if not handled %}",
            "    {% if printer.print_stats.state in (\"printing\", \"paused\") %}",
            "      " + mark,
            "    {% else %}",
            "      UPDATE_DELAYED_GCODE ID=PLR_WATCH DURATION=10",
            "    {% endif %}",
            "  {% endif %}",
            ""]
    for line in out:
        if "#" in line and not line.startswith("#"):
            raise PlanError("generated config line contains '#': " + line[:80])
    return "\n".join(out), error


def remove(path):
    try:
        os.remove(path)
        log("removed " + path)
    except FileNotFoundError:
        pass


def cmd_prepare(args):
    p = paths(args.data_dir)
    cfg_path = p["cfg"]
    if not os.path.exists(p["checkpoint"]):
        remove(cfg_path)
        return 0
    try:
        cp = load_checkpoint(p["checkpoint"])
    except (OSError, ValueError, PlanError) as e:
        log("unreadable checkpoint: %s" % e)
        remove(cfg_path)
        return 0
    user_vars = find_save_variables(p["config_dir"], cfg_path)
    var_file = user_vars or p["variables"]
    if read_handled(var_file) == cp["id"]:
        log("checkpoint %d already resumed or discarded" % cp["id"])
        remove(cfg_path)
        return 0
    try:
        with open(cfg_path) as f:
            if "checkpoint=%d\n" % cp["id"] in f.read(2048):
                log("plr.cfg is up to date")
                return 0
    except OSError:
        pass
    os.makedirs(p["state_dir"], exist_ok=True)
    try:
        pl = plan(cp)
        log(describe(pl))
        for w in pl["warnings"]:
            log("warning: " + w)
    except NotStarted as e:
        log("nothing to resume: %s" % e)
        remove(cfg_path)
        return 0
    except PlanError as e:
        pl = e
        log("can't resume: %s" % e)
    except Exception as e:  # a bad checkpoint mustn't keep Klipper from starting
        pl = PlanError("unexpected %s: %s" % (type(e).__name__, e))
        log("can't resume: %s" % pl)
    text, error = build_cfg(cp, pl, None if user_vars else p["variables"])
    if error and not isinstance(pl, PlanError):
        log("can't resume: %s" % error)
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    write_atomic(cfg_path, text)
    log("wrote " + cfg_path)
    return 0


def cmd_plan(args):
    cp = load_checkpoint(args.checkpoint)
    try:
        pl = plan(cp, args.gcode)
    except NotStarted as e:
        log("nothing to resume: %s" % e)
        return 0
    except PlanError as e:
        log("can't resume: %s" % e)
        return 1
    log(describe(pl))
    for w in pl["warnings"]:
        log("warning: " + w)
    if args.cfg:
        text, error = build_cfg(cp, pl, "~/printer_data/plr/variables.cfg")
        print(text)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default=DATA_DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("checkpoint", help="save the print state (power_monitor hook)")
    c.add_argument("--dry-run", action="store_true", help="don't emergency-stop")
    c.add_argument("--output", help="write here instead of the checkpoint file")
    sub.add_parser("prepare", help="write or remove plr.cfg (at boot)")
    pl = sub.add_parser("plan", help="show the resume plan for a checkpoint")
    pl.add_argument("checkpoint")
    pl.add_argument("--gcode", help="G-code file to use instead of the checkpoint's")
    pl.add_argument("--cfg", action="store_true", help="also print plr.cfg")
    args = ap.parse_args(argv)
    return {"checkpoint": cmd_checkpoint, "prepare": cmd_prepare, "plan": cmd_plan}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
