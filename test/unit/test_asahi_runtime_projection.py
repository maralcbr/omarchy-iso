from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "builder/asahi_runtime_projection.py"


def load_module():
    spec = importlib.util.spec_from_file_location("asahi_runtime_projection", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AsahiRuntimeProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.runtime = self.root / "runtime"
        self.repository.mkdir()
        self.runtime.mkdir()

        prefix = self.repository / "configs/airootfs/usr/share/omarchy-iso"
        (prefix / "orchestrator").mkdir(parents=True)
        (prefix / "orchestrator/__init__.py").write_text("")
        (prefix / "orchestrator/configured.py").write_text("STAGE = 'configured'\n")
        (prefix / "orchestrator/finalized.py").write_text("STAGE = 'finalized'\n")

        for relative, content in {
            "expected-packages": "919\n",
            "package-targets": "OMARCHY_RUNTIME_PACKAGE=omarchy-dev\n",
            "arm-repository": "repository\n",
        }.items():
            (self.runtime / relative).write_text(content)

        source_prefix = "configs/airootfs/usr/share/omarchy-iso/"
        self.specification = {
            "stages": {
                "configured-target": {
                    "source_paths": [
                        source_prefix + "orchestrator/__init__.py",
                        source_prefix + "orchestrator/configured.py",
                    ],
                    "runtime_inputs": [
                        {"path": "expected-packages", "required": True},
                        {"path": "package-targets", "required": True},
                        {"path": "install-debug", "required": False},
                    ],
                },
                "finalized-boot": {
                    "source_paths": [
                        source_prefix + "orchestrator/__init__.py",
                        source_prefix + "orchestrator/configured.py",
                        source_prefix + "orchestrator/finalized.py",
                    ],
                    "runtime_inputs": [
                        {"path": "arm-repository", "required": True},
                        {"path": "expected-packages", "required": True},
                    ],
                },
            }
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_stage_roots_contain_only_their_declared_code_and_runtime_inputs(self) -> None:
        configured = self.root / "configured"
        finalized = self.root / "finalized"
        self.module.project_stage_runtime(
            repository=self.repository,
            runtime_root=self.runtime,
            output_root=configured,
            specification=self.specification,
            stage="configured-target",
        )
        self.module.project_stage_runtime(
            repository=self.repository,
            runtime_root=self.runtime,
            output_root=finalized,
            specification=self.specification,
            stage="finalized-boot",
        )

        self.assertTrue((configured / "orchestrator/configured.py").is_file())
        self.assertTrue((configured / "expected-packages").is_file())
        self.assertFalse((configured / "orchestrator/finalized.py").exists())
        self.assertFalse((configured / "arm-repository").exists())
        self.assertTrue((finalized / "orchestrator/finalized.py").is_file())
        self.assertTrue((finalized / "arm-repository").is_file())
        self.assertEqual((finalized / "expected-packages").read_text(), "919\n")
        self.assertEqual(configured.stat().st_mode & 0o222, 0)
        self.assertEqual(
            (configured / "package-targets").stat().st_mode & 0o222,
            0,
        )

    def test_missing_required_or_symlinked_runtime_input_fails_closed(self) -> None:
        (self.runtime / "expected-packages").unlink()
        with self.assertRaisesRegex(
            self.module.RuntimeProjectionError,
            "required configured-target runtime input is missing: expected-packages",
        ):
            self.module.project_stage_runtime(
                repository=self.repository,
                runtime_root=self.runtime,
                output_root=self.root / "missing",
                specification=self.specification,
                stage="configured-target",
            )

        (self.runtime / "expected-packages").symlink_to("package-targets")
        with self.assertRaisesRegex(
            self.module.RuntimeProjectionError,
            "runtime input is unsafe: expected-packages",
        ):
            self.module.project_stage_runtime(
                repository=self.repository,
                runtime_root=self.runtime,
                output_root=self.root / "symlinked",
                specification=self.specification,
                stage="configured-target",
            )


if __name__ == "__main__":
    unittest.main()
