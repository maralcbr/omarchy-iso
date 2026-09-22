"""Opt-in Apple image finalization; the generic ARM Limine path is separate.

Only the finalized runtime projection may select this profile. The package
admission guard remains responsible for withholding unqualified schema-4 media.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import tempfile

PROFILE = {"schema": 1, "boot_profile": "limine", "candidate_schema": 4}
DEFERRED_STEP = "install/hardware/apple/limine-boot.sh\n"
# This is an image placeholder, never the builder host's identity. The first
# boot creates its own machine-id and the runtime discards the placeholder menu.
IMAGE_MACHINE_ID = "00000000000000000000000000000001"
# Current systemd generates cryptsetup instance units at boot; no static
# systemd-cryptsetup@.service template ships in the pinned systemd package.
# Require the generator, its actual executable target and activation target.
INITRD_EXECUTABLES = (
    "usr/lib/systemd/system-generators/systemd-cryptsetup-generator",
    "usr/bin/systemd-cryptsetup",
)
INITRD_FILES = (
    *INITRD_EXECUTABLES,
    "usr/lib/omarchy/initcpio/omarchy-mac-encrypt",
    "usr/lib/systemd/system/omarchy-mac-encrypt.service",
    "usr/lib/systemd/system/cryptsetup.target",
    r"usr/lib/systemd/system/run-systemd-cryptsetup-keydev\x2droot.mount.d/omarchy-mac-encrypt.conf",
    "usr/lib/omarchy/initcpio/omarchy-vendorfw-initrd.sh",
    "usr/lib/systemd/system/omarchy-vendorfw-initrd.service",
    "usr/lib/systemd/system/omarchy-vendorfw.service",
    "usr/lib/systemd/system/systemd-cryptsetup@.service.d/omarchy-vendorfw-initrd.conf",
)
INITRD_LINKS = {
    "usr/lib/systemd/system/initrd-root-device.target.requires/omarchy-mac-encrypt.service": "../omarchy-mac-encrypt.service",
    "usr/lib/systemd/system/initrd.target.wants/omarchy-vendorfw.service": "../omarchy-vendorfw.service",
    "usr/lib/systemd/system/cryptsetup-pre.target.wants/omarchy-vendorfw-initrd.service": "../omarchy-vendorfw-initrd.service",
    "usr/lib/systemd/system/initrd-root-device.target.wants/omarchy-vendorfw-initrd.service": "../omarchy-vendorfw-initrd.service",
    "usr/lib/systemd/system/sysinit.target.wants/omarchy-vendorfw-initrd.service": "../omarchy-vendorfw-initrd.service",
}


def selected(ctx) -> bool:
    from .configured_phases import _boot_backend

    media = Path(os.environ.get("OMARCHY_ISO_MEDIA_ROOT", "/usr/share/omarchy-iso"))
    path = media / "apple-boot-profile.json"
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file() or json.loads(path.read_text()) != PROFILE:
        raise RuntimeError("invalid Apple boot profile")
    if _boot_backend(ctx) != "asahi-grub" or not ctx.defer_provisioning or ctx.encrypt:
        raise RuntimeError("Apple Limine profile requires a plain, deferred-owner Apple image")
    return True


def regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"boot input is not a regular file: {path}")
    data = path.read_bytes()
    if not data:
        raise RuntimeError(f"boot input is empty: {path}")
    return data


def pe_sections(path: Path) -> dict[str, bytes]:
    """Read bounded PE32+ ARM64 sections without executing artifact tools."""
    data = regular(path)
    if len(data) < 64 or data[:2] != b"MZ":
        raise RuntimeError(f"not a PE image: {path}")
    offset = struct.unpack_from("<I", data, 60)[0]
    if offset + 24 > len(data) or data[offset:offset + 4] != b"PE\0\0":
        raise RuntimeError(f"invalid PE header: {path}")
    machine, count = struct.unpack_from("<HH", data, offset + 4)
    optional_size = struct.unpack_from("<H", data, offset + 20)[0]
    table = offset + 24 + optional_size
    if (machine != 0xAA64 or not 1 <= count <= 96 or optional_size < 112
            or table + count * 40 > len(data)
            or struct.unpack_from("<H", data, offset + 24)[0] != 0x20B):
        raise RuntimeError(f"not a bounded ARM64 PE32+ image: {path}")
    sections = {}
    for index in range(count):
        entry = table + 40 * index
        name = data[entry:entry + 8].rstrip(b"\0").decode("ascii")
        size, _, raw_size, raw_offset = struct.unpack_from("<IIII", data, entry + 8)
        if name in sections or raw_offset + raw_size > len(data):
            raise RuntimeError(f"invalid PE section: {path}: {name}")
        # BSS has no file bytes. Required UKI sections below must be populated.
        if raw_size and raw_offset < table + count * 40:
            raise RuntimeError(f"truncated PE section: {path}: {name}")
        if name in (".linux", ".initrd", ".osrel", ".cmdline", ".uname") and size > raw_size:
            raise RuntimeError(f"truncated UKI section: {path}: {name}")
        sections[name] = data[raw_offset:raw_offset + min(size, raw_size)] if raw_size else b""
    return sections


def kernel_cmdline(root: Path) -> str:
    text = regular(root / "etc/default/limine").decode()
    lines = re.findall(r'^KERNEL_CMDLINE\[default\]="([^"\n]*)"$', text, re.M)
    if len(lines) != 1:
        raise RuntimeError("expected one generated Limine kernel command line")
    cmdline = lines[0]
    words = shlex.split(cmdline)
    roots = [word for word in words if word.startswith("root=")]
    flags = [word for word in words if word.startswith("rootflags=")]
    if (len(roots) != 1 or not re.fullmatch(r"root=UUID=[0-9a-fA-F-]{36}", roots[0])
            or len(flags) != 1 or "subvol=@" not in flags[0][10:].split(",")
            or "rw" not in words or "rootfstype=btrfs" not in words
            or any(word.startswith(("rd.luks.", "cryptdevice=", "cryptkey=")) for word in words)):
        raise RuntimeError("fresh image has an unsafe or encrypted kernel command line")
    rows = [line.split() for line in regular(root / "etc/fstab").decode().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    root_rows = [row for row in rows if len(row) >= 4 and row[1] == "/"]
    if len(root_rows) != 1 or roots[0][5:] != root_rows[0][0] or root_rows[0][2] != "btrfs":
        raise RuntimeError("UKI root does not match the target fstab")
    return cmdline


def validate_artifacts(root: Path, kernel: str) -> None:
    if kernel not in ("linux-asahi", "linux-aurora"):
        raise RuntimeError("unsupported Apple kernel")
    esp = root / "boot/efi"
    packaged = root / "usr/share/limine/BOOTAA64.EFI"
    loader = esp / "EFI/BOOT/BOOTAA64.EFI"
    if regular(packaged) != regular(loader):
        raise RuntimeError("ESP loader differs from the installed Limine package")
    pe_sections(loader)
    regular(esp / "m1n1/boot.bin")
    uki_path = esp / f"EFI/Linux/omarchy_{kernel}.efi"
    sections = pe_sections(uki_path)
    for section, path in ((".linux", root / f"boot/vmlinuz-{kernel}"),
                          (".initrd", root / f"boot/initramfs-{kernel}.img")):
        if sections.get(section) != regular(path):
            raise RuntimeError(f"UKI {section} differs from finalized {path.name}")
    modules = [path.parent for path in (root / "usr/lib/modules").glob("*/pkgbase")
               if regular(path).decode().strip() == kernel]
    if len(modules) != 1 or not re.fullmatch(r"[A-Za-z0-9._+-]+", modules[0].name):
        raise RuntimeError("UKI requires one installed kernel release")
    release = modules[0].name.encode()
    if (sections.get(".uname") != release
            or regular(modules[0] / "vmlinuz") != sections[".linux"]):
        raise RuntimeError("UKI release differs from the installed kernel")
    # os-release may be the distro's intentional symlink into /usr/lib.
    os_release = root / "etc/os-release"
    if os_release.is_symlink():
        link = os.readlink(os_release)
        os_release = root / link.lstrip("/") if link.startswith("/") else os_release.parent / link
        if root.resolve() not in os_release.resolve().parents:
            raise RuntimeError("os-release escapes the image")
    # The pinned mkinitcpio default-preset writer prepends the selected kernel
    # release and removes the distro VERSION_ID. Retain every other byte.
    expected_release = b"VERSION_ID=" + release + b"\n" + b"".join(
        line for line in regular(os_release).splitlines(keepends=True)
        if not line.startswith(b"VERSION_ID="))
    if sections.get(".osrel") != expected_release:
        raise RuntimeError("UKI os-release differs from installed release")
    expected_cmdline = kernel_cmdline(root)
    # mkinitcpio translates the single input line's newline to a space, then
    # appends its own newline and NUL. Do not discard arbitrary whitespace.
    if sections.get(".cmdline") != expected_cmdline.encode() + b" \n\0":
        raise RuntimeError("UKI command line differs from finalized defaults")
    menu = regular(esp / "limine.conf").decode()
    entry = re.search(r"^\s*//" + re.escape(kernel) + r"\s*\n((?:(?!\s*/).*(?:\n|$))*)", menu, re.M)
    path_match = re.search(r"^\s*path: boot\(\):/EFI/Linux/omarchy_" + re.escape(kernel)
                          + r"\.efi(?:#([a-fA-F0-9]{128}))?\s*$", entry[1], re.M) if entry else None
    if ("interface_branding: Omarchy Bootloader" not in menu or not entry
            or not re.search(r"^\s*protocol: efi\s*$", entry[1], re.M) or not path_match):
        raise RuntimeError("Limine menu does not boot the finalized UKI")
    if path_match[1] and path_match[1].lower() != hashlib.blake2b(regular(uki_path)).hexdigest():
        raise RuntimeError("Limine menu UKI verification hash differs")
    overrides = re.findall(r"^\s*cmdline:\s*(.*)$", entry[1], re.M)
    if overrides and overrides != [expected_cmdline]:
        raise RuntimeError("Limine menu overrides the verified UKI command line")
    staging = esp / "omarchy"
    if staging.is_symlink() or not staging.is_dir() or any(staging.iterdir()):
        raise RuntimeError("installer ESP staging must be an empty directory")


ACTIVATE = r'''set -euo pipefail
root_device=${1:?}
root_uuid=${2:?}
[[ $root_device =~ ^/dev/loop[0-9]+$ && -b $root_device ]]
[[ $root_uuid =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]
link=/dev/disk/by-uuid/$root_uuid
created=false
for directory in /dev/disk /dev/disk/by-uuid; do
  [[ ! -L $directory && ( ! -e $directory || -d $directory ) ]]
  mkdir -p "$directory"
done
if [[ -e $link || -L $link ]]; then
  [[ -L $link && $(readlink -f "$link") == "$(readlink -f "$root_device")" ]]
else
  ln -s "$root_device" "$link"
  created=true
fi
cleanup() {
  local status=$?
  trap - EXIT
  if [[ $created == true ]]; then
    [[ -L $link && $(readlink -f "$link") == "$(readlink -f "$root_device")" ]] || exit 1
    rm "$link"
  fi
  exit "$status"
}
trap cleanup EXIT
source "$OMARCHY_PATH/install/hardware/apple/grub-console.sh"
install -Dm644 /dev/null /var/lib/omarchy/limine.enabled
source "$OMARCHY_PATH/install/hardware/apple/limine-boot.sh"
'''


def validate_initramfs(root: Path, path: str) -> None:
    listing = subprocess.check_output(["arch-chroot", str(root), "lsinitcpio", path], text=True)
    members = {line.strip().lstrip("./") for line in listing.splitlines()}
    for required in (*INITRD_FILES, *INITRD_LINKS):
        if required not in members:
            raise RuntimeError(f"final initramfs lacks {required}")
    verbose = subprocess.check_output(
        ["arch-chroot", str(root), "env", "LC_ALL=C", "lsinitcpio", "--nocolor", "--verbose", path], text=True)
    for name in INITRD_EXECUTABLES:
        if not re.search(r"^-..x[^\n]*\s(?:\./)?" + re.escape(name) + r"$", verbose, re.M):
            raise RuntimeError(f"final initramfs cryptsetup binary is not executable: {name}")
    for name, target in INITRD_LINKS.items():
        if not re.search(r"^l[^\n]*\s(?:\./)?" + re.escape(name) + r" -> " + re.escape(target) + r"$", verbose, re.M):
            raise RuntimeError(f"final initramfs activation link is invalid: {name}")


def require_owner(root: Path, rel: str, owner: str) -> None:
    actual = subprocess.check_output(
        ["arch-chroot", str(root), "pacman", "-Qqo", "--", "/" + rel], text=True).strip()
    if actual != owner:
        raise RuntimeError(f"boot input {rel} is not owned by {owner}")


def validate_maintenance(root: Path) -> None:
    contracts = {
        "etc/pacman.d/hooks/90-mkinitcpio-install.hook": (
            "limine-mkinitcpio-hook", ("Target = usr/lib/modules/*/pkgbase", "When = PostTransaction",
                                     "Exec = /usr/share/libalpm/scripts/limine-mkinitcpio-install", "NeedsTargets")),
        "etc/pacman.d/hooks/81-omarchy-mac-limine-deploy.hook": (
            None, ("Target = usr/share/limine/BOOTAA64.EFI", "When = PostTransaction",
                   "Exec = /usr/bin/omarchy-mac-limine-deploy")),
        "usr/share/libalpm/hooks/91-omarchy-mac-boot-initramfs.hook": (
            "omarchy-mac-boot", ("Target = usr/lib/omarchy/initcpio/*",
                                 "Exec = /usr/share/libalpm/scripts/mkinitcpio install", "NeedsTargets")),
    }
    for rel, (owner, required) in contracts.items():
        lines = {line.strip() for line in regular(root / rel).decode().splitlines()}
        if not set(required) <= lines:
            raise RuntimeError(f"boot maintenance hook is incomplete: {rel}")
        if owner:
            require_owner(root, rel, owner)
    for rel, owner in (
        ("usr/share/libalpm/scripts/limine-mkinitcpio-install", "limine-mkinitcpio-hook"),
        ("usr/bin/limine-update", "limine-mkinitcpio-hook"),
        ("usr/lib/omarchy/mac-boot/limine-ready", "omarchy-mac-boot"),
        ("etc/boot/hooks/pre.d/05-omarchy-mac-limine-gate", "omarchy-mac-boot"),
        ("usr/bin/omarchy-mac-limine-deploy", "omarchy"),
    ):
        regular(root / rel)
        if not os.access(root / rel, os.X_OK):
            raise RuntimeError(f"boot maintenance helper is not executable: {rel}")
        require_owner(root, rel, owner)


def retain_uki_initramfs(root: Path, kernel: str) -> None:
    # limine-update invokes mkinitcpio a second time. Timestamps/compression
    # can differ from the earlier -P image even with identical hooks. Inspect
    # the actual embedded archive and retain it as the matching Boot fallback.
    payload = pe_sections(root / f"boot/efi/EFI/Linux/omarchy_{kernel}.efi").get(".initrd")
    if not payload:
        raise RuntimeError("UKI has no populated initramfs")
    with tempfile.NamedTemporaryFile(dir=root / "boot", prefix=".omarchy-uki-initrd-", delete=False) as stream:
        staged = Path(stream.name)
        stream.write(payload)
    try:
        validate_initramfs(root, "/boot/" + staged.name)
        staged.chmod(0o644)
        staged.replace(root / f"boot/initramfs-{kernel}.img")
    finally:
        staged.unlink(missing_ok=True)


def finalize(ctx) -> None:
    from . import configured_phases as shared
    from .asahi_boot import _configure_asahi_grub_defaults

    boot = shared._boot_intent(ctx)
    if boot.get("register_firmware") or boot.get("esp_mount") != "/boot/efi":
        raise RuntimeError("Apple image requires /boot/efi and no NVRAM writes")
    root = ctx.target
    # Require package-owned hooks. The legacy GRUB finalizer writes its own
    # older vendorfw copies and must not overwrite the new boot package.
    for rel in (
        "etc/mkinitcpio.conf.d/90-omarchy-mac.conf",
        "etc/mkinitcpio.conf.d/91-omarchy-mac-encrypt.conf",
        "etc/mkinitcpio.conf.d/92-omarchy-mac-hid.conf",
        "usr/lib/omarchy/mac-first-boot/omarchy-mac-first-boot",
        "usr/lib/omarchy/initcpio/omarchy-mac-encrypt",
        "usr/lib/systemd/system/omarchy-mac-first-boot.service",
        "usr/share/limine/BOOTAA64.EFI",
        "boot/efi/m1n1/boot.bin",
    ):
        regular(root / rel)
        if rel.startswith(("etc/", "usr/lib/")):
            require_owner(root, rel, "omarchy-mac-boot")
    require_owner(root, "usr/share/limine/BOOTAA64.EFI", "limine")
    if (root / "etc/mkinitcpio.conf.d/90-omarchy-asahi.conf").exists():
        raise RuntimeError("legacy image initramfs override survived boot package replacement")
    kernel, _ = shared._stage_asahi_kernel_preset(ctx)
    installed = subprocess.check_output(
        ["arch-chroot", str(root), "/usr/bin/omarchy-mac-kernel"], text=True).strip()
    if installed != kernel:
        raise RuntimeError("configured kernel differs from installed kernel package")
    _configure_asahi_grub_defaults(ctx)
    subprocess.run(["arch-chroot", str(root), "mkinitcpio", "-P"], check=True)
    # Verify the real archive includes the conversion unit, not merely a
    # drop-in mentioning its hook. Never accept a fixture-created marker alone.
    validate_initramfs(root, f"/boot/initramfs-{kernel}.img")
    (root / "etc/machine-id").write_text(IMAGE_MACHINE_ID + "\n")
    state = root / "var/lib/omarchy"
    state.mkdir(parents=True, exist_ok=True)
    if (state / "limine.enabled").exists() or (state / "limine.enabled").is_symlink():
        raise RuntimeError("fresh configured image already has Limine activation armed")
    staging = root / "boot/efi/omarchy"
    staging.mkdir(exist_ok=True)
    with shared._target_platform_override(ctx):
        subprocess.run([
            "arch-chroot", str(root), "env", "-u", "OMARCHY_MAC_IMAGE_BUILD",
            "OMARCHY_PATH=/usr/share/omarchy",
            "PATH=/usr/share/omarchy/bin:/usr/local/sbin:/usr/local/bin:/usr/bin",
            "BOOT_PART=/boot", "EFI_PART=/boot/efi",
            "/bin/bash", "-eE", "-s", "--", shared._btrfs_root_device(ctx),
            shared._blkid_uuid(shared._btrfs_root_device(ctx)),
        ], input=ACTIVATE, text=True, check=True)
    retain_uki_initramfs(root, kernel)
    validate_artifacts(root, kernel)
    # Only a completed fresh image earns permission to convert on first boot.
    first_boot = state / "mac-first-boot"
    first_boot.mkdir(parents=True, exist_ok=True)
    (first_boot / "deferred-steps").write_text(DEFERRED_STEP)
    (first_boot / "pending").write_text("")
    subprocess.run(["arch-chroot", str(root), "systemctl", "enable",
                    "omarchy-mac-first-boot.service"], check=True)
    (root / "usr/share/omarchy/apple-boot-profile.json").write_text(json.dumps(PROFILE, sort_keys=True) + "\n")


def validate(ctx) -> None:
    from .configured_phases import _storage_intent

    root = ctx.target
    validate_artifacts(root, _storage_intent(ctx).get("kernel") or "linux-asahi")
    validate_maintenance(root)
    if regular(root / "var/lib/omarchy/mac-first-boot/deferred-steps").decode() != DEFERRED_STEP:
        raise RuntimeError("fresh image deferred steps differ from the installed first-boot contract")
    for rel in ("var/lib/omarchy/mac-first-boot/pending", "var/lib/omarchy/limine.enabled",
                "var/lib/omarchy/provisioning/pending"):
        path = root / rel
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"fresh image marker missing: {rel}")
    for name in ("omarchy-mac-first-boot", "omarchy-provision-owner"):
        link = root / f"etc/systemd/system/multi-user.target.wants/{name}.service"
        if not link.is_symlink():
            raise RuntimeError(f"fresh image service is not enabled: {name}")


def remove(root: Path, rel: str) -> None:
    path = root / rel
    # Do not traverse a symlink supplied by a package while scrubbing secrets.
    if any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
        raise RuntimeError(f"unsafe image cleanup path: {path}")
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def scrub_identity(root: Path) -> None:
    for rel in ("etc/pacman.d/gnupg", "var/lib/dbus/machine-id",
                "var/lib/systemd/random-seed", "var/lib/systemd/credential.secret",
                "var/log/journal"):
        remove(root, rel)
    remove(root, "etc/machine-id")
    (root / "etc/machine-id").write_text("")
    for path in (root / "etc/ssh").glob("ssh_host_*"):
        remove(root, str(path.relative_to(root)))
    # This disposable-builder exception must not survive in @factory either.
    for path in [root / "etc/pacman.conf", *(root / "etc/pacman.conf.d").glob("*")]:
        if path.is_symlink():
            raise RuntimeError(f"unsafe image pacman configuration: {path}")
        if path.is_file():
            path.write_text("".join(line for line in path.read_text().splitlines(keepends=True)
                                    if line.strip() != "DisableSandbox"))


def scrub_factory(root: Path) -> None:
    scrub_identity(root)
    for rel in ("var/lib/omarchy/mac-first-boot", "var/lib/omarchy/provisioning/pending",
                "var/lib/omarchy/provisioning/luks-key", "var/lib/omarchy/provisioning/luks-rekey.done",
                "boot/omarchy/encrypt.state", "boot/omarchy/provisioning.key"):
        remove(root, rel)
