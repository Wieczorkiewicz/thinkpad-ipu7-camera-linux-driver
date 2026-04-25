# ipu7-camera-led

On-demand Intel IPU7 camera activation for Linux.

Targets ThinkPad X1 2-in-1 Gen 10 (Intel Lunar Lake) running Ubuntu 24.04 / 26.04, but should work on any system where `icamerasrc` is functional.

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
| Linux kernel with IPU7 ISP support | Ubuntu 26.04 kernel 7.0.0+ includes it |
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

```bash
git clone https://github.com/Wieczorkiewicz/thinkpad-ipu7-camera-linux-driver-led
cd ipu7-camera-led
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

After installing, just open the camera normally in any app (Zoom, Brave, Signal, Telegram, …). The camera device appears as `"IPU7 Camera"` at `/dev/video32`.

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

- **Every activation takes ~10–15 s.** The HAL must initialise from scratch each time the camera is opened. This is the cost of fully releasing the sensor between uses — keeping the HAL in PAUSED would be faster but would leave the sensor (and LED) on permanently.
- **Firefox / Zen:** camera access via the xdg-desktop-portal Camera interface does not currently work. Direct V4L2 access (most native apps) and PipeWire access (Flatpak apps) both work.

---

## Technical notes

### Why v4l2loopback?

`icamerasrc` requires Intel's closed-source HAL and cannot be used by multiple processes simultaneously. The v4l2loopback device `/dev/video32` acts as a broker: a single GStreamer process owns `icamerasrc` and writes frames to the loopback; any number of apps read from it concurrently.

### Why NULL and not PAUSED?

Intel's IPU7 HAL keeps the camera sensor powered — LED on — even in GStreamer's PAUSED state. Only transitioning to NULL fully releases the sensor. This costs ~10–15 s on each activation but is the only way to reliably release hardware between uses.

### colorimetry

`icamerasrc` outputs NV12. The pipeline converts to YUY2 with `colorimetry=bt601` before writing to v4l2loopback, matching what `videotestsrc` produces in IDLE state and avoiding format negotiation failures on activation.
