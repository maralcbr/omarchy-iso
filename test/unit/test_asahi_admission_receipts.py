"""Admission receipts: schema, non-promotability, namespace safety, and the seam.

Added 2026-08-30 (plan Phase C2). Nothing consumes receipts yet: this phase
builds the slice strictly up to the signing-authority seam, and the planner's
execution blockers are untouched.

Everything here runs in a temporary receipt root. The real host namespace is
never located, created, or written.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "builder/asahi_admission_receipts.py"


def load_module():
    spec = importlib.util.spec_from_file_location("asahi_admission_receipts", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


receipts = load_module()

STAGE = "verified-package-cache"
CHECKPOINT_IDENTITY = "1" * 64
INPUT_DIGEST = "2" * 64
PRODUCER_BINDING = "3" * 64
ADMISSION_POLICY = "4" * 64
ARTIFACT_SET = "5" * 64
ISSUED_AT = "2026-08-30T00:00:00Z"


def diagnostic_receipt() -> dict:
    return receipts.issue_diagnostic_receipt(
        stage=STAGE,
        checkpoint_identity=CHECKPOINT_IDENTITY,
        input_digest=INPUT_DIGEST,
        producer_binding_identity=PRODUCER_BINDING,
        admission_policy_identity=ADMISSION_POLICY,
        issued_at=ISSUED_AT,
    )


def qualification_shaped_receipt() -> dict:
    """A fully qualification-shaped document, signature included.

    It is well formed in every structural respect. It still cannot be verified,
    because no trust root exists to verify its signature against.
    """
    receipt = dict(diagnostic_receipt())
    receipt["verification_kind"] = receipts.QUALIFICATION_KIND
    receipt["authorization_scope"] = receipts.QUALIFICATION_SCOPE
    receipt["mode"] = receipts.QUALIFICATION_MODE
    receipt["artifact_set_identity"] = ARTIFACT_SET
    payload = receipts.signed_payload(receipt)
    receipt["signature"] = {
        "trust_root_identity": "some-authority",
        "algorithm": "ed25519",
        "value": "not-a-real-signature",
        "signed_payload_sha256": receipts.hashlib.sha256(payload).hexdigest(),
    }
    return receipt


class SchemaTests(unittest.TestCase):
    def test_diagnostic_receipt_round_trips(self) -> None:
        receipt = diagnostic_receipt()

        self.assertEqual(receipt["verification_kind"], receipts.DIAGNOSTIC_KIND)
        self.assertEqual(receipt["authorization_scope"], receipts.DIAGNOSTIC_SCOPE)
        self.assertEqual(receipt["mode"], receipts.DIAGNOSTIC_MODE)
        self.assertEqual(frozenset(receipt), receipts.DIAGNOSTIC_FIELDS)
        restored = json.loads(json.dumps(receipt))
        self.assertEqual(receipts.validate_diagnostic_receipt(restored), restored)

    def test_receipt_binds_all_three_identities(self) -> None:
        receipt = diagnostic_receipt()

        self.assertEqual(receipt["producer_binding_identity"], PRODUCER_BINDING)
        self.assertEqual(receipt["admission_policy_identity"], ADMISSION_POLICY)
        # The validation-receipt identity is the verifier implementation's own
        # digest, so a change to this implementation changes every receipt.
        self.assertEqual(
            receipt["verifier_identity"],
            receipts.default_verifier().identity(),
        )
        self.assertRegex(receipt["verifier_identity"], r"^[0-9a-f]{64}$")

    def test_verifier_identity_tracks_the_implementation(self) -> None:
        expected = receipts.digest(
            {
                "verifier": receipts.default_verifier().name,
                "implementation_sha256": receipts.implementation_digest(),
            }
        )

        self.assertEqual(receipts.default_verifier().identity(), expected)

    def test_diagnostic_receipt_may_not_carry_qualification_fields(self) -> None:
        for field, value in (
            ("artifact_set_identity", ARTIFACT_SET),
            ("signature", {"trust_root_identity": "x"}),
        ):
            with self.subTest(field=field):
                receipt = dict(diagnostic_receipt())
                receipt[field] = value
                with self.assertRaises(receipts.AdmissionReceiptError):
                    receipts.validate_diagnostic_receipt(receipt)

    def test_malformed_identities_fail_closed(self) -> None:
        for field in (
            "checkpoint_identity",
            "input_digest",
            "producer_binding_identity",
            "admission_policy_identity",
            "verifier_identity",
        ):
            with self.subTest(field=field):
                receipt = dict(diagnostic_receipt())
                receipt[field] = "not-a-digest"
                with self.assertRaises(receipts.AdmissionReceiptError):
                    receipts.validate_diagnostic_receipt(receipt)

    def test_unknown_kind_is_refused(self) -> None:
        receipt = dict(diagnostic_receipt())
        receipt["verification_kind"] = "asahi-some-other-receipt"

        with self.assertRaises(receipts.AdmissionReceiptError):
            receipts.validate_receipt(receipt)


class NonPromotabilityTests(unittest.TestCase):
    """A diagnostic receipt cannot be edited into a qualification receipt."""

    def test_a_diagnostic_receipt_is_refused_as_qualification(self) -> None:
        with self.assertRaises(receipts.AdmissionReceiptError):
            receipts.validate_qualification_receipt(diagnostic_receipt())

    def test_every_axis_refuses_independently(self) -> None:
        # Each axis is checked on its own, so satisfying two of the three is
        # still refused. This is what makes non-promotability structural rather
        # than a single flag.
        base = qualification_shaped_receipt()

        axes = {
            "kind": lambda r: r.update(verification_kind=receipts.DIAGNOSTIC_KIND),
            "scope": lambda r: r.update(authorization_scope=receipts.DIAGNOSTIC_SCOPE),
            "shape": lambda r: r.pop("artifact_set_identity"),
        }
        for name, break_axis in axes.items():
            with self.subTest(axis=name):
                receipt = json.loads(json.dumps(base))
                break_axis(receipt)
                with self.assertRaises(receipts.AdmissionReceiptError):
                    receipts.validate_qualification_receipt(receipt)

    def test_field_by_field_promotion_fails_at_every_step(self) -> None:
        # Walk a diagnostic receipt one field at a time toward qualification
        # shape. Every intermediate document must be refused as qualification,
        # and once it stops being diagnostic-shaped it must be refused as
        # diagnostic too -- there is no state in between that anything accepts.
        receipt = dict(diagnostic_receipt())
        steps = [
            ("verification_kind", receipts.QUALIFICATION_KIND),
            ("authorization_scope", receipts.QUALIFICATION_SCOPE),
            ("mode", receipts.QUALIFICATION_MODE),
            ("artifact_set_identity", ARTIFACT_SET),
        ]
        for field, value in steps:
            receipt[field] = value
            with self.subTest(step=field):
                with self.assertRaises(receipts.AdmissionReceiptError):
                    receipts.validate_qualification_receipt(receipt)
                with self.assertRaises(receipts.AdmissionReceiptError):
                    receipts.validate_diagnostic_receipt(receipt)

        # Finally add a well-formed signature: structurally complete, and still
        # refused, because the refusal is the absence of an authority.
        payload = receipts.signed_payload(receipt)
        receipt["signature"] = {
            "trust_root_identity": "some-authority",
            "algorithm": "ed25519",
            "value": "not-a-real-signature",
            "signed_payload_sha256": receipts.hashlib.sha256(payload).hexdigest(),
        }
        with self.assertRaises(receipts.AdmissionReceiptError) as refusal:
            receipts.validate_qualification_receipt(receipt)
        self.assertIn("trust root", str(refusal.exception))


class TrustRootSeamTests(unittest.TestCase):
    def test_qualification_verification_is_impossible_without_a_trust_root(self) -> None:
        # Not "fails by default" -- there is exactly one verifier implementation
        # and it refuses unconditionally.
        with self.assertRaises(receipts.AdmissionReceiptError) as refusal:
            receipts.validate_qualification_receipt(qualification_shaped_receipt())

        self.assertIn("trust root", str(refusal.exception))
        self.assertIn("not configured", str(refusal.exception))

    def test_the_only_verifier_is_the_refusing_one(self) -> None:
        verifier = receipts.default_verifier()

        self.assertIsInstance(verifier, receipts.UnconfiguredTrustRootVerifier)
        self.assertIn("unconfigured-trust-root", verifier.name)
        with self.assertRaises(receipts.AdmissionReceiptError):
            verifier.verify({}, b"")

    def test_the_seam_accepts_a_future_verifier_without_schema_change(self) -> None:
        # Proves the interface is the plug point: a verifier supplied later
        # reaches the same document unchanged. This local stand-in exists only
        # inside this test; no accepting implementation ships.
        class StandInVerifier(receipts.SignatureVerifier):
            name = "test-only-stand-in"

            def verify(self, signature, payload):
                return None

        receipt = qualification_shaped_receipt()
        verified = receipts.validate_qualification_receipt(
            receipt, verifier=StandInVerifier()
        )

        self.assertEqual(verified["verification_kind"], receipts.QUALIFICATION_KIND)
        # The shipped default still refuses the very same document.
        with self.assertRaises(receipts.AdmissionReceiptError):
            receipts.validate_qualification_receipt(receipt)

    def test_qualification_issuance_is_refused_by_name(self) -> None:
        with self.assertRaises(receipts.AdmissionReceiptError) as refusal:
            receipts.issue_qualification_receipt()

        self.assertIn("trust root", str(refusal.exception))

    def test_signature_payload_excludes_the_signature_block(self) -> None:
        receipt = qualification_shaped_receipt()
        payload = json.loads(receipts.signed_payload(receipt))

        self.assertNotIn("signature", payload)
        self.assertEqual(payload["artifact_set_identity"], ARTIFACT_SET)

    def test_a_tampered_signed_payload_digest_is_refused(self) -> None:
        receipt = qualification_shaped_receipt()
        receipt["input_digest"] = "9" * 64  # payload changes, digest does not

        class StandInVerifier(receipts.SignatureVerifier):
            name = "test-only-stand-in"

            def verify(self, signature, payload):
                return None

        with self.assertRaises(receipts.AdmissionReceiptError):
            receipts.validate_qualification_receipt(
                receipt, verifier=StandInVerifier()
            )


class IdentityBindingTests(unittest.TestCase):
    def test_matching_identities_pass(self) -> None:
        receipt = diagnostic_receipt()

        self.assertEqual(
            receipts.verify_identity_bindings(
                receipt,
                stage=STAGE,
                checkpoint_identity=CHECKPOINT_IDENTITY,
                input_digest=INPUT_DIGEST,
                producer_binding_identity=PRODUCER_BINDING,
                admission_policy_identity=ADMISSION_POLICY,
            ),
            receipt,
        )

    def test_each_mismatched_identity_fails_closed(self) -> None:
        receipt = diagnostic_receipt()
        mismatches = {
            "stage": "base-images",
            "checkpoint_identity": "a" * 64,
            "input_digest": "b" * 64,
            "producer_binding_identity": "c" * 64,
            "admission_policy_identity": "d" * 64,
        }
        for field, wrong in mismatches.items():
            with self.subTest(field=field):
                with self.assertRaises(receipts.AdmissionReceiptError):
                    receipts.verify_identity_bindings(receipt, **{field: wrong})


class NamespaceTests(unittest.TestCase):
    def setUp(self) -> None:
        # The lease refuses symlinked ancestors, and the system temporary
        # directory sits under one on this platform, so the fixture lives beside
        # the tree like the other lease-backed tests. It is removed in cleanup.
        self.work = Path(
            tempfile.mkdtemp(prefix=".asahi-admission-receipts-test-", dir=ROOT)
        )
        self.addCleanup(self.remove_work)
        self.root = self.work / "receipts"

    def remove_work(self) -> None:
        for current, directories, _ in os.walk(self.work):
            for name in [current, *(os.path.join(current, d) for d in directories)]:
                try:
                    os.chmod(name, 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.work, ignore_errors=True)

    def test_receipt_is_stored_content_addressed_outside_any_checkpoint(self) -> None:
        receipt = diagnostic_receipt()

        relative = receipts.store_receipt(receipt, root=self.root)

        stage_name, identity_name, filename = relative.split("/")
        self.assertEqual(stage_name, STAGE)
        self.assertEqual(identity_name, CHECKPOINT_IDENTITY)
        self.assertEqual(filename, f"{receipts.digest(receipt)}.json")
        stored = self.root / relative
        self.assertTrue(stored.is_file())
        self.assertEqual(json.loads(stored.read_text()), receipt)

    def test_the_namespace_stays_writable_while_receipts_do_not_change(self) -> None:
        receipt = diagnostic_receipt()
        relative = receipts.store_receipt(receipt, root=self.root)

        # A second, different receipt still lands: the namespace is not frozen.
        other = receipts.issue_diagnostic_receipt(
            stage=STAGE,
            checkpoint_identity=CHECKPOINT_IDENTITY,
            input_digest=INPUT_DIGEST,
            producer_binding_identity=PRODUCER_BINDING,
            admission_policy_identity=ADMISSION_POLICY,
            issued_at="2026-08-30T01:00:00Z",
        )
        other_relative = receipts.store_receipt(other, root=self.root)

        self.assertNotEqual(relative, other_relative)
        self.assertTrue((self.root / relative).is_file())

    def test_rewriting_the_same_receipt_is_refused(self) -> None:
        receipt = diagnostic_receipt()
        receipts.store_receipt(receipt, root=self.root)

        with self.assertRaises(receipts.AdmissionReceiptError) as refusal:
            receipts.store_receipt(receipt, root=self.root)

        self.assertIn("already exists", str(refusal.exception))

    def test_a_symlinked_root_is_refused(self) -> None:
        real = self.work / "elsewhere"
        real.mkdir(mode=0o700)
        self.root.symlink_to(real)

        with self.assertRaises(receipts.AdmissionReceiptError):
            receipts.store_receipt(diagnostic_receipt(), root=self.root)

    def test_a_group_writable_ancestor_is_refused(self) -> None:
        self.root.mkdir(mode=0o700)
        self.work.chmod(0o777)
        try:
            with self.assertRaises(receipts.AdmissionReceiptError):
                receipts.store_receipt(diagnostic_receipt(), root=self.root)
        finally:
            self.work.chmod(0o755)

    def test_a_group_writable_stage_directory_is_refused(self) -> None:
        self.root.mkdir(mode=0o700)
        stage_directory = self.root / STAGE
        stage_directory.mkdir()
        stage_directory.chmod(0o777)  # mkdir's mode is masked by umask

        with self.assertRaises(receipts.AdmissionReceiptError) as refusal:
            receipts.store_receipt(diagnostic_receipt(), root=self.root)

        self.assertIn("writable", str(refusal.exception))

    def test_a_failed_write_leaves_nothing_behind(self) -> None:
        # Force the payload write to fail after the temporary file exists.
        receipt = diagnostic_receipt()
        original_write = os.write

        def failing_write(handle, data):
            raise OSError(errno.EIO, "injected failure")

        os.write = failing_write
        try:
            with self.assertRaises(OSError):
                receipts.store_receipt(receipt, root=self.root)
        finally:
            os.write = original_write

        identity_directory = self.root / STAGE / CHECKPOINT_IDENTITY
        leftovers = sorted(p.name for p in identity_directory.iterdir())
        self.assertEqual(leftovers, [])

    def test_a_second_writer_is_refused_while_the_lease_is_held(self) -> None:
        # The lease is exclusive and non-blocking, so a concurrent writer is
        # refused rather than racing. Hold it here and prove a store attempt in
        # another process cannot proceed.
        self.root.mkdir(mode=0o700, parents=True)
        lease = receipts._load_lease_module()
        root_descriptor, lease_descriptor = lease.acquire_lifecycle_lease(
            self.root, receipts.allowed_owner_ids(), create_root=False
        )
        try:
            done = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "issue-diagnostic",
                    "--stage",
                    STAGE,
                    "--checkpoint-identity",
                    CHECKPOINT_IDENTITY,
                    "--input-digest",
                    INPUT_DIGEST,
                    "--producer-binding-identity",
                    PRODUCER_BINDING,
                    "--admission-policy-identity",
                    ADMISSION_POLICY,
                    "--issued-at",
                    ISSUED_AT,
                    "--root",
                    str(self.root),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        finally:
            lease.release_lifecycle_lease(root_descriptor, lease_descriptor)

        self.assertEqual(done.returncode, 1)
        self.assertIn("already held", done.stderr)

    def test_the_lease_is_released_so_a_later_writer_succeeds(self) -> None:
        receipts.store_receipt(diagnostic_receipt(), root=self.root)
        later = receipts.issue_diagnostic_receipt(
            stage=STAGE,
            checkpoint_identity=CHECKPOINT_IDENTITY,
            input_digest=INPUT_DIGEST,
            producer_binding_identity=PRODUCER_BINDING,
            admission_policy_identity=ADMISSION_POLICY,
            issued_at="2026-08-30T02:00:00Z",
        )

        self.assertTrue(receipts.store_receipt(later, root=self.root))


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(
            tempfile.mkdtemp(prefix=".asahi-admission-receipts-cli-", dir=ROOT)
        )
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.root = self.work / "receipts"

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(MODULE_PATH), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )

    def issue(self) -> str:
        done = self.run_cli(
            "issue-diagnostic",
            "--stage", STAGE,
            "--checkpoint-identity", CHECKPOINT_IDENTITY,
            "--input-digest", INPUT_DIGEST,
            "--producer-binding-identity", PRODUCER_BINDING,
            "--admission-policy-identity", ADMISSION_POLICY,
            "--issued-at", ISSUED_AT,
            "--root", str(self.root),
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)["receipt"]

    def test_issue_diagnostic_then_verify(self) -> None:
        relative = self.issue()

        done = self.run_cli(
            "verify",
            "--receipt", str(self.root / relative),
            "--expect-stage", STAGE,
            "--expect-checkpoint-identity", CHECKPOINT_IDENTITY,
        )

        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual(result["result"], "verified")
        self.assertEqual(result["verification_kind"], receipts.DIAGNOSTIC_KIND)
        # Verifying a diagnostic receipt never authorizes reuse.
        self.assertFalse(result["authorizes_reuse"])

    def test_verify_refuses_a_mismatched_identity(self) -> None:
        relative = self.issue()

        done = self.run_cli(
            "verify",
            "--receipt", str(self.root / relative),
            "--expect-checkpoint-identity", "f" * 64,
        )

        self.assertEqual(done.returncode, 1)
        self.assertIn("does not match", done.stderr)

    def test_issue_qualification_is_refused_naming_the_missing_authority(self) -> None:
        done = self.run_cli("issue-qualification")

        self.assertEqual(done.returncode, 1)
        reason = json.loads(done.stderr)["reason"]
        self.assertIn("trust root", reason)
        self.assertIn("key custody", reason)

    def test_verify_refuses_a_qualification_receipt(self) -> None:
        path = self.work / "qualification.json"
        path.write_text(json.dumps(qualification_shaped_receipt(), indent=2))

        done = self.run_cli("verify", "--receipt", str(path))

        self.assertEqual(done.returncode, 1)
        self.assertIn("trust root", json.loads(done.stderr)["reason"])

    def test_verify_refuses_a_missing_receipt(self) -> None:
        done = self.run_cli("verify", "--receipt", str(self.work / "absent.json"))

        self.assertEqual(done.returncode, 1)
        self.assertIn("missing", json.loads(done.stderr)["reason"])


if __name__ == "__main__":
    unittest.main()
