from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "builder/asahi_source_impact.py"
SPEC_PATH = ROOT / "builder/asahi-stage-inputs.json"
COSTS_PATH = ROOT / "builder/asahi-source-impact-costs.json"


def load_module():
    spec = importlib.util.spec_from_file_location("asahi_source_impact", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AsahiSourceImpactPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()
        cls.specification = json.loads(SPEC_PATH.read_text())
        cls.costs = json.loads(COSTS_PATH.read_text())

    def preview(
        self,
        *changed_paths: str,
        intent: str = "boot-only",
        profile: str = "diagnostic",
    ) -> dict:
        return self.module.preview_source_impact(
            repository=ROOT,
            specification=self.specification,
            cost_data=self.costs,
            changed_paths=list(changed_paths),
            intent=intent,
            profile=profile,
        )

    def test_boot_logo_change_is_ready_with_only_boot_and_outputs_invalidated(self) -> None:
        preview = self.preview("builder/branding/omarchy-logo.png")

        self.assertEqual(preview["verification_kind"], "asahi-source-impact-preview")
        self.assertEqual(preview["owner_stages"], ["finalized-boot"])
        self.assertEqual(
            preview["invalidation_frontier"],
            ["finalized-boot", "sealed-release-package", "installer-metadata"],
        )
        self.assertEqual(
            preview["expected_early_stage_hits"],
            [
                "builder-toolchain",
                "verified-package-cache",
                "offline-repository-database",
                "base-images",
                "configured-target",
            ],
        )
        self.assertTrue(preview["ready_for_expensive_work"])
        self.assertFalse(preview["blocked"])
        self.assertEqual(preview["claim_scope"], "source-declarations-only")
        self.assertEqual(preview["estimated_cost"]["total_known_seconds"], 34.0)
        self.assertTrue(preview["estimated_cost"]["complete"])

    def test_root_selector_has_the_same_boot_only_frontier(self) -> None:
        preview = self.preview(
            "configs/airootfs/usr/share/omarchy-iso/orchestrator/asahi_boot.py",
            profile="qualification",
        )

        self.assertEqual(preview["owner_stages"], ["finalized-boot"])
        self.assertEqual(
            preview["invalidation_frontier"],
            ["finalized-boot", "sealed-release-package", "installer-metadata"],
        )
        self.assertEqual(preview["estimated_cost"]["total_known_seconds"], 246.0)
        self.assertTrue(preview["ready_for_expensive_work"])

    def test_shared_package_builder_owns_every_stage_and_blocks_boot_only(
        self,
    ) -> None:
        # Third pinning of this path, and the first that is endorsed rather
        # than merely measured. Until 2026-08-29 (as
        # test_shared_package_builder_blocks_a_boot_only_run_before_building)
        # it was pinned as owning base-images through installer-metadata. From
        # 2026-08-29 (as test_shared_package_builder_is_admission_only_and_does
        # _not_block) it was pinned to the WIP tree's measured behaviour:
        # declared-admission-input, no owner stages, empty invalidation
        # frontier, blocked false, ready_for_expensive_work true,
        # admission_owner_stages ["base-images"], requires_readmission true --
        # recorded as an open fork, not endorsed.
        #
        # Owner decision 8(a) resolved 2026-08-30 as option A: the dispatcher
        # moves to producer identity. It is now a common producer input, so the
        # preview classifies it declared-stage-input, every stage owns it, the
        # invalidation frontier is the whole graph, and a boot-only run is
        # refused for invalidating stages ahead of its terminal. It is no
        # longer an admission input anywhere, so requires_readmission is false
        # and admission_owner_stages is empty.
        every_stage = [
            "builder-toolchain",
            "verified-package-cache",
            "offline-repository-database",
            "base-images",
            "configured-target",
            "finalized-boot",
            "sealed-release-package",
            "installer-metadata",
        ]
        preview = self.preview("builder/build-asahi-os-package.sh")

        self.assertEqual(
            preview["path_classifications"],
            {"builder/build-asahi-os-package.sh": "declared-stage-input"},
        )
        self.assertEqual(preview["owner_stages"], every_stage)
        self.assertEqual(preview["invalidation_frontier"], every_stage)
        self.assertTrue(preview["blocked"])
        self.assertEqual(len(preview["block_reasons"]), 1)
        self.assertIn(
            "boot-only intent invalidates stages before finalized-boot",
            preview["block_reasons"][0],
        )
        self.assertFalse(preview["ready_for_expensive_work"])
        self.assertEqual(preview["admission_owner_stages"], [])
        self.assertFalse(preview["requires_readmission"])

    def test_executed_input_that_can_affect_bytes_is_not_reported_ready(self) -> None:
        # THIS IS NOW THE ENFORCED SAFETY PROPERTY, not an aspiration. It was
        # an @unittest.expectedFailure from Phase B until 2026-08-30, when
        # owner decision 8(a) was resolved as OPTION A -- control-plane files
        # whose edits can change produced bytes join producer identity, so
        # builder/build-asahi-os-package.sh became a common producer input.
        #
        # post-M1 acceptance requires that "a missing or undisclosed executed
        # input blocks before expensive work". The assertion is deliberately
        # still written against the property rather than the mechanism, so it
        # keeps holding if the binding is ever expressed a different way. Under
        # option A both limbs are now satisfied: the invalidation frontier is
        # non-empty AND readiness is gated.
        preview = self.preview("builder/build-asahi-os-package.sh")

        producer_invalidated = bool(preview["invalidation_frontier"])
        readiness_gated = not preview["ready_for_expensive_work"]
        self.assertTrue(
            producer_invalidated or readiness_gated,
            "an executed input that can affect produced bytes reported "
            "ready_for_expensive_work with neither producer invalidation nor "
            "an authority gate",
        )
        # Option A satisfies both limbs; assert each so a regression to a
        # single-limb resolution is visible rather than silently tolerated.
        self.assertTrue(producer_invalidated)
        self.assertTrue(readiness_gated)

    def test_checkpoint_admission_adapter_requires_readmission_without_rebuild(self) -> None:
        preview = self.preview("builder/asahi-checkpoint-admission.sh")

        admission_stages = [
            "base-images",
            "configured-target",
            "finalized-boot",
            "sealed-release-package",
            "installer-metadata",
        ]
        self.assertEqual(preview["owner_stages"], [])
        self.assertEqual(preview["invalidation_frontier"], [])
        self.assertEqual(preview["admission_owner_stages"], admission_stages)
        self.assertEqual(preview["admission_frontier"], admission_stages)
        self.assertEqual(
            preview["path_classifications"]["builder/asahi-checkpoint-admission.sh"],
            "declared-admission-input",
        )
        self.assertTrue(preview["requires_readmission"])
        self.assertTrue(preview["ready_for_expensive_work"])
        self.assertFalse(preview["blocked"])

    def test_package_lock_and_source_changes_invalidate_package_cache_downstream(self) -> None:
        for path in (
            "builder/arm-package-snapshots.conf",
            "builder/archinstall.packages",
        ):
            with self.subTest(path=path):
                preview = self.preview(path, intent="package-content")
                self.assertEqual(preview["owner_stages"], ["verified-package-cache"])
                self.assertEqual(
                    preview["invalidation_frontier"],
                    [
                        "verified-package-cache",
                        "offline-repository-database",
                        "base-images",
                        "configured-target",
                        "finalized-boot",
                        "sealed-release-package",
                        "installer-metadata",
                    ],
                )
                self.assertTrue(preview["ready_for_expensive_work"])
                self.assertEqual(
                    preview["estimated_cost"]["unknown_stages"],
                    ["verified-package-cache", "base-images"],
                )
                self.assertFalse(preview["estimated_cost"]["complete"])

    def test_checkpoint_verifier_change_requires_readmission_without_rebuild(self) -> None:
        preview = self.preview("builder/asahi_checkpoint.py")

        self.assertEqual(preview["owner_stages"], [])
        self.assertEqual(preview["invalidation_frontier"], [])
        self.assertEqual(
            preview["admission_owner_stages"],
            self.specification["stage_order"],
        )
        self.assertEqual(preview["admission_frontier"], self.specification["stage_order"])
        self.assertEqual(
            preview["path_classifications"]["builder/asahi_checkpoint.py"],
            "declared-admission-input",
        )
        self.assertTrue(preview["requires_readmission"])
        self.assertTrue(preview["ready_for_expensive_work"])

    def test_unknown_executable_input_blocks_fail_closed(self) -> None:
        preview = self.preview("builder/new-stage-helper.py")

        self.assertEqual(
            preview["path_classifications"],
            {"builder/new-stage-helper.py": "unknown-executable-input"},
        )
        self.assertTrue(preview["blocked"])
        self.assertFalse(preview["ready_for_expensive_work"])
        self.assertIn("unknown executable input", preview["block_reasons"][0])

    def test_unknown_archiso_input_blocks_fail_closed(self) -> None:
        preview = self.preview("archiso/new-stage-helper.sh")

        self.assertEqual(
            preview["path_classifications"],
            {"archiso/new-stage-helper.sh": "unknown-executable-input"},
        )
        self.assertTrue(preview["blocked"])
        self.assertFalse(preview["ready_for_expensive_work"])

    def test_invalid_stage_specification_blocks_before_impact_projection(self) -> None:
        specification = json.loads(json.dumps(self.specification))
        specification["stages"]["finalized-boot"]["source_paths"].append(
            "builder/omitted-source.sh"
        )

        with self.assertRaisesRegex(
            self.module.SourceImpactError,
            "stage input specification is invalid",
        ):
            self.module.preview_source_impact(
                repository=ROOT,
                specification=specification,
                cost_data=self.costs,
                changed_paths=["builder/branding/omarchy-logo.png"],
                intent="boot-only",
                profile="diagnostic",
            )

    def test_docs_and_evidence_are_not_applicable_to_source_stages(self) -> None:
        paths = [
            "docs/iteration-notes.md",
            "release/build-evidence/example/receipt.json",
        ]
        preview = self.preview(*paths)

        self.assertEqual(preview["not_applicable_paths"], paths)
        self.assertEqual(preview["owner_stages"], [])
        self.assertEqual(preview["invalidation_frontier"], [])
        self.assertTrue(preview["ready_for_expensive_work"])

    def test_cli_is_a_bounded_json_preflight_with_repeated_changed_paths(self) -> None:
        # Budget raised from 1.0 s to 2.0 s on 2026-08-29 (test previously named
        # test_cli_is_a_sub_second_json_preflight_with_repeated_changed_paths).
        # The 1.0 s bound was stricter than the documented L0 SLO -- the
        # iteration-performance-plan feedback ladder allows a read-only impact
        # preview up to 2 s. Measured on the live tree that day: 1.05-1.07 s
        # here, 1.14 s by the controller; previously ~0.03 s in-process. The
        # regression against the in-process cost is C1+ performance debt,
        # tracked in the plan's owner decision queue.
        started = time.monotonic()
        result = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "--repo-root",
                str(ROOT),
                "--spec",
                str(SPEC_PATH),
                "--cost-data",
                str(COSTS_PATH),
                "--changed-path",
                "builder/branding/omarchy-logo.png",
                "--changed-path",
                "configs/airootfs/usr/share/omarchy-iso/orchestrator/asahi_boot.py",
                "--intent",
                "boot-only",
                "--profile",
                "diagnostic",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 2.0)
        preview = json.loads(result.stdout)
        self.assertEqual(preview["owner_stages"], ["finalized-boot"])
        self.assertTrue(preview["ready_for_expensive_work"])

    def test_cli_returns_nonzero_with_json_evidence_when_preflight_blocks(self) -> None:
        # Superseded input (until 2026-08-29): this drove the blocking path with
        # builder/build-asahi-os-package.sh, which stopped blocking when it was
        # admission-declared, so the CLI exited 0 for it (plan owner decision
        # queue item 8). The contract under test here is the CLI's own
        # behaviour when the preflight does block, so it was re-pinned to an
        # input that still blocks: builder/asahi-stages/base-images.sh is a
        # declared producer input of base-images, and a boot-only intent
        # invalidating a stage before finalized-boot is an intent violation.
        # Measured rc 2 in 1.07 s. The original input blocks again since 8(a)
        # resolved as option A on 2026-08-30, but this test keeps the
        # base-images input: it exercises the same CLI contract without
        # depending on the fork's outcome. See
        # test_shared_package_builder_owns_every_stage_and_blocks_boot_only.
        #
        # Timeout raised 1.0 s -> 2.0 s for the reason recorded in
        # test_cli_is_a_bounded_json_preflight_with_repeated_changed_paths.
        result = subprocess.run(
            [
                sys.executable,
                str(MODULE_PATH),
                "--repo-root",
                str(ROOT),
                "--spec",
                str(SPEC_PATH),
                "--cost-data",
                str(COSTS_PATH),
                "--changed-path",
                "builder/asahi-stages/base-images.sh",
                "--intent",
                "boot-only",
                "--profile",
                "diagnostic",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        preview = json.loads(result.stdout)
        self.assertTrue(preview["blocked"])
        self.assertFalse(preview["ready_for_expensive_work"])
        self.assertIn("finalized-boot", preview["block_reasons"][0])


if __name__ == "__main__":
    unittest.main()
