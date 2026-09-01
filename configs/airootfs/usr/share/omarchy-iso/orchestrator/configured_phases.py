"""Configured-target phases and implementation shared with finalization.

Phase ordering (full-disk and protected/pre-mounted):

    prepare_live           → disk cleanup when wiping, load configurator
                             handlers (archinstall patch happens in the
                             wrapper before Python imports it)
    prepare_install_target → verify pre-mounted target/ESP when the JSON uses
                             pre_mounted_config; no-op for full-disk installs
    arch_install_system    → one archinstall flow for partition/mount-or-use,
                             base install, early Omarchy packages, Limine setup,
                             useradd, runtime Omarchy packages, fstab
    configure_hibernation  → root-owned swap/resume drop-ins
    run_system_finalizer   → arch-chroot root omarchy-apply-system
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import textwrap
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from . import archinstall_adapter as arch
from .command import capture, capture_identifier, require_text
from .context import InstallContext
from .keyboard import configure_keyboard
from .target_packages import (
    BOOT_BACKENDS,
    DEFAULT_BOOT_BACKEND,
    EARLY_ASAHI_GRUB_BOOTSTRAP_PACKAGES,
    EARLY_BOOTSTRAP_BASE_PACKAGES,
    EARLY_LIMINE_BOOTSTRAP_PACKAGES,
    EARLY_LUAROCKS_PACKAGES,
    TAILSCALE_PACKAGES,
    early_bootstrap_packages,
    early_packages,
    early_user_seed_packages,
    read_package_list,
    runtime_package_list,
)
from .ui import error, info


# Package targets are written by builder/build-iso.sh. Stable ISOs use the
# stable package names, while dev/local-source ISOs install the dev package
# names explicitly instead of relying on provides=omarchy resolution.
def _media_root() -> Path:
    return Path(os.environ.get("OMARCHY_ISO_MEDIA_ROOT", "/usr/share/omarchy-iso"))


def _iso_ref() -> str:
    if ref := os.environ.get("OMARCHY_ISO_REF"):
        return ref.strip()

    ref_file = Path("/root/omarchy_iso_ref")
    if ref_file.exists():
        try:
            return ref_file.read_text().strip()
        except OSError:
            pass

    return "stable"


def _default_package_targets() -> dict[str, str]:
    if _iso_ref() in {"dev", "local"}:
        return {
            "runtime": "omarchy-dev",
            "settings": "omarchy-settings-dev",
            "nvim": "omarchy-nvim",
        }

    return {
        "runtime": "omarchy",
        "settings": "omarchy-settings",
        "nvim": "omarchy-nvim",
    }


def _package_targets() -> dict[str, str]:
    targets = _default_package_targets()

    targets_file = _media_root() / "package-targets"
    if targets_file.exists():
        try:
            for raw in targets_file.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip().strip('"\'')
                match key.strip():
                    case "OMARCHY_RUNTIME_PACKAGE":
                        targets["runtime"] = value
                    case "OMARCHY_SETTINGS_PACKAGE":
                        targets["settings"] = value
                    case "OMARCHY_NVIM_PACKAGE":
                        targets["nvim"] = value
        except OSError:
            pass

    env_to_key = {
        "OMARCHY_RUNTIME_PACKAGE": "runtime",
        "OMARCHY_SETTINGS_PACKAGE": "settings",
        "OMARCHY_NVIM_PACKAGE": "nvim",
    }
    for env_name, key in env_to_key.items():
        if value := os.environ.get(env_name):
            targets[key] = value

    return targets


def _omarchy_runtime_package() -> str:
    return _package_targets()["runtime"]


def _omarchy_settings_package() -> str:
    return _package_targets()["settings"]


def _omarchy_nvim_package() -> str:
    return _package_targets()["nvim"]


# The package sets themselves live in target_packages, the single definition
# shared with the media builder's expected-closure resolver. They are imported
# above and anchored here because the phase modules, the compatibility facade,
# and existing callers read these names off this module.
_REEXPORTED_PACKAGE_SETS = (
    EARLY_LIMINE_BOOTSTRAP_PACKAGES,
    EARLY_ASAHI_GRUB_BOOTSTRAP_PACKAGES,
    EARLY_BOOTSTRAP_BASE_PACKAGES,
    EARLY_LUAROCKS_PACKAGES,
    TAILSCALE_PACKAGES,
)


def _plan_boot_backend(ctx: InstallContext | None) -> str:
    return _boot_backend(ctx) if ctx is not None else DEFAULT_BOOT_BACKEND


def _early_bootstrap_packages(ctx: InstallContext | None = None) -> list[str]:
    return early_bootstrap_packages(
        _plan_boot_backend(ctx),
        _omarchy_settings_package(),
    )


def _early_user_seed_packages() -> list[str]:
    return early_user_seed_packages(_omarchy_nvim_package())


def _early_packages(ctx: InstallContext | None = None) -> list[str]:
    return early_packages(
        _plan_boot_backend(ctx),
        _omarchy_settings_package(),
        _omarchy_nvim_package(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# prepare_live: ready the live ISO for the install — tear down any previous
# holders on the install disk (via the bash helper), then parse the
# configurator output.
#
# The live pacman keyring is deliberately NOT waited on. The offline repo is
# SigLevel = Never (see configs/pacman-offline.conf for why that is required:
# pacstrap verifies against the LIVE GpgDir, so anything short of Never makes
# installs depend on archiso's boot-time pacman-init.service). That service
# (gpg key generation + populating every keyring, Type=oneshot with no start
# timeout) can take minutes on real hardware reading from USB — blocking on
# it here stalled installs at 5% while it ground away in the background, and
# racing it failed pacstrap with "required key missing from keyring".
#
# archinstall is patched in the wrapper (omarchy-iso-install) BEFORE Python
# imports it, so no patching happens here.
# ─────────────────────────────────────────────────────────────────────────────

def prepare_live(ctx: InstallContext) -> None:
    if ctx.is_protected:
        info("› protected mode: skipping whole-disk cleanup")
    else:
        disk = _install_disk(ctx)
        if disk:
            info(f"› cleaning up holders on install disk: {disk}")
            subprocess.run(["omarchy-iso-cleanup-disk", disk], check=True)

    info("› loading configurator output")
    ctx.state["arch_config_handler"] = arch.load_arch_config(
        ctx.arch_config_path, ctx.creds_path
    )
    ctx.state["mirror_handler"] = arch.make_mirror_handler(offline=True)


def _install_disk(ctx: InstallContext) -> str | None:
    """Return the device path of the disk being wiped, or None for
    pre_mounted / no-wipe configs."""
    config = ctx.user_configuration
    for mod in config.get("disk_config", {}).get("device_modifications", []):
        if mod.get("wipe"):
            return mod.get("device")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# arch_install_system: everything inside a single Installer context manager.
# Reorders guided.py's perform_installation() so early Omarchy packages install
# before user creation and before our Omarchy-owned Limine setup copies files
# from the target's limine package.
# ─────────────────────────────────────────────────────────────────────────────

def prepare_install_target(ctx: InstallContext) -> None:
    if ctx.is_protected:
        verify_protected_mounts(ctx)


def arch_install_system(ctx: InstallContext) -> None:
    """Install the target system from the archinstall JSON.

    The phase sequence is the same for full-disk and protected installs. The
    JSON decides whether archinstall should create/mount a disk layout or use
    a pre-mounted target, and Omarchy derives boot/fstab details from that same
    input.
    """
    handler = ctx.state["arch_config_handler"]
    mirror_handler = ctx.state["mirror_handler"]
    config = handler.config
    pre_mounted = arch.is_pre_mount(config)

    if not pre_mounted:
        info("› partitioning + formatting + encrypting")
        arch.perform_filesystem_operations(config)

    info("› opening installer context")
    with arch.open_installer(config, ctx.target, silent=True) as installer:
        if not pre_mounted:
            installer.mount_ordered_layout()

        installer.sanity_check(
            offline=True,
            skip_ntp=True,
            skip_wkd=True,
        )

        if not pre_mounted and arch.is_encrypted(config):
            installer.generate_key_files()

        if config.mirror_config:
            installer.set_mirrors(mirror_handler, config.mirror_config, on_target=False)

        _mount_offline_package_cache(ctx)
        _mask_mkinitcpio_pacman_hooks(ctx)
        try:
            info("› installing base system (mkinitcpio deferred to final Limine UKI build)")
            # An empty kb_layout makes archinstall's set_keyboard_language skip
            # booting the target in a container just to run localectl; the
            # keymap is configured offline right after instead.
            kb_layout = config.locale_config.kb_layout if config.locale_config else ""
            installer.minimal_installation(
                optional_repositories=(
                    config.mirror_config.optional_repositories
                    if config.mirror_config else []
                ),
                mkinitcpio=False,
                hostname=config.hostname,
                locale_config=(
                    None
                    if pre_mounted
                    else (
                        replace(config.locale_config, kb_layout="")
                        if config.locale_config else None
                    )
                ),
                pacman_config=config.pacman_config,
            )

            if not configure_keyboard(installer.target, kb_layout):
                error(f"Invalid keyboard language specified: {kb_layout}")
            if pre_mounted and config.locale_config:
                _configure_pre_mounted_locale(ctx.target, config.locale_config)

            if config.mirror_config:
                installer.set_mirrors(mirror_handler, config.mirror_config, on_target=True)

            if config.swap and config.swap.enabled:
                installer.setup_swap(algo=config.swap.algorithm)
                _drop_archinstall_zram_conf(ctx)

            _install_early_packages(ctx, installer)
            _configure_boot(ctx, installer, config)

            info("› creating user (with /etc/skel populated)")
            if config.auth_config and config.auth_config.users:
                installer.create_users(config.auth_config.users)

            if config.app_config:
                info("› installing archinstall application selections")
                arch.install_applications(installer, config)

            info("› installing Omarchy runtime + omarchy-base.packages")
            installer.add_additional_packages(_runtime_package_list(ctx))

            # Tailscale is bundled in the offline mirror but only installed
            # when an autoinstall drive staged an auth key; must happen here,
            # while the mirror is still bind-mounted, not in the phase that
            # configures the join.
            if ctx.tailscale_authkey_path is not None:
                info("› installing tailscale (auth key staged for first boot)")
                installer.add_additional_packages(list(TAILSCALE_PACKAGES))
        finally:
            _unmask_mkinitcpio_pacman_hooks(ctx)
            _unmount_offline_package_cache(ctx)

        # Standard arch finishers. archinstall prefixes its chroot commands
        # with `arch-chroot -S`, which asks systemd for a transient scope and
        # therefore cannot run inside the package-builder container. Configure
        # pre-mounted images through their filesystem/root instead.
        if config.timezone:
            _configure_timezone(ctx, installer, config.timezone, pre_mounted)
        if config.ntp:
            _configure_time_sync(ctx, installer, config.ntp, pre_mounted)
        if root := arch.root_user(config):
            installer.set_user_password(root)

        if pre_mounted:
            _write_pre_mounted_fstab(ctx)
        else:
            installer.genfstab()


def _configure_pre_mounted_locale(target: Path, locale_config) -> None:
    """Generate a locale without archinstall's systemd-scoped chroot."""
    language = locale_config.sys_lang
    encoding = locale_config.sys_enc
    match = re.fullmatch(
        r"([A-Za-z0-9_]+)(?:\.([A-Za-z0-9-]+))?(@[A-Za-z0-9_-]+)?",
        language,
    )
    if match is None or re.fullmatch(r"[A-Za-z0-9-]+", encoding) is None:
        raise RuntimeError("invalid target locale")

    base, embedded_encoding, modifier = match.groups()
    if embedded_encoding and encoding == "UTF-8":
        encoding = embedded_encoding
    modifier = modifier or ""
    locale_pattern = re.compile(
        rf"^#?{re.escape(base)}(?:\.{re.escape(encoding)})?"
        rf"{re.escape(modifier)}[ \\t]+{re.escape(encoding)}[ \\t]*$"
    )

    locale_gen = target / "etc/locale.gen"
    if not locale_gen.is_file() or locale_gen.is_symlink():
        raise RuntimeError("target locale catalog is unavailable")
    lines = locale_gen.read_text().splitlines(keepends=True)
    lang_value = None
    for index, line in enumerate(lines):
        value = line.rstrip("\r\n")
        if locale_pattern.fullmatch(value):
            locale_name = value.removeprefix("#").split()[0]
            uncommented = f"{locale_name} {encoding}"
            ending = line[len(value) :]
            lines[index] = uncommented + ending
            lang_value = locale_name
            break
    if lang_value is None:
        raise RuntimeError("target locale is unavailable")

    locale_gen.write_text("".join(lines))
    subprocess.run(
        ["arch-chroot", str(target), "locale-gen"],
        check=True,
    )
    locale_conf = target / "etc/locale.conf"
    if locale_conf.exists() and (
        not locale_conf.is_file() or locale_conf.is_symlink()
    ):
        raise RuntimeError("target locale configuration is unsafe")
    locale_conf.write_text(f"LANG={lang_value}\n")


def _configure_timezone(ctx: InstallContext, installer, zone: str, pre_mounted: bool) -> None:
    if not pre_mounted:
        installer.set_timezone(zone)
        return

    zone_path = Path(zone)
    if zone_path.is_absolute() or ".." in zone_path.parts:
        raise RuntimeError(f"invalid timezone path: {zone}")
    if not (ctx.target / "usr/share/zoneinfo" / zone_path).exists():
        raise RuntimeError(f"timezone is not installed in target: {zone}")

    localtime = ctx.target / "etc/localtime"
    if localtime.is_dir() and not localtime.is_symlink():
        raise RuntimeError(f"target localtime path is a directory: {localtime}")
    localtime.unlink(missing_ok=True)
    localtime.symlink_to(Path("/usr/share/zoneinfo") / zone_path)


def _configure_time_sync(ctx: InstallContext, installer, enabled: bool, pre_mounted: bool) -> None:
    if not enabled:
        return
    if not pre_mounted:
        installer.activate_time_synchronization()
        return

    subprocess.run(
        [
            "systemctl",
            "--root",
            str(ctx.target),
            "enable",
            "systemd-timesyncd.service",
        ],
        check=True,
    )


def _configure_boot(ctx: InstallContext, installer, config) -> None:
    if _boot_backend(ctx) == "asahi-grub":
        _configure_asahi_grub_boot(ctx, installer, config)
        return
    _configure_limine_boot(ctx, installer, config)


def _configure_asahi_grub_boot(ctx: InstallContext, installer, config) -> None:
    if not ctx.is_protected:
        raise RuntimeError("Asahi GRUB currently requires a pre-mounted full-OS package target")
    if _boot_intent(ctx).get("register_firmware"):
        raise RuntimeError("Asahi GRUB package builds must not register UEFI firmware entries")
    if not arch.bootloader_enabled(config):
        raise RuntimeError("Asahi GRUB requires bootloader_config.bootloader=Grub")

    boot_mount = ctx.target / _storage_intent(ctx).get("boot_mount", "/boot").lstrip("/")
    esp_mount = ctx.target / _boot_intent(ctx)["esp_mount"].lstrip("/")
    if not boot_mount.is_dir() or not esp_mount.is_dir():
        raise RuntimeError("Asahi GRUB boot and ESP mounts must exist before installation")

    info("› deferring Asahi GRUB image generation until system setup is complete")
    installer._helper_flags["bootloader"] = "grub"


def _configure_limine_boot(ctx: InstallContext, installer, config) -> None:
    if not arch.bootloader_enabled(config):
        return
    if not arch.is_limine(config):
        raise RuntimeError("Omarchy installs only support Limine bootloader setup")

    info("› installing bootloader (Limine)")
    if arch.is_pre_mount(config):
        _install_pre_mounted_limine(ctx)
    else:
        _install_limine_omarchy(ctx, installer, config)

    info("› writing Limine config")
    if arch.is_pre_mount(config):
        _write_pre_mounted_limine_defaults(ctx)
    else:
        _write_limine_defaults_from_config(ctx, installer, config)


def _install_limine_omarchy(ctx: InstallContext, installer, config) -> None:
    boot_partition = installer._get_boot_partition()
    efi_partition = installer._get_efi_partition()
    root = installer._get_root()

    if boot_partition is None:
        raise RuntimeError(f"Could not detect boot at mountpoint {ctx.target}")
    if root is None:
        raise RuntimeError(f"Could not detect root at mountpoint {ctx.target}")

    bootloader_config = config.bootloader_config
    bootloader_removable = bool(
        getattr(bootloader_config, "removable", False) if bootloader_config else False
    )

    if arch.has_uefi():
        if efi_partition is None:
            raise RuntimeError("Could not detect EFI partition")
        if not efi_partition.mountpoint:
            raise RuntimeError("EFI partition is not mounted")

        _install_limine_efi(
            ctx,
            esp_mount=str(efi_partition.mountpoint),
            disk=arch.parent_device_path(efi_partition.safe_dev_path),
            part=int(efi_partition.partn),
            removable=bootloader_removable,
        )
    else:
        _install_limine_bios(ctx, boot_partition)

    installer._helper_flags["bootloader"] = "limine"


def _install_pre_mounted_limine(ctx: InstallContext) -> None:
    boot = _boot_intent(ctx)
    storage = _storage_intent(ctx)
    esp_device = storage.get("esp_device")
    if not esp_device:
        raise RuntimeError("omarchy_install.storage.esp_device missing")

    register_firmware = bool(boot.get("register_firmware"))
    pre_state = _read_efibootmgr() if register_firmware else None
    windows_before = (
        _find_label_entries(pre_state["entries"], "Windows")
        if pre_state is not None
        else []
    )
    if register_firmware:
        disk, part = _split_partition_device(esp_device)
    else:
        disk, part = Path("/dev/null"), 0
    _install_limine_efi(
        ctx,
        esp_mount=boot["esp_mount"],
        disk=Path(disk),
        part=part,
        esp_path=boot.get("esp_path", "/EFI/limine"),
        efi_binary=boot.get("efi_binary", _limine_efi_names()[1]),
        pre_state=pre_state,
        register_firmware=register_firmware,
    )

    if not register_firmware:
        return
    post_state = _read_efibootmgr()
    windows_after = _find_label_entries(post_state["entries"], "Windows")
    if windows_before and not windows_after:
        raise RuntimeError("Windows boot entry disappeared during Limine install — aborting")


def _limine_efi_names(machine: str | None = None) -> tuple[str, str, str]:
    machine = (machine or os.uname().machine).lower()
    if machine in {"aarch64", "arm64"}:
        return "BOOTAA64.EFI", "limine_aa64.efi", "BOOTAA64.EFI"
    if machine == "x86_64":
        return "BOOTX64.EFI", "limine_x64.efi", "BOOTX64.EFI"
    raise RuntimeError(f"Unsupported Limine EFI architecture: {machine}")


def _install_limine_efi(
    ctx: InstallContext,
    *,
    esp_mount: str,
    disk: Path,
    part: int,
    removable: bool = False,
    esp_path: str = "/EFI/limine",
    efi_binary: str | None = None,
    pre_state: dict | None = None,
    register_firmware: bool = True,
) -> None:
    source_name, default_binary, removable_binary = _limine_efi_names()
    efi_binary = efi_binary or default_binary
    if removable:
        esp_path = "/EFI/BOOT"
        efi_binary = removable_binary

    limine_path = ctx.target / "usr" / "share" / "limine"
    target_dir = Path(esp_mount) / esp_path.lstrip("/")
    target_path = target_dir / efi_binary
    _copy_required(limine_path / source_name, ctx.target / target_path.relative_to("/"))

    hook_command = f"/usr/bin/cp /usr/share/limine/{source_name} {target_path}"
    _write_limine_pacman_hook(ctx.target, hook_command)

    if not register_firmware:
        return
    loader = "\\" + str(Path(esp_path) / efi_binary).strip("/").replace("/", "\\")
    _register_limine_efi_entry(disk, part, loader, pre_state=pre_state)


def _register_limine_efi_entry(
    disk: Path,
    part: int,
    loader: str,
    *,
    pre_state: dict | None = None,
) -> None:
    pre_state = pre_state or _read_efibootmgr()
    stale_limine = _find_label_entries(pre_state["entries"], "Limine")
    for num in stale_limine:
        subprocess.run(
            ["efibootmgr", "--bootnum", num, "--delete-bootnum"],
            check=False, capture_output=True,
        )

    subprocess.run(
        [
            "efibootmgr",
            "--create",
            "--disk", str(disk),
            "--part", str(part),
            "--label", "Limine",
            "--loader", loader,
            "--unicode",
            "--verbose",
        ],
        check=True,
    )

    post_state = _read_efibootmgr()
    new_limine = _find_label_entries(post_state["entries"], "Limine")
    if not new_limine:
        raise RuntimeError("efibootmgr --create reported success but no Limine entry found")
    limine_num = new_limine[0]

    keep = [
        num
        for num in pre_state["order"]
        if num not in stale_limine
        and num != limine_num
        and num in pre_state["entries"]
    ]
    subprocess.run(
        ["efibootmgr", "--bootorder", ",".join([limine_num, *keep])],
        check=True, capture_output=True,
    )


def _install_limine_bios(ctx: InstallContext, boot_partition) -> None:
    boot_limine_path = ctx.target / "boot" / "limine"
    boot_limine_path.mkdir(parents=True, exist_ok=True)

    parent_dev_path = arch.parent_device_path(boot_partition.safe_dev_path)
    if unique_path := arch.unique_device_path(parent_dev_path):
        parent_dev_path = unique_path

    limine_path = ctx.target / "usr" / "share" / "limine"
    _copy_required(limine_path / "limine-bios.sys", boot_limine_path / "limine-bios.sys")
    subprocess.run(
        ["arch-chroot", str(ctx.target), "limine", "bios-install", str(parent_dev_path)],
        check=True,
    )
    hook_command = (
        f"/usr/bin/limine bios-install {parent_dev_path} && "
        "/usr/bin/cp /usr/share/limine/limine-bios.sys /boot/limine/"
    )
    _write_limine_pacman_hook(ctx.target, hook_command)


def _copy_required(src: Path, dst: Path) -> None:
    if not src.exists():
        raise RuntimeError(f"Required Limine file missing: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _write_limine_pacman_hook(target: Path, hook_command: str) -> None:
    hook_contents = textwrap.dedent(
        f"""\
        [Trigger]
        Operation = Upgrade
        Type = Package
        Target = limine

        [Action]
        Description = Deploying Omarchy Limine after upgrade...
        When = PostTransaction
        Exec = /bin/sh -c "{hook_command}"
        """
    )
    hooks_dir = target / "etc" / "pacman.d" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "99-omarchy-limine.hook").write_text(hook_contents)


def _write_limine_defaults_from_config(ctx: InstallContext, installer, config) -> None:
    if not arch.is_limine(config):
        return

    root = installer._get_root()
    if root is None:
        raise RuntimeError(f"Could not detect root at mountpoint {ctx.target}")

    cmdline = " ".join(installer._get_kernel_params(root))
    _write_limine_defaults(ctx, cmdline, esp_mount=_installer_esp_mount(installer))


def _write_limine_defaults(
    ctx: InstallContext,
    cmdline: str,
    *,
    esp_mount: str,
    enable_fallback: bool | None = None,
) -> None:
    if not cmdline.strip():
        raise RuntimeError("Could not compute kernel cmdline from install config")
    if "root=" not in cmdline:
        raise RuntimeError(f"Computed cmdline has no root=: {cmdline!r}")

    default_template = _limine_template(ctx, "default.conf")
    limine_template = _limine_template(ctx, "limine.conf")
    vendor_templates = ctx.target / "usr" / "share" / "omarchy" / "default" / "limine"
    vendor_templates.mkdir(parents=True, exist_ok=True)
    shutil.copy2(default_template, vendor_templates / "default.conf")
    shutil.copy2(limine_template, vendor_templates / "limine.conf")

    default_text = default_template.read_text()
    default_text = default_text.replace("@@CMDLINE@@", cmdline)
    default_text = re.sub(r'^ESP_PATH=.*$', f'ESP_PATH="{esp_mount}"', default_text, flags=re.MULTILINE)
    if enable_fallback is not None:
        default_text = default_text.rstrip() + f"\nENABLE_LIMINE_FALLBACK={'yes' if enable_fallback else 'no'}\n"
    if not arch.has_uefi():
        default_text = default_text.rstrip() + "\nENABLE_UKI=no\nENABLE_LIMINE_FALLBACK=no\n"

    default_limine = ctx.target / "etc" / "default" / "limine"
    default_limine.parent.mkdir(parents=True, exist_ok=True)
    default_limine.write_text(default_text)

    kernel_cmdline = ctx.target / "etc" / "kernel" / "cmdline"
    kernel_cmdline.parent.mkdir(parents=True, exist_ok=True)
    kernel_cmdline.write_text(cmdline + "\n")

    limine_conf = ctx.target / esp_mount.lstrip("/") / "limine.conf"
    limine_conf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(limine_template, limine_conf)


def _installer_esp_mount(installer) -> str:
    if efi_partition := installer._get_efi_partition():
        if efi_partition.mountpoint:
            return str(efi_partition.mountpoint)
    return "/boot"



def _limine_template(ctx: InstallContext, filename: str) -> Path:
    bundled_assets = Path(__file__).resolve().parent.parent / "assets" / "limine"
    candidates = [
        ctx.target / "usr" / "share" / "omarchy" / "install" / "assets" / "limine" / filename,
        ctx.target / "usr" / "share" / "omarchy" / "default" / "limine" / filename,
        ctx.omarchy_path / "install" / "assets" / "limine" / filename,
        ctx.omarchy_path / "default" / "limine" / filename,
        bundled_assets / filename,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    searched = "\n  ".join(str(p) for p in candidates)
    raise RuntimeError(f"Limine template {filename} not found. Searched:\n  {searched}")


DEFERRED_BOOT_HOOKS = (
    "60-mkinitcpio-remove.hook",
    "60-limine-mkinitcpio-remove-pre.hook",
    "80-limine-efi-deploy.hook",
    "90-limine-mkinitcpio-remove-post.hook",
    "90-mkinitcpio-install.hook",
)

# Inside the target chroot, only the install hook is worth deferring.
# limine-entry-tool's 90-mkinitcpio-install.hook triggers on usr/lib/firmware/*,
# usr/src/*/dkms.conf and usr/lib/modules/*/pkgbase, and anything but a
# usr/lib/modules path makes it rebuild the initramfs and UKI for EVERY
# installed kernel. omarchy-apply-system's hardware scripts routinely install
# such packages (sof-firmware on Intel audio, nvidia-open-dkms, linux-ptl on
# Panther Lake, linux-t2 on Macs), so the phase can pay for several full UKI
# builds. finalize_limine_boot runs limine-update right after, which pipes
# "rebuild" into the same script and rebuilds every kernel unconditionally —
# those mid-phase builds are always thrown away, and are stale anyway (nvidia.sh
# writes its mkinitcpio drop-in after installing the driver).
#
# The kernel-removal hooks stay live: they only prune Limine entries, which is
# exactly what ptl-kernel.sh's "pacman -Rdd linux" needs.
TARGET_DEFERRED_BOOT_HOOKS = ("90-mkinitcpio-install.hook",)


def _drop_archinstall_zram_conf(ctx: InstallContext) -> None:
    """Remove the zram-generator.conf archinstall's setup_swap writes directly.

    omarchy-settings ships the tuning as a vendor drop-in at
    /usr/lib/systemd/zram-generator.conf.d/90-omarchy.conf, which outranks the
    main config file. setup_swap's generic /etc copy decides nothing and only
    implies /etc is where zram gets configured, so drop it — we still want the
    zram-generator package and service that setup_swap installs.
    """
    zram_conf = ctx.target / "etc" / "systemd" / "zram-generator.conf"
    zram_conf.unlink(missing_ok=True)


def _install_early_packages(ctx: InstallContext, installer) -> None:
    bootstrap_packages = _early_bootstrap_packages(ctx)
    user_seed_packages = _early_user_seed_packages()

    info(f"› installing early Omarchy packages: {', '.join(bootstrap_packages)}")
    installer.add_additional_packages(bootstrap_packages)

    info(f"› installing LuaRocks prerequisites: {', '.join(EARLY_LUAROCKS_PACKAGES)}")
    installer.add_additional_packages(EARLY_LUAROCKS_PACKAGES)

    info(f"› installing user seed packages: {', '.join(user_seed_packages)}")
    installer.add_additional_packages(user_seed_packages)


def _offline_package_source() -> Path:
    """Resolve the verified mirror namespace visible to the current installer."""
    return Path(
        os.environ.get(
            "OMARCHY_OFFLINE_MIRROR_ROOT",
            "/var/cache/omarchy/mirror/offline",
        )
    )


def _mount_offline_package_cache(ctx: InstallContext) -> None:
    """Let pacstrap consume bundled packages without copying them first.

    Pacstrap always points pacman's CacheDir inside the target. Without this
    bind mount, pacman copies every package from the ISO's file:// repository
    into that cache and then extracts it, duplicating several GiB of I/O.
    Mount the already-populated offline repository at the target cache for the
    duration of package installation. It is unmounted before genfstab so the
    live-only bind can never leak into the installed system's fstab.
    """
    source = _offline_package_source()
    target = ctx.target / "var" / "cache" / "pacman" / "pkg"
    if not source.is_dir():
        raise RuntimeError(f"offline package cache missing: {source}")

    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(["mount", "--bind", str(source), str(target)], check=True)
    ctx.state.setdefault("bind_mounts", []).append(str(target))


def _unmount_offline_package_cache(ctx: InstallContext) -> None:
    target = str(ctx.target / "var" / "cache" / "pacman" / "pkg")
    subprocess.run(["umount", target], check=True)
    try:
        ctx.state.get("bind_mounts", []).remove(target)
    except ValueError:
        pass


def _is_devnull_symlink(path: Path) -> bool:
    try:
        return path.is_symlink() and path.readlink() == Path("/dev/null")
    except OSError:
        return False


def _mask_mkinitcpio_pacman_hooks(
    ctx: InstallContext,
    root: Path = Path("/"),
    names: tuple[str, ...] = DEFERRED_BOOT_HOOKS,
) -> None:
    """Temporarily suppress boot-image pacman hooks around a package install.

    With the default root this masks the LIVE hook dir, which is what pacstrap
    reads: pacstrap uses the live system's /etc/pacman.conf, and pacman.conf(5)
    notes that HookDir is absolute and the target root is not prepended, so
    target-side /mnt/etc/pacman.d/hooks masks do not override target
    /usr/share/libalpm hooks during installation. The target's real hooks still
    get installed and become active after reboot.

    Passing ctx.target masks the same way inside the target, for pacman runs
    that happen under arch-chroot (see TARGET_DEFERRED_BOOT_HOOKS).
    """
    hooks_dir = root / "etc/pacman.d/hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = hooks_dir / name
        backup = hooks_dir / f"{name}.omarchy-backup"
        if _is_devnull_symlink(path):
            continue
        if path.exists() or path.is_symlink():
            backup.unlink(missing_ok=True)
            path.rename(backup)
        path.symlink_to("/dev/null")


def _unmask_mkinitcpio_pacman_hooks(
    ctx: InstallContext,
    root: Path = Path("/"),
    names: tuple[str, ...] = DEFERRED_BOOT_HOOKS,
) -> None:
    hooks_dir = root / "etc/pacman.d/hooks"
    for name in names:
        path = hooks_dir / name
        backup = hooks_dir / f"{name}.omarchy-backup"
        try:
            if _is_devnull_symlink(path):
                path.unlink()
            if backup.exists() or backup.is_symlink():
                backup.rename(path)
        except OSError as exc:
            info(f"warning: failed to restore pacman hook mask for {name}: {exc}")


def _runtime_package_list(ctx: InstallContext) -> list[str]:
    """Selected Omarchy runtime package + every package in the ISO-bundled
    base package list that isn't already installed early."""
    return runtime_package_list(
        read_package_list(_media_root() / "omarchy-base.packages"),
        boot_backend=_plan_boot_backend(ctx),
        runtime_package=_omarchy_runtime_package(),
        settings_package=_omarchy_settings_package(),
        nvim_package=_omarchy_nvim_package(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Install intent helpers: normalize the Omarchy-specific part of the
# configurator JSON so full-disk and pre-mounted installs feed the same boot
# and target setup code.
# ─────────────────────────────────────────────────────────────────────────────

def _boot_intent(ctx: InstallContext) -> dict:
    boot = dict(ctx.omarchy_install.get("boot") or {})
    boot.setdefault("backend", "limine")
    boot.setdefault("esp_mount", "/boot")
    boot.setdefault("esp_path", "/EFI/limine")
    boot.setdefault("efi_binary", _limine_efi_names()[1])
    boot.setdefault("enable_fallback", not ctx.is_protected)
    boot.setdefault("register_firmware", True)
    return boot


def _boot_backend(ctx: InstallContext) -> str:
    backend = _boot_intent(ctx)["backend"]
    if backend not in BOOT_BACKENDS:
        raise RuntimeError(f"Unsupported Omarchy boot backend: {backend}")
    return backend


def _storage_intent(ctx: InstallContext) -> dict:
    return dict(ctx.omarchy_install.get("storage") or {})


def verify_protected_mounts(ctx: InstallContext) -> None:
    target = ctx.target
    boot = _boot_intent(ctx)
    storage = _storage_intent(ctx)

    # Devices before the mountpoint: when the configurator hands over paths for
    # partitions that do not exist, "root_device /dev/nvme0n1p12 does not
    # exist" is diagnosable from the log alone, where "/mnt is not a
    # mountpoint" sends everyone looking at the mount instead of the paths.
    required_devices = ["esp_device", "root_device"]
    if storage.get("boot_device"):
        required_devices.append("boot_device")
    for key in required_devices:
        device = storage.get(key)
        if not device:
            raise RuntimeError(f"protected mode: omarchy_install.storage.{key} missing")
        if not Path(device).exists():
            raise RuntimeError(f"protected mode: {key} {device} does not exist")

    if not _is_mountpoint(target):
        raise RuntimeError(f"protected mode: {target} is not a mountpoint")

    boot_device = storage.get("boot_device")
    boot_mount = storage.get("boot_mount", "/boot")
    if boot_device:
        boot_mp = target / boot_mount.lstrip("/")
        if not _is_mountpoint(boot_mp):
            info(f"› remounting protected boot filesystem {boot_device} at {boot_mp}")
            boot_mp.mkdir(parents=True, exist_ok=True)
            subprocess.run(["mount", boot_device, str(boot_mp)], check=True)

    esp_mp = target / boot["esp_mount"].lstrip("/")
    if not _is_mountpoint(esp_mp):
        esp_dev = storage["esp_device"]
        info(f"› remounting protected ESP {esp_dev} at {esp_mp}")
        esp_mp.mkdir(parents=True, exist_ok=True)
        subprocess.run(["mount", esp_dev, str(esp_mp)], check=True)

    info(f"› protected target verified: kernel={storage.get('kernel', 'linux')} esp={boot['esp_mount']}")


def _is_mountpoint(path: Path) -> bool:
    res = capture(["findmnt", "-rn", str(path)])
    return res.returncode == 0 and bool(res.stdout.strip())


# ── pre-mounted fstab / crypttab / cmdline ───────────────────────────────────

def _btrfs_root_device(ctx: InstallContext) -> str:
    storage = _storage_intent(ctx)
    if storage.get("luks_uuid"):
        return storage.get("root_mapper") or "/dev/mapper/omarchy_root"
    return storage["root_device"]


def _blkid_uuid(device: str) -> str:
    uuid = capture_identifier(
        ["blkid", "-s", "UUID", "-o", "value", device], f"the UUID of {device}"
    )
    if not uuid:
        raise RuntimeError(f"blkid returned no UUID for {device}")
    return uuid


def _esp_device(ctx: InstallContext) -> str:
    storage = _storage_intent(ctx)
    if esp_device := storage.get("esp_device"):
        return esp_device

    boot = _boot_intent(ctx)
    esp_mp = ctx.target / boot["esp_mount"].lstrip("/")
    dev = capture_identifier(
        ["findmnt", "-n", "-o", "SOURCE", str(esp_mp)], f"the ESP device at {esp_mp}"
    )
    if not dev:
        raise RuntimeError(f"could not resolve ESP device at {esp_mp}")
    return dev


def _write_pre_mounted_fstab(ctx: InstallContext) -> None:
    boot = _boot_intent(ctx)
    storage = _storage_intent(ctx)
    btrfs_dev = _btrfs_root_device(ctx)
    btrfs_uuid = _blkid_uuid(btrfs_dev)
    esp_uuid = _blkid_uuid(_esp_device(ctx))
    esp_mount = boot["esp_mount"]

    root_opts = "noatime,compress=zstd"
    if storage.get("grow_root"):
        root_opts += ",x-systemd.growfs"
    btrfs_opts = "noatime,compress=zstd,subvol="
    lines = [
        "# /etc/fstab — generated by Omarchy ISO",
        "# <device>  <mount>  <fs>  <options>  <dump>  <pass>",
        f"UUID={btrfs_uuid}  /                      btrfs  {root_opts},subvol=@       0 0",
        f"UUID={btrfs_uuid}  /home                  btrfs  {btrfs_opts}@home   0 0",
        f"UUID={btrfs_uuid}  /var/log               btrfs  {btrfs_opts}@log    0 0",
        f"UUID={btrfs_uuid}  /var/cache/pacman/pkg  btrfs  {btrfs_opts}@pkg    0 0",
    ]
    if boot_device := storage.get("boot_device"):
        boot_uuid = _blkid_uuid(boot_device)
        boot_mount = storage.get("boot_mount", "/boot")
        lines.append(
            f"UUID={boot_uuid}  {boot_mount}                  ext4   defaults,noatime       0 2"
        )
    lines.extend(
        [
            f"UUID={esp_uuid}  {esp_mount}                   vfat   umask=0077              0 2",
            "",
        ]
    )
    (ctx.target / "etc" / "fstab").write_text("\n".join(lines))


def _write_pre_mounted_crypttab(ctx: InstallContext) -> None:
    storage = _storage_intent(ctx)
    luks_uuid = storage.get("luks_uuid")
    if not luks_uuid:
        return
    crypttab = ctx.target / "etc" / "crypttab.initramfs"
    crypttab.write_text(f"omarchy_root  UUID={luks_uuid}  none  luks,discard\n")


def _build_pre_mounted_cmdline(ctx: InstallContext, btrfs_uuid: str) -> str:
    storage = _storage_intent(ctx)
    if storage.get("luks_uuid"):
        root_mapper = storage.get("root_mapper") or "/dev/mapper/omarchy_root"
        return (
            f"cryptdevice=UUID={storage['luks_uuid']}:omarchy_root "
            f"root={root_mapper} zswap.enabled=0 "
            "rootflags=subvol=@ rw rootfstype=btrfs"
        )
    return (
        f"root=UUID={btrfs_uuid} zswap.enabled=0 "
        "rootflags=subvol=@ rw rootfstype=btrfs"
    )


def _write_pre_mounted_limine_defaults(ctx: InstallContext) -> None:
    boot = _boot_intent(ctx)
    btrfs_uuid = _blkid_uuid(_btrfs_root_device(ctx))
    cmdline = _build_pre_mounted_cmdline(ctx, btrfs_uuid)

    _write_pre_mounted_crypttab(ctx)
    _write_limine_defaults(
        ctx,
        cmdline,
        esp_mount=boot["esp_mount"],
        enable_fallback=bool(boot.get("enable_fallback")),
    )


# ── efibootmgr ───────────────────────────────────────────────────────────────

_BOOT_ENTRY_RE = re.compile(r"^Boot([0-9A-Fa-f]{4})\*?\s+(.*)$")
_BOOT_ORDER_RE = re.compile(r"^BootOrder:\s*(.*)$")


def _read_efibootmgr() -> dict:
    res = capture(["efibootmgr"], check=True)
    entries: dict[str, str] = {}
    order: list[str] = []
    for line in res.stdout.splitlines():
        m = _BOOT_ENTRY_RE.match(line)
        if m:
            entries[m.group(1).upper()] = m.group(2).strip()
            continue
        m = _BOOT_ORDER_RE.match(line)
        if m:
            order = [n.strip().upper() for n in m.group(1).split(",") if n.strip()]
    return {"entries": entries, "order": order, "raw": res.stdout}


def _find_label_entries(entries: dict[str, str], needle: str) -> list[str]:
    return [num for num, label in entries.items() if needle.lower() in label.lower()]


def _split_partition_device(part_dev: str) -> tuple[str, int]:
    parent = capture_identifier(
        ["lsblk", "-ndo", "PKNAME", part_dev], f"the parent disk of {part_dev}"
    )
    if not parent:
        raise RuntimeError(f"could not find parent disk for {part_dev}")
    part_num = capture_identifier(
        ["lsblk", "-ndo", "PARTN", part_dev], f"the partition number of {part_dev}"
    )
    if not part_num:
        raise RuntimeError(f"could not find partition number for {part_dev}")
    return f"/dev/{parent}", int(part_num)


def configure_hibernation(ctx: InstallContext) -> None:
    """Configure swap/resume in the target as root before user setup.

    Hibernation is system boot configuration, not per-user setup. The final
    Limine UKI build still happens later in finalize_limine_boot after this
    writes the resume hook and kernel cmdline drop-in.
    """
    if _boot_backend(ctx) == "asahi-grub":
        info("› Apple Silicon package: skipping Limine-specific hibernation setup")
        return

    setup = ctx.target / "usr" / "bin" / "omarchy-hibernation-setup"
    if not setup.exists():
        _debug_log(ctx, "skipping hibernation: /usr/bin/omarchy-hibernation-setup is not installed")
        return

    subprocess.run([
        "arch-chroot", str(ctx.target),
        "env",
        "OMARCHY_PATH=/usr/share/omarchy",
        "OMARCHY_INSTALL_LOG_FILE=/var/log/omarchy-install.log",
        "/usr/bin/omarchy-hibernation-setup", "--force", "--no-rebuild",
    ], check=True)


def _install_debug_enabled() -> bool:
    return os.environ.get("OMARCHY_INSTALL_DEBUG") == "1" or (
        _media_root() / "install-debug"
    ).exists()


def _debug_log(ctx: InstallContext, message: str) -> None:
    if not _install_debug_enabled():
        return
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    with ctx.log_path.open("a", encoding="utf-8") as log:
        log.write(f"[install-debug] {message}\n")


def _debug_dump_file(ctx: InstallContext, path: Path, max_lines: int = 120) -> None:
    if not _install_debug_enabled():
        return
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        _debug_log(ctx, f"dumping {path} sha256={digest}")
        with ctx.log_path.open("a", encoding="utf-8") as log:
            for line_no, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
                if line_no > max_lines:
                    log.write(f"[install-debug] ... truncated after {max_lines} lines ...\n")
                    break
                log.write(f"[install-debug] {path}:{line_no}: {line}\n")
    except OSError as exc:
        _debug_log(ctx, f"unable to dump {path}: {exc}")


def _debug_run(ctx: InstallContext, cmd: list[str]) -> None:
    if not _install_debug_enabled():
        return
    _debug_log(ctx, "+ " + " ".join(cmd))
    proc = capture(cmd)
    if proc.stdout:
        with ctx.log_path.open("a", encoding="utf-8") as log:
            for line in proc.stdout.splitlines():
                log.write(f"[install-debug] stdout: {line}\n")
    if proc.stderr:
        with ctx.log_path.open("a", encoding="utf-8") as log:
            for line in proc.stderr.splitlines():
                log.write(f"[install-debug] stderr: {line}\n")
    _debug_log(ctx, f"exit {proc.returncode}: " + " ".join(cmd))


# ─────────────────────────────────────────────────────────────────────────────
# Target setup phases:
#  1. point the target at the offline pacman.conf
#  2. bind-mount the offline mirror + /opt/packages into /mnt for target pacman
#     and bundled language runtimes
#  3. arch-chroot as root → omarchy-apply-system --first-install
#  4. arch-chroot as user → omarchy-provision-user --first-install
# ─────────────────────────────────────────────────────────────────────────────

def _prepare_target_setup(ctx: InstallContext) -> None:
    if ctx.state.get("target_setup_prepared"):
        return

    shutil.copy("/etc/pacman.conf", str(ctx.target / "etc" / "pacman.conf"))

    offline_source = str(_offline_package_source())
    bind_mounts = [
        (offline_source, offline_source),
        ("/opt/packages", "/opt/packages"),
    ]
    ctx.state.setdefault("bind_mounts", [])
    mounted = set(ctx.state["bind_mounts"])
    for src, dst in bind_mounts:
        target_dst = ctx.target / dst.lstrip("/")
        target_dst.mkdir(parents=True, exist_ok=True)
        if str(target_dst) not in mounted:
            subprocess.run(["mount", "--bind", src, str(target_dst)], check=True)
            ctx.state["bind_mounts"].append(str(target_dst))
            mounted.add(str(target_dst))

    ctx.state["target_setup_prepared"] = True


def _ensure_finalizer_log_started(ctx: InstallContext) -> tuple[str, int]:
    if "omarchy_start_time" not in ctx.state:
        ctx.state["omarchy_start_epoch"] = int(time.time())
        ctx.state["omarchy_start_time"] = time.strftime("%Y-%m-%d %H:%M:%S")

    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.log_path.touch(exist_ok=True)
    ctx.log_path.chmod(0o666)

    if not ctx.state.get("omarchy_finalizer_header_written"):
        with ctx.log_path.open("a", encoding="utf-8") as log:
            log.write(f"=== Omarchy Target Setup Started: {ctx.state['omarchy_start_time']} ===\n")
        ctx.state["omarchy_finalizer_header_written"] = True

    return ctx.state["omarchy_start_time"], ctx.state["omarchy_start_epoch"]


def _target_user_env(ctx: InstallContext, user: str) -> list[str]:
    home = f"/home/{user}"
    shell = "/bin/bash"
    passwd = ctx.target / "etc" / "passwd"

    try:
        for line in passwd.read_text(errors="ignore").splitlines():
            fields = line.split(":")
            if len(fields) >= 7 and fields[0] == user:
                home = fields[5] or home
                shell = fields[6] or shell
                break
    except OSError:
        pass

    return [
        f"HOME={home}",
        f"USER={user}",
        f"LOGNAME={user}",
        f"SHELL={shell}",
    ]


def _target_platform_env(ctx: InstallContext) -> list[str]:
    """Provide a deterministic hardware probe while constructing an image.

    The full-OS package is built in an aarch64 container, not on the eventual
    Apple machine, so its mounted /proc/device-tree correctly describes the
    builder rather than the target. Omarchy's platform-aware setup already
    accepts OMARCHY_PROC_ROOT for this purpose; stage only the minimum
    compatible property and let the installed system use its real /proc after
    first boot.
    """
    if _boot_backend(ctx) != "asahi-grub":
        return []

    probe = ctx.target / "run/omarchy-install/platform-probe/device-tree/compatible"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_bytes(b"apple,arm-platform\0")
    return ["OMARCHY_PROC_ROOT=/run/omarchy-install/platform-probe"]


def _repair_legacy_apple_dmi_probe(ctx: InstallContext) -> None:
    """Keep the pinned runtime's Intel-only DMI probe safe on Apple Silicon."""
    if _boot_backend(ctx) != "asahi-grub":
        return

    leaf = (
        ctx.target
        / "usr/share/omarchy/install/hardware/apple/fix-spi-keyboard.sh"
    )
    if not leaf.is_file() or leaf.is_symlink():
        raise RuntimeError("installed legacy Apple hardware probe is unsafe")

    original = (
        'product_name="$(cat /sys/class/dmi/id/product_name 2>/dev/null)"'
    )
    repaired = (
        'product_name="$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)"'
    )
    contents = leaf.read_text()
    if contents.count(repaired) == 1 and original not in contents:
        return
    if contents.count(original) != 1 or repaired in contents:
        raise RuntimeError("installed legacy Apple hardware probe changed")
    leaf.write_text(contents.replace(original, repaired))


@contextmanager
def _target_platform_override(ctx: InstallContext):
    """Make image-construction hardware routing deterministic, then restore it.

    The installed detector remains the signed package-owned binary. During the
    chrooted setup only, an Asahi full-OS image must route through Apple setup
    even though uname and /proc still describe the Linux builder VM.
    """
    if _boot_backend(ctx) != "asahi-grub":
        yield
        return

    detector = ctx.target / "usr/bin/omarchy-hw-apple-silicon"
    backup = detector.with_name(
        "omarchy-hw-apple-silicon.omarchy-image-original"
    )
    if (
        not detector.is_file()
        or detector.is_symlink()
        or not os.access(detector, os.X_OK)
        or backup.exists()
        or backup.is_symlink()
    ):
        raise RuntimeError("installed Apple Silicon detector is unsafe")

    detector.rename(backup)
    try:
        detector.write_text("#!/bin/bash\nexit 0\n")
        detector.chmod(0o755)
        yield
    finally:
        detector.unlink(missing_ok=True)
        backup.rename(detector)


def _run_target_setup_command(ctx: InstallContext, cmd: list[str], *, user: str | None = None) -> None:
    _repair_legacy_apple_dmi_probe(ctx)
    _prepare_target_setup(ctx)
    omarchy_start_time, omarchy_start_epoch = _ensure_finalizer_log_started(ctx)

    target_log = ctx.target / "var" / "log" / "omarchy-install.log"
    target_log.parent.mkdir(parents=True, exist_ok=True)
    target_log.touch(exist_ok=True)
    target_log.chmod(0o666)

    log_bind_mounted = False
    try:
        subprocess.run(["mount", "--bind", str(ctx.log_path), str(target_log)], check=True)
        log_bind_mounted = True
    except subprocess.CalledProcessError as exc:
        with ctx.log_path.open("a", encoding="utf-8") as log:
            log.write(f"[orchestrator] WARNING: failed to bind unified setup log: {exc}\n")

    mirror_channel = _read_omarchy_mirror()
    env_extras = [
        "OMARCHY_PATH=/usr/share/omarchy",
        "OMARCHY_INSTALL=/usr/share/omarchy/install",
        f"OMARCHY_INSTALL_USER={ctx.username}",
        f"OMARCHY_START_TIME={omarchy_start_time}",
        f"OMARCHY_START_EPOCH={omarchy_start_epoch}",
        f"OMARCHY_USER_NAME={ctx.full_name}",
        f"OMARCHY_USER_EMAIL={ctx.email}",
        f"OMARCHY_MIRROR={mirror_channel}",
        f"OMARCHY_ISO_REF={_iso_ref()}",
        f"OMARCHY_RUNTIME_PACKAGE={_omarchy_runtime_package()}",
        f"OMARCHY_SETTINGS_PACKAGE={_omarchy_settings_package()}",
        f"OMARCHY_NVIM_PACKAGE={_omarchy_nvim_package()}",
        "OMARCHY_INSTALL_LOG_FILE=/var/log/omarchy-install.log",
        "OMARCHY_LOG_TO_STDOUT=1",
    ]
    env_extras.extend(_target_platform_env(ctx))
    if _install_debug_enabled():
        env_extras.append("OMARCHY_INSTALL_DEBUG=1")
        _debug_log(ctx, "running target setup command: " + " ".join(cmd))

    chroot_cmd = ["arch-chroot"]
    if user:
        chroot_cmd += ["-u", user]
        env_extras.extend(_target_user_env(ctx, user))
    chroot_cmd += [str(ctx.target), "env", "--unset=XDG_RUNTIME_DIR", *env_extras, *cmd]

    try:
        with _target_platform_override(ctx):
            subprocess.run(chroot_cmd, check=True)
    finally:
        if log_bind_mounted:
            subprocess.run(["umount", str(target_log)], check=False, capture_output=True)
            try:
                shutil.copy2(ctx.log_path, target_log)
                target_log.chmod(0o644)
            except OSError:
                pass
        else:
            try:
                with ctx.log_path.open("a", encoding="utf-8") as live_log:
                    live_log.write("\n=== Target setup log ===\n")
                    live_log.write(target_log.read_text(errors="ignore"))
            except OSError:
                pass


def run_system_finalizer(ctx: InstallContext) -> None:
    if ctx.defer_provisioning:
        cmd = ["/usr/bin/omarchy-apply-system", "--defer-provisioning", "--first-install"]
    else:
        cmd = ["/usr/bin/omarchy-apply-system", "--install-user", ctx.username, "--first-install"]

    _mask_mkinitcpio_pacman_hooks(ctx, ctx.target, TARGET_DEFERRED_BOOT_HOOKS)
    try:
        _run_target_setup_command(ctx, cmd)
    finally:
        _unmask_mkinitcpio_pacman_hooks(ctx, ctx.target, TARGET_DEFERRED_BOOT_HOOKS)


# ─────────────────────────────────────────────────────────────────────────────
# stage_provisioning_state: produce the on-disk "provisioning state" the runtime's first-boot
# setup (omarchy-provision-owner) and factory reset (omarchy-system-factory-reset) consume.
#
# Every install stashes the bundled Node tarball in /var/lib/omarchy/provisioning/
# so a later factory reset can finalize the new owner's user offline. deferred-provisioning
# installs additionally arm the first-boot setup service and, on encrypted
# targets, stage the throwaway LUKS passphrase: the keyfile embedded in the
# initramfs auto-unlocks boot during the provisioning window, and first-boot setup
# re-keys the volume to the owner's password and removes it.
#
# Runs before finalize_limine_boot so the cryptkey cmdline drop-in and the
# keyfile land in the final UKI build.
# ─────────────────────────────────────────────────────────────────────────────

PROVISION_STATE_DIR = "var/lib/omarchy/provisioning"
PROVISION_KEYFILE = "etc/omarchy/provisioning.key"
NODE_PACKAGES_DIR = Path("/opt/packages")


def stage_provisioning_state(ctx: InstallContext) -> None:
    # World-readable: first-boot finalization reads the Node tarball as the
    # new user. The only secret inside (luks-key) is itself 0600 root.
    provisioning_dir = ctx.target / PROVISION_STATE_DIR
    provisioning_dir.mkdir(parents=True, exist_ok=True)
    provisioning_dir.chmod(0o755)

    _stage_node_tarball(ctx, provisioning_dir)

    if not ctx.defer_provisioning:
        return

    service_src = ctx.target / "usr/share/omarchy/install/provisioning/omarchy-provision-owner.service"
    setup_bin = ctx.target / "usr/bin/omarchy-provision-owner"
    if not service_src.exists() or not setup_bin.exists():
        raise RuntimeError(
            "deferred-provisioning install requested, but the installed Omarchy runtime does not ship "
            "first-boot setup (omarchy-provision-owner + install/provisioning/omarchy-provision-owner.service). "
            "Update the runtime package this ISO bundles before installing in deferred provisioning."
        )

    info("› arming first-boot setup")
    (provisioning_dir / "pending").touch()

    unit_dst = ctx.target / "etc/systemd/system/omarchy-provision-owner.service"
    unit_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(service_src, unit_dst)
    wants_dir = ctx.target / "etc/systemd/system/multi-user.target.wants"
    wants_dir.mkdir(parents=True, exist_ok=True)
    link = wants_dir / "omarchy-provision-owner.service"
    link.unlink(missing_ok=True)
    link.symlink_to("/etc/systemd/system/omarchy-provision-owner.service")

    if _provision_install_encrypted(ctx):
        _stage_provisioning_luks_unlock(ctx, provisioning_dir)


def _stage_node_tarball(ctx: InstallContext, provisioning_dir) -> None:
    tarballs = sorted(NODE_PACKAGES_DIR.glob(_node_tarball_pattern()))
    if not tarballs:
        # Hard error on every install, not just deferred-provisioning installs: the stash is what lets a
        # later factory reset finalize the next owner offline, and an ISO
        # build always bundles the tarball — its absence means a broken build.
        raise RuntimeError(
            f"no bundled Node tarball in {NODE_PACKAGES_DIR} — first-boot setup "
            "and factory reset could not finalize a user offline"
        )

    packages_dir = provisioning_dir / "packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    target_tarball = packages_dir / tarballs[0].name
    if not target_tarball.exists():
        info("› stashing Node tarball for offline first-boot setup")
        shutil.copy2(tarballs[0], target_tarball)


def _node_tarball_pattern(machine: str | None = None) -> str:
    machine = (machine or os.uname().machine).lower()
    if machine in {"aarch64", "arm64"}:
        return "node-v*-linux-arm64.tar.gz"
    if machine == "x86_64":
        return "node-v*-linux-x64.tar.gz"
    raise RuntimeError(f"Unsupported Node bundle architecture: {machine}")


def _provision_install_encrypted(ctx: InstallContext) -> bool:
    if _storage_intent(ctx).get("luks_uuid"):
        return True
    disk_encryption = (ctx.user_configuration.get("disk_config") or {}).get("disk_encryption")
    if disk_encryption and disk_encryption.get("encryption_type", "luks") != "no_encryption":
        return True
    return ctx.encrypt


def _provision_encryption_password(ctx: InstallContext) -> str | None:
    disk_encryption = (ctx.user_configuration.get("disk_config") or {}).get("disk_encryption") or {}
    return disk_encryption.get("encryption_password") or ctx.user_credentials.get("encryption_password")


def _stage_provisioning_luks_unlock(ctx: InstallContext, provisioning_dir) -> None:
    password = _provision_encryption_password(ctx)
    if not password:
        # Full-disk deferred-provisioning installs get a generated passphrase injected by
        # InstallContext; only a pre-mounted (rig-partitioned) LUKS target can
        # land here, and it must hand over the passphrase it formatted with.
        raise RuntimeError(
            "deferred-provisioning install on a pre-encrypted target requires the LUKS passphrase "
            "in user_credentials.json (encryption_password) so first boot can re-key"
        )

    info("› staging LUKS auto-unlock for the provisioning window")

    # Byte-for-byte the slot passphrase: no trailing newline anywhere.
    luks_key = provisioning_dir / "luks-key"
    luks_key.write_text(password)
    luks_key.chmod(0o600)

    keyfile = ctx.target / PROVISION_KEYFILE
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    keyfile.write_text(password)
    keyfile.chmod(0o600)

    cmdline_dropin = ctx.target / "etc/limine-entry-tool.d/99-omarchy-provisioning-unlock.conf"
    cmdline_dropin.parent.mkdir(parents=True, exist_ok=True)
    cmdline_dropin.write_text(
        'KERNEL_CMDLINE[default]+=" cryptkey=rootfs:/etc/omarchy/provisioning.key"\n'
    )

    files_dropin = ctx.target / "etc/mkinitcpio.conf.d/99-omarchy-provisioning-key.conf"
    files_dropin.parent.mkdir(parents=True, exist_ok=True)
    files_dropin.write_text("FILES+=(/etc/omarchy/provisioning.key)\n")
































def _read_omarchy_mirror() -> str:
    p = Path("/root/omarchy_mirror")
    return p.read_text().strip() if p.exists() else "stable"


# ─────────────────────────────────────────────────────────────────────────────
# configure_login: seed SDDM's last user/session for the password-only Omarchy
# greeter. Encrypted installs autologin because the LUKS prompt is the auth
# boundary; unencrypted installs leave SDDM as the auth screen.
# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# configure_ssh_access: make the installed machine reachable over SSH with the
# keys an autoinstall drive supplied. A stock Omarchy install ships openssh but
# leaves sshd disabled, and its firewall.sh opens only LocalSend and docker DNS,
# so all three pieces -- keys, service, firewall -- have to be done here.
# ─────────────────────────────────────────────────────────────────────────────





# ─────────────────────────────────────────────────────────────────────────────
# configure_tailscale: stage the tailnet join an autoinstall drive asked for.
# `tailscale up` needs a running tailscaled and there is no systemd in the
# chroot, so the install only stages: the key, the enabled services, and a
# oneshot first-boot unit that performs the join once the network is really
# there. The package itself was installed from the offline mirror during
# arch_install_system -- nothing is fetched at boot.
# ─────────────────────────────────────────────────────────────────────────────


# systemd expands $VAR in ExecStart, so the retry loop avoids `$` entirely.
# network-online.target can be reached before there is real connectivity, so
# retry inside the boot -- but NOT as a oneshot: target units implicitly gain
# After= for their Wants=, so a oneshot in multi-user.target holds the whole
# boot (SDDM included) hostage until it finishes. Type=simple counts as
# started the moment it forks, letting boot proceed while the join retries in
# the background for as long as the boot lasts. Cleanup lives inside the
# script because it must only run after a successful join: the key is removed
# and the unit disabled on success, while on a boot with no connectivity both
# survive -- so a machine installed offline joins on the first boot that can.






# ─────────────────────────────────────────────────────────────────────────────
# validate_boot: hard checks before reboot. If the install ran but produced a
# boot config or UKI that can't actually boot, halt here rather than surprise
# the user.
# ─────────────────────────────────────────────────────────────────────────────











# Every kernel package leaves its pkgbase next to its modules, which is also
# the name limine-mkinitcpio-hook builds the UKI under.




# ─────────────────────────────────────────────────────────────────────────────
# create_factory_snapshot: read-only snapshot of @ kept at the btrfs top level
# as @factory — outside snapper's .snapshots, so cleanup timers and the Limine
# snapshot menu never touch it. Zero bytes at creation; grows only with drift.
# Taken at the end of every install, it is what makes omarchy-system-factory-reset a
# true factory reset.
# ─────────────────────────────────────────────────────────────────────────────



# Provisioning credentials staged for THIS deployment's first boot must not
# survive into the factory image: a reset years later would otherwise hand the
# next owner the original deployment's SSH keys or rejoin its tailnet, and a
# stale LUKS key (dead after the first re-key) has no business lingering.
# The mkinitcpio/cmdline drop-ins go with the keyfile — a reset rebuild would
# otherwise fail on FILES pointing at a scrubbed path.






CPU_SYSFS = Path("/sys/devices/system/cpu")


def boost_cpu_governor() -> dict[Path, str]:
    """Run the live CPUs flat out for the install.

    Package extraction and the UKI build are both CPU-bound, and archiso boots
    on whatever governor the kernel defaults to. Writing an unsupported
    governor just fails, so nothing needs probing first, and hosts without
    cpufreq (most VMs) have no paths at all. Returns the prior governors.
    """
    saved: dict[Path, str] = {}
    for path in sorted(CPU_SYSFS.glob("cpu*/cpufreq/scaling_governor")):
        try:
            saved[path] = path.read_text().strip()
            path.write_text("performance\n")
        except OSError:
            saved.pop(path, None)

    if saved:
        info(f"› CPU governor set to performance ({len(saved)} CPUs)")
    return saved


def restore_cpu_governors(saved: dict[Path, str]) -> None:
    """Only matters when an install fails and the user keeps using the live
    environment — a successful one reboots out of it."""
    for path, governor in saved.items():
        try:
            path.write_text(f"{governor}\n")
        except OSError:
            continue


# ─────────────────────────────────────────────────────────────────────────────
# cleanup_bind_mounts: invoked from main()'s finally so bind mounts get
# unwound on success, failure, or interrupt. Idempotent.
# ─────────────────────────────────────────────────────────────────────────────

def cleanup_bind_mounts(ctx: InstallContext) -> None:
    for mount_point in ctx.state.get("bind_mounts", []):
        subprocess.run(["umount", mount_point], check=False, capture_output=True)
    ctx.state["bind_mounts"] = []


def cleanup_target_hook_masks(ctx: InstallContext) -> None:
    """Restore the target's deferred boot hooks. Idempotent, and a no-op when
    nothing was masked, so main()'s finally can call it on any exit path: an
    interrupt must never leave the installed system with its UKI rebuild hook
    pointing at /dev/null."""
    _unmask_mkinitcpio_pacman_hooks(ctx, ctx.target, TARGET_DEFERRED_BOOT_HOOKS)


def cleanup_protected_state(ctx: InstallContext) -> None:
    """Tear down protected-mode mounts and LUKS mapper after a failed install.

    Idempotent and safe to call multiple times. Successful protected installs
    intentionally keep the target mounted until reboot.
    """
    if not ctx.is_protected:
        return

    subprocess.run(["umount", "-R", str(ctx.target)], check=False, capture_output=True)
    if Path("/dev/mapper/omarchy_root").exists():
        subprocess.run(
            ["cryptsetup", "close", "omarchy_root"],
            check=False,
            capture_output=True,
        )
