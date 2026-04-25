#!/usr/bin/env bash
# ipu7-camera-led installer
# On-demand LED control for Intel IPU7 cameras (Lunar Lake / ThinkPad X1 2-in-1 Gen 10).
# The camera LED turns on only when an app uses the camera, and off when it stops.
# Apps see a virtual camera at /dev/video32 ("IPU7 Camera"), visible to Zoom and most
# V4L2-based apps. A PipeWire node is also published for Flatpak/portal-based apps.
#
# Prerequisites (must be installed before running this script):
#   - icamerasrc GStreamer element  (from Intel IPU7 HAL + gstreamer-icamerasrc)
#   - v4l2loopback-dkms             (sudo apt install v4l2loopback-dkms)
#   - python3-gi                    (sudo apt install python3-gi gir1.2-gstreamer-1.0)
#   - gstreamer1.0-plugins-bad      (for pipewiresink)
#
# Usage:  sudo bash install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="ipu7-camera-dynamic"

# ── helpers ────────────────────────────────────────────────────────────────────

info()  { echo "[  OK  ] $*"; }
warn()  { echo "[ WARN ] $*"; }
die()   { echo "[ FAIL ] $*" >&2; exit 1; }

# ── root + user detection ──────────────────────────────────────────────────────

[[ $EUID -eq 0 ]] || die "Run with sudo:  sudo bash install.sh"

TARGET_USER="${SUDO_USER:-}"
[[ -n "$TARGET_USER" ]] || die "Could not detect the invoking user. Run via sudo, not as root directly."

TARGET_UID=$(id -u "$TARGET_USER")
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
PW_RUNTIME="/run/user/$TARGET_UID"

echo ""
echo "ipu7-camera-led installer"
echo "  target user : $TARGET_USER (uid=$TARGET_UID)"
echo "  PipeWire dir: $PW_RUNTIME"
echo ""

# ── prerequisite checks ────────────────────────────────────────────────────────

echo "Checking prerequisites..."

python3 -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst, GLib" 2>/dev/null \
    || die "python3-gi / GStreamer Python bindings not found.\n  Fix: sudo apt install python3-gi gir1.2-gstreamer-1.0"

PIPEWIRE_RUNTIME_DIR="$PW_RUNTIME" gst-inspect-1.0 icamerasrc &>/dev/null \
    || die "icamerasrc GStreamer plugin not found.\n  Install the Intel IPU7 HAL and gstreamer-icamerasrc first."

PIPEWIRE_RUNTIME_DIR="$PW_RUNTIME" gst-inspect-1.0 pipewiresink &>/dev/null \
    || die "pipewiresink not found.\n  Fix: sudo apt install gstreamer1.0-plugins-bad"

if ! modinfo v4l2loopback &>/dev/null; then
    die "v4l2loopback kernel module not found.\n  Fix: sudo apt install v4l2loopback-dkms"
fi

info "All prerequisites met."
echo ""

# ── step 1: modprobe.d ─────────────────────────────────────────────────────────

echo "==> Installing modprobe config..."
install -m 644 "$SCRIPT_DIR/modprobe/v4l2loopback-ipu7.conf" \
               /etc/modprobe.d/v4l2loopback-ipu7.conf
info "/etc/modprobe.d/v4l2loopback-ipu7.conf installed."

# Reload module with new parameters
if lsmod | grep -q v4l2loopback; then
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        info "Stopping $SERVICE_NAME to reload kernel module..."
        systemctl stop "$SERVICE_NAME"
    fi
    modprobe -r v4l2loopback 2>/dev/null \
        || warn "Could not unload v4l2loopback — reboot may be needed to pick up new parameters."
fi
modprobe v4l2loopback
info "v4l2loopback loaded — /dev/video32 ready."

# ── step 2: udev rules ─────────────────────────────────────────────────────────

echo "==> Installing udev rules..."
install -m 644 "$SCRIPT_DIR/udev/99-ipu7-camera-loopback.rules" \
               /etc/udev/rules.d/99-ipu7-camera-loopback.rules
install -m 644 "$SCRIPT_DIR/udev/90-ipu7-hide.rules" \
               /etc/udev/rules.d/90-ipu7-hide.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=video4linux
info "udev rules installed and reloaded."

# ── step 3: WirePlumber config ─────────────────────────────────────────────────

echo "==> Installing WirePlumber config..."
WP_DIR="$TARGET_HOME/.config/wireplumber/wireplumber.conf.d"
mkdir -p "$WP_DIR"
chown "$TARGET_USER:$TARGET_USER" \
      "$TARGET_HOME/.config/wireplumber" \
      "$WP_DIR" 2>/dev/null || true
install -m 644 -o "$TARGET_USER" -g "$TARGET_USER" \
    "$SCRIPT_DIR/wireplumber/51-ipu7-camera.conf" \
    "$WP_DIR/51-ipu7-camera.conf"
info "WirePlumber config installed → $WP_DIR/51-ipu7-camera.conf"
info "WirePlumber will pick it up on next login (or: systemctl --user restart wireplumber)"

# ── step 4: bridge script ──────────────────────────────────────────────────────

echo "==> Installing bridge script..."
install -m 755 "$SCRIPT_DIR/src/ipu7-camera-dynamic.py" \
               /usr/local/sbin/ipu7-camera-dynamic
info "/usr/local/sbin/ipu7-camera-dynamic installed."

# ── step 5: systemd service ────────────────────────────────────────────────────

echo "==> Installing systemd service..."
sed "s|__PW_RUNTIME__|$PW_RUNTIME|g" \
    "$SCRIPT_DIR/systemd/ipu7-camera-dynamic.service" \
    > /etc/systemd/system/ipu7-camera-dynamic.service
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
info "Service enabled and started."

# ── done ───────────────────────────────────────────────────────────────────────

echo ""
echo "┌─────────────────────────────────────────────────────┐"
echo "│  ipu7-camera-led installed successfully             │"
echo "│                                                     │"
echo "│  Camera device : /dev/video32  (\"IPU7 Camera\")      │"
echo "│  LED turns on when an app uses the camera,          │"
echo "│  and off when it stops.                             │"
echo "│                                                     │"
echo "│  Watch logs:  journalctl -u ipu7-camera-dynamic -f  │"
echo "│  Uninstall:   sudo bash uninstall.sh                │"
echo "└─────────────────────────────────────────────────────┘"
echo ""
