# ipu7-camera-led

On-demand LED control for Intel IPU7 cameras on Linux.

Targets ThinkPad X1 2-in-1 Gen 10 (Intel Lunar Lake) running Ubuntu 24.04 / 26.04, but should work on any system where `icamerasrc` is functional.

**Without this:** the camera LED is always on while any app could use the camera (bridge service always running), or requires manual management.

**With this:** the LED turns on only when an app opens the camera, and turns off within ~1 second after the app stops.

---

## How it works

A single Python/GStreamer process runs permanently. It holds a static dual-source pipeline:

```
videotestsrc (black frames) ──┐
                               ├─ input-selector ─ tee ─┬─ v4l2sink  → /dev/video99
icamerasrc (IPU7 camera)   ──┘                          └─ pipewiresink → PipeWire
```

- **IDLE:** `icamerasrc` is locked in NULL state (HAL not active, LED off). `videotestsrc` feeds black frames to keep `/dev/video99` alive for apps that check it.
- **ACTIVE:** when an app opens `/dev/video99`, `icamerasrc` starts up (10–15 s first time, faster on repeat), the selector switches, real frames flow, LED on.
- When the app closes the camera, the service detects it within ~300 ms and shuts `icamerasrc` back to NULL → LED off.

Detection uses two signals combined (whichever fires first):
1. `/sys/devices/virtual/video4linux/video99/state` changes away from `"capture"` (VIDIOC_STREAMOFF)
2. No external process holds `/dev/video99` open (fd-based check)

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
The camera LED should turn on and you should see a preview window.

---

## Install

```bash
git clone https://github.com/YOUR_USERNAME/ipu7-camera-led
cd ipu7-camera-led
sudo bash install.sh
```

The installer:
1. Checks all prerequisites
2. Patches `v4l2loopback` to report `driver="uvcvideo"` (required for Firefox / Zen to enumerate the camera)
3. Installs a `modprobe.d` config to create `/dev/video99` at boot
4. Installs a WirePlumber config to hide raw IPU7 nodes and expose a clean camera node
5. Installs the bridge script to `/usr/local/sbin/ipu7-camera-dynamic`
6. Enables and starts `ipu7-camera-dynamic.service`

### Uninstall

```bash
sudo bash uninstall.sh
```

---

## Usage

After installing, just use the camera normally:

- Open any camera app (Brave, Firefox, Signal, `gst-launch-1.0 v4l2src device=/dev/video99 ! autovideosink`, …)
- LED turns on automatically (first activation takes 10–15 s while HAL initialises; subsequent ones are similar since the HAL fully closes to turn the LED off)
- Stop using the camera → LED turns off within ~1 s

### Logs

```bash
journalctl -u ipu7-camera-dynamic -f
```

---

## Known limitations

- **First / every activation takes 10–15 s.** The HAL must fully initialise from NULL each time. This is required to turn the LED off reliably — keeping the HAL in PAUSED state would leave the LED on.
- **Zen browser (Firefox Flatpak):** if Zen is running and has accessed any raw IPU7 node (`/dev/video0`–`/dev/video31`), it holds kernel DMA buffers and blocks the HAL. Close Zen before using other camera apps.
- **Telegram / Zoom:** not tested.

---

## Technical notes

### Why v4l2loopback?

`icamerasrc` requires Intel's closed-source HAL and cannot be used by multiple processes simultaneously. A v4l2loopback device (`/dev/video99`) acts as a broker: a single GStreamer process owns `icamerasrc` and writes frames to the loopback; any number of apps read from it.

### Why the driver-name patch?

Firefox-based browsers enumerate `/dev/video*` directly using `VIDIOC_QUERYCAP` and reject any device whose `driver` field is not `"uvcvideo"`. The patch changes the string in the v4l2loopback kernel module source and rebuilds via DKMS.

### Why NULL and not PAUSED?

Intel's IPU7 HAL keeps the camera sensor powered (LED on) even in GStreamer's PAUSED state. Only transitioning to NULL fully releases the sensor. This costs ~10–15 s on re-activation but is the only way to reliably turn off the LED.

### colorimetry

`icamerasrc` outputs NV12 with BT.709 colorimetry. The v4l2sink on video99 expects bt601 (from the idle `videotestsrc`). The bridge enforces `colorimetry=bt601` in the caps after `videoconvert` to prevent a mismatch that causes black frames.
