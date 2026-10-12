#!/bin/bash

C=/dev/console
exec >"$C" 2>&1 </dev/console

# liability warning at boot
clear
cat /etc/issue 2>/dev/null || printf "  MULTIPLE\n"
printf "\n"
printf "  !! WARNING !!\n"
printf "  This tool modifies firmware security state (TPM, GBB, enrollment).\n"
printf "  You are solely responsible for any damage or data loss.\n"
printf "  Do not use on devices you do not own.\n"
printf "  Created by bashberryy. Not for bypassing legitimate restrictions.\n"
printf "\n"
read -p "  Accept liability and continue? [y/N] " _ack
case "$_ack" in
    y|Y) ;;
    *) printf "  Aborted.\n"; sleep 2; reboot -f ;;
esac

BANNER=/etc/issue

# --- helpers -----------------------------------------------------------------

get_board() {
    crossystem hwid 2>/dev/null | awk '{print tolower($1)}' || \
    grep -o 'CHROMEOS_RELEASE_BOARD=[^$]*' /etc/lsb-release 2>/dev/null | cut -d= -f2 || \
    echo "unknown"
}

get_kernver() {
    local raw
    raw=$(crossystem tpm_kernver 2>/dev/null) || { echo "unknown"; return; }
    printf "%d" "$((raw & 0xff))" 2>/dev/null || echo "unknown"
}

get_milestone() {
    grep CHROMEOS_RELEASE_MILESTONE /etc/lsb-release 2>/dev/null | cut -d= -f2
}

get_bios() {
    crossystem ro_fwid 2>/dev/null || echo "unknown"
}

in_recovery() {
    crossystem "mainfw_type?recovery" 2>/dev/null
}

# --- tpm reset ---------------------------------------------------------------

do_tpm_reset() {
    printf "\n  TPM Reset\n  ---------\n\n"

    if ! in_recovery; then
        printf "  [!] Not in recovery mode.\n"
        printf "      Boot this USB via Esc+Refresh (recovery) and try again.\n\n"
        return
    fi

    crossystem clear_tpm_owner_request=1 2>/dev/null \
        && printf "  [+] TPM owner clear requested\n" \
        || printf "  [-] clear_tpm_owner_request failed\n"

    mosys nvram clear 2>/dev/null \
        && printf "  [+] NVData cleared\n" \
        || printf "  [-] NVData clear failed (may be non-fatal)\n"

    local ok=0

    if command -v tpm_manager_client >/dev/null 2>&1; then
        tpm_manager_client take_ownership 2>/dev/null \
            && printf "  [+] tpm_manager_client: ownership taken\n" && ok=1 \
            || printf "  [-] tpm_manager_client failed\n"
    fi

    if [ "$ok" -eq 0 ] && command -v chromeos-tpm-recovery >/dev/null 2>&1; then
        chromeos-tpm-recovery 2>/dev/null \
            && printf "  [+] chromeos-tpm-recovery OK\n" && ok=1 \
            || printf "  [-] chromeos-tpm-recovery failed\n"
    fi

    if [ "$ok" -eq 0 ] && command -v trunks_send >/dev/null 2>&1; then
        trunks_send --raw 80020000000c0000014100000107 2>/dev/null \
            && printf "  [+] trunks_send TPM clear OK\n" && ok=1 \
            || printf "  [-] trunks_send failed\n"
    fi

    [ "$ok" -eq 0 ] && printf "  [-] All TPM methods failed. Try Cr50 Reset first.\n"

    local kv
    kv=$(get_kernver)
    printf "\n  kernver after reset: %s\n" "$kv"
    case "$kv" in
        0|1) printf "  [+] Good — kernver at floor, downgrade should work.\n" ;;
        *)   printf "  [!] kernver still %s — reboot and try again.\n" "$kv" ;;
    esac
    printf "\n"
}

# --- gbb flags ---------------------------------------------------------------

do_gbb() {
    printf "\n  GBB Flags\n  ---------\n\n"

    local cur
    cur=$(/usr/share/vboot/bin/get_gbb_flags.sh 2>/dev/null | grep -i 'GBB flags' | awk '{print $NF}')
    printf "  Current flags: %s\n\n" "${cur:-unknown}"
    printf "  1) Force dev mode       (0x8)\n"
    printf "  2) Short dev delay      (0x1)\n"
    printf "  3) Dev + short delay    (0x9)\n"
    printf "  4) Disable FWMP         (0x40)\n"
    printf "  5) Dev + disable FWMP   (0x48)\n"
    printf "  6) All useful           (0x49)\n"
    printf "  7) Reset to factory     (0x0)\n"
    printf "  8) Custom hex value\n"
    printf "  9) Back\n\n"
    read -p "  Choice: " g

    local flags
    case "$g" in
        1) flags=0x8  ;; 2) flags=0x1  ;; 3) flags=0x9  ;;
        4) flags=0x40 ;; 5) flags=0x48 ;; 6) flags=0x49 ;;
        7) flags=0x0  ;;
        8) read -p "  Hex: " flags ;;
        9) return ;;
        *) printf "  Invalid.\n"; return ;;
    esac

    # hardware WP must be off for this to work
    # on most devices you need to unplug the battery cable (MB connector)
    flashrom -p host --wp-disable 2>/dev/null || true
    /usr/share/vboot/bin/set_gbb_flags.sh "$flags" 2>/dev/null \
        && printf "  [+] GBB flags set to %s\n" "$flags" \
        || printf "  [-] Failed. Hardware write-protect is probably on.\n"
    printf "\n"
}

# --- cr50 / ti50 reset -------------------------------------------------------

do_cr50_reset() {
    printf "\n  Cr50 / Ti50 Reset\n  -----------------\n\n"

    if /usr/share/cros/cr50-reset.sh 2>/dev/null; then
        printf "  [+] cr50-reset.sh OK\n"
    elif gsctool -a --ccd_open 2>/dev/null; then
        printf "  [+] CCD opened via gsctool (Ti50)\n"
    else
        printf "  [-] Both methods failed.\n"
        printf "      From crosh on a working boot: gsctool -a --ccd_open\n"
    fi
    printf "\n"
}

# --- dev mode ----------------------------------------------------------------

do_devmode() {
    printf "\n  Developer Mode\n  --------------\n\n"
    printf "  Current devsw_boot: %s\n\n" "$(crossystem devsw_boot 2>/dev/null || echo unknown)"
    printf "  1) Enable dev mode\n"
    printf "  2) Disable dev mode\n"
    printf "  3) Back\n\n"
    read -p "  Choice: " d

    case "$d" in
        1)
            # GBB 0x8 forces the transition without the 30-second delay screen
            # Still needs hardware WP off to stick across reboots
            printf "\n"
            flashrom -p host --wp-disable 2>/dev/null || true
            /usr/share/vboot/bin/set_gbb_flags.sh 0x8 2>/dev/null \
                && printf "  [+] GBB force-dev-mode set (0x8)\n" \
                || printf "  [-] GBB write failed — WP likely on\n"
            # clear the devmode block flags so it can actually boot
            crossystem block_devmode=0 2>/dev/null \
                && printf "  [+] block_devmode cleared\n" \
                || printf "  [-] block_devmode write failed\n"
            vpd -i RW_VPD -s block_devmode=0 2>/dev/null \
                && printf "  [+] VPD block_devmode cleared\n" \
                || printf "  [-] VPD write failed\n"
            printf "\n  Reboot. You will see 'OS verification is OFF' — press Ctrl+D.\n"
            ;;
        2)
            crossystem disable_dev_request=1 2>/dev/null \
                && printf "  [+] disable_dev_request set — reboot to apply\n" \
                || printf "  [-] Failed\n"
            ;;
        3) return ;;
        *) printf "  Invalid.\n" ;;
    esac
    printf "\n"
}

# --- enrollment --------------------------------------------------------------

do_enrollment() {
    printf "\n  Enrollment\n  ----------\n\n"
    printf "  VPD check_enrollment: %s\n" "$(vpd -g check_enrollment 2>/dev/null || echo unknown)"
    printf "\n"
    printf "  1) Unenroll (wipes device on reboot)\n"
    printf "  2) Force re-enrollment on next boot\n"
    printf "  3) Back\n\n"
    read -p "  Choice: " e

    case "$e" in
        1)
            printf "\n  [!] This will trigger a powerwash (full wipe) on reboot.\n"
            read -p "  Sure? [y/N] " _c
            case "$_c" in y|Y) ;; *) printf "  Cancelled.\n"; return ;; esac

            # request TPM owner clear — enrollment is tied to TPM state
            crossystem clear_tpm_owner_request=1 2>/dev/null \
                && printf "  [+] TPM owner clear requested\n" \
                || printf "  [-] TPM clear failed\n"

            # remove firmware management parameters (FWMP) — blocks devmode on enrolled devices
            if command -v cryptohome >/dev/null 2>&1; then
                cryptohome --action=remove_firmware_management_parameters 2>/dev/null \
                    && printf "  [+] FWMP removed\n" \
                    || printf "  [-] FWMP removal failed (may not exist)\n"
            fi

            # clear the enrollment VPD flags
            vpd -i RW_VPD -s check_enrollment=0 2>/dev/null \
                && printf "  [+] VPD check_enrollment cleared\n" \
                || printf "  [-] VPD write failed\n"
            vpd -i RW_VPD -s block_devmode=0 2>/dev/null \
                && printf "  [+] VPD block_devmode cleared\n" \
                || printf "  [-] VPD block_devmode failed\n"

            printf "\n  Reboot — device will powerwash and come up unenrolled.\n"
            ;;
        2)
            vpd -i RW_VPD -s check_enrollment=1 2>/dev/null \
                && printf "  [+] check_enrollment=1 set — will prompt to enroll on next boot\n" \
                || printf "  [-] VPD write failed\n"
            ;;
        3) return ;;
        *) printf "  Invalid.\n" ;;
    esac
    printf "\n"
}

# --- device info -------------------------------------------------------------

do_info() {
    printf "\n  Device Info\n  -----------\n\n"
    printf "  HWID:              %s\n" "$(crossystem hwid 2>/dev/null || echo unknown)"
    printf "  BIOS:              %s\n" "$(get_bios)"
    printf "  kernver:           %s\n" "$(get_kernver)"
    printf "  Chrome OS:         M%s\n" "$(get_milestone)"
    printf "  mainfw_type:       %s\n" "$(crossystem mainfw_type 2>/dev/null || echo unknown)"
    printf "  devsw_boot:        %s\n" "$(crossystem devsw_boot 2>/dev/null || echo unknown)"
    printf "  wpsw_boot:         %s\n" "$(crossystem wpsw_boot 2>/dev/null || echo unknown)"
    printf "  block_devmode:     %s\n" "$(crossystem block_devmode 2>/dev/null || echo unknown)"
    printf "  check_enrollment:  %s\n" "$(vpd -g check_enrollment 2>/dev/null || echo unknown)"
    printf "  Cr50 RW:           %s\n" "$(gsctool -a -f -M 2>/dev/null | grep RW_FW_VER | cut -d= -f2 || echo unknown)"
    printf "\n"
    read -p "  Enter to continue..."
}

# --- main loop ---------------------------------------------------------------

while true; do
    clear
    cat "$BANNER" 2>/dev/null || printf "  MULTIPLE\n"
    printf "\n"
    printf "  board: %s   cros: M%s   kernver: %s\n\n" \
        "$(get_board)" "$(get_milestone)" "$(get_kernver)"
    printf "  1) TPM Reset\n"
    printf "  2) GBB Flags\n"
    printf "  3) Cr50 / Ti50 Reset\n"
    printf "  4) Developer Mode\n"
    printf "  5) Enrollment\n"
    printf "  6) Device Info\n"
    printf "  7) Reboot\n"
    printf "  8) Shell\n"
    printf "\n"
    read -p "  > " choice

    case "$choice" in
        1) do_tpm_reset;    read -p "  Enter to continue..." ;;
        2) do_gbb;          read -p "  Enter to continue..." ;;
        3) do_cr50_reset;   read -p "  Enter to continue..." ;;
        4) do_devmode;      read -p "  Enter to continue..." ;;
        5) do_enrollment;   read -p "  Enter to continue..." ;;
        6) do_info ;;
        7) reboot -f ;;
        8) printf "  type 'exit' to return\n"; bash ;;
        *) ;;
    esac
done
