"""One shared schema-2 builder-toolchain fixture family.

Added 2026-08-29 (plan Phase B). The same baseline manifest and the same
mutations are driven through three independent validator surfaces so their
accept/reject outcomes can be compared:

  a. Producer  -- verify_cached_manifest in
     builder/ensure-asahi-toolchain-image.sh, exercised by
     test/unit/asahi-schema2-producer-parity-test.sh with a fake docker on PATH.
  b. Planner   -- the schema-2 metadata path in builder/asahi_checkpoint_plan.py,
     exercised by test/unit/test_asahi_schema2_manifest_parity.py.
  c. Projection gate -- the jq projection and gate in bin/omarchy-iso-make,
     exercised by test/unit/test_asahi_schema2_manifest_parity.py.

This module is a helper, not a test module: it deliberately does not match the
runner's discovery globs (test/unit/*-test.sh, test/unit/test_*.py), so it is
not registered in test/parallel-safe.tests.

The mutation definitions live here so the shell and python surfaces cannot
drift apart. Each surface builds its own baseline -- the producer surface must
compute declared_inputs from the real script so the producer will accept it,
while the planner surface uses synthetic but structurally valid values -- and
then applies these shared mutations.

Outcomes are recorded by each surface as characterization. The surfaces
disagree, and this phase documents the disagreement rather than resolving it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from typing import Any, Callable


VALID = "valid-baseline"
UNKNOWN_FIELD = "unknown-extra-field"
CACHE_HIT = "manifest-cache-hit"
COMPAT_REASON = "tampered-compatibility-reason"
COMPAT_LOCK = "tampered-compatibility-lock"
IMAGE_ABSENT = "docker-image-absent"

FIXTURE_NAMES = [
    VALID,
    UNKNOWN_FIELD,
    CACHE_HIT,
    COMPAT_REASON,
    COMPAT_LOCK,
    IMAGE_ABSENT,
]


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def compatibility_block(*, source_lock: str, target_lock: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "reason": "stage-input-granularity-v1",
        "source_checkpoint_identity": "c" * 64,
        "source_lock_sha256": source_lock,
        "target_lock_sha256": target_lock,
    }


def _unknown_extra_field(manifest: dict[str, Any]) -> dict[str, Any]:
    # An undeclared top-level key. Neither a forgery nor a downgrade on its own;
    # it distinguishes surfaces that close over the key set from surfaces that
    # check only the fields they name.
    mutated = copy.deepcopy(manifest)
    mutated["unexpected_field"] = "schema-2-parity-fixture"
    return mutated


def _manifest_cache_hit(manifest: dict[str, Any]) -> dict[str, Any]:
    # A cached manifest claiming it was itself produced from a cache hit. Both
    # the producer and the planner treat a cache_hit manifest as unusable
    # provenance.
    mutated = copy.deepcopy(manifest)
    mutated["cache_hit"] = True
    return mutated


def _tampered_compatibility_reason(manifest: dict[str, Any]) -> dict[str, Any]:
    # A compatibility block whose reason is not the one rekeying is allowed to
    # assert.
    mutated = copy.deepcopy(manifest)
    block = dict(mutated["compatibility"])
    block["reason"] = "operator-asserted-equivalence"
    mutated["compatibility"] = block
    return mutated


def _tampered_compatibility_lock(manifest: dict[str, Any]) -> dict[str, Any]:
    # A compatibility block pointing at a source lock that is not the one the
    # legacy lock file actually hashes to.
    mutated = copy.deepcopy(manifest)
    block = dict(mutated["compatibility"])
    block["source_lock_sha256"] = "d" * 64
    mutated["compatibility"] = block
    return mutated


def _docker_image_absent(manifest: dict[str, Any]) -> dict[str, Any]:
    # The manifest is untouched. The mutation is in the world, not the
    # document: the image it names is not present in the container runtime.
    # Only a surface that inspects docker can see this.
    return copy.deepcopy(manifest)


MUTATIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    VALID: copy.deepcopy,
    UNKNOWN_FIELD: _unknown_extra_field,
    CACHE_HIT: _manifest_cache_hit,
    COMPAT_REASON: _tampered_compatibility_reason,
    COMPAT_LOCK: _tampered_compatibility_lock,
    IMAGE_ABSENT: _docker_image_absent,
}

# Fixtures whose mutation is environmental rather than documentary. A surface
# that never inspects the container runtime cannot distinguish these from the
# baseline, and that is the point of including them.
ENVIRONMENTAL_FIXTURES = {IMAGE_ABSENT}

# Fixtures that require the manifest to carry a compatibility block.
COMPATIBILITY_FIXTURES = {COMPAT_REASON, COMPAT_LOCK}


# Keys the producer's schema-2 run manifest carries. The projection gate in
# bin/omarchy-iso-make consumes this document, not the checkpoint manifest, so
# the fixture family is projected through this derivation before reaching that
# surface. Note the rename: the checkpoint manifest's declared_input_digest is
# the run manifest's input_digest.
RUN_RECORD_KEYS = (
    "schema_version",
    "stage",
    "mode",
    "checkpoint_identity",
    "validation",
    "completed_at",
    "elapsed_seconds",
    "cache_hit",
    "output",
)


def run_record_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Derive the schema-2 run manifest that accompanies a checkpoint manifest."""
    record = {
        key: copy.deepcopy(manifest[key])
        for key in RUN_RECORD_KEYS
        if key in manifest
    }
    record["input_digest"] = manifest["declared_input_digest"]
    if "compatibility" in manifest:
        record["compatibility"] = copy.deepcopy(manifest["compatibility"])
    # Carry any undeclared key through, so the projection surface is exercised
    # with the same anomaly the other two surfaces see.
    for key, value in manifest.items():
        if key == "unexpected_field":
            record[key] = copy.deepcopy(value)
    return record


def apply_mutation(name: str, manifest: dict[str, Any]) -> dict[str, Any]:
    try:
        mutate = MUTATIONS[name]
    except KeyError:
        raise SystemExit(f"unknown schema-2 fixture: {name}") from None
    return mutate(manifest)


def main(argv: list[str]) -> int:
    """Shell entry point: apply one mutation to a manifest on stdin."""
    if len(argv) != 2:
        print(f"usage: {argv[0]} <fixture-name>", file=sys.stderr)
        return 2
    manifest = json.load(sys.stdin)
    json.dump(apply_mutation(argv[1], manifest), sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
