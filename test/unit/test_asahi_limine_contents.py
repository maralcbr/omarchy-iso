"""The emitted content/config evidence must inspect the selected Limine bytes."""
import importlib.util
import json
from pathlib import Path
import unittest

import test_asahi_limine as fixture
import test_asahi_os_package_contents as contents

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('installed', ROOT/'builder/verify-asahi-installed-system.py')
installed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installed)


class LimineContents(unittest.TestCase):
    write = fixture.AppleLimine.write

    def setUp(self):
        fixture.AppleLimine.setUp(self)
        files = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        grub = (f'menuentry "Omarchy" {{\n linux /vmlinuz-linux-asahi root=UUID={self.uuid} '
                'rootflags=subvol=@\n initrd /initramfs-linux-asahi.img\n}\n')
        contents._write_complete_target(self.root, grub.encode())
        for name, data in files.items():
            (self.root/name).write_bytes(data)
        self.write('usr/share/omarchy/apple-boot-profile.json', json.dumps(fixture.boot.PROFILE))
        for name in ('omarchy-mac-boot', 'limine', 'limine-mkinitcpio-hook', 'limine-snapper-sync', 'uboot-asahi'):
            self.write(f'var/lib/pacman/local/{name}-1-1/desc', f'%NAME%\n{name}\n')

    def capture(self):
        return contents.MODULE.capture(self.root, contents.NODE_IDENTITY, 'linux-asahi')

    def test_content_evidence_binds_real_menu_uki_defaults_loader_and_profile(self):
        evidence = self.capture()
        self.assertEqual(evidence['boot_contract']['backend'], 'asahi-limine')
        self.assertEqual(evidence['boot_contract']['root_selector'], 'UUID='+self.uuid)
        for name in ('esp_limine_menu', 'esp_limine_uki', 'root_limine_defaults', 'root_boot_profile', 'root_limine_loader'):
            self.assertIn(name, evidence['artifacts'])
        verification = installed.Verification()
        installed.check_limine(verification, self.root, 'linux-asahi')
        self.assertEqual(verification.failed, [])

    def test_inactive_grub_bridge_does_not_override_active_limine_root_contract(self):
        path = self.root / 'boot/grub/grub.cfg'
        path.write_text(path.read_text().replace('rootflags=subvol=@',
                       'rootflags=subvol=@ rootflags=x-systemd.device-timeout=0'))
        evidence = self.capture()
        self.assertEqual(evidence['boot_contract']['retained_artifacts'],
                         {'boot_grub_config': 'inactive-configured-bridge'})
        self.assertIn('boot_grub_config', evidence['artifacts'])
        self.assertNotIn('linux_entries', evidence['boot_contract'])
        self.write('etc/default/limine', 'KERNEL_CMDLINE[default]="' +
                   self.cmdline.replace(self.uuid, '87654321-1234-1234-1234-123456789abc') + '"\n')
        with self.assertRaisesRegex(contents.MODULE.ContentEvidenceError, 'fstab'):
            self.capture()

    def test_content_and_config_verifiers_reject_stale_embedded_initramfs(self):
        self.write('boot/efi/EFI/Linux/omarchy_linux-asahi.efi', fixture.pe({**self.sections, '.initrd': b'stale'}))
        with self.assertRaisesRegex(contents.MODULE.ContentEvidenceError, 'UKI .initrd'):
            self.capture()
        verification = installed.Verification()
        installed.check_limine(verification, self.root, 'linux-asahi')
        self.assertEqual(verification.failed, ['boot-limine-artifacts'])

    def test_unknown_profile_cannot_fall_back_to_grub(self):
        self.write('usr/share/omarchy/apple-boot-profile.json', '{"boot_profile":"unknown"}')
        with self.assertRaisesRegex(contents.MODULE.ContentEvidenceError, 'invalid installed Apple boot profile'):
            self.capture()

    def test_legacy_boot_owner_cannot_coexist(self):
        self.write('var/lib/pacman/local/omarchy-first-boot-1-1/desc', '%NAME%\nomarchy-first-boot\n')
        verification = installed.Verification()
        installed.check_limine(verification, self.root, 'linux-asahi')
        self.assertEqual(verification.failed, ['boot-limine-packages'])


if __name__ == '__main__':
    unittest.main()
