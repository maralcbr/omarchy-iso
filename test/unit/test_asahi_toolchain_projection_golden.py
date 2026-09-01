"""Byte-stability proof for the schema-1 projection (plan Phase C1).

The projected builder-toolchain-identity.json bytes feed the
verified-package-cache checkpoint identity, so the canonical projection in
builder/asahi_toolchain_metadata.py must reproduce, byte for byte, what the
retired jq program produced.

The reference is deliberately not the working tree: it is the copy of
bin/omarchy-iso-make preserved in the Phase A rollback checkpoint, which is
read-only and cannot drift. Goldens are generated from that program with jq at
test time and compared against the canonical emitter.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import asahi_schema2_fixtures as fixtures


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "builder"))

import asahi_toolchain_metadata as canonical  # noqa: E402

CHECKPOINT_ISO_MAKE = Path(
    "/Users/maralc/dev/omarchy/rollback-checkpoints/"
    "2026-08-29-iso-schema2-dirty-tree/worktree/bin/omarchy-iso-make"
)

# The projection filter exactly as it shipped before Phase C1, lifted from the
# preserved checkpoint copy. test_reference_program_is_the_preserved_one asserts
# this text is really in that file, so the reference cannot quietly become
# something else.
RETIRED_PROJECTION_FILTER = """{schema_version: 1, stage, mode, checkpoint_identity, input_digest,
    validation, output, compatibility: (.compatibility // null)}"""


def build_baseline_manifest() -> dict:
    from test_asahi_schema2_manifest_parity import build_baseline_manifest as builder

    return builder()


class ProjectionByteStabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.jq = shutil.which("jq")
        if cls.jq is None:
            raise unittest.SkipTest("jq is unavailable")

    def golden_bytes(self, run_record: dict) -> bytes:
        """What the retired jq program emitted for this run manifest."""
        with tempfile.TemporaryDirectory(prefix="toolchain-golden-") as directory:
            source = Path(directory) / "run-manifest.json"
            source.write_text(json.dumps(run_record, indent=2, sort_keys=True) + "\n")
            done = subprocess.run(
                [self.jq, "-S", RETIRED_PROJECTION_FILTER, str(source)],
                check=True,
                capture_output=True,
            )
            return done.stdout

    def test_reference_program_is_the_preserved_one(self) -> None:
        self.assertTrue(
            CHECKPOINT_ISO_MAKE.is_file(),
            f"the Phase A checkpoint copy is missing: {CHECKPOINT_ISO_MAKE}",
        )
        self.assertIn(RETIRED_PROJECTION_FILTER, CHECKPOINT_ISO_MAKE.read_text())

    def test_every_fixture_projects_byte_identically(self) -> None:
        for name in fixtures.FIXTURE_NAMES:
            with self.subTest(fixture=name):
                manifest = fixtures.apply_mutation(name, build_baseline_manifest())
                record = fixtures.run_record_from_manifest(manifest)

                self.assertEqual(canonical.projection_bytes(record), self.golden_bytes(record))

    def test_projection_without_compatibility_is_byte_identical(self) -> None:
        # The `.compatibility // null` arm: a run manifest that carries no
        # compatibility block must still project to an explicit null.
        manifest = build_baseline_manifest()
        del manifest["compatibility"]
        record = fixtures.run_record_from_manifest(manifest)

        self.assertEqual(canonical.projection_bytes(record), self.golden_bytes(record))
        self.assertIsNone(json.loads(canonical.projection_bytes(record))["compatibility"])

    def test_rekeyed_run_manifest_projects_byte_identically(self) -> None:
        # `rekeyed` is an allowed run-record field that the projection drops.
        manifest = build_baseline_manifest()
        record = fixtures.run_record_from_manifest(manifest)
        record["rekeyed"] = True
        record["cache_hit"] = False

        self.assertEqual(canonical.projection_bytes(record), self.golden_bytes(record))

    def test_projection_emits_exactly_the_gate_key_set(self) -> None:
        record = fixtures.run_record_from_manifest(build_baseline_manifest())
        projected = json.loads(canonical.projection_bytes(record))

        self.assertEqual(
            sorted(projected),
            [
                "checkpoint_identity",
                "compatibility",
                "input_digest",
                "mode",
                "output",
                "schema_version",
                "stage",
                "validation",
            ],
        )
        self.assertEqual(sorted(canonical.PROJECTION_KEYS), sorted(projected))

    def test_integer_magnitude_beyond_double_precision_is_refused(self) -> None:
        # jq carries numbers as doubles, so an integer at or above 2**53 would
        # project differently under jq than under this emitter. Rather than
        # emit bytes that could not be reproduced, the validator refuses it.
        manifest = build_baseline_manifest()
        manifest["output"]["size_bytes"] = canonical.MAXIMUM_EXACT_INTEGER + 1

        with self.assertRaises(canonical.ToolchainMetadataError):
            canonical.validate_checkpoint_manifest(manifest)

        record = fixtures.run_record_from_manifest(manifest)
        with self.assertRaises(canonical.ToolchainMetadataError):
            canonical.validate_run_record(record)

    def test_emitter_matches_jq_on_non_ascii_content(self) -> None:
        # Never reachable through a validated document -- stage is pinned to an
        # ASCII literal -- but pinned so the emitter cannot regress to escaping
        # where jq emits raw UTF-8.
        record = fixtures.run_record_from_manifest(build_baseline_manifest())
        record["mode"] = "sharéd"

        self.assertEqual(canonical.projection_bytes(record), self.golden_bytes(record))


if __name__ == "__main__":
    unittest.main()
