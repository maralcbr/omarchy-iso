from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "builder/asahi_stage_inputs.py"
SPEC_PATH = ROOT / "builder/asahi-stage-inputs.json"
LOCK_PATH = ROOT / "builder/asahi-build-lock.json"
PRODUCT_PATH = ROOT / "builder/products/omarchy-mx-mac.json"


def load_module():
    spec = importlib.util.spec_from_file_location("asahi_stage_inputs", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AsahiStageInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()
        cls.specification = cls.module.load_specification(SPEC_PATH)
        cls.lock = json.loads(LOCK_PATH.read_text())

    def fingerprints(
        self,
        *,
        content_overrides: dict[str, bytes] | None = None,
        lock: dict | None = None,
        specification: dict | None = None,
    ) -> dict[str, str]:
        return self.module.declared_stage_fingerprints(
            repository=ROOT,
            specification=specification or self.specification,
            build_lock=lock or self.lock,
            mode="diagnostic",
            content_overrides=content_overrides or {},
        )

    def admission_fingerprints(
        self,
        *,
        content_overrides: dict[str, bytes] | None = None,
        specification: dict | None = None,
    ) -> dict[str, str]:
        return self.module.declared_admission_fingerprints(
            repository=ROOT,
            specification=specification or self.specification,
            mode="diagnostic",
            content_overrides=content_overrides or {},
        )

    def test_qualification_product_uses_the_current_versioned_package_name(self) -> None:
        product = json.loads(PRODUCT_PATH.read_text())
        self.assertEqual(
            product["package_filename"],
            "omarchy-2026.09.02-aarch64-apple-silicon-asahi-os-package.zip",
        )

    def assert_frontier(
        self,
        before: dict[str, str],
        after: dict[str, str],
        *,
        hits: set[str],
        misses: set[str],
    ) -> None:
        self.assertEqual(set(before), set(after))
        self.assertTrue(hits.isdisjoint(misses))
        self.assertEqual(hits | misses, set(before))
        for stage in hits:
            self.assertEqual(before[stage], after[stage], stage)
        for stage in misses:
            self.assertNotEqual(before[stage], after[stage], stage)

    def test_boot_selector_or_logo_change_only_invalidates_boot_and_outputs(self) -> None:
        before = self.fingerprints()
        common_hits = {
            "builder-toolchain",
            "verified-package-cache",
            "offline-repository-database",
            "base-images",
            "configured-target",
        }
        downstream_misses = {
            "finalized-boot",
            "sealed-release-package",
            "installer-metadata",
        }
        for path in (
            "configs/airootfs/usr/share/omarchy-iso/orchestrator/asahi_boot.py",
            "configs/airootfs/usr/share/omarchy-iso/orchestrator/finalized_phases.py",
            "builder/asahi_orchestrator_finalized.py",
            "builder/branding/omarchy-logo.png",
        ):
            original = (ROOT / path).read_bytes()
            after = self.fingerprints(content_overrides={path: original + b"test-change"})
            self.assert_frontier(
                before,
                after,
                hits=common_hits,
                misses=downstream_misses,
            )

    def test_package_lock_change_invalidates_repository_and_downstream(self) -> None:
        before = self.fingerprints()
        changed_lock = copy.deepcopy(self.lock)
        changed_lock["node"]["sha256"] = "f" * 64
        after = self.fingerprints(lock=changed_lock)
        self.assert_frontier(
            before,
            after,
            hits={"builder-toolchain"},
            misses={
                "verified-package-cache",
                "offline-repository-database",
                "base-images",
                "configured-target",
                "finalized-boot",
                "sealed-release-package",
                "installer-metadata",
            },
        )

    def test_boot_stage_declaration_change_only_invalidates_boot_and_outputs(self) -> None:
        before = self.fingerprints()
        changed_specification = copy.deepcopy(self.specification)
        changed_specification["stages"]["finalized-boot"]["source_paths"].append(
            "builder/branding/README.md"
        )
        after = self.fingerprints(specification=changed_specification)
        self.assert_frontier(
            before,
            after,
            hits={
                "builder-toolchain",
                "verified-package-cache",
                "offline-repository-database",
                "base-images",
                "configured-target",
            },
            misses={
                "finalized-boot",
                "sealed-release-package",
                "installer-metadata",
            },
        )

    def test_serialized_finalized_spec_change_preserves_upstream_identities(
        self,
    ) -> None:
        before = self.fingerprints()
        common_hits = {
            "builder-toolchain",
            "verified-package-cache",
            "offline-repository-database",
            "base-images",
            "configured-target",
        }
        downstream_misses = {
            "finalized-boot",
            "sealed-release-package",
            "installer-metadata",
        }
        for field, value in (
            ("source_paths", "builder/branding/README.md"),
            ("runtime_settings", "OMARCHY_ROOT_SELECTOR"),
        ):
            with self.subTest(field=field):
                changed_specification = copy.deepcopy(self.specification)
                changed_specification["stages"]["finalized-boot"][field].append(
                    value
                )
                serialized_specification = (
                    json.dumps(changed_specification, indent=2, sort_keys=True) + "\n"
                ).encode()
                after = self.fingerprints(
                    specification=changed_specification,
                    content_overrides={
                        "builder/asahi-stage-inputs.json": serialized_specification
                    },
                )
                self.assert_frontier(
                    before,
                    after,
                    hits=common_hits,
                    misses=downstream_misses,
                )

    def test_source_date_epoch_is_finalized_only_runtime_identity(self) -> None:
        configured = self.specification["stages"]["configured-target"]
        finalized = self.specification["stages"]["finalized-boot"]
        self.assertNotIn("SOURCE_DATE_EPOCH", configured["runtime_settings"])
        self.assertIn("SOURCE_DATE_EPOCH", finalized["runtime_settings"])

        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            for runtime_input in finalized["runtime_inputs"]:
                if runtime_input["required"]:
                    (runtime / runtime_input["path"]).write_text(
                        f"{runtime_input['path']}\n"
                    )
            settings = {
                name: f"value-for-{name}" for name in finalized["runtime_settings"]
            }
            settings["SOURCE_DATE_EPOCH"] = "1787974800"
            before_runtime = self.module.build_stage_runtime_manifest(
                root=runtime,
                stage="finalized-boot",
                declaration=finalized,
                settings=settings,
            )
            settings["SOURCE_DATE_EPOCH"] = "1787974801"
            after_runtime = self.module.build_stage_runtime_manifest(
                root=runtime,
                stage="finalized-boot",
                declaration=finalized,
                settings=settings,
            )
            self.assertNotEqual(
                before_runtime["input_digest"], after_runtime["input_digest"]
            )

        before = self.fingerprints()
        without_epoch = copy.deepcopy(self.specification)
        without_epoch["stages"]["finalized-boot"]["runtime_settings"].remove(
            "SOURCE_DATE_EPOCH"
        )
        serialized_without_epoch = (
            json.dumps(without_epoch, indent=2, sort_keys=True) + "\n"
        ).encode()
        after = self.fingerprints(
            specification=without_epoch,
            content_overrides={
                "builder/asahi-stage-inputs.json": serialized_without_epoch
            },
        )
        self.assert_frontier(
            before,
            after,
            hits={
                "builder-toolchain",
                "verified-package-cache",
                "offline-repository-database",
                "base-images",
                "configured-target",
            },
            misses={
                "finalized-boot",
                "sealed-release-package",
                "installer-metadata",
            },
        )

    def test_trust_and_checkpoint_verifier_have_intended_fail_closed_scope(self) -> None:
        before = self.fingerprints()
        trust_path = "builder/omarchy-arm-repository.asc"
        trust_after = self.fingerprints(
            content_overrides={trust_path: (ROOT / trust_path).read_bytes() + b"\n"}
        )
        self.assert_frontier(
            before,
            trust_after,
            hits={"builder-toolchain"},
            misses={
                "verified-package-cache",
                "offline-repository-database",
                "base-images",
                "configured-target",
                "finalized-boot",
                "sealed-release-package",
                "installer-metadata",
            },
        )

        verifier_path = "builder/asahi_checkpoint.py"
        verifier_after = self.fingerprints(
            content_overrides={
                verifier_path: (ROOT / verifier_path).read_bytes() + b"\n# verifier change\n"
            }
        )
        self.assertEqual(
            before,
            verifier_after,
            "admission-policy changes must not change producer identities",
        )
        admission_before = self.admission_fingerprints()
        admission_after = self.admission_fingerprints(
            content_overrides={
                verifier_path: (ROOT / verifier_path).read_bytes() + b"\n# verifier change\n"
            }
        )
        self.assertTrue(
            all(
                admission_before[stage] != admission_after[stage]
                for stage in admission_before
            ),
            "checkpoint-verifier changes must rotate every admission policy",
        )

    def test_admission_declaration_change_does_not_change_producer_identity(self) -> None:
        before = self.fingerprints()
        admission_before = self.admission_fingerprints()
        changed = copy.deepcopy(self.specification)
        changed["stages"]["finalized-boot"]["admission_paths"].append(
            "builder/branding/README.md"
        )

        self.assertEqual(before, self.fingerprints(specification=changed))
        admission_after = self.admission_fingerprints(specification=changed)
        self.assert_frontier(
            admission_before,
            admission_after,
            hits={
                "builder-toolchain",
                "verified-package-cache",
                "offline-repository-database",
                "base-images",
                "configured-target",
            },
            misses={
                "finalized-boot",
                "sealed-release-package",
                "installer-metadata",
            },
        )

    def test_undeclared_executed_input_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "builder").mkdir()
            (repository / "builder/entrypoint.sh").write_text(
                "#!/bin/bash\nbash /builder/required-helper.sh\n"
            )
            (repository / "builder/required-helper.sh").write_text("#!/bin/bash\n")
            specification = {
                "schema_version": 1,
                "common_producer_inputs": [],
                "common_admission_inputs": [],
                "stage_order": ["builder-toolchain"],
                "stages": {
                    "builder-toolchain": {
                        "depends_on": [],
                        "entrypoints": ["builder/entrypoint.sh"],
                        "source_paths": ["builder/entrypoint.sh"],
                        "admission_paths": [],
                        "lock_paths": [],
                        "runtime_inputs": [],
                        "runtime_settings": [],
                    }
                },
            }
            with self.assertRaisesRegex(
                self.module.StageInputError,
                "executed input is omitted.*builder/required-helper.sh",
            ):
                self.module.validate_specification(repository, specification)

    def test_declared_paths_reject_traversal_absolute_and_symlinks(self) -> None:
        def specification(path: str) -> dict:
            return {
                "schema_version": 1,
                "common_producer_inputs": [],
                "common_admission_inputs": [],
                "stage_order": ["builder-toolchain"],
                "stages": {
                    "builder-toolchain": {
                        "depends_on": [],
                        "entrypoints": [path],
                        "source_paths": [path],
                        "admission_paths": [],
                        "lock_paths": [],
                        "runtime_inputs": [],
                        "runtime_settings": [],
                    }
                },
            }

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "builder").mkdir()
            (repository / "builder/input.sh").write_text("#!/bin/bash\n")
            with self.assertRaisesRegex(self.module.StageInputError, "unsafe"):
                self.module.validate_specification(
                    repository,
                    specification("builder/../builder/input.sh"),
                )
            with self.assertRaisesRegex(self.module.StageInputError, "unsafe"):
                self.module.validate_specification(
                    repository,
                    specification(str(repository / "builder/input.sh")),
                )
            for alias in ("builder/./input.sh", "builder//input.sh"):
                with self.subTest(alias=alias), self.assertRaisesRegex(
                    self.module.StageInputError,
                    "unsafe",
                ):
                    self.module.validate_specification(
                        repository,
                        specification(alias),
                    )

            (repository / "builder/final-link.sh").symlink_to("input.sh")
            with self.assertRaisesRegex(self.module.StageInputError, "symlink is forbidden"):
                self.module.validate_specification(
                    repository,
                    specification("builder/final-link.sh"),
                )

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            real_builder = repository / "real-builder"
            real_builder.mkdir()
            (real_builder / "input.sh").write_text("#!/bin/bash\n")
            (repository / "builder").symlink_to(real_builder, target_is_directory=True)
            with self.assertRaisesRegex(
                self.module.StageInputError,
                "symlinked ancestor is forbidden",
            ):
                self.module.validate_specification(
                    repository,
                    specification("builder/input.sh"),
                )

    def test_transitive_relative_shell_and_python_input_omission_is_detected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "builder").mkdir()
            (repository / "builder/entrypoint.sh").write_text(
                "#!/bin/bash\nsource ./helper.sh\n"
            )
            (repository / "builder/helper.sh").write_text(
                "#!/bin/bash\npython3 ./transitive.py\n"
            )
            (repository / "builder/transitive.py").write_text(
                "import local_dependency\n"
            )
            (repository / "builder/local_dependency.py").write_text("VALUE = 1\n")
            specification = {
                "schema_version": 1,
                "common_producer_inputs": [],
                "common_admission_inputs": [],
                "stage_order": ["builder-toolchain"],
                "stages": {
                    "builder-toolchain": {
                        "depends_on": [],
                        "entrypoints": ["builder/entrypoint.sh"],
                        "source_paths": [
                            "builder/entrypoint.sh",
                            "builder/helper.sh",
                            "builder/transitive.py",
                        ],
                        "admission_paths": [],
                        "lock_paths": [],
                        "runtime_inputs": [],
                        "runtime_settings": [],
                    }
                },
            }
            with self.assertRaisesRegex(
                self.module.StageInputError,
                "executed input is omitted.*builder/local_dependency.py",
            ):
                self.module.validate_specification(repository, specification)

    def test_ordinary_relative_execution_is_discovered(self) -> None:
        for command, helper in (
            ("./helper.sh", "helper.sh"),
            ("bash ./helper.sh", "helper.sh"),
            ("bash -e ./helper.sh", "helper.sh"),
            ("python3 ./helper.py", "helper.py"),
            ("python3 -u ./helper.py", "helper.py"),
            ("env python3 ./helper.py", "helper.py"),
            ("if ./helper.sh; then :; fi", "helper.sh"),
        ):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary)
                (repository / "builder").mkdir()
                (repository / "builder/entrypoint.sh").write_text(
                    f"#!/bin/bash\n{command}\n"
                )
                (repository / "builder" / helper).write_text("#!/bin/bash\n")
                specification = {
                    "schema_version": 1,
                    "common_producer_inputs": [],
                    "common_admission_inputs": [],
                    "stage_order": ["builder-toolchain"],
                    "stages": {
                        "builder-toolchain": {
                            "depends_on": [],
                            "entrypoints": ["builder/entrypoint.sh"],
                            "source_paths": ["builder/entrypoint.sh"],
                            "admission_paths": [],
                            "lock_paths": [],
                            "runtime_inputs": [],
                            "runtime_settings": [],
                        }
                    },
                }
                with self.assertRaisesRegex(
                    self.module.StageInputError,
                    f"executed input is omitted.*builder/{helper}",
                ):
                    self.module.validate_specification(repository, specification)

    def test_missing_executed_input_is_rejected_in_every_repository_root(
        self,
    ) -> None:
        for root_name in ("archiso", "bin", "builder", "configs"):
            with self.subTest(root=root_name), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary)
                for local_root in ("archiso", "bin", "builder", "configs"):
                    (repository / local_root).mkdir()
                (repository / "builder/entrypoint.sh").write_text(
                    f"#!/bin/bash\nbash /{root_name}/required-helper.sh\n"
                )
                (repository / root_name / "required-helper.sh").write_text(
                    "#!/bin/bash\n"
                )
                specification = {
                    "schema_version": 1,
                    "common_producer_inputs": [],
                    "common_admission_inputs": [],
                    "stage_order": ["builder-toolchain"],
                    "stages": {
                        "builder-toolchain": {
                            "depends_on": [],
                            "entrypoints": ["builder/entrypoint.sh"],
                            "source_paths": ["builder/entrypoint.sh"],
                            "admission_paths": [],
                            "lock_paths": [],
                            "runtime_inputs": [],
                            "runtime_settings": [],
                        }
                    },
                }
                with self.assertRaisesRegex(
                    self.module.StageInputError,
                    f"executed input is omitted.*{root_name}/required-helper.sh",
                ):
                    self.module.validate_specification(repository, specification)

    def test_shell_comment_does_not_create_an_executed_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "builder").mkdir()
            (repository / "builder/entrypoint.sh").write_text(
                "#!/bin/bash\n# /builder/not-executed.sh is documentation only\n"
            )
            (repository / "builder/not-executed.sh").write_text("#!/bin/bash\n")
            specification = {
                "schema_version": 1,
                "common_producer_inputs": [],
                "common_admission_inputs": [],
                "stage_order": ["builder-toolchain"],
                "stages": {
                    "builder-toolchain": {
                        "depends_on": [],
                        "entrypoints": ["builder/entrypoint.sh"],
                        "source_paths": ["builder/entrypoint.sh"],
                        "admission_paths": [],
                        "lock_paths": [],
                        "runtime_inputs": [],
                        "runtime_settings": [],
                    }
                },
            }

            self.module.validate_specification(repository, specification)

    def test_variable_root_execution_and_transitive_source_are_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            for local_root in ("archiso", "bin", "builder", "configs"):
                (repository / local_root).mkdir()
            (repository / "builder/entrypoint.sh").write_text(
                "#!/bin/bash\n"
                "source \"$PROJECT_ROOT/configs/helper.sh\"\n"
                "\"$PROJECT_ROOT/bin/direct-helper\"\n"
            )
            (repository / "configs/helper.sh").write_text(
                "#!/bin/bash\nbash /archiso/transitive-helper.sh\n"
            )
            (repository / "bin/direct-helper").write_text("#!/bin/bash\n")
            (repository / "archiso/transitive-helper.sh").write_text("#!/bin/bash\n")
            specification = {
                "schema_version": 1,
                "common_producer_inputs": [],
                "common_admission_inputs": [],
                "stage_order": ["builder-toolchain"],
                "stages": {
                    "builder-toolchain": {
                        "depends_on": [],
                        "entrypoints": ["builder/entrypoint.sh"],
                        "source_paths": [
                            "builder/entrypoint.sh",
                            "configs/helper.sh",
                        ],
                        "admission_paths": [],
                        "lock_paths": [],
                        "runtime_inputs": [],
                        "runtime_settings": [],
                    }
                },
            }

            with self.assertRaisesRegex(
                self.module.StageInputError,
                "executed input is omitted.*archiso/transitive-helper.sh",
            ):
                self.module.validate_specification(repository, specification)

            specification["stages"]["builder-toolchain"]["source_paths"].append(
                "archiso/transitive-helper.sh"
            )
            with self.assertRaisesRegex(
                self.module.StageInputError,
                "executed input is omitted.*bin/direct-helper",
            ):
                self.module.validate_specification(repository, specification)

    def test_control_entrypoint_closure_is_fail_closed_and_admission_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "bin").mkdir()
            (repository / "builder").mkdir()
            (repository / "configs").mkdir()
            controller = repository / "bin/controller"
            controller.write_text(
                "#!/bin/bash\nsource /configs/required-policy.sh\n"
            )
            policy = repository / "configs/required-policy.sh"
            policy.write_text("#!/bin/bash\n")
            specification = {
                "schema_version": 1,
                "common_producer_inputs": [],
                "common_admission_inputs": [],
                "stage_order": ["builder-toolchain"],
                "stages": {
                    "builder-toolchain": {
                        "depends_on": [],
                        "entrypoints": ["builder/producer.sh"],
                        "control_entrypoints": ["bin/controller"],
                        "source_paths": ["builder/producer.sh"],
                        "admission_paths": ["bin/controller"],
                        "lock_paths": [],
                        "runtime_inputs": [],
                        "runtime_settings": [],
                    }
                },
            }
            (repository / "builder/producer.sh").write_text("#!/bin/bash\n")
            with self.assertRaisesRegex(
                self.module.StageInputError,
                "executed input is omitted.*configs/required-policy.sh",
            ):
                self.module.validate_specification(repository, specification)

            specification["stages"]["builder-toolchain"][
                "admission_paths"
            ].append("configs/required-policy.sh")
            self.module.validate_specification(repository, specification)
            before_producer = self.module.declared_stage_fingerprints(
                repository=repository,
                specification=specification,
                build_lock={},
                mode="diagnostic",
            )
            before_admission = self.module.declared_admission_fingerprints(
                repository=repository,
                specification=specification,
                mode="diagnostic",
            )
            override = {
                "bin/controller": controller.read_bytes() + b"# control change\n"
            }
            self.assertEqual(
                before_producer,
                self.module.declared_stage_fingerprints(
                    repository=repository,
                    specification=specification,
                    build_lock={},
                    mode="diagnostic",
                    content_overrides=override,
                ),
            )
            self.assertNotEqual(
                before_admission,
                self.module.declared_admission_fingerprints(
                    repository=repository,
                    specification=specification,
                    mode="diagnostic",
                    content_overrides=override,
                ),
            )

    def test_cross_stage_boundaries_require_graph_scoped_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "builder").mkdir()
            (repository / "builder/root.sh").write_text(
                "#!/bin/bash\nbash /builder/child.sh\n"
            )
            (repository / "builder/child.sh").write_text("#!/bin/bash\n")
            (repository / "builder/unrelated.sh").write_text("#!/bin/bash\n")

            def stage(
                entrypoint: str,
                *,
                depends_on: list[str],
                dispatches: list[str] | None = None,
            ) -> dict[str, object]:
                return {
                    "depends_on": depends_on,
                    "dispatches": dispatches or [],
                    "entrypoints": [entrypoint],
                    "source_paths": [entrypoint],
                    "admission_paths": [],
                    "lock_paths": [],
                    "runtime_inputs": [],
                    "runtime_settings": [],
                }

            specification = {
                "schema_version": 1,
                "common_producer_inputs": [],
                "common_admission_inputs": [],
                "stage_order": ["root", "child", "unrelated"],
                "stages": {
                    "root": stage("builder/root.sh", depends_on=[]),
                    "child": stage(
                        "builder/child.sh",
                        depends_on=["root"],
                    ),
                    "unrelated": stage(
                        "builder/unrelated.sh",
                        depends_on=[],
                    ),
                },
            }
            with self.assertRaisesRegex(
                self.module.StageInputError,
                "executed input is omitted from root: builder/child.sh",
            ):
                self.module.validate_specification(repository, specification)

            specification["stages"]["root"]["dispatches"] = [
                "builder/child.sh"
            ]
            self.module.validate_specification(repository, specification)

            (repository / "builder/root.sh").write_text(
                "#!/bin/bash\nbash /builder/unrelated.sh\n"
            )
            specification["stages"]["root"]["dispatches"] = [
                "builder/unrelated.sh"
            ]
            with self.assertRaisesRegex(
                self.module.StageInputError,
                "root dispatch target is outside its dependency graph: "
                "builder/unrelated.sh",
            ):
                self.module.validate_specification(repository, specification)

    def test_data_manifest_paths_are_not_traversed_as_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "builder").mkdir()
            (repository / "builder/entrypoint.sh").write_text(
                "#!/bin/bash\npython3 /builder/tool.py /builder/inputs.json\n"
            )
            (repository / "builder/tool.py").write_text("VALUE = 1\n")
            (repository / "builder/inputs.json").write_text(
                '{"documented": "builder/not-executed.sh"}\n'
            )
            (repository / "builder/not-executed.sh").write_text("#!/bin/bash\n")
            specification = {
                "schema_version": 1,
                "common_producer_inputs": [],
                "common_admission_inputs": [],
                "stage_order": ["builder-toolchain"],
                "stages": {
                    "builder-toolchain": {
                        "depends_on": [],
                        "entrypoints": ["builder/entrypoint.sh"],
                        "source_paths": [
                            "builder/entrypoint.sh",
                            "builder/tool.py",
                            "builder/inputs.json",
                        ],
                        "admission_paths": [],
                        "lock_paths": [],
                        "runtime_inputs": [],
                        "runtime_settings": [],
                    }
                },
            }
            self.module.validate_specification(repository, specification)

    def test_package_controller_is_producer_bound_with_stage_byte_boundaries(
        self,
    ) -> None:
        # Renamed 2026-08-30 from
        # test_package_controller_is_control_only_with_stage_byte_boundaries.
        # It used to assert the controller was control-only: in
        # base-images.control_entrypoints and base-images.admission_paths, and
        # in no stage's entrypoints or source_paths. Owner decision 8(a)
        # resolved as option A -- the controller sets the work layout,
        # sequences every stage and writes the manifests they consume, so
        # editing it can change produced bytes and it belongs to producer
        # identity. It is now a common producer input and a base-images
        # entrypoint, and per-stage disjointness means it may appear in no
        # stage's admission_paths. The stage byte-boundary assertions below are
        # unchanged: the controller still owns no byte-moving function itself.
        root = "builder/build-asahi-os-package.sh"
        dispatch = "builder/asahi-package-dispatch.sh"
        base = self.specification["stages"]["base-images"]
        self.assertIn(root, self.specification["common_producer_inputs"])
        self.assertIn(dispatch, self.specification["common_producer_inputs"])
        self.assertIn(root, base["entrypoints"])
        self.assertIn(dispatch, base["entrypoints"])
        for stage in self.specification["stage_order"]:
            with self.subTest(stage=stage):
                declaration = self.specification["stages"][stage]
                # Declared once, in the common producer list -- never sprinkled
                # into a stage's own source_paths, and never admission-side.
                self.assertNotIn(root, declaration["source_paths"])
                self.assertNotIn(dispatch, declaration["source_paths"])
                self.assertNotIn(root, declaration["admission_paths"])
                self.assertNotIn(dispatch, declaration["admission_paths"])
                self.assertNotIn(root, declaration.get("control_entrypoints", []))
                self.assertNotIn(dispatch, declaration.get("control_entrypoints", []))

        controller_text = (ROOT / root).read_text()
        image_runtime_text = (
            ROOT / "builder/asahi-stages/image-runtime.sh"
        ).read_text()
        for function in (
            "unmount_target_tree",
            "attach_images",
            "detach_images",
            "write_install_config",
            "run_orchestrator_stage",
        ):
            self.assertNotIn(f"{function}() {{", controller_text)
            self.assertIn(f"{function}() {{", image_runtime_text)

        verified = self.specification["stages"]["verified-package-cache"]
        package_module = "builder/asahi-stages/verified-package-cache.sh"
        build_controller = "builder/build-iso.sh"
        self.assertIn(package_module, verified["entrypoints"])
        self.assertIn(package_module, verified["source_paths"])
        self.assertIn(build_controller, verified["control_entrypoints"])
        self.assertIn(build_controller, verified["admission_paths"])
        self.assertNotIn(build_controller, verified["entrypoints"])
        self.assertNotIn(build_controller, verified["source_paths"])
        projector = "builder/asahi_runtime_projection.py"
        configured = self.specification["stages"]["configured-target"]
        self.assertIn(projector, configured["entrypoints"])
        self.assertIn(projector, configured["source_paths"])
        asahi_dispatch = ROOT / "builder/asahi-package-dispatch.sh"
        self.assertIn("--stage configured-target", asahi_dispatch.read_text())
        self.assertIn("--stage finalized-boot", asahi_dispatch.read_text())
        self.assertNotIn("--stage configured-target", (ROOT / build_controller).read_text())
        self.assertNotIn(
            'cp -a "$build_cache_dir/airootfs/usr/share/omarchy-iso/."',
            (ROOT / build_controller).read_text(),
        )
        self.assertNotIn(
            "prepare_verified_package_cache() {",
            (ROOT / build_controller).read_text(),
        )
        self.assertIn(
            "prepare_verified_package_cache() {",
            (ROOT / package_module).read_text(),
        )
        for stage in ("builder-toolchain", "verified-package-cache"):
            declaration = self.specification["stages"][stage]
            self.assertIn("bin/omarchy-iso-make", declaration["control_entrypoints"])
            self.assertNotIn("bin/omarchy-iso-make", declaration["source_paths"])

    def test_controller_and_installer_changes_have_exact_producer_frontiers(
        self,
    ) -> None:
        before = self.fingerprints()
        root = "builder/build-asahi-os-package.sh"
        root_override = {
            root: (ROOT / root).read_bytes() + b"\n# root change\n"
        }
        root_after = self.fingerprints(content_overrides=root_override)
        # Inverted 2026-08-30 by owner decision 8(a) option A. This used to
        # assert the exact opposite -- that a controller edit left every
        # producer fingerprint untouched and moved only the admission
        # fingerprints. The controller is now a common producer input, so its
        # edit invalidates every stage and touches no admission fingerprint at
        # all, because it is no longer an admission input anywhere.
        self.assert_frontier(
            before,
            root_after,
            hits=set(),
            misses=set(before),
        )
        self.assertEqual(
            self.admission_fingerprints(),
            self.admission_fingerprints(content_overrides=root_override),
        )

        build_controller = "builder/build-iso.sh"
        build_controller_override = {
            build_controller: (ROOT / build_controller).read_bytes()
            + b"\n# controller-only change\n"
        }
        self.assertEqual(
            before,
            self.fingerprints(content_overrides=build_controller_override),
        )
        self.assertNotEqual(
            self.admission_fingerprints(),
            self.admission_fingerprints(
                content_overrides=build_controller_override
            ),
        )

        package_module = "builder/asahi-stages/verified-package-cache.sh"
        package_after = self.fingerprints(
            content_overrides={
                package_module: (ROOT / package_module).read_bytes()
                + b"\n# package producer change\n"
            }
        )
        self.assert_frontier(
            before,
            package_after,
            hits={"builder-toolchain"},
            misses=set(before) - {"builder-toolchain"},
        )

        package_roles = "builder/package-architecture.sh"
        package_roles_after = self.fingerprints(
            content_overrides={
                package_roles: (ROOT / package_roles).read_bytes()
                + b"\n# package-role change\n"
            }
        )
        self.assert_frontier(
            before,
            package_roles_after,
            hits={"builder-toolchain"},
            misses=set(before) - {"builder-toolchain"},
        )

        image_runtime = "builder/asahi-stages/image-runtime.sh"
        runtime_after = self.fingerprints(
            content_overrides={
                image_runtime: (ROOT / image_runtime).read_bytes()
                + b"\n# shared image runtime change\n"
            }
        )
        self.assert_frontier(
            before,
            runtime_after,
            hits={
                "builder-toolchain",
                "verified-package-cache",
                "offline-repository-database",
                "base-images",
            },
            misses={
                "configured-target",
                "finalized-boot",
                "sealed-release-package",
                "installer-metadata",
            },
        )

        projector = "builder/asahi_runtime_projection.py"
        projector_after = self.fingerprints(
            content_overrides={
                projector: (ROOT / projector).read_bytes()
                + b"\n# projection implementation change\n"
            }
        )
        self.assert_frontier(
            before,
            projector_after,
            hits={
                "builder-toolchain",
                "verified-package-cache",
                "offline-repository-database",
                "base-images",
            },
            misses={
                "configured-target",
                "finalized-boot",
                "sealed-release-package",
                "installer-metadata",
            },
        )

        configured_runtime = "builder/asahi-stages/configured-runtime-inputs.sh"
        configured_runtime_after = self.fingerprints(
            content_overrides={
                configured_runtime: (ROOT / configured_runtime).read_bytes()
                + b"\n# configured runtime change\n"
            }
        )
        self.assert_frontier(
            before,
            configured_runtime_after,
            hits={
                "builder-toolchain",
                "verified-package-cache",
                "offline-repository-database",
                "base-images",
            },
            misses={
                "configured-target",
                "finalized-boot",
                "sealed-release-package",
                "installer-metadata",
            },
        )

        finalized_runtime = "builder/asahi-stages/finalized-runtime-inputs.sh"
        finalized_runtime_after = self.fingerprints(
            content_overrides={
                finalized_runtime: (ROOT / finalized_runtime).read_bytes()
                + b"\n# finalized runtime change\n"
            }
        )
        self.assert_frontier(
            before,
            finalized_runtime_after,
            hits={
                "builder-toolchain",
                "verified-package-cache",
                "offline-repository-database",
                "base-images",
                "configured-target",
            },
            misses={
                "finalized-boot",
                "sealed-release-package",
                "installer-metadata",
            },
        )

        media_only = "builder/archiso-media-output.sh"
        media_override = {
            media_only: (ROOT / media_only).read_bytes()
            + b"\n# validation ISO media-only change\n"
        }
        self.assertEqual(
            before,
            self.fingerprints(content_overrides=media_override),
        )
        self.assertNotEqual(
            self.admission_fingerprints(),
            self.admission_fingerprints(content_overrides=media_override),
        )

        installer = "builder/asahi-stages/installer-metadata.sh"
        installer_after = self.fingerprints(
            content_overrides={
                installer: (ROOT / installer).read_bytes()
                + b"\n# installer-only change\n"
            }
        )
        self.assert_frontier(
            before,
            installer_after,
            hits=set(before) - {"installer-metadata"},
            misses={"installer-metadata"},
        )

        host_controller = "bin/omarchy-iso-make"
        host_override = {
            host_controller: (ROOT / host_controller).read_bytes()
            + b"\n# control-only change\n"
        }
        self.assertEqual(
            before,
            self.fingerprints(content_overrides=host_override),
        )
        self.assertNotEqual(
            self.admission_fingerprints(),
            self.admission_fingerprints(content_overrides=host_override),
        )

    def test_executed_admission_helper_has_only_admission_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "builder").mkdir()
            (repository / "builder/entrypoint.sh").write_text(
                "#!/bin/bash\nsource ./policy.sh\n"
            )
            policy = repository / "builder/policy.sh"
            policy.write_text("#!/bin/bash\n")
            specification = {
                "schema_version": 1,
                "common_producer_inputs": [],
                "common_admission_inputs": ["builder/policy.sh"],
                "stage_order": ["builder-toolchain"],
                "stages": {
                    "builder-toolchain": {
                        "depends_on": [],
                        "entrypoints": ["builder/entrypoint.sh"],
                        "source_paths": ["builder/entrypoint.sh"],
                        "admission_paths": [],
                        "lock_paths": [],
                        "runtime_inputs": [],
                        "runtime_settings": [],
                    }
                },
            }
            self.module.validate_specification(repository, specification)
            before_producer = self.module.declared_stage_fingerprints(
                repository=repository,
                specification=specification,
                build_lock={},
                mode="diagnostic",
            )
            before_admission = self.module.declared_admission_fingerprints(
                repository=repository,
                specification=specification,
                mode="diagnostic",
            )
            override = {"builder/policy.sh": policy.read_bytes() + b"# changed\n"}
            after_producer = self.module.declared_stage_fingerprints(
                repository=repository,
                specification=specification,
                build_lock={},
                mode="diagnostic",
                content_overrides=override,
            )
            after_admission = self.module.declared_admission_fingerprints(
                repository=repository,
                specification=specification,
                mode="diagnostic",
                content_overrides=override,
            )
            self.assertEqual(before_producer, after_producer)
            self.assertNotEqual(before_admission, after_admission)

    def test_repository_spec_is_complete_and_uses_whole_files(self) -> None:
        self.module.validate_specification(ROOT, self.specification)
        self.assertNotIn("common_checkpoint_inputs", self.specification)
        self.assertIn(
            "builder/asahi_stage_inputs.py",
            self.specification["common_producer_inputs"],
        )
        self.assertNotIn(
            "builder/asahi_stage_inputs.py",
            self.specification["common_admission_inputs"],
        )
        self.assertIn(
            "builder/asahi_checkpoint.py",
            self.specification["common_admission_inputs"],
        )
        for stage in self.specification["stage_order"]:
            for path in (
                *self.specification["stages"][stage]["source_paths"],
                *self.specification["stages"][stage]["admission_paths"],
            ):
                self.assertNotIn(":", path, "line-range identities are forbidden")
                self.assertNotIn("__pycache__", path)
                self.assertTrue((ROOT / path).is_file(), path)

        verified_cache_sources = self.specification["stages"][
            "verified-package-cache"
        ]["source_paths"]
        self.assertNotIn("builder/build-iso.sh", verified_cache_sources)
        self.assertIn(
            "builder/build-iso.sh",
            self.specification["stages"]["verified-package-cache"][
                "admission_paths"
            ],
        )

        finalized_sources = self.specification["stages"]["finalized-boot"][
            "source_paths"
        ]
        configured_sources = self.specification["stages"]["configured-target"][
            "source_paths"
        ]
        self.assertIn(
            "configs/airootfs/usr/share/omarchy-iso/orchestrator/configured_phases.py",
            configured_sources,
        )
        self.assertNotIn(
            "configs/airootfs/usr/share/omarchy-iso/orchestrator/finalized_phases.py",
            configured_sources,
        )
        self.assertNotIn(
            "configs/airootfs/usr/share/omarchy-iso/orchestrator/phases_impl.py",
            configured_sources,
        )
        self.assertIn(
            "configs/airootfs/usr/share/omarchy-iso/orchestrator/finalized_phases.py",
            finalized_sources,
        )
        self.assertNotIn(
            "configs/airootfs/usr/share/omarchy-iso/orchestrator/phases_impl.py",
            finalized_sources,
        )
        self.assertIn("builder/branding/omarchy-logo.png", finalized_sources)
        self.assertNotIn(
            "configs/airootfs/usr/share/omarchy-iso/branding/omarchy-logo.svg",
            finalized_sources,
        )

    def test_dynamic_orchestrator_inputs_cannot_be_omitted_from_their_stage(
        self,
    ) -> None:
        cases = (
            ("configured-target", "builder/run-asahi-configured-stage.py"),
            ("finalized-boot", "builder/run-asahi-finalized-stage.py"),
            ("configured-target", "builder/asahi_orchestrator_configured.py"),
            ("finalized-boot", "builder/asahi_orchestrator_finalized.py"),
            (
                "configured-target",
                "configs/airootfs/usr/share/omarchy-iso/orchestrator/configured_phases.py",
            ),
            (
                "finalized-boot",
                "configs/airootfs/usr/share/omarchy-iso/orchestrator/finalized_phases.py",
            ),
        )
        for stage, path in cases:
            with self.subTest(stage=stage, path=path):
                changed = copy.deepcopy(self.specification)
                declaration = changed["stages"][stage]
                declaration["source_paths"].remove(path)
                if path in declaration["entrypoints"]:
                    declaration["entrypoints"].remove(path)
                with self.assertRaisesRegex(
                    self.module.StageInputError,
                    f"executed input is omitted from {stage}: {path}",
                ):
                    self.module.validate_specification(ROOT, changed)

    def test_package_role_helper_cannot_be_omitted_from_verified_cache(self) -> None:
        changed = copy.deepcopy(self.specification)
        changed["stages"]["verified-package-cache"]["source_paths"].remove(
            "builder/package-architecture.sh"
        )
        with self.assertRaisesRegex(
            self.module.StageInputError,
            "executed input is omitted from verified-package-cache: "
            "builder/package-architecture.sh",
        ):
            self.module.validate_specification(ROOT, changed)

    def test_runtime_projector_cannot_be_omitted_from_configured_stage(self) -> None:
        changed = copy.deepcopy(self.specification)
        changed["stages"]["configured-target"]["source_paths"].remove(
            "builder/asahi_runtime_projection.py"
        )
        with self.assertRaisesRegex(
            self.module.StageInputError,
            "builder/asahi_runtime_projection.py",
        ):
            self.module.validate_specification(ROOT, changed)

    def test_installer_media_verifier_helpers_cannot_be_omitted(self) -> None:
        for path in (
            "builder/verify-apple-media-layout.sh",
            "builder/validate-apple-platform-snapshot.sh",
        ):
            with self.subTest(path=path):
                changed = copy.deepcopy(self.specification)
                changed["stages"]["installer-metadata"]["admission_paths"].remove(
                    path
                )
                with self.assertRaisesRegex(
                    self.module.StageInputError,
                    f"executed input is omitted from installer-metadata: {path}",
                ):
                    self.module.validate_specification(ROOT, changed)

    def test_generation_emits_separate_read_only_producer_and_admission_manifests(
        self,
    ) -> None:
        expected_admission = self.admission_fingerprints()
        expected_producer = self.fingerprints()
        expected_bindings = self.module.declared_stage_identity_bindings(
            repository=ROOT,
            specification=self.specification,
            build_lock=self.lock,
            mode="diagnostic",
        )
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "stage-inputs"
            index = self.module.generate_stage_inputs(
                repository=ROOT,
                specification=self.specification,
                build_lock=self.lock,
                mode="diagnostic",
                output_root=output_root,
            )
            admission_index = json.loads(
                (output_root / "admission-index.json").read_text()
            )

            for stage in self.specification["stage_order"]:
                source_path = output_root / stage / "source-manifest.json"
                policy_path = output_root / stage / "admission-policy.json"
                source = json.loads(source_path.read_text())
                policy = json.loads(policy_path.read_text())
                self.assertNotIn(
                    "builder/asahi_checkpoint.py",
                    source["paths"],
                )
                self.assertIn(
                    "builder/asahi_checkpoint.py",
                    policy["paths"],
                )
                self.assertEqual(
                    policy["admission_policy_identity"],
                    expected_admission[stage],
                )
                self.assertEqual(
                    admission_index["stages"][stage]["admission_policy_identity"],
                    expected_admission[stage],
                )
                self.assertEqual(
                    source["producer_binding_identity"],
                    expected_producer[stage],
                )
                self.assertEqual(
                    index["stages"][stage]["producer_binding_identity"],
                    expected_producer[stage],
                )
                self.assertNotIn("admission_policy_identity", index["stages"][stage])
                self.assertEqual(source_path.stat().st_mode & 0o222, 0)
                self.assertEqual(policy_path.stat().st_mode & 0o222, 0)
                for record_name, filename in (
                    ("source_manifest", "source-manifest.json"),
                    ("source_lock", "source-lock.json"),
                ):
                    generated_path = output_root / stage / filename
                    content = generated_path.read_bytes()
                    actual = {
                        "filename": filename,
                        "size_bytes": len(content),
                        "sha256": self.module._file_digest(content),
                    }
                    if record_name == "source_manifest":
                        actual["executable_mode"] = (
                            generated_path.stat().st_mode & 0o111
                        )
                    self.assertEqual(
                        actual,
                        expected_bindings[stage][record_name],
                    )

    def test_builder_identity_is_shared_while_downstream_profiles_are_separate(
        self,
    ) -> None:
        diagnostic = self.fingerprints()
        qualification = self.module.declared_stage_fingerprints(
            repository=ROOT,
            specification=self.specification,
            build_lock=self.lock,
            mode="qualification",
        )
        admission_diagnostic = self.admission_fingerprints()
        admission_qualification = self.module.declared_admission_fingerprints(
            repository=ROOT,
            specification=self.specification,
            mode="qualification",
        )
        self.assertEqual(
            diagnostic["builder-toolchain"],
            qualification["builder-toolchain"],
        )
        self.assertEqual(
            admission_diagnostic["builder-toolchain"],
            admission_qualification["builder-toolchain"],
        )
        for stage in self.specification["stage_order"][1:]:
            self.assertNotEqual(diagnostic[stage], qualification[stage], stage)
            self.assertNotEqual(
                admission_diagnostic[stage],
                admission_qualification[stage],
                stage,
            )

        with tempfile.TemporaryDirectory() as temporary:
            output_roots = {}
            for mode in ("diagnostic", "qualification"):
                output_root = Path(temporary) / mode
                self.module.generate_stage_inputs(
                    repository=ROOT,
                    specification=self.specification,
                    build_lock=self.lock,
                    mode=mode,
                    output_root=output_root,
                )
                output_roots[mode] = output_root

            for mode, output_root in output_roots.items():
                source = json.loads(
                    (output_root / "builder-toolchain/source-manifest.json").read_text()
                )
                source_lock = json.loads(
                    (output_root / "builder-toolchain/source-lock.json").read_text()
                )
                policy = json.loads(
                    (output_root / "builder-toolchain/admission-policy.json").read_text()
                )
                index = json.loads((output_root / "index.json").read_text())
                admission_index = json.loads(
                    (output_root / "admission-index.json").read_text()
                )
                self.assertEqual(source["producer_binding_mode"], "shared", mode)
                self.assertEqual(source_lock["mode"], "shared", mode)
                self.assertEqual(policy["mode"], "shared", mode)
                self.assertEqual(
                    index["stages"]["builder-toolchain"]["producer_binding_mode"],
                    "shared",
                    mode,
                )
                self.assertEqual(
                    admission_index["stages"]["builder-toolchain"][
                        "admission_policy_mode"
                    ],
                    "shared",
                    mode,
                )
                configured_lock = json.loads(
                    (output_root / "configured-target/source-lock.json").read_text()
                )
                self.assertEqual(configured_lock["mode"], mode)

    def test_generated_producer_binding_rotates_downstream_with_package_lock(
        self,
    ) -> None:
        changed_lock = copy.deepcopy(self.lock)
        changed_lock["node"]["sha256"] = "e" * 64
        expected_before = self.fingerprints()
        expected_after = self.fingerprints(lock=changed_lock)
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            before = self.module.generate_stage_inputs(
                repository=ROOT,
                specification=self.specification,
                build_lock=self.lock,
                mode="diagnostic",
                output_root=temporary_root / "before",
            )
            after = self.module.generate_stage_inputs(
                repository=ROOT,
                specification=self.specification,
                build_lock=changed_lock,
                mode="diagnostic",
                output_root=temporary_root / "after",
            )

            for stage in self.specification["stage_order"]:
                self.assertEqual(
                    before["stages"][stage]["producer_binding_identity"],
                    expected_before[stage],
                )
                self.assertEqual(
                    after["stages"][stage]["producer_binding_identity"],
                    expected_after[stage],
                )
            self.assertEqual(
                before["stages"]["builder-toolchain"]["producer_binding_identity"],
                after["stages"]["builder-toolchain"]["producer_binding_identity"],
            )
            for stage in self.specification["stage_order"][1:]:
                self.assertNotEqual(
                    before["stages"][stage]["producer_binding_identity"],
                    after["stages"][stage]["producer_binding_identity"],
                )

    def test_generation_rejects_symlinked_output_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            output_root = temporary_root / "stage-inputs"
            outside = temporary_root / "outside"
            output_root.mkdir()
            outside.mkdir()
            (output_root / "builder-toolchain").symlink_to(
                outside,
                target_is_directory=True,
            )
            with self.assertRaisesRegex(
                self.module.StageInputError,
                "unsafe generated stage-input directory",
            ):
                self.module.generate_stage_inputs(
                    repository=ROOT,
                    specification=self.specification,
                    build_lock=self.lock,
                    mode="diagnostic",
                    output_root=output_root,
                )
            self.assertFalse((outside / "source-manifest.json").exists())

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            output_root = temporary_root / "stage-inputs"
            stage_root = output_root / "builder-toolchain"
            outside = temporary_root / "outside.json"
            stage_root.mkdir(parents=True)
            outside.write_text("sentinel\n")
            (stage_root / "source-manifest.json").symlink_to(outside)
            with self.assertRaisesRegex(
                self.module.StageInputError,
                "unsafe generated stage-input file",
            ):
                self.module._write_json_beneath(
                    output_root,
                    Path("builder-toolchain/source-manifest.json"),
                    {"unsafe": False},
                )
            self.assertEqual(outside.read_text(), "sentinel\n")

    def test_git_status_presentation_is_evidence_not_source_identity(self) -> None:
        declaration = {
            "depends_on": [],
            "entrypoints": ["builder/input.sh"],
            "source_paths": ["builder/input.sh"],
            "admission_paths": [],
            "lock_paths": [],
            "runtime_inputs": [],
            "runtime_settings": [],
        }
        records = [
            {
                "path": "builder/input.sh",
                "size_bytes": 12,
                "sha256": "a" * 64,
                "executable_mode": 73,
            }
        ]

        def manifest(status: str) -> dict:
            def git_result(_repository: Path, *arguments: str) -> str:
                if arguments[0] == "log":
                    return "b" * 40
                if arguments[0] == "status":
                    return status
                raise AssertionError(arguments)

            with (
                mock.patch.object(self.module, "_git", side_effect=git_result),
                mock.patch.object(self.module, "_source_records", return_value=records),
            ):
                return self.module.build_stage_source_manifest(
                    ROOT,
                    "builder-toolchain",
                    ["builder/input.sh"],
                    declaration,
                )

        untracked = manifest("?? builder/input.sh")
        intent_to_add = manifest(" A builder/input.sh")
        self.assertNotEqual(untracked["status"], intent_to_add["status"])
        self.assertEqual(untracked["source_identity"], intent_to_add["source_identity"])

    def test_configured_runtime_manifest_uses_only_declared_stage_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            configured = {
                "package-targets": b"OMARCHY_RUNTIME_PACKAGE=omarchy-dev\n",
                "omarchy-base.packages": b"base\nlinux-asahi\n",
                "expected-packages": b"917\n",
                "expected-package-closure": b"base\t1\nlinux-asahi\t2\n",
            }
            for relative, content in configured.items():
                (runtime / relative).write_bytes(content)
            (runtime / "arm-repository").write_bytes(b"finalized-only")

            declaration = self.specification["stages"]["configured-target"]
            settings = {
                name: f"value-for-{name}"
                for name in declaration["runtime_settings"]
            }
            before = self.module.build_stage_runtime_manifest(
                root=runtime,
                stage="configured-target",
                declaration=declaration,
                settings=settings,
            )
            entries = {entry["path"]: entry for entry in before["entries"]}
            self.assertEqual(
                set(entries),
                {
                    "expected-packages",
                    "expected-package-closure",
                    "install-debug",
                    "omarchy-base.packages",
                    "package-targets",
                },
            )
            self.assertEqual(entries["install-debug"], {
                "path": "install-debug",
                "present": False,
            })

            alternate = runtime / "alternate-root"
            alternate.mkdir()
            for relative, content in configured.items():
                (alternate / relative).write_bytes(content)
            same_content_at_another_location = self.module.build_stage_runtime_manifest(
                root=alternate,
                stage="configured-target",
                declaration=declaration,
                settings=settings,
            )
            self.assertEqual(
                before["input_digest"],
                same_content_at_another_location["input_digest"],
            )

            (runtime / "arm-repository").write_bytes(b"changed-finalized-only")
            after_unrelated = self.module.build_stage_runtime_manifest(
                root=runtime,
                stage="configured-target",
                declaration=declaration,
                settings=settings,
            )
            self.assertEqual(before, after_unrelated)

            (runtime / "omarchy-base.packages").write_bytes(b"changed\n")
            after_required = self.module.build_stage_runtime_manifest(
                root=runtime,
                stage="configured-target",
                declaration=declaration,
                settings=settings,
            )
            self.assertNotEqual(before["input_digest"], after_required["input_digest"])

    def test_runtime_manifest_fails_closed_on_missing_or_unsafe_declared_input(self) -> None:
        declaration = self.specification["stages"]["configured-target"]
        settings = {
            name: f"value-for-{name}"
            for name in declaration["runtime_settings"]
        }
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime / "package-targets").write_text("targets\n")
            (runtime / "omarchy-base.packages").write_text("base\n")
            (runtime / "expected-package-closure").write_text("base\t1\n")
            with self.assertRaisesRegex(
                self.module.StageInputError,
                "required configured-target runtime input is missing: expected-packages",
            ):
                self.module.build_stage_runtime_manifest(
                    root=runtime,
                    stage="configured-target",
                    declaration=declaration,
                    settings=settings,
                )

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            runtime = temporary_root / "runtime"
            outside = temporary_root / "outside"
            runtime.mkdir()
            outside.mkdir()
            (outside / "secret").write_text("outside\n")
            (runtime / "nested").symlink_to(outside, target_is_directory=True)
            ancestor_declaration = {
                "runtime_inputs": [{"path": "nested/secret", "required": True}],
                "runtime_settings": [],
            }
            with self.assertRaisesRegex(
                self.module.StageInputError,
                "symlinked ancestor is forbidden",
            ):
                self.module.build_stage_runtime_manifest(
                    root=runtime,
                    stage="configured-target",
                    declaration=ancestor_declaration,
                    settings={},
                )

            (runtime / "package-targets").write_text("targets\n")
            (runtime / "omarchy-base.packages").write_text("base\n")
            (runtime / "expected-package-closure").write_text("base\t1\n")
            (runtime / "expected-packages").write_text("917\n")
            (runtime / "package-targets").unlink()
            (runtime / "package-targets").symlink_to(runtime / "expected-packages")
            with self.assertRaisesRegex(
                self.module.StageInputError,
                "symlink is forbidden",
            ):
                self.module.build_stage_runtime_manifest(
                    root=runtime,
                    stage="configured-target",
                    declaration=declaration,
                    settings=settings,
                )

    def test_runtime_manifest_requires_exact_declared_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            for relative in (
                "package-targets",
                "omarchy-base.packages",
                "expected-packages",
            ):
                (runtime / relative).write_text(f"{relative}\n")
            declaration = self.specification["stages"]["configured-target"]
            expected = set(declaration["runtime_settings"])
            settings = {name: "pinned" for name in expected}
            settings.pop(next(iter(expected)))
            with self.assertRaisesRegex(
                self.module.StageInputError,
                "runtime settings are incomplete or excessive",
            ):
                self.module.build_stage_runtime_manifest(
                    root=runtime,
                    stage="configured-target",
                    declaration=declaration,
                    settings=settings,
                )

    def test_stage_product_projection_excludes_unrelated_downstream_fields(self) -> None:
        product = json.loads((ROOT / "builder/products/omarchy-mx-mac.json").read_text())
        configured = self.module.build_stage_product_manifest(
            product=product,
            stage="configured-target",
        )
        changed = copy.deepcopy(product)
        changed["package_filename"] = "unrelated-sealed-package-name.zip"
        self.assertEqual(
            configured,
            self.module.build_stage_product_manifest(
                product=changed,
                stage="configured-target",
            ),
        )

        changed["kernel_package"] = "different-kernel"
        self.assertNotEqual(
            configured["input_digest"],
            self.module.build_stage_product_manifest(
                product=changed,
                stage="configured-target",
            )["input_digest"],
        )


if __name__ == "__main__":
    unittest.main()
