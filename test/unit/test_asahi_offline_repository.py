from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "builder/capture-asahi-offline-repository.py"


def load_module():
    spec = importlib.util.spec_from_file_location("asahi_offline_repository", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OfflineRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.mirror = self.root / "mirror"
        self.mirror.mkdir()
        self.package = self.mirror / "example-1.0-1-aarch64.pkg.tar.xz"
        self.package.write_bytes(b"package")
        self.signature = self.package.with_name(self.package.name + ".sig")
        self.signature.write_bytes(b"signature")
        self.requested = self.root / "requested"
        self.requested.write_text(self.package.name + "\n")
        self.lock = self.root / "snapshot.lock"
        self.lock.write_text("snapshot=exact\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self):
        return self.module.capture_repository(
            mirror=self.mirror,
            requested_list=self.requested,
            snapshot_locks={"snapshot": self.lock},
            verify_signature=lambda _package, _signature: "A" * 40,
            package_metadata=lambda _package: ("example", "1.0-1"),
            trust_state={"fingerprints": ["A" * 40], "ownertrust_sha256": "b" * 64},
            repo_add_version="repo-add 7.1.0",
            repo_add_options=["repo-add", "offline.db.tar.gz", "<sorted-packages>"],
        )

    def test_manifest_contains_exact_payload_signature_trust_and_closure(self) -> None:
        manifest = self.capture()
        package = manifest["packages"][0]
        self.assertEqual(package["filename"], self.package.name)
        self.assertEqual(package["size_bytes"], len(b"package"))
        self.assertEqual(package["signature_filename"], self.signature.name)
        self.assertEqual(package["signer_fingerprint"], "A" * 40)
        self.assertEqual(manifest["requested_package_files"], [self.package.name])
        self.assertEqual(
            manifest["resolved_closure"],
            [{"filename": self.package.name, "name": "example", "version": "1.0-1"}],
        )
        self.assertEqual(manifest["validation"], {"result": "passed", "signatures": "required"})
        self.assertRegex(manifest["identity"], r"^[0-9a-f]{64}$")

    def test_missing_or_orphaned_signature_fails_closed(self) -> None:
        self.signature.unlink()
        with self.assertRaisesRegex(self.module.RepositoryCaptureError, "signature"):
            self.capture()

        self.signature.write_bytes(b"signature")
        orphan = self.mirror / "orphan.pkg.tar.xz.sig"
        orphan.write_bytes(b"orphan")
        with self.assertRaisesRegex(self.module.RepositoryCaptureError, "orphan"):
            self.capture()

    def test_unrequested_payload_fails_closed(self) -> None:
        extra = self.mirror / "extra-1-1-any.pkg.tar.xz"
        extra.write_bytes(b"extra")
        extra.with_name(extra.name + ".sig").write_bytes(b"signature")
        with self.assertRaisesRegex(self.module.RepositoryCaptureError, "requested closure"):
            self.capture()

    def test_database_projection_ignores_only_snapshot_lock_provenance(self) -> None:
        source = self.capture()
        target = self.capture()
        target["snapshot_locks"] = {
            "projected-lock": {
                "filename": "projected.json",
                "size_bytes": 42,
                "sha256": "c" * 64,
            }
        }
        target["identity"] = self.module.canonical_digest(
            {key: value for key, value in target.items() if key != "identity"}
        )

        source_projection = self.module.repository_database_projection(source)
        target_projection = self.module.repository_database_projection(target)
        self.assertEqual(source_projection, target_projection)

        target["trust"]["ownertrust_sha256"] = "d" * 64
        target["identity"] = self.module.canonical_digest(
            {key: value for key, value in target.items() if key != "identity"}
        )
        self.assertNotEqual(
            source_projection,
            self.module.repository_database_projection(target),
        )

    def test_database_projection_rejects_stale_manifest_identity(self) -> None:
        manifest = self.capture()
        manifest["packages"][0]["sha256"] = "e" * 64
        with self.assertRaisesRegex(self.module.RepositoryCaptureError, "identity"):
            self.module.repository_database_projection(manifest)

    def test_ownertrust_identity_ignores_only_export_comments_and_order(self) -> None:
        first = "\n".join(
            [
                "# List of assigned trustvalues, created yesterday",
                f"{'A' * 40}:6:",
                f"{'B' * 40}:4:",
                f"{'C' * 40}:134:",
                "",
            ]
        )
        second = "\n".join(
            [
                "# List of assigned trustvalues, created today",
                f"{'b' * 40}:4:",
                f"{'c' * 40}:134:",
                f"{'a' * 40}:6:",
                "",
            ]
        )
        changed = second.replace(":6:", ":5:")

        self.assertEqual(
            self.module.canonical_ownertrust_digest(first),
            self.module.canonical_ownertrust_digest(second),
        )
        self.assertNotEqual(
            self.module.canonical_ownertrust_digest(first),
            self.module.canonical_ownertrust_digest(changed),
        )
        with self.assertRaisesRegex(self.module.RepositoryCaptureError, "ownertrust"):
            self.module.canonical_ownertrust_digest("not-an-ownertrust-record\n")

    def test_system_trust_state_uses_canonical_ownertrust_records(self) -> None:
        fingerprint = "A" * 40
        listing = f"pub:u:::::::u:\nfpr:::::::::{fingerprint}:\n"
        first = f"# generated first\n{fingerprint}:134:\n"
        second = f"# generated later\n{fingerprint.lower()}:134:\n"

        with mock.patch.object(self.module, "run", side_effect=[listing, first]):
            first_state = self.module.system_trust_state(self.root)
        with mock.patch.object(self.module, "run", side_effect=[listing, second]):
            second_state = self.module.system_trust_state(self.root)

        self.assertEqual(first_state, second_state)
        self.assertEqual(first_state["fingerprints"], [fingerprint])

    def test_database_transition_proves_exact_lock_projection(self) -> None:
        legacy_lock = self.root / "asahi-build-lock.json"
        legacy_value = {
            "schema_version": 1,
            "builder": {"base_image": "sha256:builder"},
            "compression": {"workers": 1},
            "modes": {"diagnostic": {"root_size_bytes": 1}},
            "node": {"version": "26.8.1", "sha256": "a" * 64},
            "retention": {"maximum_checkpoints_per_stage": 3},
            "stages": ["verified-package-cache"],
        }
        legacy_lock.write_text(json.dumps(legacy_value, sort_keys=True) + "\n")
        package_lock = self.root / "source-lock.json"
        package_value = {
            "schema_version": 1,
            "stage": "verified-package-cache",
            "mode": "diagnostic",
            "inputs": {"node": legacy_value["node"]},
        }
        package_lock.write_text(json.dumps(package_value, sort_keys=True) + "\n")

        source = self.capture()
        source["snapshot_locks"] = {
            "build-lock": self.module.path_record(legacy_lock),
            "snapshot": source["snapshot_locks"]["snapshot"],
        }
        source["identity"] = self.module.canonical_digest(
            {key: value for key, value in source.items() if key != "identity"}
        )
        target = self.capture()
        target["snapshot_locks"] = {
            "package-source-lock": self.module.path_record(package_lock),
            "snapshot": target["snapshot_locks"]["snapshot"],
        }
        target["identity"] = self.module.canonical_digest(
            {key: value for key, value in target.items() if key != "identity"}
        )

        projected = self.module.project_repository_manifest(
            source_manifest=source,
            legacy_build_lock=legacy_lock,
            package_source_lock=package_lock,
            mode="diagnostic",
        )
        self.assertEqual(projected, target)

        proof = self.module.verify_repository_database_transition(
            source_manifest=source,
            target_manifest=target,
            legacy_build_lock=legacy_lock,
            package_source_lock=package_lock,
            mode="diagnostic",
        )
        self.assertEqual(proof["kind"], "repository-database-manifest-v1")
        self.assertRegex(proof["proof_digest"], r"^[0-9a-f]{64}$")

        package_value["inputs"]["node"]["version"] = "changed"
        package_lock.write_text(json.dumps(package_value, sort_keys=True) + "\n")
        with self.assertRaisesRegex(self.module.RepositoryCaptureError, "projection"):
            self.module.verify_repository_database_transition(
                source_manifest=source,
                target_manifest=target,
                legacy_build_lock=legacy_lock,
                package_source_lock=package_lock,
                mode="diagnostic",
            )

    def test_database_transition_requires_exact_ownertrust_normalization(self) -> None:
        legacy_lock = self.root / "asahi-build-lock.json"
        legacy_value = {
            "schema_version": 1,
            "builder": {"base_image": "sha256:builder"},
            "compression": {"workers": 1},
            "modes": {"diagnostic": {"root_size_bytes": 1}},
            "node": {"version": "26.8.1", "sha256": "a" * 64},
            "retention": {"maximum_checkpoints_per_stage": 3},
            "stages": ["verified-package-cache"],
        }
        legacy_lock.write_text(json.dumps(legacy_value, sort_keys=True) + "\n")
        package_lock = self.root / "source-lock.json"
        package_lock.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "stage": "verified-package-cache",
                    "mode": "diagnostic",
                    "inputs": {"node": legacy_value["node"]},
                },
                sort_keys=True,
            )
            + "\n"
        )

        source = self.capture()
        source["snapshot_locks"] = {
            "build-lock": self.module.path_record(legacy_lock),
            "snapshot": source["snapshot_locks"]["snapshot"],
        }
        source["identity"] = self.module.canonical_digest(
            {key: value for key, value in source.items() if key != "identity"}
        )
        normalized_ownertrust = "d" * 64
        target = self.module.project_repository_manifest(
            source_manifest=source,
            legacy_build_lock=legacy_lock,
            package_source_lock=package_lock,
            mode="diagnostic",
            target_ownertrust_sha256=normalized_ownertrust,
        )
        transition = {
            "kind": "ownertrust-canonicalization-v1",
            "source_ownertrust_sha256": "b" * 64,
            "target_ownertrust_sha256": normalized_ownertrust,
        }

        proof = self.module.verify_repository_database_transition(
            source_manifest=source,
            target_manifest=target,
            legacy_build_lock=legacy_lock,
            package_source_lock=package_lock,
            mode="diagnostic",
            ownertrust_transition=transition,
        )
        self.assertEqual(proof["kind"], "repository-database-manifest-v1")

        wrong_transition = transition | {"target_ownertrust_sha256": "e" * 64}
        with self.assertRaisesRegex(self.module.RepositoryCaptureError, "ownertrust"):
            self.module.verify_repository_database_transition(
                source_manifest=source,
                target_manifest=target,
                legacy_build_lock=legacy_lock,
                package_source_lock=package_lock,
                mode="diagnostic",
                ownertrust_transition=wrong_transition,
            )

        target["packages"][0]["sha256"] = "f" * 64
        target["identity"] = self.module.canonical_digest(
            {key: value for key, value in target.items() if key != "identity"}
        )
        with self.assertRaisesRegex(self.module.RepositoryCaptureError, "projection"):
            self.module.verify_repository_database_transition(
                source_manifest=source,
                target_manifest=target,
                legacy_build_lock=legacy_lock,
                package_source_lock=package_lock,
                mode="diagnostic",
                ownertrust_transition=transition,
            )


if __name__ == "__main__":
    unittest.main()
