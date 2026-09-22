import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]

class CheckpointLocks(unittest.TestCase):
    def check_path(self, candidate, schema=3):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "verified-package-cache"
            stage.mkdir()
            for name in ("source-lock.json", "source-manifest.json", "runtime.json"):
                (stage / name).write_text("{}")
            source = (ROOT / "builder/checkpoint-verified-package-cache.sh").read_text()
            source = source[source.index("create_verified_package_cache_identity() {"):]
            source = source.replace("/out/build-evidence/", str(root / "evidence") + "/")
            script = root / "test.sh"
            script.write_text("#!/bin/bash\nset -eu\n" + source + "\npython3() { printf '%s\\n' \"$@\"; return 17; }\ncheckpoint_verified_package_cache\n".replace('\"', '"'))
            env = dict(os.environ, OMARCHY_BUILD_RUN_ID="test", OMARCHY_ASAHI_CHECKPOINT_ROOT=str(root / "cache"), offline_mirror_dir=str(root / "mirror"), asahi_stage_input_root=str(root), verified_package_runtime_manifest=str(stage / "runtime.json"), requested_package_files=str(root / "requested"), OMARCHY_DEPENDENCY_ROOT="/dependencies" if candidate else "", OMARCHY_CANDIDATE_ROOT="/candidate", OMARCHY_CANDIDATE_SCHEMA=str(schema))
            result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 17, result.stderr)
            if candidate:
                for value in ("candidate-receipt=/candidate/signing.json", "dependency-manifest=/dependencies/manifest.json", "dependency-origin=/dependencies/origin.db", "hyprland-repair=/builder/quattro-hyprland-repair.json"):
                    self.assertIn(value, result.stdout)
                self.assertNotIn("ARM-PACKAGES", result.stdout)
                self.assertNotIn("arm-snapshot=", result.stdout)
            else:
                self.assertIn("ARM-PACKAGES", result.stdout)
                self.assertIn("arm-snapshot=", result.stdout)
                self.assertNotIn("dependency-manifest=", result.stdout)
            self.assertIn("apple-platform=", result.stdout)
            if schema == 4:
                self.assertIn('limine=/builder/quattro-limine.json', result.stdout)
            else:
                self.assertNotIn('limine=/builder/quattro-limine.json', result.stdout)

    def test_candidate_provenance(self): self.check_path(True)
    def test_limine_provenance(self): self.check_path(True, 4)
    def test_legacy_provenance(self): self.check_path(False)

if __name__ == "__main__": unittest.main()
