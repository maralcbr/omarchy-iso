#!/usr/bin/env python3
"""Admission receipts: diagnostic evidence and the qualification seam.

An admission receipt records that a checkpoint was admitted, and under which
authority. There are two kinds and they are deliberately not the same document:

  - A diagnostic receipt is evidence. It records what was observed during a
    diagnostic run and authorizes nothing. Anyone can issue one.
  - A qualification receipt authorizes reuse. It can only exist if an external
    signing authority vouches for it against a trust root.

The trust root does not exist yet. It is an open owner decision covering the
signing trust root and key custody. Until it is decided, qualification receipts
cannot be verified at all: the verifier seam has exactly one implementation and
that implementation refuses everything, by name. This is not a flag that can be
flipped -- there is no code path that accepts a qualification receipt.

STRUCTURAL NON-PROMOTABILITY
----------------------------
A diagnostic receipt cannot be edited into a qualification receipt. The two
kinds differ along three independent axes, and the qualification validator
checks all three:

  1. verification_kind  -- distinct literals, neither is a prefix or variant
  2. authorization_scope -- distinct literals naming what each may authorize
  3. field set          -- exact and disjoint required keys; a qualification
                           receipt requires an artifact set identity and a
                           signature block that a diagnostic receipt is
                           forbidden to carry at all

Mutating a diagnostic receipt one field at a time toward qualification shape
fails at every intermediate step, and the fully-shaped result still fails
because no trust root can verify its signature.

THREE IDENTITIES
----------------
Every receipt binds all three identities the checkpoint model distinguishes:
the producer identity (what built the artifact), the admission policy identity
(which policy admitted it), and the validation-receipt identity (the exact
verifier implementation that issued or checked this document, recorded as a
digest of its own source).

BYTE-LEVEL CHOICES
------------------
Digests use compact separators, sorted keys, and ASCII escaping, matching the
convention already used for toolchain metadata digests; this is the form whose
bytes downstream identities are computed over, so it must not drift. Receipt
files on disk are written with two-space indentation and sorted keys for
readability -- the file bytes are never themselves an identity input, only the
canonical form is.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any


SCHEMA_VERSION = 1

DIAGNOSTIC_KIND = "asahi-diagnostic-admission-receipt"
QUALIFICATION_KIND = "asahi-qualification-admission-receipt"

DIAGNOSTIC_SCOPE = "diagnostic-evidence-only-authorizes-no-reuse"
QUALIFICATION_SCOPE = "qualification-admission-authorizes-checkpoint-reuse"

DIAGNOSTIC_MODE = "diagnostic"
QUALIFICATION_MODE = "qualification"

SHA256 = re.compile(r"^[0-9a-f]{64}$")
STAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Identity fields every receipt binds, whatever its kind.
_SHARED_IDENTITY_FIELDS = (
    "stage",
    "checkpoint_identity",
    "input_digest",
    "producer_binding_identity",
    "admission_policy_identity",
    "verifier_identity",
)

DIAGNOSTIC_FIELDS = frozenset(
    {
        "schema_version",
        "verification_kind",
        "authorization_scope",
        "mode",
        "issued_at",
        *_SHARED_IDENTITY_FIELDS,
    }
)

# A qualification receipt additionally binds the artifact set it authorizes and
# carries the external signature. Both are forbidden on a diagnostic receipt.
QUALIFICATION_ONLY_FIELDS = frozenset({"artifact_set_identity", "signature"})
QUALIFICATION_FIELDS = DIAGNOSTIC_FIELDS | QUALIFICATION_ONLY_FIELDS

SIGNATURE_FIELDS = frozenset(
    {"trust_root_identity", "algorithm", "value", "signed_payload_sha256"}
)

RECEIPT_ROOT_ENVIRONMENT = "OMARCHY_ASAHI_ADMISSION_RECEIPT_ROOT"
DEFAULT_RECEIPT_ROOT = Path.home() / ".cache/omarchy/asahi-admission-receipts"

_LEASE_MODULE_FILENAME = "asahi-lifecycle-lease.py"


class AdmissionReceiptError(Exception):
    """A receipt, or the namespace holding it, is not valid."""


# --------------------------------------------------------------------------
# canonical forms


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _file_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def implementation_digest() -> str:
    """Digest of this implementation's own source."""
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# the signing-authority seam


class SignatureVerifier:
    """Interface for the external authority that vouches for qualification.

    Implementations are supplied once the trust root and key custody decision
    is made. The interface is fixed now so that decision plugs in without any
    schema change: an implementation provides a stable name and a verify()
    that raises on refusal.
    """

    name = "abstract"

    def verify(self, signature: dict[str, Any], payload: bytes) -> None:
        raise NotImplementedError

    def identity(self) -> str:
        return digest(
            {"verifier": self.name, "implementation_sha256": implementation_digest()}
        )


class UnconfiguredTrustRootVerifier(SignatureVerifier):
    """The only implementation that exists. It refuses everything.

    Named for the authority it is missing. While this is the configured
    verifier, no qualification receipt can be verified by any code path -- the
    refusal is the implementation, not a check that could be skipped.
    """

    name = "unconfigured-trust-root-pending-owner-decision"

    def verify(self, signature: dict[str, Any], payload: bytes) -> None:
        raise AdmissionReceiptError(
            "qualification admission requires a signing trust root that is not "
            "configured; the trust root and key custody decision is still open"
        )


def default_verifier() -> SignatureVerifier:
    return UnconfiguredTrustRootVerifier()


# --------------------------------------------------------------------------
# validation


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AdmissionReceiptError(message)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _validate_shared_identities(receipt: dict[str, Any], role: str) -> None:
    _require(
        isinstance(receipt.get("stage"), str)
        and STAGE_NAME.fullmatch(receipt["stage"]) is not None,
        f"{role} stage is invalid",
    )
    for field in (
        "checkpoint_identity",
        "input_digest",
        "producer_binding_identity",
        "admission_policy_identity",
        "verifier_identity",
    ):
        _require(_is_sha256(receipt.get(field)), f"{role} {field} is invalid")
    _require(
        isinstance(receipt.get("issued_at"), str)
        and TIMESTAMP.fullmatch(receipt["issued_at"]) is not None,
        f"{role} issued_at is invalid",
    )
    _require(receipt.get("schema_version") == SCHEMA_VERSION, f"{role} schema_version is invalid")


def validate_diagnostic_receipt(receipt: Any) -> dict[str, Any]:
    """Validate a diagnostic receipt. Authorizes nothing by construction."""
    role = "diagnostic admission receipt"
    _require(isinstance(receipt, dict), f"{role} is not an object")
    _require(frozenset(receipt) == DIAGNOSTIC_FIELDS, f"{role} field set is invalid")
    _require(
        receipt.get("verification_kind") == DIAGNOSTIC_KIND,
        f"{role} verification_kind is invalid",
    )
    _require(
        receipt.get("authorization_scope") == DIAGNOSTIC_SCOPE,
        f"{role} authorization_scope is invalid",
    )
    _require(receipt.get("mode") == DIAGNOSTIC_MODE, f"{role} mode is invalid")
    _validate_shared_identities(receipt, role)
    return receipt


def validate_qualification_receipt(
    receipt: Any,
    *,
    verifier: SignatureVerifier | None = None,
) -> dict[str, Any]:
    """Validate a qualification receipt.

    Every axis is checked independently, so a document that is diagnostic in
    any respect is refused even if it has been edited to look qualification-
    shaped in the others. The signature is then handed to the configured
    verifier, which today refuses unconditionally.
    """
    role = "qualification admission receipt"
    verifier = verifier or default_verifier()

    _require(isinstance(receipt, dict), f"{role} is not an object")

    # Axis 1: kind.
    _require(
        receipt.get("verification_kind") == QUALIFICATION_KIND,
        f"{role} verification_kind is invalid",
    )
    # Axis 2: scope.
    _require(
        receipt.get("authorization_scope") == QUALIFICATION_SCOPE,
        f"{role} authorization_scope is invalid",
    )
    # Axis 3: shape.
    _require(frozenset(receipt) == QUALIFICATION_FIELDS, f"{role} field set is invalid")

    _require(receipt.get("mode") == QUALIFICATION_MODE, f"{role} mode is invalid")
    _validate_shared_identities(receipt, role)
    _require(
        _is_sha256(receipt.get("artifact_set_identity")),
        f"{role} artifact_set_identity is invalid",
    )

    signature = receipt.get("signature")
    _require(isinstance(signature, dict), f"{role} signature is invalid")
    _require(frozenset(signature) == SIGNATURE_FIELDS, f"{role} signature is invalid")
    _require(
        isinstance(signature.get("trust_root_identity"), str)
        and signature["trust_root_identity"],
        f"{role} signature is invalid",
    )
    _require(
        isinstance(signature.get("algorithm"), str) and signature["algorithm"],
        f"{role} signature is invalid",
    )
    _require(
        isinstance(signature.get("value"), str) and signature["value"],
        f"{role} signature is invalid",
    )
    payload = signed_payload(receipt)
    _require(
        signature.get("signed_payload_sha256")
        == hashlib.sha256(payload).hexdigest(),
        f"{role} signature is invalid",
    )

    # No trust root exists, so this always raises today.
    verifier.verify(signature, payload)
    return receipt


def signed_payload(receipt: dict[str, Any]) -> bytes:
    """The canonical bytes a signature covers: the receipt minus the signature."""
    return canonical_bytes(
        {key: value for key, value in receipt.items() if key != "signature"}
    )


def verify_identity_bindings(
    receipt: dict[str, Any],
    *,
    stage: str | None = None,
    checkpoint_identity: str | None = None,
    input_digest: str | None = None,
    producer_binding_identity: str | None = None,
    admission_policy_identity: str | None = None,
    artifact_set_identity: str | None = None,
) -> dict[str, Any]:
    """Fail closed unless every supplied identity matches the receipt."""
    expected = {
        "stage": stage,
        "checkpoint_identity": checkpoint_identity,
        "input_digest": input_digest,
        "producer_binding_identity": producer_binding_identity,
        "admission_policy_identity": admission_policy_identity,
        "artifact_set_identity": artifact_set_identity,
    }
    for field, value in expected.items():
        if value is None:
            continue
        _require(
            receipt.get(field) == value,
            f"admission receipt {field} does not match the expected identity",
        )
    return receipt


# --------------------------------------------------------------------------
# issuance


def issue_diagnostic_receipt(
    *,
    stage: str,
    checkpoint_identity: str,
    input_digest: str,
    producer_binding_identity: str,
    admission_policy_identity: str,
    issued_at: str,
    verifier: SignatureVerifier | None = None,
) -> dict[str, Any]:
    verifier = verifier or default_verifier()
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "verification_kind": DIAGNOSTIC_KIND,
        "authorization_scope": DIAGNOSTIC_SCOPE,
        "mode": DIAGNOSTIC_MODE,
        "stage": stage,
        "checkpoint_identity": checkpoint_identity,
        "input_digest": input_digest,
        "producer_binding_identity": producer_binding_identity,
        "admission_policy_identity": admission_policy_identity,
        "verifier_identity": verifier.identity(),
        "issued_at": issued_at,
    }
    return validate_diagnostic_receipt(receipt)


def issue_qualification_receipt(*_args: Any, **_keywords: Any) -> dict[str, Any]:
    """Always refuses. Qualification issuance needs an authority that is absent."""
    raise AdmissionReceiptError(
        "qualification admission receipts cannot be issued: a signing trust "
        "root and key custody decision is required and is still open"
    )


# --------------------------------------------------------------------------
# namespace


def _load_lease_module():
    path = Path(__file__).resolve().parent / _LEASE_MODULE_FILENAME
    specification = importlib.util.spec_from_file_location(
        "asahi_lifecycle_lease", path
    )
    if specification is None or specification.loader is None:
        raise AdmissionReceiptError("the lifecycle lease primitives are unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def allowed_owner_ids() -> frozenset[int]:
    """Owners permitted on the namespace and every ancestor above it.

    Matches the convention the rest of the lifecycle tooling uses: the
    superuser, because the directories above a user cache are root owned, plus
    the invoking user, who owns the namespace itself.
    """
    return frozenset({0, os.geteuid()})


def receipt_root(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    configured = os.environ.get(RECEIPT_ROOT_ENVIRONMENT)
    if configured:
        return Path(configured)
    return DEFAULT_RECEIPT_ROOT


def receipt_relative_path(receipt: dict[str, Any]) -> str:
    """Content-addressed location: stage / checkpoint identity / receipt digest."""
    return (
        f"{receipt['stage']}/{receipt['checkpoint_identity']}/{digest(receipt)}.json"
    )


def _require_owned_directory_descriptor(descriptor: int, role: str) -> None:
    metadata = os.fstat(descriptor)
    _require(stat.S_ISDIR(metadata.st_mode), f"{role} is not a real directory")
    _require(metadata.st_uid == os.geteuid(), f"{role} has an untrusted owner")
    _require(
        not stat.S_IMODE(metadata.st_mode) & 0o022,
        f"{role} is group/world writable",
    )


def _open_child_directory(parent: int, name: str, role: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    except OSError as error:
        raise AdmissionReceiptError(f"{role} could not be created") from error
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as error:
        raise AdmissionReceiptError(f"{role} could not be opened safely") from error
    try:
        _require_owned_directory_descriptor(descriptor, role)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def store_receipt(
    receipt: dict[str, Any],
    *,
    root: Path | None = None,
) -> str:
    """Write a receipt write-once into the namespace, guarded by the lease.

    The namespace is a host-owned root, separate from the checkpoint store, and
    stays writable while checkpoint objects remain immutable. Receipts are never
    written inside a checkpoint identity directory.

    Creation is atomic and refuses to overwrite: the payload is written to a
    temporary file and then linked into place, which fails if the destination
    already exists. A failed write leaves nothing behind.
    """
    lease = _load_lease_module()
    destination = receipt_root(root)
    relative = receipt_relative_path(receipt)
    stage_name, identity_name, filename = relative.split("/")

    try:
        root_descriptor, lease_descriptor = lease.acquire_lifecycle_lease(
            destination,
            allowed_owner_ids(),
            create_root=True,
        )
    except lease.LifecycleLeaseError as error:
        raise AdmissionReceiptError(f"receipt namespace is unusable: {error}") from error

    stage_descriptor: int | None = None
    identity_descriptor: int | None = None
    temporary = f".{filename}.{os.getpid()}.tmp"
    try:
        stage_descriptor = _open_child_directory(
            root_descriptor, stage_name, "receipt stage directory"
        )
        identity_descriptor = _open_child_directory(
            stage_descriptor, identity_name, "receipt identity directory"
        )
        payload = _file_bytes(receipt)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            handle = os.open(temporary, flags, 0o444, dir_fd=identity_descriptor)
        except OSError as error:
            raise AdmissionReceiptError("receipt could not be staged") from error
        try:
            written = 0
            while written < len(payload):
                written += os.write(handle, payload[written:])
            os.fsync(handle)
        except BaseException:
            os.close(handle)
            os.unlink(temporary, dir_fd=identity_descriptor)
            raise
        os.close(handle)
        try:
            os.link(
                temporary,
                filename,
                src_dir_fd=identity_descriptor,
                dst_dir_fd=identity_descriptor,
            )
        except FileExistsError as error:
            os.unlink(temporary, dir_fd=identity_descriptor)
            raise AdmissionReceiptError(
                "an admission receipt already exists at this identity"
            ) from error
        except OSError as error:
            os.unlink(temporary, dir_fd=identity_descriptor)
            if error.errno == errno.EEXIST:
                raise AdmissionReceiptError(
                    "an admission receipt already exists at this identity"
                ) from error
            raise AdmissionReceiptError("receipt could not be committed") from error
        os.unlink(temporary, dir_fd=identity_descriptor)
        return relative
    finally:
        for descriptor in (identity_descriptor, stage_descriptor):
            if descriptor is not None:
                os.close(descriptor)
        lease.release_lifecycle_lease(root_descriptor, lease_descriptor)


def load_receipt(path: Path) -> dict[str, Any]:
    resolved = Path(path)
    _require(resolved.is_file(), f"admission receipt is missing: {resolved}")
    _require(not resolved.is_symlink(), "admission receipt path is a symlink")
    try:
        value = json.loads(resolved.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AdmissionReceiptError(
            f"admission receipt is unreadable: {resolved}"
        ) from error
    _require(isinstance(value, dict), "admission receipt is not an object")
    return value


def validate_receipt(
    receipt: dict[str, Any],
    *,
    verifier: SignatureVerifier | None = None,
) -> dict[str, Any]:
    """Dispatch on the declared kind. An unknown kind is refused."""
    kind = receipt.get("verification_kind")
    if kind == DIAGNOSTIC_KIND:
        return validate_diagnostic_receipt(receipt)
    if kind == QUALIFICATION_KIND:
        return validate_qualification_receipt(receipt, verifier=verifier)
    raise AdmissionReceiptError("admission receipt verification_kind is unrecognized")


# --------------------------------------------------------------------------
# command line


def _command_issue_diagnostic(arguments: argparse.Namespace) -> int:
    receipt = issue_diagnostic_receipt(
        stage=arguments.stage,
        checkpoint_identity=arguments.checkpoint_identity,
        input_digest=arguments.input_digest,
        producer_binding_identity=arguments.producer_binding_identity,
        admission_policy_identity=arguments.admission_policy_identity,
        issued_at=arguments.issued_at,
    )
    relative = store_receipt(receipt, root=arguments.root)
    print(json.dumps({"result": "issued", "receipt": relative}, sort_keys=True))
    return 0


def _command_issue_qualification(_arguments: argparse.Namespace) -> int:
    issue_qualification_receipt()
    return 1  # unreachable; issuance always raises


def _command_verify(arguments: argparse.Namespace) -> int:
    receipt = load_receipt(arguments.receipt)
    validate_receipt(receipt)
    verify_identity_bindings(
        receipt,
        stage=arguments.expect_stage,
        checkpoint_identity=arguments.expect_checkpoint_identity,
        input_digest=arguments.expect_input_digest,
        producer_binding_identity=arguments.expect_producer_binding_identity,
        admission_policy_identity=arguments.expect_admission_policy_identity,
        artifact_set_identity=arguments.expect_artifact_set_identity,
    )
    print(
        json.dumps(
            {
                "result": "verified",
                "verification_kind": receipt["verification_kind"],
                "authorization_scope": receipt["authorization_scope"],
                "authorizes_reuse": False,
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Admission receipt issuance and verification")
    commands = parser.add_subparsers(dest="command", required=True)

    issuer = commands.add_parser("issue-diagnostic")
    issuer.add_argument("--stage", required=True)
    issuer.add_argument("--checkpoint-identity", required=True)
    issuer.add_argument("--input-digest", required=True)
    issuer.add_argument("--producer-binding-identity", required=True)
    issuer.add_argument("--admission-policy-identity", required=True)
    issuer.add_argument("--issued-at", required=True)
    issuer.add_argument("--root", type=Path)
    issuer.set_defaults(handler=_command_issue_diagnostic)

    refuser = commands.add_parser("issue-qualification")
    refuser.set_defaults(handler=_command_issue_qualification)

    verifier = commands.add_parser("verify")
    verifier.add_argument("--receipt", type=Path, required=True)
    verifier.add_argument("--expect-stage")
    verifier.add_argument("--expect-checkpoint-identity")
    verifier.add_argument("--expect-input-digest")
    verifier.add_argument("--expect-producer-binding-identity")
    verifier.add_argument("--expect-admission-policy-identity")
    verifier.add_argument("--expect-artifact-set-identity")
    verifier.set_defaults(handler=_command_verify)

    arguments = parser.parse_args(argv)
    try:
        return arguments.handler(arguments)
    except AdmissionReceiptError as error:
        print(json.dumps({"result": "refused", "reason": str(error)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
