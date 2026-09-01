#!/usr/bin/env python3
"""Apply bounded, sparse-aware retention to the Asahi checkpoint cache."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat


DIGEST = re.compile(r"^[0-9a-f]{64}$")


class RetentionError(RuntimeError):
    pass


def require_directory(path: Path, role: str) -> None:
    if not path.is_dir() or path.is_symlink():
        raise RetentionError(f"{role} is missing or unsafe: {path}")


def allocated_bytes(path: Path) -> int:
    total = 0
    nodes = [path]
    if path.is_dir() and not path.is_symlink():
        nodes.extend(path.rglob("*"))
    for node in nodes:
        status = node.lstat()
        if stat.S_ISLNK(status.st_mode):
            raise RetentionError(f"symlink is forbidden in checkpoint cache: {node}")
        if stat.S_ISREG(status.st_mode):
            total += status.st_blocks * 512
        elif not stat.S_ISDIR(status.st_mode):
            raise RetentionError(f"special file is forbidden in checkpoint cache: {node}")
    return total


def load_json(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise RetentionError(f"manifest is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RetentionError(f"manifest is invalid: {path}") from error
    if not isinstance(value, dict):
        raise RetentionError(f"manifest is not an object: {path}")
    return value


def protected_identities(paths: list[Path]) -> set[tuple[str, str]]:
    result = set()
    for path in paths:
        value = load_json(path)
        stage = value.get("stage")
        identity = value.get("checkpoint_identity")
        if not isinstance(stage, str) or not isinstance(identity, str) or not DIGEST.fullmatch(identity):
            raise RetentionError(f"protected run manifest lacks an exact identity: {path}")
        result.add((stage, identity))
    return result


def writable_remove_tree(path: Path) -> int:
    size = allocated_bytes(path)
    for node in sorted([*path.rglob("*"), path], key=lambda item: len(item.parts), reverse=True):
        mode = stat.S_IMODE(node.lstat().st_mode)
        os.chmod(node, mode | stat.S_IWUSR)
    shutil.rmtree(path)
    return size


def checkpoint_records(root: Path) -> list[dict]:
    checkpoints = root / "checkpoints"
    if not checkpoints.exists():
        return []
    require_directory(checkpoints, "checkpoint directory")
    records = []
    for stage_directory in sorted(checkpoints.iterdir()):
        require_directory(stage_directory, "checkpoint stage directory")
        for checkpoint in sorted(stage_directory.iterdir()):
            require_directory(checkpoint, "checkpoint identity directory")
            if not DIGEST.fullmatch(checkpoint.name):
                raise RetentionError(f"unsafe checkpoint identity directory: {checkpoint}")
            manifest = load_json(checkpoint / "manifest.json")
            if manifest.get("stage") != stage_directory.name or manifest.get("checkpoint_identity") != checkpoint.name:
                raise RetentionError(f"checkpoint directory and manifest disagree: {checkpoint}")
            records.append(
                {
                    "stage": stage_directory.name,
                    "identity": checkpoint.name,
                    "completed_at": str(manifest.get("completed_at", "")),
                    "path": checkpoint,
                }
            )
    return records


def referenced_objects(records: list[dict]) -> set[str]:
    references = set()
    for record in records:
        manifest = load_json(record["path"] / "manifest.json")
        for output in manifest.get("outputs", []):
            storage = output.get("storage", {})
            if storage.get("kind") == "sha256-object":
                digest = storage.get("sha256")
                if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                    raise RetentionError("checkpoint contains an invalid object reference")
                references.add(digest)
    return references


def prune(
    *,
    cache_root: Path,
    maximum_bytes: int,
    maximum_checkpoints_per_stage: int,
    protected: set[tuple[str, str]],
) -> dict:
    require_directory(cache_root, "cache root")
    if maximum_bytes <= 0 or maximum_checkpoints_per_stage <= 0:
        raise RetentionError("retention bounds must be positive")
    before = allocated_bytes(cache_root)
    records = checkpoint_records(cache_root)
    to_evict: list[dict] = []
    for stage in sorted({record["stage"] for record in records}):
        stage_records = sorted(
            (record for record in records if record["stage"] == stage),
            key=lambda record: (record["completed_at"], record["identity"]),
            reverse=True,
        )
        for record in stage_records[maximum_checkpoints_per_stage:]:
            if (record["stage"], record["identity"]) not in protected:
                to_evict.append(record)

    evicted = []
    for record in to_evict:
        reclaimed = writable_remove_tree(record["path"])
        evicted.append(
            {
                "kind": "checkpoint",
                "stage": record["stage"],
                "identity": record["identity"],
                "reclaimed_bytes": reclaimed,
                "cause": "per-stage-retention-limit",
            }
        )

    remaining = [record for record in records if record not in to_evict]
    for record in sorted(remaining, key=lambda item: (item["completed_at"], item["identity"])):
        if allocated_bytes(cache_root) <= maximum_bytes:
            break
        if (record["stage"], record["identity"]) in protected:
            continue
        reclaimed = writable_remove_tree(record["path"])
        evicted.append(
            {
                "kind": "checkpoint",
                "stage": record["stage"],
                "identity": record["identity"],
                "reclaimed_bytes": reclaimed,
                "cause": "cache-byte-limit",
            }
        )
        remaining.remove(record)

    references = referenced_objects(remaining)
    objects = cache_root / "objects" / "sha256"
    if objects.exists():
        require_directory(objects, "object store")
        for object_path in sorted(objects.glob("*/*")):
            if object_path.is_symlink() or not object_path.is_file() or not DIGEST.fullmatch(object_path.name):
                raise RetentionError(f"unsafe object-store entry: {object_path}")
            if object_path.name in references:
                continue
            reclaimed = allocated_bytes(object_path)
            os.chmod(object_path, stat.S_IMODE(object_path.stat().st_mode) | stat.S_IWUSR)
            object_path.unlink()
            evicted.append(
                {
                    "kind": "object",
                    "sha256": object_path.name,
                    "reclaimed_bytes": reclaimed,
                    "cause": "unreferenced-object",
                }
            )
        for directory in sorted(objects.iterdir()):
            if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
                directory.rmdir()

    after = allocated_bytes(cache_root)
    return {
        "schema_version": 1,
        "result": "passed" if after <= maximum_bytes else "limit-unmet-protected-state",
        "maximum_bytes": maximum_bytes,
        "maximum_checkpoints_per_stage": maximum_checkpoints_per_stage,
        "allocated_bytes_before": before,
        "allocated_bytes_after": after,
        "reclaimed_bytes": before - after,
        "protected_identities": [
            {"stage": stage, "checkpoint_identity": identity}
            for stage, identity in sorted(protected)
        ],
        "evicted": evicted,
    }


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RetentionError(f"output is a symlink: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--maximum-bytes", type=int, required=True)
    parser.add_argument("--maximum-checkpoints-per-stage", type=int, required=True)
    parser.add_argument("--protect-run-manifest", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = prune(
        cache_root=args.cache_root,
        maximum_bytes=args.maximum_bytes,
        maximum_checkpoints_per_stage=args.maximum_checkpoints_per_stage,
        protected=protected_identities(args.protect_run_manifest),
    )
    if args.output is not None:
        atomic_json(args.output, report)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RetentionError as error:
        raise SystemExit(f"prune-asahi-checkpoints: {error}") from error
