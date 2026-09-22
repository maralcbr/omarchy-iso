#!/usr/bin/env python3
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]

class RepairTest(unittest.TestCase):
    def run_case(self, failure=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "packages"
            destination.mkdir()
            mocks = root / "bin"
            mocks.mkdir()
            record = json.loads((ROOT / "builder/quattro-hyprland-repair.json").read_text())
            record["sha256"] = hashlib.sha256(b"archive").hexdigest()
            (root / "quattro-hyprland-repair.json").write_text(json.dumps(record))
            (destination / record["filename"]).write_bytes(b"corrupt" if failure == "hash" else b"archive")
            signer = "WRONG" if failure == "signer" else record["signer"]
            metadata = "pkgname = hyprland\npkgver = 0.56.2-3\narch = aarch64\ndepend = libaquamarine.so=14-64\n"
            if failure == "metadata":
                metadata = metadata.replace("14-64", "13-64")
            scripts = {
                "curl": "exit 91\n",  # Cache reuse must not require the network.
                "gpg": 'if [[ " $* " == *" --verify "* ]]; then echo "[GNUPG:] VALIDSIG ' + signer + '"; fi\n',
                "bsdtar": "cat <<'EOF'\n" + metadata + "EOF\n",
            }
            for name, body in scripts.items():
                path = mocks / name
                path.write_text("#!/bin/bash\n" + body)
                path.chmod(0o755)
            env = dict(os.environ, BUILDER_ROOT=str(root), PATH=str(mocks) + ":" + os.environ["PATH"])
            result = subprocess.run(["bash", str(ROOT / "builder/fetch-quattro-hyprland.sh"), str(destination)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode == 0, failure is None, result.stderr)
            self.assertEqual((destination / (record["filename"] + ".sig")).exists(), failure is None)

    def test_verified_cached_package(self): self.run_case()
    def test_bad_cached_bytes_require_download(self): self.run_case("hash")
    def test_wrong_signer_rejected(self): self.run_case("signer")
    def test_wrong_abi_rejected(self): self.run_case("metadata")

if __name__ == "__main__":
    unittest.main()
