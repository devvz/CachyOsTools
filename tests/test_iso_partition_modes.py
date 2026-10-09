"""Exercise the free-space / existing-partition install helpers without touching any disk.

`parted` is replaced by a shell function that keeps a partition table in a text
file and prints it in parted's machine-readable (-m) format, so the parsing,
alignment, numbering and "existing partitions unchanged" checks run for real.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
COMMON = REPO / 'iso/common.sh'
INSTALLER = REPO / 'iso/installer.sh'

MIB = 1048576

MOCK = r'''
STATE=$1
mock_print() {
    local disk=$1 free=$2 size table nextfree n s e fs name flags prev=17408 last rows used=' '
    size=$(<"$STATE/disk_bytes"); table=$(<"$STATE/table"); last=$((size - 17409))
    printf 'BYT;\n%s:%sB:scsi:512:512:%s:Mock Disk:;\n' "$disk" "$size" "$table"
    [[ $table == gpt ]] || return 0
    rows=$(sort -t: -k2,2n "$STATE/rows")
    while IFS=: read -r n s e fs name flags; do [[ -n $n ]] && used+="$n "; done <<< "$rows"
    nextfree=1; while [[ $used == *" $nextfree "* ]]; do nextfree=$((nextfree + 1)); done
    while IFS=: read -r n s e fs name flags; do
        [[ -n $n ]] || continue
        if (( free && s > prev )); then printf '%s:%sB:%sB:%sB:free;\n' "$nextfree" "$prev" "$((s - 1))" "$((s - prev))"; fi
        printf '%s:%sB:%sB:%sB:%s:%s:%s;\n' "$n" "$s" "$e" "$((e - s + 1))" "$fs" "$name" "$flags"
        prev=$((e + 1))
    done <<< "$rows"
    if (( free && last >= prev )); then printf '%s:%sB:%sB:%sB:free;\n' "$nextfree" "$prev" "$last" "$((last - prev + 1))"; fi
}
mock_mkpart() {   # name fs startMiB endMiB  (end is exclusive, like parted with MiB units)
    local name=$1 fs=$2 s=$(( ${3%MiB} * 1048576 )) e=$(( ${4%MiB} * 1048576 - 1 )) n=1 rn rs re
    local size; size=$(<"$STATE/disk_bytes")
    (( s >= 17408 && e <= size - 17409 && e > s )) || { echo 'mock: outside usable area' >&2; return 1; }
    while IFS=: read -r rn rs re _; do
        [[ -n $rn ]] || continue
        if (( s <= re && e >= rs )); then echo 'mock: overlaps an existing partition' >&2; return 1; fi
    done < "$STATE/rows"
    while cut -d: -f1 "$STATE/rows" | grep -qx "$n"; do n=$((n + 1)); done
    echo "$n:$s:$e:$fs:$name:" >> "$STATE/rows"
    if [[ -n ${MOCK_CORRUPT:-} ]]; then   # simulate a parted that shrinks an existing partition
        awk -F: -v OFS=: -v n="$MOCK_CORRUPT" '$1 == n {$3 = $3 - 1048576} 1' "$STATE/rows" > "$STATE/rows.new"
        mv "$STATE/rows.new" "$STATE/rows"
    fi
}
parted() {
    local a; local -a rest=()
    for a in "$@"; do [[ $a == -m || $a == -s ]] || rest+=("$a"); done
    case ${rest[1]} in
        unit) mock_print "${rest[0]}" "$([[ ${rest[4]:-} == free ]] && echo 1 || echo 0)" ;;
        mkpart) mock_mkpart "${rest[2]}" "${rest[3]}" "${rest[4]}" "${rest[5]}" ;;
        set) awk -F: -v OFS=: -v n="${rest[2]}" '$1 == n {$6 = "boot, esp"} 1' "$STATE/rows" > "$STATE/rows.new" &&
             mv "$STATE/rows.new" "$STATE/rows" ;;
        *) echo "mock parted: unsupported ${rest[*]}" >&2; return 1 ;;
    esac
}
source "$2"
'''


def row(n, start_mib, end_excl_mib, fs='', name='', flags=''):
    return f'{n}:{start_mib * MIB}:{end_excl_mib * MIB - 1}:{fs}:{name}:{flags}'


# A 500 GiB disk laid out like a fresh Windows install after shrinking C:.
WINDOWS_DISK = [
    row(1, 1, 101, 'fat32', 'EFI system partition', 'boot, esp'),
    row(2, 101, 117, '', 'Microsoft reserved', 'msftres'),
    row(3, 117, 307200, 'ntfs', 'Basic data partition', 'msftdata'),
    row(4, 510976, 511999, 'ntfs', 'Recovery', 'hidden, diag'),
]


class PartitionHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='iso-parts-')
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.set_disk(WINDOWS_DISK)

    def set_disk(self, rows, table='gpt', size_mib=512000):
        (self.state / 'disk_bytes').write_text(str(size_mib * MIB))
        (self.state / 'table').write_text(table)
        (self.state / 'rows').write_text('\n'.join(rows) + '\n')

    def rows(self):
        return (self.state / 'rows').read_text().splitlines()

    def sh(self, body, source=COMMON, env=None):
        code = MOCK + body
        full_env = dict(os.environ, **(env or {}))
        return subprocess.run(['bash', '-c', code, 'test', str(self.state), str(source)],
                              capture_output=True, text=True, timeout=10, env=full_env)

    # ---- reading ---------------------------------------------------------
    def test_table_type(self):
        self.assertEqual(self.sh('iso_gpt_table /dev/mock0').stdout.strip(), 'gpt')
        self.set_disk(WINDOWS_DISK, table='msdos')
        self.assertEqual(self.sh('iso_gpt_table /dev/mock0').stdout.strip(), 'msdos')

    def test_free_region_after_shrunk_windows_partition(self):
        gib = 1024 ** 3
        result = self.sh(f'iso_free_regions /dev/mock0 {150 * gib}')
        self.assertEqual(result.returncode, 0, result.stderr)
        # Exactly the gap between C: and Recovery; the 1 MiB sliver in front of
        # the first partition and the tail of the disk are far too small.
        self.assertEqual(result.stdout, '307200\t510976\n')

    def test_free_region_too_small_is_not_offered(self):
        gib = 1024 ** 3
        self.assertEqual(self.sh(f'iso_free_regions /dev/mock0 {250 * gib}').stdout, '')

    def test_region_bounds_are_rounded_inward_to_whole_mib(self):
        # Free space that starts and ends off a MiB boundary must shrink, never grow.
        rows = [row(1, 1, 101, 'fat32')]
        rows.append(f'2:{200 * MIB + 4096}:{300 * MIB + 8191}:ext4::')
        self.set_disk(rows)
        result = self.sh(f'iso_free_regions /dev/mock0 {10 * MIB}')
        regions = [tuple(map(int, line.split('\t'))) for line in result.stdout.splitlines()]
        self.assertIn((101, 200), regions)       # ends before the unaligned partition
        self.assertIn((301, 511999), regions)    # starts after it, ends before the backup GPT

    def test_partition_rows_exclude_free_space(self):
        result = self.sh('iso_partition_rows /dev/mock0')
        self.assertEqual([line.split('\t')[0] for line in result.stdout.splitlines()],
                         ['1', '2', '3', '4'])

    # ---- creating --------------------------------------------------------
    def test_create_in_free_space_leaves_existing_partitions_alone(self):
        before = self.rows()
        result = self.sh('iso_create_efi_root /dev/mock0 307200 2048 510976')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '5 6')
        after = self.rows()
        self.assertEqual(sorted(before), sorted(r for r in after if r.split(':')[0] in '1234'))
        esp = next(r for r in after if r.startswith('5:')).split(':')
        root = next(r for r in after if r.startswith('6:')).split(':')
        self.assertEqual(int(esp[1]), 307200 * MIB)
        self.assertEqual(int(esp[2]), 309248 * MIB - 1)
        self.assertEqual(esp[5], 'boot, esp')
        self.assertEqual(int(root[1]), 309248 * MIB)
        self.assertEqual(int(root[2]), 510976 * MIB - 1)
        self.assertEqual(root[3], 'ext4')

    def test_new_partition_numbers_come_from_position_not_assumption(self):
        # Partition 2 was deleted earlier, so the table has a hole: parted reuses it.
        self.set_disk([r for r in WINDOWS_DISK if not r.startswith('2:')])
        result = self.sh('iso_create_efi_root /dev/mock0 307200 2048 510976')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '2 5')

    def test_modified_existing_partition_aborts_before_formatting(self):
        result = self.sh('iso_create_efi_root /dev/mock0 307200 2048 510976',
                         env={'MOCK_CORRUPT': '3'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Existing partition 3 was modified', result.stderr)
        self.assertEqual(result.stdout.strip(), '')

    def test_plan_that_overlaps_an_existing_partition_fails(self):
        result = self.sh('iso_create_efi_root /dev/mock0 300000 2048 510976')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sorted(self.rows()), sorted(WINDOWS_DISK))

    def test_invalid_plans_are_rejected_without_writing(self):
        for plan in ('0 2048 510976', '307200 0 510976', '307200 2048 309000', 'x 2048 510976'):
            with self.subTest(plan=plan):
                result = self.sh(f'iso_create_efi_root /dev/mock0 {plan}')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Invalid partition plan', result.stderr)
        self.assertEqual(sorted(self.rows()), sorted(WINDOWS_DISK))


class SelectionTests(unittest.TestCase):
    """installer_select_free with mocked disks (parted mock from above)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='iso-select-')
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.helpers = PartitionHelperTests('test_table_type')
        self.helpers.state = self.state
        self.helpers.set_disk(WINDOWS_DISK)

    def run_free(self, bytes_needed, extra=''):
        body = r'''
source "$3"
lsblk() { case "$*" in *NAME,TYPE*) printf '/dev/mock0 disk\n' ;; *SIZE*) printf '500G\n' ;; esac; }
iso_validate_disk() { return 0; }
''' + extra + f'''
installer_select_free "" {bytes_needed} || exit $?
echo "$SEL_DISK $SEL_START $SEL_END"
'''
        return subprocess.run(['bash', '-c', MOCK + body, 'test', str(self.state), str(COMMON), str(INSTALLER)],
                              capture_output=True, text=True, timeout=10)

    def test_single_region_is_selected_automatically(self):
        result = self.run_free(150 * 1024 ** 3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines()[-1], '/dev/mock0 307200 510976')

    def test_no_region_large_enough_explains_and_fails(self):
        result = self.run_free(400 * 1024 ** 3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('free region of at least 401 GiB', result.stderr)
        self.assertIn('No disk has been changed', result.stderr)

    def test_disk_without_gpt_is_skipped(self):
        self.helpers.set_disk(WINDOWS_DISK, table='msdos')
        result = self.run_free(150 * 1024 ** 3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('free region', result.stderr)

    def test_choice_between_two_regions(self):
        self.helpers.set_disk([
            row(1, 1, 101, 'fat32'), row(2, 101, 204800, 'ntfs'),
            row(3, 256000, 409600, 'ntfs'),
        ])
        picked = 'installer_menu() { printf "1\\n"; }'
        result = self.run_free(40 * 1024 ** 3, extra=picked)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Region 0 is 204800..256000 (50 GiB); region 1 is 409600..511999.
        self.assertEqual(result.stdout.strip().splitlines()[-1], '/dev/mock0 409600 511999')


class MenuAndConfirmTests(unittest.TestCase):
    def run_installer(self, body, stdin=''):
        code = f'source "$1"\n{body}'
        return subprocess.run(['bash', '-c', code, 'test', str(INSTALLER)],
                              input=stdin, capture_output=True, text=True, timeout=10)

    def test_menu_returns_chosen_tag_and_prints_options_to_stderr(self):
        result = self.run_installer("installer_menu T 'Pick one' a 'Alpha' b 'Beta'", stdin='b\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'b\n')
        self.assertIn('Alpha', result.stderr)

    def test_menu_rejects_unknown_tags_and_end_of_input(self):
        for stdin in ('z\n', '\n', ''):
            with self.subTest(stdin=stdin):
                result = self.run_installer("installer_menu T 'Pick one' a 'Alpha' b 'Beta'", stdin=stdin)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')

    def test_plan_confirmation_needs_the_exact_word(self):
        ok = self.run_installer("installer_confirm_plan 'summary' 'danger' && echo CONFIRMED", stdin='INSTALL\n')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn('CONFIRMED', ok.stdout)
        for answer in ('\n', 'yes\n', 'WIPE\n', 'install\n', ''):
            with self.subTest(answer=answer):
                bad = self.run_installer("installer_confirm_plan 'summary' 'danger' && echo CONFIRMED", stdin=answer)
                self.assertNotEqual(bad.returncode, 0)
                self.assertNotIn('CONFIRMED', bad.stdout)

    def test_partition_with_existing_filesystem_must_be_typed_back(self):
        body = "installer_confirm_plan 'summary' 'danger' /dev/sda3 && echo CONFIRMED"
        ok = self.run_installer(body, stdin='INSTALL\n/dev/sda3\n')
        self.assertIn('CONFIRMED', ok.stdout)
        for answer in ('INSTALL\n', 'INSTALL\n/dev/sda4\n', 'INSTALL\nyes\n'):
            with self.subTest(answer=answer):
                bad = self.run_installer(body, stdin=answer)
                self.assertNotEqual(bad.returncode, 0)
                self.assertNotIn('CONFIRMED', bad.stdout)

    def test_dialog_confirmation_is_default_no_and_asks_twice(self):
        code = r'''
source "$1"
records=$2; calls=0
clear() { :; }
dialog() { calls=$((calls + 1)); printf '%s\0' "$@" > "$records/$calls"; return 0; }
installer_confirm_plan 'the summary' 'the danger' && echo CONFIRMED
'''
        with tempfile.TemporaryDirectory(prefix='iso-plan-') as tmp:
            master, slave = os.openpty()
            try:
                result = subprocess.run(['bash', '-c', code, 'test', str(INSTALLER), tmp],
                                        stdin=slave, capture_output=True, text=True, timeout=5)
            finally:
                os.close(slave)
                os.close(master)
            dialogs = [p.read_text().split('\0')[:-1] for p in sorted(Path(tmp).iterdir())]
        self.assertIn('CONFIRMED', result.stdout)
        self.assertEqual(len(dialogs), 2)
        for args in dialogs:
            self.assertIn('--defaultno', args)
        self.assertEqual(dialogs[0][dialogs[0].index('--yesno') + 1], 'the summary')
        self.assertEqual(dialogs[1][dialogs[1].index('--yesno') + 1], 'the danger')


class DryRunTests(unittest.TestCase):
    """Drive the real installer_main with --dry-run against a patched copy.

    The copy points the EFI check, the payload directory and the root check at
    harmless fakes and the shell mocks lsblk/parted, so nothing here can touch a disk.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='iso-dryrun-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        helpers = PartitionHelperTests('test_table_type')
        helpers.state = self.state
        helpers.set_disk(WINDOWS_DISK)
        self.clone = self.root / 'clone'
        self.clone.mkdir()
        (self.root / 'efi').mkdir()
        (self.clone / 'common.sh').write_text(
            COMMON.read_text() + '\niso_need() { :; }\niso_validate_disk() { return 0; }\n')
        self.write_meta(uefi=1, bios=0)
        (self.clone / 'snapshot.sha256').write_text('')
        text = INSTALLER.read_text()
        text = text.replace('/sys/firmware/efi', str(self.root / 'efi')).replace('/opt/clone', str(self.clone))
        text = text.replace('[[ $EUID == 0 ]]', 'true')
        self.installer = self.root / 'installer.sh'
        self.installer.write_text(text)

    def write_meta(self, uefi, bios):
        (self.clone / 'snapshot.meta').write_text(
            f'FORMAT=1\nARCH=x86_64\nBYTES={20 * 1024 ** 3}\nUEFI={uefi}\nBIOS={bios}\nKERNELS=1\n')

    def run_dry(self, stdin, extra_env=None):
        body = MOCK + r'''
source "$3"
installer_show_logo() { :; }
installer_protected_disks() { printf '\n'; }
lsblk() {
    case "$*" in
        *MAJ:MIN*) printf '8:0 SERIAL WWN\n' ;;
        *NAME,TYPE*) printf '/dev/mock0 disk\n' ;;
        *-bdnro*) printf '536870912000\n' ;;
        *SIZE,MODEL*) printf '500G Mock Disk\n' ;;
        *SIZE*) printf '500G\n' ;;
    esac
}
installer_main --dry-run
'''
        before = (self.state / 'rows').read_text()
        result = subprocess.run(['bash', '-c', body, 'test', str(self.state), str(COMMON), str(self.installer)],
                                input=stdin, capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, **(extra_env or {})))
        self.assertEqual((self.state / 'rows').read_text(), before, 'dry-run changed the partition table')
        return result

    @unittest.skipUnless(os.uname().machine == 'x86_64', 'the installer only supports x86_64')
    def test_esp_size_override_applies_to_the_plan(self):
        default = self.run_dry('free\n')
        self.assertIn('New EFI partition:  2048 MiB', default.stdout)
        big = self.run_dry('free\n', {'XETAL_ESP_MIB': '4096'})
        self.assertEqual(big.returncode, 0, big.stderr + big.stdout)
        self.assertIn('New EFI partition:  4096 MiB', big.stdout)

    @unittest.skipUnless(os.uname().machine == 'x86_64', 'the installer only supports x86_64')
    def test_invalid_esp_size_override_is_rejected(self):
        for bad in ('abc', '100', '99999', '4G', '-5'):
            result = self.run_dry('free\n', {'XETAL_ESP_MIB': bad})
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertIn('XETAL_ESP_MIB must be', result.stderr, bad)

    @unittest.skipUnless(os.uname().machine == 'x86_64', 'the installer only supports x86_64')
    def test_free_space_plan_is_printed_and_nothing_is_written(self):
        result = self.run_dry('free\n')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('Install the cloned system into FREE SPACE', result.stdout)
        self.assertIn('199 GiB, starting at 307200 MiB', result.stdout)
        self.assertIn('New EFI partition:  2048 MiB', result.stdout)
        self.assertIn('Existing partitions (Windows etc.) are NOT modified', result.stdout)
        self.assertIn('[dry-run] No changes were made.', result.stdout)

    @unittest.skipUnless(os.uname().machine == 'x86_64', 'the installer only supports x86_64')
    def test_whole_disk_mode_still_works_in_dry_run(self):
        result = self.run_dry('wipe\n/dev/mock0\n')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('Whole-disk install to /dev/mock0', result.stdout)
        self.assertIn('[dry-run] No changes were made.', result.stdout)

    @unittest.skipUnless(os.uname().machine == 'x86_64', 'the installer only supports x86_64')
    def test_free_space_mode_is_refused_when_booted_in_bios_mode(self):
        (self.root / 'efi').rmdir()
        self.write_meta(uefi=0, bios=1)
        result = self.run_dry('free\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('need the USB booted in UEFI mode', result.stderr)
        self.assertNotIn('Plan:', result.stdout)

    @unittest.skipUnless(os.uname().machine == 'x86_64', 'the installer only supports x86_64')
    def test_invalid_mode_choice_is_rejected(self):
        result = self.run_dry('format-everything\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid selection', result.stderr)


class SecureBootTests(unittest.TestCase):
    """Optional sbctl signing: only with keys present, and never fatal."""

    def sh(self, body):
        code = f'source "$1"\n{body}'
        return subprocess.run(['bash', '-c', code, 'test', str(COMMON)],
                              capture_output=True, text=True, timeout=10)

    def make_root(self, sbctl=True, keys='/var/lib/sbctl/keys/db/db.key'):
        tmp = tempfile.TemporaryDirectory(prefix='iso-sb-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        if sbctl:
            (root / 'usr/bin').mkdir(parents=True)
            (root / 'usr/bin/sbctl').write_text('#!/bin/sh\n')
            (root / 'usr/bin/sbctl').chmod(0o755)
        if keys:
            key = root / keys.lstrip('/')
            key.parent.mkdir(parents=True)
            key.write_text('key')
        return root

    def test_ready_needs_both_sbctl_and_a_signing_key(self):
        cases = [(dict(), 0), (dict(sbctl=False), 1), (dict(keys=None), 1),
                 (dict(keys='/usr/share/secureboot/keys/db/db.key'), 0)]
        for kwargs, expected in cases:
            with self.subTest(**kwargs):
                root = self.make_root(**kwargs)
                self.assertEqual(self.sh(f'iso_sbctl_ready "{root}"').returncode, expected)

    def test_signing_calls_sbctl_when_ready(self):
        result = self.sh('iso_sbctl_ready() { return 0; }\n'
                         'sbctl() { echo "sbctl $*"; }\n'
                         'iso_sign_boot_file /efi/Xetal/vmlinuz-6.1')
        self.assertEqual(result.returncode, 0, result.stderr)
        # sbctl's own output is hidden; only the call matters here.
        result = self.sh('iso_sbctl_ready() { return 0; }\n'
                         'sbctl() { echo "$*" >> "$LOG"; }\n'
                         'LOG=$(mktemp); iso_sign_boot_file /efi/Xetal/vmlinuz-6.1; cat "$LOG"')
        self.assertEqual(result.stdout.strip(), 'sign -s /efi/Xetal/vmlinuz-6.1')

    def test_signing_failure_only_warns(self):
        result = self.sh('iso_sbctl_ready() { return 0; }\nsbctl() { return 1; }\n'
                         'iso_sign_boot_file /boot/xetal/vmlinuz-6.1')
        self.assertEqual(result.returncode, 0)
        self.assertIn('[WARN] Could not sign /boot/xetal/vmlinuz-6.1', result.stderr)

    def test_nothing_is_signed_without_keys(self):
        result = self.sh('iso_sbctl_ready() { return 1; }\nsbctl() { echo CALLED; }\n'
                         'iso_sign_boot_file /efi/x')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_kernel_copies_are_signed_for_the_loader_that_reads_them(self):
        text = (REPO / 'iso/restore-boot.sh').read_text()
        self.assertIn('if [[ $BOOTLOADER == grub ]]; then iso_sign_boot_file "/boot/xetal/vmlinuz-$version"; fi', text)
        sd_boot = text.index('if [[ $BOOTLOADER == systemd-boot ]]; then\n            cp "/boot/xetal')
        self.assertLess(sd_boot, text.index('iso_sign_boot_file "/efi/Xetal/vmlinuz-$version"'))

    def test_grub_flags_are_only_added_when_signing_is_active(self):
        text = INSTALLER.read_text()
        self.assertIn('local -a grub_sb=()', text)
        self.assertIn('if (( sb_sign )); then grub_sb=(--modules=tpm --disable-shim-lock); fi', text)
        self.assertIn('--removable --no-nvram "${grub_sb[@]}"', text)
        self.assertEqual(text.count('--disable-shim-lock'), 1)
        self.assertIn('if (( uefi )) && iso_sbctl_ready "$TARGET"; then sb_sign=1; fi', text)

    def test_installer_still_refuses_to_run_with_secure_boot_enabled(self):
        self.assertIn('Disable Secure Boot before restoring', INSTALLER.read_text())


class WipeModeUnchangedTests(unittest.TestCase):
    def test_whole_disk_partitioning_commands_are_still_present_verbatim(self):
        text = INSTALLER.read_text()
        for command in ('parted -s "$disk" mklabel gpt',
                        'parted -s "$disk" mkpart BIOS 1MiB 3MiB',
                        'parted -s "$disk" mkpart ESP fat32 3MiB "$((esp_mib + 3))MiB"',
                        'parted -s "$disk" mkpart ROOT ext4 "$((esp_mib + 3))MiB" 100%'):
            self.assertIn(command, text)

    def test_mklabel_only_runs_in_the_wipe_branch(self):
        text = INSTALLER.read_text()
        wipe = text.index('    wipe)\n        echo \'[1/6]')
        free = text.index('    free)\n        echo \'[1/6]')
        self.assertLess(wipe, text.index('mklabel gpt'))
        self.assertLess(text.index('mklabel gpt'), free)
        self.assertEqual(text.count('mklabel'), 1)


if __name__ == '__main__':
    unittest.main()
