#!/usr/bin/env python3
"""
ipu7-camera-dynamic — static dual-source pipeline:
  IDLE:   icamerasrc locked in NULL, videotestsrc feeds black frames
  ACTIVE: icamerasrc NULL→PLAYING (first cold start may take a few seconds),
          input-selector switches to it

Resolution is selected at startup from the HAL JSON using PREFERRED_RESOLUTIONS
priority order, trying each until PAUSED succeeds. Overridable via CAMERA_RESOLUTION.
"""
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GLib
import json as _json
import os, re, sys, time, threading

Gst.init(None)

DEVICE      = "/dev/video32"
SYSFS_STATE = "/sys/devices/virtual/video4linux/video32/state"
STOP_DELAY  = 0
HAL_JSON    = "/etc/camera/ipu7x/sensors/ov08x40-uf.json"

# Highest priority first; high-fps candidates are probed even if HAL reports lower max fps
PREFERRED_RESOLUTIONS = [
    (3840, 2160, 30),
    (1920, 1080, 60),
    (1920, 1080, 30),
    (1280,  720, 30),
    ( 640,  480, 30),
]

PW_PROPS = (
    "props,media.class=Video/Source,media.role=Camera,"
    "node.name=ipu7-hal-camera,node.description=Front-Camera,"
    "node.nick=Front-Camera,api.v4l2.cap.driver=uvcvideo,"
    "api.v4l2.cap.card=Front-Camera,device.description=Front-Camera"
)

def log(msg): print(msg, flush=True)


def read_hal_resolutions():
    """Parse HAL JSON → (w, h, fps) list ordered by PREFERRED_RESOLUTIONS priority."""
    try:
        raw = open(HAL_JSON).read()
        raw = re.sub(r'//[^\n]*', '', raw)
        data = _json.loads(raw)
        meta = data["CameraSettings"]["Sensor"][0]["StaticMetadata"]
        cfgs = meta["supportedStreamConfig"]
        supported_sizes = {(c["size"][0], c["size"][1]) for c in cfgs}
        fps_vals = meta.get("fpsRange", [30])
        hal_max_fps = max(fps_vals)
        high_fps = [(w, h, f) for w, h, f in PREFERRED_RESOLUTIONS if f > hal_max_fps]
        if high_fps:
            log(f"[res] HAL reports max {hal_max_fps}fps; probing high-fps candidates anyway: {high_fps}")
        result = [
            (w, h, f) for w, h, f in PREFERRED_RESOLUTIONS
            if (w, h) in supported_sizes
        ]
        if result:
            return result
    except Exception as e:
        log(f"[res] could not parse HAL JSON: {e}")
    return [(1920, 1080, 30), (1280, 720, 30)]


def resolution_candidates():
    """
    CAMERA_RESOLUTION=auto (default) — read from HAL JSON, highest first.
    CAMERA_RESOLUTION=1920x1080      — use exactly, fps defaults to 30.
    CAMERA_RESOLUTION=1920x1080x60   — use exactly with specified fps.
    """
    cfg = os.environ.get("CAMERA_RESOLUTION", "auto").strip().lower()
    if cfg != "auto":
        parts = re.split(r'[x×]', cfg)
        try:
            if len(parts) == 3:
                return [(int(parts[0]), int(parts[1]), int(parts[2]))]
            elif len(parts) == 2:
                return [(int(parts[0]), int(parts[1]), 30)]
        except ValueError:
            pass
        log(f"[res] cannot parse CAMERA_RESOLUTION={cfg!r}, falling back to auto")
    return read_hal_resolutions()


class CameraSwitch:
    def __init__(self):
        self.pipeline  = None
        self.sel       = None
        self.icam      = None
        self.idle_pad  = None
        self.icam_pad  = None
        self.is_active = False
        self.lock      = threading.Lock()
        self.res_w     = 1920
        self.res_h     = 1080
        self.res_fps   = 30

    def _res_tag(self):
        return f"{self.res_w}×{self.res_h}@{self.res_fps}"

    def build(self, w, h, fps):
        self.res_w, self.res_h, self.res_fps = w, h, fps
        caps_yuy2 = (f"video/x-raw,format=YUY2,width={w},height={h},"
                     f"framerate={fps}/1,colorimetry=bt601")
        caps_nv12 = f"video/x-raw,format=NV12,width={w},height={h},framerate={fps}/1"

        desc = (
            f'input-selector name=sel sync-streams=false '
            f'! capsfilter caps="{caps_yuy2}" '
            f'! tee name=t '
            f'  t. ! queue ! v4l2sink device={DEVICE} sync=false '
            f'  t. ! queue ! pipewiresink mode=2 stream-properties="{PW_PROPS}" '
            f'videotestsrc pattern=black is-live=true name=idle '
            f'! capsfilter caps="{caps_yuy2}" '
            f'! sel. '
            f'icamerasrc device-name=0 name=icam '
            f'! capsfilter caps="{caps_nv12}" '
            f'! videoconvert '
            f'! capsfilter caps="{caps_yuy2}" '
            f'! queue leaky=downstream max-size-buffers=2 '
            f'! sel. '
        )
        self.pipeline = Gst.parse_launch(desc)
        self.sel  = self.pipeline.get_by_name("sel")
        self.icam = self.pipeline.get_by_name("icam")

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)

    def _teardown(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline.get_state(5 * Gst.SECOND)
            self.pipeline = None
            self.sel = self.icam = None

    def start(self):
        """Bring pipeline to PAUSED then PLAYING. Returns True on success, False on failure."""
        self.icam.set_locked_state(True)

        log(f"[{self._res_tag()}] pipeline → PAUSED (icamerasrc locked in NULL)...")
        self.pipeline.set_state(Gst.State.PAUSED)
        ret = self.pipeline.get_state(15 * Gst.SECOND)
        if ret[0] == Gst.StateChangeReturn.FAILURE:
            log(f"[{self._res_tag()}] PAUSED failed")
            self._teardown()
            return False
        log(f"[{self._res_tag()}] PAUSED OK")

        it = self.sel.iterate_sink_pads()
        pads = {}
        while True:
            r, pad = it.next()
            if r != Gst.IteratorResult.OK:
                break
            pads[pad.name] = pad
        self.idle_pad = pads.get("sink_0")
        self.icam_pad = pads.get("sink_1")
        if not self.idle_pad or not self.icam_pad:
            log(f"FATAL: selector sink pads not found: {list(pads.keys())}")
            self._teardown()
            return False
        log(f"idle pad: {self.idle_pad.name}, icam pad: {self.icam_pad.name}")

        self.sel.set_property("active-pad", self.idle_pad)
        self.pipeline.set_state(Gst.State.PLAYING)
        log(f"pipeline running — IDLE @ {self._res_tag()}")
        return True

    def go_active(self):
        with self.lock:
            if self.is_active:
                return
            self.is_active = True

        log(f"activating camera (icamerasrc → PLAYING) @ {self._res_tag()}")
        self.icam.set_locked_state(False)
        self.icam.set_state(Gst.State.PLAYING)

        ret = self.icam.get_state(20 * Gst.SECOND)
        if ret[0] == Gst.StateChangeReturn.FAILURE:
            log("ERROR: icamerasrc could not reach PLAYING, aborting activation")
            self.icam.set_locked_state(True)
            with self.lock:
                self.is_active = False
            return

        time.sleep(0.5)
        self.sel.set_property("active-pad", self.icam_pad)
        log("ACTIVE — real camera output, LED on")

    def go_idle(self):
        with self.lock:
            if not self.is_active:
                return
            self.is_active = False

        log("switching to idle (LED off)...")
        self.sel.set_property("active-pad", self.idle_pad)
        time.sleep(0.3)
        self.icam.set_state(Gst.State.NULL)
        self.icam.get_state(20 * Gst.SECOND)
        self.icam.set_locked_state(True)
        log("IDLE — LED off")

    def _fd_openers(self):
        result = set()
        my_pid = os.getpid()
        try:
            for pid_s in os.listdir('/proc'):
                if not pid_s.isdigit() or int(pid_s) == my_pid:
                    continue
                try:
                    for fd in os.listdir(f'/proc/{pid_s}/fd'):
                        try:
                            if os.readlink(f'/proc/{pid_s}/fd/{fd}') == DEVICE:
                                result.add(int(pid_s))
                                break
                        except OSError:
                            pass
                except OSError:
                    pass
        except OSError:
            pass
        return result

    def is_captured(self):
        try:
            state_ok = open(SYSFS_STATE).read().strip() == "capture"
        except OSError:
            state_ok = True
        if not state_ok:
            return False
        return bool(self._fd_openers())

    def monitor_loop(self):
        no_capture_since = None
        while True:
            capturing = self.is_captured()
            with self.lock:
                is_active = self.is_active

            if capturing:
                no_capture_since = None
                if not is_active:
                    log("capturer detected (STREAMON + fd open) — activating")
                    threading.Thread(target=self.go_active, daemon=True).start()
            else:
                if is_active:
                    if no_capture_since is None:
                        no_capture_since = time.time()
                        log(f"capturer gone — switching to idle in {STOP_DELAY}s")
                    elif time.time() - no_capture_since >= STOP_DELAY:
                        no_capture_since = None
                        threading.Thread(target=self.go_idle, daemon=True).start()
                else:
                    no_capture_since = None
            time.sleep(0.3)

    def run(self):
        candidates = resolution_candidates()
        log(f"[res] candidates (highest first): {candidates}")

        started = False
        for w, h, fps in candidates:
            log(f"[res] trying {w}×{h}@{fps}...")
            self.build(w, h, fps)
            if self.start():
                log(f"[res] using {w}×{h}@{fps}fps")
                started = True
                break
            log(f"[res] {w}×{h}@{fps} failed, trying next")

        if not started:
            log("FATAL: all resolutions failed, exiting")
            sys.exit(1)

        threading.Thread(target=self.monitor_loop, daemon=True).start()
        GLib.MainLoop().run()

    def _on_bus(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log(f"GST ERROR [{msg.src.get_name()}]: {err.message}")
            if dbg:
                log(f"  debug: {dbg}")
        elif msg.type == Gst.MessageType.WARNING:
            w, _ = msg.parse_warning()
            log(f"GST WARN [{msg.src.get_name()}]: {w.message}")


if __name__ == "__main__":
    os.environ.setdefault("PIPEWIRE_RUNTIME_DIR", "/run/user/1000")
    CameraSwitch().run()
