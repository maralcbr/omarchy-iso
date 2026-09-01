from __future__ import annotations

import importlib.util
import hashlib
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "builder/capture-asahi-os-package-contents.py"
SPEC = importlib.util.spec_from_file_location("asahi_contents", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

ROOT_UUID = "4f4d5801-524f-4f54-8000-000000000001"
NODE_IDENTITY = {
    "schema_version": 1,
    "verification_kind": "pinned-node-lock-v1",
    "filename": "node-v1-linux-arm64.tar.gz",
    "sha256": hashlib.sha256(b"node").hexdigest(),
    "size_bytes": len(b"node"),
}


def _write_complete_target(target: Path, grub_config: bytes) -> None:
    files = {
        "boot/efi/m1n1/boot.bin": b"m1n1",
        "boot/efi/EFI/BOOT/BOOTAA64.EFI": b"grub",
        "boot/vmlinuz-linux-asahi": b"kernel",
        "boot/initramfs-linux-asahi.img": b"initramfs",
        "boot/grub/grub.cfg": grub_config,
        "usr/bin/omarchy": b"#!/bin/sh\n",
        "usr/bin/omarchy-provision-owner": b"#!/bin/sh\n",
        "usr/bin/omarchy-apple-installed-verify": b"#!/bin/bash\n",
        "usr/share/omarchy/apple-silicon-full-os": (
            b"schema_version=1\n"
            b"product_id=omarchy-mx-mac\n"
            b"mode=installed-full-os\n"
        ),
        "usr/bin/fsck.btrfs": b"#!/bin/sh\n",
        "usr/share/omarchy/install/hardware/apple/fix-spi-keyboard.sh": (
            b'product_name="$(cat /sys/class/dmi/id/product_name '
            b'2>/dev/null || true)"\n'
        ),
        "etc/fstab": (
            f"UUID={ROOT_UUID} / btrfs defaults,subvol=@ 0 0\n".encode()
        ),
        "etc/locale.conf": b"LANG=en_US.UTF-8\n",
        "usr/lib/locale/locale-archive": b"compiled-locale",
        "etc/systemd/system/omarchy-provision-owner.service": b"[Service]\n",
        "var/lib/omarchy/provisioning/pending": b"",
        "var/lib/omarchy/provisioning/packages/node-v1-linux-arm64.tar.gz": b"node",
        "usr/lib/modules/7.1.6-asahi/pkgbase": b"linux-asahi\n",
        "usr/lib/modules/7.1.6-asahi/vmlinuz": b"kernel",
        "var/lib/pacman/local/omarchy-1/desc": b"%NAME%\nomarchy\n",
    }
    for relative, data in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    enabled = (
        target
        / "etc/systemd/system/multi-user.target.wants/omarchy-provision-owner.service"
    )
    enabled.parent.mkdir(parents=True)
    enabled.symlink_to("/etc/systemd/system/omarchy-provision-owner.service")


class AsahiOSPackageContentsTests(unittest.TestCase):
    def test_complete_target_emits_boot_root_and_provisioning_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write_complete_target(
                target,
                (
                    "menuentry 'Omarchy' {\n"
                    "  linux /vmlinuz-linux-asahi "
                    f"root=UUID={ROOT_UUID} rootflags=subvol=@ rw rootfstype=btrfs\n"
                    "  initrd /initramfs-linux-asahi.img\n"
                    "}\n"
                ).encode(),
            )

            evidence = MODULE.capture(target, NODE_IDENTITY)

            self.assertEqual(evidence["content_kind"], "asahi-full-os-images")
            self.assertEqual(evidence["kernel"]["package"], "linux-asahi")
            self.assertEqual(
                evidence["boot_contract"]["root_selector"],
                f"UUID={ROOT_UUID}",
            )
            self.assertEqual(evidence["boot_contract"]["root_subvolume"], "@")
            self.assertEqual(evidence["package_database"]["installed_packages"], 1)
            self.assertTrue(evidence["provisioning"]["pending"])
            self.assertTrue(evidence["provisioning"]["service_enabled"])
            self.assertEqual(
                evidence["provisioning"]["node_archive_sha256"],
                NODE_IDENTITY["sha256"],
            )
            self.assertGreater(evidence["artifacts"]["boot_kernel"]["size_bytes"], 0)
            self.assertGreater(
                evidence["artifacts"]["root_legacy_apple_probe"]["size_bytes"],
                0,
            )
            self.assertGreater(
                evidence["artifacts"]["root_locale_archive"]["size_bytes"],
                0,
            )
            self.assertGreater(
                evidence["artifacts"]["root_btrfs_fsck"]["size_bytes"],
                0,
            )
            self.assertGreater(
                evidence["artifacts"]["root_installed_verify"]["size_bytes"],
                0,
            )
            self.assertGreater(
                evidence["artifacts"]["root_full_os_marker"]["size_bytes"],
                0,
            )

    def test_any_grub_linux_line_with_builder_local_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write_complete_target(
                target,
                (
                    "menuentry 'Omarchy' {\n"
                    "  linux /vmlinuz-linux-asahi "
                    f"root=UUID={ROOT_UUID} rootflags=subvol=@ rw rootfstype=btrfs\n"
                    "  initrd /initramfs-linux-asahi.img\n"
                    "}\n"
                    "menuentry 'Omarchy fallback' {\n"
                    "  linux /vmlinuz-linux-asahi "
                    "root=/var/cache/omarchy-asahi-package.TEST/root.img "
                    "rootflags=subvol=@ rw rootfstype=btrfs\n"
                    "  initrd /initramfs-linux-asahi.img\n"
                    "}\n"
                ).encode(),
            )

            with self.assertRaisesRegex(
                MODULE.ContentEvidenceError,
                "GRUB Linux root selector does not match installed root UUID",
            ):
                MODULE.capture(target, NODE_IDENTITY)

    def test_missing_boot_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                MODULE.ContentEvidenceError,
                "missing installed artifact",
            ):
                MODULE.capture(Path(tmp), NODE_IDENTITY)

    def test_installed_node_must_match_exact_lock_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write_complete_target(
                target,
                (
                    "menuentry 'Omarchy' {\n"
                    "  linux /vmlinuz-linux-asahi "
                    f"root=UUID={ROOT_UUID} rootflags=subvol=@ rw rootfstype=btrfs\n"
                    "  initrd /initramfs-linux-asahi.img\n"
                    "}\n"
                ).encode(),
            )
            stale_identity = NODE_IDENTITY | {"sha256": "0" * 64}
            with self.assertRaisesRegex(
                MODULE.ContentEvidenceError,
                "installed Node archive differs from the pinned lock",
            ):
                MODULE.capture(target, stale_identity)

    def test_installed_node_filename_and_size_must_match_lock_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _write_complete_target(
                target,
                (
                    "menuentry 'Omarchy' {\n"
                    "  linux /vmlinuz-linux-asahi "
                    f"root=UUID={ROOT_UUID} rootflags=subvol=@ rw rootfstype=btrfs\n"
                    "  initrd /initramfs-linux-asahi.img\n"
                    "}\n"
                ).encode(),
            )
            for changed_identity in (
                NODE_IDENTITY | {"filename": "node-v2-linux-arm64.tar.gz"},
                NODE_IDENTITY | {"size_bytes": NODE_IDENTITY["size_bytes"] + 1},
            ):
                with self.subTest(identity=changed_identity):
                    with self.assertRaises(MODULE.ContentEvidenceError):
                        MODULE.capture(target, changed_identity)

    def test_extra_node_archive_of_any_name_or_architecture_is_rejected(self) -> None:
        for extra_name in (
            "node-v1-linux-x64.tar.gz",
            "node-stale-offline-copy.tar.xz",
        ):
            with self.subTest(extra_name=extra_name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp)
                _write_complete_target(
                    target,
                    (
                        "menuentry 'Omarchy' {\n"
                        "  linux /vmlinuz-linux-asahi "
                        f"root=UUID={ROOT_UUID} rootflags=subvol=@ rw rootfstype=btrfs\n"
                        "  initrd /initramfs-linux-asahi.img\n"
                        "}\n"
                    ).encode(),
                )
                extra = target / "var/lib/omarchy/provisioning/packages" / extra_name
                extra.write_bytes(b"unprojected-node-archive")
                with self.assertRaisesRegex(
                    MODULE.ContentEvidenceError,
                    "Node archive inventory is not exact",
                ):
                    MODULE.capture(target, NODE_IDENTITY)


if __name__ == "__main__":
    unittest.main()
