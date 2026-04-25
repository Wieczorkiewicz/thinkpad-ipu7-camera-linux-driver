#!/usr/bin/env bash
# ipu7-camera-led installer
# Enables on-demand LED control for Intel IPU7 cameras (Lunar Lake / ThinkPad X1 2-in-1 Gen 10).
# The camera LED turns on only when an app actually uses the camera, and turns off when it stops.
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
ask()   { read -r -p "         $* [y/N] " _ans; [[ "$_ans" =~ ^[Yy]$ ]]; }

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

# python3-gi
python3 -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst, GLib" 2>/dev/null \
    || die "python3-gi / GStreamer Python bindings not found.\n  Fix: sudo apt install python3-gi gir1.2-gstreamer-1.0"

# icamerasrc
PIPEWIRE_RUNTIME_DIR="$PW_RUNTIME" gst-inspect-1.0 icamerasrc &>/dev/null \
    || die "icamerasrc GStreamer plugin not found.\n  Install the Intel IPU7 HAL and gstreamer-icamerasrc first."

# pipewiresink
PIPEWIRE_RUNTIME_DIR="$PW_RUNTIME" gst-inspect-1.0 pipewiresink &>/dev/null \
    || die "pipewiresink not found.\n  Fix: sudo apt install gstreamer1.0-plugins-bad"

# v4l2loopback
if ! modinfo v4l2loopback &>/dev/null; then
    die "v4l2loopback kernel module not found.\n  Fix: sudo apt install v4l2loopback-dkms"
fi

info "All prerequisites met."
echo ""

# ── step 1: v4l2loopback driver-name patch ─────────────────────────────────────
# Firefox and Zen (Firefox Flatpak) enumerate /dev/video* directly and reject
# devices whose VIDIOC_QUERYCAP driver field is "v4l2 loopback".
# We patch the source to report "uvcvideo" instead, then rebuild with DKMS.

echo "==> Patching v4l2loopback driver name..."

V4L2_SRC=$(find /usr/src -maxdepth 2 -name "v4l2loopback.c" 2>/dev/null | sort -V | tail -1)

if [[ -z "$V4L2_SRC" ]]; then
    warn "v4l2loopback source not found under /usr/src — skipping patch."
    warn "If Firefox/Zen cannot see the camera, reinstall v4l2loopback-dkms and re-run this script."
else
    if grep -q '"v4l2 loopback"' "$V4L2_SRC"; then
        V4L2_VER=$(basename "$(dirname "$V4L2_SRC")" | sed 's/v4l2loopback-//')
        info "Found v4l2loopback $V4L2_VER source at $V4L2_SRC"
        cp "$V4L2_SRC" "${V4L2_SRC}.orig"
        sed -i 's/"v4l2 loopback"/"uvcvideo"/g' "$V4L2_SRC"
        info "Patch applied. Rebuilding DKMS module (this may take ~30s)..."
        dkms build  "v4l2loopback/$V4L2_VER" -q 2>/dev/null || true
        dkms install "v4l2loopback/$V4L2_VER" --force -q 2>/dev/null || true
        info "v4l2loopback rebuilt."
    else
        info "v4l2loopback source already patched — skipping rebuild."
    fi
fi

# ── step 2: modprobe.d ─────────────────────────────────────────────────────────

echo "==> Installing modprobe config..."
install -m 644 "$SCRIPT_DIR/modprobe/v4l2loopback-ipu7.conf" \
               /etc/modprobe.d/v4l2loopback-ipu7.conf
info "/etc/modprobe.d/v4l2loopback-ipu7.conf installed."

# Reload module with new parameters
if lsmod | grep -q v4l2loopback; then
    # Stop the service if running so we can unload the module
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        info "Stopping $SERVICE_NAME to reload kernel module..."
        systemctl stop "$SERVICE_NAME"
    fi
    modprobe -r v4l2loopback 2>/dev/null || warn "Could not unload v4l2loopback — reboot may be needed to pick up new parameters."
fi
modprobe v4l2loopback
info "v4l2loopback loaded — /dev/video99 ready."

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
echo "│  Camera LED will now turn on only when an app       │"
echo "│  opens the camera, and turn off when it stops.      │"
echo "│                                                     │"
echo "│  Watch logs:  journalctl -u ipu7-camera-dynamic -f  │"
echo "│  Uninstall:   sudo bash uninstall.sh                │"
echo "└─────────────────────────────────────────────────────┘"
echo ""
