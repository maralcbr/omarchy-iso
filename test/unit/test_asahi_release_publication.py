from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "builder/asahi-release-publication.py"
PACKAGE = "omarchy-test-aarch64-apple-silicon-asahi-os-package.zip"


def load_module():
    spec = importlib.util.spec_from_file_location("release_publication", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleasePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.temporary = tempfile.TemporaryDirectory(prefix=".release-publish-test-", dir=ROOT)
        self.root = Path(self.temporary.name)
        self.private = self.root / "run-output"
        self.release = self.root / "release"
        self.private.mkdir(mode=0o700)
        self.release.mkdir(mode=0o700)
        self.sources = {
            PACKAGE: b"package",
            PACKAGE + ".asahi-package-evidence.json": b'{"evidence":true}\n',
            PACKAGE + ".installer-data.json": b'{"metadata":true}\n',
        }
        for name, content in self.sources.items():
            path = self.private / name
            path.write_bytes(content)
            path.chmod(0o444)
        self.manifest = self.private / "publication.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def publish(self):
        return self.module.publish_release(
            private_root=self.private,
            release_root=self.release,
            package_filename=PACKAGE,
            run_id="run-123",
            manifest_path=self.manifest,
            allowed_owner_ids={0, os.geteuid()},
        )

    def test_first_publication_is_exact_and_no_clobber(self) -> None:
        result = self.publish()
        self.assertFalse(result["reproducibility_match"])
        for name, content in self.sources.items():
            self.assertEqual((self.release / name).read_bytes(), content)
            self.assertEqual((self.private / name).read_bytes(), content)
        self.assertEqual(json.loads(self.manifest.read_text()), result)

    def test_identical_existing_release_is_retained_as_reproducibility_match(self) -> None:
        first = self.publish()
        inode = (self.release / PACKAGE).stat().st_ino
        second = self.publish()
        self.assertTrue(second["reproducibility_match"])
        self.assertEqual((self.release / PACKAGE).stat().st_ino, inode)
        self.assertEqual(first["outputs"], second["outputs"])

    def test_different_or_partial_existing_release_fails_closed(self) -> None:
        final = self.release / PACKAGE
        final.write_bytes(b"different")
        with self.assertRaisesRegex(self.module.PublicationError, "differs|partial"):
            self.publish()
        self.assertEqual(final.read_bytes(), b"different")
        final.unlink()
        (self.release / (PACKAGE + ".installer-data.json")).write_bytes(b"partial")
        with self.assertRaisesRegex(self.module.PublicationError, "partial"):
            self.publish()

    def test_unrelated_newer_zip_is_never_selected(self) -> None:
        unrelated = self.release / "newest-unrelated.zip"
        unrelated.write_bytes(b"stale")
        os.utime(unrelated, (4_000_000_000, 4_000_000_000))
        result = self.publish()
        self.assertEqual(result["package_filename"], PACKAGE)
        self.assertEqual((self.release / PACKAGE).read_bytes(), b"package")
        self.assertEqual(unrelated.read_bytes(), b"stale")

    def test_symlinked_source_or_release_root_is_rejected(self) -> None:
        source = self.private / PACKAGE
        source.unlink()
        source.symlink_to(self.root / "outside")
        with self.assertRaisesRegex(self.module.PublicationError, "unsafe"):
            self.publish()

    def test_build_driver_uses_private_exact_release_publication(self) -> None:
        driver = (ROOT / "builder/build-asahi-os-package.sh").read_text()
        self.assertIn(
            'private_release_root=$output_dir/.omarchy-run-$run_id', driver
        )
        self.assertIn('/builder/asahi-release-publication.py', driver)
        self.assertIn('--source-date-epoch "$SOURCE_DATE_EPOCH"', driver)
        self.assertIn('mkdir -m 0700 -- "$run_evidence"', driver)
        self.assertIn('/builder/asahi-release-publication.py cleanup', driver)
        self.assertNotRegex(
            driver, r"(?m)^package=[$]output_dir/[$]package_filename$"
        )

    def test_inode_bound_cleanup_removes_only_expected_private_files(self) -> None:
        private = self.root / "cleanup-private"
        private.mkdir(mode=0o700)
        for name, content in self.sources.items():
            path = private / name
            path.write_bytes(content)
            path.chmod(0o444)
        manifest = private / "release-publication.json"
        manifest.write_text("{}\n")
        manifest.chmod(0o444)
        identity = private.stat()

        self.module.cleanup_private_release(
            private_root=private,
            package_filename=PACKAGE,
            manifest_name=manifest.name,
            expected_device=identity.st_dev,
            expected_inode=identity.st_ino,
            allowed_owner_ids={0, os.geteuid()},
        )

        self.assertFalse(private.exists())

    def test_cleanup_rejects_wrong_identity_or_unexpected_entry(self) -> None:
        private = self.root / "unsafe-cleanup"
        private.mkdir(mode=0o700)
        identity = private.stat()
        with self.assertRaisesRegex(self.module.PublicationError, "identity changed"):
            self.module.cleanup_private_release(
                private_root=private,
                package_filename=PACKAGE,
                manifest_name="release-publication.json",
                expected_device=identity.st_dev,
                expected_inode=identity.st_ino + 1,
                allowed_owner_ids={0, os.geteuid()},
            )
        unexpected = private / "unexpected"
        unexpected.write_text("keep")
        unexpected.chmod(0o444)
        with self.assertRaisesRegex(self.module.PublicationError, "unexpected entries"):
            self.module.cleanup_private_release(
                private_root=private,
                package_filename=PACKAGE,
                manifest_name="release-publication.json",
                expected_device=identity.st_dev,
                expected_inode=identity.st_ino,
                allowed_owner_ids={0, os.geteuid()},
            )
        self.assertEqual(unexpected.read_text(), "keep")

    def test_dispatch_never_recursively_changes_release_root_ownership(self) -> None:
        dispatch = (ROOT / "builder/asahi-package-dispatch.sh").read_text()
        self.assertNotIn('chown -R "$HOST_UID:$HOST_GID" /out/', dispatch)
        self.assertIn('release-publication.json', dispatch)
        self.assertNotIn('reproducibility_match == false', dispatch)

    def test_host_reports_exact_product_output_without_newest_file_selection(self) -> None:
        wrapper = (ROOT / "bin/omarchy-iso-make").read_text()
        package_branch = wrapper.split(
            'if [[ $OMARCHY_ARTIFACT_KIND == "asahi-os-package" ]]', 1
        )[1].split('latest_iso=', 1)[0]
        self.assertNotIn("ls -t", package_branch)
        self.assertIn("package_filename", package_branch)
        self.assertIn("release-publication.json", package_branch)
        self.assertIn("Refusing to reuse existing build run evidence", wrapper)

    def test_unsafe_run_ids_fail_before_creating_output(self) -> None:
        output = self.root / "must-not-exist"
        driver = ROOT / "builder/build-asahi-os-package.sh"
        for run_id in ("../escape", "nested/id", "line\nbreak", "x" * 129):
            with self.subTest(run_id=run_id):
                result = subprocess.run(
                    ["/bin/bash", str(driver)],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "OMARCHY_ASAHI_OUTPUT_DIR": str(output),
                        "OMARCHY_BUILD_RUN_ID": run_id,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsafe build run ID", result.stderr)
                self.assertFalse(output.exists())

    def test_reused_run_id_cannot_mix_with_stale_green_evidence(self) -> None:
        output = self.root / "output"
        stale = output / "build-evidence/reused/build-report.json"
        stale.parent.mkdir(parents=True)
        stale.write_text('{"catalog_eligible":true}\n')
        driver = ROOT / "builder/build-asahi-os-package.sh"
        result = subprocess.run(
            ["/bin/bash", str(driver)],
            cwd=ROOT,
            env={
                **os.environ,
                "OMARCHY_ASAHI_OUTPUT_DIR": str(output),
                "OMARCHY_BUILD_RUN_ID": "reused",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to reuse existing build run evidence", result.stderr)
        self.assertEqual(stale.read_text(), '{"catalog_eligible":true}\n')


if __name__ == "__main__":
    unittest.main()
