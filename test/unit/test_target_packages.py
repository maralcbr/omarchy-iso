"""The expected package target set has exactly one definition.

The regression these tests pin is the measured nine-name divergence between the
packages an Apple Silicon diagnostic build actually installed and the closure
the media builder expected: btrfs-progs, gst-plugin-pipewire, vulkan-asahi,
vulkan-mesa-implicit-layers and zram-generator were installed but not expected;
alsa-firmware, efibootmgr, efivar and tailscale were expected but not
installed. efivar is a dependency of efibootmgr and leaves the resolved closure
with it, so the target-set fixtures below cover the other eight names.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MEDIA_SOURCE_ROOT = ROOT / "configs/airootfs/usr/share/omarchy-iso"
sys.path.insert(0, str(MEDIA_SOURCE_ROOT))

from orchestrator import target_packages  # noqa: E402

APPLE_MEDIA_TARGET = "aarch64/apple-silicon"

# The measured divergence, by side.
INSTALLED_ONLY = (
    "btrfs-progs",
    "gst-plugin-pipewire",
    "vulkan-asahi",
    "vulkan-mesa-implicit-layers",
    "zram-generator",
)
EXPECTED_ONLY_TARGETS = ("alsa-firmware", "efibootmgr", "tailscale")

# A stand-in for the base package list the media build ships. Only its shape
# matters: the fixtures assert about names the orchestrator adds or withholds.
BASE_PACKAGES = (
    "base",
    "grub",
    "linux-asahi",
    "mkinitcpio",
    "openssh",
    "pipewire",
    "systemd",
)


def filtered_archinstall_packages(media_target: str, arch: str) -> list[str]:
    """Run the real platform package filter over the real archinstall list."""
    bash = os.environ.get("OMARCHY_TEST_BASH") or "bash"
    script = (
        "source builder/package-architecture.sh\n"
        "filter_target_packages < builder/archinstall.packages\n"
    )
    result = subprocess.run(
        [bash, "-c", script],
        cwd=ROOT,
        env={
            **os.environ,
            "OMARCHY_MEDIA_TARGET": media_target,
            "OMARCHY_ARCH": arch,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def apple_plan(**overrides) -> target_packages.TargetPackagePlan:
    fields = {
        "boot_backend": "asahi-grub",
        "runtime_package": "omarchy-dev",
        "settings_package": "omarchy-settings-dev",
        "nvim_package": "omarchy-nvim",
        "archinstall_packages": filtered_archinstall_packages(
            APPLE_MEDIA_TARGET, "aarch64"
        ),
        "base_packages": BASE_PACKAGES,
    }
    fields.update(overrides)
    return target_packages.TargetPackagePlan(**fields)


class AppleSiliconTargetSetTests(unittest.TestCase):
    def test_diagnostic_build_target_set_is_exactly_the_measured_set(self) -> None:
        targets = target_packages.expected_package_targets(apple_plan())

        self.assertEqual(
            targets,
            sorted(
                {
                    # archinstall list entries this flow really installs
                    "base",
                    "base-devel",
                    "linux-asahi",
                    "linux-firmware",
                    "grub",
                    "omarchy-keyring",
                    "openssh",
                    "pipewire",
                    # early bootstrap, LuaRocks prerequisites, user seed
                    "btrfs-progs",
                    "git",
                    "lua51",
                    "luarocks",
                    "omarchy-nvim",
                    "omarchy-settings-dev",
                    # runtime list
                    "omarchy-dev",
                    "mkinitcpio",
                    "systemd",
                    # archinstall's own install configuration
                    "zram-generator",
                    "gst-plugin-pipewire",
                    # the target's Apple platform system finalizer
                    "vulkan-asahi",
                    "vulkan-mesa-implicit-layers",
                }
            ),
        )

    def test_every_installed_only_package_is_now_a_target(self) -> None:
        targets = set(target_packages.expected_package_targets(apple_plan()))

        for name in INSTALLED_ONLY:
            with self.subTest(package=name):
                self.assertIn(name, targets)

    def test_expected_only_packages_are_no_longer_targets(self) -> None:
        targets = set(target_packages.expected_package_targets(apple_plan()))

        for name in EXPECTED_ONLY_TARGETS:
            with self.subTest(package=name):
                self.assertNotIn(name, targets)

    def test_btrfs_progs_comes_from_the_asahi_bootstrap_set(self) -> None:
        self.assertIn("btrfs-progs", target_packages.EARLY_ASAHI_GRUB_BOOTSTRAP_PACKAGES)
        self.assertNotIn("btrfs-progs", target_packages.EARLY_LIMINE_BOOTSTRAP_PACKAGES)

    def test_efibootmgr_is_decided_by_the_bootstrap_set_not_the_list(self) -> None:
        archinstall_packages = filtered_archinstall_packages(
            APPLE_MEDIA_TARGET, "aarch64"
        )
        self.assertIn("efibootmgr", archinstall_packages)

        asahi = set(target_packages.expected_package_targets(apple_plan()))
        limine = set(
            target_packages.expected_package_targets(
                apple_plan(boot_backend="limine")
            )
        )

        self.assertNotIn("efibootmgr", asahi)
        self.assertIn("efibootmgr", limine)
        self.assertIn("efibootmgr", target_packages.EARLY_LIMINE_BOOTSTRAP_PACKAGES)

    def test_tailscale_is_a_target_only_when_an_auth_key_is_staged(self) -> None:
        without_key = set(target_packages.expected_package_targets(apple_plan()))
        with_key = set(
            target_packages.expected_package_targets(
                apple_plan(tailscale_enabled=True)
            )
        )

        self.assertNotIn("tailscale", without_key)
        self.assertIn("tailscale", with_key)
        self.assertEqual(with_key - without_key, {"tailscale"})

    def test_archinstall_implicit_packages_follow_the_install_options(self) -> None:
        without_swap = set(
            target_packages.expected_package_targets(apple_plan(swap_enabled=False))
        )
        without_audio = set(
            target_packages.expected_package_targets(apple_plan(audio=None))
        )
        complete = set(target_packages.expected_package_targets(apple_plan()))

        self.assertEqual(complete - without_swap, {"zram-generator"})
        self.assertEqual(complete - without_audio, {"gst-plugin-pipewire"})

    def test_platform_finalizer_packages_are_apple_silicon_only(self) -> None:
        limine = set(
            target_packages.expected_package_targets(
                apple_plan(boot_backend="limine")
            )
        )

        self.assertTrue(
            {"vulkan-asahi", "vulkan-mesa-implicit-layers"}.isdisjoint(limine)
        )

    def test_selected_package_targets_are_always_present(self) -> None:
        targets = set(
            target_packages.expected_package_targets(
                apple_plan(
                    runtime_package="omarchy",
                    settings_package="omarchy-settings",
                    nvim_package="omarchy-nvim",
                )
            )
        )

        self.assertTrue(
            {"omarchy", "omarchy-settings", "omarchy-nvim"}.issubset(targets)
        )

    def test_media_target_selects_the_boot_backend(self) -> None:
        self.assertEqual(
            target_packages.boot_backend_for_media_target(APPLE_MEDIA_TARGET),
            "asahi-grub",
        )
        self.assertEqual(
            target_packages.boot_backend_for_media_target("x86_64/pc"),
            "limine",
        )

    def test_unsupported_options_are_refused(self) -> None:
        with self.assertRaises(target_packages.TargetPackageError):
            apple_plan(boot_backend="systemd-boot")
        with self.assertRaises(target_packages.TargetPackageError):
            apple_plan(audio="pulseaudio")


class RuntimePackageListTests(unittest.TestCase):
    def test_runtime_list_leads_with_the_runtime_and_skips_early_packages(self) -> None:
        packages = target_packages.runtime_package_list(
            ["base", "git", "omarchy-settings-dev", "systemd", "systemd"],
            boot_backend="asahi-grub",
            runtime_package="omarchy-dev",
            settings_package="omarchy-settings-dev",
            nvim_package="omarchy-nvim",
        )

        self.assertEqual(packages, ["omarchy-dev", "base", "systemd"])

    def test_stable_package_names_are_never_reinstalled_late(self) -> None:
        packages = target_packages.runtime_package_list(
            ["omarchy", "omarchy-settings", "omarchy-nvim", "base"],
            boot_backend="limine",
            runtime_package="omarchy-dev",
            settings_package="omarchy-settings-dev",
            nvim_package="omarchy-nvim",
        )

        self.assertEqual(packages, ["omarchy-dev", "base"])


class SingleDefinitionTests(unittest.TestCase):
    """No consumer may carry a second copy of the package sets."""

    def test_configured_phases_reuses_the_same_objects(self) -> None:
        sys.modules.setdefault(
            "orchestrator.archinstall_adapter",
            types.ModuleType("orchestrator.archinstall_adapter"),
        )
        from orchestrator import configured_phases

        for name in (
            "EARLY_LIMINE_BOOTSTRAP_PACKAGES",
            "EARLY_ASAHI_GRUB_BOOTSTRAP_PACKAGES",
            "EARLY_BOOTSTRAP_BASE_PACKAGES",
            "EARLY_LUAROCKS_PACKAGES",
            "TAILSCALE_PACKAGES",
        ):
            with self.subTest(package_set=name):
                self.assertIs(
                    getattr(configured_phases, name),
                    getattr(target_packages, name),
                )

    def test_configured_phases_declares_no_package_name_literals(self) -> None:
        source = (MEDIA_SOURCE_ROOT / "orchestrator/configured_phases.py").read_text()

        for name in (
            "limine-mkinitcpio-hook",
            "limine-snapper-sync",
            "btrfs-progs",
            "luarocks",
            "lua51",
            "base-devel",
            "omarchy-keyring",
            "tailscale",
        ):
            with self.subTest(package=name):
                self.assertNotIn(f'"{name}"', source)

    def test_the_resolver_asks_the_module_for_its_targets(self) -> None:
        source = (
            ROOT / "builder/asahi-stages/configured-runtime-inputs.sh"
        ).read_text()

        self.assertIn("python3 -m orchestrator.target_packages", source)
        # The hand-union the resolver used to build must not come back: it read
        # the two package lists with a comment-stripping grep and unioned them
        # with the selected package names.
        self.assertNotIn("grep -hv", source)

    def test_the_media_install_configuration_matches_the_generator(self) -> None:
        source = (ROOT / "builder/asahi-stages/image-runtime.sh").read_text()
        configuration = target_packages.MEDIA_BUILD_INSTALL_CONFIGURATION

        self.assertIn(
            f'"audio": "{configuration["audio"]}"',
            source,
        )
        self.assertIn(
            f'"swap": {json.dumps(configuration["swap"])}',
            source,
        )
        backend = target_packages.boot_backend_for_media_target(APPLE_MEDIA_TARGET)
        self.assertIn(f'"backend": "{backend}"', source)


class CommandLineTests(unittest.TestCase):
    def test_the_command_line_prints_the_function_result(self) -> None:
        plan = apple_plan()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archinstall = directory / "archinstall.packages"
            base = directory / "omarchy-base.packages"
            archinstall.write_text(
                "# comment\n\n" + "\n".join(plan.archinstall_packages) + "\n"
            )
            base.write_text("\n".join(plan.base_packages) + "\n")

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "orchestrator.target_packages",
                    "--media-target",
                    APPLE_MEDIA_TARGET,
                    "--archinstall-packages",
                    str(archinstall),
                    "--base-packages",
                    str(base),
                    "--runtime-package",
                    plan.runtime_package,
                    "--settings-package",
                    plan.settings_package,
                    "--nvim-package",
                    plan.nvim_package,
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PYTHONPATH": str(MEDIA_SOURCE_ROOT),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                capture_output=True,
                text=True,
                check=True,
            )

        self.assertEqual(
            result.stdout.splitlines(),
            target_packages.expected_package_targets(plan),
        )

    def test_the_command_line_stages_tailscale_only_on_request(self) -> None:
        plan = apple_plan()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archinstall = directory / "archinstall.packages"
            base = directory / "omarchy-base.packages"
            archinstall.write_text("\n".join(plan.archinstall_packages) + "\n")
            base.write_text("\n".join(plan.base_packages) + "\n")
            arguments = [
                "--media-target",
                APPLE_MEDIA_TARGET,
                "--archinstall-packages",
                str(archinstall),
                "--base-packages",
                str(base),
                "--runtime-package",
                plan.runtime_package,
                "--settings-package",
                plan.settings_package,
                "--nvim-package",
                plan.nvim_package,
            ]

            self.assertNotIn("tailscale", _cli_output(arguments))
            self.assertIn(
                "tailscale",
                _cli_output([*arguments, "--tailscale-authkey-staged"]),
            )


def _cli_output(arguments: list[str]) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "orchestrator.target_packages", *arguments],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(MEDIA_SOURCE_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


class PurityTests(unittest.TestCase):
    def test_the_module_imports_nothing_installation_specific(self) -> None:
        source = (MEDIA_SOURCE_ROOT / "orchestrator/target_packages.py").read_text()
        imports = re.findall(r"(?m)^(?:from|import)\s+([A-Za-z_.]+)", source)

        self.assertTrue(
            set(imports).issubset(
                {"__future__", "argparse", "sys", "dataclasses", "pathlib", "typing"}
            ),
            imports,
        )


if __name__ == "__main__":
    unittest.main()
