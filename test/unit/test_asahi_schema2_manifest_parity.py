"""Parity characterization: planner and projection views of one schema-2 fixture.

Added 2026-08-29 (plan Phase B). The shared fixture family lives in
asahi_schema2_fixtures.py and is driven through three validator surfaces. This
module covers two of them; the producer surface is covered by
test/unit/asahi-schema2-producer-parity-test.sh, which applies the same
mutations to a manifest the real producer script accepts.

Measured accept/reject after C1b (a = producer, b = planner, c = projection):

  fixture                          a          b          c
  valid-baseline                   accept     accept     accept
  unknown-extra-field              REJECT     REJECT     REJECT
  manifest-cache-hit               REJECT     REJECT     accept   <- by design
  tampered-compatibility-reason    REJECT     REJECT     REJECT
  tampered-compatibility-lock      REJECT     REJECT     REJECT
  docker-image-absent              REJECT     accept     accept   <- intended

All three surfaces now agree on every documentary fixture. Two entries are
deliberately not uniform, and both are properties of the surfaces rather than
disagreements about the schema.

manifest-cache-hit, projection accepts. A checkpoint manifest may never claim
cache_hit -- it would mean the stored artifact was itself restored rather than
produced -- and the producer and planner both refuse it. The projection consumes
a *run* manifest, and a run manifest legitimately carries cache_hit: true; that
is precisely what the producer writes when it verifies a cached toolchain and
skips the build. Rejecting it here would break the ordinary cache-hit path.

docker-image-absent, producer only. The manifest is byte-identical to the
baseline; only the container runtime differs. The producer is the sole surface
that inspects it, and the other two are metadata-only by design.
  - Runtime state. Only the producer inspects docker, so an absent image is
    invisible to the other two. This is the expected metadata-only divergence.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import asahi_schema2_fixtures as fixtures


ROOT = Path(__file__).resolve().parents[2]
PLANNER_PATH = ROOT / "builder/asahi_checkpoint_plan.py"
ISO_MAKE_PATH = ROOT / "bin/omarchy-iso-make"

sys.path.insert(0, str(ROOT / "builder"))
import asahi_toolchain_metadata as canonical  # noqa: E402

LOCK_SHA256 = "a" * 64
LEGACY_LOCK_PATH = ROOT / "builder/asahi-build-lock.json"
LEGACY_LOCK_SHA256 = hashlib.sha256(LEGACY_LOCK_PATH.read_bytes()).hexdigest()
INVENTORY = ["bash 5.2.037-1"]
SYNC_DATABASES = ["5" * 64 + "  /var/lib/pacman/sync/core.db"]

# The inline jq projection that bin/omarchy-iso-make carried until 2026-08-30.
# It is retained only so the delegation test can assert it has NOT come back
# alongside the canonical call. The byte-stability of what replaced it is proven
# in test/unit/test_asahi_toolchain_projection_golden.py against the copy
# preserved in the Phase A rollback checkpoint.
RETIRED_PROJECTION_FILTER = """{schema_version: 1, stage, mode, checkpoint_identity, input_digest,
    validation, output, compatibility: (.compatibility // null)}"""


def load_planner():
    spec = importlib.util.spec_from_file_location("asahi_checkpoint_plan", PLANNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {PLANNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_baseline_manifest() -> dict:
    """A structurally valid schema-2 checkpoint manifest for the planner."""
    inventory_digest = hashlib.sha256(
        ("\n".join(INVENTORY) + "\n").encode("utf-8")
    ).hexdigest()
    declared_inputs = {
        "base_image": "docker.io/library/archlinux@sha256:" + "1" * 64,
        "source_lock_sha256": LOCK_SHA256,
        "containerfile_sha256": "2" * 64,
        "script_sha256": "3" * 64,
        "source": {
            "omarchy_iso_stage": "4" * 64,
            "omarchy_iso_producer": "5" * 64,
            "manifest_sha256": "6" * 64,
        },
        "toolchain_packages": ["base-devel", "git"],
    }
    actual_inputs = {
        **copy.deepcopy(declared_inputs),
        "package_inventory_sha256": inventory_digest,
        "package_inventory": list(INVENTORY),
        "synchronized_database_digests": list(SYNC_DATABASES),
    }
    return {
        "schema_version": 2,
        "stage": "builder-toolchain",
        "mode": "shared",
        "declared_inputs": declared_inputs,
        "declared_input_digest": fixtures.digest(declared_inputs),
        "actual_inputs": actual_inputs,
        "checkpoint_identity": fixtures.digest(actual_inputs),
        "output": {
            "image_id": "sha256:" + "7" * 64,
            "size_bytes": 12345,
            "package_inventory_sha256": inventory_digest,
        },
        "validation": {"result": "passed"},
        "completed_at": "2026-08-29T00:00:00Z",
        "elapsed_seconds": 1.0,
        "cache_hit": False,
        "immutable": True,
        "environment": "OMARCHY_ASAHI_TOOLCHAIN_PREPARED=1",
        "compatibility": fixtures.compatibility_block(
            # The planner now binds this to the digest of the real legacy build
            # lock, matching what the producer always required, so the baseline
            # has to carry the genuine digest to be accepted.
            source_lock=LEGACY_LOCK_SHA256,
            target_lock=LOCK_SHA256,
        ),
    }


class Schema2ManifestParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.planner = load_planner()
        cls.planner_module_loaded = True

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="asahi-schema2-parity-"))
        self.addCleanup(self.remove_work)

    def remove_work(self) -> None:
        # The fixture store is deliberately made read-only, so restore write
        # permission before removing it. Nothing outside this tmpdir is touched.
        for current, directories, _ in os.walk(self.work):
            for name in [current, *(os.path.join(current, d) for d in directories)]:
                try:
                    os.chmod(name, 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.work, ignore_errors=True)

    # -- surface (b): planner, metadata only, no docker --------------------

    def planner_outcome(self, name: str) -> tuple[bool, str]:
        manifest = fixtures.apply_mutation(name, build_baseline_manifest())
        identity = manifest["checkpoint_identity"]
        cache_root = self.work / name / "cache"
        checkpoint = cache_root / "builder-toolchain" / identity
        checkpoint.mkdir(parents=True)
        manifest_path = checkpoint / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        manifest_path.chmod(0o444)
        checkpoint.chmod(0o555)

        try:
            loaded, _, checkpoint_path = self.planner._load_toolchain_manifest(
                cache_root, identity
            )
            self.planner._validate_toolchain_manifest_contents(
                loaded,
                checkpoint=checkpoint_path,
                checkpoint_identity=identity,
            )
            compatibility = loaded.get("compatibility")
            if compatibility is not None:
                # Mirrors _toolchain_metadata_candidate, which since 2026-08-30
                # binds source_lock_sha256 to the real legacy build lock.
                self.planner._validate_toolchain_compatibility(
                    compatibility,
                    expected_target_lock=loaded["declared_inputs"][
                        "source_lock_sha256"
                    ],
                    expected_source_lock=LEGACY_LOCK_SHA256,
                    role="builder-toolchain manifest",
                )
        except self.planner.CheckpointPlanError as error:
            return False, str(error)
        return True, ""

    # -- surface (c): projection gate, jq copied verbatim ------------------

    def projection_outcome(self, name: str) -> tuple[bool, str]:
        # Drives the real mechanism: the same canonical CLI, with the same
        # arguments, that bin/omarchy-iso-make invokes.
        manifest = fixtures.apply_mutation(name, build_baseline_manifest())
        run_record = fixtures.run_record_from_manifest(manifest)
        source = self.work / f"{name}-run.json"
        source.write_text(json.dumps(run_record, indent=2, sort_keys=True) + "\n")

        done = subprocess.run(
            [
                sys.executable,
                str(ROOT / "builder/asahi_toolchain_metadata.py"),
                "project",
                "--run-manifest",
                str(source),
                "--legacy-lock",
                str(LEGACY_LOCK_PATH),
                "--output",
                str(self.work / f"{name}-identity.json"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return done.returncode == 0, done.stderr.strip()

    # -- drift guard -------------------------------------------------------

    def test_all_three_surfaces_delegate_to_the_canonical_module(self) -> None:
        # Replaces the jq-verbatim drift guard, which pinned copies of the
        # inline programs that used to live in bin/omarchy-iso-make. Those are
        # gone: both shell surfaces now call the canonical module, so what has
        # to be pinned is the delegation itself and the absence of any private
        # reimplementation.
        iso_make = ISO_MAKE_PATH.read_text()
        producer = (ROOT / "builder/ensure-asahi-toolchain-image.sh").read_text()

        self.assertIn("asahi_toolchain_metadata.py", iso_make)
        self.assertIn("project", iso_make)
        self.assertIn("asahi_toolchain_metadata.py", producer)
        self.assertIn("validate-checkpoint-manifest", producer)

        # The retired jq programs must not reappear alongside the delegation.
        self.assertNotIn(RETIRED_PROJECTION_FILTER, iso_make)
        self.assertNotIn("keys == [\"checkpoint_identity\"", iso_make)

    def test_canonical_key_set_is_the_projected_key_set(self) -> None:
        # PROJECTION_KEYS is now the only definition of the projected shape.
        record = fixtures.run_record_from_manifest(build_baseline_manifest())
        projected = json.loads(canonical.projection_bytes(record))

        self.assertEqual(sorted(canonical.PROJECTION_KEYS), sorted(projected))
        canonical.validate_projection(projected)

    # -- baseline ----------------------------------------------------------

    def test_baseline_is_accepted_by_planner_and_projection(self) -> None:
        planner_accepted, reason = self.planner_outcome(fixtures.VALID)
        self.assertTrue(planner_accepted, reason)
        projection_accepted, gate_reason = self.projection_outcome(fixtures.VALID)
        self.assertTrue(projection_accepted, gate_reason)

    # -- divergences -------------------------------------------------------

    def test_unknown_field_is_rejected_by_both_metadata_surfaces(self) -> None:
        # ALIGNED. Until 2026-08-30 the projection rebuilt the document from a
        # fixed key list, so an undeclared key was silently dropped and the
        # gate's own key closure never saw it. Both surfaces now close over the
        # key set before anything is emitted.
        planner_accepted, reason = self.planner_outcome(fixtures.UNKNOWN_FIELD)
        self.assertFalse(planner_accepted)
        self.assertIn("fields are invalid", reason)

        projection_accepted, gate_reason = self.projection_outcome(
            fixtures.UNKNOWN_FIELD
        )
        self.assertFalse(projection_accepted)
        self.assertIn("fields are invalid", gate_reason)

    def test_cache_hit_is_rejected_by_planner_but_not_carried_by_projection(
        self,
    ) -> None:
        # Divergence. cache_hit is provenance the planner refuses outright, and
        # the projection does not carry the field at all, so the gate cannot
        # refuse it.
        planner_accepted, reason = self.planner_outcome(fixtures.CACHE_HIT)
        self.assertFalse(planner_accepted)
        self.assertIn("manifest is invalid", reason)

        projection_accepted, gate_reason = self.projection_outcome(fixtures.CACHE_HIT)
        self.assertTrue(projection_accepted, gate_reason)

    def test_tampered_compatibility_reason_is_rejected_by_both_surfaces(self) -> None:
        # ALIGNED. The reason literal is what a rekey is allowed to assert, so
        # neither surface may carry a block claiming anything else. The
        # projection used to copy compatibility verbatim without inspecting it.
        planner_accepted, reason = self.planner_outcome(fixtures.COMPAT_REASON)
        self.assertFalse(planner_accepted)
        self.assertIn("compatibility metadata is invalid", reason)

        projection_accepted, gate_reason = self.projection_outcome(
            fixtures.COMPAT_REASON
        )
        self.assertFalse(projection_accepted)
        self.assertIn("compatibility metadata is invalid", gate_reason)

    def test_tampered_compatibility_lock_digest_is_rejected_by_both_surfaces(
        self,
    ) -> None:
        # ALIGNED, and the divergence that started this work. Until 2026-08-30
        # only the producer bound compatibility.source_lock_sha256 to the actual
        # digest of the legacy lock file; the planner checked only that it was a
        # well-formed sha256, and the projection did not look at all. Every
        # surface that can reach the file now binds it.
        planner_accepted, reason = self.planner_outcome(fixtures.COMPAT_LOCK)
        self.assertFalse(planner_accepted)
        self.assertIn("compatibility metadata is invalid", reason)

        projection_accepted, gate_reason = self.projection_outcome(fixtures.COMPAT_LOCK)
        self.assertFalse(projection_accepted)
        self.assertIn("compatibility metadata is invalid", gate_reason)

    def test_absent_docker_image_is_invisible_to_both_metadata_surfaces(self) -> None:
        # Expected divergence: neither surface inspects the container runtime,
        # so a manifest naming an image that does not exist is indistinguishable
        # from the baseline. Only the producer surface can reject it.
        planner_accepted, reason = self.planner_outcome(fixtures.IMAGE_ABSENT)
        self.assertTrue(planner_accepted, reason)

        projection_accepted, gate_reason = self.projection_outcome(
            fixtures.IMAGE_ABSENT
        )
        self.assertTrue(projection_accepted, gate_reason)

    def test_every_fixture_is_exercised_against_both_surfaces(self) -> None:
        # Guards the family itself: adding a mutation without deciding its
        # outcome on both surfaces fails here.
        for name in fixtures.FIXTURE_NAMES:
            with self.subTest(fixture=name):
                self.assertIsInstance(self.planner_outcome(name)[0], bool)
                self.assertIsInstance(self.projection_outcome(name)[0], bool)


if __name__ == "__main__":
    unittest.main()
