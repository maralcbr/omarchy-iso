from __future__ import annotations

import inspect
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "configs/airootfs/usr/share/omarchy-iso"))
sys.modules.setdefault(
    "orchestrator.archinstall_adapter", types.ModuleType("orchestrator.archinstall_adapter")
)

from orchestrator import configured_phases, phases_impl  # noqa: E402


class ArmLimineTest(unittest.TestCase):
    def test_pre_mounted_locale_is_generated_without_systemd_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "etc").mkdir()
            (target / "etc/locale.gen").write_text(
                "#en_US.UTF-8 UTF-8  \n#en_GB.UTF-8 UTF-8\n"
            )
            locale = SimpleNamespace(sys_lang="en_US.UTF-8", sys_enc="UTF-8")

            with patch.object(phases_impl.subprocess, "run") as run:
                phases_impl._configure_pre_mounted_locale(target, locale)

            self.assertEqual(
                (target / "etc/locale.gen").read_text(),
                "en_US.UTF-8 UTF-8\n#en_GB.UTF-8 UTF-8\n",
            )
            self.assertEqual(
                (target / "etc/locale.conf").read_text(),
                "LANG=en_US.UTF-8\n",
            )
            run.assert_called_once_with(
                ["arch-chroot", str(target), "locale-gen"],
                check=True,
            )

    def test_pre_mounted_timezone_is_configured_without_systemd_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "etc").mkdir()
            timezone = target / "usr/share/zoneinfo/Australia/Brisbane"
            timezone.parent.mkdir(parents=True)
            timezone.write_bytes(b"TZif")
            ctx = SimpleNamespace(target=target)
            installer = SimpleNamespace(set_timezone=lambda _zone: self.fail("used arch-chroot -S"))

            phases_impl._configure_timezone(ctx, installer, "Australia/Brisbane", True)

            self.assertEqual(
                (target / "etc/localtime").readlink(),
                Path("/usr/share/zoneinfo/Australia/Brisbane"),
            )

    def test_pre_mounted_time_sync_is_enabled_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            ctx = SimpleNamespace(target=target)
            installer = SimpleNamespace(
                activate_time_synchronization=lambda: self.fail("used arch-chroot -S")
            )

            with patch.object(phases_impl.subprocess, "run") as run:
                phases_impl._configure_time_sync(ctx, installer, True, True)

            run.assert_called_once_with(
                [
                    "systemctl",
                    "--root",
                    str(target),
                    "enable",
                    "systemd-timesyncd.service",
                ],
                check=True,
            )

    def test_asahi_grub_bootstrap_excludes_limine_and_snapper(self) -> None:
        ctx = SimpleNamespace(
            is_protected=True,
            omarchy_install={"boot": {"backend": "asahi-grub"}},
        )

        packages = phases_impl._early_bootstrap_packages(ctx)

        self.assertIn("grub", packages)
        self.assertIn("btrfs-progs", packages)
        self.assertIn(phases_impl._omarchy_settings_package(), packages)
        self.assertTrue(
            {"limine", "limine-mkinitcpio-hook", "limine-snapper-sync", "snapper"}.isdisjoint(
                packages
            )
        )

    def test_asahi_grub_prepares_kernel_initramfs_and_official_updater(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            module_dir = target / "usr/lib/modules/7.1.6-asahi"
            module_dir.mkdir(parents=True)
            (module_dir / "pkgbase").write_text("linux-asahi\n")
            (module_dir / "vmlinuz").write_bytes(b"kernel")
            (target / "usr/bin").mkdir(parents=True)
            (target / "usr/bin/update-grub").write_text("#!/bin/sh\n")
            (target / "usr/bin/grub-mkconfig").write_text("#!/bin/sh\n")
            (target / "usr/lib/grub/arm64-efi").mkdir(parents=True)
            (target / "boot/efi/m1n1").mkdir(parents=True)
            (target / "boot/efi/m1n1/boot.bin").write_bytes(b"m1n1")
            ctx = SimpleNamespace(
                target=target,
                is_protected=True,
                encrypt=False,
                user_configuration={},
                omarchy_install={
                    "boot": {
                        "backend": "asahi-grub",
                        "esp_mount": "/boot/efi",
                        "register_firmware": False,
                    },
                    "storage": {
                        "root_device": "/dev/loop7",
                        "kernel": "linux-asahi",
                        "boot_mount": "/boot",
                    },
                },
            )

            def fake_run(argv, **kwargs):
                if argv[-2:] == ["mkinitcpio", "-P"]:
                    (target / "boot/initramfs-linux-asahi.img").write_bytes(b"initramfs")
                elif argv[2:5] == ["/bin/bash", "-s", "--"]:
                    self.assertEqual(argv[5], "/dev/loop7")
                    self.assertEqual(
                        argv[6],
                        "4f4d5801-524f-4f54-8000-000000000001",
                    )
                    self.assertTrue(kwargs["text"])
                    self.assertIn("/dev/disk/by-uuid", kwargs["input"])
                    self.assertIn("/usr/bin/update-grub", kwargs["input"])
                    self.assertIn("trap cleanup EXIT", kwargs["input"])
                    self.assertTrue(
                        (target / "boot/grub").is_dir(),
                        "update-grub requires its GRUB directory before grub-mkrelpath",
                    )
                    self.assertTrue(
                        (target / "boot/efi/EFI/BOOT").is_dir(),
                        "update-grub requires the removable EFI target directory",
                    )
                    (target / "boot/grub").mkdir(parents=True, exist_ok=True)
                    (target / "boot/grub/grub.cfg").write_text("menuentry 'Omarchy' {}\n")
                    (target / "boot/efi/EFI/BOOT").mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    (target / "boot/efi/EFI/BOOT/BOOTAA64.EFI").write_bytes(b"grub")
                return subprocess.CompletedProcess(argv, 0)

            with patch.object(
                configured_phases,
                "_blkid_uuid",
                return_value="4f4d5801-524f-4f54-8000-000000000001",
            ), patch.object(phases_impl.subprocess, "run", side_effect=fake_run) as run:
                phases_impl.finalize_boot(ctx)

            self.assertEqual((target / "boot/vmlinuz-linux-asahi").read_bytes(), b"kernel")
            self.assertIn(
                "asahi",
                (target / "etc/mkinitcpio.conf.d/90-omarchy-asahi.conf").read_text(),
            )
            self.assertIn(
                'GRUB_DISTRIBUTOR="Omarchy"',
                (target / "etc/default/grub").read_text(),
            )
            self.assertIn(
                'GRUB_CMDLINE_LINUX="zswap.enabled=0 rootfstype=btrfs"',
                (target / "etc/default/grub").read_text(),
            )
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(
                ["arch-chroot", str(target), "mkinitcpio", "-P"],
                commands,
            )
            self.assertIn(
                [
                    "arch-chroot",
                    str(target),
                    "/bin/bash",
                    "-s",
                    "--",
                    "/dev/loop7",
                    "4f4d5801-524f-4f54-8000-000000000001",
                ],
                commands,
            )
            self.assertFalse(any("efibootmgr" in command for command in commands))

    def test_asahi_boot_validation_uses_vendor_update_hooks_not_limine_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            vendor_hooks = target / "usr/share/libalpm/hooks"
            vendor_hooks.mkdir(parents=True)
            (vendor_hooks / "90-mkinitcpio-install.hook").write_text(
                "Target = usr/lib/modules/*/vmlinuz\n"
                "Exec = /usr/share/libalpm/scripts/mkinitcpio install\n"
            )
            (vendor_hooks / "95-m1n1-install.hook").write_text(
                "Target = usr/lib/asahi-boot/*\nExec = /usr/bin/update-m1n1\n"
            )
            mkinitcpio_script = target / "usr/share/libalpm/scripts/mkinitcpio"
            mkinitcpio_script.parent.mkdir(parents=True)
            mkinitcpio_script.write_text("#!/bin/sh\n")
            mkinitcpio_script.chmod(0o755)
            preset = target / "etc/mkinitcpio.d/linux-asahi.preset"
            preset.parent.mkdir(parents=True)
            preset.write_text(
                "ALL_kver='/boot/vmlinuz-linux-asahi'\n"
                "default_image='/boot/initramfs-linux-asahi.img'\n"
            )
            updater = target / "usr/bin/update-m1n1"
            updater.parent.mkdir(parents=True)
            updater.write_text("#!/bin/sh\n")
            updater.chmod(0o755)
            ctx = SimpleNamespace(
                target=target,
                is_protected=True,
                omarchy_install={"boot": {"backend": "asahi-grub"}},
            )

            phases_impl._assert_boot_hooks_restored(ctx)

    def test_asahi_target_setup_uses_a_deterministic_apple_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            ctx = SimpleNamespace(
                target=target,
                is_protected=True,
                omarchy_install={"boot": {"backend": "asahi-grub"}},
            )

            env = phases_impl._target_platform_env(ctx)

            self.assertEqual(
                env,
                ["OMARCHY_PROC_ROOT=/run/omarchy-install/platform-probe"],
            )
            self.assertIn(
                b"apple,arm-platform\0",
                (target / "run/omarchy-install/platform-probe/device-tree/compatible").read_bytes(),
            )

    def test_asahi_target_setup_temporarily_overrides_packaged_detector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            detector = target / "usr/bin/omarchy-hw-apple-silicon"
            detector.parent.mkdir(parents=True)
            detector.write_text("#!/bin/bash\nexit 1\n")
            detector.chmod(0o755)
            ctx = SimpleNamespace(
                target=target,
                is_protected=True,
                omarchy_install={"boot": {"backend": "asahi-grub"}},
            )

            with phases_impl._target_platform_override(ctx):
                self.assertEqual(detector.read_text(), "#!/bin/bash\nexit 0\n")
                self.assertTrue(
                    detector.with_name("omarchy-hw-apple-silicon.omarchy-image-original").exists()
                )

            self.assertEqual(detector.read_text(), "#!/bin/bash\nexit 1\n")
            self.assertFalse(
                detector.with_name("omarchy-hw-apple-silicon.omarchy-image-original").exists()
            )

    def test_asahi_target_setup_repairs_the_legacy_dmi_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            leaf = target / "usr/share/omarchy/install/hardware/apple/fix-spi-keyboard.sh"
            leaf.parent.mkdir(parents=True)
            leaf.write_text(
                'product_name="$(cat /sys/class/dmi/id/product_name 2>/dev/null)"\n'
            )
            ctx = SimpleNamespace(
                target=target,
                is_protected=True,
                omarchy_install={"boot": {"backend": "asahi-grub"}},
            )

            phases_impl._repair_legacy_apple_dmi_probe(ctx)

            self.assertIn("2>/dev/null || true", leaf.read_text())

    def test_asahi_package_does_not_stage_limine_hibernation_state(self) -> None:
        ctx = SimpleNamespace(
            is_protected=True,
            omarchy_install={"boot": {"backend": "asahi-grub"}},
        )

        with patch.object(phases_impl.subprocess, "run") as run:
            phases_impl.configure_hibernation(ctx)

        run.assert_not_called()

    def test_package_builder_can_select_verified_offline_mirror_namespace(self) -> None:
        with patch.dict(
            "os.environ",
            {"OMARCHY_OFFLINE_MIRROR_ROOT": "/var/cache/airootfs/verified/offline"},
        ):
            self.assertEqual(
                phases_impl._offline_package_source(),
                Path("/var/cache/airootfs/verified/offline"),
            )

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                phases_impl._offline_package_source(),
                Path("/var/cache/omarchy/mirror/offline"),
            )

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
        script = phases_impl._arm_limine_update_script("linux-aarch64")
        self.assertIn('--kernelimage "$kernel_image"', script)
        self.assertIn('kernel="linux-aarch64"', script)
        self.assertIn('kernel_image="$ALL_kver"', script)
        self.assertIn("### BEGIN OMARCHY ARM ENTRY ###", script)
        self.assertIn("protocol: efi", script)

    def test_apple_product_selects_linux_asahi_for_uki_and_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            ctx = SimpleNamespace(
                target=target,
                omarchy_install={"storage": {"kernel": "linux-asahi"}},
            )
            phases_impl._install_arm_limine_updater(ctx)

            updater = target / "usr/local/bin/omarchy-arm-limine-update"
            hook = target / "etc/pacman.d/hooks/95-omarchy-arm-limine.hook"
            self.assertIn('kernel="linux-asahi"', updater.read_text())
            self.assertIn("Target = linux-asahi", hook.read_text())

    def test_package_fstab_mounts_boot_and_grows_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "etc").mkdir()
            ctx = SimpleNamespace(
                target=target,
                is_protected=True,
                omarchy_install={
                    "boot": {"esp_mount": "/boot/efi"},
                    "storage": {
                        "root_device": "/dev/loop-root",
                        "boot_device": "/dev/loop-boot",
                        "boot_mount": "/boot",
                        "esp_device": "/dev/loop-esp",
                        "grow_root": True,
                    },
                },
            )
            identifiers = {
                "/dev/loop-root": "root-uuid",
                "/dev/loop-boot": "boot-uuid",
                "/dev/loop-esp": "esp-uuid",
            }
            with patch.object(
                configured_phases,
                "_blkid_uuid",
                side_effect=lambda device: identifiers[device],
            ):
                phases_impl._write_pre_mounted_fstab(ctx)

            fstab = (target / "etc/fstab").read_text()
            self.assertIn(
                "UUID=root-uuid  /                      btrfs  "
                "noatime,compress=zstd,x-systemd.growfs,subvol=@",
                fstab,
            )
            self.assertIn("UUID=boot-uuid  /boot", fstab)
            self.assertIn("UUID=esp-uuid  /boot/efi", fstab)

    def test_image_build_copies_limine_without_touching_firmware_nvram(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            source = target / "usr/share/limine/BOOTAA64.EFI"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"limine")
            ctx = SimpleNamespace(
                target=target,
                is_protected=True,
                omarchy_install={
                    "boot": {
                        "esp_mount": "/boot/efi",
                        "esp_path": "/EFI/BOOT",
                        "efi_binary": "BOOTAA64.EFI",
                        "register_firmware": False,
                    },
                    "storage": {"esp_device": "/dev/loop-esp"},
                },
            )
            with patch.object(phases_impl, "_read_efibootmgr") as read_nvram, patch.object(
                phases_impl,
                "_register_limine_efi_entry",
            ) as register:
                phases_impl._install_pre_mounted_limine(ctx)

            self.assertEqual(
                (target / "boot/efi/EFI/BOOT/BOOTAA64.EFI").read_bytes(),
                b"limine",
            )
            read_nvram.assert_not_called()
            register.assert_not_called()

    def test_arm_encryption_uses_systemd_hook_and_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            cmdline = phases_impl._prepare_arm_systemd_encryption(
                SimpleNamespace(target=target),
                "quiet console=ttyAMA0 cryptdevice=UUID=1234-abcd:root root=/dev/mapper/root "
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
            "rd.luks.key=/etc/omarchy/provisioning.key splash console=tty0 "
            "plymouth.ignore-serial-consoles",
        )
        self.assertNotIn("console=ttyAMA0", cmdline)
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
