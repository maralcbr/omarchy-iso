from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "builder/asahi-lifecycle-lease.py"
LOCK_NAME = ".omarchy-lifecycle.lease"
RUN_RESERVATION_NAME = ".omarchy-run-reservation.json"


class AsahiLifecycleLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".asahi-lifecycle-lease-test-", dir=ROOT
        )
        self.root = Path(self.temporary.name)
        self.lease_root = self.root / "lease"
        self.lease_root.mkdir(mode=0o700)
        self.allowed_owners = sorted({0, os.geteuid()})

    def tearDown(self) -> None:
        if self.root.exists():
            paths = sorted(
                self.root.rglob("*"), key=lambda path: len(path.parts), reverse=True
            )
            for path in paths:
                if path.is_symlink():
                    continue
                try:
                    path.chmod(0o700 if path.is_dir() else 0o600)
                except FileNotFoundError:
                    pass
            self.root.chmod(0o700)
        self.temporary.cleanup()

    def helper_command(
        self,
        command: list[str],
        *,
        lease_root: Path | None = None,
        allowed_owners: list[int] | None = None,
    ) -> list[str]:
        arguments = [
            sys.executable,
            str(HELPER),
            "run",
            "--lease-root",
            str(lease_root or self.lease_root),
        ]
        for owner in allowed_owners or self.allowed_owners:
            arguments.extend(("--allowed-owner", str(owner)))
        return [*arguments, "--", *command]

    def run_helper(
        self,
        command: list[str],
        *,
        lease_root: Path | None = None,
        allowed_owners: list[int] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.helper_command(
                command,
                lease_root=lease_root,
                allowed_owners=allowed_owners,
            ),
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def run_lifecycle_operation(
        self, operation: str, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(HELPER), operation, *arguments]
        if operation in {
            "create-run-reservation",
            "admit-run-evidence",
            "verify-run-evidence",
        }:
            for owner in self.allowed_owners:
                command.extend(("--allowed-owner", str(owner)))
        return subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def create_run_reservation(
        self, run_id: str = "run-123", *, name: str | None = None
    ) -> Path:
        reservation = self.root / (name or f"{run_id}.reservation.json")
        result = self.run_lifecycle_operation(
            "create-run-reservation",
            "--run-id",
            run_id,
            "--output",
            str(reservation),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return reservation

    def admit_run_evidence(
        self,
        evidence_root: Path,
        reservation: Path,
        *,
        operation: str = "admit-run-evidence",
        run_id: str = "run-123",
    ) -> subprocess.CompletedProcess[str]:
        return self.run_lifecycle_operation(
            operation,
            "--run-id",
            run_id,
            "--reservation",
            str(reservation),
            "--evidence-root",
            str(evidence_root),
        )

    @staticmethod
    def marker_command(marker: Path, content: str = "ran") -> list[str]:
        return [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])",
            str(marker),
            content,
        ]

    def wait_for_path(self, path: Path, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {path}")

    def test_successful_child_runs_under_the_lease(self) -> None:
        marker = self.root / "successful-child"

        result = self.run_helper(self.marker_command(marker, "complete"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text(), "complete")
        self.assertTrue((self.lease_root / LOCK_NAME).is_file())

    def test_child_can_validate_the_exact_inherited_locked_descriptor(self) -> None:
        validation_command = [
            sys.executable,
            "-c",
            (
                "import os, subprocess, sys; "
                "command = [sys.executable, sys.argv[1], 'validate-held', "
                "'--lease-root', os.environ['OMARCHY_ASAHI_LIFECYCLE_LEASE_ROOT'], "
                "'--lease-fd', os.environ['OMARCHY_ASAHI_LIFECYCLE_LEASE_FD']]; "
                "[command.extend(('--allowed-owner', owner)) for owner in sys.argv[2:]]; "
                "fd = int(os.environ['OMARCHY_ASAHI_LIFECYCLE_LEASE_FD']); "
                "raise SystemExit(subprocess.run(command, check=False, "
                "pass_fds=(fd,)).returncode)"
            ),
            str(HELPER),
            *(str(owner) for owner in self.allowed_owners),
        ]

        result = self.run_helper(validation_command)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_forged_closed_descriptor_is_rejected(self) -> None:
        created = self.run_helper([sys.executable, "-c", "pass"])
        self.assertEqual(created.returncode, 0, created.stderr)
        command = [
            sys.executable,
            str(HELPER),
            "validate-held",
            "--lease-root",
            str(self.lease_root),
            "--lease-fd",
            "9999",
        ]
        for owner in self.allowed_owners:
            command.extend(("--allowed-owner", str(owner)))

        forged = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertNotEqual(forged.returncode, 0)
        self.assertIn("Bad file descriptor", forged.stderr)

    def test_concurrent_run_fails_before_its_child_executes(self) -> None:
        started = self.root / "first-started"
        release = self.root / "release-first"
        second_marker = self.root / "second-ran"
        first_command = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import sys, time; "
                "Path(sys.argv[1]).write_text('started'); "
                "release = Path(sys.argv[2]); "
                "exec(\"while not release.exists():\\n time.sleep(0.01)\")"
            ),
            str(started),
            str(release),
        ]
        first = subprocess.Popen(
            self.helper_command(first_command),
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.wait_for_path(started)

            second = self.run_helper(self.marker_command(second_marker))

            self.assertNotEqual(second.returncode, 0)
            self.assertIn("already held", second.stderr)
            self.assertFalse(second_marker.exists())
        finally:
            release.write_text("release")
            stdout, stderr = first.communicate(timeout=5)
            if first.returncode != 0:
                self.fail(
                    f"first lease holder failed with {first.returncode}: {stdout}{stderr}"
                )

    def test_lease_is_released_after_child_exit(self) -> None:
        first_marker = self.root / "first"
        second_marker = self.root / "second"

        first = self.run_helper(self.marker_command(first_marker))
        second = self.run_helper(self.marker_command(second_marker))

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(first_marker.exists())
        self.assertTrue(second_marker.exists())

    def test_successful_parent_with_background_descendant_fails_and_cleans_group(self) -> None:
        descendant_pid = self.root / "background-descendant.pid"
        parent = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import subprocess, sys, time; "
                "marker = Path(sys.argv[1]); "
                "subprocess.Popen([sys.executable, '-c', "
                "'from pathlib import Path; import os, sys, time; "
                "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)', "
                "str(marker)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL, close_fds=True); "
                "deadline = time.monotonic() + 3; "
                "exec(\"while not marker.exists() and time.monotonic() < deadline:\\n "
                "time.sleep(0.01)\"); "
                "raise SystemExit(0 if marker.exists() else 9)"
            ),
            str(descendant_pid),
        ]

        result = self.run_helper(parent)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("background descendants", result.stderr)
        pid = int(descendant_pid.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail(f"background descendant still alive after cleanup: {pid}")

    def test_symlinked_root_ancestor_or_lock_file_is_rejected(self) -> None:
        real_parent = self.root / "real-parent"
        real_lease = real_parent / "lease"
        real_lease.mkdir(parents=True, mode=0o700)
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        linked_root = self.root / "linked-root"
        linked_root.symlink_to(real_lease, target_is_directory=True)
        attacker = self.root / "attacker-lock"
        attacker.write_text("attacker")
        attacker.chmod(0o600)
        (self.lease_root / LOCK_NAME).symlink_to(attacker)

        cases = (
            linked_parent / "lease",
            linked_root,
            self.lease_root,
        )
        for index, lease_root in enumerate(cases):
            with self.subTest(lease_root=lease_root):
                marker = self.root / f"symlink-child-{index}"
                result = self.run_helper(
                    self.marker_command(marker), lease_root=lease_root
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists())

    def test_group_or_world_writable_ancestor_root_or_lock_is_rejected(self) -> None:
        mode_parent = self.root / "mode-parent"
        mode_lease = mode_parent / "lease"
        mode_lease.mkdir(parents=True, mode=0o700)
        cases = (
            (mode_parent, mode_lease, 0o770),
            (mode_lease, mode_lease, 0o707),
        )
        for index, (changed_path, lease_root, unsafe_mode) in enumerate(cases):
            with self.subTest(path=changed_path, mode=oct(unsafe_mode)):
                changed_path.chmod(unsafe_mode)
                marker = self.root / f"mode-child-{index}"
                try:
                    result = self.run_helper(
                        self.marker_command(marker), lease_root=lease_root
                    )
                finally:
                    changed_path.chmod(0o700)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("group/world writable", result.stderr)
                self.assertFalse(marker.exists())

        lock = self.lease_root / LOCK_NAME
        lock.write_text("unsafe")
        lock.chmod(0o660)
        marker = self.root / "unsafe-lock-child"
        result = self.run_helper(self.marker_command(marker))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("group/world writable", result.stderr)
        self.assertFalse(marker.exists())

    def test_owner_outside_the_allowed_set_is_rejected(self) -> None:
        marker = self.root / "owner-child"

        result = self.run_helper(
            self.marker_command(marker), allowed_owners=[os.geteuid() + 10000]
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("untrusted owner", result.stderr)
        self.assertFalse(marker.exists())

    def test_hardlinked_lock_file_is_rejected(self) -> None:
        outside = self.root / "outside-lock"
        outside.write_text("shared inode")
        outside.chmod(0o600)
        os.link(outside, self.lease_root / LOCK_NAME)
        marker = self.root / "hardlink-child"

        result = self.run_helper(self.marker_command(marker))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private regular file", result.stderr)
        self.assertFalse(marker.exists())

    def test_non_directory_ancestor_is_rejected(self) -> None:
        regular = self.root / "regular-file"
        regular.write_text("not a directory")
        marker = self.root / "non-directory-child"

        result = self.run_helper(
            self.marker_command(marker), lease_root=regular / "lease"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_descriptor_safe_creation_never_follows_a_symlinked_ancestor(self) -> None:
        real_parent = self.root / "real-create-parent"
        real_parent.mkdir(mode=0o700)
        linked_parent = self.root / "linked-create-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        escaped_leaf = real_parent / "must-not-exist"

        rejected = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "ensure-directory",
                "--path",
                str(linked_parent / "must-not-exist"),
                "--allowed-owner",
                "0",
                "--allowed-owner",
                str(os.geteuid()),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse(escaped_leaf.exists())

        safe_leaf = self.root / "safe-create-parent" / "created"
        accepted = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "ensure-directory",
                "--path",
                str(safe_leaf),
                "--allowed-owner",
                "0",
                "--allowed-owner",
                str(os.geteuid()),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertTrue(safe_leaf.is_dir())

    def test_child_exit_and_signal_results_are_preserved(self) -> None:
        exited = self.run_helper([sys.executable, "-c", "raise SystemExit(23)"])
        signalled = self.run_helper(
            [
                sys.executable,
                "-c",
                "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
            ]
        )

        self.assertEqual(exited.returncode, 23, exited.stderr)
        self.assertEqual(signalled.returncode, 128 + signal.SIGTERM, signalled.stderr)

    def test_run_reservation_binds_early_and_later_evidence_phases(self) -> None:
        run_id = "run-123"
        reservation = self.create_run_reservation(run_id)
        evidence_root = self.root / "output/build-evidence" / run_id
        evidence_root.parent.parent.mkdir(mode=0o700)
        record = json.loads(reservation.read_text())

        self.assertEqual(
            set(record), {"schema_version", "kind", "run_id", "nonce"}
        )
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["kind"], "asahi-build-run-reservation-v1")
        self.assertEqual(record["run_id"], run_id)
        self.assertRegex(record["nonce"], r"^[0-9a-f]{64}$")
        self.assertEqual(reservation.stat().st_mode & 0o777, 0o400)

        early = self.admit_run_evidence(evidence_root, reservation, run_id=run_id)
        self.assertEqual(early.returncode, 0, early.stderr)
        (evidence_root / "verified-package-cache.json").write_text("{}\n")

        later = self.admit_run_evidence(evidence_root, reservation, run_id=run_id)
        verified = self.admit_run_evidence(
            evidence_root,
            reservation,
            operation="verify-run-evidence",
            run_id=run_id,
        )
        self.assertEqual(later.returncode, 0, later.stderr)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(
            (evidence_root / RUN_RESERVATION_NAME).read_bytes(),
            reservation.read_bytes(),
        )
        self.assertEqual(
            (evidence_root / "verified-package-cache.json").read_text(), "{}\n"
        )

    def test_stale_or_different_run_evidence_is_rejected_without_mutation(self) -> None:
        run_id = "run-123"
        reservation = self.create_run_reservation(run_id)
        evidence_root = self.root / "output/build-evidence" / run_id
        evidence_root.mkdir(parents=True, mode=0o700)
        stale = evidence_root / "build-report.json"
        stale.write_text('{"catalog_eligible":true}\n')

        missing_marker = self.admit_run_evidence(
            evidence_root, reservation, run_id=run_id
        )
        self.assertNotEqual(missing_marker.returncode, 0)
        self.assertIn("reservation marker is missing", missing_marker.stderr)
        self.assertEqual(stale.read_text(), '{"catalog_eligible":true}\n')

        stale.unlink()
        evidence_root.rmdir()
        admitted = self.admit_run_evidence(evidence_root, reservation, run_id=run_id)
        self.assertEqual(admitted.returncode, 0, admitted.stderr)
        early = evidence_root / "verified-package-cache.json"
        early.write_text("early\n")
        other_reservation = self.create_run_reservation(
            run_id, name="other.reservation.json"
        )
        different = self.admit_run_evidence(
            evidence_root, other_reservation, run_id=run_id
        )
        self.assertNotEqual(different.returncode, 0)
        self.assertIn("does not match", different.stderr)
        self.assertEqual(early.read_text(), "early\n")

    def test_run_evidence_rejects_symlinks_writable_markers_and_missing_verify(self) -> None:
        run_id = "run-123"
        reservation = self.create_run_reservation(run_id)
        evidence_root = self.root / "output/build-evidence" / run_id

        missing = self.admit_run_evidence(
            evidence_root,
            reservation,
            operation="verify-run-evidence",
            run_id=run_id,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertFalse(evidence_root.exists())

        outside = self.root / "outside-evidence"
        outside.mkdir(mode=0o700)
        evidence_root.parent.mkdir(parents=True, mode=0o700)
        evidence_root.symlink_to(outside, target_is_directory=True)
        linked = self.admit_run_evidence(evidence_root, reservation, run_id=run_id)
        self.assertNotEqual(linked.returncode, 0)
        self.assertFalse((outside / RUN_RESERVATION_NAME).exists())

        evidence_root.unlink()
        admitted = self.admit_run_evidence(evidence_root, reservation, run_id=run_id)
        self.assertEqual(admitted.returncode, 0, admitted.stderr)
        marker = evidence_root / RUN_RESERVATION_NAME
        marker.chmod(0o600)
        writable = self.admit_run_evidence(evidence_root, reservation, run_id=run_id)
        self.assertNotEqual(writable.returncode, 0)
        self.assertIn("reservation marker is writable", writable.stderr)


if __name__ == "__main__":
    unittest.main()
