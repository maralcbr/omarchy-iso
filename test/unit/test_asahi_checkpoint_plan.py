from __future__ import annotations

import importlib.util
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "builder"
MODULE_PATH = BUILDER / "asahi_checkpoint_plan.py"
CHECKPOINT_MODULE_PATH = BUILDER / "asahi_checkpoint.py"
STAGE_INPUT_MODULE_PATH = BUILDER / "asahi_stage_inputs.py"


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def run_record_accounting() -> dict:
    """The truthful accounting fields every emitted run record carries.

    Hand-built records in this file predate those fields; the parity test
    proves these stand-ins have the same shape the library really emits.
    """
    return {
        "bytes_read": 16,
        "bytes_written": 16,
        "verification_timing": {
            "checkpoint_verification_seconds": 0.01,
            "content_readback_seconds": 0.02,
            "transfer_seconds": 0.03,
        },
    }


class AsahiCheckpointPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        (self.repository / "builder").mkdir()
        for name, content in (
            ("tool.sh", "#!/bin/sh\n"),
            ("ensure-asahi-toolchain-image.sh", "#!/bin/bash\nexit 0\n"),
            ("asahi-toolchain.Containerfile", "FROM scratch\n"),
            ("configure.sh", "#!/bin/sh\n"),
            ("logo.bin", "logo\n"),
            ("seal.sh", "#!/bin/sh\n"),
            ("metadata.py", "print('metadata')\n"),
            ("admission.py", "print('admit')\n"),
        ):
            (self.repository / "builder" / name).write_text(content)
        subprocess.run(["git", "init", "-q", str(self.repository)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repository), "add", "builder"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "-c",
                "user.name=Omarchy Test",
                "-c",
                "user.email=omarchy-test.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        self.specification = {
            "schema_version": 1,
            "common_producer_inputs": [],
            "common_admission_inputs": ["builder/admission.py"],
            "stage_order": [
                "builder-toolchain",
                "configured-target",
                "finalized-boot",
                "sealed-release-package",
                "installer-metadata",
            ],
            "stages": {
                "builder-toolchain": self.declaration(
                    "builder/ensure-asahi-toolchain-image.sh"
                ),
                "configured-target": self.declaration(
                    "builder/configure.sh", "builder-toolchain"
                ),
                "finalized-boot": self.declaration(
                    "builder/logo.bin", "configured-target"
                ),
                "sealed-release-package": self.declaration(
                    "builder/seal.sh", "finalized-boot"
                ),
                "installer-metadata": self.declaration(
                    "builder/metadata.py",
                    "sealed-release-package",
                    "finalized-boot",
                ),
            },
        }
        self.specification["stages"]["builder-toolchain"][
            "source_paths"
        ].append("builder/asahi-toolchain.Containerfile")
        self.specification["stages"]["builder-toolchain"]["lock_paths"] = [
            "builder"
        ]
        self.cost_data = {
            "schema_version": 1,
            "metric": "elapsed-seconds",
            "stages": {},
        }
        self.cache = self.root / "cache"
        self.identities = self.root / "identities"
        self.identities.mkdir()
        self.lock = self.root / "source-lock.json"
        self.build_lock = {
            "schema_version": 1,
            "builder": {
                "base_image": "example.invalid/toolchain@sha256:" + "a" * 64,
                "maximum_workers": 10,
                "toolchain_packages": ["bash", "jq"],
            },
        }
        self.lock.write_text(json.dumps(self.build_lock, sort_keys=True) + "\n")
        self.checkpoint = load_module(
            "fixture_asahi_checkpoint", CHECKPOINT_MODULE_PATH
        )
        self.stage_inputs = load_module(
            "fixture_asahi_stage_inputs", STAGE_INPUT_MODULE_PATH
        )
        self.stage_input_roots = {}
        self.producer_bindings = {}
        for mode in ("diagnostic", "qualification"):
            output_root = self.root / f"stage-inputs-{mode}"
            self.stage_inputs.generate_stage_inputs(
                repository=self.repository,
                specification=self.specification,
                build_lock=self.build_lock,
                mode=mode,
                output_root=output_root,
            )
            self.stage_input_roots[mode] = output_root
            self.producer_bindings[mode] = (
                self.stage_inputs.declared_stage_identity_bindings(
                    repository=self.repository,
                    specification=self.specification,
                    build_lock=self.build_lock,
                    mode=mode,
                )
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def declaration(path: str, *depends_on: str) -> dict:
        return {
            "depends_on": list(depends_on),
            "entrypoints": [path],
            "source_paths": [path],
            "admission_paths": [],
            "lock_paths": [],
            "runtime_inputs": [],
            "runtime_settings": [],
        }

    @staticmethod
    def digest_json(value: object) -> str:
        import hashlib

        content = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    def toolchain_declared_inputs(self, mode: str) -> dict:
        import hashlib

        binding = self.producer_bindings[mode]["builder-toolchain"]
        script = self.repository / "builder/ensure-asahi-toolchain-image.sh"
        containerfile = self.repository / "builder/asahi-toolchain.Containerfile"
        return {
            "base_image": self.build_lock["builder"]["base_image"],
            "source_lock_sha256": binding["source_lock"]["sha256"],
            "containerfile_sha256": hashlib.sha256(
                containerfile.read_bytes()
            ).hexdigest(),
            "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            "source": {
                "omarchy_iso_stage": binding["source_identity"],
                "omarchy_iso_producer": binding[
                    "producer_binding_identity"
                ],
                "manifest_sha256": binding["source_manifest"]["sha256"],
            },
            "toolchain_packages": self.build_lock["builder"][
                "toolchain_packages"
            ],
        }

    def store_stage(
        self, stage: str, payload: bytes, *, mode: str = "diagnostic"
    ) -> dict:
        if stage == "builder-toolchain":
            return self.store_toolchain_stage(payload, mode=mode)
        identity = self.build_stage_identity(stage, mode=mode)
        (self.identities / f"{stage}.identity.json").write_text(
            json.dumps(identity, sort_keys=True)
        )
        output = self.root / f"{stage}.output"
        output.write_bytes(payload)
        return self.checkpoint.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"artifact": output},
            elapsed_seconds=1.0,
        )

    def store_toolchain_stage(
        self,
        payload: bytes,
        *,
        mode: str = "diagnostic",
    ) -> dict:
        import hashlib

        declared_inputs = self.toolchain_declared_inputs(mode)
        declared_digest = self.digest_json(declared_inputs)
        inventory = [f"bash fixture-{payload.hex()}-aarch64", "jq fixture-aarch64"]
        inventory_digest = hashlib.sha256(
            ("\n".join(inventory) + "\n").encode("utf-8")
        ).hexdigest()
        actual_inputs = declared_inputs | {
            "package_inventory_sha256": inventory_digest,
            "package_inventory": inventory,
            "synchronized_database_digests": [
                hashlib.sha256(b"sync" + payload).hexdigest()
                + "  /var/lib/pacman/sync/core.db"
            ],
        }
        checkpoint_identity = self.digest_json(actual_inputs)
        output = {
            "image_id": "sha256:"
            + hashlib.sha256(b"image" + payload).hexdigest(),
            "size_bytes": 4096 + len(payload),
            "package_inventory_sha256": inventory_digest,
        }
        completed_at = "2026-08-29T00:00:00Z"
        manifest = {
            "schema_version": 2,
            "stage": "builder-toolchain",
            "mode": "shared",
            "declared_inputs": declared_inputs,
            "declared_input_digest": declared_digest,
            "actual_inputs": actual_inputs,
            "checkpoint_identity": checkpoint_identity,
            "output": output,
            "validation": {"result": "passed"},
            "completed_at": completed_at,
            "elapsed_seconds": 1.0,
            "cache_hit": False,
            "immutable": True,
            "environment": "OMARCHY_ASAHI_TOOLCHAIN_PREPARED=1",
        }
        checkpoint = self.cache / "builder-toolchain" / checkpoint_identity
        checkpoint.mkdir(parents=True)
        manifest_path = checkpoint / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True))
        manifest_path.chmod(0o444)
        checkpoint.chmod(0o555)
        run_record = {
            "schema_version": 2,
            "stage": "builder-toolchain",
            "mode": "shared",
            "checkpoint_identity": checkpoint_identity,
            "input_digest": declared_digest,
            "validation": {"result": "passed"},
            "completed_at": completed_at,
            "elapsed_seconds": 0.0,
            "cache_hit": True,
            "output": output,
        }
        evidence_path = self.identities / "builder-toolchain.json"
        evidence_path.write_text(json.dumps(run_record, sort_keys=True))
        return {
            "schema_version": 2,
            "checkpoint_identity": checkpoint_identity,
            "manifest_path": str(manifest_path),
            "identity_evidence_path": str(evidence_path),
            "output": output,
            "outputs": [{"size_bytes": output["size_bytes"]}],
        }

    @staticmethod
    def rewrite_json(path: Path, mutate) -> None:
        path.chmod(0o644)
        value = json.loads(path.read_text())
        mutate(value)
        path.write_text(json.dumps(value, sort_keys=True))
        path.chmod(0o444)

    def plan_toolchain_candidate(self, name: str, *, profile: str = "diagnostic"):
        planner = load_module(name, MODULE_PATH)
        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/configure.sh"],
            intent="full",
            profile=profile,
            identity_root=self.identities,
            cache_root=self.cache,
        )
        return planner, plan

    def build_stage_identity(
        self, stage: str, *, mode: str = "diagnostic"
    ) -> dict:
        stage_root = self.stage_input_roots[mode] / stage
        source_manifest = json.loads(
            (stage_root / "source-manifest.json").read_text()
        )
        identity = self.checkpoint.build_identity(
            stage=stage,
            mode=mode,
            source_lock=stage_root / "source-lock.json",
            source_commits={
                "omarchy_iso_stage": source_manifest["source_identity"],
                "omarchy_iso_producer": source_manifest[
                    "producer_binding_identity"
                ],
            },
            inputs={"source-manifest": stage_root / "source-manifest.json"},
        )
        self.assertEqual(
            identity["source_lock"],
            self.producer_bindings[mode][stage]["source_lock"],
        )
        return identity

    def test_schema_two_shared_toolchain_is_a_metadata_only_candidate(self) -> None:
        stored = self.store_stage("builder-toolchain", b"toolchain")
        planner, plan = self.plan_toolchain_candidate(
            "asahi_checkpoint_plan_toolchain_schema_two"
        )

        record = {item["stage"]: item for item in plan["stages"]}[
            "builder-toolchain"
        ]
        self.assertEqual(record["classification"], "manifest-candidate-hit")
        self.assertEqual(record["identity"]["schema_version"], 2)
        self.assertEqual(record["identity"]["mode"], "shared")
        self.assertEqual(
            record["identity_evidence_kind"],
            "builder-toolchain-schema-2-run-record",
        )
        self.assertEqual(
            record["checkpoint_identity"], stored["checkpoint_identity"]
        )
        self.assertTrue(plan["advisory_selection_ready"])
        self.assertFalse(plan["checkpoint_content_verified"])
        self.assertFalse(plan["ready_for_execution"])
        self.assertFalse(plan["ready_for_authoritative_execution"])
        self.assertIn(
            "diagnostic-artifacts-are-qualification-ineligible",
            plan["authoritative_execution_blockers"],
        )
        with self.assertRaisesRegex(
            planner.CheckpointPlanError,
            "cannot authorize execution",
        ):
            planner.validate_execution_selection(plan)

    def test_schema_two_toolchain_rekey_cannot_claim_cache_hit(self) -> None:
        stored = self.store_stage("builder-toolchain", b"toolchain")
        self.rewrite_json(
            Path(stored["identity_evidence_path"]),
            lambda value: value.update(
                rekeyed=True,
                compatibility={
                    "schema_version": 1,
                    "reason": "stage-input-granularity-v1",
                    "source_checkpoint_identity": "f" * 64,
                    "source_lock_sha256": "e" * 64,
                    "target_lock_sha256": "d" * 64,
                },
            ),
        )

        _, plan = self.plan_toolchain_candidate(
            "asahi_checkpoint_plan_toolchain_rekey_cache_hit"
        )

        self.assertEqual(plan["unsafe_or_malformed_stages"], ["builder-toolchain"])
        self.assertIn("rekey claim is invalid", " ".join(plan["block_reasons"]))
        self.assertFalse(plan["advisory_selection_ready"])

    def test_schema_two_toolchain_mode_tamper_is_rejected(self) -> None:
        stored = self.store_stage("builder-toolchain", b"toolchain")
        self.rewrite_json(
            Path(stored["identity_evidence_path"]),
            lambda value: value.update(mode="diagnostic"),
        )

        _, plan = self.plan_toolchain_candidate(
            "asahi_checkpoint_plan_toolchain_mode_tamper"
        )

        self.assertEqual(plan["unsafe_or_malformed_stages"], ["builder-toolchain"])
        self.assertIn("run record is invalid", " ".join(plan["block_reasons"]))
        self.assertFalse(plan["advisory_selection_ready"])
        self.assertFalse(plan["ready_for_execution"])

    def test_schema_two_toolchain_digest_tamper_is_rejected(self) -> None:
        stored = self.store_stage("builder-toolchain", b"toolchain")
        self.rewrite_json(
            Path(stored["manifest_path"]),
            lambda value: value.update(declared_input_digest="f" * 64),
        )

        _, plan = self.plan_toolchain_candidate(
            "asahi_checkpoint_plan_toolchain_digest_tamper"
        )

        self.assertEqual(plan["unsafe_or_malformed_stages"], ["builder-toolchain"])
        self.assertIn("input digest is mismatched", " ".join(plan["block_reasons"]))
        self.assertFalse(plan["advisory_selection_ready"])

    def test_schema_two_toolchain_identity_tamper_is_rejected(self) -> None:
        stored = self.store_stage("builder-toolchain", b"toolchain")
        self.rewrite_json(
            Path(stored["manifest_path"]),
            lambda value: value.update(checkpoint_identity="f" * 64),
        )

        _, plan = self.plan_toolchain_candidate(
            "asahi_checkpoint_plan_toolchain_identity_tamper"
        )

        self.assertEqual(plan["unsafe_or_malformed_stages"], ["builder-toolchain"])
        self.assertIn("directory binding", " ".join(plan["block_reasons"]))
        self.assertFalse(plan["advisory_selection_ready"])

    def test_schema_two_toolchain_checkpoint_path_tamper_is_rejected(self) -> None:
        stored = self.store_stage("builder-toolchain", b"toolchain")
        wrong_identity = "f" * 64
        checkpoint = Path(stored["manifest_path"]).parent
        checkpoint.rename(checkpoint.parent / wrong_identity)
        self.rewrite_json(
            Path(stored["identity_evidence_path"]),
            lambda value: value.update(checkpoint_identity=wrong_identity),
        )

        _, plan = self.plan_toolchain_candidate(
            "asahi_checkpoint_plan_toolchain_checkpoint_path_tamper"
        )

        self.assertEqual(plan["unsafe_or_malformed_stages"], ["builder-toolchain"])
        self.assertIn("directory binding", " ".join(plan["block_reasons"]))
        self.assertFalse(plan["advisory_selection_ready"])

    def test_schema_two_toolchain_writable_ancestors_are_rejected(self) -> None:
        stored = self.store_stage("builder-toolchain", b"toolchain")
        checkpoint = Path(stored["manifest_path"]).parent
        ancestors = (
            ("cache-root", self.cache),
            ("stage-root", checkpoint.parent),
        )
        for index, (role, ancestor) in enumerate(ancestors):
            with self.subTest(role=role):
                ancestor.chmod(0o775)
                try:
                    _, plan = self.plan_toolchain_candidate(
                        f"asahi_checkpoint_plan_toolchain_writable_{index}"
                    )
                finally:
                    ancestor.chmod(0o755)
                self.assertEqual(
                    plan["unsafe_or_malformed_stages"],
                    ["builder-toolchain"],
                )
                self.assertIn(
                    "group/world writable",
                    " ".join(plan["block_reasons"]),
                )
                self.assertFalse(plan["advisory_selection_ready"])
                self.assertEqual(plan["execution_selection"]["restore_frontier"], [])

    def test_schema_two_toolchain_evidence_path_tamper_is_rejected(self) -> None:
        stored = self.store_stage("builder-toolchain", b"toolchain")
        evidence_path = Path(stored["identity_evidence_path"])
        relocated = self.root / "relocated-builder-toolchain.json"
        evidence_path.rename(relocated)
        evidence_path.symlink_to(relocated)

        _, plan = self.plan_toolchain_candidate(
            "asahi_checkpoint_plan_toolchain_path_tamper"
        )

        self.assertEqual(plan["unsafe_or_malformed_stages"], ["builder-toolchain"])
        self.assertIn("symlink is forbidden", " ".join(plan["block_reasons"]))
        self.assertFalse(plan["advisory_selection_ready"])

    def test_schema_two_toolchain_stale_current_binding_is_rejected(self) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        (self.repository / "builder/ensure-asahi-toolchain-image.sh").write_text(
            "#!/bin/bash\nprintf changed\n"
        )

        _, plan = self.plan_toolchain_candidate(
            "asahi_checkpoint_plan_toolchain_current_binding"
        )

        records = {item["stage"]: item for item in plan["stages"]}
        self.assertEqual(
            records["builder-toolchain"]["classification"],
            "producer-binding-rejected",
        )
        self.assertEqual(
            plan["producer_binding_rejected_stages"],
            ["builder-toolchain"],
        )
        self.assertIn("current inputs", " ".join(plan["block_reasons"]))
        self.assertFalse(plan["advisory_selection_ready"])

    def test_boot_change_selects_restore_frontier_without_content_claim(
        self,
    ) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        configured = self.store_stage("configured-target", b"configured-target")
        planner = load_module("asahi_checkpoint_plan", MODULE_PATH)
        output_record = configured["outputs"][0]
        object_path = (
            self.cache
            / "objects/sha256"
            / output_record["sha256"][:2]
            / output_record["sha256"]
        )
        original_open = Path.open

        def guarded_open(path: Path, *args, **kwargs):
            if path == object_path:
                raise AssertionError("planner must not open checkpoint object content")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            plan = planner.plan_checkpoint_execution(
                repository=self.repository,
                specification=self.specification,
                build_lock=self.build_lock,
                cost_data=self.cost_data,
                changed_paths=["builder/logo.bin"],
                intent="boot-only",
                profile="diagnostic",
                identity_root=self.identities,
                cache_root=self.cache,
            )

        by_stage = {stage["stage"]: stage for stage in plan["stages"]}
        self.assertEqual(
            by_stage["configured-target"]["classification"],
            "manifest-candidate-hit",
        )
        self.assertEqual(
            by_stage["configured-target"]["content_verification"],
            "deferred-content-verification",
        )
        self.assertEqual(
            by_stage["configured-target"]["classifications"],
            ["manifest-candidate-hit", "deferred-content-verification"],
        )
        self.assertEqual(
            by_stage["finalized-boot"]["classification"],
            "declared-invalidated",
        )
        self.assertEqual(
            plan["materialization_forecast"]["stages"], ["configured-target"]
        )
        self.assertEqual(
            plan["materialization_forecast"]["referenced_bytes"],
            configured["outputs"][0]["size_bytes"],
        )
        self.assertFalse(plan["checkpoint_content_verified"])
        self.assertFalse(plan["current_producer_inputs_verified"])
        self.assertFalse(plan["ready_for_authoritative_execution"])
        self.assertTrue(plan["advisory_selection_ready"])
        self.assertFalse(plan["ready_for_execution"])
        self.assertEqual(
            [
                record["stage"]
                for record in plan["execution_selection"]["restore_frontier"]
            ],
            ["configured-target"],
        )
        self.assertEqual(
            plan["execution_selection"]["restore_frontier"][0]["identity"][
                "checkpoint_identity"
            ],
            configured["checkpoint_identity"],
        )
        self.assertRegex(
            plan["execution_selection"]["restore_frontier"][0][
                "artifact_set_identity"
            ],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            plan["execution_selection"]["first_stage_to_run"], "finalized-boot"
        )
        self.assertEqual(
            plan["execution_selection"]["skipped_stages"],
            ["builder-toolchain", "configured-target"],
        )
        self.assertEqual(
            plan["execution_selection"]["stages_to_run"], ["finalized-boot"]
        )
        self.assertEqual(
            plan["execution_selection"]["checkpoint_state"],
            "content-verification-and-admission-required",
        )
        planner.validate_plan_digest(plan)
        planner.validate_advisory_selection(
            plan,
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            identity_root=self.identities,
            cache_root=self.cache,
            expected_profile="diagnostic",
        )
        with self.assertRaisesRegex(
            planner.CheckpointPlanError, "cannot authorize execution"
        ):
            planner.validate_execution_selection(plan)
        tampered = copy.deepcopy(plan)
        tampered["materialization_forecast"]["referenced_bytes"] += 1
        with self.assertRaisesRegex(planner.CheckpointPlanError, "plan digest"):
            planner.validate_plan_digest(tampered)

        reordered = copy.deepcopy(plan)
        reordered["execution_selection"]["skipped_stages"].reverse()
        reordered["plan_digest"] = planner._digest(
            {key: value for key, value in reordered.items() if key != "plan_digest"}
        )
        with self.assertRaisesRegex(
            planner.CheckpointPlanError, "advisory selection drifted"
        ):
            planner.validate_advisory_selection(
                reordered,
                repository=self.repository,
                specification=self.specification,
                build_lock=self.build_lock,
                cost_data=self.cost_data,
                identity_root=self.identities,
                cache_root=self.cache,
                expected_profile="diagnostic",
            )

    def test_qualification_metadata_change_restores_both_direct_parents(
        self,
    ) -> None:
        stored = {}
        for stage in (
            "builder-toolchain",
            "configured-target",
            "finalized-boot",
            "sealed-release-package",
        ):
            stored[stage] = self.store_stage(
                stage, stage.encode(), mode="qualification"
            )
        planner = load_module("asahi_checkpoint_plan_dag", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/metadata.py"],
            intent="boot-only",
            profile="qualification",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        self.assertTrue(plan["advisory_selection_ready"])
        toolchain_record = {item["stage"]: item for item in plan["stages"]}[
            "builder-toolchain"
        ]
        self.assertEqual(toolchain_record["identity"]["schema_version"], 2)
        self.assertEqual(toolchain_record["identity"]["mode"], "shared")
        self.assertFalse(plan["ready_for_authoritative_execution"])
        self.assertIn(
            "qualification-receipt-authority-unavailable",
            plan["authoritative_execution_blockers"],
        )
        self.assertEqual(
            plan["execution_selection"]["stages_to_run"], ["installer-metadata"]
        )
        self.assertEqual(
            [
                record["stage"]
                for record in plan["execution_selection"]["restore_frontier"]
            ],
            ["finalized-boot", "sealed-release-package"],
        )
        self.assertEqual(
            plan["materialization_forecast"]["referenced_bytes"],
            sum(
                stored[stage]["outputs"][0]["size_bytes"]
                for stage in ("finalized-boot", "sealed-release-package")
            ),
        )

    # -- resume context (plan Phase C3, modelling only) --------------------

    def regenerate_stage_inputs(self) -> None:
        """Rebuild stage inputs after the fixture graph is changed in a test.

        Stage inputs and producer bindings are generated once in setUp, so a
        test that alters the dependency graph has to regenerate them or every
        stored identity binds a declaration that no longer exists.
        """
        for mode in ("diagnostic", "qualification"):
            output_root = self.root / f"stage-inputs-{mode}-regenerated"
            self.stage_inputs.generate_stage_inputs(
                repository=self.repository,
                specification=self.specification,
                build_lock=self.build_lock,
                mode=mode,
                output_root=output_root,
            )
            self.stage_input_roots[mode] = output_root
            self.producer_bindings[mode] = (
                self.stage_inputs.declared_stage_identity_bindings(
                    repository=self.repository,
                    specification=self.specification,
                    build_lock=self.build_lock,
                    mode=mode,
                )
            )

    def qualification_plan(self, name: str, stages: tuple[str, ...]) -> dict:
        for stage in stages:
            self.store_stage(stage, stage.encode(), mode="qualification")
        planner = load_module(name, MODULE_PATH)
        return planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/metadata.py"],
            intent="boot-only",
            profile="qualification",
            identity_root=self.identities,
            cache_root=self.cache,
        )

    def test_resume_context_models_typed_output_handles(self) -> None:
        stored = {
            stage: self.store_stage(stage, stage.encode(), mode="qualification")
            for stage in (
                "builder-toolchain",
                "configured-target",
                "finalized-boot",
                "sealed-release-package",
            )
        }
        planner = load_module("asahi_checkpoint_plan_resume_handles", MODULE_PATH)
        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/metadata.py"],
            intent="boot-only",
            profile="qualification",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        frontier = {
            record["stage"]: record
            for record in plan["execution_selection"]["restore_frontier"]
        }
        context = frontier["finalized-boot"]["resume_context"]

        self.assertEqual(context["verification_kind"], "asahi-checkpoint-resume-context")
        self.assertEqual(context["claim_scope"], "checkpoint-metadata-only")
        self.assertEqual(context["stage"], "finalized-boot")

        # Handles bind exactly the outputs the manifest declared, with the
        # restore-mode metadata the manifest carries.
        expected = stored["finalized-boot"]["outputs"]
        self.assertEqual(len(context["output_handles"]), len(expected))
        handle = context["output_handles"][0]
        declared = expected[0]
        self.assertEqual(handle["name"], declared["name"])
        self.assertEqual(handle["kind"], declared["kind"])
        self.assertEqual(handle["sha256"], declared["sha256"])
        self.assertEqual(handle["size_bytes"], declared["size_bytes"])
        self.assertEqual(handle["restore_mode"], declared["restore_mode"])
        self.assertEqual(handle["storage_kind"], declared["storage"]["kind"])
        self.assertTrue(handle["restorable_via_destinations"])

    def test_resume_context_states_the_destination_set_contract(self) -> None:
        plan = self.qualification_plan(
            "asahi_checkpoint_plan_resume_destinations",
            ("builder-toolchain", "configured-target", "finalized-boot",
             "sealed-release-package"),
        )
        frontier = {
            record["stage"]: record
            for record in plan["execution_selection"]["restore_frontier"]
        }
        context = frontier["finalized-boot"]["resume_context"]

        # Restore refuses unless the destination set equals the output set, so
        # the context must state that set exactly. An executor binds paths from
        # this without reading any stage script.
        self.assertEqual(
            context["required_destination_names"],
            sorted(handle["name"] for handle in context["output_handles"]),
        )
        self.assertIn("must equal", context["destination_set_contract"])

    def test_resume_context_models_every_parent_of_a_multi_parent_stage(self) -> None:
        # The shipped graph's canonical multi-parent stage is configured-target,
        # which depends on both base-images and offline-repository-database.
        # This fixture graph is linear, so give one stage two real parents and
        # assert the whole set is modelled.
        self.specification["stages"]["sealed-release-package"] = self.declaration(
            "builder/seal.sh", "finalized-boot", "configured-target"
        )
        self.regenerate_stage_inputs()
        plan = self.qualification_plan(
            "asahi_checkpoint_plan_resume_multi_parent",
            ("builder-toolchain", "configured-target", "finalized-boot",
             "sealed-release-package"),
        )
        frontier = {
            record["stage"]: record
            for record in plan["execution_selection"]["restore_frontier"]
        }
        context = frontier["sealed-release-package"]["resume_context"]

        self.assertEqual(context["parent_count"], 2)
        self.assertEqual(
            sorted(parent["stage"] for parent in context["parents"]),
            ["configured-target", "finalized-boot"],
        )
        for parent in context["parents"]:
            with self.subTest(parent=parent["stage"]):
                self.assertTrue(parent["candidate_available"])
                self.assertRegex(parent["checkpoint_identity"], r"^[0-9a-f]{64}$")
                self.assertRegex(parent["artifact_set_identity"], r"^[0-9a-f]{64}$")
        self.assertEqual(context["unresolved_parents"], [])

    def test_shipped_specification_has_the_canonical_multi_parent_stage(self) -> None:
        # Ties the fixture above to the real graph: configured-target really is
        # the two-parent case the resume context has to handle.
        specification = json.loads(
            (ROOT / "builder/asahi-stage-inputs.json").read_text()
        )

        self.assertEqual(
            specification["stages"]["configured-target"]["depends_on"],
            ["base-images", "offline-repository-database"],
        )

    def test_a_parent_without_a_candidate_blocks_the_whole_plan(self) -> None:
        # The resume context carries candidate_available and unresolved_parents
        # so an unresolved parent is explicit rather than silently absent. In
        # the current planner that state is unreachable in a selectable plan:
        # a parent that should be reusable but has no candidate is an
        # unexpected expensive miss, which blocks selection outright. That is
        # strictly stronger than surfacing a marker, so it is what gets pinned.
        self.specification["stages"]["sealed-release-package"] = self.declaration(
            "builder/seal.sh", "finalized-boot", "configured-target"
        )
        self.regenerate_stage_inputs()
        plan = self.qualification_plan(
            "asahi_checkpoint_plan_resume_missing_parent",
            ("builder-toolchain", "finalized-boot", "sealed-release-package"),
        )

        classifications = {
            record["stage"]: record["classification"] for record in plan["stages"]
        }
        self.assertEqual(classifications["configured-target"], "missing/rejected")
        self.assertFalse(plan["advisory_selection_ready"])
        self.assertIn(
            "unexpected expensive miss outside declared invalidation frontier: "
            "configured-target",
            plan["block_reasons"],
        )
        # Blocked, so nothing actionable is published.
        self.assertEqual(plan["execution_selection"]["restore_frontier"], [])
        self.assertNotIn("asahi-checkpoint-resume-context", json.dumps(plan))

    def test_resume_context_marks_parent_resolution_explicitly(self) -> None:
        # The positive half of the same contract: when every parent resolves,
        # each is named with its identities and the unresolved list is empty,
        # so an executor never has to infer a parent's absence from silence.
        self.specification["stages"]["sealed-release-package"] = self.declaration(
            "builder/seal.sh", "finalized-boot", "configured-target"
        )
        self.regenerate_stage_inputs()
        plan = self.qualification_plan(
            "asahi_checkpoint_plan_resume_parent_marks",
            ("builder-toolchain", "configured-target", "finalized-boot",
             "sealed-release-package"),
        )
        frontier = {
            record["stage"]: record
            for record in plan["execution_selection"]["restore_frontier"]
        }
        context = frontier["sealed-release-package"]["resume_context"]

        self.assertEqual(
            sorted(context["parents"], key=lambda parent: parent["stage"]),
            sorted(
                (
                    {
                        "stage": parent["stage"],
                        "candidate_available": True,
                        "checkpoint_identity": parent["checkpoint_identity"],
                        "artifact_set_identity": parent["artifact_set_identity"],
                    }
                    for parent in context["parents"]
                ),
                key=lambda parent: parent["stage"],
            ),
        )
        self.assertEqual(context["unresolved_parents"], [])
        self.assertTrue(all(parent["candidate_available"] for parent in context["parents"]))

    def test_a_blocked_plan_exposes_no_resume_context(self) -> None:
        # A blocked plan must not leak actionable handles. It carries the same
        # fail-closed emptiness the frontiers already have.
        self.store_stage("builder-toolchain", b"toolchain")
        planner = load_module("asahi_checkpoint_plan_resume_blocked", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/unknown-executable.sh"],
            intent="boot-only",
            profile="qualification",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        self.assertFalse(plan["advisory_selection_ready"])
        self.assertEqual(plan["execution_selection"]["restore_frontier"], [])
        self.assertEqual(plan["execution_selection"]["admission_frontier"], [])
        serialized = json.dumps(plan)
        self.assertNotIn("asahi-checkpoint-resume-context", serialized)
        self.assertNotIn("required_destination_names", serialized)

    def test_authoritative_execution_blockers_are_the_truthful_list(self) -> None:
        # "complete-resume-context-not-modeled" was removed 2026-08-30: every
        # selected stage now carries typed output handles, its full parent set,
        # and the destination-name set restore requires, so the blocker was no
        # longer true. Everything else in the list is unchanged, and modelling
        # authorizes nothing -- the plan is still not executable.
        plan = self.qualification_plan(
            "asahi_checkpoint_plan_blocker_truth",
            ("builder-toolchain", "configured-target", "finalized-boot",
             "sealed-release-package"),
        )

        self.assertEqual(
            plan["authoritative_execution_blockers"],
            [
                "checkpoint-content-unverified",
                "full-producer-descriptor-not-recomputed",
                "read-only-admission-adapter-unimplemented",
                "current-admission-receipts-missing",
                "qualification-receipt-authority-unavailable",
            ],
        )
        self.assertNotIn(
            "complete-resume-context-not-modeled",
            plan["authoritative_execution_blockers"],
        )
        self.assertFalse(plan["ready_for_authoritative_execution"])
        self.assertFalse(plan["ready_for_execution"])
        self.assertFalse(plan["checkpoint_content_verified"])

    def test_a_tampered_output_never_reaches_a_resume_context(self) -> None:
        # The handles are built from data the candidate validation already
        # checked, so a tampered output is refused upstream rather than being
        # copied into the context.
        stored = self.store_stage(
            "finalized-boot", b"boot", mode="qualification"
        )
        self.store_stage("builder-toolchain", b"toolchain", mode="qualification")
        self.rewrite_json(
            Path(stored["manifest_path"]),
            lambda value: value["outputs"][0].update(sha256="f" * 64),
        )
        planner = load_module("asahi_checkpoint_plan_resume_tamper", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/metadata.py"],
            intent="boot-only",
            profile="qualification",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        self.assertIn("finalized-boot", plan["unsafe_or_malformed_stages"])
        serialized = json.dumps(plan)
        self.assertNotIn("f" * 64, serialized)

    def test_advisory_validation_rejects_unreported_producer_drift(self) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        self.store_stage("configured-target", b"configured-target")
        planner = load_module("asahi_checkpoint_plan_producer_drift", MODULE_PATH)
        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/logo.bin"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )
        self.assertTrue(plan["advisory_selection_ready"])

        (self.repository / "builder/configure.sh").write_text(
            "#!/bin/sh\nprintf changed\n"
        )
        with self.assertRaisesRegex(
            planner.CheckpointPlanError, "advisory selection drifted"
        ):
            planner.validate_advisory_selection(
                plan,
                repository=self.repository,
                specification=self.specification,
                build_lock=self.build_lock,
                cost_data=self.cost_data,
                identity_root=self.identities,
                cache_root=self.cache,
                expected_profile="diagnostic",
            )

    def test_current_producer_claim_with_stale_source_lock_is_rejected(self) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        identity = self.build_stage_identity("configured-target")
        identity["source_lock"]["sha256"] = "f" * 64
        unsigned = {
            key: value
            for key, value in identity.items()
            if key not in {"input_digest", "checkpoint_identity"}
        }
        identity["input_digest"] = self.checkpoint._json_digest(unsigned)
        identity["checkpoint_identity"] = self.checkpoint._json_digest(
            unsigned | {"input_digest": identity["input_digest"]}
        )
        (self.identities / "configured-target.identity.json").write_text(
            json.dumps(identity, sort_keys=True)
        )
        output = self.root / "configured-target-stale.output"
        output.write_bytes(b"configured-target")
        self.checkpoint.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"artifact": output},
            elapsed_seconds=1.0,
        )
        planner = load_module("asahi_checkpoint_plan_stale_lock", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/logo.bin"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        by_stage = {record["stage"]: record for record in plan["stages"]}
        self.assertEqual(
            by_stage["configured-target"]["classification"],
            "producer-binding-rejected",
        )
        self.assertIn(
            "source lock",
            " ".join(plan["block_reasons"]),
        )

    def test_missing_immediate_predecessor_blocks_unexpected_expensive_rebuild(
        self,
    ) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        identity = self.build_stage_identity("configured-target")
        (self.identities / "configured-target.identity.json").write_text(
            json.dumps(identity, sort_keys=True)
        )
        planner = load_module("asahi_checkpoint_plan_missing", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/logo.bin"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        by_stage = {stage["stage"]: stage for stage in plan["stages"]}
        self.assertEqual(
            by_stage["configured-target"]["classification"], "missing/rejected"
        )
        self.assertEqual(plan["unexpected_expensive_misses"], ["configured-target"])
        self.assertFalse(plan["advisory_selection_ready"])
        self.assertFalse(plan["ready_for_execution"])
        self.assertIn("unexpected expensive miss", plan["block_reasons"][0])
        self.assertEqual(plan["execution_selection"]["restore_frontier"], [])
        self.assertEqual(plan["execution_selection"]["admission_frontier"], [])
        self.assertEqual(plan["execution_selection"]["stages_to_run"], [])
        self.assertEqual(plan["execution_selection"]["skipped_stages"], [])
        self.assertIsNone(plan["execution_selection"]["first_stage_to_run"])
        self.assertEqual(
            plan["execution_selection"]["checkpoint_state"],
            "blocked-no-execution-selection",
        )

    def test_policy_only_change_readmits_terminal_checkpoint_without_rebuild(self) -> None:
        for stage in ("builder-toolchain", "configured-target", "finalized-boot"):
            self.store_stage(stage, stage.encode())
        planner = load_module("asahi_checkpoint_plan_policy_only", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/admission.py"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        self.assertEqual(plan["source_preview"]["invalidation_frontier"], [])
        self.assertTrue(plan["source_preview"]["requires_readmission"])
        self.assertTrue(plan["advisory_selection_ready"])
        self.assertFalse(plan["ready_for_execution"])
        self.assertEqual(
            [
                record["stage"]
                for record in plan["execution_selection"]["admission_frontier"]
            ],
            ["builder-toolchain", "configured-target", "finalized-boot"],
        )
        self.assertEqual(plan["execution_selection"]["restore_frontier"], [])
        self.assertEqual(plan["execution_selection"]["stages_to_run"], [])
        self.assertIsNone(plan["execution_selection"]["first_stage_to_run"])
        self.assertEqual(
            plan["execution_selection"]["checkpoint_state"],
            "current-policy-admission-required",
        )
        producer_before = dict(plan["producer_binding_identities"])
        admission_before = dict(plan["admission_policy_identities"])
        planner.validate_advisory_selection(
            plan,
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            identity_root=self.identities,
            cache_root=self.cache,
            expected_profile="diagnostic",
        )
        (self.repository / "builder/admission.py").write_text("print('changed')\n")
        all_producer_after = self.stage_inputs.declared_stage_fingerprints(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            mode="diagnostic",
        )
        producer_after = {
            stage: all_producer_after[stage] for stage in producer_before
        }
        all_admission_after = self.stage_inputs.declared_admission_fingerprints(
            repository=self.repository,
            specification=self.specification,
            mode="diagnostic",
        )
        admission_after = {
            stage: all_admission_after[stage] for stage in admission_before
        }
        self.assertEqual(producer_before, producer_after)
        self.assertNotEqual(admission_before, admission_after)
        with self.assertRaisesRegex(
            planner.CheckpointPlanError, "advisory selection drifted"
        ):
            planner.validate_advisory_selection(
                plan,
                repository=self.repository,
                specification=self.specification,
                build_lock=self.build_lock,
                cost_data=self.cost_data,
                identity_root=self.identities,
                cache_root=self.cache,
                expected_profile="diagnostic",
            )

    def test_unsafe_bypassed_manifest_still_fails_closed(self) -> None:
        builder = self.store_stage("builder-toolchain", b"toolchain")
        self.store_stage("configured-target", b"configured-target")
        Path(builder["manifest_path"]).chmod(0o644)
        planner = load_module("asahi_checkpoint_plan_unsafe", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/logo.bin"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        self.assertEqual(plan["execution_selection"]["restore_frontier"], [])
        self.assertEqual(plan["unsafe_or_malformed_stages"], ["builder-toolchain"])
        self.assertTrue(plan["blocked"])
        self.assertIn("writable", " ".join(plan["block_reasons"]))

    def test_inline_checkpoint_tree_symlink_is_rejected_without_hashing(self) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        identity = self.build_stage_identity("configured-target")
        (self.identities / "configured-target.identity.json").write_text(
            json.dumps(identity, sort_keys=True)
        )
        output = self.root / "configured-directory.output"
        output.mkdir()
        (output / "state.json").write_text('{"result":"passed"}\n')
        stored = self.checkpoint.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"artifact": output},
            elapsed_seconds=1.0,
        )
        inline = Path(stored["manifest_path"]).parent / "outputs" / "artifact"
        inline.chmod(0o755)
        (inline / "unsafe-link").symlink_to(self.root / "outside")
        inline.chmod(0o555)
        planner = load_module("asahi_checkpoint_plan_inline", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/logo.bin"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        by_stage = {stage["stage"]: stage for stage in plan["stages"]}
        self.assertEqual(
            by_stage["configured-target"]["classification"], "missing/rejected"
        )
        self.assertEqual(plan["unsafe_or_malformed_stages"], ["configured-target"])
        self.assertIn("symlink", " ".join(plan["block_reasons"]))

    def test_output_record_with_unknown_field_is_rejected(self) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        configured = self.store_stage("configured-target", b"configured-target")
        manifest_path = Path(configured["manifest_path"])
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest["outputs"][0]["undeclared"] = True
        manifest_path.write_text(json.dumps(manifest))
        manifest_path.chmod(0o444)
        planner = load_module("asahi_checkpoint_plan_output_fields", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/logo.bin"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        by_stage = {stage["stage"]: stage for stage in plan["stages"]}
        self.assertEqual(
            by_stage["configured-target"]["classification"], "missing/rejected"
        )
        self.assertIn("output fields", " ".join(plan["block_reasons"]))

    def test_invalid_checkpoint_migration_metadata_is_rejected(self) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        configured = self.store_stage("configured-target", b"configured-target")
        manifest_path = Path(configured["manifest_path"])
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest["migration"] = {
            "source_checkpoint_identity": "not-a-digest",
            "reason": "stage-input-granularity-v1",
            "transition_digest": "f" * 64,
        }
        manifest_path.write_text(json.dumps(manifest))
        manifest_path.chmod(0o444)
        planner = load_module("asahi_checkpoint_plan_migration", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/logo.bin"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        by_stage = {stage["stage"]: stage for stage in plan["stages"]}
        self.assertEqual(
            by_stage["configured-target"]["classification"], "missing/rejected"
        )
        self.assertIn("migration", " ".join(plan["block_reasons"]))

    def test_symlinked_object_store_ancestor_is_rejected(self) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        configured = self.store_stage("configured-target", b"configured-target")
        digest = configured["outputs"][0]["sha256"]
        prefix = self.cache / "objects" / "sha256" / digest[:2]
        relocated = self.root / "relocated-object-prefix"
        prefix.rename(relocated)
        prefix.symlink_to(relocated, target_is_directory=True)
        planner = load_module("asahi_checkpoint_plan_ancestor", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/logo.bin"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        by_stage = {stage["stage"]: stage for stage in plan["stages"]}
        self.assertEqual(
            by_stage["configured-target"]["classification"], "missing/rejected"
        )
        self.assertIn("symlink", " ".join(plan["block_reasons"]))

    def test_preview_drift_is_rejected_before_checkpoint_planning(self) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        self.store_stage("configured-target", b"configured-target")
        planner = load_module("asahi_checkpoint_plan_drift", MODULE_PATH)
        baseline = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/logo.bin"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )
        stale_preview = copy.deepcopy(baseline["source_preview"])
        stale_preview["invalidation_frontier"] = []

        with self.assertRaisesRegex(planner.CheckpointPlanError, "preview drift"):
            planner.plan_checkpoint_execution(
                repository=self.repository,
                specification=self.specification,
                build_lock=self.build_lock,
                cost_data=self.cost_data,
                changed_paths=["builder/logo.bin"],
                intent="boot-only",
                profile="diagnostic",
                identity_root=self.identities,
                cache_root=self.cache,
                expected_preview=stale_preview,
            )

    def test_unknown_executable_input_blocks_even_with_complete_candidates(
        self,
    ) -> None:
        for stage in ("builder-toolchain", "configured-target", "finalized-boot"):
            self.store_stage(stage, stage.encode())
        planner = load_module("asahi_checkpoint_plan_unknown", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/new-stage-helper.py"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        self.assertTrue(plan["blocked"])
        self.assertEqual(plan["unexpected_expensive_misses"], [])
        self.assertIn("unknown executable input", " ".join(plan["block_reasons"]))

    def test_cli_emits_subsecond_metadata_only_plan(self) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        self.store_stage("configured-target", b"configured-target")
        specification = self.root / "stages.json"
        costs = self.root / "costs.json"
        specification.write_text(json.dumps(self.specification))
        costs.write_text(json.dumps(self.cost_data))

        command = [
            sys.executable,
            str(MODULE_PATH),
            "--repo-root",
            str(self.repository),
            "--spec",
            str(specification),
            "--cost-data",
            str(costs),
            "--build-lock",
            str(self.lock),
            "--identity-root",
            str(self.identities),
            "--cache-root",
            str(self.cache),
            "--changed-path",
            "builder/logo.bin",
            "--intent",
            "boot-only",
            "--profile",
            "diagnostic",
        ]
        started = time.monotonic()
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["claim_scope"], "checkpoint-metadata-only")
        self.assertFalse(plan["checkpoint_content_verified"])
        blocked_command = list(command)
        blocked_command[
            blocked_command.index("builder/logo.bin")
        ] = "builder/new-stage-helper.py"
        blocked = subprocess.run(
            blocked_command,
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        elapsed = time.monotonic() - started

        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertTrue(json.loads(blocked.stdout)["blocked"])
        self.assertLess(elapsed, 1.0)

    def test_retained_run_records_can_supply_exact_identity_fields(self) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        stage = "configured-target"
        stored = self.store_stage(stage, b"configured-target")
        identity_path = self.identities / f"{stage}.identity.json"
        identity = json.loads(identity_path.read_text())
        identity_path.unlink()
        run_record = identity | run_record_accounting() | {
            "outputs": stored["outputs"],
            "validation": {"result": "passed"},
            "completed_at": "2026-08-29T00:00:00Z",
            "elapsed_seconds": 1.0,
            "cache_hit": True,
            "checkpoint_manifest": stored["manifest_path"],
        }
        (self.identities / f"{stage}.json").write_text(json.dumps(run_record))
        planner = load_module("asahi_checkpoint_plan_run_record", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/logo.bin"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        self.assertTrue(plan["advisory_selection_ready"])
        self.assertFalse(plan["ready_for_execution"])
        self.assertEqual(
            [
                record["stage"]
                for record in plan["execution_selection"]["restore_frontier"]
            ],
            ["configured-target"],
        )

    def test_retained_recomputation_match_record_is_not_a_cache_hit(self) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        stage = "configured-target"
        stored = self.store_stage(stage, b"configured-target")
        identity_path = self.identities / f"{stage}.identity.json"
        identity = json.loads(identity_path.read_text())
        identity_path.unlink()
        run_record = identity | run_record_accounting() | {
            "outputs": stored["outputs"],
            "validation": {"result": "passed"},
            "completed_at": "2026-08-29T00:00:00Z",
            "elapsed_seconds": 1.0,
            "cache_hit": False,
            "reproducibility_match": True,
            "checkpoint_manifest": stored["manifest_path"],
        }
        (self.identities / f"{stage}.json").write_text(json.dumps(run_record))
        planner = load_module("asahi_checkpoint_plan_recomputation_match", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/logo.bin"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        by_stage = {record["stage"]: record for record in plan["stages"]}
        self.assertEqual(
            by_stage[stage]["identity_evidence_kind"],
            "retained-run-record",
        )
        self.assertFalse(plan["ready_for_execution"])

    def test_retained_recomputation_match_metadata_fails_closed(self) -> None:
        self.store_stage("builder-toolchain", b"toolchain")
        stage = "configured-target"
        stored = self.store_stage(stage, b"configured-target")
        identity_path = self.identities / f"{stage}.identity.json"
        identity = json.loads(identity_path.read_text())
        identity_path.unlink()
        evidence_path = self.identities / f"{stage}.json"
        planner = load_module(
            "asahi_checkpoint_plan_invalid_recomputation_match",
            MODULE_PATH,
        )

        for marker, cache_hit in ((False, False), ("true", False), (True, True)):
            with self.subTest(marker=marker, cache_hit=cache_hit):
                run_record = identity | run_record_accounting() | {
                    "outputs": stored["outputs"],
                    "validation": {"result": "passed"},
                    "completed_at": "2026-08-29T00:00:00Z",
                    "elapsed_seconds": 1.0,
                    "cache_hit": cache_hit,
                    "reproducibility_match": marker,
                    "checkpoint_manifest": stored["manifest_path"],
                }
                evidence_path.write_text(json.dumps(run_record))
                plan = planner.plan_checkpoint_execution(
                    repository=self.repository,
                    specification=self.specification,
                    build_lock=self.build_lock,
                    cost_data=self.cost_data,
                    changed_paths=["builder/logo.bin"],
                    intent="boot-only",
                    profile="diagnostic",
                    identity_root=self.identities,
                    cache_root=self.cache,
                )

                by_stage = {record["stage"]: record for record in plan["stages"]}
                self.assertEqual(
                    by_stage[stage]["classification"],
                    "missing/rejected",
                )
                self.assertIn(
                    "reproducibility match metadata",
                    " ".join(plan["block_reasons"]),
                )

    def test_retained_run_record_with_unknown_fields_is_rejected(self) -> None:
        self.store_stage("builder-toolchain", b"builder-toolchain")
        stage = "configured-target"
        stored = self.store_stage(stage, stage.encode())
        identity_path = self.identities / f"{stage}.identity.json"
        identity = json.loads(identity_path.read_text())
        identity_path.unlink()
        run_record = identity | run_record_accounting() | {
            "outputs": stored["outputs"],
            "validation": {"result": "passed"},
            "completed_at": "2026-08-29T00:00:00Z",
            "elapsed_seconds": 1.0,
            "cache_hit": True,
            "checkpoint_manifest": stored["manifest_path"],
            "package_evidence": {"unrelated": True},
        }
        (self.identities / f"{stage}.json").write_text(json.dumps(run_record))
        planner = load_module("asahi_checkpoint_plan_run_record_fields", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/logo.bin"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        by_stage = {record["stage"]: record for record in plan["stages"]}
        self.assertEqual(
            by_stage["configured-target"]["classification"], "missing/rejected"
        )
        self.assertIn("run record fields", " ".join(plan["block_reasons"]))

    # -- emitted-versus-accepted parity (plan slice C5) ----------------------

    def emit_real_run_records(self, stage: str) -> dict[str, dict]:
        """Drive real store, reproducibility and restore records through the
        checkpoint library, so the shapes under test are the emitted ones."""
        identity = self.build_stage_identity(stage)
        output = self.root / f"{stage}.output"
        output.write_bytes(stage.encode())
        emitted = {}
        for label, cache_hit in (("store", False), ("reproducibility", False)):
            path = self.root / f"emitted-{label}.json"
            self.checkpoint.store_checkpoint(
                cache_root=self.cache,
                identity=identity,
                outputs={"artifact": output},
                elapsed_seconds=1.0,
                run_manifest=path,
            )
            emitted[label] = json.loads(path.read_text())
            self.assertIs(emitted[label]["cache_hit"], cache_hit)
        restore_path = self.root / "emitted-restore.json"
        self.checkpoint.restore_checkpoint(
            cache_root=self.cache,
            identity=identity,
            destinations={"artifact": self.root / f"{stage}.restored"},
            run_manifest=restore_path,
        )
        emitted["restore"] = json.loads(restore_path.read_text())
        return emitted

    def test_emitted_run_records_match_the_accepted_closed_shapes(self) -> None:
        # Until 2026-08-30 the planner accepted only the store and
        # reproducibility shapes, so a restore-written record -- which carries
        # cache_hit_timing -- was rejected outright and could never serve as
        # retained identity evidence. The accepted sets now cover exactly the
        # three shapes the library emits, and this proves it against real
        # records rather than hand-built stand-ins.
        self.store_stage("builder-toolchain", b"toolchain")
        stage = "configured-target"
        emitted = self.emit_real_run_records(stage)
        planner = load_module("asahi_checkpoint_plan_emitted_parity", MODULE_PATH)

        self.assertEqual(set(emitted["store"]), planner.RUN_RECORD_KEYS)
        self.assertEqual(
            set(emitted["reproducibility"]), planner.REPRODUCIBILITY_RUN_RECORD_KEYS
        )
        self.assertEqual(set(emitted["restore"]), planner.CACHE_HIT_RUN_RECORD_KEYS)
        self.assertEqual(
            set(emitted["store"]["verification_timing"]),
            planner.VERIFICATION_TIMING_KEYS,
        )
        self.assertEqual(
            set(emitted["restore"]["cache_hit_timing"]), planner.CACHE_HIT_TIMING_KEYS
        )

        # emit_real_run_records never writes a standalone identity file, so the
        # retained run record is the only evidence the planner can read here.
        evidence_path = self.identities / f"{stage}.json"
        for label, record in emitted.items():
            with self.subTest(record=label):
                evidence_path.write_text(json.dumps(record))
                identity, path, kind = planner._load_identity_evidence(
                    self.identities, stage
                )
                self.assertEqual(kind, "retained-run-record")
                self.assertEqual(path, evidence_path)
                self.assertEqual(set(identity), planner.IDENTITY_KEYS)
                self.assertEqual(
                    identity["checkpoint_identity"], record["checkpoint_identity"]
                )

    def test_emitted_restore_record_reaches_the_restore_frontier(self) -> None:
        # The parity above is key sets; this is the consequence that matters.
        # A restore-written record now survives the whole planning path and
        # supplies the stage's identity, which it could not do before.
        self.store_stage("builder-toolchain", b"toolchain")
        stage = "configured-target"
        emitted = self.emit_real_run_records(stage)
        (self.identities / f"{stage}.json").write_text(
            json.dumps(emitted["restore"])
        )
        planner = load_module("asahi_checkpoint_plan_emitted_restore", MODULE_PATH)

        plan = planner.plan_checkpoint_execution(
            repository=self.repository,
            specification=self.specification,
            build_lock=self.build_lock,
            cost_data=self.cost_data,
            changed_paths=["builder/logo.bin"],
            intent="boot-only",
            profile="diagnostic",
            identity_root=self.identities,
            cache_root=self.cache,
        )

        by_stage = {record["stage"]: record for record in plan["stages"]}
        self.assertEqual(
            by_stage[stage]["identity_evidence_kind"], "retained-run-record"
        )
        self.assertTrue(plan["advisory_selection_ready"])
        self.assertFalse(plan["ready_for_execution"])
        self.assertEqual(
            [
                record["stage"]
                for record in plan["execution_selection"]["restore_frontier"]
            ],
            [stage],
        )

    def test_emitted_record_with_a_mutated_shape_is_still_rejected(self) -> None:
        # Growing the accepted sets must not have loosened them into a subset
        # check: a record that drops, gains or corrupts a field is refused.
        self.store_stage("builder-toolchain", b"toolchain")
        stage = "configured-target"
        emitted = self.emit_real_run_records(stage)
        evidence_path = self.identities / f"{stage}.json"
        planner = load_module("asahi_checkpoint_plan_mutated_shape", MODULE_PATH)

        def without(record: dict, key: str) -> dict:
            return {name: value for name, value in record.items() if name != key}

        cases = (
            ("dropped-bytes-written", without(emitted["store"], "bytes_written"),
             "run record fields"),
            ("dropped-verification-timing",
             without(emitted["store"], "verification_timing"),
             "run record fields"),
            ("unknown-field",
             emitted["store"] | {"package_evidence": {"unrelated": True}},
             "run record fields"),
            ("store-shape-with-cache-hit-timing",
             without(emitted["restore"], "bytes_read"),
             "run record fields"),
            ("negative-bytes-read", emitted["store"] | {"bytes_read": -1},
             "transfer accounting"),
            ("non-integer-bytes-written",
             emitted["store"] | {"bytes_written": 12.5},
             "transfer accounting"),
            ("boolean-bytes-read", emitted["store"] | {"bytes_read": True},
             "transfer accounting"),
            ("verification-timing-extra-key",
             emitted["store"]
             | {
                 "verification_timing": emitted["store"]["verification_timing"]
                 | {"unmeasured_seconds": 0.0}
             },
             "timing split"),
            ("verification-timing-negative",
             emitted["store"]
             | {
                 "verification_timing": emitted["store"]["verification_timing"]
                 | {"transfer_seconds": -0.5}
             },
             "timing split"),
            ("cache-hit-timing-missing-key",
             emitted["restore"]
             | {
                 "cache_hit_timing": without(
                     emitted["restore"]["cache_hit_timing"],
                     "materialization_and_readback_seconds",
                 )
             },
             "timing split"),
            ("cache-hit-timing-without-cache-hit",
             emitted["restore"] | {"cache_hit": False},
             "cache hit metadata"),
        )
        for label, record, expected in cases:
            with self.subTest(case=label):
                evidence_path.write_text(json.dumps(record))
                with self.assertRaisesRegex(planner.CheckpointPlanError, expected):
                    planner._load_identity_evidence(self.identities, stage)


if __name__ == "__main__":
    unittest.main()
