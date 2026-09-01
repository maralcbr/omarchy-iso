"""Characterize what a stage's producer declaration digest actually binds.

Added 2026-08-29 (plan Phase B). `asahi_stage_inputs.py` replaced whole-file
spec binding with per-declaration digests: each stage's generated source
manifest carries `declaration_sha256` over its own producer declaration, so
editing one stage's declaration no longer invalidates every other stage.

These tests pin that coverage. Phase C1 adopted item 8(b) and folded
`dispatches` into the producer declaration, so widening a stage's suppression
list now invalidates checkpoints produced under the narrower declaration; the
test that expressed that as an intended property is now a plain passing test.

`admission_paths` remains consumed but unbound, which is items 8(a)/(c) and
still open.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "builder/asahi_stage_inputs.py"
SPEC_PATH = ROOT / "builder/asahi-stage-inputs.json"

# base-images carries the largest dispatches list in the specification, so it is
# the clearest witness for suppression-list coverage.
STAGE = "base-images"

PRODUCER_DECLARATION_KEYS = [
    "depends_on",
    "dispatches",
    "entrypoints",
    "lock_paths",
    "runtime_inputs",
    "runtime_settings",
    "source_paths",
]


def load_module():
    spec = importlib.util.spec_from_file_location("asahi_stage_inputs", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProducerDeclarationBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()
        cls.specification = json.loads(SPEC_PATH.read_text())

    def declaration(self) -> dict:
        return copy.deepcopy(self.specification["stages"][STAGE])

    def digest_of(self, declaration: dict) -> str:
        return self.module._digest(self.module._producer_declaration(declaration))

    def test_producer_declaration_covers_exactly_seven_keys(self) -> None:
        # Six until 2026-08-30; `dispatches` was added by Phase C1.
        projection = self.module._producer_declaration(self.declaration())

        self.assertEqual(sorted(projection), PRODUCER_DECLARATION_KEYS)

    def test_source_paths_are_identity_bound(self) -> None:
        # Control: the keys that are bound really do move the digest.
        mutated = self.declaration()
        mutated["source_paths"] = [
            *mutated["source_paths"],
            "builder/build-asahi-os-package.sh",
        ]

        self.assertNotEqual(self.digest_of(self.declaration()), self.digest_of(mutated))

    def test_dispatches_are_identity_bound(self) -> None:
        # `dispatches` is a suppression list: entries in it exempt an executed
        # path from the "executed input is omitted from <stage>" and
        # "cross-stage dispatch is undeclared" guards in the specification
        # validator. Adding an entry widens what a stage may execute unchecked,
        # so it must move the producer identity. Until 2026-08-30 it did not;
        # Phase C1 adopted item 8(b) and bound it.
        mutated = self.declaration()
        mutated["dispatches"] = [
            *mutated.get("dispatches", []),
            "builder/asahi-stages/newly-dispatched.sh",
        ]

        self.assertNotEqual(self.digest_of(self.declaration()), self.digest_of(mutated))

    def test_dispatches_are_bound_even_when_the_list_is_empty(self) -> None:
        # A stage that dispatches nothing still carries the key, so its identity
        # changes the moment it starts dispatching something.
        empty = self.declaration()
        empty["dispatches"] = []
        first = self.declaration()
        first["dispatches"] = ["builder/asahi-stages/newly-dispatched.sh"]

        self.assertIn("dispatches", self.module._producer_declaration(empty))
        self.assertNotEqual(self.digest_of(empty), self.digest_of(first))

    def test_admission_paths_are_not_identity_bound(self) -> None:
        # Characterization. Producer and admission inputs are required to be
        # disjoint, so declaring an executed path under admission_paths both
        # satisfies the executed-input guard and keeps it out of the producer
        # identity. Plan owner decision queue item 8.
        mutated = self.declaration()
        mutated["admission_paths"] = [
            *mutated["admission_paths"],
            "builder/asahi-cache-hit-policy.sh",
        ]

        self.assertEqual(self.digest_of(self.declaration()), self.digest_of(mutated))

    def test_consumed_suppression_lists_are_identity_bound(self) -> None:
        # Was an @unittest.expectedFailure until 2026-08-30, expressing the
        # intended property while `dispatches` was unbound. Phase C1 adopted
        # item 8(b) and bound it, so this now passes as written.
        declaration = self.declaration()
        mutated = copy.deepcopy(declaration)
        mutated["dispatches"] = [
            *mutated.get("dispatches", []),
            "builder/asahi-stages/newly-dispatched.sh",
        ]

        self.assertTrue(
            "dispatches" not in declaration
            or self.digest_of(declaration) != self.digest_of(mutated),
            "widening a stage's dispatches suppression list left its producer "
            "declaration digest unchanged",
        )


if __name__ == "__main__":
    unittest.main()
