#!/usr/bin/env python3
"""Run explicit unit-test lanes with bounded, deterministic reporting."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import math
from pathlib import Path
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid


DEFAULT_TEST_TIMEOUT_SECONDS = 300.0
PROCESS_GROUP_TERMINATION_GRACE_SECONDS = 1.0
TIMEOUT_RETURNCODE = 124
CANCELLED_RETURNCODE = 130
RESULT_LEDGER_SCHEMA_VERSION = 2
PYTHON_UNITTEST_RUNNER = """
import importlib
import sys
import unittest

module = importlib.import_module(sys.argv[1])
suite = unittest.defaultTestLoader.loadTestsFromModule(module)
if suite.countTestCases() == 0:
    print(f"no unittest cases collected from {sys.argv[1]}", file=sys.stderr)
    raise SystemExit(3)
result = unittest.TextTestRunner().run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
"""


@dataclass(frozen=True)
class Result:
    path: str
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float
    status: str

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def read_manifest(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def validate(root: Path, parallel: list[str], serial: list[str]) -> None:
    declared = [*parallel, *serial]
    duplicates = sorted({value for value in declared if declared.count(value) > 1})
    if duplicates:
        raise RuntimeError("duplicate test manifest entries: " + ",".join(duplicates))
    discovered = sorted(
        path.relative_to(root).as_posix()
        for pattern in ("*-test.sh", "test_*.py")
        for path in (root / "test/unit").glob(pattern)
    )
    if sorted(declared) != discovered:
        missing = sorted(set(discovered) - set(declared))
        extra = sorted(set(declared) - set(discovered))
        raise RuntimeError(f"test manifests are incomplete: missing={missing}, extra={extra}")
    for value in declared:
        path = root / value
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"test path is missing or unsafe: {value}")


def _cancelled_result(relative: str, reason: str) -> Result:
    return Result(
        path=relative,
        returncode=CANCELLED_RETURNCODE,
        stdout="",
        stderr=f"cancelled: {reason}\n",
        elapsed_seconds=0.0,
        status="cancelled",
    )


def _terminate_process_group(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float = PROCESS_GROUP_TERMINATION_GRACE_SECONDS,
) -> tuple[str, str]:
    """Terminate the process and descendants created in its new session."""

    deadline = time.monotonic() + grace_seconds
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        output: tuple[str, str] | None = process.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        output = None

    while _process_group_exists(process.pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if output is None:
        output = process.communicate()

    kill_deadline = time.monotonic() + grace_seconds
    while _process_group_exists(process.pid) and time.monotonic() < kill_deadline:
        time.sleep(0.01)
    if _process_group_exists(process.pid):
        raise RuntimeError(f"process group {process.pid} survived SIGKILL")
    return output


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def execute(
    root: Path,
    relative: str,
    *,
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
) -> Result:
    cancellation = cancel_event or threading.Event()
    if cancellation.is_set():
        return _cancelled_result(relative, "another test failed")

    path = root / relative
    environment = os.environ.copy()
    bash = environment.get("OMARCHY_TEST_BASH") or shutil.which("bash")
    if bash is None:
        raise RuntimeError("bash is unavailable")
    bash = str(Path(bash).resolve())
    environment["OMARCHY_TEST_BASH"] = bash
    if path.suffix == ".py":
        python_test_path = str(root / "test/unit")
        environment["PYTHONPATH"] = os.pathsep.join(
            [python_test_path, environment.get("PYTHONPATH", "")]
        )
        command = [sys.executable, "-c", PYTHON_UNITTEST_RUNNER, path.stem]
    else:
        command = [bash, str(path)]
    portability_paths = [
        Path(bash).parent,
        Path("/opt/homebrew/opt/grep/libexec/gnubin"),
        Path("/opt/homebrew/opt/gnu-sed/libexec/gnubin"),
        Path("/opt/homebrew/opt/coreutils/libexec/gnubin"),
    ]
    available = [str(path) for path in portability_paths if path.is_dir()]
    if available:
        environment["PATH"] = os.pathsep.join([*available, environment.get("PATH", "")])

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=True,
        )
    except OSError as error:
        cancellation.set()
        return Result(
            path=relative,
            returncode=1,
            stdout="",
            stderr=f"could not start test: {error}\n",
            elapsed_seconds=time.monotonic() - started,
            status="failed",
        )

    deadline = started + timeout_seconds
    while True:
        if cancellation.is_set():
            stdout, stderr = _terminate_process_group(process)
            return Result(
                path=relative,
                returncode=CANCELLED_RETURNCODE,
                stdout=stdout,
                stderr=stderr + "cancelled: another test failed\n",
                elapsed_seconds=time.monotonic() - started,
                status="cancelled",
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            cancellation.set()
            stdout, stderr = _terminate_process_group(process)
            return Result(
                path=relative,
                returncode=TIMEOUT_RETURNCODE,
                stdout=stdout,
                stderr=stderr + f"timed out after {timeout_seconds:g} seconds\n",
                elapsed_seconds=time.monotonic() - started,
                status="timed-out",
            )

        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            continue

        if _process_group_exists(process.pid):
            _terminate_process_group(process)
            cancellation.set()
            return Result(
                path=relative,
                returncode=1,
                stdout=stdout,
                stderr=stderr + "test left a process group running after exit\n",
                elapsed_seconds=time.monotonic() - started,
                status="failed",
            )

        status = "passed" if process.returncode == 0 else "failed"
        if status != "passed":
            cancellation.set()
        return Result(
            path=relative,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            elapsed_seconds=time.monotonic() - started,
            status=status,
        )


def _future_result(future: Future[Result], relative: str) -> Result:
    try:
        return future.result()
    except Exception as error:  # pragma: no cover - defensive worker boundary
        return Result(
            path=relative,
            returncode=1,
            stdout="",
            stderr=f"test runner worker failed: {error}\n",
            elapsed_seconds=0.0,
            status="failed",
        )


def run_parallel(
    root: Path,
    tests: list[str],
    *,
    jobs: int,
    timeout_seconds: float,
) -> list[Result]:
    if not tests:
        return []

    cancellation = threading.Event()
    results: dict[str, Result] = {}
    executor = ThreadPoolExecutor(max_workers=min(jobs, len(tests)))
    futures = {
        executor.submit(
            execute,
            root,
            relative,
            timeout_seconds=timeout_seconds,
            cancel_event=cancellation,
        ): relative
        for relative in tests
    }
    pending = set(futures)
    try:
        while pending:
            completed, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                relative = futures[future]
                results[relative] = _future_result(future, relative)
            if any(not results[futures[future]].passed for future in completed):
                cancellation.set()
                for future in list(pending):
                    if future.cancel():
                        relative = futures[future]
                        results[relative] = _cancelled_result(
                            relative,
                            "another parallel-safe test failed",
                        )
                        pending.remove(future)
    except BaseException:
        cancellation.set()
        for future in pending:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    return [results[relative] for relative in tests]


def run_serial(
    root: Path,
    tests: list[str],
    *,
    timeout_seconds: float,
) -> list[Result]:
    cancellation = threading.Event()
    results: list[Result] = []
    for index, relative in enumerate(tests):
        result = execute(
            root,
            relative,
            timeout_seconds=timeout_seconds,
            cancel_event=cancellation,
        )
        results.append(result)
        if not result.passed:
            results.extend(
                _cancelled_result(value, "an earlier serial test failed")
                for value in tests[index + 1 :]
            )
            break
    return results


def emit(result: Result) -> None:
    print(f"==> {result.path} [{result.status}; {result.elapsed_seconds:.3f}s]")
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)


def build_result_ledger(
    *,
    run_id: str,
    started_at: str,
    completed_at: str,
    mode: str,
    requested_jobs: int,
    parallel_worker_count: int,
    timeout_seconds: float,
    parallel_results: list[Result],
    serial_results: list[Result],
) -> dict[str, object]:
    records = [
        {
            "elapsed_seconds": round(result.elapsed_seconds, 3),
            "lane": lane,
            "path": result.path,
            "result": result.status,
            "returncode": result.returncode,
        }
        for lane, results in (
            ("parallel-safe", parallel_results),
            ("serial", serial_results),
        )
        for result in results
    ]
    return {
        "completed_at": completed_at,
        "kind": "omarchy-unit-test-result-ledger",
        "mode": mode,
        "parallel_worker_count": parallel_worker_count,
        "requested_worker_count": requested_jobs,
        "result": "passed" if all(record["result"] == "passed" for record in records) else "failed",
        "run_id": run_id,
        "schema_version": RESULT_LEDGER_SCHEMA_VERSION,
        "started_at": started_at,
        "test_timeout_seconds": timeout_seconds,
        "tests": records,
    }


def build_incomplete_ledger(
    *,
    run_id: str,
    started_at: str,
    mode: str,
    requested_jobs: int,
    parallel_worker_count: int,
    timeout_seconds: float,
    parallel_tests: list[str],
    serial_tests: list[str],
) -> dict[str, object]:
    return {
        "completed_at": None,
        "kind": "omarchy-unit-test-result-ledger",
        "mode": mode,
        "parallel_worker_count": parallel_worker_count,
        "requested_worker_count": requested_jobs,
        "result": "incomplete",
        "run_id": run_id,
        "schema_version": RESULT_LEDGER_SCHEMA_VERSION,
        "started_at": started_at,
        "test_timeout_seconds": timeout_seconds,
        "tests": [
            {
                "elapsed_seconds": 0.0,
                "lane": lane,
                "path": path,
                "result": "not-run",
                "returncode": None,
            }
            for lane, tests in (
                ("parallel-safe", parallel_tests),
                ("serial", serial_tests),
            )
            for path in tests
        ],
    }


def write_result_ledger(path: Path, ledger: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"result ledger path is an unsafe symlink: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def acquire_result_ledger_lease(path: Path, run_id: str) -> int:
    if not run_id or "\n" in run_id:
        raise RuntimeError("test result run identity is invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow:
        raise RuntimeError("safe test result locking is unsupported")
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | nofollow | cloexec,
            0o600,
        )
    except OSError as error:
        raise RuntimeError(f"test result lease is unsafe: {lock_path}") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise RuntimeError(f"test result lease must be a private file: {lock_path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                f"another test runner owns the result ledger: {path}"
            ) from error
        payload = (
            json.dumps(
                {"pid": os.getpid(), "run_id": run_id},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        os.ftruncate(descriptor, 0)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise RuntimeError("test result lease write made no progress")
            written += count
        os.fsync(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def release_result_ledger_lease(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def run_registered_tests(
    *,
    root: Path,
    parallel: list[str],
    serial: list[str],
    mode: str,
    jobs: int,
    timeout_seconds: float,
    result_ledger: Path,
    run_id: str,
    started_at: str,
) -> int:
    write_result_ledger(
        result_ledger,
        build_incomplete_ledger(
            run_id=run_id,
            started_at=started_at,
            mode=mode,
            requested_jobs=jobs,
            parallel_worker_count=min(jobs, len(parallel)),
            timeout_seconds=timeout_seconds,
            parallel_tests=parallel,
            serial_tests=serial,
        ),
    )

    parallel_results = run_parallel(
        root,
        parallel,
        jobs=jobs,
        timeout_seconds=timeout_seconds,
    )
    for result in parallel_results:
        emit(result)

    if any(not result.passed for result in parallel_results):
        serial_results = [
            _cancelled_result(relative, "a parallel-safe test failed")
            for relative in serial
        ]
    else:
        serial_results = run_serial(
            root,
            serial,
            timeout_seconds=timeout_seconds,
        )
        for result in serial_results:
            emit(result)

    ledger = build_result_ledger(
        run_id=run_id,
        started_at=started_at,
        completed_at=utc_now(),
        mode=mode,
        requested_jobs=jobs,
        parallel_worker_count=min(jobs, len(parallel)),
        timeout_seconds=timeout_seconds,
        parallel_results=parallel_results,
        serial_results=serial_results,
    )
    write_result_ledger(result_ledger, ledger)
    print(f"test result ledger: {result_ledger}")

    failures = [*parallel_results, *serial_results]
    failures = [result for result in failures if not result.passed]
    if failures:
        print(
            "test failures: "
            + ",".join(f"{result.path}({result.status})" for result in failures),
            file=sys.stderr,
        )
        return 1
    return 0


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=10)
    parser.add_argument("--mode", choices=("diagnostic", "qualification"), default="qualification")
    parser.add_argument("--changed-file", action="append", default=[])
    parser.add_argument(
        "--test-timeout-seconds",
        type=positive_float,
        default=positive_float(os.environ.get("OMARCHY_TEST_TIMEOUT_SECONDS", "300")),
    )
    parser.add_argument(
        "--result-ledger",
        type=Path,
        default=Path(
            os.environ.get(
                "OMARCHY_TEST_RESULT_LEDGER",
                "test-runs/unit-test-results.json",
            )
        ),
    )
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.jobs <= 0:
        parser.error("--jobs must be positive")

    root = Path(__file__).resolve().parents[1]
    parallel = read_manifest(root / "test/parallel-safe.tests")
    serial = read_manifest(root / "test/serial.tests")
    validate(root, parallel, serial)
    if args.changed_file:
        changed = ",".join(sorted(set(args.changed_file)))
        print(
            f"advisory: changed files={changed}; mode={args.mode}; manifest selection remains complete",
            file=sys.stderr,
        )
    if args.validate_only:
        print(f"validated {len(parallel)} parallel-safe and {len(serial)} serial tests")
        return 0

    result_ledger = args.result_ledger
    if not result_ledger.is_absolute():
        result_ledger = root / result_ledger
    run_id = uuid.uuid4().hex
    started_at = utc_now()
    try:
        lease = acquire_result_ledger_lease(result_ledger, run_id)
    except RuntimeError as error:
        print(f"test runner refused to start: {error}", file=sys.stderr)
        return 2
    try:
        return run_registered_tests(
            root=root,
            parallel=parallel,
            serial=serial,
            mode=args.mode,
            jobs=args.jobs,
            timeout_seconds=args.test_timeout_seconds,
            result_ledger=result_ledger,
            run_id=run_id,
            started_at=started_at,
        )
    finally:
        release_result_ledger_lease(lease)


if __name__ == "__main__":
    raise SystemExit(main())
