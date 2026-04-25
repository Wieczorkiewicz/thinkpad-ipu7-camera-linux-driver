# ipu7-camera

On-demand Intel IPU7 camera activation for Linux.

Tested on ThinkPad X1 2-in-1 Gen 10 (Intel Lunar Lake) running Ubuntu 24.04 / 26.04. The Intel IPU7 driver was merged into mainline Linux 6.17 and covers both Lunar Lake and the upcoming Panther Lake, so this project should work on any IPU7-equipped system where `icamerasrc` is functional — though only Lunar Lake has been tested so far.

**The problem:** Intel's IPU7 HAL (`icamerasrc`) cannot be shared between processes and keeps the camera sensor fully powered whenever it is running — even when no app is actually using the camera.

**What this does:** the HAL is started only when an app opens the camera, and fully shut down the moment the app stops. The camera LED, as a result, accurately reflects actual usage: on when someone is using the camera, off when no one is.

---

## How it works

A single Python/GStreamer process runs permanently and holds a dual-source pipeline:

```
videotestsrc (black frames) ──┐
                               ├─ input-selector ─ tee ─┬─ v4l2sink  → /dev/video32
icamerasrc (IPU7 camera)   ──┘                          └─ pipewiresink → PipeWire
```

- **IDLE:** `icamerasrc` is locked in NULL state — HAL not running, sensor off, LED off. `videotestsrc` feeds black frames to keep `/dev/video32` visible to apps that enumerate devices at startup.
- **ACTIVE:** when an app opens `/dev/video32`, `icamerasrc` transitions to PLAYING (HAL initialises, ~10–15 s on first use), the selector switches to real frames, LED on.
- When the app closes the camera, the service detects it within ~300 ms and drives `icamerasrc` back to NULL → HAL fully released, LED off.

Detection combines two signals:
1. `/sys/devices/virtual/video4linux/video32/state` leaves `"capture"` (VIDIOC_STREAMOFF)
2. No external process holds `/dev/video32` open

Both must be false before the HAL is shut down.

---

## Prerequisites

These must be working **before** running the installer:

| Component | How to get it |
|-----------|---------------|
| Linux kernel with IPU7 ISP support | Mainline Linux 6.17+ (merged upstream); Ubuntu 26.04 ships kernel 7.0.0+ which includes it |
| `intel-ipu7-dkms` (IPU7 kernel driver) | OEM / PPA / build from source |
| Intel Camera HAL (`libcamhal`) | Build from [intel/ipu7-camera-hal](https://github.com/intel/ipu7-camera-hal) |
| `icamerasrc` GStreamer plugin | Build from [intel/icamerasrc](https://github.com/intel/icamerasrc) |
| `v4l2loopback-dkms` | `sudo apt install v4l2loopback-dkms` |
| `python3-gi` | `sudo apt install python3-gi gir1.2-gstreamer-1.0` |
| `gstreamer1.0-plugins-bad` | `sudo apt install gstreamer1.0-plugins-bad` |

Verify `icamerasrc` is working before installing:
```bash
gst-launch-1.0 icamerasrc device-name=0 \
  ! "video/x-raw,format=NV12,width=1280,height=720,framerate=30/1" \
  ! videoconvert ! autovideosink
```
You should see a preview window and the camera LED should turn on.

---

## Install

### Debian / Ubuntu

Download the latest `.deb` from the [Releases](https://github.com/Wieczorkiewicz/thinkpad-ipu7-camera-linux-driver/releases) page and install:

```bash
sudo apt install ./ipu7-camera_1.0.1-1_all.deb
```

### Arch Linux (AUR)

```bash
yay -S thinkpad-ipu7-camera-linux-driver
```

Or manually:

```bash
git clone https://aur.archlinux.org/thinkpad-ipu7-camera-linux-driver.git
cd thinkpad-ipu7-camera-linux-driver
makepkg -si
```

### From source

```bash
git clone https://github.com/Wieczorkiewicz/thinkpad-ipu7-camera-linux-driver
cd thinkpad-ipu7-camera-linux-driver
sudo bash install.sh
```

The installer:
1. Checks all prerequisites
2. Installs a `modprobe.d` config to create `/dev/video32` (`"IPU7 Camera"`) at boot
3. Installs udev rules to expose `/dev/video32` to the logged-in user and hide the raw IPU7 sensor nodes
4. Installs a WirePlumber config
5. Installs the bridge script to `/usr/local/sbin/ipu7-camera-dynamic`
6. Enables and starts `ipu7-camera-dynamic.service`

### Uninstall

```bash
sudo bash uninstall.sh
```

---

## Usage

After installing, just open the camera normally in any app (Zoom, Brave, Signal, Telegram, WeChat, Element, …). The camera device appears as `"IPU7 Camera"` at `/dev/video32`.

Each app negotiates its own resolution with the device; the pipeline delivers whatever the app requests from the HAL's supported list.

```bash
# Quick test
gst-launch-1.0 v4l2src device=/dev/video32 ! videoconvert ! autovideosink
```

### Logs

```bash
journalctl -u ipu7-camera-dynamic -f
```

### Resolution

Resolution is selected automatically from the HAL's supported list, highest first (default: 3840×2160@30). Override with an environment variable:

```bash
# In /etc/systemd/system/ipu7-camera-dynamic.service [Service] section:
Environment=CAMERA_RESOLUTION=1920x1080x60
```

---

## Known limitations

- **First activation after boot may take a few seconds** while the HAL and firmware initialise. In practice this has not been noticeably slow. Subsequent activations are near-instant.
- **Firefox / Zen (help wanted):** camera access via the xdg-desktop-portal Camera interface does not currently work. The device is detected (`getUserMedia()` resolves the device list), but the stream never starts.

  The path Firefox uses: Firefox → xdg-desktop-portal (Camera interface) → PipeWire → WirePlumber v4l2 node for `/dev/video32`.

  What we know: WirePlumber successfully creates a PipeWire node from `/dev/video32` and the portal connects to it, but the connection drops after ~2 seconds — before any frames are delivered to the browser. The `pipewiresink` node in our pipeline has `object.register=false` and is therefore invisible to the portal; the portal uses the separate WirePlumber-managed node instead. The drop appears to be a format negotiation failure between Firefox's PipeWire consumer and the WirePlumber node, but the root cause has not been pinned down. KDE Plasma 6 + Wayland runs two portal backends (KDE and GTK) simultaneously, which may also be a factor.

  Other apps work fine: native V4L2 apps (Zoom, Telegram desktop) read `/dev/video32` directly; Flatpak apps (Brave, Signal) connect via PipeWire to the `pipewiresink` node.

  If you have experience with WirePlumber session management or xdg-desktop-portal on KDE Plasma 6, contributions are very welcome.

---

## Technical notes

### Why v4l2loopback?

`icamerasrc` requires Intel's closed-source HAL and cannot be used by multiple processes simultaneously. The v4l2loopback device `/dev/video32` acts as a broker: a single GStreamer process owns `icamerasrc` and writes frames to the loopback; any number of apps read from it concurrently.

### Why /dev/video32?

The device number matters. Apps like Zoom and Telegram do not enumerate the full `/dev/video*` range — they stop at some upper limit. Testing confirmed that `/dev/video34` and `/dev/video35` were still visible to Zoom, while `/dev/video99` was not. The exact cutoff is unknown but likely around 63 (a natural power-of-two boundary). `/dev/video32` was chosen as a safe, low number well within range, while leaving `/dev/video0`–`/dev/video31` free for other devices. The raw IPU7 sensor nodes (`/dev/video30`, `/dev/video31`) are hidden from non-root users via udev to prevent apps from accidentally trying to open them directly.

### Why NULL and not PAUSED?

Intel's IPU7 HAL keeps the camera sensor powered — LED on — even in GStreamer's PAUSED state. Only transitioning to NULL fully releases the sensor. This is the only way to reliably release hardware between uses; in practice reactivation is near-instant.

### colorimetry

`icamerasrc` outputs NV12. The pipeline converts to YUY2 with `colorimetry=bt601` before writing to v4l2loopback, matching what `videotestsrc` produces in IDLE state and avoiding format negotiation failures on activation.

---

## Other sensors on the same hardware (potential future work)

The ThinkPad X1 2-in-1 Gen 10 (Lunar Lake) has two more sensor subsystems beyond the RGB camera. On Windows, these work together as a seamless unlock sequence: the presence sensor wakes the machine when someone approaches, the IR illuminator and IR camera activate for Windows Hello face recognition, and an IR floodlight briefly fires to assist in low light. If the face is recognised the screen unlocks immediately; otherwise it falls back to fingerprint or PIN. All three components are physically present on Linux — none of them are fully functional yet.

**IR camera (face recognition)**

The IR sensor (`INT347D`, managed by Intel CVS `INTC10DE`) is physically present and its kernel driver (`intel_cvs`) loads successfully. It does not appear as a usable video node today — no `icamerasrc` device index has been confirmed for it. There is no Linux face-recognition framework that supports it yet, but the hardware path through the IPU7 ISP may be explorable via `icamerasrc device-name=1` or similar.

Note: the ThinkPad physical camera shutter covers only the RGB camera, not the IR camera. When the shutter is closed, Windows Hello cannot recognise the face even though the IR camera remains unobstructed — confirming that the RGB camera is also part of the recognition pipeline, not just the IR sensor.

**Presence detection**

A human-presence sensor (also routed through Intel CVS) feeds data via the Intel Sensor Hub (ISH) and surfaces as 23 `HID-SENSOR-2000e1` nodes in sysfs — not as a video node. The ISH firmware is already installed at `/lib/firmware/LENOVO/ish/`.

In practice, the sysfs values do not update: the `intel_cvs` driver has a resume bug where the CVS chip's response GPIO stays asserted after the driver's own suspend cycle (`cvs_resume: Wrong gpio_response val:1 read via bridge`). The driver logs the error but does not attempt a GPIO reset, leaving the CVS chip unable to communicate with the ISH firmware. As a result, `enable_sensor` eventually returns `Invalid argument` and no presence data flows. This is a driver-level issue requiring a fix in `intel_cvs.c` (the reset GPIO `icvs->rst` exists but is unused in the resume path).

Both are independent of this project and do not require any changes here to develop further.
