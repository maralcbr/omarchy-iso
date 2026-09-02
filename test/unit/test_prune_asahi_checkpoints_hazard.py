"""Retention keeps every object reachable from a protected root.

Added 2026-08-29 as a characterization of the pruner's externally-referenced
object hazard; flipped 2026-09-02 when the hazard was fixed.

builder/prune-asahi-checkpoints.py decides which objects to keep by walking the
checkpoints that remain in the store after eviction. A run manifest lives
outside the store, so the objects it names were invisible to that walk and were
deleted as unreferenced even when the manifest was passed as
--protect-run-manifest. The pruner now reads object references out of every
protected run manifest and keeps those objects too.

These tests run entirely inside a temporary fixture store. The real checkpoint
store is never located, opened, or passed to prune().
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PRUNE_SCRIPT = ROOT / "builder/prune-asahi-checkpoints.py"

STAGE = "verified-package-cache"
MAXIMUM_BYTES = 1 << 30


def load_prune_module():
    spec = importlib.util.spec_from_file_location(
        "prune_asahi_checkpoints", PRUNE_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {PRUNE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrunerExternalReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_prune_module()

    def setUp(self) -> None:
        self.cache = Path(tempfile.mkdtemp(prefix="asahi-prune-hazard-"))
        self.addCleanup(shutil.rmtree, self.cache, ignore_errors=True)
        self.objects = self.cache / "objects" / "sha256"
        self.objects.mkdir(parents=True)
        self.checkpoints = self.cache / "checkpoints"
        self.checkpoints.mkdir()

    # -- fixture store construction ----------------------------------------

    def store_object(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        shard = self.objects / digest[:2]
        shard.mkdir(exist_ok=True)
        (shard / digest).write_bytes(payload)
        return digest

    def object_path(self, digest: str) -> Path:
        return self.objects / digest[:2] / digest

    def write_checkpoint(
        self,
        identity: str,
        *,
        completed_at: str,
        object_digests: list[str],
    ) -> Path:
        checkpoint = self.checkpoints / STAGE / identity
        checkpoint.mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "stage": STAGE,
            "checkpoint_identity": identity,
            "completed_at": completed_at,
            "outputs": [
                {
                    "name": f"output-{index}",
                    "storage": {"kind": "sha256-object", "sha256": digest},
                }
                for index, digest in enumerate(object_digests)
            ],
        }
        (checkpoint / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return checkpoint

    def build_store(self) -> dict:
        """Three checkpoints in one stage, plus an object only an external
        manifest references."""
        shared = self.store_object(b"payload retained by a surviving checkpoint")
        stale = self.store_object(b"payload of the checkpoint that will be evicted")
        external = self.store_object(b"payload referenced only from outside the store")

        newest = self.write_checkpoint(
            "1" * 64, completed_at="2026-08-29T03:00:00Z", object_digests=[shared]
        )
        middle = self.write_checkpoint(
            "2" * 64, completed_at="2026-08-29T02:00:00Z", object_digests=[shared]
        )
        oldest = self.write_checkpoint(
            "3" * 64, completed_at="2026-08-29T01:00:00Z", object_digests=[stale]
        )

        # The external manifest lives outside the cache root entirely, the way a
        # run manifest from another build would. It is deliberately NOT passed
        # to prune() as a protected identity.
        external_manifest = self.cache.parent / f"{self.cache.name}-external.json"
        external_manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "stage": STAGE,
                    "checkpoint_identity": "4" * 64,
                    "outputs": [
                        {
                            "name": "external-output",
                            "storage": {"kind": "sha256-object", "sha256": external},
                        }
                    ],
                },
                indent=2,
            )
            + "\n"
        )
        self.addCleanup(external_manifest.unlink, missing_ok=True)

        return {
            "external_manifest": external_manifest,
            "shared": shared,
            "stale": stale,
            "external": external,
            "newest": newest,
            "middle": middle,
            "oldest": oldest,
        }

    def prune_with_limit(
        self, *, per_stage: int, protected=frozenset(), manifests=()
    ) -> dict:
        identities, objects = self.module.protected_roots(list(manifests))
        return self.module.prune(
            cache_root=self.cache,
            maximum_bytes=MAXIMUM_BYTES,
            maximum_checkpoints_per_stage=per_stage,
            protected=set(protected) | identities,
            protected_objects=objects,
        )

    # -- retention contract -------------------------------------------------

    def test_prune_evicts_the_oldest_checkpoint_over_the_per_stage_limit(self) -> None:
        store = self.build_store()

        report = self.prune_with_limit(per_stage=2)

        evicted = {
            (item["stage"], item["identity"])
            for item in report["evicted"]
            if item["kind"] == "checkpoint"
        }
        self.assertEqual(evicted, {(STAGE, "3" * 64)})
        self.assertFalse(store["oldest"].exists())
        self.assertTrue(store["newest"].exists())
        self.assertTrue(store["middle"].exists())

    def test_prune_deletes_an_object_nobody_protects(self) -> None:
        # A manifest outside the store that is NOT handed to the pruner is
        # unknown to it; its object is reclaimed with the evicted checkpoint's.
        store = self.build_store()

        report = self.prune_with_limit(per_stage=2)

        deleted_objects = {
            item["sha256"] for item in report["evicted"] if item["kind"] == "object"
        }
        self.assertIn(store["external"], deleted_objects)
        self.assertIn(store["stale"], deleted_objects)
        # The object a surviving checkpoint references is kept.
        self.assertNotIn(store["shared"], deleted_objects)
        self.assertTrue(self.object_path(store["shared"]).exists())

    def test_a_protected_run_manifest_protects_the_objects_it_names(self) -> None:
        store = self.build_store()

        report = self.prune_with_limit(
            per_stage=2, manifests=[store["external_manifest"]]
        )

        deleted_objects = {
            item["sha256"] for item in report["evicted"] if item["kind"] == "object"
        }
        self.assertNotIn(store["external"], deleted_objects)
        self.assertTrue(self.object_path(store["external"]).exists())
        self.assertEqual(report["protected_objects"], [store["external"]])
        # Eviction of the over-limit checkpoint is unaffected.
        self.assertIn(store["stale"], deleted_objects)
        self.assertFalse(store["oldest"].exists())

    def test_prune_never_touches_paths_outside_the_cache_root(self) -> None:
        # Guards this fixture as much as the pruner: everything removed must sit
        # under the temporary cache root.
        self.build_store()
        outside = self.cache.parent / f"{self.cache.name}-bystander"
        outside.write_bytes(b"must survive")
        self.addCleanup(outside.unlink, missing_ok=True)

        self.prune_with_limit(per_stage=2)

        self.assertTrue(outside.exists())
        self.assertEqual(os.path.commonpath([str(self.cache)]), str(self.cache))

    def test_externally_protected_roots_are_retained(self) -> None:
        # An object reachable from a protected root survives retention,
        # whether that root is a checkpoint inside the store or a manifest
        # outside it.
        store = self.build_store()

        self.prune_with_limit(per_stage=2, manifests=[store["external_manifest"]])

        self.assertTrue(
            self.object_path(store["external"]).exists(),
            "an object referenced only by a protected external manifest was "
            "deleted by retention",
        )


if __name__ == "__main__":
    unittest.main()
