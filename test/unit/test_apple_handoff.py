import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE = (
    ROOT
    / "configs/airootfs/usr/local/lib/omarchy/apple_handoff.py"
)
SPEC = importlib.util.spec_from_file_location("apple_handoff", MODULE)
apple_handoff = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = apple_handoff
SPEC.loader.exec_module(apple_handoff)


class AppleHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.private_key = cls.root / "private.pem"
        cls.public_key = cls.root / "public.pem"
        subprocess.run(
            [
                "openssl", "genpkey", "-algorithm", "ED25519",
                "-out", str(cls.private_key),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                "openssl", "pkey", "-in", str(cls.private_key),
                "-pubout", "-out", str(cls.public_key),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        der = subprocess.run(
            [
                "openssl", "pkey", "-pubin", "-in", str(cls.public_key),
                "-outform", "DER",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout
        cls.trusted_key = "sha256:" + hashlib.sha256(der).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.plan_digest = "a" * 64
        self.manifest = {
            "schema_version": 1,
            "sequence": 7,
            "installation_id": "12345678-1234-4234-9234-123456789abc",
            "device_identifier": "apple,j314s",
            "plan_digest": self.plan_digest,
            "layout_digest": "sha256:" + "b" * 64,
            "disk": {
                "gpt_disk_uuid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                "size_bytes": 1_000_000_000_000,
                "logical_sector_bytes": 512,
            },
            "install_partition": {
                "partition_uuid": "77777777-6666-4555-8444-333333333333",
                "partition_type": apple_handoff.LINUX_FILESYSTEM_PARTITION_TYPE,
                "offset_bytes": 700_000_000_000,
                "length_bytes": 100_000_000_000,
            },
            "paired_esp": {
                "partition_uuid": "11111111-2222-4333-8444-555555555555",
                "partition_type": apple_handoff.EFI_SYSTEM_PARTITION_TYPE,
                "offset_bytes": 699_000_000_000,
                "length_bytes": 500_170_752,
            },
            "artifacts": {
                "engine_digest": "sha256:" + "c" * 64,
                "metadata_digest": "sha256:" + "d" * 64,
                "payload_digest": "sha256:" + "e" * 64,
            },
        }
        self.layout = apple_handoff.DiskLayout(
            device="/dev/nvme0n1",
            gpt_disk_uuid=self.manifest["disk"]["gpt_disk_uuid"],
            size_bytes=self.manifest["disk"]["size_bytes"],
            logical_sector_bytes=512,
            partitions=(
                apple_handoff.Partition(
                    node="/dev/nvme0n1p4",
                    partition_uuid=self.manifest["paired_esp"]["partition_uuid"],
                    partition_type=apple_handoff.EFI_SYSTEM_PARTITION_TYPE,
                    extent=apple_handoff.Extent(699_000_000_000, 500_170_752),
                ),
                apple_handoff.Partition(
                    node="/dev/nvme0n1p5",
                    partition_uuid=self.manifest["install_partition"]["partition_uuid"],
                    partition_type=apple_handoff.LINUX_FILESYSTEM_PARTITION_TYPE,
                    extent=apple_handoff.Extent(700_000_000_000, 100_000_000_000),
                ),
            ),
        )

    def test_valid_handoff_exposes_only_bounded_writes(self):
        verified = self._verify()

        verified.assert_write(700_000_000_000, 4096)
        verified.assert_write(799_999_995_904, 4096)
        with self.assertRaisesRegex(apple_handoff.HandoffError, "escapes"):
            verified.assert_write(699_999_999_999, 4096)
        with self.assertRaisesRegex(apple_handoff.HandoffError, "escapes"):
            verified.assert_write(799_999_999_999, 4096)

    def test_verifies_swift_generated_handoff_fixture(self):
        manifest = json.dumps(
            self.manifest, separators=(",", ":"), sort_keys=True
        ).encode()
        signature = base64.b64decode(
            "Wh44JOPKL/KZA1mENqsu//glN/MCMsMApNiqsJKLDrV0BzWoH6TOK9jIumf3cWfG"
            "YTMxjX9Samx4K2ZuMKqbAw=="
        )
        public_key = base64.b64decode(
            "LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUNvd0JRWURLMlZ3QXlFQUE2RUh2"
            "L1BPRUw0ZGNOMFk1MHZBbVdmazFqQ2JwUTFmSGR5R1pCSlZNYmc9Ci0tLS0tRU5E"
            "IFBVQkxJQyBLRVktLS0tLQo="
        )

        verified = apple_handoff.verify_handoff(
            manifest,
            signature,
            public_key,
            "sha256:a050837d85070582ccf7394b0988847cc312cb88259b894899f6f239cf1791a5",
            self.plan_digest,
            self.manifest["installation_id"],
            self.layout,
        )

        self.assertEqual(
            verified.install_partition.partition_uuid,
            self.manifest["install_partition"]["partition_uuid"],
        )

    def test_signature_covers_exact_manifest_bytes(self):
        manifest, signature = self._signed()
        changed = manifest.replace(b'"sequence":7', b'"sequence":8')

        with self.assertRaisesRegex(apple_handoff.HandoffError, "signature"):
            self._verify_bytes(changed, signature)

    def test_boot_chain_plan_digest_is_required(self):
        with self.assertRaisesRegex(apple_handoff.HandoffError, "boot-chain plan"):
            self._verify(expected_plan="f" * 64)

    def test_boot_chain_installation_id_is_required(self):
        with self.assertRaisesRegex(apple_handoff.HandoffError, "boot-chain installation"):
            self._verify(
                expected_installation="99999999-8888-4777-8666-555555555555"
            )

    def test_boot_chain_key_fingerprint_is_required(self):
        with self.assertRaisesRegex(apple_handoff.HandoffError, "trust anchor"):
            self._verify(trusted_key="sha256:" + "0" * 64)

    def test_existing_partition_inside_extent_fails_closed(self):
        intruder = apple_handoff.Partition(
            node="/dev/nvme0n1p6",
            partition_uuid="99999999-8888-4777-8666-555555555555",
            partition_type="0fc63daf-8483-4772-8e79-3d69d8477de4",
            extent=apple_handoff.Extent(750_000_000_000, 1_000_000),
        )
        layout = apple_handoff.DiskLayout(
            device=self.layout.device,
            gpt_disk_uuid=self.layout.gpt_disk_uuid,
            size_bytes=self.layout.size_bytes,
            logical_sector_bytes=self.layout.logical_sector_bytes,
            partitions=self.layout.partitions + (intruder,),
        )

        with self.assertRaisesRegex(apple_handoff.HandoffError, "overlaps"):
            self._verify(layout=layout)

    def test_changed_install_partition_fails_closed(self):
        self.manifest["install_partition"]["length_bytes"] -= 512

        with self.assertRaisesRegex(apple_handoff.HandoffError, "install partition"):
            self._verify()

    def test_changed_paired_esp_fails_closed(self):
        self.manifest["paired_esp"]["length_bytes"] += 512

        with self.assertRaisesRegex(apple_handoff.HandoffError, "paired ESP"):
            self._verify()

    def test_unknown_whole_disk_field_fails_closed(self):
        self.manifest["whole_disk"] = True

        with self.assertRaisesRegex(apple_handoff.HandoffError, "manifest fields"):
            self._verify()

    def test_duplicate_safety_field_fails_closed(self):
        manifest, _ = self._signed()
        duplicate = manifest.replace(
            b'"sequence":7',
            b'"sequence":6,"sequence":7',
        )
        signature = self._sign(duplicate)

        with self.assertRaisesRegex(apple_handoff.HandoffError, "duplicate JSON"):
            self._verify_bytes(duplicate, signature)

    def test_boot_contract_requires_unique_complete_fields(self):
        values = apple_handoff.read_boot_contract(
            "quiet "
            f"omarchy.handoff_key_sha256={self.trusted_key} "
            f"omarchy.handoff_plan_digest={self.plan_digest} "
            "omarchy.handoff_installation_id=12345678-1234-4234-9234-123456789abc"
        )
        self.assertEqual(
            values,
            (
                self.trusted_key,
                self.plan_digest,
                "12345678-1234-4234-9234-123456789abc",
            ),
        )
        with self.assertRaisesRegex(apple_handoff.HandoffError, "complete"):
            apple_handoff.read_boot_contract(
                f"omarchy.handoff_key_sha256={self.trusted_key}"
            )
        with self.assertRaisesRegex(apple_handoff.HandoffError, "duplicate"):
            apple_handoff.read_boot_contract(
                f"omarchy.handoff_key_sha256={self.trusted_key} "
                f"omarchy.handoff_key_sha256={self.trusted_key} "
                f"omarchy.handoff_plan_digest={self.plan_digest} "
                "omarchy.handoff_installation_id=12345678-1234-4234-9234-123456789abc"
            )

    def test_apple_handoff_requires_exact_media_target(self):
        apple_handoff.require_apple_media_target(b"aarch64/apple-silicon\n")

        for marker in (
            b"aarch64/generic\n",
            b"x86_64/generic\n",
            b"aarch64/apple-silicon-extra\n",
            b"\xff",
        ):
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(
                    apple_handoff.HandoffError,
                    "media target",
                ):
                    apple_handoff.require_apple_media_target(marker)

    def test_sfdisk_sectors_are_converted_to_byte_extents(self):
        raw = json.dumps({
            "partitiontable": {
                "label": "gpt",
                "id": self.layout.gpt_disk_uuid,
                "device": "/dev/nvme0n1",
                "unit": "sectors",
                "sectorsize": 512,
                "partitions": [{
                    "node": "/dev/nvme0n1p4",
                    "start": 1_365_234_375,
                    "size": 976_562,
                    "type": apple_handoff.EFI_SYSTEM_PARTITION_TYPE.upper(),
                    "uuid": self.manifest["paired_esp"]["partition_uuid"].upper(),
                }],
            }
        }).encode()

        layout = apple_handoff.parse_sfdisk_layout(raw, 1_000_000_000_000)

        self.assertEqual(layout.partitions[0].extent.offset_bytes, 699_000_000_000)
        self.assertEqual(layout.partitions[0].extent.length_bytes, 499_999_744)

    def _verify(
        self,
        *,
        trusted_key=None,
        expected_plan=None,
        expected_installation=None,
        layout=None,
    ):
        manifest, signature = self._signed()
        return self._verify_bytes(
            manifest,
            signature,
            trusted_key=trusted_key,
            expected_plan=expected_plan,
            expected_installation=expected_installation,
            layout=layout,
        )

    def _verify_bytes(
        self,
        manifest,
        signature,
        *,
        trusted_key=None,
        expected_plan=None,
        expected_installation=None,
        layout=None,
    ):
        return apple_handoff.verify_handoff(
            manifest,
            signature,
            self.public_key.read_bytes(),
            trusted_key or self.trusted_key,
            expected_plan or self.plan_digest,
            expected_installation or self.manifest["installation_id"],
            layout or self.layout,
        )

    def _signed(self):
        manifest = json.dumps(
            self.manifest, separators=(",", ":"), sort_keys=True
        ).encode()
        return manifest, self._sign(manifest)

    def _sign(self, manifest):
        manifest_path = self.root / "manifest.json"
        signature_path = self.root / "manifest.sig"
        manifest_path.write_bytes(manifest)
        subprocess.run(
            [
                "openssl", "pkeyutl", "-sign", "-inkey", str(self.private_key),
                "-rawin", "-in", str(manifest_path), "-out", str(signature_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return signature_path.read_bytes()


if __name__ == "__main__":
    unittest.main()
