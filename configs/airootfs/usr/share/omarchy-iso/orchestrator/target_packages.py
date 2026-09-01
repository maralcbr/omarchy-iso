"""The one definition of the packages a configured install installs.

Two consumers need the same answer and must never be allowed to drift:

* the orchestrator itself, which installs the early bootstrap set, the
  LuaRocks prerequisites, the user seed packages, the Omarchy runtime list and
  the conditional Tailscale package while configuring a target;
* the media builder, which resolves that same target set against the verified
  offline repository and ships the resolved closure so the finished target's
  installed inventory can be compared to it exactly.

Everything here is pure: no environment, no filesystem, no archinstall import.
Callers supply the two package lists the media build already produces plus the
options that decide the conditionals, and get back the complete set of package
names the install will ask pacman for. Dependencies are deliberately NOT
resolved here — pacman resolves them from the verified repository.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


BOOT_BACKENDS = ("limine", "asahi-grub")

# Media targets whose installs boot through the Asahi GRUB backend. Every other
# media target uses the PC/UEFI Limine backend.
MEDIA_TARGET_BOOT_BACKENDS = {"aarch64/apple-silicon": "asahi-grub"}
DEFAULT_BOOT_BACKEND = "limine"

# Packages installed BEFORE useradd. The selected omarchy-settings package and
# omarchy-nvim populate /etc/skel so the user's home gets seeded correctly, and
# omarchy-settings also ships the limine/snapper configs. Target-side setup
# commands are installed later by the selected Omarchy runtime package and
# executed in chroot.
EARLY_LIMINE_BOOTSTRAP_PACKAGES = [
    "base-devel",
    "git",
    "limine",
    "limine-mkinitcpio-hook",
    "limine-snapper-sync",
    "snapper",
    "efibootmgr",
    "omarchy-keyring",
]

EARLY_ASAHI_GRUB_BOOTSTRAP_PACKAGES = [
    "base-devel",
    "btrfs-progs",
    "git",
    "grub",
    "omarchy-keyring",
]

# Compatibility name retained for tests and downstream imports that describe
# the default PC/UEFI package set.
EARLY_BOOTSTRAP_BASE_PACKAGES = EARLY_LIMINE_BOOTSTRAP_PACKAGES

# Install LuaRocks before omarchy-nvim pulls in lua51-lpeg. Arch's lua-luarocks
# post_install script tries to rebuild manifests for existing rocks trees before
# the unversioned luarocks-admin command exists if both arrive in the wrong
# transaction order. Splitting this transaction avoids the harmless but noisy
# "luarocks-admin: command not found" line during ISO installs.
EARLY_LUAROCKS_PACKAGES = [
    "lua51",
    "luarocks",
]

# Installed only when an autoinstall drive staged an auth key for first boot.
TAILSCALE_PACKAGES = ["tailscale"]

_BOOTSTRAP_PACKAGES_BY_BACKEND = {
    "limine": EARLY_LIMINE_BOOTSTRAP_PACKAGES,
    "asahi-grub": EARLY_ASAHI_GRUB_BOOTSTRAP_PACKAGES,
}

# The archinstall package list is the media build's declaration of the packages
# the archinstall side of an install carries. Two classes of entry in it must
# not be read as "this install installs it".
#
# Decided elsewhere in the flow — the list alone must not imply them:
#   efibootmgr  boot-manager registration, installed only by the platform
#               bootstrap set that selects it (the Limine set does; the Asahi
#               GRUB set boots through its own updater and does not).
#   tailscale   installed only when an auth key is staged for first boot.
ARCHINSTALL_LIST_PACKAGES_DECIDED_ELSEWHERE = frozenset({"efibootmgr", "tailscale"})

# Carried so the offline mirror holds it, but installed by no path in the flow:
#   alsa-firmware  PC audio firmware. Its sibling sof-firmware is already
#                  dropped for Apple Silicon by the platform package filter;
#                  this one is installed on neither platform.
ARCHINSTALL_LIST_MIRROR_ONLY_PACKAGES = frozenset({"alsa-firmware"})

# Packages archinstall installs itself, over and above the declared lists,
# because of the install configuration the media build writes. Each collection
# names only the members no declared list already carries — the declared lists
# stay the source for everything they do carry.
#
# Swap: archinstall's setup_swap installs the zram generator (the orchestrator
# then drops the generic /etc config it writes, keeping the package).
ARCHINSTALL_SWAP_PACKAGES = ("zram-generator",)

# Audio: archinstall's audio configuration installs its backend package set.
ARCHINSTALL_AUDIO_PACKAGES = {"pipewire": ("gst-plugin-pipewire",)}

# Packages the target's own system finalizer installs while configuring the
# platform in chroot. The Apple Silicon path routes through Apple hardware
# setup, which installs the Asahi Vulkan driver and its implicit layers. Listed
# here because the installing script belongs to the Omarchy runtime package,
# which declares no list this build can read.
SYSTEM_FINALIZER_PLATFORM_PACKAGES = {
    "limine": (),
    "asahi-grub": ("vulkan-asahi", "vulkan-mesa-implicit-layers"),
}

# The archinstall configuration the media build writes for a full-OS package
# build. Kept here so the expected target set and the generated configuration
# cannot describe different installs; a unit test pins the generator to it.
MEDIA_BUILD_INSTALL_CONFIGURATION = {"swap": True, "audio": "pipewire"}


class TargetPackageError(RuntimeError):
    """Raised when a package plan cannot describe a real install."""


def boot_backend_for_media_target(media_target: str) -> str:
    return MEDIA_TARGET_BOOT_BACKENDS.get(media_target, DEFAULT_BOOT_BACKEND)


def _validated_backend(boot_backend: str) -> str:
    if boot_backend not in BOOT_BACKENDS:
        raise TargetPackageError(f"Unsupported Omarchy boot backend: {boot_backend}")
    return boot_backend


@dataclass(frozen=True)
class TargetPackagePlan:
    """Everything that decides which packages a configured install installs.

    ``archinstall_packages`` and ``base_packages`` are the two package lists the
    media build has already produced and platform-filtered; the remaining
    fields are the install options whose values the conditionals read.
    """

    boot_backend: str
    runtime_package: str
    settings_package: str
    nvim_package: str
    archinstall_packages: Sequence[str] = field(default_factory=tuple)
    base_packages: Sequence[str] = field(default_factory=tuple)
    swap_enabled: bool = bool(MEDIA_BUILD_INSTALL_CONFIGURATION["swap"])
    audio: str | None = MEDIA_BUILD_INSTALL_CONFIGURATION["audio"]
    tailscale_enabled: bool = False

    def __post_init__(self) -> None:
        _validated_backend(self.boot_backend)
        if self.audio is not None and self.audio not in ARCHINSTALL_AUDIO_PACKAGES:
            raise TargetPackageError(f"Unsupported audio backend: {self.audio}")


def early_bootstrap_packages(boot_backend: str, settings_package: str) -> list[str]:
    """Packages installed before useradd, for the selected boot backend."""
    base = _BOOTSTRAP_PACKAGES_BY_BACKEND[_validated_backend(boot_backend)]
    return [*base, settings_package]


def early_user_seed_packages(nvim_package: str) -> list[str]:
    return [nvim_package]


def early_packages(
    boot_backend: str,
    settings_package: str,
    nvim_package: str,
) -> list[str]:
    return [
        *early_bootstrap_packages(boot_backend, settings_package),
        *EARLY_LUAROCKS_PACKAGES,
        *early_user_seed_packages(nvim_package),
    ]


def runtime_package_list(
    base_packages: Iterable[str],
    *,
    boot_backend: str,
    runtime_package: str,
    settings_package: str,
    nvim_package: str,
) -> list[str]:
    """Selected Omarchy runtime package + every package in the ISO-bundled
    base package list that isn't already installed early."""
    packages = [runtime_package]
    already_installed = set(
        early_packages(boot_backend, settings_package, nvim_package)
    ) | {
        runtime_package,
        settings_package,
        nvim_package,
        "omarchy",
        "omarchy-settings",
        "omarchy-nvim",
    }
    for name in base_packages:
        if name not in already_installed and name not in packages:
            packages.append(name)
    return packages


def archinstall_list_packages(
    archinstall_packages: Iterable[str],
) -> list[str]:
    """The archinstall list entries this flow really installs."""
    excluded = (
        ARCHINSTALL_LIST_PACKAGES_DECIDED_ELSEWHERE
        | ARCHINSTALL_LIST_MIRROR_ONLY_PACKAGES
    )
    return [name for name in archinstall_packages if name not in excluded]


def archinstall_implicit_packages(plan: TargetPackagePlan) -> list[str]:
    """Packages archinstall installs because of the install configuration."""
    packages: list[str] = []
    if plan.swap_enabled:
        packages.extend(ARCHINSTALL_SWAP_PACKAGES)
    if plan.audio is not None:
        packages.extend(ARCHINSTALL_AUDIO_PACKAGES[plan.audio])
    return packages


def expected_package_targets(plan: TargetPackagePlan) -> list[str]:
    """Every package name a configured install asks pacman for, sorted.

    The result is the resolver's target list: pacman turns it into the exact
    closure the finished target must match.
    """
    _validated_backend(plan.boot_backend)
    targets: set[str] = set()
    targets.update(archinstall_list_packages(plan.archinstall_packages))
    targets.update(
        runtime_package_list(
            plan.base_packages,
            boot_backend=plan.boot_backend,
            runtime_package=plan.runtime_package,
            settings_package=plan.settings_package,
            nvim_package=plan.nvim_package,
        )
    )
    targets.update(
        early_packages(
            plan.boot_backend,
            plan.settings_package,
            plan.nvim_package,
        )
    )
    targets.update(archinstall_implicit_packages(plan))
    targets.update(SYSTEM_FINALIZER_PLATFORM_PACKAGES[plan.boot_backend])
    if plan.tailscale_enabled:
        targets.update(TAILSCALE_PACKAGES)
    return sorted(targets)


def read_package_list(path: Path) -> list[str]:
    """Read one package name per line, ignoring blanks and comments."""
    names: list[str] = []
    for raw in path.read_text().splitlines():
        name = raw.strip()
        if not name or name.startswith("#"):
            continue
        if name not in names:
            names.append(name)
    return names


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Print the expected package targets.")
    parser.add_argument("--media-target", required=True)
    parser.add_argument("--archinstall-packages", type=Path, required=True)
    parser.add_argument("--base-packages", type=Path, required=True)
    parser.add_argument("--runtime-package", required=True)
    parser.add_argument("--settings-package", required=True)
    parser.add_argument("--nvim-package", required=True)
    parser.add_argument("--tailscale-authkey-staged", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    plan = TargetPackagePlan(
        boot_backend=boot_backend_for_media_target(arguments.media_target),
        runtime_package=arguments.runtime_package,
        settings_package=arguments.settings_package,
        nvim_package=arguments.nvim_package,
        archinstall_packages=read_package_list(arguments.archinstall_packages),
        base_packages=read_package_list(arguments.base_packages),
        tailscale_enabled=arguments.tailscale_authkey_staged,
    )
    for name in expected_package_targets(plan):
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
