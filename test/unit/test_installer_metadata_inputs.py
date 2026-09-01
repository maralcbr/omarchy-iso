"""Characterize the installer-metadata stage's undeclared-input gap.

Added 2026-08-29 (plan Phase B).

builder/asahi-stages/installer-metadata.sh binds its checkpoint identity to
exactly three inputs (release-package, product, package-verifier). The evidence
step that runs *after* restore then reads several more inputs and splices them
into the published evidence document: the finalized installed-contents, the
configured installed contract, the Apple platform snapshot (whose sha256 is
recorded in the evidence), and source identities taken from the environment.

None of those are identity-bound, so a restored checkpoint is reused on the
strength of three inputs while the artifact it produces depends on more. These
tests pin the gap as it stands today; the intended contract is expressed as an
expected failure below.

Assertions here are text-level against the stage script, which is enough to
fail if anyone changes the declared identity inputs or drops one of the
undeclared reads without revisiting this characterization.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
STAGE_PATH = ROOT / "builder/asahi-stages/installer-metadata.sh"

DECLARED_INPUT_NAMES = {"release-package", "product", "package-verifier"}

# Inputs the post-restore evidence step consumes but the identity does not
# bind. Each entry is a literal fragment of the stage script.
UNDECLARED_CONSUMED = {
    "finalized-installed-contents": '"$finalized_directory/installed-contents.json"',
    "configured-installed-contract": '"$configured_installed_contract"',
    "apple-platform-snapshot": "/builder/apple-platform-snapshot.json",
    "iso-source-identity": "OMARCHY_ISO_SOURCE_IDENTITY",
    "iso-source-commit": "OMARCHY_ISO_SOURCE_COMMIT",
    "archiso-source-commit": "OMARCHY_ARCHISO_SOURCE_COMMIT",
}


def stage_text() -> str:
    return STAGE_PATH.read_text()


def identity_declaration(text: str) -> str:
    """The create_stage_identity call, following backslash continuations."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "create_stage_identity installer-metadata" in line:
            block = [line]
            while block[-1].rstrip().endswith("\\") and index + 1 < len(lines):
                index += 1
                block.append(lines[index])
            return "\n".join(block)
    raise AssertionError(
        "installer-metadata no longer calls create_stage_identity; "
        "this characterization needs revisiting"
    )


class InstallerMetadataInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = stage_text()
        cls.declaration = identity_declaration(cls.text)

    def declared_input_names(self) -> set[str]:
        return set(re.findall(r"--input\s+([A-Za-z0-9-]+)=", self.declaration))

    def test_identity_binds_exactly_three_inputs(self) -> None:
        # Pins today's identity surface. Adding or removing an --input entry
        # fails here, which is the point: the gap below is measured against
        # this exact set.
        self.assertEqual(self.declared_input_names(), DECLARED_INPUT_NAMES)

    def test_declared_inputs_are_the_package_product_and_verifier(self) -> None:
        self.assertIn('--input release-package="$package"', self.declaration)
        self.assertIn('--input product="$product"', self.declaration)
        self.assertIn(
            "--input package-verifier=/builder/verify-asahi-os-package.py",
            self.declaration,
        )

    def test_evidence_step_consumes_inputs_the_identity_does_not_bind(self) -> None:
        # Characterization of the gap. Each of these is read after restore and
        # folded into the published evidence document, yet none appears in the
        # identity declaration.
        for name, fragment in UNDECLARED_CONSUMED.items():
            with self.subTest(input=name):
                self.assertIn(
                    fragment,
                    self.text,
                    f"{name} is no longer consumed; update this characterization",
                )
                self.assertNotIn(
                    fragment,
                    self.declaration,
                    f"{name} is now identity-bound; update this characterization",
                )

    def test_platform_snapshot_digest_reaches_the_published_evidence(self) -> None:
        # The strongest single instance: the snapshot's digest is recorded in
        # the evidence document, so the artifact's content depends on a file
        # that never entered the identity.
        self.assertIn("platform_snapshot_sha256", self.text)
        self.assertIn(
            "sha256sum /builder/apple-platform-snapshot.json",
            self.text,
        )
        self.assertNotIn("apple-platform-snapshot", self.declaration)

    def test_evidence_is_rewritten_after_restore(self) -> None:
        # Establishes that the undeclared reads happen on the restore path too,
        # not only when the stage is executed: the copy out of the restored
        # destination is followed unconditionally by the rewrite.
        self.assertIn('cp -- "$metadata_evidence"', self.text)
        self.assertIn('mv "$tmp_evidence"', self.text)
        restore_index = self.text.index("restore_stage installer-metadata")
        rewrite_index = self.text.index('mv "$tmp_evidence"')
        self.assertLess(restore_index, rewrite_index)

    @unittest.expectedFailure
    def test_every_consumed_input_is_identity_bound(self) -> None:
        # Intended contract, not current behaviour: an input that can change
        # the produced evidence must be part of the identity that decides
        # whether the evidence may be restored instead of produced.
        #
        # Phrased against the declaration text rather than any particular
        # mechanism, so it turns green once each consumed input is declared --
        # for example as an additional --input entry naming the finalized
        # contents, the configured contract, and the platform snapshot.
        missing = sorted(
            name
            for name, fragment in UNDECLARED_CONSUMED.items()
            if fragment not in self.declaration
        )

        self.assertEqual(
            missing,
            [],
            "installer-metadata consumes inputs its identity does not bind: "
            + ", ".join(missing),
        )


if __name__ == "__main__":
    unittest.main()
