from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "builder"
SCRIPT = BUILDER / "apply-asahi-checkpoint-rekey.py"


def canonical_digest(value: dict) -> str:
    content = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(content).hexdigest()


def load_module():
    sys.path.insert(0, str(BUILDER))
    try:
        spec = importlib.util.spec_from_file_location("apply_asahi_checkpoint_rekey", SCRIPT)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load {SCRIPT}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(BUILDER))


class RekeyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.lock = self.root / "source-lock.json"
        self.lock.write_text('{"schema_version":1}\n')
        self.source_manifest_identity = "c" * 64
        self.producer_binding_identity = "d" * 64
        self.source_manifest = self.root / "source-manifest.json"
        self.source_manifest.write_text(
            json.dumps(
                {
                    "source_identity": self.source_manifest_identity,
                    "producer_binding_identity": self.producer_binding_identity,
                }
            )
            + "\n"
        )
        self.shared = self.root / "shared"
        self.shared.write_bytes(b"shared input")
        self.legacy = self.root / "legacy"
        self.legacy.write_bytes(b"legacy input")
        self.output_sha256 = "e" * 64
        self.source_identity = self._identity(
            {"archiso": "a" * 40, "omarchy_iso": "b" * 40},
            {"legacy-source": self.legacy, "shared": self.shared},
        )
        self.target_identity = self._identity(
            {
                "omarchy_iso_stage": self.source_manifest_identity,
                "omarchy_iso_producer": self.producer_binding_identity,
            },
            {"shared": self.shared, "source-manifest": self.source_manifest},
        )
        self.plan = self._plan()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _identity(self, source_commits: dict, inputs: dict, mode="diagnostic") -> dict:
        return self.module.checkpoint.build_identity(
            stage="base-images",
            mode=mode,
            source_lock=self.lock,
            source_commits=source_commits,
            inputs=inputs,
        )

    def _plan(self) -> dict:
        return {
            "schema_version": 2,
            "stage": "base-images",
            "mode": "diagnostic",
            "source_identity_kind": "legacy-monolithic-v0",
            "source_checkpoint_identity": self.source_identity["checkpoint_identity"],
            "target_checkpoint_identity": self.target_identity["checkpoint_identity"],
            "target_source_manifest_identity": self.source_manifest_identity,
            "target_producer_binding_identity": self.producer_binding_identity,
            "target_source_lock_sha256": self.target_identity["source_lock"]["sha256"],
            "equivalent_inputs": {"shared": "shared"},
            "projected_equivalent_inputs": {},
            "repository_manifest_transition": None,
            "configured_target_transition": None,
            "legacy_immutable_admission": None,
            "allowed_added_inputs": ["source-manifest"],
            "allowed_removed_inputs": ["legacy-source"],
            "allow_source_lock_change": False,
            "allow_source_commits_change": True,
            "expected_outputs": {
                "root-image": {"sha256": self.output_sha256, "size_bytes": 64}
            },
            "reason": "stage-input-granularity-v1",
        }

    def _validate(self) -> str:
        return self.module.validate_rekey_contract(
            plan=self.plan,
            source_manifest={"migration": None},
            source_identity=self.source_identity,
            target_identity=self.target_identity,
        )

    def test_exact_legacy_shape_is_classified_but_not_authorized(self) -> None:
        self.assertEqual(
            self.module._classify_legacy_source_identity(self.source_identity),
            "legacy-monolithic-v0",
        )
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "no current exact stage-declaration authority",
        ):
            self._validate()

    def test_exact_prebinding_shape_is_classified_but_not_authorized(self) -> None:
        self.source_identity = self._identity(
            {"omarchy_iso_stage": "f" * 64},
            {"shared": self.shared, "source-manifest": self.source_manifest},
        )
        self.plan["source_identity_kind"] = "stage-specific-prebinding-v1"
        self.assertEqual(
            self.module._classify_legacy_source_identity(self.source_identity),
            "stage-specific-prebinding-v1",
        )
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "no current exact stage-declaration authority",
        ):
            self._validate()

    def test_matching_opaque_target_digests_do_not_create_authority(self) -> None:
        opaque_source_identity = "1" * 64
        opaque_producer_identity = "2" * 64
        self.target_identity = self._identity(
            {
                "omarchy_iso_stage": opaque_source_identity,
                "omarchy_iso_producer": opaque_producer_identity,
            },
            {"shared": self.shared, "source-manifest": self.source_manifest},
        )
        self.plan.update(
            {
                "target_checkpoint_identity": self.target_identity[
                    "checkpoint_identity"
                ],
                "target_source_manifest_identity": opaque_source_identity,
                "target_producer_binding_identity": opaque_producer_identity,
            }
        )
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "no current exact stage-declaration authority",
        ):
            self._validate()

    def test_source_commit_replacement_flag_does_not_supply_authority(self) -> None:
        self.assertIs(self.plan["allow_source_commits_change"], True)
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "no current exact stage-declaration authority",
        ):
            self._validate()

    def test_plan_pins_both_target_identity_roles(self) -> None:
        self.plan["target_source_manifest_identity"] = "f" * 64
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "target source-manifest identity is stale",
        ):
            self._validate()
        self.plan["target_source_manifest_identity"] = self.source_manifest_identity
        self.plan["target_producer_binding_identity"] = "f" * 64
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "target producer binding identity is stale",
        ):
            self._validate()

    def test_target_must_carry_distinct_modern_source_roles(self) -> None:
        self.target_identity = self._identity(
            {"omarchy_iso_stage": self.source_manifest_identity},
            {"shared": self.shared, "source-manifest": self.source_manifest},
        )
        self.plan["target_checkpoint_identity"] = self.target_identity[
            "checkpoint_identity"
        ]
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "target source commit roles are invalid",
        ):
            self._validate()

    def test_qualification_and_arbitrary_reason_are_rejected(self) -> None:
        self.plan["mode"] = "qualification"
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "rekey is diagnostic-only",
        ):
            self._validate()
        self.plan["mode"] = "diagnostic"
        self.plan["reason"] = "operator-chosen-reason"
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "checkpoint rekey reason is not authorized",
        ):
            self._validate()

    def test_mixed_unknown_and_modern_sources_are_rejected(self) -> None:
        invalid = (
            {
                "archiso": "a" * 40,
                "omarchy_iso": "b" * 40,
                "omarchy_iso_stage": "c" * 64,
            },
            {"unknown": "f" * 64},
            {
                "omarchy_iso_stage": "c" * 64,
                "omarchy_iso_producer": "d" * 64,
            },
        )
        for commits in invalid:
            with self.subTest(commits=commits):
                self.source_identity = self._identity(
                    commits,
                    {"shared": self.shared, "source-manifest": self.source_manifest},
                )
                with self.assertRaisesRegex(
                    self.module.checkpoint.CheckpointError,
                    "source identity is not an exact supported legacy shape",
                ):
                    self._validate()

    def test_migration_of_migration_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "migration-of-migration is forbidden",
        ):
            self.module.validate_rekey_contract(
                plan=self.plan,
                source_manifest={"migration": {"reason": "old"}},
                source_identity=self.source_identity,
                target_identity=self.target_identity,
            )

    def test_physical_artifact_digest_cannot_be_producer_binding(self) -> None:
        self.target_identity = self._identity(
            {
                "omarchy_iso_stage": self.source_manifest_identity,
                "omarchy_iso_producer": self.output_sha256,
            },
            {"shared": self.shared, "source-manifest": self.source_manifest},
        )
        self.plan["target_checkpoint_identity"] = self.target_identity[
            "checkpoint_identity"
        ]
        self.plan["target_producer_binding_identity"] = self.output_sha256
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "producer binding collides with a physical artifact digest",
        ):
            self._validate()

    def test_late_invalid_plan_cannot_seal_or_rekey_checkpoint(self) -> None:
        cache = self.root / "cache"
        output = self.root / "root.img"
        output.write_bytes(b"verified legacy output")
        stored = self.module.checkpoint.store_checkpoint(
            cache_root=cache,
            identity=self.source_identity,
            outputs={"root-image": output},
            elapsed_seconds=1,
        )
        output_record = stored["outputs"][0]
        self.plan.update(
            {
                "source_checkpoint_identity": self.source_identity[
                    "checkpoint_identity"
                ],
                "target_checkpoint_identity": self.target_identity[
                    "checkpoint_identity"
                ],
                "expected_outputs": {
                    "root-image": {
                        "sha256": output_record["sha256"],
                        "size_bytes": output_record["size_bytes"],
                    }
                },
                "legacy_immutable_admission": {},
                "projected_equivalent_inputs": {
                    "first-late-invalid-input": "first-late-invalid-input",
                    "second-late-invalid-input": "second-late-invalid-input",
                },
            }
        )
        checkpoint_directory = Path(stored["manifest_path"]).parent
        manifest_path = checkpoint_directory / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        self.plan["legacy_immutable_admission"] = {
            "kind": "legacy-checkpoint-immutable-admission-v1",
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "manifest_size_bytes": len(manifest_bytes),
        }
        object_path = (
            cache
            / "objects"
            / "sha256"
            / output_record["sha256"][:2]
            / output_record["sha256"]
        )
        observed_paths = (
            checkpoint_directory,
            checkpoint_directory / "outputs",
            manifest_path,
            object_path,
        )
        for path in observed_paths:
            path.chmod(0o755 if path.is_dir() else 0o644)
        metadata_before = {
            path: (
                path.lstat().st_mode,
                path.lstat().st_size,
                path.lstat().st_mtime_ns,
                path.lstat().st_ctime_ns,
            )
            for path in observed_paths
        }
        target_path = self.root / "target-identity.json"
        target_path.write_text(json.dumps(self.target_identity, sort_keys=True) + "\n")
        plan_path = self.root / "rekey-plan.json"
        plan_path.write_text(json.dumps(self.plan, sort_keys=True) + "\n")

        with (
            mock.patch.object(
                self.module.checkpoint,
                "seal_legacy_checkpoint",
                wraps=self.module.checkpoint.seal_legacy_checkpoint,
            ) as seal,
            mock.patch.object(
                self.module.checkpoint,
                "rekey_checkpoint",
                wraps=self.module.checkpoint.rekey_checkpoint,
            ) as rekey,
        ):
            with self.assertRaisesRegex(
                self.module.checkpoint.CheckpointError,
                "no current exact stage-declaration authority",
            ):
                self.module.apply_plan(
                    cache_root=cache,
                    target_identity_path=target_path,
                    plan_path=plan_path,
                )

        seal.assert_not_called()
        rekey.assert_not_called()
        self.assertEqual(
            metadata_before,
            {
                path: (
                    path.lstat().st_mode,
                    path.lstat().st_size,
                    path.lstat().st_mtime_ns,
                    path.lstat().st_ctime_ns,
                )
                for path in observed_paths
            },
        )
        target_checkpoint = (
            cache
            / "checkpoints"
            / "base-images"
            / self.target_identity["checkpoint_identity"]
        )
        self.assertFalse(target_checkpoint.exists())


class ConfiguredTargetTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.lock = self.root / "source-lock.json"
        self.lock.write_text('{"schema_version":1}\n')
        self.runtime = self.root / "configured-runtime.json"
        self.runtime.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "stage": "configured-target",
                    "input_digest": "1" * 64,
                },
                sort_keys=True,
            )
            + "\n"
        )
        self.product = self.root / "configured-product.json"
        self.product.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "stage": "configured-target",
                    "input_digest": "2" * 64,
                },
                sort_keys=True,
            )
            + "\n"
        )
        self.repository = self.root / "repository.json"
        self.repository.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "identity": "3" * 64,
                    "validation": {"result": "passed", "signatures": "required"},
                },
                sort_keys=True,
            )
            + "\n"
        )
        self.node = self.root / "node.tar.gz"
        self.node.write_bytes(b"exact node runtime")
        self.validator = self.root / "validator.py"
        self.validator.write_bytes(b"exact configured-target validator")
        self.source_manifest = self.root / "source-manifest.json"
        self.source_manifest.write_text('{"source_identity":"precise"}\n')
        self.outputs = {
            "root-image": {"sha256": "4" * 64, "size_bytes": 16},
            "boot-image": {"sha256": "5" * 64, "size_bytes": 8},
            "esp-image": {"sha256": "6" * 64, "size_bytes": 4},
            "stage-state": {"sha256": "7" * 64, "size_bytes": 2},
        }
        self.source_identity = {
            "stage": "configured-target",
            "checkpoint_identity": "8" * 64,
            "inputs": [
                {
                    "name": "build-implementation",
                    "kind": "file",
                    "sha256": "9" * 64,
                    "size_bytes": 1,
                },
                {
                    "name": "configured-source",
                    "kind": "file",
                    "sha256": "a" * 64,
                    "size_bytes": 1,
                },
            ],
        }
        self.transition = {
            "kind": "configured-target-installed-contract-v1",
            "source_build_implementation_sha256": "9" * 64,
            "source_configured_source_sha256": "a" * 64,
        }
        self.proof = self.root / "configured-target-contract.json"
        self._write_proof()
        self.target_identity = self._target_identity()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_proof(self, **changes) -> None:
        value = {
            "schema_version": 1,
            "verification_kind": "configured-target-installed-contract-v1",
            "validator_sha256": self.module.checkpoint.sha256_file(self.validator),
            "source_checkpoint_identity": self.source_identity["checkpoint_identity"],
            "checkpoint_outputs": self.outputs,
            "repository_identity": "3" * 64,
            "runtime_input_digest": "1" * 64,
            "product_input_digest": "2" * 64,
            "filesystems": {},
            "installed_packages": 918,
            "package_inventory_sha256": "b" * 64,
            "stage_state": {},
            "staged_node": {
                "filename": self.node.name,
                "sha256": self.module.checkpoint.sha256_file(self.node),
                "size_bytes": self.node.stat().st_size,
            },
            "validation": {"result": "passed"},
        }
        value.update(changes)
        value["proof_digest"] = canonical_digest(value)
        self.proof.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

    def _target_identity(self) -> dict:
        return self.module.checkpoint.build_identity(
            stage="configured-target",
            mode="diagnostic",
            source_lock=self.lock,
            source_commits={"omarchy_iso_stage": "c" * 64},
            inputs={
                "configured-contract-proof": self.proof,
                "configured-product": self.product,
                "configured-runtime": self.runtime,
                "node-runtime": self.node,
                "offline-repository": self.repository,
                "source-manifest": self.source_manifest,
            },
        )

    def _verify(self, **changes):
        arguments = {
            "source_identity": self.source_identity,
            "target_identity": self.target_identity,
            "transition": self.transition,
            "expected_outputs": self.outputs,
            "configured_contract_proof": self.proof,
            "configured_runtime_manifest": self.runtime,
            "configured_product_manifest": self.product,
            "configured_repository_manifest": self.repository,
            "configured_node_runtime": self.node,
            "configured_validator": self.validator,
        }
        arguments.update(changes)
        return self.module.verify_configured_target_transition(**arguments)

    def test_self_asserted_installed_contract_has_no_authority(self) -> None:
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "requires an executed authoritative validator",
        ):
            self._verify()

    def test_missing_contract_proof_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "configured transition inputs are missing",
        ):
            self._verify(configured_contract_proof=None)

    def test_contract_byte_change_fails_closed(self) -> None:
        self.proof.write_text(self.proof.read_text() + " ")
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "configured contract proof differs from target identity",
        ):
            self._verify()

    def test_validator_byte_change_fails_closed(self) -> None:
        self.validator.write_bytes(b"different validator")
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "configured contract validator differs",
        ):
            self._verify()

    def test_output_mismatch_fails_closed(self) -> None:
        changed_outputs = dict(self.outputs)
        changed_outputs["root-image"] = {
            "sha256": "d" * 64,
            "size_bytes": 16,
        }
        self._write_proof(checkpoint_outputs=changed_outputs)
        self.target_identity = self._target_identity()
        with self.assertRaisesRegex(
            self.module.checkpoint.CheckpointError,
            "configured contract outputs differ",
        ):
            self._verify()

    def test_checkpoint_input_object_path_adds_storage_descriptor(self) -> None:
        content = b"stored repository manifest"
        digest = hashlib.sha256(content).hexdigest()
        cache_root = self.root / "cache"
        object_path = cache_root / "objects" / "sha256" / digest[:2] / digest
        object_path.parent.mkdir(parents=True)
        object_path.write_bytes(content)
        object_path.chmod(0o444)
        record = {
            "kind": "file",
            "name": "offline-repository",
            "path": "offline-repository",
            "sha256": digest,
            "size_bytes": len(content),
            "executable_mode": 0,
        }
        self.assertEqual(
            self.module._checkpoint_input_object_path(
                cache_root,
                record,
                "configured repository manifest object",
            ),
            object_path,
        )


if __name__ == "__main__":
    unittest.main()
