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
sys.path.insert(0, str(ROOT / "configs/airootfs/usr/share/omarchy-iso"))
sys.modules.setdefault(
    "orchestrator.archinstall_adapter", types.ModuleType("orchestrator.archinstall_adapter")
)

from orchestrator import phases_impl  # noqa: E402


class ArmPackageRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.target = self.root / "target"
        self.media.mkdir()
        (self.target / "etc").mkdir(parents=True)
        self.ctx = SimpleNamespace(target=self.target)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_non_arm_media_leaves_target_unchanged(self) -> None:
        with patch.dict(os.environ, {"OMARCHY_ISO_MEDIA_ROOT": str(self.media)}), patch(
            "subprocess.run"
        ) as run:
            phases_impl.configure_arm_package_repository(self.ctx)

        run.assert_not_called()
        self.assertFalse((self.target / "etc/pacman.conf").exists())

    def test_candidate_does_not_claim_old_runtime_release(self) -> None:
        for name in ("arm-repository", "arm-runtime", "arm-runtime-channel",
                     "pacman-online-installed-arm.conf", "omarchy-arm-repository.asc"):
            (self.media / name).write_text("baseline fixture")
        (self.media / "candidate-signing.json").write_text('{"candidate_only": true}')
        with patch.dict(os.environ, {"OMARCHY_ISO_MEDIA_ROOT": str(self.media)}), patch("subprocess.run"):
            phases_impl.configure_arm_package_repository(self.ctx)
        state = self.target / "var/lib/omarchy"
        self.assertFalse((state / "asahi-quattro-release").exists())
        self.assertFalse((state / "package-snapshots/ARM-RUNTIME").exists())
        self.assertEqual((state / "package-snapshots/QUATTRO-CANDIDATE.json").read_text(),
                         '{"candidate_only": true}')

    def test_dependency_snapshot_does_not_install_inherited_feed(self) -> None:
        for name in ("dependency-manifest.json", "dependency-manifest.json.sig", "candidate-signing.json"):
            (self.media / name).write_text("fixture")
        config = "[omarchy-aarch64]\nServer = https://example.invalid/edge\n"
        (self.media / "pacman-online-installed-quattro.conf").write_text(config)
        with patch.dict(os.environ, {"OMARCHY_ISO_MEDIA_ROOT": str(self.media)}), patch("subprocess.run") as run:
            phases_impl.configure_arm_package_repository(self.ctx)
        self.assertEqual((self.target / "etc/pacman.conf").read_text(), config)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("pacman-key", str(run.call_args))
        state = self.target / "var/lib/omarchy/package-snapshots"
        self.assertTrue((state / "DEPENDENCIES.json.sig").is_file())
        self.assertFalse((state / "ARM-REPOSITORY").exists())

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


if __name__ == "__main__":
    unittest.main()
