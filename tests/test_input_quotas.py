from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from conftest import (
    ROOT,
    assert_replay_error,
    production_module,
    read_json,
    replay_error,
    write_json,
)

SUITE_LIMIT = 1_000_000
TRACE_LIMIT = 1_000_000
TOTAL_TRACE_LIMIT = 8_000_000


def _pad_suite(path: Path, size: int) -> None:
    content = path.read_bytes()
    assert len(content) <= size
    path.write_bytes(content + b" " * (size - len(content)))


def _pad_trace(path: Path, size: int) -> None:
    content = path.read_bytes()
    assert content.endswith(b"\n") and len(content) <= size
    path.write_bytes(content[:-1] + b" " * (size - len(content)) + b"\n")


def _run_cli(suite: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "toolcall_replay.cli",
            "evaluate",
            str(suite),
            "--output-dir",
            str(output),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def test_suite_accepts_exact_byte_limit_and_rejects_next_byte(suite_copy: Path) -> None:
    load_suite = production_module("suite").load_suite
    _pad_suite(suite_copy, SUITE_LIMIT)
    assert load_suite(suite_copy).suite_id == "synthetic-internal-request"

    suite_copy.write_bytes(suite_copy.read_bytes() + b" ")
    with pytest.raises(replay_error()) as caught:
        load_suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_TOO_LARGE", str(suite_copy))


def test_trace_accepts_exact_byte_limit_and_rejects_next_byte(suite_copy: Path) -> None:
    load_trace = production_module("trace").load_trace
    trace = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    _pad_trace(trace, TRACE_LIMIT)
    assert len(load_trace(trace, "bounded-update").events) == 5

    _pad_trace(trace, TRACE_LIMIT + 1)
    with pytest.raises(replay_error()) as caught:
        load_trace(trace, "bounded-update")
    assert_replay_error(caught.value, "TRACE_TOO_LARGE", str(trace))


def test_suite_size_is_checked_on_open_descriptor_before_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = production_module("suite")
    suite = tmp_path / "oversized.json"
    with suite.open("wb") as output:
        output.truncate(SUITE_LIMIT + 1)

    def unexpected_fdopen(*_: object, **__: object) -> None:
        raise AssertionError("oversized suite reached fdopen")

    monkeypatch.setattr(module.os, "fdopen", unexpected_fdopen)
    with pytest.raises(replay_error()) as caught:
        module.load_suite(suite)
    assert_replay_error(caught.value, "SUITE_TOO_LARGE", str(suite))


def test_trace_size_is_checked_on_open_descriptor_before_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = production_module("trace")
    trace = tmp_path / "oversized.jsonl"
    with trace.open("wb") as output:
        output.truncate(TRACE_LIMIT + 1)

    def unexpected_fdopen(*_: object, **__: object) -> None:
        raise AssertionError("oversized trace reached fdopen")

    monkeypatch.setattr(module.os, "fdopen", unexpected_fdopen)
    with pytest.raises(replay_error()) as caught:
        module.load_trace(trace, "bounded-update")
    assert_replay_error(caught.value, "TRACE_TOO_LARGE", str(trace))


def test_suite_read_itself_is_bounded(monkeypatch: pytest.MonkeyPatch, suite_copy: Path) -> None:
    module = production_module("suite")
    real_fdopen = module.os.fdopen
    suite_limit = SUITE_LIMIT - 1
    read_arguments: list[tuple[object, ...]] = []

    class RecordingSource:
        def __init__(self, source: Any) -> None:
            self.source = source

        def __enter__(self) -> RecordingSource:
            self.source.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self.source.__exit__(*args)

        def read(self, *args: object) -> object:
            read_arguments.append(args)
            return self.source.read(*args)

        def fileno(self) -> int:
            return cast(int, self.source.fileno())

    def recording_fdopen(*args: Any, **kwargs: Any) -> RecordingSource:
        return RecordingSource(real_fdopen(*args, **kwargs))

    monkeypatch.setattr(module, "MAX_SUITE_BYTES", suite_limit)
    monkeypatch.setattr(module.os, "fdopen", recording_fdopen)
    assert module.load_suite(suite_copy).suite_id == "synthetic-internal-request"
    assert read_arguments[0] == (suite_limit + 1,)


def test_trace_read_itself_is_bounded(monkeypatch: pytest.MonkeyPatch, suite_copy: Path) -> None:
    module = production_module("trace")
    trace = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    real_fdopen = module.os.fdopen
    read_arguments: list[tuple[object, ...]] = []

    class RecordingSource:
        def __init__(self, source: Any) -> None:
            self.source = source

        def __enter__(self) -> RecordingSource:
            self.source.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self.source.__exit__(*args)

        def read(self, *args: object) -> object:
            read_arguments.append(args)
            return self.source.read(*args)

        def fileno(self) -> int:
            return cast(int, self.source.fileno())

    def recording_fdopen(*args: Any, **kwargs: Any) -> RecordingSource:
        return RecordingSource(real_fdopen(*args, **kwargs))

    monkeypatch.setattr(module.os, "fdopen", recording_fdopen)
    assert len(module.load_trace(trace, "bounded-update").events) == 5
    assert read_arguments == [(TRACE_LIMIT + 1,)]


def test_repeated_trace_references_are_rejected_before_any_trace_read(
    monkeypatch: pytest.MonkeyPatch, suite_copy: Path
) -> None:
    module = production_module("suite")
    trace = suite_copy.parent / "traces/repeated.jsonl"
    with trace.open("wb") as output:
        output.truncate(900_000)
    payload = read_json(suite_copy)
    template = payload["cases"][0]
    payload["cases"] = []
    for index in range(5):
        case = copy.deepcopy(template)
        case["case_id"] = f"case-{index}"
        case["baseline_trace"] = "traces/repeated.jsonl"
        case["candidate_trace"] = "traces/repeated.jsonl"
        payload["cases"].append(case)
    write_json(suite_copy, payload)

    def unexpected_load(*_: object, **__: object) -> None:
        raise AssertionError("aggregate trace budget was checked after a trace read")

    monkeypatch.setattr(module, "_load_confined_trace", unexpected_load)
    with pytest.raises(replay_error()) as caught:
        module.load_suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_TRACE_BUDGET_EXCEEDED", str(suite_copy))


def test_multibyte_suite_quota_counts_encoded_bytes(suite_copy: Path) -> None:
    payload = read_json(suite_copy)
    payload["cases"][0]["name"] = "é" * 500_000
    write_json(suite_copy, payload)
    assert len(suite_copy.read_text(encoding="utf-8")) < SUITE_LIMIT
    assert suite_copy.stat().st_size > SUITE_LIMIT

    with pytest.raises(replay_error()) as caught:
        production_module("suite").load_suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_TOO_LARGE", str(suite_copy))


def test_quota_error_is_bounded_and_writes_no_report(suite_copy: Path, tmp_path: Path) -> None:
    _pad_suite(suite_copy, SUITE_LIMIT + 1)
    output = tmp_path / "out"
    result = _run_cli(suite_copy, output)
    assert result.returncode == 2
    assert result.stderr == (
        f"ERROR code=SUITE_TOO_LARGE location={suite_copy} "
        "message=suite exceeds 1000000-byte limit\n"
    )
    assert len(result.stderr.encode("utf-8")) < 2_048
    assert not output.exists()


def test_all_cli_error_fields_have_an_explicit_output_bound(tmp_path: Path) -> None:
    missing = tmp_path / ("x" * 5_000)
    result = _run_cli(missing, tmp_path / "out")
    assert result.returncode == 2
    assert len(result.stderr.encode("utf-8")) < 2_048
    assert result.stderr.endswith("... message=suite not found\n")
    assert result.stderr.count("\n") == 1
