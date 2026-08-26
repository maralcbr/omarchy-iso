"""Verify the signed, path-scoped paired-ESP write authorization."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from .apple_handoff import HandoffError, VerifiedHandoff, _verify_signature


MAX_CONTRACT_BYTES = 16_384
GRUB_FALLBACK_PATH = "/EFI/BOOT/BOOTAA64.EFI"
M1N1_BOOT_PATH = "/m1n1/boot.bin"
ROOT_KEYS = {
    "schema_version",
    "installation_id",
    "plan_digest",
    "paired_esp_partition_uuid",
    "boot_backend",
    "writes",
    "preserve",
}
WRITE_KEYS = {"path", "sha256", "owner", "operation"}


@dataclass(frozen=True)
class BootFileWrite:
    path: str
    sha256: str
    owner: str
    operation: str


@dataclass(frozen=True)
class VerifiedBootFileContract:
    installation_id: str
    plan_digest: str
    paired_esp_partition_uuid: str
    writes: tuple[BootFileWrite, ...]
    preserve: tuple[str, ...]
    contract_sha256: str

    def authorize(self, path: str, content: bytes) -> None:
        matches = [write for write in self.writes if write.path == path]
        if len(matches) != 1:
            raise HandoffError("paired ESP path is not authorized")
        if hashlib.sha256(content).hexdigest() != matches[0].sha256:
            raise HandoffError("paired ESP content does not match signed authorization")


def verify_boot_file_contract(
    contract: bytes,
    signature: bytes,
    public_key: bytes,
    trusted_key_sha256: str,
    handoff: VerifiedHandoff,
) -> VerifiedBootFileContract:
    if not contract or len(contract) > MAX_CONTRACT_BYTES:
        raise HandoffError("boot-file contract has invalid size")
    _verify_signature(contract, signature, public_key, trusted_key_sha256)
    try:
        document = json.loads(contract, object_pairs_hook=_reject_duplicates)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise HandoffError("invalid boot-file contract JSON") from error
    canonical = json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
    if canonical != contract:
        raise HandoffError("boot-file contract is not canonical JSON")
    if not isinstance(document, dict) or set(document) != ROOT_KEYS:
        raise HandoffError("unexpected boot-file contract fields")
    if document["schema_version"] != 1:
        raise HandoffError("unsupported boot-file contract schema")
    if document["installation_id"] != handoff.installation_id:
        raise HandoffError("boot-file contract installation does not match handoff")
    if document["plan_digest"] != handoff.plan_digest:
        raise HandoffError("boot-file contract plan does not match handoff")
    if document["paired_esp_partition_uuid"] != handoff.paired_esp.partition_uuid:
        raise HandoffError("boot-file contract paired ESP does not match handoff")
    if document["boot_backend"] != "asahi-grub":
        raise HandoffError("boot-file contract requires Asahi GRUB")
    if document["preserve"] != [M1N1_BOOT_PATH]:
        raise HandoffError("boot-file contract must preserve the Asahi m1n1 boot object")
    if not isinstance(document["writes"], list) or len(document["writes"]) != 1:
        raise HandoffError("boot-file contract must authorize exactly one ESP write")
    raw = document["writes"][0]
    if not isinstance(raw, dict) or set(raw) != WRITE_KEYS:
        raise HandoffError("unexpected boot-file write fields")
    if raw != {
        "path": GRUB_FALLBACK_PATH,
        "sha256": raw.get("sha256"),
        "owner": "asahi-update-grub",
        "operation": "atomic-replace",
    }:
        raise HandoffError("boot-file contract authorizes an unsupported ESP write")
    digest = raw["sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HandoffError("invalid authorized boot-file digest")
    write = BootFileWrite(**raw)
    return VerifiedBootFileContract(
        installation_id=handoff.installation_id,
        plan_digest=handoff.plan_digest,
        paired_esp_partition_uuid=handoff.paired_esp.partition_uuid,
        writes=(write,),
        preserve=(M1N1_BOOT_PATH,),
        contract_sha256=hashlib.sha256(contract).hexdigest(),
    )


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HandoffError(f"duplicate boot-file contract field: {key}")
        result[key] = value
    return result
