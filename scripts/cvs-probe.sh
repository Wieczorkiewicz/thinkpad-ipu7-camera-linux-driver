#!/usr/bin/env bash
# cvs-probe.sh — explore Intel CVS (presence sensor + IR camera) sysfs interfaces
# Usage:
#   sudo bash cvs-probe.sh            # dump all sensor state
#   sudo bash cvs-probe.sh on         # enable presence sensor, poll values
#   sudo bash cvs-probe.sh off        # disable presence sensor
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo bash $0 [on|off]"; exit 1; }

MODE="${1:-dump}"

# ── find all HID-SENSOR-2000e1 instances ──────────────────────────────────────

mapfile -t NODES < <(find /sys/bus/platform/devices -maxdepth 1 \
    -name "HID-SENSOR-2000e1*.auto" | sort)

if [[ ${#NODES[@]} -eq 0 ]]; then
    echo "No HID-SENSOR-2000e1 devices found — CVS ISH driver not loaded?"
    exit 1
fi

echo "Found ${#NODES[@]} HID-SENSOR-2000e1 instance(s)"
echo ""

# ── helpers ───────────────────────────────────────────────────────────────────

read_inputs() {
    local node="$1"
    for input_dir in "$node"/input-*/; do
        [[ -d "$input_dir" ]] || continue
        local name_file=$(ls "$input_dir"*-name 2>/dev/null | head -1)
        local val_file=$(ls  "$input_dir"*-value 2>/dev/null | head -1)
        [[ -f "$name_file" && -f "$val_file" ]] || continue
        local name val
        name=$(cat "$name_file" 2>/dev/null)
        val=$(cat  "$val_file"  2>/dev/null)
        printf "    %-40s %s\n" "$name" "$val"
    done
}

# ── dump mode ─────────────────────────────────────────────────────────────────

if [[ "$MODE" == "dump" ]]; then
    for node in "${NODES[@]}"; do
        label=$(basename "$node")
        enabled=$(cat "$node/enable_sensor" 2>/dev/null || echo "?")
        echo "[$label]  enable_sensor=$enabled"
        read_inputs "$node"
        echo ""
    done

    echo "CVS ACPI node: /sys/bus/acpi/devices/INTC10DE:00"
    ls /sys/bus/acpi/devices/INTC10DE:00/ 2>/dev/null || true
    echo ""
    echo "IR camera ACPI node: /sys/bus/acpi/devices/INT347D:00"
    ls /sys/bus/acpi/devices/INT347D:00/ 2>/dev/null || true
    exit 0
fi

# ── on/off mode: operate on the first instance with the most inputs ───────────

# Pick the instance with the most input-* entries (likely the presence sensor)
BEST_NODE=""
BEST_COUNT=0
for node in "${NODES[@]}"; do
    count=$(find "$node" -maxdepth 1 -name "input-*" -type d | wc -l)
    if (( count > BEST_COUNT )); then
        BEST_COUNT=$count
        BEST_NODE="$node"
    fi
done

echo "Using: $(basename "$BEST_NODE")  ($BEST_COUNT inputs)"

if [[ "$MODE" == "off" ]]; then
    echo 0 > "$BEST_NODE/enable_sensor"
    echo "Presence sensor disabled."
    exit 0
fi

if [[ "$MODE" == "on" ]]; then
    echo 1 > "$BEST_NODE/enable_sensor"
    echo "Presence sensor enabled. Polling every 0.5s (Ctrl-C to stop)..."
    echo ""
    while true; do
        printf "\r[$(date +%T)]  "
        read_inputs "$BEST_NODE" | tr '\n' '|'
        sleep 0.5
    done
fi

echo "Unknown mode: $MODE  (use: on | off | dump)"
exit 1
