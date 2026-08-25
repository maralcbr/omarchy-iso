from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

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
