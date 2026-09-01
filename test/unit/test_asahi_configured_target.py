from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "builder/capture-asahi-configured-target.py"


def load_module():
    spec = importlib.util.spec_from_file_location("asahi_configured_target", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AsahiConfiguredTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target = self.root / "target"
        self.state = self.root / "state"
        self.runtime = self.root / "runtime"
        for path in (
            self.target / "var/lib/pacman/local",
            self.target / "usr/lib/systemd",
            self.target / "etc/systemd/system/multi-user.target.wants",
            self.target / "var/lib/omarchy/provisioning/packages",
            self.target / "sbin",
            self.state,
            self.runtime,
        ):
            path.mkdir(parents=True, exist_ok=True)

        systemd = self.target / "usr/lib/systemd/systemd"
        systemd.write_bytes(b"systemd")
        systemd.chmod(0o755)
        (self.target / "sbin/init").symlink_to("../lib/systemd/systemd")
        service = self.target / "etc/systemd/system/omarchy-provision-owner.service"
        service.write_text("[Service]\nExecStart=/usr/bin/true\n")
        (self.target / "etc/systemd/system/multi-user.target.wants/omarchy-provision-owner.service").symlink_to(
            "/etc/systemd/system/omarchy-provision-owner.service"
        )
        (self.target / "var/lib/omarchy/provisioning/pending").write_text("")
        node = self.target / "var/lib/omarchy/provisioning/packages/node-v26.8.1-linux-arm64.tar.gz"
        node.write_bytes(b"node-runtime")
        self.node_identity = {
            "schema_version": 1,
            "verification_kind": "pinned-node-lock-v1",
            "filename": node.name,
            "sha256": hashlib.sha256(b"node-runtime").hexdigest(),
            "size_bytes": len(b"node-runtime"),
        }

        self.package_versions = {
            "base": "3-3",
            "bash": "5.3.3-2",
            "grub": "2:2.14-1.1",
            "linux-asahi": "7.1.6.asahi1-1",
            "mkinitcpio": "40-3",
            "omarchy-dev": "4.0.1.r6680.gfe8d2bf-1",
            "omarchy-nvim": "2026.8.1-1",
            "omarchy-settings-dev": "4.0.1.r6680.gfe8d2bf-1",
            "systemd": "261.2-1",
        }
        for name, version in self.package_versions.items():
            package = self.target / "var/lib/pacman/local" / f"{name}-{version}"
            package.mkdir()
            (package / "desc").write_text(
                f"%NAME%\n{name}\n\n%VERSION%\n{version}\n"
            )

        (self.runtime / "package-targets").write_text(
            "OMARCHY_RUNTIME_PACKAGE=omarchy-dev\n"
            "OMARCHY_SETTINGS_PACKAGE=omarchy-settings-dev\n"
            "OMARCHY_NVIM_PACKAGE=omarchy-nvim\n"
        )
        (self.runtime / "omarchy-base.packages").write_text(
            "base\ngrub\nlinux-asahi\nmkinitcpio\nsystemd\n"
        )
        (self.runtime / "expected-packages").write_text(
            f"{len(self.package_versions)}\n"
        )
        (self.runtime / "expected-package-closure").write_text(
            "".join(
                f"{name}\t{version}\n"
                for name, version in sorted(self.package_versions.items())
            )
        )
        self.runtime_manifest = self.module.build_runtime_manifest_for_test(
            self.runtime,
            required=(
                "expected-package-closure",
                "expected-packages",
                "omarchy-base.packages",
                "package-targets",
            ),
            optional=("install-debug",),
        )
        self.product_manifest = self.module.with_digest({
            "schema_version": 1,
            "stage": "configured-target",
            "inputs": {
                "boot_backend": "asahi-grub",
                "boot_filesystem_uuid": "4f4d5801-424f-4f54-8000-000000000001",
                "esp_volume_id": "0x4f4d5801",
                "kernel_package": "linux-asahi",
                "root_filesystem_uuid": "4f4d5801-524f-4f54-8000-000000000001",
            },
        })
        self.repository_manifest = {
            "schema_version": 1,
            "identity": "a" * 64,
            "resolved_closure": [
                {"name": name, "version": version, "filename": f"{name}.pkg.tar.xz"}
                for name, version in sorted(self.package_versions.items())
            ],
            "validation": {"result": "passed", "signatures": "required"},
        }
        self.checkpoint_manifest = {
            "checkpoint_identity": "b" * 64,
            "validation": {"result": "passed"},
            "immutable": True,
            "outputs": [
                {"name": "root-image", "sha256": "1" * 64, "size_bytes": 100},
                {"name": "boot-image", "sha256": "2" * 64, "size_bytes": 200},
                {"name": "esp-image", "sha256": "3" * 64, "size_bytes": 300},
                {"name": "stage-state", "sha256": "4" * 64, "size_bytes": 400},
            ],
        }
        self.filesystems = {
            "root": {
                "type": "btrfs",
                "label": "OMARCHY_ROOT",
                "uuid": "4f4d5801-524f-4f54-8000-000000000001",
                "mount_options": ["ro", "subvol=/@"],
            },
            "boot": {
                "type": "ext4",
                "label": "OMARCHY_BOOT",
                "uuid": "4f4d5801-424f-4f54-8000-000000000001",
            },
            "esp": {
                "type": "vfat",
                "label": "OMARCHYESP",
                "uuid": "4F4D-5801",
            },
        }
        (self.target / "etc/fstab").write_text(
            "UUID=4f4d5801-524f-4f54-8000-000000000001 / btrfs subvol=@ 0 0\n"
            "UUID=4f4d5801-424f-4f54-8000-000000000001 /boot ext4 defaults 0 2\n"
            "UUID=4F4D-5801 /boot/efi vfat umask=0077 0 2\n"
        )
        phases = [
            "Preparing live environment",
            "Preparing install target",
            "Installing Arch + Omarchy",
            "Configuring hibernation",
            "Configuring system",
            "Staging provisioning",
        ]
        (self.state / "state.json").write_text(json.dumps({
            "total_phases": len(phases),
            "current_phase": "Installation complete",
            "phases": [{"name": name, "status": "ok"} for name in phases],
            "installed_packages": len(self.package_versions),
            "expected_packages": len(self.package_versions),
        }))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self):
        return self.module.capture_configured_target(
            target=self.target,
            state_dir=self.state,
            runtime_root=self.runtime,
            runtime_manifest=self.runtime_manifest,
            product_manifest=self.product_manifest,
            repository_manifest=self.repository_manifest,
            checkpoint_manifest=self.checkpoint_manifest,
            filesystems=self.filesystems,
            node_identity=self.node_identity,
        )

    def capture_installed_state(self):
        return self.module.capture_configured_target(
            target=self.target,
            state_dir=self.state,
            runtime_root=self.runtime,
            runtime_manifest=self.runtime_manifest,
            product_manifest=self.product_manifest,
            repository_manifest=self.repository_manifest,
            checkpoint_manifest=None,
            filesystems=self.filesystems,
            node_identity=self.node_identity,
        )

    def test_exact_configured_target_contract_is_admitted(self) -> None:
        proof = self.capture()
        self.assertEqual(proof["validation"], {"result": "passed"})
        self.assertEqual(proof["installed_packages"], len(self.package_versions))
        self.assertRegex(proof["proof_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            proof["validator_sha256"],
            self.module.file_sha256(MODULE_PATH),
        )
        self.assertEqual(
            proof["source_checkpoint_identity"],
            self.checkpoint_manifest["checkpoint_identity"],
        )

    def test_missing_required_package_fails_closed(self) -> None:
        package = next(
            (self.target / "var/lib/pacman/local").glob("linux-asahi-*")
        )
        for child in package.iterdir():
            child.unlink()
        package.rmdir()
        with self.assertRaisesRegex(
            self.module.ConfiguredTargetError,
            "required configured package is absent: linux-asahi",
        ):
            self.capture()

    def test_runtime_input_change_fails_closed(self) -> None:
        (self.runtime / "package-targets").write_text(
            "OMARCHY_RUNTIME_PACKAGE=untrusted-runtime\n"
        )
        with self.assertRaisesRegex(
            self.module.ConfiguredTargetError,
            "runtime input digest or size mismatch: package-targets",
        ):
            self.capture()

    def test_wrong_filesystem_or_fstab_fails_closed(self) -> None:
        self.filesystems["root"]["uuid"] = "wrong"
        with self.assertRaisesRegex(
            self.module.ConfiguredTargetError,
            "configured root UUID is invalid",
        ):
            self.capture()

    def test_installed_node_must_match_exact_lock_projection(self) -> None:
        self.node_identity["sha256"] = "0" * 64
        with self.assertRaisesRegex(
            self.module.ConfiguredTargetError,
            "configured Node runtime differs from the pinned lock",
        ):
            self.capture()

    def test_installed_node_filename_must_match_exact_lock_projection(self) -> None:
        self.node_identity["filename"] = "node-v26.8.2-linux-arm64.tar.gz"
        with self.assertRaisesRegex(
            self.module.ConfiguredTargetError,
            "configured target Node archive inventory is not exact",
        ):
            self.capture()

    def test_installed_node_size_must_match_exact_lock_projection(self) -> None:
        self.node_identity["size_bytes"] += 1
        with self.assertRaisesRegex(
            self.module.ConfiguredTargetError,
            "configured Node runtime differs from the pinned lock",
        ):
            self.capture()

    def test_extra_wrong_arch_node_archive_fails_fresh_production_gate(self) -> None:
        extra = (
            self.target
            / "var/lib/omarchy/provisioning/packages"
            / "node-v26.8.1-linux-x64.tar.gz"
        )
        extra.write_bytes(b"stale-wrong-architecture")
        with self.assertRaisesRegex(
            self.module.ConfiguredTargetError,
            "configured target Node archive inventory is not exact",
        ):
            self.capture_installed_state()

    def test_nonmatching_stale_node_tarball_fails_closed(self) -> None:
        extra = (
            self.target
            / "var/lib/omarchy/provisioning/packages"
            / "node-stale-offline-copy.tar.xz"
        )
        extra.write_bytes(b"stale-nonmatching-archive")
        with self.assertRaisesRegex(
            self.module.ConfiguredTargetError,
            "configured target Node archive inventory is not exact",
        ):
            self.capture()

    def test_incomplete_installed_count_fails_closed(self) -> None:
        state_path = self.state / "state.json"
        state = json.loads(state_path.read_text())
        state["installed_packages"] -= 1
        state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(
            self.module.ConfiguredTargetError,
            "configured installed package count is stale",
        ):
            self.capture()

    def test_wrong_installed_version_fails_closed(self) -> None:
        description = next(
            (self.target / "var/lib/pacman/local").glob("linux-asahi-*/desc")
        )
        description.write_text("%NAME%\nlinux-asahi\n\n%VERSION%\nstale-1\n")
        with self.assertRaisesRegex(
            self.module.ConfiguredTargetError,
            "installed package is absent or differs from verified repository",
        ):
            self.capture()

    def test_same_count_wrong_package_fails_fresh_production_gate(self) -> None:
        expected = self.target / "var/lib/pacman/local/bash-5.3.3-2"
        for child in expected.iterdir():
            child.unlink()
        expected.rmdir()
        substitute_name = "unrelated-live-package"
        substitute_version = "1.0-1"
        substitute = (
            self.target
            / "var/lib/pacman/local"
            / f"{substitute_name}-{substitute_version}"
        )
        substitute.mkdir()
        (substitute / "desc").write_text(
            f"%NAME%\n{substitute_name}\n\n%VERSION%\n{substitute_version}\n"
        )
        self.repository_manifest["resolved_closure"].append(
            {
                "name": substitute_name,
                "version": substitute_version,
                "filename": f"{substitute_name}.pkg.tar.xz",
            }
        )

        with self.assertRaisesRegex(
            self.module.ConfiguredTargetError,
            "installed package inventory differs from exact resolved closure",
        ):
            self.capture_installed_state()

    def test_missing_systemd_init_fails_closed(self) -> None:
        (self.target / "sbin/init").unlink()
        with self.assertRaisesRegex(
            self.module.ConfiguredTargetError,
            "configured /sbin/init is not the systemd link",
        ):
            self.capture()

    def test_fresh_installed_state_proof_omits_unstored_checkpoint_claims(self) -> None:
        proof = self.module.capture_configured_target(
            target=self.target,
            state_dir=self.state,
            runtime_root=self.runtime,
            runtime_manifest=self.runtime_manifest,
            product_manifest=self.product_manifest,
            repository_manifest=self.repository_manifest,
            checkpoint_manifest=None,
            filesystems=self.filesystems,
            node_identity=self.node_identity,
        )
        self.assertEqual(
            proof["verification_kind"], "configured-target-installed-state-v1"
        )
        self.assertNotIn("source_checkpoint_identity", proof)
        self.assertNotIn("checkpoint_outputs", proof)


if __name__ == "__main__":
    unittest.main()
