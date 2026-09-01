from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "builder/summarize-asahi-build.py"


def load_module():
    spec = importlib.util.spec_from_file_location("summarize_asahi_build", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildSummaryCatalogAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".build-summary-test-", dir=ROOT
        )
        self.root = Path(self.temporary.name)
        self.package = self.root / "omarchy.zip"
        self.package.write_bytes(b"sealed-package")
        self._write_phase("verified-package-cache", ["repository-manifest"])
        self._write_phase(
            "offline-repository-database", ["repository-db", "repository-files"]
        )
        self._write_phase("base-images", ["root-image", "boot-image", "esp-image"])
        self._write_phase("configured-target", ["installed-contract"])
        self._write_phase("finalized-boot", ["installed-contents"])
        self._write_phase(
            "sealed-release-package",
            ["release-package"],
            reproducibility_match=True,
        )
        self._write_phase(
            "installer-metadata", ["package-evidence", "installer-data"]
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_phase(
        self,
        stage: str,
        outputs: list[str],
        *,
        cache_hit: bool = False,
        reproducibility_match: bool = False,
        validation: str = "passed",
    ) -> None:
        value = {
            "stage": stage,
            "checkpoint_identity": f"identity-{stage}",
            "elapsed_seconds": 1,
            "cache_hit": cache_hit,
            "outputs": [{"name": name, "kind": "file"} for name in outputs],
            "validation": {"result": validation},
        }
        if reproducibility_match:
            value["reproducibility_match"] = True
        (self.root / f"{stage}.json").write_text(json.dumps(value))

    def report(self, *, mode: str = "qualification", epoch: int | None = 1):
        return self.module.build_report(
            mode=mode,
            run_id="run-1",
            evidence_root=self.root,
            package=self.package if mode == "qualification" else None,
            source_date_epoch=epoch,
        )

    def test_independent_same_input_rebuild_is_catalog_eligible(self) -> None:
        report = self.report()
        self.assertTrue(report["catalog_eligible"])
        self.assertEqual(report["catalog_admission"], {"result": "passed", "reasons": []})
        self.assertEqual(report["source_date_epoch"], 1)

    def test_mode_alone_never_makes_first_build_catalog_eligible(self) -> None:
        self._write_phase("sealed-release-package", ["release-package"])
        report = self.report()
        self.assertFalse(report["catalog_eligible"])
        self.assertIn(
            "sealed release was not independently rebuilt with identical bytes",
            report["catalog_admission"]["reasons"],
        )

    def test_checkpoint_restore_is_not_an_independent_rebuild(self) -> None:
        self._write_phase(
            "sealed-release-package",
            ["release-package"],
            cache_hit=True,
            reproducibility_match=True,
        )
        report = self.report()
        self.assertFalse(report["catalog_eligible"])
        self.assertIn(
            "sealed release was restored instead of independently rebuilt",
            report["catalog_admission"]["reasons"],
        )

    def test_epoch_and_installed_contract_are_required(self) -> None:
        self._write_phase("configured-target", ["root-image"])
        report = self.report(epoch=None)
        self.assertFalse(report["catalog_eligible"])
        self.assertIn(
            "SOURCE_DATE_EPOCH was not bound",
            report["catalog_admission"]["reasons"],
        )
        self.assertIn(
            "configured installed-content contract is missing",
            report["catalog_admission"]["reasons"],
        )

    def test_diagnostic_report_is_never_catalog_eligible(self) -> None:
        report = self.report(mode="diagnostic", epoch=None)
        self.assertFalse(report["catalog_eligible"])
        self.assertIn(
            "diagnostic mode is never catalog eligible",
            report["catalog_admission"]["reasons"],
        )


if __name__ == "__main__":
    unittest.main()
