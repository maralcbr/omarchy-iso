from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MEDIA_SOURCE = ROOT / "configs/airootfs/usr/share/omarchy-iso"
sys.path.insert(0, str(MEDIA_SOURCE))
sys.modules.setdefault(
    "orchestrator.archinstall_adapter", types.ModuleType("orchestrator.archinstall_adapter")
)

from orchestrator import phases_impl  # noqa: E402


def _context(target: Path, kernel: str) -> SimpleNamespace:
    return SimpleNamespace(
        target=target,
        omarchy_install={"storage": {"kernel": kernel}},
        user_configuration={"kernels": [kernel]},
    )


class ArmPackageRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.target = self.root / "target"
        self.media.mkdir()
        (self.target / "etc").mkdir(parents=True)
        self.ctx = _context(self.target, "linux-asahi")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_media(self, *, aurora_config: str | None) -> None:
        inputs = {
            "arm-repository": "repository record\n",
            "arm-runtime": "runtime record\n",
            "arm-runtime-channel": "format=1\nsequence=7\ntag=asahi-quattro-dddddddd\n",
            "pacman-online-installed-arm.conf": (
                MEDIA_SOURCE / "pacman-online-installed-arm.conf"
            ).read_text(),
            "omarchy-arm-repository.asc": "public key\n",
        }
        if aurora_config is not None:
            inputs["pacman-online-installed-arm-aurora.conf"] = aurora_config
        for name, content in inputs.items():
            (self.media / name).write_text(content)

    def test_non_arm_media_leaves_target_unchanged(self) -> None:
        with patch.dict(os.environ, {"OMARCHY_ISO_MEDIA_ROOT": str(self.media)}), patch(
            "subprocess.run"
        ) as run:
            phases_impl.configure_arm_package_repository(self.ctx)

        run.assert_not_called()
        self.assertFalse((self.target / "etc/pacman.conf").exists())

    def test_arm_media_installs_pinned_config_key_and_records(self) -> None:
        inputs = {
            "arm-repository": "repository record\n",
            "arm-runtime": "runtime record\n",
            "arm-runtime-channel": "format=1\nsequence=7\ntag=asahi-quattro-dddddddd\n",
            "pacman-online-installed-arm.conf": "[options]\nArchitecture = aarch64\n",
            "omarchy-arm-repository.asc": "public key\n",
        }
        for name, content in inputs.items():
            (self.media / name).write_text(content)
        # What the build-time offline repository leaves behind, and nothing for
        # the repositories the installed configuration names.
        sync_dir = self.target / "var/lib/pacman/sync"
        sync_dir.mkdir(parents=True)
        (sync_dir / "offline.db").write_bytes(b"build-time database")
        (sync_dir / "omarchy.db").write_bytes(b"stale pin database")

        with patch.dict(os.environ, {"OMARCHY_ISO_MEDIA_ROOT": str(self.media)}), patch(
            "subprocess.run"
        ) as run:
            phases_impl.configure_arm_package_repository(self.ctx)

        self.assertEqual(
            (self.target / "etc/pacman.conf").read_text(),
            inputs["pacman-online-installed-arm.conf"],
        )
        self.assertEqual(
            (self.target / "var/lib/omarchy/package-snapshots/ARM-REPOSITORY").read_text(),
            inputs["arm-repository"],
        )
        self.assertEqual(
            (self.target / "var/lib/omarchy/package-snapshots/ARM-RUNTIME").read_text(),
            inputs["arm-runtime"],
        )
        self.assertEqual(
            (self.target / "usr/share/omarchy/omarchy-arm-repository.asc").read_text(),
            inputs["omarchy-arm-repository.asc"],
        )
        release_state = self.target / "var/lib/omarchy/asahi-quattro-release"
        self.assertEqual(release_state.read_text(), inputs["arm-runtime-channel"])
        self.assertEqual(release_state.stat().st_mode & 0o777, 0o644)
        self.assertEqual(list(sync_dir.iterdir()), [])
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[0][2:4], ["pacman-key", "--add"])
        self.assertEqual(run.call_args_list[1].args[0][-2:], [
            "--lsign-key",
            "C81AC3E2A99556F9B21D5FEA3DD49BC9F8360BDC",
        ])
        self.assertEqual(
            run.call_args_list[2].args[0],
            ["pacman", "--sysroot", str(self.target), "--disable-sandbox", "--noconfirm", "-Sy"],
        )

    def test_arm_marker_requires_every_pinned_input(self) -> None:
        (self.media / "arm-repository").write_text("repository record\n")

        with patch.dict(os.environ, {"OMARCHY_ISO_MEDIA_ROOT": str(self.media)}):
            with self.assertRaisesRegex(RuntimeError, "ARM package input is missing"):
                phases_impl.configure_arm_package_repository(self.ctx)

    def test_asahi_install_keeps_the_generic_configuration(self) -> None:
        # The stage projection carries both tracked files into every media root.
        self.write_media(
            aurora_config=(MEDIA_SOURCE / "pacman-online-installed-arm-aurora.conf").read_text()
        )

        with patch.dict(os.environ, {"OMARCHY_ISO_MEDIA_ROOT": str(self.media)}), patch(
            "subprocess.run"
        ):
            phases_impl.configure_arm_package_repository(self.ctx)

        self.assertEqual(
            (self.target / "etc/pacman.conf").read_bytes(),
            (MEDIA_SOURCE / "pacman-online-installed-arm.conf").read_bytes(),
        )

    def test_aurora_install_keeps_the_aurora_repository(self) -> None:
        aurora_config = (MEDIA_SOURCE / "pacman-online-installed-arm-aurora.conf").read_text()
        self.write_media(aurora_config=aurora_config)
        contexts = {
            "storage intent": _context(self.target, "linux-aurora"),
            "configured kernels": SimpleNamespace(
                target=self.target,
                omarchy_install={},
                user_configuration={"kernels": ["linux-aurora"]},
            ),
        }
        for name, ctx in contexts.items():
            with self.subTest(kernel_source=name):
                (self.target / "etc/pacman.conf").unlink(missing_ok=True)
                with patch.dict(
                    os.environ, {"OMARCHY_ISO_MEDIA_ROOT": str(self.media)}
                ), patch("subprocess.run"):
                    phases_impl.configure_arm_package_repository(ctx)

                installed = (self.target / "etc/pacman.conf").read_text()
                self.assertEqual(installed, aurora_config)
                self.assertLess(
                    installed.index("[omarchy-aurora]\n"), installed.index("[omarchy]\n")
                )

    def test_aurora_install_without_aurora_configuration_fails_closed(self) -> None:
        self.write_media(aurora_config=None)

        with patch.dict(os.environ, {"OMARCHY_ISO_MEDIA_ROOT": str(self.media)}), patch(
            "subprocess.run"
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "Aurora package input is missing"):
                phases_impl.configure_arm_package_repository(
                    _context(self.target, "linux-aurora")
                )

        run.assert_not_called()
        self.assertFalse((self.target / "etc/pacman.conf").exists())

    def test_aurora_install_rejects_a_configuration_without_its_repository_first(self) -> None:
        aurora_config = (MEDIA_SOURCE / "pacman-online-installed-arm-aurora.conf").read_text()
        aurora_section = aurora_config[
            aurora_config.index("[omarchy-aurora]\n") : aurora_config.index("[omarchy]\n")
        ]
        generic = (MEDIA_SOURCE / "pacman-online-installed-arm.conf").read_text()
        cases = {
            "generic configuration": generic,
            "behind omarchy": aurora_config.replace(aurora_section, "") + "\n" + aurora_section,
            "plain http": aurora_config.replace(
                "Server = https://github.com/maralcbr/omarchy-pkgs/releases/download/aurora-",
                "Server = http://github.com/maralcbr/omarchy-pkgs/releases/download/aurora-",
            ),
        }
        for name, content in cases.items():
            with self.subTest(case=name):
                self.write_media(aurora_config=content)
                with patch.dict(
                    os.environ, {"OMARCHY_ISO_MEDIA_ROOT": str(self.media)}
                ), patch("subprocess.run") as run:
                    with self.assertRaisesRegex(RuntimeError, r"\[omarchy-aurora\]"):
                        phases_impl.configure_arm_package_repository(
                            _context(self.target, "linux-aurora")
                        )
                run.assert_not_called()
                self.assertFalse((self.target / "etc/pacman.conf").exists())


if __name__ == "__main__":
    unittest.main()
