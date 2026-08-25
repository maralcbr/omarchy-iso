from __future__ import annotations

import inspect
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "configs/airootfs/usr/share/omarchy-iso"))
sys.modules.setdefault(
    "orchestrator.archinstall_adapter", types.ModuleType("orchestrator.archinstall_adapter")
)

from orchestrator import phases_impl  # noqa: E402


class ArmLimineTest(unittest.TestCase):
    def test_aarch64_uses_aa64_efi_binary(self) -> None:
        self.assertEqual(
            phases_impl._limine_efi_names("aarch64"),
            ("BOOTAA64.EFI", "limine_aa64.efi", "BOOTAA64.EFI"),
        )

    def test_x86_64_keeps_x64_efi_binary(self) -> None:
        self.assertEqual(
            phases_impl._limine_efi_names("x86_64"),
            ("BOOTX64.EFI", "limine_x64.efi", "BOOTX64.EFI"),
        )

    def test_arm_machine_detection(self) -> None:
        self.assertTrue(phases_impl._is_arm_machine("aarch64"))
        self.assertTrue(phases_impl._is_arm_machine("arm64"))
        self.assertFalse(phases_impl._is_arm_machine("x86_64"))

    def test_arm_updater_builds_uki_and_manages_one_entry(self) -> None:
        script = phases_impl.ARM_LIMINE_UPDATE_SCRIPT
        self.assertIn('--kernelimage "$kernel_image"', script)
        self.assertIn("### BEGIN OMARCHY ARM ENTRY ###", script)
        self.assertIn("protocol: efi", script)
        self.assertIn(
            "Target = linux-aarch64",
            inspect.getsource(phases_impl._install_arm_limine_updater),
        )

    def test_arm_encryption_uses_systemd_hook_and_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            cmdline = phases_impl._prepare_arm_systemd_encryption(
                SimpleNamespace(target=target),
                "quiet cryptdevice=UUID=1234-abcd:root root=/dev/mapper/root "
                "cryptkey=rootfs:/etc/omarchy/provisioning.key",
            )
            dropin = target / "etc/mkinitcpio.conf.d/90-omarchy-arm-encryption.conf"
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    (
                        "HOOKS=(base systemd autodetect microcode modconf kms keyboard "
                        f"sd-vconsole block filesystems fsck); source '{dropin}'; "
                        "printf '%s\\n' \"${HOOKS[*]}\""
                    ),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            crypttab = (target / "etc/crypttab.initramfs").read_text()

        self.assertEqual(
            result.stdout.strip(),
            "base systemd autodetect microcode modconf kms keyboard sd-vconsole "
            "block plymouth sd-encrypt filesystems fsck",
        )
        self.assertEqual(
            cmdline,
            "quiet rd.luks.name=1234-abcd=root root=/dev/mapper/root "
            "rd.luks.key=/etc/omarchy/provisioning.key splash",
        )
        self.assertEqual(
            crypttab,
            "root UUID=1234-abcd none luks,discard\n",
        )

    def test_bundled_templates_exist(self) -> None:
        assets = ROOT / "configs/airootfs/usr/share/omarchy-iso/assets/limine"
        self.assertIn("@@CMDLINE@@", (assets / "default.conf").read_text())
        self.assertIn("Omarchy Bootloader", (assets / "limine.conf").read_text())

    def test_bootstrap_installs_arm_limine_integration(self) -> None:
        self.assertTrue(
            {
                "limine",
                "limine-mkinitcpio-hook",
                "limine-snapper-sync",
                "snapper",
            }.issubset(phases_impl.EARLY_BOOTSTRAP_BASE_PACKAGES)
        )

    def test_offline_mirror_contains_arm_limine_integration(self) -> None:
        packages = set((ROOT / "builder/archinstall.packages").read_text().splitlines())
        self.assertTrue(
            {"limine-mkinitcpio-hook", "limine-snapper-sync", "snapper"}.issubset(
                packages
            )
        )


if __name__ == "__main__":
    unittest.main()
