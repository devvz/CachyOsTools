#!/usr/bin/env bash
# Installed in the restored system and called after subsequent kernel updates.
set -Eeuo pipefail

restore_boot_main() {
    source /usr/local/lib/xetal-iso/common.sh
    [[ $EUID == 0 ]] || { iso_die 'Boot regeneration requires root.'; return 1; }
    source /etc/xetal-boot.conf
    [[ $ROOT_UUID =~ ^[a-fA-F0-9-]+$ ]] || { iso_die 'Invalid root UUID.'; return 1; }
    [[ $BOOTLOADER == grub || $BOOTLOADER == systemd-boot || $BOOTLOADER == limine ]] || return 1
    local tool kernels version pkgbase kernel image microcode entry entry_tmp config_tmp windows_guid early_modules
    local limine_conf=/efi/EFI/BOOT/limine.conf limine_efi=/efi/EFI/BOOT/BOOTX64.EFI limine_hash=0
    local -a post_options=()
    tool=$(iso_initramfs_tool /) || return 1
    kernels=$(iso_kernels /) || return 1
    mkdir -p /boot/xetal
    if [[ $BOOTLOADER == systemd-boot || $BOOTLOADER == limine ]]; then
        mountpoint -q /efi || { iso_die 'Mount the EFI system partition at /efi before updating boot files.'; return 1; }
        mkdir -p /efi/Xetal
    fi
    if [[ $BOOTLOADER == systemd-boot ]]; then mkdir -p /efi/loader/entries; fi
    if [[ $BOOTLOADER == limine ]]; then
        mkdir -p /efi/EFI/BOOT
        # With sbctl keys present, pin every file by hash so the config can be enrolled
        # into the (signed) Limine binary and Limine boots under Secure Boot.
        if iso_sbctl_ready; then limine_hash=1; fi
        printf 'timeout: 5\n' > "$limine_conf.new"
    fi

    # The restored root is a new, unencrypted ext4 filesystem. Do not embed the
    # source disk's encryption, LVM, resume UUIDs or Btrfs subvolume parameters.
    # Keep any GPU driver the source loaded early (e.g. NVIDIA): such systems may not
    # get a working display when the driver only loads after the root filesystem.
    early_modules=$(restore_boot_gpu_modules) || early_modules=''
    {
        printf 'MODULES=(ext4%s)\n' "${early_modules:+ $early_modules}"
        printf 'BINARIES=()\nFILES=()\nHOOKS=(base udev modconf keyboard block filesystems fsck)\nCOMPRESSION="gzip"\n'
    } > /etc/mkinitcpio-xetal.conf
    mkdir -p /etc/dracut-xetal.conf.d
    printf 'hostonly="no"\nhostonly_cmdline="no"\n' > /etc/dracut-xetal.conf
    if [[ $tool == mkinitcpio ]] && mkinitcpio --help | grep -q -- --nopost; then post_options+=(--nopost); fi
    config_tmp=$(mktemp /boot/xetal/grub.cfg.XXXXXX)
    printf 'set default=0\nset timeout=5\n' > "$config_tmp"

    while IFS=$'\t' read -r version pkgbase kernel; do
        image="/boot/xetal/initramfs-$version.img"
        echo "[*] Generating $tool image for $pkgbase ($version)..."
        if [[ $tool == mkinitcpio ]]; then
            mkinitcpio -c /etc/mkinitcpio-xetal.conf -k "$version" -g "$image.new" "${post_options[@]}" || return 1
        else
            dracut --force --no-hostonly --no-hostonly-cmdline --conf /etc/dracut-xetal.conf \
                --confdir /etc/dracut-xetal.conf.d --add-drivers ext4 \
                --kernel-cmdline "root=UUID=$ROOT_UUID rw" "$image.new" "$version" || return 1
        fi
        [[ -s $image.new ]] || { iso_die "No initramfs was produced for $version."; return 1; }
        mv "$image.new" "$image"
        cp "$kernel" "/boot/xetal/vmlinuz-$version"
        # GRUB loads this copy. A fresh copy is unsigned, so sign it again (Secure Boot).
        if [[ $BOOTLOADER == grub ]]; then iso_sign_boot_file "/boot/xetal/vmlinuz-$version"; fi
        {
            printf "menuentry 'Cloned system - %s' {\n" "$pkgbase"
            printf "  search --no-floppy --fs-uuid --set=root %s\n" "$ROOT_UUID"
            printf '  linux /boot/xetal/vmlinuz-%s root=UUID=%s rw\n' "$version" "$ROOT_UUID"
            printf '  initrd'
            for microcode in /boot/intel-ucode.img /boot/amd-ucode.img; do
                [[ ! -s $microcode ]] || printf ' %s' "$microcode"
            done
            printf ' /boot/xetal/initramfs-%s.img\n}\n' "$version"
        } >> "$config_tmp"
        if [[ $BOOTLOADER == systemd-boot ]]; then
            cp "/boot/xetal/vmlinuz-$version" "/efi/Xetal/vmlinuz-$version"
            iso_sign_boot_file "/efi/Xetal/vmlinuz-$version"
            cp "$image" "/efi/Xetal/initramfs-$version.img"
            entry="/efi/loader/entries/xetal-$version.conf"
            entry_tmp="$entry.new"
            {
                printf 'title Cloned system - %s\nlinux /Xetal/vmlinuz-%s\n' "$pkgbase" "$version"
                for microcode in /boot/intel-ucode.img /boot/amd-ucode.img; do
                    if [[ -s $microcode ]]; then
                        cp "$microcode" "/efi/Xetal/${microcode##*/}"
                        printf 'initrd /Xetal/%s\n' "${microcode##*/}"
                    fi
                done
                printf 'initrd /Xetal/initramfs-%s.img\noptions root=UUID=%s rw\n' "$version" "$ROOT_UUID"
            } > "$entry_tmp"
            mv "$entry_tmp" "$entry"
        fi
        if [[ $BOOTLOADER == limine ]]; then
            # Kernel and initramfs live on the FAT EFI partition, which Limine always reads.
            cp "/boot/xetal/vmlinuz-$version" "/efi/Xetal/vmlinuz-$version"
            cp "$image" "/efi/Xetal/initramfs-$version.img"
            {
                printf '\n/Cloned system - %s\n    protocol: linux\n' "$pkgbase"
                printf '    path: boot():/Xetal/vmlinuz-%s%s\n' "$version" "$(restore_boot_hash "/efi/Xetal/vmlinuz-$version")"
                for microcode in /boot/intel-ucode.img /boot/amd-ucode.img; do
                    if [[ -s $microcode ]]; then
                        cp "$microcode" "/efi/Xetal/${microcode##*/}"
                        printf '    module_path: boot():/Xetal/%s%s\n' "${microcode##*/}" "$(restore_boot_hash "/efi/Xetal/${microcode##*/}")"
                    fi
                done
                printf '    module_path: boot():/Xetal/initramfs-%s.img%s\n' "$version" "$(restore_boot_hash "/efi/Xetal/initramfs-$version.img")"
                printf '    cmdline: root=UUID=%s rw\n' "$ROOT_UUID"
            } >> "$limine_conf.new"
        fi
    done <<< "$kernels"
    if [[ $BOOTLOADER == grub ]]; then
        mkdir -p /boot/grub
        grub-script-check "$config_tmp" || return 1
        mv "$config_tmp" /boot/grub/grub.cfg
    elif [[ $BOOTLOADER == systemd-boot ]]; then
        rm "$config_tmp"
        printf 'default xetal-*\ntimeout 5\n' > /efi/loader/loader.conf
        # Remove only our entries for kernels that no longer exist.
        for entry in /efi/loader/entries/xetal-*.conf; do
            [[ -f $entry ]] || continue
            version=${entry##*/xetal-}; version=${version%.conf}
            if [[ ! -d /usr/lib/modules/$version ]]; then
                rm -- "$entry" "/efi/Xetal/vmlinuz-$version" "/efi/Xetal/initramfs-$version.img"
            fi
        done
    else
        rm "$config_tmp"
        # Offer Windows if its boot manager is on another ESP. Not hash-pinned: Windows
        # updates rewrite bootmgfw.efi, which would make a stored hash go stale.
        windows_guid=$(iso_windows_esp_guid) || windows_guid=
        if [[ -n $windows_guid ]]; then
            printf '\n/Windows\n    protocol: efi\n    path: guid(%s):/EFI/Microsoft/Boot/bootmgfw.efi\n' \
                "$windows_guid" >> "$limine_conf.new"
        fi
        restore_boot_limine "$limine_conf" "$limine_efi" "$limine_hash" || return 1
        # Remove only our kernel and initramfs copies for kernels that no longer exist.
        for image in /efi/Xetal/vmlinuz-* /efi/Xetal/initramfs-*.img; do
            [[ -f $image ]] || continue
            version=${image##*/}; version=${version#vmlinuz-}; version=${version#initramfs-}; version=${version%.img}
            [[ -d /usr/lib/modules/$version ]] || rm -- "$image"
        done
    fi
    echo '[*] Boot files regenerated successfully.'
}

# Prints (space separated) the known GPU driver modules that this system's mkinitcpio
# config loads early. Everything else (btrfs, dm-crypt, ...) belongs to the source's disk
# layout and must not leak into the restored ext4 root, so only a GPU allowlist is kept.
restore_boot_gpu_modules() {
    local m out=''
    while read -r m; do
        case $m in
            nvidia|nvidia_modeset|nvidia_uvm|nvidia_drm|amdgpu|radeon|i915|xe|nouveau|virtio_gpu|vmwgfx|qxl|bochs)
                [[ " $out " == *" $m "* ]] || out+="${out:+ }$m" ;;
        esac
    done < <(bash -c 'MODULES=(); for f in "$@"; do [[ -r $f ]] && . "$f"; done; printf "%s\n" "${MODULES[@]}"' \
        _ /etc/mkinitcpio.conf /etc/mkinitcpio.conf.d/*.conf 2>/dev/null)
    printf '%s' "$out"
}

# Prints "#<blake2b>" for FILE when Limine entries are hash-pinned, nothing otherwise.
restore_boot_hash() {
    (( ${limine_hash:-0} )) || return 0
    printf '#%s' "$(b2sum -- "$1" | cut -d' ' -f1)"
}

# Deploy a fresh Limine binary and the new config. With hash pinning, the config's
# BLAKE2b is enrolled into the binary first and the binary is signed afterwards
# (signing must come last: enrolling changes the file).
restore_boot_limine() {
    local conf=$1 efi=$2 pinned=$3
    [[ -s /usr/share/limine/BOOTX64.EFI ]] || { iso_die 'The limine package files are missing.'; return 1; }
    install -m644 /usr/share/limine/BOOTX64.EFI "$efi.new" || return 1
    if (( pinned )); then
        limine enroll-config "$efi.new" "$(b2sum -- "$conf.new" | cut -d' ' -f1)" || {
            rm -f -- "$efi.new"; iso_die 'Could not enroll the Limine config hash.'; return 1
        }
    fi
    mv "$conf.new" "$conf"
    mv "$efi.new" "$efi"
    if (( pinned )); then iso_sign_boot_file "$efi"; fi
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then restore_boot_main "$@"; fi
