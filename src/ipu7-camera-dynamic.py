#!/usr/bin/env python3
"""
ipu7-camera-dynamic — 静态双 source pipeline：
  IDLE:   icamerasrc 锁在 NULL（启动时跳过 HAL 初始化），videotestsrc 输出黑帧
  ACTIVE: icamerasrc NULL→PLAYING（首次约 10-15s），selector 切换到它
          go_idle 后 icamerasrc 回到 NULL（HAL 完全释放），再次激活需重新初始化

关键：icamerasrc 在管道 PAUSED 之前就锁住（停在 NULL）。
不经历 "PAUSED 后被 selector active-pad 变化打断" 的 RECONFIGURE 问题，
streaming task 不会在错误时刻运行，彻底避免 not-negotiated / VIDIOC_REQBUFS EBUSY。

分辨率自动选择：启动时按 CAMERA_RESOLUTION 环境变量（auto|WxH）从 HAL JSON 读取
支持的分辨率列表，从高到低依次尝试，首个成功的分辨率作为管道分辨率。
"""
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GLib
import json as _json
import os, re, sys, time, threading

Gst.init(None)

DEVICE      = "/dev/video99"
SYSFS_STATE = "/sys/devices/virtual/video4linux/video99/state"
STOP_DELAY  = 0
HAL_JSON    = "/etc/camera/ipu7x/sensors/ov08x40-uf.json"

# Priority order — (width, height, fps), highest first.
# 1080p/60 is probed first; HAL JSON only declares 30fps max, so it will likely
# fail and fall through — but costs nothing to try.
PREFERRED_RESOLUTIONS = [
    (3840, 2160, 30),   # confirmed working: full sensor resolution, hardware-limited to 30fps
    (1920, 1080, 60),   # confirmed working: HAL JSON declares 30fps max but 60 accepted
    (1920, 1080, 30),
    (1280,  720, 30),
    ( 640,  480, 30),
    ( 640,  360, 30),
]

PW_PROPS = (
    "props,media.class=Video/Source,media.role=Camera,"
    "node.name=ipu7-hal-camera,node.description=Front-Camera,"
    "node.nick=Front-Camera,api.v4l2.cap.driver=uvcvideo,"
    "api.v4l2.cap.card=Front-Camera,device.description=Front-Camera"
)

def log(msg): print(msg, flush=True)


def read_hal_resolutions():
    """
    解析 HAL JSON，返回按 PREFERRED_RESOLUTIONS 优先级排好序的 (w, h, fps) 列表。
    HAL JSON 的 fpsRange 给出支持的 fps 上限（通常 30）；
    60fps 探测候选保留在列表顶部，让 start() 决定是否可行。
    """
    try:
        raw = open(HAL_JSON).read()
        raw = re.sub(r'//[^\n]*', '', raw)   # strip // comments
        data = _json.loads(raw)
        meta = data["CameraSettings"]["Sensor"][0]["StaticMetadata"]
        cfgs = meta["supportedStreamConfig"]
        supported_sizes = {(c["size"][0], c["size"][1]) for c in cfgs}
        # fpsRange = list of (min, max) pairs → take overall max
        fps_vals = meta.get("fpsRange", [30])
        hal_max_fps = max(fps_vals)
        # Keep PREFERRED_RESOLUTIONS order exactly; include all sizes in supported_sizes
        # regardless of fps (probe candidates like 60fps stay in declared position).
        high_fps = [(w, h, fps) for w, h, fps in PREFERRED_RESOLUTIONS if fps > hal_max_fps]
        if high_fps:
            log(f"[res] HAL 声明最高 {hal_max_fps}fps；仍探测高帧率候选: {high_fps}")
        result = [
            (w, h, fps) for w, h, fps in PREFERRED_RESOLUTIONS
            if (w, h) in supported_sizes
        ]
        if result:
            return result
    except Exception as e:
        log(f"[res] 无法解析 HAL JSON ({HAL_JSON}): {e}")
    return [(1920, 1080, 30), (1280, 720, 30)]   # safe fallback


def resolution_candidates():
    """
    返回要依次尝试的 (width, height, fps) 列表。
    CAMERA_RESOLUTION=auto（默认）→ 从 HAL JSON 读取，高到低排序。
    CAMERA_RESOLUTION=1920x1080   → 直接使用，fps 默认 30。
    CAMERA_RESOLUTION=1920x1080x60 → 使用指定 fps。
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
        log(f"[res] 无法解析 CAMERA_RESOLUTION={cfg!r}，退回到 auto")
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
        self.res_w     = 1280
        self.res_h     = 720
        self.res_fps   = 30

    # ── 构建 pipeline ──────────────────────────────────────────────────────────

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
        """销毁当前 pipeline（分辨率尝试失败时用）。"""
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline.get_state(5 * Gst.SECOND)
            self.pipeline = None
            self.sel = self.icam = None

    def _res_tag(self):
        return f"{self.res_w}×{self.res_h}@{self.res_fps}"

    def start(self):
        """
        尝试将 pipeline 带到 PLAYING 状态。
        成功返回 True；失败返回 False（并清理 pipeline）。
        """
        self.icam.set_locked_state(True)

        log(f"[{self._res_tag()}] Pipeline → PAUSED（icamerasrc 锁在 NULL）...")
        self.pipeline.set_state(Gst.State.PAUSED)
        ret = self.pipeline.get_state(15 * Gst.SECOND)
        if ret[0] == Gst.StateChangeReturn.FAILURE:
            log(f"[{self._res_tag()}] PAUSED 失败")
            self._teardown()
            return False
        log(f"[{self._res_tag()}] PAUSED OK")

        # 找 sel 的两个 sink pad
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
            log(f"FATAL: 找不到 sel 的 sink pads，找到：{list(pads.keys())}")
            self._teardown()
            return False
        log(f"idle pad: {self.idle_pad.name}, icam pad: {self.icam_pad.name}")

        # selector 指向 idle（videotestsrc），icamerasrc 仍在 NULL
        self.sel.set_property("active-pad", self.idle_pad)

        # 整个 pipeline → PLAYING（icamerasrc 因 locked_state 留在 NULL）
        self.pipeline.set_state(Gst.State.PLAYING)
        log(f"Pipeline running — IDLE @ {self._res_tag()}（黑帧输出）")
        return True

    # ── 状态切换 ───────────────────────────────────────────────────────────────

    def go_active(self):
        with self.lock:
            if self.is_active:
                return
            self.is_active = True

        log(f"激活摄像头（icamerasrc → PLAYING，首次约 10-15s）@ {self._res_tag()}")
        self.icam.set_locked_state(False)
        self.icam.set_state(Gst.State.PLAYING)

        # 等待 PLAYING（NULL→PLAYING 含 HAL 初始化，耗时较长；PAUSED→PLAYING 很快）
        ret = self.icam.get_state(20 * Gst.SECOND)
        if ret[0] == Gst.StateChangeReturn.FAILURE:
            log("ERROR: icamerasrc 无法到达 PLAYING，取消激活")
            self.icam.set_locked_state(True)
            with self.lock:
                self.is_active = False
            return

        time.sleep(0.5)  # 让第一帧填充队列
        self.sel.set_property("active-pad", self.icam_pad)
        log("ACTIVE — 真实摄像头输出")

    def go_idle(self):
        with self.lock:
            if not self.is_active:
                return
            self.is_active = False

        log("切换到 idle...")
        self.sel.set_property("active-pad", self.idle_pad)
        time.sleep(0.3)
        # NULL: HAL 完全释放 sensor。下次激活需重新初始化（10-15s）。
        # PAUSED 不足以关闭 Intel HAL sensor 电源。
        self.icam.set_state(Gst.State.NULL)
        ret = self.icam.get_state(20 * Gst.SECOND)
        self.icam.set_locked_state(True)
        log("IDLE — 摄像头已释放")

    # ── capturer 监测 ──────────────────────────────────────────────────────────

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
        # Requires BOTH: sysfs state=="capture" (STREAMON active) AND fd held by external process.
        # If either is gone, camera is not truly in use:
        #   - STREAMOFF fired → state changes → fast deactivation
        #   - fd closed without STREAMOFF → fd check fails → handles stale sysfs state
        try:
            state_ok = open(SYSFS_STATE).read().strip() == "capture"
        except OSError:
            state_ok = True  # can't read → be conservative, fall through to fd check
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
                    log("检测到 capturer（STREAMON + fd）→ 激活")
                    threading.Thread(target=self.go_active, daemon=True).start()
            else:
                if is_active:
                    if no_capture_since is None:
                        no_capture_since = time.time()
                        log(f"capturer 停止 — {STOP_DELAY}s 后切换到 idle")
                    elif time.time() - no_capture_since >= STOP_DELAY:
                        no_capture_since = None
                        threading.Thread(target=self.go_idle, daemon=True).start()
                else:
                    no_capture_since = None
            time.sleep(0.3)

    # ── 入口 ───────────────────────────────────────────────────────────────────

    def run(self):
        candidates = resolution_candidates()
        log(f"[res] 分辨率候选（高到低）: {candidates}")

        started = False
        for w, h, fps in candidates:
            log(f"[res] 尝试 {w}×{h}@{fps} ...")
            self.build(w, h, fps)
            if self.start():
                log(f"[res] 使用分辨率 {w}×{h}@{fps}fps")
                started = True
                break
            log(f"[res] {w}×{h}@{fps} 失败，尝试下一个")

        if not started:
            log("FATAL: 所有分辨率均失败，退出")
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
