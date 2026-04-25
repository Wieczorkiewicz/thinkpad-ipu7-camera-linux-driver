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
sudo apt install ./ipu7-camera_1.0.0-1_all.deb
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

The device number matters. Apps like Zoom and Telegram enumerate `/dev/video0` through approximately `/dev/video33` and stop there — they simply do not see higher-numbered devices. Earlier versions of this project used `/dev/video99`, which worked for PipeWire-based apps (Brave, Signal) but was invisible to Zoom and Telegram entirely. `/dev/video32` was chosen as the highest number that reliably falls within the scan range of these apps while leaving `/dev/video0`–`/dev/video31` free for other devices. The raw IPU7 sensor nodes (`/dev/video30`, `/dev/video31`) are hidden from non-root users via udev to prevent apps from accidentally trying to open them directly.

### Why NULL and not PAUSED?

Intel's IPU7 HAL keeps the camera sensor powered — LED on — even in GStreamer's PAUSED state. Only transitioning to NULL fully releases the sensor. This costs ~10–15 s on each activation but is the only way to reliably release hardware between uses.

### colorimetry

`icamerasrc` outputs NV12. The pipeline converts to YUY2 with `colorimetry=bt601` before writing to v4l2loopback, matching what `videotestsrc` produces in IDLE state and avoiding format negotiation failures on activation.

---

## Other sensors on the same hardware (potential future work)

The ThinkPad X1 2-in-1 Gen 10 (Lunar Lake) has two more sensor subsystems beyond the RGB camera:

**IR camera (face recognition)**

The IR sensor (`INT347D`, managed by Intel CVS `INTC10DE`) is physically present and its kernel driver (`intel_cvs`) loads successfully. It does not appear as a usable video node today — no `icamerasrc` device index has been confirmed for it. On Windows it powers face-unlock (Windows Hello). There is no Linux face-recognition framework that supports it yet, but the hardware path through the IPU7 ISP may be explorable via `icamerasrc device-name=1` or similar.

**Presence detection**

A human-presence sensor (also routed through Intel CVS) feeds data via the Intel Sensor Hub (ISH) and surfaces as `HID-SENSOR-2000e1` in sysfs — not as a video node. Writing `1` to its `enable_sensor` attribute appears to activate it, but interpreting the output data and building a userspace daemon that triggers screen wake / lock has not been attempted. The ISH firmware is already installed at `/lib/firmware/LENOVO/ish/`.

Both are independent of this project and do not require any changes here to develop further.
