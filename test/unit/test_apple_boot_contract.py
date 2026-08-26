import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "configs/airootfs/usr/local/lib"))

from omarchy.apple_boot_contract import (  # noqa: E402
    GRUB_FALLBACK_PATH,
    verify_boot_file_contract,
)
from omarchy.apple_handoff import (  # noqa: E402
    DiskLayout,
    EFI_SYSTEM_PARTITION_TYPE,
    Extent,
    HandoffError,
    LINUX_FILESYSTEM_PARTITION_TYPE,
    Partition,
    VerifiedHandoff,
)


class AppleBootContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.private_key = cls.root / "private.pem"
        cls.public_key = cls.root / "public.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", cls.private_key],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["openssl", "pkey", "-in", cls.private_key, "-pubout", "-out", cls.public_key],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        der = subprocess.run(["openssl", "pkey", "-pubin", "-in", cls.public_key, "-outform", "DER"],
                             check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
        cls.key_digest = "sha256:" + hashlib.sha256(der).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        esp = Partition("/dev/nvme0n1p4", "11111111-2222-4333-8444-555555555555",
                        EFI_SYSTEM_PARTITION_TYPE, Extent(699_000_000_000, 500_170_752))
        target = Partition("/dev/nvme0n1p5", "77777777-6666-4555-8444-333333333333",
                           LINUX_FILESYSTEM_PARTITION_TYPE, Extent(700_000_000_000, 100_000_000_000))
        self.handoff = VerifiedHandoff(
            "12345678-1234-4234-9234-123456789abc", 7, "apple,j314s", "a" * 64,
            DiskLayout("/dev/nvme0n1", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                       1_000_000_000_000, 512, (esp, target)), target, esp)
        self.content = b"signed GRUB core image"
        self.document = {
            "schema_version": 1,
            "installation_id": self.handoff.installation_id,
            "plan_digest": self.handoff.plan_digest,
            "paired_esp_partition_uuid": self.handoff.paired_esp.partition_uuid,
            "boot_backend": "asahi-grub",
            "writes": [{
                "path": GRUB_FALLBACK_PATH,
                "sha256": hashlib.sha256(self.content).hexdigest(),
                "owner": "asahi-update-grub",
                "operation": "atomic-replace",
            }],
            "preserve": ["/m1n1/boot.bin"],
        }

    def test_signed_contract_authorizes_only_exact_grub_bytes(self):
        contract, signature = self._signed()
        verified = verify_boot_file_contract(contract, signature, self.public_key.read_bytes(),
                                             self.key_digest, self.handoff)
        verified.authorize(GRUB_FALLBACK_PATH, self.content)
        with self.assertRaisesRegex(HandoffError, "content"):
            verified.authorize(GRUB_FALLBACK_PATH, self.content + b"changed")
        with self.assertRaisesRegex(HandoffError, "path"):
            verified.authorize("/m1n1/boot.bin", self.content)

    def test_contract_cannot_retarget_the_paired_esp(self):
        self.document["paired_esp_partition_uuid"] = "99999999-8888-4777-8666-555555555555"
        contract, signature = self._signed()
        with self.assertRaisesRegex(HandoffError, "paired ESP"):
            verify_boot_file_contract(contract, signature, self.public_key.read_bytes(),
                                      self.key_digest, self.handoff)

    def test_contract_cannot_authorize_m1n1_or_an_extra_path(self):
        self.document["writes"][0]["path"] = "/m1n1/boot.bin"
        contract, signature = self._signed()
        with self.assertRaisesRegex(HandoffError, "unsupported ESP write"):
            verify_boot_file_contract(contract, signature, self.public_key.read_bytes(),
                                      self.key_digest, self.handoff)

    def test_contract_signature_and_canonical_bytes_are_both_required(self):
        contract, signature = self._signed()
        changed = contract.replace(b'"schema_version":1', b'"schema_version":2')
        with self.assertRaisesRegex(HandoffError, "signature"):
            verify_boot_file_contract(changed, signature, self.public_key.read_bytes(),
                                      self.key_digest, self.handoff)
        pretty = json.dumps(self.document, indent=2, sort_keys=True).encode()
        pretty_signature = self._sign(pretty)
        with self.assertRaisesRegex(HandoffError, "canonical"):
            verify_boot_file_contract(pretty, pretty_signature, self.public_key.read_bytes(),
                                      self.key_digest, self.handoff)

    def test_verifies_deterministic_swift_contract_fixture(self):
        contract = (
            b'{"boot_backend":"asahi-grub","installation_id":"12345678-1234-4234-9234-123456789abc",'
            b'"paired_esp_partition_uuid":"11111111-2222-4333-8444-555555555555",'
            b'"plan_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"preserve":["/m1n1/boot.bin"],"schema_version":1,"writes":[{'
            b'"operation":"atomic-replace","owner":"asahi-update-grub",'
            b'"path":"/EFI/BOOT/BOOTAA64.EFI",'
            b'"sha256":"1314307310f104df7f7bd3908fb40ebf71ef4493d882093a1c062b36804dcdb1"}]}'
        )
        signature = base64.b64decode(
            "YI9wK2sjFJu/twFWPjIgfzo55RrpYRlnXIe8xR2f1FznA9dp8CrJiPTOKksrqMYM"
            "vcTg21444GIIqzQmcU1cCg=="
        )
        public_key = base64.b64decode(
            "LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUNvd0JRWURLMlZ3QXlFQUE2RUh2"
            "L1BPRUw0ZGNOMFk1MHZBbVdmazFqQ2JwUTFmSGR5R1pCSlZNYmc9Ci0tLS0tRU5E"
            "IFBVQkxJQyBLRVktLS0tLQo="
        )

        verified = verify_boot_file_contract(
            contract,
            signature,
            public_key,
            "sha256:a050837d85070582ccf7394b0988847cc312cb88259b894899f6f239cf1791a5",
            self.handoff,
        )
        verified.authorize(GRUB_FALLBACK_PATH, self.content)

    def _signed(self):
        contract = json.dumps(self.document, separators=(",", ":"), sort_keys=True).encode()
        return contract, self._sign(contract)

    def _sign(self, contract):
        contract_path = self.root / "boot-contract.json"
        signature_path = self.root / "boot-contract.sig"
        contract_path.write_bytes(contract)
        subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", self.private_key,
                        "-rawin", "-in", contract_path, "-out", signature_path],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return signature_path.read_bytes()


if __name__ == "__main__":
    unittest.main()
