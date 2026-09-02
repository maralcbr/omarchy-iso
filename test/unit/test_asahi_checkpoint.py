from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "builder/asahi_checkpoint.py"
PRUNE_SCRIPT = ROOT / "builder/prune-asahi-checkpoints.py"


def load_checkpoint_module():
    spec = importlib.util.spec_from_file_location("asahi_checkpoint", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AsahiCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_checkpoint_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.lock = self.root / "source-lock.json"
        self.lock.write_text('{"schema_version":1,"builder_image":"sha256:pinned"}\n')

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def identity(
        self,
        stage: str = "base-images",
        mode: str = "qualification",
        inputs: dict[str, Path] | None = None,
    ) -> dict:
        return self.module.build_identity(
            stage=stage,
            mode=mode,
            source_lock=self.lock,
            source_commits={
                "omarchy_iso": "a" * 40,
                "archiso": "b" * 40,
            },
            inputs=inputs or {},
        )

    def test_identity_is_canonical_and_changes_with_exact_input(self) -> None:
        payload = self.root / "payload"
        payload.write_bytes(b"first")

        first = self.identity(inputs={"payload": payload})
        repeat = self.identity(inputs={"payload": payload})
        self.assertEqual(first, repeat)
        self.assertRegex(first["checkpoint_identity"], r"^[0-9a-f]{64}$")
        self.assertEqual(first["source_lock"]["sha256"], self.module.sha256_file(self.lock))

        payload.write_bytes(b"second")
        changed = self.identity(inputs={"payload": payload})
        self.assertNotEqual(first["checkpoint_identity"], changed["checkpoint_identity"])
        self.assertNotEqual(first["input_digest"], changed["input_digest"])

    def test_identity_records_relative_paths_types_and_executable_modes(self) -> None:
        source = self.root / "source"
        source.mkdir()
        executable = source / "run.sh"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)

        first = self.identity(inputs={"source-tree": source})
        record = first["inputs"][0]
        self.assertEqual(record["path"], "source-tree")
        entry = next(item for item in record["entries"] if item["path"] == "run.sh")
        self.assertEqual(entry["kind"], "file")
        self.assertEqual(entry["executable_mode"], 0o111)

        executable.chmod(0o644)
        changed = self.identity(inputs={"source-tree": source})
        self.assertNotEqual(first["checkpoint_identity"], changed["checkpoint_identity"])
        changed_entry = changed["inputs"][0]["entries"][0]
        self.assertEqual(changed_entry["executable_mode"], 0)

    def test_source_manifest_captures_commit_dirty_and_untracked_inputs(self) -> None:
        repository = self.root / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", repository], check=True)
        subprocess.run(
            ["git", "-C", repository, "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repository, "config", "user.name", "Checkpoint Test"],
            check=True,
        )
        (repository / "builder").mkdir()
        tracked = repository / "builder/tracked.sh"
        tracked.write_text("#!/bin/sh\n")
        tracked.chmod(0o755)
        subprocess.run(["git", "-C", repository, "add", "builder/tracked.sh"], check=True)
        subprocess.run(["git", "-C", repository, "commit", "-qm", "base"], check=True)

        tracked.write_text("#!/bin/sh\necho dirty\n")
        untracked = repository / "builder/new.conf"
        untracked.write_text("untracked=true\n")

        manifest = self.module.build_source_manifest(repository, ["builder"])
        commit = subprocess.run(
            ["git", "-C", repository, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(manifest["commit"], commit)
        self.assertTrue(manifest["dirty"])
        self.assertEqual(
            manifest["commit_dirty_identity"],
            f"{commit}+dirty:{manifest['tree_digest']}",
        )
        entries = {entry["path"]: entry for entry in manifest["entries"]}
        self.assertEqual(entries["builder/tracked.sh"]["executable_mode"], 0o111)
        self.assertEqual(entries["builder/new.conf"]["kind"], "file")
        self.assertEqual(manifest["status"], [" M builder/tracked.sh", "?? builder/new.conf"])

    def test_store_recomputation_and_restore_cache_hit_manifests_are_exact(self) -> None:
        payload = self.root / "payload"
        payload.write_bytes(b"immutable payload")
        identity = self.identity(inputs={"payload": payload})
        output = self.root / "root.img"
        output.write_bytes(b"configured image")

        first_run = self.root / "first-run.json"
        first = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output},
            elapsed_seconds=12.5,
            run_manifest=first_run,
        )
        self.assertFalse(first["cache_hit"])
        manifest_path = Path(first["manifest_path"])
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["validation"]["result"], "passed")
        self.assertEqual(manifest["elapsed_seconds"], 12.5)
        self.assertFalse(manifest["cache_hit"])
        self.assertEqual(manifest["outputs"][0]["size_bytes"], len(b"configured image"))
        self.assertEqual(manifest["outputs"][0]["storage"]["kind"], "sha256-object")
        self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o444)
        object_path = (
            self.cache
            / "objects/sha256"
            / manifest["outputs"][0]["sha256"][:2]
            / manifest["outputs"][0]["sha256"]
        )
        self.assertTrue(object_path.is_file())
        self.assertFalse(object_path.is_symlink())
        self.assertEqual(stat.S_IMODE(object_path.stat().st_mode), 0o444)

        second_run = self.root / "second-run.json"
        second = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output},
            elapsed_seconds=0.1,
            run_manifest=second_run,
        )
        self.assertFalse(second["cache_hit"])
        self.assertTrue(second["reproducibility_match"])
        second_evidence = json.loads(second_run.read_text())
        self.assertFalse(second_evidence["cache_hit"])
        self.assertTrue(second_evidence["reproducibility_match"])

        restored = self.root / "restored.img"
        restore_run = self.root / "restore-run.json"
        result = self.module.restore_checkpoint(
            cache_root=self.cache,
            identity=identity,
            destinations={"root.img": restored},
            run_manifest=restore_run,
        )
        self.assertTrue(result["cache_hit"])
        self.assertNotIn("reproducibility_match", result)
        self.assertEqual(restored.read_bytes(), b"configured image")
        self.assertTrue(os.access(restored, os.W_OK))

    def test_store_recomputation_fails_closed_when_existing_output_differs(self) -> None:
        identity = self.identity()
        output = self.root / "root.img"
        output.write_bytes(b"configured A")
        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output},
            elapsed_seconds=1,
        )
        stored_digest = stored["outputs"][0]["sha256"]

        output.write_bytes(b"configured B")
        run_manifest = self.root / "mismatched-recomputation.json"
        with self.assertRaisesRegex(
            self.module.CheckpointError,
            "existing checkpoint output differs from rebuilt output: root.img",
        ):
            self.module.store_checkpoint(
                cache_root=self.cache,
                identity=identity,
                outputs={"root.img": output},
                elapsed_seconds=2,
                run_manifest=run_manifest,
            )

        self.assertFalse(run_manifest.exists())
        verified = self.module.verify_checkpoint(self.cache, identity)
        self.assertEqual(verified["outputs"][0]["sha256"], stored_digest)

    def test_restore_records_real_phase_time_and_hashes_source_once(self) -> None:
        identity = self.identity()
        output = self.root / "root.img"
        output.write_bytes(b"configured image")
        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output},
            elapsed_seconds=12.5,
        )
        object_record = stored["outputs"][0]
        object_path = (
            self.cache
            / "objects/sha256"
            / object_record["sha256"][:2]
            / object_record["sha256"]
        )
        restored = self.root / "restored.img"
        run_manifest = self.root / "restore-run.json"
        digest_calls = []
        real_sha256_file = self.module.sha256_file

        def counting_sha256(path):
            digest_calls.append(Path(path))
            return real_sha256_file(path)

        with (
            mock.patch(
                "time.monotonic",
                side_effect=[10.0, 10.25, 10.75],
            ),
            mock.patch.object(
                self.module,
                "sha256_file",
                side_effect=counting_sha256,
            ),
        ):
            result = self.module.restore_checkpoint(
                cache_root=self.cache,
                identity=identity,
                destinations={"root.img": restored},
                run_manifest=run_manifest,
            )

        self.assertEqual(result["elapsed_seconds"], 0.75)
        self.assertEqual(
            result["cache_hit_timing"],
            {
                "lookup_and_verification_seconds": 0.25,
                "materialization_and_readback_seconds": 0.5,
            },
        )
        self.assertEqual(
            json.loads(run_manifest.read_text()),
            {key: value for key, value in result.items() if key != "manifest_path"},
        )
        # Until 2026-08-30 the source object was hashed here by sha256_file
        # during verification and then read a second time by the copy. Restore
        # now authenticates the object's bytes while streaming them, so
        # sha256_file never touches the source at all and the object is opened
        # exactly once. The destination read-back is unchanged: it is what
        # detects a torn write, and dropping it would weaken truthfulness.
        self.assertEqual(digest_calls.count(object_path), 0)
        self.assertEqual(digest_calls.count(restored), 1)

    # -- one-pass streaming verification (plan Phase C4) --------------------

    def count_read_opens(self):
        """Count read-opens per path, at the seam every reader goes through."""
        counts: dict[str, int] = {}
        real_open = Path.open

        def counting_open(self_path, *args, **keywords):
            mode = str(args[0] if args else keywords.get("mode", "r"))
            if "r" in mode and "w" not in mode and "a" not in mode:
                counts[str(self_path)] = counts.get(str(self_path), 0) + 1
            return real_open(self_path, *args, **keywords)

        return counts, mock.patch.object(Path, "open", counting_open)

    def test_restore_opens_the_object_once_and_reads_back_once(self) -> None:
        # Before 2026-08-30 restore read the source object twice: once for the
        # digest inside verify_checkpoint, once for the copy. It now hashes
        # while it copies, so the object is opened exactly once. The
        # destination read-back stays at one -- that read is what detects a
        # torn or bit-flipped write.
        identity = self.identity()
        output = self.root / "root.img"
        output.write_bytes(b"configured image payload " * 4096)
        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output},
            elapsed_seconds=1.0,
        )
        digest = stored["outputs"][0]["sha256"]
        object_path = self.cache / "objects/sha256" / digest[:2] / digest
        restored = self.root / "restored.img"

        counts, patcher = self.count_read_opens()
        with patcher:
            self.module.restore_checkpoint(
                cache_root=self.cache,
                identity=identity,
                destinations={"root.img": restored},
            )

        self.assertEqual(counts.get(str(object_path), 0), 1)
        self.assertEqual(counts.get(str(restored), 0), 1)

    def test_store_reads_the_source_once_per_copy_and_verifies_writes_once(
        self,
    ) -> None:
        # The source is opened twice in total and that is inherent, not waste:
        # once to compute the content address that decides where the object
        # lives and whether it already exists, once to copy it. What C4 removed
        # is the redundant verification of the written bytes, which used to be
        # read three times -- the temporary digest, the post-rename re-read, and
        # the closing verify_checkpoint -- and is now read exactly once.
        identity = self.identity()
        output = self.root / "root.img"
        output.write_bytes(b"configured image payload " * 4096)

        counts, patcher = self.count_read_opens()
        with patcher:
            stored = self.module.store_checkpoint(
                cache_root=self.cache,
                identity=identity,
                outputs={"root.img": output},
                elapsed_seconds=1.0,
            )

        digest = stored["outputs"][0]["sha256"]
        object_path = self.cache / "objects/sha256" / digest[:2] / digest
        written_reads = sum(
            value
            for key, value in counts.items()
            if key == str(object_path) or ".tmp" in key
        )

        self.assertEqual(counts.get(str(output), 0), 2)
        self.assertEqual(written_reads, 1)

    def test_the_post_rename_object_re_read_is_gone(self) -> None:
        # _store_object used to finish by re-hashing the object it had just
        # renamed -- the same inode it had already read back. Storing now ends
        # with metadata-only validation, so no read of the final object path
        # happens after the rename.
        identity = self.identity()
        output = self.root / "root.img"
        output.write_bytes(b"configured image payload " * 4096)

        counts, patcher = self.count_read_opens()
        with patcher:
            stored = self.module.store_checkpoint(
                cache_root=self.cache,
                identity=identity,
                outputs={"root.img": output},
                elapsed_seconds=1.0,
            )

        digest = stored["outputs"][0]["sha256"]
        object_path = self.cache / "objects/sha256" / digest[:2] / digest
        self.assertEqual(counts.get(str(object_path), 0), 0)

    def test_streamed_hash_matches_a_standalone_digest_for_a_sparse_file(
        self,
    ) -> None:
        # The streamed hash must cover the logical byte stream, holes included,
        # or a sparse object would be rejected on restore.
        source = self.root / "sparse.img"
        with source.open("wb") as stream:
            stream.truncate(1 << 20)
            stream.seek(1 << 19)
            stream.write(b"payload in the middle")
        destination = self.root / "sparse-copy.img"

        streamed = self.module._copy_sparse_file(source, destination)

        self.assertEqual(streamed, self.module.sha256_file(source))
        self.assertEqual(streamed, self.module.sha256_file(destination))

    # -- truthful transfer and verification accounting (plan slice C5) -------

    def assert_timing_split_is_plausible(self, record: dict) -> None:
        split = record["verification_timing"]
        self.assertEqual(
            set(split),
            {
                "checkpoint_verification_seconds",
                "content_readback_seconds",
                "transfer_seconds",
            },
        )
        for name, seconds in split.items():
            with self.subTest(timing=name):
                self.assertIsInstance(seconds, float)
                self.assertGreaterEqual(seconds, 0.0)

    def test_sha256_file_hashes_holes_without_reading_them(self) -> None:
        # The digest must cover the logical stream, holes included, exactly
        # as a linear read would; but the holes must come from the zero buffer,
        # not from a read, or an empty 34 GB image costs a full pass.
        source = self.root / "holes.img"
        with source.open("wb") as stream:
            stream.truncate(64 * 1024 * 1024)
            stream.seek(20 * 1024 * 1024)
            stream.write(b"data" * 1024)
            stream.seek(40 * 1024 * 1024 + 123)
            stream.write(os.urandom(300000))
            stream.truncate(64 * 1024 * 1024 + 7)
        import hashlib

        linear = hashlib.sha256(source.read_bytes()).hexdigest()
        real_read = os.read
        read_bytes = 0

        def counting_read(descriptor, size):
            nonlocal read_bytes
            chunk = real_read(descriptor, size)
            read_bytes += len(chunk)
            return chunk

        with source.open("rb") as probe:
            try:
                os.lseek(probe.fileno(), 0, os.SEEK_DATA)
            except OSError:
                self.skipTest("filesystem has no SEEK_DATA support")
        self.assertEqual(self.module.sha256_file(source), linear)
        with source.open("rb", buffering=0) as stream:
            stream.read(1)
        # A hashing pass over the same file through a raw descriptor must
        # touch only the data extents, never the 60 MB of holes.
        with mock.patch.object(os, "read", counting_read):
            self.module.sha256_file(source)
        self.assertLess(read_bytes, 8 * 1024 * 1024)

    def test_restore_accounting_reports_the_object_stream_it_materialized(
        self,
    ) -> None:
        # C4 measured the pass counts but could not record them: the planner
        # rejected any run record carrying extra fields. With the accepted sets
        # grown, restore states what it moved -- and what it states is the
        # object's own logical size, because it streams that object exactly
        # once.
        identity = self.identity()
        output = self.root / "root.img"
        payload = b"configured image payload " * 4096
        output.write_bytes(payload)
        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output},
            elapsed_seconds=1.0,
        )
        object_record = stored["outputs"][0]
        restored = self.root / "restored.img"
        run_manifest = self.root / "restore-run.json"

        result = self.module.restore_checkpoint(
            cache_root=self.cache,
            identity=identity,
            destinations={"root.img": restored},
            run_manifest=run_manifest,
        )

        self.assertEqual(result["bytes_read"], object_record["size_bytes"])
        self.assertEqual(result["bytes_read"], len(payload))
        self.assertEqual(result["bytes_written"], restored.stat().st_size)
        self.assert_timing_split_is_plausible(result)
        self.assertEqual(
            json.loads(run_manifest.read_text()),
            {key: value for key, value in result.items() if key != "manifest_path"},
        )

    def test_restore_accounting_counts_a_sparse_object_by_logical_size(self) -> None:
        # Holes are hashed as the zeros a reader sees, so the accounting has to
        # report the logical stream too, or a sparse restore would understate
        # what it authenticated.
        identity = self.identity()
        output = self.root / "sparse.img"
        with output.open("wb") as stream:
            stream.truncate(1 << 20)
            stream.seek(1 << 19)
            stream.write(b"payload in the middle")
        logical_size = output.stat().st_size
        self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"sparse.img": output},
            elapsed_seconds=1.0,
        )
        restored = self.root / "restored-sparse.img"

        result = self.module.restore_checkpoint(
            cache_root=self.cache,
            identity=identity,
            destinations={"sparse.img": restored},
        )

        self.assertEqual(result["bytes_read"], logical_size)
        self.assertEqual(result["bytes_written"], logical_size)
        self.assertEqual(restored.stat().st_size, logical_size)

    def test_store_accounting_reports_the_bytes_it_materialized(self) -> None:
        identity = self.identity()
        output = self.root / "root.img"
        payload = b"configured image payload " * 4096
        output.write_bytes(payload)
        tree = self.root / "tree"
        tree.mkdir()
        (tree / "one.txt").write_bytes(b"a" * 128)
        (tree / "two.txt").write_bytes(b"b" * 256)
        run_manifest = self.root / "store-run.json"

        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output, "tree": tree},
            elapsed_seconds=1.0,
            run_manifest=run_manifest,
        )

        digest = next(
            record["sha256"]
            for record in stored["outputs"]
            if record["name"] == "root.img"
        )
        object_path = self.cache / "objects/sha256" / digest[:2] / digest
        inline = (
            self.module._checkpoint_directory(self.cache, identity) / "outputs" / "tree"
        )
        materialized = object_path.stat().st_size + sum(
            child.stat().st_size for child in sorted(inline.iterdir())
        )
        self.assertEqual(stored["bytes_written"], materialized)
        self.assertEqual(stored["bytes_read"], len(payload) + 128 + 256)
        self.assert_timing_split_is_plausible(stored)
        self.assertEqual(
            json.loads(run_manifest.read_text()),
            {key: value for key, value in stored.items() if key != "manifest_path"},
        )

    def test_store_accounting_is_zero_when_nothing_was_copied(self) -> None:
        # Two paths copy nothing and must say so rather than reporting bytes
        # they only read: a rebuild that matches an existing checkpoint, and an
        # object the store already holds under a different checkpoint.
        identity = self.identity()
        output = self.root / "root.img"
        output.write_bytes(b"configured image payload " * 4096)
        self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output},
            elapsed_seconds=1.0,
        )

        recomputed = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output},
            elapsed_seconds=1.0,
        )
        self.assertTrue(recomputed["reproducibility_match"])
        self.assertEqual(recomputed["bytes_read"], 0)
        self.assertEqual(recomputed["bytes_written"], 0)
        self.assert_timing_split_is_plausible(recomputed)
        self.assertEqual(recomputed["verification_timing"]["transfer_seconds"], 0.0)

        deduplicated = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=self.identity(stage="configured-target"),
            outputs={"root.img": output},
            elapsed_seconds=1.0,
        )
        self.assertEqual(deduplicated["bytes_written"], 0)
        self.assertEqual(deduplicated["bytes_read"], 0)
        self.assert_timing_split_is_plausible(deduplicated)

    def test_verify_checkpoint_still_reads_objects_by_default(self) -> None:
        # The standalone entry point is unchanged: it still authenticates
        # stored content. Only restore and the tail of store opt out, because
        # they have already read those bytes.
        identity = self.identity()
        output = self.root / "root.img"
        output.write_bytes(b"configured image payload " * 4096)
        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output},
            elapsed_seconds=1.0,
        )
        digest = stored["outputs"][0]["sha256"]
        object_path = self.cache / "objects/sha256" / digest[:2] / digest

        counts, patcher = self.count_read_opens()
        with patcher:
            self.module.verify_checkpoint(self.cache, identity)

        self.assertEqual(counts.get(str(object_path), 0), 1)

    def test_restore_fails_closed_if_verified_object_changes_before_copy(self) -> None:
        identity = self.identity()
        output = self.root / "root.img"
        output.write_bytes(b"configured image")
        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output},
            elapsed_seconds=12.5,
        )
        object_record = stored["outputs"][0]
        object_path = (
            self.cache
            / "objects/sha256"
            / object_record["sha256"][:2]
            / object_record["sha256"]
        )
        restored = self.root / "restored.img"
        # File objects are streamed by _copy_sparse_file now, not _copy_path, so
        # the mutation hook moves to that seam. The refusal also moves earlier:
        # the streamed hash catches the swap before the temporary is renamed,
        # where previously only the destination read-back caught it afterwards.
        real_copy = self.module._copy_sparse_file

        def mutate_then_copy(source, destination, **keywords):
            # The stand-in forwards the transfer counters restore now passes.
            if Path(source) == object_path:
                object_path.chmod(0o644)
                object_path.write_bytes(b"corrupted payload")
                object_path.chmod(0o444)
            return real_copy(source, destination, **keywords)

        with mock.patch.object(
            self.module,
            "_copy_sparse_file",
            side_effect=mutate_then_copy,
        ):
            with self.assertRaisesRegex(
                self.module.CheckpointError,
                "checkpoint object digest or size mismatch",
            ):
                self.module.restore_checkpoint(
                    cache_root=self.cache,
                    identity=identity,
                    destinations={"root.img": restored},
                )
        self.assertFalse(restored.exists())

    def test_restore_rechecks_symlink_after_initial_verification(self) -> None:
        identity = self.identity()
        output = self.root / "root.img"
        output.write_bytes(b"configured image")
        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output},
            elapsed_seconds=12.5,
        )
        object_record = stored["outputs"][0]
        object_path = (
            self.cache
            / "objects/sha256"
            / object_record["sha256"][:2]
            / object_record["sha256"]
        )
        restored = self.root / "restored.img"
        real_verify_checkpoint = self.module.verify_checkpoint

        def verify_then_replace(cache_root, passed_identity, **keywords):
            # Restore now passes verify_object_content=False, so the stand-in
            # forwards whatever keywords it is given.
            result = real_verify_checkpoint(cache_root, passed_identity, **keywords)
            object_path.chmod(0o644)
            object_path.unlink()
            object_path.symlink_to(output)
            return result

        with mock.patch.object(
            self.module,
            "verify_checkpoint",
            side_effect=verify_then_replace,
        ):
            with self.assertRaisesRegex(self.module.CheckpointError, "symlink"):
                self.module.restore_checkpoint(
                    cache_root=self.cache,
                    identity=identity,
                    destinations={"root.img": restored},
                )
        self.assertFalse(restored.exists())

    def test_restore_removes_partial_temporary_when_copy_fails(self) -> None:
        identity = self.identity()
        output = self.root / "root.img"
        output.write_bytes(b"configured image")
        self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output},
            elapsed_seconds=12.5,
        )
        restored = self.root / "restored.img"
        temporary = restored.with_name(
            f".{restored.name}.{os.getpid()}.restore"
        )

        def partial_copy_then_fail(_source, destination, **_keywords):
            Path(destination).write_bytes(b"partial")
            raise self.module.CheckpointError("injected copy failure")

        # File objects stream through _copy_sparse_file now; the cleanup
        # guarantee it proves is unchanged.
        with mock.patch.object(
            self.module,
            "_copy_sparse_file",
            side_effect=partial_copy_then_fail,
        ):
            with self.assertRaisesRegex(
                self.module.CheckpointError,
                "injected copy failure",
            ):
                self.module.restore_checkpoint(
                    cache_root=self.cache,
                    identity=identity,
                    destinations={"root.img": restored},
                )
        self.assertFalse(restored.exists())
        self.assertFalse(temporary.exists())
        self.assertFalse(temporary.is_symlink())

    def test_corrupt_or_mutable_checkpoint_fails_closed(self) -> None:
        identity = self.identity()
        output = self.root / "boot.img"
        output.write_bytes(b"boot")
        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"boot.img": output},
            elapsed_seconds=1,
        )
        output_record = stored["outputs"][0]
        checkpoint_output = (
            self.cache
            / "objects/sha256"
            / output_record["sha256"][:2]
            / output_record["sha256"]
        )
        checkpoint_output.chmod(0o644)

        with self.assertRaisesRegex(self.module.CheckpointError, "writable"):
            self.module.verify_checkpoint(self.cache, identity)

        checkpoint_output.chmod(0o444)
        checkpoint_output.chmod(0o644)
        checkpoint_output.write_bytes(b"changed")
        checkpoint_output.chmod(0o444)
        with self.assertRaisesRegex(self.module.CheckpointError, "digest"):
            self.module.verify_checkpoint(self.cache, identity)

    def test_rekey_reuses_only_verified_outputs_with_exact_transition(self) -> None:
        shared = self.root / "shared"
        shared.write_bytes(b"same input")
        broad = self.root / "broad"
        broad.write_bytes(b"legacy broad manifest")
        source_identity = self.identity(
            inputs={"broad-source": broad, "shared": shared}
        )
        output = self.root / "root.img"
        output.write_bytes(b"verified configured target")
        source = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=source_identity,
            outputs={"root-image": output},
            elapsed_seconds=91,
        )

        precise = self.root / "precise"
        precise.write_bytes(b"exact stage manifest")
        target_identity = self.identity(
            inputs={"source-manifest": precise, "shared": shared}
        )
        migrated = self.module.rekey_checkpoint(
            cache_root=self.cache,
            source_identity=source_identity,
            target_identity=target_identity,
            equivalent_inputs={"shared": "shared"},
            allowed_added_inputs={"source-manifest"},
            allowed_removed_inputs={"broad-source"},
            allow_source_lock_change=False,
            allow_source_commits_change=False,
            expected_outputs={
                "root-image": {
                    "sha256": source["outputs"][0]["sha256"],
                    "size_bytes": source["outputs"][0]["size_bytes"],
                }
            },
            reason="stage-input-granularity-v1",
        )
        self.assertEqual(
            migrated["checkpoint_identity"],
            target_identity["checkpoint_identity"],
        )
        self.assertEqual(
            migrated["migration"]["source_checkpoint_identity"],
            source_identity["checkpoint_identity"],
        )
        restored = self.root / "restored.img"
        result = self.module.restore_checkpoint(
            cache_root=self.cache,
            identity=target_identity,
            destinations={"root-image": restored},
        )
        self.assertTrue(result["cache_hit"])
        self.assertEqual(restored.read_bytes(), output.read_bytes())

    def test_rekey_copies_immutable_inline_directory_before_restoring_mode(self) -> None:
        shared = self.root / "shared"
        shared.write_bytes(b"same input")
        source_identity = self.identity(inputs={"shared": shared})
        state = self.root / "state"
        state.mkdir()
        (state / "state.json").write_text('{"result":"passed"}\n')
        source = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=source_identity,
            outputs={"stage-state": state},
            elapsed_seconds=1,
        )
        target_identity = self.identity(inputs={"shared": shared})
        target_identity["source_commits"]["omarchy_iso"] = "c" * 40
        unsigned = {
            key: value
            for key, value in target_identity.items()
            if key not in {"input_digest", "checkpoint_identity"}
        }
        target_identity["input_digest"] = self.module._json_digest(unsigned)
        target_identity["checkpoint_identity"] = self.module._json_digest(
            unsigned | {"input_digest": target_identity["input_digest"]}
        )

        migrated = self.module.rekey_checkpoint(
            cache_root=self.cache,
            source_identity=source_identity,
            target_identity=target_identity,
            equivalent_inputs={"shared": "shared"},
            allowed_added_inputs=set(),
            allowed_removed_inputs=set(),
            allow_source_lock_change=False,
            allow_source_commits_change=True,
            expected_outputs={
                "stage-state": {
                    "sha256": source["outputs"][0]["sha256"],
                    "size_bytes": source["outputs"][0]["size_bytes"],
                }
            },
            reason="immutable-inline-directory-v1",
        )
        self.assertEqual(
            migrated["checkpoint_identity"],
            target_identity["checkpoint_identity"],
        )
        restored = self.root / "restored-state"
        result = self.module.restore_checkpoint(
            cache_root=self.cache,
            identity=target_identity,
            destinations={"stage-state": restored},
        )
        self.assertTrue(result["cache_hit"])
        self.assertEqual(
            (restored / "state.json").read_text(),
            (state / "state.json").read_text(),
        )

    def test_rekey_fails_closed_on_input_output_or_allowlist_mismatch(self) -> None:
        shared = self.root / "shared"
        shared.write_bytes(b"source input")
        removed = self.root / "removed"
        removed.write_bytes(b"legacy")
        source_identity = self.identity(
            inputs={"removed": removed, "shared": shared}
        )
        output = self.root / "boot.img"
        output.write_bytes(b"boot")
        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=source_identity,
            outputs={"boot-image": output},
            elapsed_seconds=1,
        )
        changed = self.root / "changed"
        changed.write_bytes(b"different input")
        added = self.root / "added"
        added.write_bytes(b"precise")
        target_identity = self.identity(
            inputs={"added": added, "shared": changed}
        )
        common = dict(
            cache_root=self.cache,
            source_identity=source_identity,
            target_identity=target_identity,
            equivalent_inputs={"shared": "shared"},
            allowed_added_inputs={"added"},
            allowed_removed_inputs={"removed"},
            allow_source_lock_change=False,
            allow_source_commits_change=False,
            expected_outputs={
                "boot-image": {
                    "sha256": stored["outputs"][0]["sha256"],
                    "size_bytes": stored["outputs"][0]["size_bytes"],
                }
            },
            reason="stage-input-granularity-v1",
        )
        with self.assertRaisesRegex(self.module.CheckpointError, "equivalent input"):
            self.module.rekey_checkpoint(**common)

        target_identity = self.identity(inputs={"added": added, "shared": shared})
        common["target_identity"] = target_identity
        common["allowed_added_inputs"] = set()
        with self.assertRaisesRegex(self.module.CheckpointError, "added input allowlist"):
            self.module.rekey_checkpoint(**common)

        common["allowed_added_inputs"] = {"added"}
        common["expected_outputs"] = {
            "boot-image": {"sha256": "0" * 64, "size_bytes": 4}
        }
        with self.assertRaisesRegex(self.module.CheckpointError, "expected output"):
            self.module.rekey_checkpoint(**common)

    def test_rekey_requires_executed_verifier_for_projected_equivalence(self) -> None:
        source_manifest = self.root / "legacy-repository-manifest.json"
        source_manifest.write_text('{"snapshot_lock":"broad"}\n')
        source_identity = self.identity(inputs={"repository-manifest": source_manifest})
        output = self.root / "offline.db.tar.gz"
        output.write_bytes(b"verified repository database")
        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=source_identity,
            outputs={"repository-db": output},
            elapsed_seconds=3,
        )

        target_manifest = self.root / "projected-repository-manifest.json"
        target_manifest.write_text('{"snapshot_lock":"projected"}\n')
        target_identity = self.identity(inputs={"repository-manifest": target_manifest})
        common = dict(
            cache_root=self.cache,
            source_identity=source_identity,
            target_identity=target_identity,
            equivalent_inputs={},
            projected_equivalent_inputs={
                "repository-manifest": "repository-manifest"
            },
            allowed_added_inputs=set(),
            allowed_removed_inputs=set(),
            allow_source_lock_change=False,
            allow_source_commits_change=False,
            expected_outputs={
                "repository-db": {
                    "sha256": stored["outputs"][0]["sha256"],
                    "size_bytes": stored["outputs"][0]["size_bytes"],
                }
            },
            reason="stage-input-granularity-v1",
        )

        with self.assertRaisesRegex(self.module.CheckpointError, "projected.*verifier"):
            self.module.rekey_checkpoint(**common)

        calls = []

        def verifier(source_record, target_record):
            calls.append((source_record["sha256"], target_record["sha256"]))
            return {
                "kind": "repository-database-manifest-v1",
                "proof_digest": "c" * 64,
            }

        common["projected_equivalence_verifier"] = verifier
        migrated = self.module.rekey_checkpoint(**common)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            migrated["checkpoint_identity"], target_identity["checkpoint_identity"]
        )

    def test_rekey_rejects_invalid_projected_equivalence_proof(self) -> None:
        source_value = self.root / "source-value"
        source_value.write_text("source\n")
        source_identity = self.identity(inputs={"value": source_value})
        output = self.root / "output"
        output.write_text("output\n")
        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=source_identity,
            outputs={"value": output},
            elapsed_seconds=1,
        )
        target_value = self.root / "target-value"
        target_value.write_text("target\n")
        target_identity = self.identity(inputs={"value": target_value})
        with self.assertRaisesRegex(self.module.CheckpointError, "projection proof"):
            self.module.rekey_checkpoint(
                cache_root=self.cache,
                source_identity=source_identity,
                target_identity=target_identity,
                equivalent_inputs={},
                projected_equivalent_inputs={"value": "value"},
                projected_equivalence_verifier=lambda _source, _target: {
                    "kind": "unsafe",
                    "proof_digest": "not-a-digest",
                },
                allowed_added_inputs=set(),
                allowed_removed_inputs=set(),
                allow_source_lock_change=False,
                allow_source_commits_change=False,
                expected_outputs={
                    "value": {
                        "sha256": stored["outputs"][0]["sha256"],
                        "size_bytes": stored["outputs"][0]["size_bytes"],
                    }
                },
                reason="stage-input-granularity-v1",
            )

    def test_explicit_legacy_admission_seals_only_exact_verified_checkpoint(self) -> None:
        identity = self.identity()
        output = self.root / "root.img"
        output.write_bytes(b"legacy verified image")
        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root-image": output},
            elapsed_seconds=4,
        )
        manifest_path = Path(stored["manifest_path"])
        object_record = stored["outputs"][0]
        object_path = (
            self.cache
            / "objects/sha256"
            / object_record["sha256"][:2]
            / object_record["sha256"]
        )
        checkpoint_path = manifest_path.parent
        checkpoint_path.chmod(0o700)
        manifest_path.chmod(0o644)
        object_path.chmod(0o644)
        with self.assertRaisesRegex(self.module.CheckpointError, "writable"):
            self.module.verify_checkpoint(self.cache, identity)

        manifest_bytes = manifest_path.read_bytes()
        sealed = self.module.seal_legacy_checkpoint(
            cache_root=self.cache,
            identity=identity,
            expected_manifest={
                "sha256": self.module.hashlib.sha256(manifest_bytes).hexdigest(),
                "size_bytes": len(manifest_bytes),
            },
            expected_outputs={
                "root-image": {
                    "sha256": object_record["sha256"],
                    "size_bytes": object_record["size_bytes"],
                }
            },
            reason="legacy-checkpoint-immutable-admission-v1",
        )
        self.assertEqual(sealed["checkpoint_identity"], identity["checkpoint_identity"])
        self.assertFalse(manifest_path.stat().st_mode & 0o222)
        self.assertFalse(object_path.stat().st_mode & 0o222)
        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)

    def test_legacy_admission_fails_before_mode_change_on_digest_mismatch(self) -> None:
        identity = self.identity()
        output = self.root / "boot.img"
        output.write_bytes(b"boot")
        stored = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"boot-image": output},
            elapsed_seconds=1,
        )
        manifest_path = Path(stored["manifest_path"])
        manifest_path.parent.chmod(0o700)
        manifest_path.chmod(0o644)
        with self.assertRaisesRegex(self.module.CheckpointError, "manifest digest"):
            self.module.seal_legacy_checkpoint(
                cache_root=self.cache,
                identity=identity,
                expected_manifest={"sha256": "0" * 64, "size_bytes": 1},
                expected_outputs={
                    "boot-image": {
                        "sha256": stored["outputs"][0]["sha256"],
                        "size_bytes": stored["outputs"][0]["size_bytes"],
                    }
                },
                reason="legacy-checkpoint-immutable-admission-v1",
            )
        self.assertTrue(manifest_path.stat().st_mode & 0o200)

    def test_symlinked_input_and_unsafe_destination_are_rejected(self) -> None:
        real = self.root / "real"
        real.write_bytes(b"payload")
        linked = self.root / "linked"
        linked.symlink_to(real)
        with self.assertRaisesRegex(self.module.CheckpointError, "symlink"):
            self.identity(inputs={"payload": linked})

        identity = self.identity()
        output = self.root / "root.img"
        output.write_bytes(b"root")
        self.module.store_checkpoint(
            cache_root=self.cache,
            identity=identity,
            outputs={"root.img": output},
            elapsed_seconds=1,
        )
        destination_target = self.root / "destination-target"
        destination_target.write_bytes(b"owner data")
        destination = self.root / "destination"
        destination.symlink_to(destination_target)
        with self.assertRaisesRegex(self.module.CheckpointError, "destination"):
            self.module.restore_checkpoint(
                cache_root=self.cache,
                identity=identity,
                destinations={"root.img": destination},
            )
        self.assertEqual(destination_target.read_bytes(), b"owner data")

    def test_diagnostic_and_qualification_identities_never_alias(self) -> None:
        diagnostic = self.identity(mode="diagnostic")
        qualification = self.identity(mode="qualification")
        self.assertNotEqual(
            diagnostic["checkpoint_identity"], qualification["checkpoint_identity"]
        )

    def test_retention_prunes_only_unprotected_checkpoints_and_orphan_objects(self) -> None:
        first_input = self.root / "first-input"
        first_input.write_bytes(b"first")
        first_identity = self.identity(inputs={"payload": first_input})
        first_output = self.root / "first-output"
        first_output.write_bytes(b"old object")
        first = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=first_identity,
            outputs={"root.img": first_output},
            elapsed_seconds=1,
        )

        second_input = self.root / "second-input"
        second_input.write_bytes(b"second")
        second_identity = self.identity(inputs={"payload": second_input})
        second_output = self.root / "second-output"
        second_output.write_bytes(b"protected object")
        run_manifest = self.root / "protected-run.json"
        second = self.module.store_checkpoint(
            cache_root=self.cache,
            identity=second_identity,
            outputs={"root.img": second_output},
            elapsed_seconds=1,
            run_manifest=run_manifest,
        )

        completed = subprocess.run(
            [
                "python3",
                str(PRUNE_SCRIPT),
                "--cache-root",
                str(self.cache),
                "--maximum-bytes",
                "1",
                "--maximum-checkpoints-per-stage",
                "1",
                "--protect-run-manifest",
                str(run_manifest),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        report = json.loads(completed.stdout)
        self.assertTrue(report["evicted"])
        self.assertFalse(Path(first["manifest_path"]).exists())
        self.assertTrue(Path(second["manifest_path"]).exists())
        old_object = (
            self.cache
            / "objects/sha256"
            / first["outputs"][0]["sha256"][:2]
            / first["outputs"][0]["sha256"]
        )
        self.assertFalse(old_object.exists())


if __name__ == "__main__":
    unittest.main()
