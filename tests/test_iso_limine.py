"""Limine support: detection, generated config, hash pinning, and branch regressions.

restore-boot.sh is run for real inside a throw-away directory tree: every absolute
path under /efi, /boot, /etc and /usr is rewritten to the sandbox, and the system
tools (mkinitcpio, limine, sbctl wrappers, mountpoint) are shell mocks.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
COMMON = REPO / 'iso/common.sh'
RESTORE = REPO / 'iso/restore-boot.sh'
INSTALLER = REPO / 'iso/installer.sh'

ROOT_UUID = '1234abcd-12ab-34cd-56ef-0123456789ab'
HEX128 = re.compile(r'^[0-9a-f]{128}$')


def b2(path):
    return hashlib.blake2b(Path(path).read_bytes()).hexdigest()


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='iso-limine-detect-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def file(self, name, content='x', mode=0o755):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)

    def limine_package(self):
        self.file('usr/bin/limine')
        self.file('usr/share/limine/BOOTX64.EFI')

    def systemd_boot(self):
        self.file('usr/bin/bootctl')
        self.file('usr/lib/systemd/boot/efi/systemd-bootx64.efi')

    def bootloader(self, mode='x86_64-efi'):
        return subprocess.run(['bash', '-c', 'source "$1"; iso_bootloader "$2" "$3"', 'test',
                               str(COMMON), str(self.root), mode], capture_output=True, text=True)

    def test_cachyos_style_limine_source_is_detected(self):
        self.limine_package()
        self.systemd_boot()  # bootctl ships with systemd on every Arch system
        self.file('etc/default/limine')
        self.assertEqual(self.bootloader().stdout.strip(), 'limine')

    def test_each_known_marker_is_enough(self):
        for marker in ('etc/default/limine', 'etc/limine-entry-tool.conf', 'boot/limine.conf',
                       'boot/limine/limine.conf', 'efi/limine.conf', 'boot/EFI/BOOT/limine.conf'):
            with self.subTest(marker=marker):
                self.setUp()
                self.limine_package()
                self.file(marker)
                self.assertEqual(self.bootloader().stdout.strip(), 'limine')

    def test_installed_package_alone_does_not_override_systemd_boot(self):
        self.limine_package()
        self.systemd_boot()
        self.assertEqual(self.bootloader().stdout.strip(), 'systemd-boot')

    def test_grub_keeps_priority_over_limine(self):
        self.limine_package()
        self.file('etc/default/limine')
        self.file('usr/bin/grub-install')
        self.file('usr/lib/grub/x86_64-efi/modinfo.sh')
        self.assertEqual(self.bootloader().stdout.strip(), 'grub')

    def test_marker_without_the_limine_binary_is_not_enough(self):
        self.file('etc/default/limine')
        self.systemd_boot()
        self.assertEqual(self.bootloader().stdout.strip(), 'systemd-boot')
        self.file('usr/bin/limine')  # binary but no EFI file
        self.assertEqual(self.bootloader().stdout.strip(), 'systemd-boot')

    def test_bios_mode_never_selects_limine(self):
        self.limine_package()
        self.file('etc/default/limine')
        result = self.bootloader('i386-pc')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Limine or systemd-boot', result.stderr)


class RestoreBootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='iso-limine-boot-')
        self.addCleanup(self.tmp.cleanup)
        self.r = Path(self.tmp.name)
        for directory in ('efi', 'boot', 'etc', 'usr/lib/modules/6.1.0-test', 'usr/share/limine', 'usr/local/lib/xetal-iso'):
            (self.r / directory).mkdir(parents=True)
        (self.r / 'usr/lib/modules/6.1.0-test/vmlinuz').write_bytes(b'KERNEL-IMAGE')
        (self.r / 'usr/share/limine/BOOTX64.EFI').write_bytes(b'LIMINE-BINARY')
        (self.r / 'boot/intel-ucode.img').write_bytes(b'INTEL-MICROCODE')
        (self.r / 'usr/local/lib/xetal-iso/common.sh').write_text(COMMON.read_text() + r'''
iso_initramfs_tool() { echo mkinitcpio; }
iso_kernels() { printf '6.1.0-test\tlinux-test\t%s\n' "$KERNEL_FILE"; }
iso_sbctl_ready() { [[ ${MOCK_SB:-0} == 1 ]]; }
iso_sign_boot_file() { echo "sign $1" >> "$MOCK_LOG"; }
''')
        text = RESTORE.read_text().replace('[[ $EUID == 0 ]]', 'true')
        text = re.sub(r'(^|[ "\'=(:])/(efi|boot|etc|usr)(?=[/ "\'\n)]|$)', lambda m: f'{m.group(1)}{self.r}/{m.group(2)}',
                      text, flags=re.M)
        self.script = self.r / 'restore-boot.sh'
        self.script.write_text(text)
        self.log = self.r / 'mock.log'
        self.log.write_text('')

    def boot_conf(self, bootloader):
        (self.r / 'etc/xetal-boot.conf').write_text(f'ROOT_UUID={ROOT_UUID}\nBOOTLOADER={bootloader}\n')

    def run_boot(self, bootloader, sb=False):
        self.boot_conf(bootloader)
        body = r'''
source "$1"
mountpoint() { return 0; }
grub-script-check() { return 0; }
mkinitcpio() { local out i; for ((i = 1; i <= $#; i++)); do [[ ${!i} == -g ]] && out=${@:i+1:1}; done
    [[ -n ${out:-} ]] && printf 'INITRAMFS' > "$out"; return 0; }
limine() { echo "enroll $*" >> "$MOCK_LOG"; printf 'ENROLLED' >> "$2"; }
restore_boot_main
'''
        env = dict(os.environ, MOCK_LOG=str(self.log), MOCK_SB='1' if sb else '0',
                   KERNEL_FILE=str(self.r / 'usr/lib/modules/6.1.0-test/vmlinuz'))
        return subprocess.run(['bash', '-c', body, 'test', str(self.script)],
                              capture_output=True, text=True, timeout=30, env=env)

    def efi(self, name):
        return self.r / 'efi' / name

    # ---- Limine ----------------------------------------------------------
    def test_limine_config_binary_and_files_without_secure_boot(self):
        result = self.run_boot('limine')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        conf = self.efi('EFI/BOOT/limine.conf').read_text()
        self.assertEqual(conf.splitlines()[0], 'timeout: 5')
        for line in ('/Cloned system - linux-test', '    protocol: linux',
                     '    path: boot():/Xetal/vmlinuz-6.1.0-test',
                     '    module_path: boot():/Xetal/intel-ucode.img',
                     '    module_path: boot():/Xetal/initramfs-6.1.0-test.img',
                     f'    cmdline: root=UUID={ROOT_UUID} rw'):
            self.assertIn(line + '\n', conf)
        self.assertNotIn('#', conf)  # no hash pinning without sbctl keys
        # microcode must load before the initramfs
        self.assertLess(conf.index('intel-ucode.img'), conf.index('initramfs-6.1.0-test.img'))
        self.assertEqual(self.efi('EFI/BOOT/BOOTX64.EFI').read_bytes(), b'LIMINE-BINARY')
        self.assertEqual(self.efi('Xetal/vmlinuz-6.1.0-test').read_bytes(), b'KERNEL-IMAGE')
        self.assertEqual(self.efi('Xetal/initramfs-6.1.0-test.img').read_bytes(), b'INITRAMFS')
        self.assertEqual(self.efi('Xetal/intel-ucode.img').read_bytes(), b'INTEL-MICROCODE')
        self.assertEqual(self.log.read_text(), '')  # nothing enrolled or signed
        self.assertFalse(self.efi('loader').exists())  # no systemd-boot leftovers
        self.assertFalse((self.efi('EFI/BOOT/limine.conf.new')).exists())
        self.assertFalse((self.efi('EFI/BOOT/BOOTX64.EFI.new')).exists())

    def test_limine_hash_pinning_enrolls_the_final_config_then_signs(self):
        result = self.run_boot('limine', sb=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        conf_path = self.efi('EFI/BOOT/limine.conf')
        conf = conf_path.read_text()
        pinned = dict(re.findall(r'(?:path|module_path): boot\(\):/Xetal/(\S+?)#(\S+)', conf))
        self.assertEqual(set(pinned), {'vmlinuz-6.1.0-test', 'intel-ucode.img', 'initramfs-6.1.0-test.img'})
        for name, digest in pinned.items():
            self.assertRegex(digest, HEX128)
            self.assertEqual(digest, b2(self.efi('Xetal') / name), name)
        # every path line carries a hash (Limine panics on an unpinned path when enrolled)
        for line in conf.splitlines():
            if re.match(r'\s+(path|module_path):', line):
                self.assertIn('#', line)
        log = self.log.read_text().splitlines()
        self.assertEqual(len(log), 2, log)
        enroll, sign = log
        self.assertTrue(enroll.startswith('enroll enroll-config '), enroll)
        self.assertTrue(enroll.split()[2].endswith('BOOTX64.EFI.new'))
        self.assertEqual(enroll.split()[3], b2(conf_path))  # hash of the config exactly as written
        self.assertEqual(sign, f'sign {self.efi("EFI/BOOT/BOOTX64.EFI")}')
        # the enrolled binary is what ended up at the final path
        self.assertEqual(self.efi('EFI/BOOT/BOOTX64.EFI').read_bytes(), b'LIMINE-BINARYENROLLED')

    def test_stale_kernel_copies_are_removed_but_other_files_stay(self):
        (self.efi('Xetal')).mkdir()
        for name in ('vmlinuz-5.0-old', 'initramfs-5.0-old.img', 'amd-ucode.img', 'unrelated.txt'):
            self.efi(f'Xetal/{name}').write_text('x')
        self.assertEqual(self.run_boot('limine').returncode, 0)
        self.assertFalse(self.efi('Xetal/vmlinuz-5.0-old').exists())
        self.assertFalse(self.efi('Xetal/initramfs-5.0-old.img').exists())
        self.assertTrue(self.efi('Xetal/amd-ucode.img').exists())
        self.assertTrue(self.efi('Xetal/unrelated.txt').exists())
        self.assertTrue(self.efi('Xetal/vmlinuz-6.1.0-test').exists())

    def test_missing_limine_package_files_fail_cleanly_without_touching_the_binary(self):
        (self.r / 'usr/share/limine/BOOTX64.EFI').unlink()
        self.efi('EFI/BOOT').mkdir(parents=True)
        self.efi('EFI/BOOT/BOOTX64.EFI').write_bytes(b'OLD-WORKING-BINARY')
        result = self.run_boot('limine')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('limine package files are missing', result.stderr)
        self.assertEqual(self.efi('EFI/BOOT/BOOTX64.EFI').read_bytes(), b'OLD-WORKING-BINARY')
        self.assertFalse(self.efi('EFI/BOOT/limine.conf').exists())

    def test_requires_the_efi_partition_to_be_mounted(self):
        self.boot_conf('limine')
        body = 'source "$1"\nmountpoint() { return 1; }\nrestore_boot_main'
        result = subprocess.run(['bash', '-c', body, 'test', str(self.script)], capture_output=True, text=True,
                                env=dict(os.environ, KERNEL_FILE='x', MOCK_LOG=str(self.log)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Mount the EFI system partition', result.stderr)

    # ---- the branches that existed before must still behave ----------------
    def test_systemd_boot_branch_is_unchanged(self):
        result = self.run_boot('systemd-boot')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        entry = self.efi('loader/entries/xetal-6.1.0-test.conf').read_text()
        self.assertIn('title Cloned system - linux-test', entry)
        self.assertIn('linux /Xetal/vmlinuz-6.1.0-test', entry)
        self.assertIn(f'options root=UUID={ROOT_UUID} rw', entry)
        self.assertEqual(self.efi('loader/loader.conf').read_text(), 'default xetal-*\ntimeout 5\n')
        self.assertFalse(self.efi('EFI/BOOT/limine.conf').exists())
        self.assertFalse(self.efi('EFI/BOOT/BOOTX64.EFI').exists())

    def test_grub_branch_is_unchanged(self):
        result = self.run_boot('grub')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        cfg = (self.r / 'boot/grub/grub.cfg').read_text()
        self.assertIn("menuentry 'Cloned system - linux-test'", cfg)
        self.assertIn(f'root=UUID={ROOT_UUID} rw', cfg)
        self.assertFalse(self.efi('EFI/BOOT/limine.conf').exists())
        self.assertFalse((self.r / 'efi/loader').exists())


class InstallerWiringTests(unittest.TestCase):
    def test_hook_retriggers_on_limine_package_updates(self):
        self.assertIn('Target = usr/share/limine/BOOTX64.EFI', INSTALLER.read_text())

    def test_limine_never_falls_into_the_systemd_boot_install_branch(self):
        text = INSTALLER.read_text()
        self.assertIn("elif [[ $loader == limine ]]; then", text)
        self.assertIn("elif [[ $loader == limine ]]; then", text.split("bootctl --esp-path")[0])
        # bootctl must only be reachable in the final (systemd-boot) else branch
        before_bootctl = text.split('bootctl --esp-path')[0]
        self.assertLess(before_bootctl.rindex("elif [[ $loader == limine ]]"), before_bootctl.rindex('    else\n'))


if __name__ == '__main__':
    unittest.main()
