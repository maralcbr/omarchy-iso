from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "builder/pinned-node-cache.py"


def load_module():
    specification = importlib.util.spec_from_file_location(
        "pinned_node_cache", MODULE_PATH
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"could not load {MODULE_PATH}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class PinnedNodeCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".pinned-node-cache-test-", dir=ROOT
        )
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.destination = self.root / "destination"
        self.cache.mkdir(mode=0o700)
        self.destination.mkdir(mode=0o700)
        self.filename = "node-v26.8.1-linux-arm64.tar.gz"
        self.payload = (b"exact-pinned-node-payload\n" * 65536) + b"end"
        self.payload_sha256 = hashlib.sha256(self.payload).hexdigest()
        self.source = self.cache / self.filename
        self.source.write_bytes(self.payload)
        self.source.chmod(0o444)
        self.allowed_owners = {0, os.geteuid()}

    def tearDown(self) -> None:
        for path in self.root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
            elif not path.is_symlink():
                path.chmod(0o600)
        self.temporary.cleanup()

    def snapshot(self) -> Path:
        return self.module.snapshot_cached_payload(
            cache_root=self.cache,
            filename=self.filename,
            destination_root=self.destination,
            expected_sha256=self.payload_sha256,
            expected_size=len(self.payload),
            allowed_owner_ids=self.allowed_owners,
        )

    def test_exact_payload_is_copied_once_and_immediately_reverified(self) -> None:
        result = self.snapshot()

        self.assertEqual(result, self.destination / self.filename)
        self.assertEqual(result.read_bytes(), self.payload)
        self.assertEqual(result.stat().st_size, len(self.payload))
        self.assertEqual(hashlib.sha256(result.read_bytes()).hexdigest(), self.payload_sha256)
        self.assertEqual(result.stat().st_mode & 0o777, 0o444)

    def test_symlinked_cache_root_or_ancestor_is_rejected(self) -> None:
        real_cache = self.root / "real-cache"
        real_cache.mkdir(mode=0o700)
        (real_cache / self.filename).write_bytes(self.payload)
        (real_cache / self.filename).chmod(0o444)

        linked_cache = self.root / "linked-cache"
        linked_cache.symlink_to(real_cache, target_is_directory=True)
        with self.assertRaisesRegex(self.module.NodeCacheError, "symlink|unsafe"):
            self.module.validate_cache_root(linked_cache, self.allowed_owners)

        real_parent = self.root / "real-parent"
        (real_parent / "cache").mkdir(parents=True, mode=0o700)
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaisesRegex(self.module.NodeCacheError, "symlink|unsafe"):
            self.module.validate_cache_root(
                linked_parent / "cache", self.allowed_owners
            )

    def test_group_or_world_writable_ancestor_cache_or_file_is_rejected(self) -> None:
        cases = (
            (self.root, 0o775, self.cache),
            (self.cache, 0o707, self.cache),
            (self.source, 0o466, self.cache),
        )
        for changed_path, mode, cache_root in cases:
            with self.subTest(path=changed_path.name, mode=oct(mode)):
                original_mode = changed_path.stat().st_mode & 0o777
                changed_path.chmod(mode)
                try:
                    with self.assertRaisesRegex(
                        self.module.NodeCacheError, "group/world writable"
                    ):
                        self.module.snapshot_cached_payload(
                            cache_root=cache_root,
                            filename=self.filename,
                            destination_root=self.destination,
                            expected_sha256=self.payload_sha256,
                            expected_size=len(self.payload),
                            allowed_owner_ids=self.allowed_owners,
                        )
                finally:
                    changed_path.chmod(original_mode)

    def test_symlinked_cache_file_is_rejected(self) -> None:
        attacker = self.root / "attacker.tar.gz"
        attacker.write_bytes(self.payload)
        attacker.chmod(0o444)
        self.source.unlink()
        self.source.symlink_to(attacker)

        with self.assertRaisesRegex(self.module.NodeCacheError, "symlink|unsafe"):
            self.snapshot()

    def test_cache_owned_outside_current_user_or_root_is_rejected(self) -> None:
        with self.assertRaisesRegex(self.module.NodeCacheError, "owner"):
            self.module.validate_cache_root(
                self.cache, {0, os.geteuid() + 1}
            )

    def test_path_swap_after_fd_open_fails_closed(self) -> None:
        original_read = os.read
        original_source = self.cache / f"{self.filename}.original"
        attacker = self.cache / f"{self.filename}.attacker"
        attacker.write_bytes(b"x" * len(self.payload))
        attacker.chmod(0o444)
        swapped = False

        def swap_then_read(descriptor: int, count: int) -> bytes:
            nonlocal swapped
            if not swapped:
                swapped = True
                self.source.rename(original_source)
                attacker.rename(self.source)
            return original_read(descriptor, count)

        with mock.patch.object(self.module.os, "read", side_effect=swap_then_read):
            with self.assertRaisesRegex(
                self.module.NodeCacheError, "changed while being read"
            ):
                self.snapshot()
        self.assertFalse((self.destination / self.filename).exists())

    def test_post_copy_destination_mutation_is_detected_and_removed(self) -> None:
        original_replace = os.replace

        def replace_then_mutate(*args, **kwargs):
            result = original_replace(*args, **kwargs)
            installed = self.destination / self.filename
            installed.chmod(0o600)
            installed.write_bytes(b"z" * len(self.payload))
            return result

        with mock.patch.object(
            self.module.os, "replace", side_effect=replace_then_mutate
        ):
            with self.assertRaisesRegex(
                self.module.NodeCacheError, "snapshot verification failed"
            ):
                self.snapshot()
        self.assertFalse((self.destination / self.filename).exists())

    def test_concurrent_publisher_output_is_never_removed_on_inode_mismatch(self) -> None:
        original_replace = os.replace
        contender = self.destination / "contender.tar.gz"
        contender.write_bytes(self.payload)
        contender.chmod(0o444)
        contender_inode = contender.stat().st_ino

        def replace_then_publish_contender(*args, **kwargs):
            result = original_replace(*args, **kwargs)
            original_replace(contender, self.destination / self.filename)
            return result

        with mock.patch.object(
            self.module.os, "replace", side_effect=replace_then_publish_contender
        ):
            with self.assertRaisesRegex(
                self.module.NodeCacheError, "snapshot verification failed"
            ):
                self.snapshot()

        installed = self.destination / self.filename
        self.assertTrue(installed.is_file())
        self.assertEqual(installed.stat().st_ino, contender_inode)
        self.assertEqual(installed.read_bytes(), self.payload)

    def test_mutating_cache_after_success_does_not_change_snapshot(self) -> None:
        installed = self.snapshot()
        self.source.chmod(0o600)
        self.source.write_bytes(b"m" * len(self.payload))

        self.assertEqual(installed.read_bytes(), self.payload)
        self.assertEqual(hashlib.sha256(installed.read_bytes()).hexdigest(), self.payload_sha256)


if __name__ == "__main__":
    unittest.main()
