"""Apple image finalization fixtures; no mount, block device or host trust changes."""
import importlib.util
import contextlib
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "configs/airootfs/usr/share/omarchy-iso"))
sys.modules.setdefault("orchestrator.archinstall_adapter", types.ModuleType("orchestrator.archinstall_adapter"))
from orchestrator import asahi_limine as boot
from orchestrator import configured_phases as shared
from orchestrator import finalized_phases


def pe(payloads, machine=0xAA64):
    """Small PE32+ image with normal file alignment and virtual section sizes."""
    header = bytearray(1024)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 128)
    header[128:132] = b"PE\0\0"
    struct.pack_into("<HH", header, 132, machine, len(payloads))
    struct.pack_into("<H", header, 148, 240)
    struct.pack_into("<H", header, 152, 0x20B)
    output = header
    for index, (name, data) in enumerate(payloads.items()):
        size = (len(data) + 511) // 512 * 512
        section = 392 + 40 * index
        output[section:section + 8] = name.encode().ljust(8, b"\0")
        struct.pack_into("<IIII", output, section + 8, len(data), 0x1000 * (index + 1), size, len(output))
        output += data.ljust(size, b"\0")
    return bytes(output)


class AppleLimine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir()
        self.media = Path(self.tmp.name) / "media"
        self.media.mkdir()
        self.environment = mock.patch.dict(os.environ, {"OMARCHY_ISO_MEDIA_ROOT": str(self.media)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.uuid = "12345678-1234-1234-1234-123456789abc"
        self.cmdline = f"root=UUID={self.uuid} rw rootflags=subvol=@,x-systemd.device-timeout=0 rootfstype=btrfs"
        self.kver = "7.1.6-asahi"
        self.sections = {".linux": b"fixture Apple kernel", ".initrd": b"fixture final initramfs",
                         ".osrel": f"VERSION_ID={self.kver}\nID=archarm\n".encode(),
                         ".uname": self.kver.encode(), ".cmdline": self.cmdline.encode() + b" \n\0"}
        self.write("usr/share/limine/BOOTAA64.EFI", pe({".text": b"fixture loader"}))
        self.write("boot/efi/EFI/BOOT/BOOTAA64.EFI", pe({".text": b"fixture loader"}))
        self.write("boot/efi/EFI/Linux/omarchy_linux-asahi.efi", pe(self.sections))
        self.write("boot/vmlinuz-linux-asahi", self.sections[".linux"])
        self.write("boot/initramfs-linux-asahi.img", self.sections[".initrd"])
        self.write("etc/os-release", b"ID=archarm\n")
        self.write(f"usr/lib/modules/{self.kver}/pkgbase", "linux-asahi\n")
        self.write(f"usr/lib/modules/{self.kver}/vmlinuz", self.sections[".linux"])
        self.write("etc/fstab", f"UUID={self.uuid} / btrfs subvol=@ 0 0\n")
        self.write("etc/default/limine", f'KERNEL_CMDLINE[default]="{self.cmdline}"\n')
        self.write("boot/efi/m1n1/boot.bin", b"fixture m1n1")
        self.write("boot/efi/limine.conf", "interface_branding: Omarchy Bootloader\n/+Omarchy\n  //linux-asahi\n    protocol: efi\n    path: boot():/EFI/Linux/omarchy_linux-asahi.efi\n")
        (self.root / "boot/efi/omarchy").mkdir()

    def write(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        return path

    def check(self):
        boot.validate_artifacts(self.root, "linux-asahi")

    def test_artifacts_have_matching_embedded_kernel_initramfs_release_and_cmdline(self):
        self.check()

    def test_uki_cannot_retain_old_kernel_or_initramfs(self):
        for section in (".linux", ".initrd", ".osrel", ".cmdline", ".uname"):
            with self.subTest(section=section):
                self.write("boot/efi/EFI/Linux/omarchy_linux-asahi.efi", pe({**self.sections, section: b"stale"}))
                with self.assertRaisesRegex(RuntimeError, "UKI"):
                    self.check()

    def test_osrel_replaces_only_distro_version_id_with_selected_kernel(self):
        self.write("etc/os-release", b"VERSION_ID=rolling\nID=archarm\n")
        self.check()
        self.write("etc/os-release", b"VERSION_ID=rolling\nID=other\n")
        with self.assertRaisesRegex(RuntimeError, "os-release"):
            self.check()

    def test_release_requires_unique_installed_kernel_with_matching_bytes(self):
        self.write(f"usr/lib/modules/{self.kver}/vmlinuz", b"different module kernel")
        with self.assertRaisesRegex(RuntimeError, "installed kernel"):
            self.check()
        self.write(f"usr/lib/modules/{self.kver}/vmlinuz", self.sections[".linux"])
        self.write("usr/lib/modules/another/pkgbase", "linux-asahi\n")
        with self.assertRaisesRegex(RuntimeError, "one installed kernel"):
            self.check()
        (self.root / "usr/lib/modules/another/pkgbase").unlink()
        (self.root / f"usr/lib/modules/{self.kver}/pkgbase").unlink()
        with self.assertRaisesRegex(RuntimeError, "one installed kernel"):
            self.check()

    def test_cmdline_accepts_only_exact_native_writer_delimiters(self):
        for cmdline in (self.cmdline.encode() + b"\0", self.cmdline.encode() + b"  \n\0",
                        self.cmdline.replace(" rw ", " ro ").encode() + b" \n\0",
                        self.cmdline.encode() + b"\0ignored \n\0"):
            with self.subTest(cmdline=cmdline):
                self.write("boot/efi/EFI/Linux/omarchy_linux-asahi.efi", pe({**self.sections, ".cmdline": cmdline}))
                with self.assertRaisesRegex(RuntimeError, "command line"):
                    self.check()

    def test_same_size_grub_or_wrong_architecture_loader_rejected(self):
        self.write("boot/efi/EFI/BOOT/BOOTAA64.EFI", pe({".text": b"another loader"}))
        with self.assertRaisesRegex(RuntimeError, "differs"):
            self.check()
        for rel in ("usr/share/limine/BOOTAA64.EFI", "boot/efi/EFI/BOOT/BOOTAA64.EFI"):
            self.write(rel, pe({".text": b"x86 loader"}, 0x8664))
        with self.assertRaisesRegex(RuntimeError, "ARM64"):
            self.check()

    def test_truncated_duplicate_and_out_of_bounds_pe_sections_rejected(self):
        path = self.root / "boot/efi/EFI/Linux/omarchy_linux-asahi.efi"
        for change in ("truncated", "duplicate", "offset"):
            with self.subTest(change=change):
                data = bytearray(pe(self.sections))
                if change == "truncated":
                    del data[-3:]
                elif change == "duplicate":
                    data[432:440] = data[392:400]
                else:
                    struct.pack_into("<I", data, 412, 1)
                path.write_bytes(data)
                with self.assertRaises(RuntimeError):
                    boot.pe_sections(path)

    def test_menu_must_reference_uki_and_not_override_verified_cmdline(self):
        path = self.root / "boot/efi/limine.conf"
        original = path.read_text()
        path.write_text(original.replace("omarchy_linux-asahi.efi", "stale.efi"))
        with self.assertRaisesRegex(RuntimeError, "menu"):
            self.check()
        path.write_text(original + "    cmdline: root=/dev/wrong\n")
        with self.assertRaisesRegex(RuntimeError, "overrides"):
            self.check()

    def test_native_entry_tool_blake2_path_hash_is_verified(self):
        path = self.root / "boot/efi/limine.conf"
        digest = hashlib.blake2b((self.root / "boot/efi/EFI/Linux/omarchy_linux-asahi.efi").read_bytes()).hexdigest()
        original = path.read_text()
        path.write_text(original.replace(".efi\n", f".efi#{digest}\n"))
        self.check()
        path.write_text(original.replace(".efi\n", f".efi#{'f' * 128}\n"))
        with self.assertRaisesRegex(RuntimeError, "verification hash"):
            self.check()

    def test_installer_staging_and_fstab_are_checked(self):
        staged = self.write("boot/efi/omarchy/install.conf", "encrypt=1\n")
        with self.assertRaisesRegex(RuntimeError, "staging"):
            self.check()
        staged.unlink()
        self.write("etc/fstab", "UUID=87654321-1234-1234-1234-123456789abc / btrfs subvol=@ 0 0\n")
        with self.assertRaisesRegex(RuntimeError, "fstab"):
            self.check()

    def test_shipping_root_scrub_preserves_fresh_markers_and_offline_node(self):
        self.seed_private_state()
        boot.scrub_identity(self.root)
        self.assertEqual((self.root / "etc/machine-id").read_bytes(), b"")
        for rel in ("etc/pacman.d/gnupg", "var/lib/systemd/random-seed", "var/log/journal", "etc/ssh/ssh_host_ed25519_key"):
            self.assertFalse((self.root / rel).exists(), rel)
        self.assertTrue((self.root / "var/lib/omarchy/mac-first-boot/pending").exists())
        self.assertTrue((self.root / "var/lib/omarchy/provisioning/packages/node-test.tar.gz").exists())

    def seed_private_state(self):
        for rel in ("etc/machine-id", "etc/pacman.d/gnupg/private-keys-v1.d/key",
                    "etc/ssh/ssh_host_ed25519_key", "var/log/journal/id/data",
                    "var/lib/systemd/random-seed", "var/lib/dbus/machine-id",
                    "var/lib/omarchy/mac-first-boot/pending", "var/lib/omarchy/mac-first-boot/deferred-steps",
                    "var/lib/omarchy/provisioning/pending", "var/lib/omarchy/provisioning/luks-key",
                    "var/lib/omarchy/provisioning/packages/node-test.tar.gz", "var/lib/omarchy/limine.enabled"):
            self.write(rel, b"fixture")

    def test_factory_scrub_removes_conversion_permission_and_owner_keys(self):
        self.seed_private_state()
        boot.scrub_factory(self.root)
        for rel in ("var/lib/omarchy/mac-first-boot", "var/lib/omarchy/provisioning/pending",
                    "var/lib/omarchy/provisioning/luks-key", "etc/pacman.d/gnupg"):
            self.assertFalse((self.root / rel).exists(), rel)
        for rel in ("var/lib/omarchy/limine.enabled", "etc/default/limine",
                    "var/lib/omarchy/provisioning/packages/node-test.tar.gz"):
            self.assertTrue((self.root / rel).is_file(), rel)

    def test_scrub_rejects_symlink_parent_without_touching_external_tree(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "private-key").write_text("fixture")
        (self.root / "etc/pacman.d").symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "unsafe image cleanup"):
            boot.scrub_identity(self.root)
        self.assertEqual((outside / "private-key").read_text(), "fixture")

    def test_only_explicit_finalized_profile_selects_apple_limine(self):
        ctx = types.SimpleNamespace(defer_provisioning=True, encrypt=False)
        with mock.patch.object(shared, "_boot_backend", return_value="asahi-grub"):
            self.assertFalse(boot.selected(ctx))
            path = self.media / "apple-boot-profile.json"
            path.write_text(json.dumps(boot.PROFILE))
            self.assertTrue(boot.selected(ctx))
            path.write_text(json.dumps({**boot.PROFILE, "candidate_schema": 3}))
            with self.assertRaisesRegex(RuntimeError, "invalid Apple boot profile"):
                boot.selected(ctx)

    def run_finalizer(self, fail_activation=False, missing_hook=False):
        for rel in ("etc/mkinitcpio.conf.d/90-omarchy-mac.conf",
                    "etc/mkinitcpio.conf.d/91-omarchy-mac-encrypt.conf",
                    "etc/mkinitcpio.conf.d/92-omarchy-mac-hid.conf",
                    "usr/lib/omarchy/mac-first-boot/omarchy-mac-first-boot",
                    "usr/lib/omarchy/initcpio/omarchy-mac-encrypt",
                    "usr/lib/systemd/system/omarchy-mac-first-boot.service"):
            self.write(rel, "fixture package-owned input\n")
        (self.root / "usr/share/omarchy").mkdir(parents=True, exist_ok=True)
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if "/bin/bash" in argv:
                self.assertFalse((self.root / "var/lib/omarchy/mac-first-boot/deferred-steps").exists())
                self.assertIn(["arch-chroot", str(self.root), "mkinitcpio", "-P"], calls)
                self.assertIn("-u", argv)
                self.assertIn("OMARCHY_MAC_IMAGE_BUILD", argv)
                script = kwargs['input']
                self.assertLess(script.index('grub-console.sh'), script.index('install -Dm644 /dev/null'))
                self.assertLess(script.index('install -Dm644 /dev/null'), script.index('limine-boot.sh'))
                if fail_activation:
                    raise subprocess.CalledProcessError(1, argv)
                self.write("var/lib/omarchy/limine.enabled", "")
            return subprocess.CompletedProcess(argv, 0)
        def output(argv, **kwargs):
            if argv[-1] == "/usr/bin/omarchy-mac-kernel":
                return "linux-asahi\n"
            if "pacman" in argv:
                return "limine\n" if argv[-1].startswith("/usr/share/limine/") else "omarchy-mac-boot\n"
            if "--verbose" in argv:
                return "\n".join(
                    [f"lrwxrwxrwx 1 root root 31 Jan 1 2026 {name} -> {target}" for name, target in boot.INITRD_LINKS.items()]
                    + [f"-rwxr-xr-x 1 root root 1 Jan 1 2026 {name}" for name in boot.INITRD_EXECUTABLES])
            members = [*boot.INITRD_FILES, *boot.INITRD_LINKS]
            return "\n".join(members[1:] if missing_hook else members)
        with contextlib.ExitStack() as stack:
            for method, value in (("_boot_intent", {"esp_mount": "/boot/efi", "register_firmware": False}),
                                  ("_stage_asahi_kernel_preset", ("linux-asahi", self.root / "boot/vmlinuz-linux-asahi")),
                                  ("_target_platform_override", contextlib.nullcontext()),
                                  ("_btrfs_root_device", "/dev/loop7"), ("_blkid_uuid", self.uuid)):
                stack.enter_context(mock.patch.object(shared, method, return_value=value))
            stack.enter_context(mock.patch.object(boot.subprocess, "run", side_effect=run))
            stack.enter_context(mock.patch.object(boot.subprocess, "check_output", side_effect=output))
            boot.finalize(types.SimpleNamespace(target=self.root))
        return calls

    def test_finalizer_arms_real_contract_only_after_mkinitcpio_and_activation(self):
        self.run_finalizer()
        self.assertEqual((self.root / "var/lib/omarchy/mac-first-boot/deferred-steps").read_text(), boot.DEFERRED_STEP)
        self.assertTrue((self.root / "var/lib/omarchy/mac-first-boot/pending").is_file())
        self.assertEqual(json.loads((self.root / "usr/share/omarchy/apple-boot-profile.json").read_text()), boot.PROFILE)
        self.check()

    def test_failed_activation_cannot_arm_fresh_conversion(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_finalizer(fail_activation=True)
        self.assertFalse((self.root / "var/lib/omarchy/mac-first-boot/pending").exists())

    def test_missing_conversion_hook_cannot_arm_fresh_conversion(self):
        with self.assertRaisesRegex(RuntimeError, "initramfs lacks"):
            self.run_finalizer(missing_hook=True)
        self.assertFalse((self.root / "var/lib/omarchy/mac-first-boot/pending").exists())

    def test_initrd_enabled_links_are_checked_using_real_cpio_listing(self):
        tree = Path(self.tmp.name) / "initrd"
        tree.mkdir()
        for name in boot.INITRD_FILES:
            p = tree / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("fixture unit or binary\n")
            if name in boot.INITRD_EXECUTABLES:
                p.chmod(0o755)
        for name, target in boot.INITRD_LINKS.items():
            p = tree / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.symlink_to(target)
        archive = Path(self.tmp.name) / "initrd.cpio"
        original = subprocess.check_output
        def output(argv, **kwargs):
            # Inspect only this fixture archive, without a chroot or mounts.
            self.assertEqual(argv[:2], ["arch-chroot", str(self.root)])
            return original(argv[2:], **kwargs)
        def check():
            subprocess.run(["bsdtar", "--format=newc", "-cf", str(archive), "-C", str(tree), "usr"], check=True)
            with mock.patch.object(boot.subprocess, "check_output", side_effect=output):
                boot.validate_initramfs(self.root, str(archive))
        # The pinned systemd generates cryptsetup instance units; a static
        # template is absent, while its generator and executable must work.
        self.assertFalse((tree / "usr/lib/systemd/system/systemd-cryptsetup@.service").exists())
        check()
        for name in boot.INITRD_EXECUTABLES:
            binary = tree / name
            contents = binary.read_bytes()
            binary.unlink()
            with self.assertRaisesRegex(RuntimeError, "initramfs lacks"):
                check()
            binary.write_bytes(contents)
            binary.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "cryptsetup binary is not executable"):
                check()
            binary.chmod(0o755)
        check()
        link = tree / next(iter(boot.INITRD_LINKS))
        link.unlink()
        with self.assertRaisesRegex(RuntimeError, "initramfs lacks"):
            check()
        link.write_text("a regular file cannot activate a unit")
        with self.assertRaisesRegex(RuntimeError, "activation link"):
            check()
        link.unlink()
        link.symlink_to("../unrelated.service")
        with self.assertRaisesRegex(RuntimeError, "activation link"):
            check()

    def test_effective_limine_override_and_helper_ownership_are_required(self):
        paths = {
            "etc/pacman.d/hooks/90-mkinitcpio-install.hook": ("limine-mkinitcpio-hook", "Target = usr/lib/modules/*/pkgbase\nWhen = PostTransaction\nExec = /usr/share/libalpm/scripts/limine-mkinitcpio-install\nNeedsTargets\n"),
            "etc/pacman.d/hooks/81-omarchy-mac-limine-deploy.hook": (None, "Target = usr/share/limine/BOOTAA64.EFI\nWhen = PostTransaction\nExec = /usr/bin/omarchy-mac-limine-deploy\n"),
            "usr/share/libalpm/hooks/91-omarchy-mac-boot-initramfs.hook": ("omarchy-mac-boot", "Target = usr/lib/omarchy/initcpio/*\nExec = /usr/share/libalpm/scripts/mkinitcpio install\nNeedsTargets\n"),
            "usr/share/libalpm/scripts/limine-mkinitcpio-install": ("limine-mkinitcpio-hook", "fixture executable"),
            "usr/bin/limine-update": ("limine-mkinitcpio-hook", "fixture executable"),
            "usr/lib/omarchy/mac-boot/limine-ready": ("omarchy-mac-boot", "fixture executable"),
            "etc/boot/hooks/pre.d/05-omarchy-mac-limine-gate": ("omarchy-mac-boot", "fixture executable"),
            "usr/bin/omarchy-mac-limine-deploy": ("omarchy", "fixture executable"),
        }
        for rel, (_, data) in paths.items():
            self.write(rel, data).chmod(0o755)
        owners = {"/" + rel: owner for rel, (owner, _) in paths.items()}
        with mock.patch.object(boot.subprocess, "check_output", side_effect=lambda argv, **kw: owners[argv[-1]]):
            boot.validate_maintenance(self.root)
            override = self.root / "etc/pacman.d/hooks/90-mkinitcpio-install.hook"
            content = override.read_text()
            override.unlink()
            with self.assertRaisesRegex(RuntimeError, "regular file"):
                boot.validate_maintenance(self.root)
            override.write_text(content.replace("limine-mkinitcpio-install", "mkinitcpio install"))
            with self.assertRaisesRegex(RuntimeError, "maintenance hook"):
                boot.validate_maintenance(self.root)
            override.write_text(content)
            owners["/usr/bin/limine-update"] = "another-package"
            with self.assertRaisesRegex(RuntimeError, "not owned"):
                boot.validate_maintenance(self.root)

    def test_factory_scrubs_mounted_log_subvolume_before_snapshot(self):
        self.seed_private_state()
        state = Path(self.tmp.name) / "state"
        top = state / "factory-top"
        source = top / "@"
        # A top-level view of @ does not expose the separately mounted @log.
        (source / "etc").mkdir(parents=True)
        ctx = types.SimpleNamespace(target=self.root, state_dir=state)
        def run(argv, **kwargs):
            if argv[:3] == ["btrfs", "subvolume", "snapshot"]:
                self.assertFalse((self.root / "var/log/journal").exists())
                self.assertFalse((self.root / "etc/pacman.d/gnupg").exists())
                shutil.copytree(source, top / "@factory")
            return subprocess.CompletedProcess(argv, 0)
        with mock.patch.object(boot, "selected", return_value=True), \
             mock.patch.object(finalized_phases, "_findmnt_value", side_effect=lambda p, field: {
                 "FSTYPE": "btrfs", "OPTIONS": "rw,subvol=/@", "SOURCE": "/dev/loop7[/@]"}[field]), \
             mock.patch.object(finalized_phases.subprocess, "run", side_effect=run):
            finalized_phases.create_factory_snapshot(ctx)
        self.assertEqual((top / "@factory/etc/machine-id").read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
