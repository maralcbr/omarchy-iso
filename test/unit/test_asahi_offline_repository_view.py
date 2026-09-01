from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "builder/verify-asahi-offline-repository-view.py"


def load_module():
    specification = importlib.util.spec_from_file_location(
        "asahi_offline_repository_view", MODULE_PATH
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {MODULE_PATH}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class OfflineRepositoryViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".offline-view-test-", dir=ROOT
        )
        self.root = Path(self.temporary.name)
        self.mirror = self.root / "mirror"
        self.mirror.mkdir(mode=0o700)
        self.package_name = "example-1-1-aarch64.pkg.tar.zst"
        self.signature_name = self.package_name + ".sig"
        self.package = b"signed-package"
        self.signature = b"detached-signature"
        self.database = b"repository-db"
        self.files = b"repository-files"
        for name, content in (
            (self.package_name, self.package),
            (self.signature_name, self.signature),
            ("offline.db.tar.gz", self.database),
            ("offline.files.tar.gz", self.files),
        ):
            path = self.mirror / name
            path.write_bytes(content)
            path.chmod(0o444)
        (self.mirror / "offline.db").symlink_to("offline.db.tar.gz")
        (self.mirror / "offline.files").symlink_to("offline.files.tar.gz")
        unsigned = {
            "schema_version": 1,
            "verification_kind": "asahi-offline-repository-inputs",
            "packages": [{
                "filename": self.package_name,
                "size_bytes": len(self.package),
                "sha256": sha256(self.package),
                "signature_filename": self.signature_name,
                "signature_size_bytes": len(self.signature),
                "signature_sha256": sha256(self.signature),
                "signer_fingerprint": "A" * 40,
            }],
            "requested_package_files": [self.package_name],
            "resolved_closure": [{
                "filename": self.package_name,
                "name": "example",
                "version": "1-1",
            }],
            "snapshot_locks": {},
            "trust": {
                "fingerprints": ["A" * 40],
                "ownertrust_sha256": "b" * 64,
            },
            "repo_add": {
                "version": "repo-add 1",
                "options": ["repo-add", "offline.db.tar.gz", "<sorted-packages>"],
            },
            "validation": {"result": "passed", "signatures": "required"},
        }
        self.manifest = unsigned | {
            "identity": self.module.canonical_digest(unsigned)
        }
        self.database_run = {
            "schema_version": 1,
            "stage": "offline-repository-database",
            "validation": {"result": "passed"},
            "outputs": [
                {
                    "name": "repository-db",
                    "kind": "file",
                    "size_bytes": len(self.database),
                    "sha256": sha256(self.database),
                },
                {
                    "name": "repository-files",
                    "kind": "file",
                    "size_bytes": len(self.files),
                    "sha256": sha256(self.files),
                },
            ],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def verify(self):
        return self.module.verify_offline_repository_view(
            mirror=self.mirror,
            repository_manifest=self.manifest,
            database_run_manifest=self.database_run,
        )

    def test_exact_manifest_payload_signatures_and_databases_are_admitted(self) -> None:
        result = self.verify()
        self.assertEqual(result["validation"], {"result": "passed"})
        self.assertEqual(result["repository_identity"], self.manifest["identity"])
        self.assertEqual(result["packages"], 1)

    def test_mutation_between_capture_and_install_fails_closed(self) -> None:
        self.verify()
        self.mirror.joinpath(self.package_name).chmod(0o600)
        self.mirror.joinpath(self.package_name).write_bytes(b"mutated-package")
        with self.assertRaisesRegex(
            self.module.RepositoryViewError, "digest or size mismatch"
        ):
            self.verify()

    def test_signature_or_database_mutation_fails_closed(self) -> None:
        for name in (self.signature_name, "offline.db.tar.gz", "offline.files.tar.gz"):
            with self.subTest(name=name):
                path = self.mirror / name
                original = path.read_bytes()
                path.chmod(0o600)
                path.write_bytes(b"mutated")
                with self.assertRaisesRegex(
                    self.module.RepositoryViewError, "digest or size mismatch"
                ):
                    self.verify()
                path.write_bytes(original)
                path.chmod(0o444)

    def test_extra_package_or_symlinked_payload_fails_closed(self) -> None:
        extra = self.mirror / "extra-1-1-aarch64.pkg.tar.zst"
        extra.write_bytes(b"extra")
        with self.assertRaisesRegex(
            self.module.RepositoryViewError, "inventory differs"
        ):
            self.verify()
        extra.unlink()
        package = self.mirror / self.package_name
        package.unlink()
        package.symlink_to(self.root / "outside")
        with self.assertRaisesRegex(
            self.module.RepositoryViewError, "unsafe|inventory differs"
        ):
            self.verify()


if __name__ == "__main__":
    unittest.main()
