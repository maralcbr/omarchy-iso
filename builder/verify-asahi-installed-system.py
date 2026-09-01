#!/usr/bin/env python3
"""Validate the installed-system configuration of a built Apple Silicon OS.

Runs against the finalized root tree (and optionally the boot tree) while the
images are mounted during the build, or against any extracted copy of them.
Every check here corresponds to a way a built image installed fine but broke
on first use:

- /etc/pacman.conf missing [asahi-alarm] broke omarchy-update-asahi-bundle;
- speakersafetyd.service left disabled keeps the built-in speakers silent;
- bluetooth.service left disabled leaves Bluetooth off;
- a builder-local GRUB root selector failed Switch Root on first boot.

Emits canonical JSON evidence on stdout and exits non-zero when any check
fails. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EXPECTED_ROOT_UUID = "4f4d5801-524f-4f54-8000-000000000001"
REQUIRED_PACMAN_SECTIONS = ("omarchy", "asahi-alarm", "core", "extra", "alarm", "aur")
REQUIRED_PACKAGES = (
    "linux-asahi",
    "asahi-fwextract",
    "asahi-desktop-meta",
    "vulkan-asahi",
    "speakersafetyd",
    "alsa-ucm-conf-asahi",
    "iwd",
    "networkmanager",
    "bluez",
    "wireplumber",
)
REQUIRED_MULTI_USER_UNITS = (
    "NetworkManager.service",
    "omarchy-vendor-firmware.service",
    "speakersafetyd.service",
)


class Verification:
    def __init__(self) -> None:
        self.checks: dict[str, dict[str, str]] = {}

    def record(self, identifier: str, passed: bool, detail: str) -> None:
        if identifier in self.checks:
            raise RuntimeError(f"duplicate check identifier: {identifier}")
        self.checks[identifier] = {
            "result": "passed" if passed else "failed",
            "detail": detail,
        }

    @property
    def failed(self) -> list[str]:
        return sorted(
            identifier
            for identifier, check in self.checks.items()
            if check["result"] != "passed"
        )


def parse_pacman_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        match = re.fullmatch(r"\[([^]]+)\]", stripped)
        if match:
            current = sections.setdefault(match.group(1), [])
            continue
        if current is not None and stripped:
            current.append(stripped)
    return sections


def check_pacman(verification: Verification, root: Path) -> None:
    config = root / "etc/pacman.conf"
    if not config.is_file():
        verification.record("pacman-conf-present", False, "/etc/pacman.conf is missing")
        return
    verification.record("pacman-conf-present", True, "/etc/pacman.conf exists")
    text = config.read_text(errors="replace")
    sections = parse_pacman_sections(text)

    missing = [name for name in REQUIRED_PACMAN_SECTIONS if name not in sections]
    verification.record(
        "pacman-required-repositories",
        not missing,
        "all required repositories configured"
        if not missing
        else "missing repositories: " + ", ".join(missing),
    )

    asahi_servers = [
        line for line in sections.get("asahi-alarm", []) if line.startswith("Server =")
    ]
    verification.record(
        "pacman-asahi-alarm-server",
        any("https://github.com/asahi-alarm/" in line for line in asahi_servers),
        asahi_servers[0] if asahi_servers else "no Server entry under [asahi-alarm]",
    )

    build_only = "arm-snapshots" in sections or re.search(
        r"^Server = file://", text, flags=re.MULTILINE
    )
    verification.record(
        "pacman-no-build-only-paths",
        not build_only,
        "no build-only repositories or file:// servers"
        if not build_only
        else "build-only repository or file:// server leaked into the image",
    )

    architecture = re.search(r"^Architecture = (\S+)$", text, flags=re.MULTILINE)
    verification.record(
        "pacman-architecture",
        bool(architecture) and architecture.group(1) == "aarch64",
        architecture.group(0) if architecture else "no Architecture line",
    )


def check_network(verification: Verification, root: Path) -> None:
    backend = root / "etc/NetworkManager/conf.d/wifi_backend.conf"
    content = backend.read_text(errors="replace") if backend.is_file() else ""
    verification.record(
        "network-wifi-backend-iwd",
        "wifi.backend=iwd" in content,
        "NetworkManager is configured with wifi.backend=iwd"
        if "wifi.backend=iwd" in content
        else "wifi_backend.conf missing or not configured for iwd",
    )


def check_enabled_units(verification: Verification, root: Path) -> None:
    system = root / "etc/systemd/system"
    wants = system / "multi-user.target.wants"
    for unit in REQUIRED_MULTI_USER_UNITS:
        link = wants / unit
        verification.record(
            f"unit-enabled-{unit.removesuffix('.service')}",
            link.is_symlink() or link.is_file(),
            f"{unit} enabled in multi-user.target"
            if link.is_symlink() or link.is_file()
            else f"{unit} is not enabled",
        )

    bluetooth_alias = system / "dbus-org.bluez.service"
    bluetooth_wants = system / "bluetooth.target.wants/bluetooth.service"
    bluetooth_enabled = (
        bluetooth_alias.is_symlink()
        or bluetooth_wants.is_symlink()
        or bluetooth_wants.is_file()
    )
    verification.record(
        "unit-enabled-bluetooth",
        bluetooth_enabled,
        "bluetooth.service is enabled"
        if bluetooth_enabled
        else "bluetooth.service is not enabled",
    )

    display_manager = system / "display-manager.service"
    target = display_manager.readlink().name if display_manager.is_symlink() else ""
    verification.record(
        "unit-display-manager-sddm",
        target == "sddm.service",
        f"display-manager.service -> {target}"
        if target
        else "display-manager.service symlink is missing",
    )

    firmware_unit = system / "omarchy-vendor-firmware.service"
    firmware_text = (
        firmware_unit.read_text(errors="replace") if firmware_unit.is_file() else ""
    )
    verification.record(
        "unit-vendor-firmware-content",
        "/boot/efi/vendorfw/firmware.tar" in firmware_text,
        "vendor firmware unit extracts the ESP firmware archive"
        if "/boot/efi/vendorfw/firmware.tar" in firmware_text
        else "omarchy-vendor-firmware.service is missing or does not reference vendorfw",
    )


def installed_package_names(root: Path) -> set[str]:
    database = root / "var/lib/pacman/local"
    names: set[str] = set()
    if not database.is_dir():
        return names
    for entry in database.iterdir():
        match = re.fullmatch(r"(.+)-[^-]+-[^-]+", entry.name)
        if match:
            names.add(match.group(1))
    return names


def check_packages(verification: Verification, root: Path) -> None:
    installed = installed_package_names(root)
    missing = [name for name in REQUIRED_PACKAGES if name not in installed]
    verification.record(
        "packages-required-present",
        not missing,
        f"all {len(REQUIRED_PACKAGES)} required packages installed"
        if not missing
        else "missing packages: " + ", ".join(missing),
    )


def check_identity(verification: Verification, root: Path) -> None:
    version_file = root / "usr/share/omarchy/version"
    version = (
        version_file.read_text(errors="replace").strip()
        if version_file.is_file()
        else ""
    )
    verification.record(
        "omarchy-version",
        version.startswith("4."),
        f"Omarchy version {version}" if version else "version file is missing",
    )

    marker = root / "usr/share/omarchy/apple-silicon-full-os"
    content = marker.read_text(errors="replace") if marker.is_file() else ""
    verification.record(
        "full-os-marker",
        "mode=installed-full-os" in content,
        "full-OS marker present"
        if "mode=installed-full-os" in content
        else "apple-silicon-full-os marker is missing or wrong",
    )


def check_boot(verification: Verification, boot: Path, expected_root_uuid: str) -> None:
    grub_config = boot / "grub/grub.cfg"
    if not grub_config.is_file():
        verification.record("boot-grub-config-present", False, "grub/grub.cfg is missing")
        return
    verification.record("boot-grub-config-present", True, "grub/grub.cfg exists")
    text = grub_config.read_text(errors="replace")

    linux_lines = [
        line.strip()
        for line in text.splitlines()
        if re.match(r"\s*linux\s", line)
    ]
    expected_selector = f"root=UUID={expected_root_uuid}"
    selector_ok = bool(linux_lines) and all(
        expected_selector in line and "rootflags=subvol=@" in line
        for line in linux_lines
    )
    verification.record(
        "boot-grub-root-selector",
        selector_ok,
        f"every kernel line selects {expected_selector} with rootflags=subvol=@"
        if selector_ok
        else "kernel line with wrong or missing root selector: "
        + (linux_lines[0] if linux_lines else "no linux lines at all"),
    )

    builder_local = [
        line for line in linux_lines if re.search(r"root=/(?!dev/disk/by-uuid/)", line)
    ]
    verification.record(
        "boot-grub-no-builder-root",
        not builder_local,
        "no builder-local root path on any kernel line"
        if not builder_local
        else "builder-local root path leaked: " + builder_local[0],
    )

    for name, identifier in (
        ("vmlinuz-linux-asahi", "boot-kernel-present"),
        ("initramfs-linux-asahi.img", "boot-initramfs-present"),
    ):
        path = boot / name
        present = path.is_file() and path.stat().st_size > 0
        verification.record(
            identifier,
            present,
            f"{name} present ({path.stat().st_size} bytes)"
            if present
            else f"{name} is missing or empty",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-tree", required=True, type=Path)
    parser.add_argument("--boot-tree", type=Path)
    parser.add_argument("--expected-root-uuid", default=EXPECTED_ROOT_UUID)
    arguments = parser.parse_args()

    if not arguments.root_tree.is_dir():
        print(f"ERROR: root tree is not a directory: {arguments.root_tree}", file=sys.stderr)
        return 2
    if arguments.boot_tree is not None and not arguments.boot_tree.is_dir():
        print(f"ERROR: boot tree is not a directory: {arguments.boot_tree}", file=sys.stderr)
        return 2

    verification = Verification()
    check_pacman(verification, arguments.root_tree)
    check_network(verification, arguments.root_tree)
    check_enabled_units(verification, arguments.root_tree)
    check_packages(verification, arguments.root_tree)
    check_identity(verification, arguments.root_tree)
    if arguments.boot_tree is not None:
        check_boot(verification, arguments.boot_tree, arguments.expected_root_uuid)

    evidence = {
        "schema_version": 1,
        "verification_kind": "asahi-installed-system-config-v1",
        "root_tree": str(arguments.root_tree),
        "boot_tree": str(arguments.boot_tree) if arguments.boot_tree else None,
        "checks": verification.checks,
        "failed_checks": verification.failed,
        "result": "failed" if verification.failed else "passed",
    }
    json.dump(evidence, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")

    if verification.failed:
        print(
            "ERROR: installed-system validation failed: "
            + ", ".join(verification.failed),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
