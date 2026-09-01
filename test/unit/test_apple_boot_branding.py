from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "builder/brand-apple-silicon-boot.py"
SPEC = importlib.util.spec_from_file_location("apple_boot_branding", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AppleBootBrandingTests(unittest.TestCase):
    def test_repository_branding_assets_match_the_fail_closed_manifest(self) -> None:
        manifest_path = ROOT / "builder/branding/branding-manifest.json"
        manifest = MODULE.load_manifest(manifest_path)
        product = json.loads(
            (ROOT / "builder/products/omarchy-mx-mac.json").read_text()
        )

        MODULE.verify_assets(manifest, manifest_path.parent)

        self.assertEqual(
            manifest["source_logo"]["sha256"],
            "edd69e61d711d8b423555f27a5afc64935c299f6e7f779112d2ce970ec0236e4",
        )
        self.assertEqual(
            manifest["volume_icon"]["sha256"],
            "cf26ed5d2831db99c00d62ca046040e01a18e08e63363d629340d04ac6ec8c23",
        )
        self.assertEqual(manifest["m1n1"]["input"]["size_bytes"], 6_205_289)
        self.assertEqual(
            manifest["m1n1"]["input"]["sha256"],
            "bb6829c44d8de26d6615406b41edc0beef2254766b5ed114afad2029db7ae856",
        )
        self.assertEqual(
            manifest["m1n1"]["output"]["sha256"],
            "28c76ee3eb046a903db81277ad8f7ab887059b2fa522eacb47445c7f8868288b",
        )
        self.assertEqual(
            product["branding"]["m1n1_boot_sha256"],
            manifest["m1n1"]["output"]["sha256"],
        )
        self.assertEqual(
            [item["offset"] for item in manifest["m1n1"]["replacements"]],
            [458_752, 467_968, 533_504],
        )

    def test_m1n1_patch_changes_only_declared_logo_regions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assets = root / "assets"
            assets.mkdir()
            source = bytearray(b"x" * 256)
            replacements = []
            for index, (offset, size) in enumerate(((24, 12), (80, 20))):
                original = bytes([index + 1]) * size
                replacement = bytes([index + 21]) * size
                source[offset : offset + size] = original
                filename = f"replacement-{index}.bin"
                (assets / filename).write_bytes(replacement)
                replacements.append(
                    {
                        "offset": offset,
                        "size_bytes": size,
                        "source_sha256": _sha256(original),
                        "replacement": {
                            "filename": filename,
                            "size_bytes": size,
                            "sha256": _sha256(replacement),
                        },
                    }
                )
            expected = bytearray(source)
            for replacement in replacements:
                data = (assets / replacement["replacement"]["filename"]).read_bytes()
                offset = replacement["offset"]
                expected[offset : offset + len(data)] = data
            manifest = {
                "schema_version": 1,
                "source_logo": {
                    "filename": "unused.png",
                    "size_bytes": 1,
                    "sha256": "0" * 64,
                },
                "volume_icon": {
                    "filename": "unused.icns",
                    "size_bytes": 8,
                    "sha256": "0" * 64,
                    "representations": [],
                },
                "m1n1": {
                    "input": {
                        "size_bytes": len(source),
                        "sha256": _sha256(source),
                    },
                    "output": {
                        "size_bytes": len(expected),
                        "sha256": _sha256(expected),
                    },
                    "replacements": replacements,
                },
            }
            source_path = root / "boot.bin"
            output_path = root / "branded.bin"
            source_path.write_bytes(source)

            MODULE.patch_m1n1_boot(manifest, assets, source_path, output_path)

            actual = output_path.read_bytes()
            self.assertEqual(actual, expected)
            changed = {
                index
                for index, (before, after) in enumerate(zip(source, actual))
                if before != after
            }
            expected_changed = set(range(24, 36)) | set(range(80, 100))
            self.assertEqual(changed, expected_changed)

    def test_m1n1_patch_rejects_stale_input_before_writing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = root / "boot.bin"
            output_path = root / "branded.bin"
            source_path.write_bytes(b"stale")
            manifest = {
                "schema_version": 1,
                "m1n1": {
                    "input": {"size_bytes": 5, "sha256": _sha256(b"fresh")},
                    "output": {"size_bytes": 5, "sha256": _sha256(b"fresh")},
                    "replacements": [],
                },
            }

            with self.assertRaisesRegex(
                MODULE.BrandingError,
                "m1n1 input digest mismatch",
            ):
                MODULE.patch_m1n1_boot(manifest, root, source_path, output_path)

            self.assertFalse(output_path.exists())

    def test_m1n1_patch_rejects_undeclared_region_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = b"0123456789"
            replacement = b"AB"
            (root / "replacement.bin").write_bytes(replacement)
            manifest = {
                "schema_version": 1,
                "m1n1": {
                    "input": {
                        "size_bytes": len(source),
                        "sha256": _sha256(source),
                    },
                    "output": {
                        "size_bytes": len(source),
                        "sha256": _sha256(b"01AB456789"),
                    },
                    "replacements": [
                        {
                            "offset": 2,
                            "size_bytes": 2,
                            "source_sha256": _sha256(b"wrong"),
                            "replacement": {
                                "filename": "replacement.bin",
                                "size_bytes": 2,
                                "sha256": _sha256(replacement),
                            },
                        }
                    ],
                },
            }
            source_path = root / "boot.bin"
            source_path.write_bytes(source)

            with self.assertRaisesRegex(
                MODULE.BrandingError,
                "m1n1 source logo region digest mismatch",
            ):
                MODULE.patch_m1n1_boot(
                    manifest,
                    root,
                    source_path,
                    root / "branded.bin",
                )


if __name__ == "__main__":
    unittest.main()
