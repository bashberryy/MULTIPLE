#!/bin/bash
set -e

# ── liability / usage warning ────────────────────────────────────────────────
clear
cat /etc/issue 2>/dev/null || echo "  MULTIPLE"
echo ""
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║              MULTIPLE — USAGE WARNING                   ║"
echo "  ╠══════════════════════════════════════════════════════════╣"
echo "  ║  This tool modifies low-level Chromebook firmware and   ║"
echo "  ║  security state (TPM, GBB, enrollment, dev mode).       ║"
echo "  ║                                                          ║"
echo "  ║  • YOU are solely responsible for what happens.         ║"
echo "  ║  • This may void your warranty.                         ║"
echo "  ║  • It does NOT bypass legitimate access controls.       ║"
echo "  ║  • Not for use on devices you don't own.                ║"
echo "  ║                                                          ║"
echo "  ║  Created by bashberryy. Use at your own risk.           ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo ""
read -p "  I understand and accept full liability. Continue? [y/N] " _ack
case "$_ack" in
    y|Y|yes|YES) ;;
    *) echo "  Aborted."; sleep 2; reboot -f ;;
esac

BANNER=/etc/issue

# ── helpers ──────────────────────────────────────────────────────────────────
get_board() {
    local b
    b="$(crossystem hwid 2>/dev/null | awk '{print tolower($1)}')"
    [ -z "$b" ] && b="$(grep CHROMEOS_RELEASE_BOARD /etc/lsb-release 2>/dev/null | cut -d= -f2)"
    echo "${b:-unknown}"
}

get_kernver() {
    local raw
    raw="$(crossystem tpm_kernver 2>/dev/null)"
    [ -z "$raw" ] && echo "unknown" && return
    printf "%d" "$((raw & 0xff))" 2>/dev/null || echo "unknown"
}

get_rollback_version() {
    local raw
    raw="$(crossystem tpm_kernver 2>/dev/null)"
    [ -z "$raw" ] && echo "unknown" && return
    printf "%d" "$(((raw >> 8) & 0xff))" 2>/dev/null || echo "unknown"
}

get_milestone() {
    grep CHROMEOS_RELEASE_MILESTONE /etc/lsb-release 2>/dev/null | cut -d= -f2
}

get_bios_version() {
    crossystem ro_fwid 2>/dev/null || echo "unknown"
}

setup_disk_vars() {
    intdis="$(get_fixed_dst_drive 2>/dev/null)"
    if [ -z "$intdis" ]; then
        for dev in /sys/block/mmcblk* /sys/block/sd* /sys/block/nvme*; do
            [ -d "$dev" ] || continue
            [ "$(cat "$dev/removable" 2>/dev/null)" = "1" ] && continue
            [ "$(cat "$dev/size" 2>/dev/null)" -lt 2097152 ] && continue
            intdis="/dev/$(basename "$dev")"
            break
        done
    fi
    if [ -z "$intdis" ]; then
        echo "  ERROR: cannot find internal disk" >&2
        return 1
    fi
    if echo "$intdis" | grep -q '[0-9]$'; then
        intdis_prefix="${intdis}p"
    else
        intdis_prefix="$intdis"
    fi
}

# ── tpm reset ────────────────────────────────────────────────────────────────
reset_tpm() {
    echo ""
    echo "  TPM Reset"
    echo "  =========="
    echo ""
    if ! crossystem "mainfw_type?recovery" 2>/dev/null; then
        echo "  [!] Not in recovery mode. Boot from the MULTIPLE USB in recovery"
        echo "      mode (Esc+Refresh then plug in) and try again."
        return
    fi
    mosys nvram clear 2>/dev/null && echo "  [+] NVData cleared" || echo "  [-] NVData clear failed (non-fatal)"
    crossystem clear_tpm_owner_request=1 2>/dev/null && echo "  [+] TPM owner clear requested" || true
    local tpm_ok=0
    if command -v tpm_manager_client >/dev/null 2>&1; then
        tpm_manager_client take_ownership 2>/dev/null && echo "  [+] TPM ownership taken via tpm_manager" && tpm_ok=1 || echo "  [-] tpm_manager_client failed"
    fi
    if [ "$tpm_ok" -eq 0 ] && command -v chromeos-tpm-recovery >/dev/null 2>&1; then
        chromeos-tpm-recovery 2>/dev/null && echo "  [+] TPM recovery OK" && tpm_ok=1 || echo "  [-] chromeos-tpm-recovery failed"
    fi
    if [ "$tpm_ok" -eq 0 ] && command -v trunks_send >/dev/null 2>&1; then
        trunks_send --raw 80020000000c0000014100000107 2>/dev/null && echo "  [+] TPM cleared via trunks_send" && tpm_ok=1 || echo "  [-] trunks_send failed"
    fi
    [ "$tpm_ok" -eq 0 ] && echo "  [-] All TPM reset methods failed. Try Cr50 Reset first."
    echo ""
    local kv; kv="$(get_kernver)"
    echo "  Reported TPM Kernel Rollback Version: $kv"
    if [ "$kv" = "1" ] || [ "$kv" = "0" ]; then
        echo "  [+] kernver at minimum — downgrade should work."
    else
        echo "  [!] kernver still $kv — reboot and run again, or do Cr50 Reset first."
    fi
    echo ""
    echo "  Done."
}

# ── gbb flags ────────────────────────────────────────────────────────────────
edit_gbb() {
    echo ""
    echo "  GBB Flags Editor"
    echo "  ================"
    echo "  Current: $(/usr/share/vboot/bin/get_gbb_flags.sh 2>/dev/null | grep -i flags || echo unknown)"
    echo ""
    echo "  1) Force dev mode on       (0x8)"
    echo "  2) Short dev delay         (0x1)"
    echo "  3) Dev + short delay       (0x9)"
    echo "  4) Disable FWMP            (0x40)"
    echo "  5) Dev + disable FWMP      (0x48)"
    echo "  6) All useful flags        (0x49)"
    echo "  7) Reset to factory        (0x0)"
    echo "  8) Custom hex"
    echo "  9) Back"
    echo ""
    read -p "  Choose: " g
    local flags
    case $g in
        1) flags=0x8  ;; 2) flags=0x1  ;; 3) flags=0x9  ;;
        4) flags=0x40 ;; 5) flags=0x48 ;; 6) flags=0x49 ;;
        7) flags=0x0  ;;
        8) read -p "  Hex value: " flags ;;
        9) return ;;
        *) echo "  Invalid."; return ;;
    esac
    flashrom -p host --wp-disable >/dev/null 2>&1 || true
    /usr/share/vboot/bin/set_gbb_flags.sh "$flags" \
        && echo "  [+] GBB flags set to $flags" \
        || echo "  [-] Failed — HW write protect may be on"
}

# ── cr50 reset ───────────────────────────────────────────────────────────────
reset_cr50() {
    echo ""
    echo "  Cr50 / Ti50 Reset"
    echo "  =================="
    echo ""
    if /usr/share/cros/cr50-reset.sh 2>/dev/null; then
        echo "  [+] Cr50 reset complete"
    elif gsctool -a --ccd_open 2>/dev/null; then
        echo "  [+] CCD opened via gsctool (Ti50)"
    else
        echo "  [-] cr50/Ti50 reset failed — try from crosh: gsctool -a --ccd_open"
    fi
}

# ── dev mode on/off ──────────────────────────────────────────────────────────
toggle_devmode() {
    echo ""
    echo "  Developer Mode"
    echo "  ==============="
    echo ""
    local cur
    cur="$(crossystem devsw_boot 2>/dev/null || echo unknown)"
    echo "  Current dev mode boot flag: $cur"
    echo ""
    echo "  1) Enable developer mode"
    echo "  2) Disable developer mode (transition back to verified boot)"
    echo "  3) Back"
    echo ""
    read -p "  Choose: " dm
    case $dm in
        1)
            echo ""
            echo "  [*] Enabling dev mode via crossystem..."
            # GBB flag 0x8 forces dev mode without the scary screen delay
            flashrom -p host --wp-disable >/dev/null 2>&1 || true
            /usr/share/vboot/bin/set_gbb_flags.sh 0x8 2>/dev/null \
                && echo "  [+] GBB 0x8 set (force-dev-mode)" \
                || echo "  [-] GBB write failed (WP may be on)"
            crossystem disable_dev_request=0 2>/dev/null || true
            echo "  [+] Reboot to enter developer mode."
            echo "  [!] You will see a 'OS verification is OFF' screen — press Ctrl+D or wait."
            ;;
        2)
            echo ""
            echo "  [*] Disabling dev mode..."
            crossystem disable_dev_request=1 2>/dev/null \
                && echo "  [+] Disable requested — reboot to apply." \
                || echo "  [-] Failed"
            ;;
        3) return ;;
        *) echo "  Invalid." ;;
    esac
    echo ""
}

# ── enrollment ───────────────────────────────────────────────────────────────
manage_enrollment() {
    echo ""
    echo "  Enrollment Management"
    echo "  ======================"
    echo ""
    # Check current enrollment state
    local vpd_val
    vpd_val="$(vpd -g check_enrollment 2>/dev/null || echo unknown)"
    local pref_file="/mnt/stateful_partition/home/.shadow/enrollment_state"
    echo "  VPD check_enrollment: $vpd_val"
    echo ""
    echo "  1) Unenroll (clear enterprise enrollment)"
    echo "  2) Force re-enrollment on next boot"
    echo "  3) Back"
    echo ""
    read -p "  Choose: " en
    case $en in
        1)
            echo ""
            echo "  [*] Attempting unenrollment..."
            echo "  [!] WARNING: This will wipe the device on reboot."
            read -p "  Are you sure? [y/N] " _conf
            case "$_conf" in y|Y)
                # Clear TPM owner (enrollment is tied to TPM)
                crossystem clear_tpm_owner_request=1 2>/dev/null && echo "  [+] TPM owner clear requested" || true
                # Clear VPD enrollment flag
                vpd -i RW_VPD -s check_enrollment=0 2>/dev/null && echo "  [+] VPD check_enrollment cleared" || echo "  [-] VPD write failed"
                # Trigger powerwash / enterprise wipe
                if command -v tpm_manager_client >/dev/null 2>&1; then
                    tpm_manager_client take_ownership 2>/dev/null || true
                fi
                echo "  [+] Reboot to complete unenrollment (device will wipe)."
                ;; *) echo "  Cancelled." ;;
            esac
            ;;
        2)
            echo ""
            vpd -i RW_VPD -s check_enrollment=1 2>/dev/null \
                && echo "  [+] Enrollment flag set — device will prompt to enroll on next boot." \
                || echo "  [-] VPD write failed"
            ;;
        3) return ;;
        *) echo "  Invalid." ;;
    esac
    echo ""
}

# ── device info ──────────────────────────────────────────────────────────────
view_device_info() {
    echo ""
    echo "  Device Information"
    echo "  ==================="
    echo ""
    echo "  Board (HWID):      $(crossystem hwid 2>/dev/null || echo unknown)"
    echo "  BIOS Version:      $(get_bios_version)"
    echo "  Kernel Version:    $(get_kernver)"
    echo "  Rollback Counter:  $(get_rollback_version)"
    echo "  Chrome OS Build:   M$(get_milestone)"
    echo "  Dev Switch Boot:   $(crossystem devsw_boot 2>/dev/null || echo unknown)"
    echo "  WP Switch Boot:    $(crossystem wpsw_boot 2>/dev/null || echo unknown)"
    echo "  Firmware Type:     $(crossystem mainfw_type 2>/dev/null || echo unknown)"
    echo "  Cr50 RW Version:   $(gsctool -a -f -M 2>/dev/null | grep RW_FW_VER | cut -d= -f2 || echo unknown)"
    echo "  VPD Enrollment:    $(vpd -g check_enrollment 2>/dev/null || echo unknown)"
    echo ""
    read -p "  Press Enter to continue..."
}

# ── main menu ─────────────────────────────────────────────────────────────────
while true; do
    clear
    cat "$BANNER" 2>/dev/null || echo "  MULTIPLE"
    echo ""
    KV="$(get_kernver)"
    MILE="$(get_milestone)"
    BOARD="$(get_board)"
    echo "  board: $BOARD   cros: M$MILE   kernver: $KV"
    echo ""
    echo "  1) TPM Reset           (keyroll fix)"
    echo "  2) GBB Flags           (firmware flags)"
    echo "  3) Cr50 / Ti50 Reset"
    echo "  4) Developer Mode      (on / off)"
    echo "  5) Enrollment          (enroll / unenroll)"
    echo "  6) Device Info"
    echo "  7) Reboot"
    echo "  8) Shell"
    echo ""
    read -p "  Choose option: " choice
    case "$(echo "$choice" | tr 'a-z' 'A-Z')" in
        1) reset_tpm;           read -p "  Press Enter to continue..." ;;
        2) edit_gbb;            read -p "  Press Enter to continue..." ;;
        3) reset_cr50;          read -p "  Press Enter to continue..." ;;
        4) toggle_devmode;      read -p "  Press Enter to continue..." ;;
        5) manage_enrollment;   read -p "  Press Enter to continue..." ;;
        6) view_device_info ;;
        7) reboot -f ;;
        8) echo "  Type 'exit' to return"; bash ;;
        *) echo "  Invalid."; sleep 1 ;;
    esac
done
