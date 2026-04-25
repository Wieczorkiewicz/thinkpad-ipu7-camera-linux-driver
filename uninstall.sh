#!/usr/bin/env bash
# ipu7-camera-led uninstaller
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo bash uninstall.sh" >&2; exit 1; }

TARGET_USER="${SUDO_USER:-}"
[[ -n "$TARGET_USER" ]] || { echo "Run via sudo, not as root directly." >&2; exit 1; }
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)

echo "Removing ipu7-camera-dynamic service..."
systemctl stop    ipu7-camera-dynamic 2>/dev/null || true
systemctl disable ipu7-camera-dynamic 2>/dev/null || true
rm -f /etc/systemd/system/ipu7-camera-dynamic.service
systemctl daemon-reload

echo "Removing bridge script..."
rm -f /usr/local/sbin/ipu7-camera-dynamic

echo "Removing modprobe config..."
rm -f /etc/modprobe.d/v4l2loopback-ipu7.conf

echo "Removing udev rules..."
rm -f /etc/udev/rules.d/99-ipu7-camera-loopback.rules
rm -f /etc/udev/rules.d/90-ipu7-hide.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=video4linux

echo "Removing WirePlumber config..."
rm -f "$TARGET_HOME/.config/wireplumber/wireplumber.conf.d/51-ipu7-camera.conf"

echo ""
echo "Uninstall complete."
echo "Reboot or reload modules to apply changes."
