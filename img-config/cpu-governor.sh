#!/bin/sh
# Set the CPU frequency governor (default schedutil) at boot. The kernel
# starts in performance, which keeps the CPU at its top speed all the time.
GOVERNOR="${1:-schedutil}"
# cpufreq-dt is a module, so the policy may appear a little after boot
for _ in $(seq 30); do
    [ -e /sys/devices/system/cpu/cpufreq/policy0/scaling_governor ] && break
    sleep 1
done
for policy in /sys/devices/system/cpu/cpufreq/policy*; do
    [ -w "$policy/scaling_governor" ] || continue
    if grep -qw "$GOVERNOR" "$policy/scaling_available_governors"; then
        echo "$GOVERNOR" > "$policy/scaling_governor"
        echo "$(basename "$policy"): $GOVERNOR"
    else
        echo "$(basename "$policy"): $GOVERNOR isn't available ($(cat "$policy/scaling_available_governors"))"
    fi
done
