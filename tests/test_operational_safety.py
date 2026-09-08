from __future__ import annotations

import json
import os
import shutil
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


def _fd_count() -> int:
    return len(list(Path("/proc/self/fd").iterdir()))


@pytest.mark.parametrize(
    "rule",
    [
        {
            "rule_id": "x",
            "type": "call_presence",
            "tool": "request.lookup",
            "min_calls": -1,
            "max_calls": 1,
        },
        {
            "rule_id": "x",
            "type": "call_presence",
            "tool": "request.lookup",
            "min_calls": 0,
            "max_calls": -1,
        },
        {
            "rule_id": "x",
            "type": "call_presence",
            "tool": "request.lookup",
            "min_calls": 2,
            "max_calls": 1,
        },
        {
            "rule_id": "x",
            "type": "call_presence",
            "tool": "request.lookup",
            "min_calls": 0,
            "max_calls": True,
        },
        {"rule_id": "x", "type": "call_order", "first_tool": "Bad", "then_tool": "request.update"},
        {
            "rule_id": "x",
            "type": "argument_constraint",
            "tool": "request.lookup",
            "argument": "",
            "allowed_values": ["x"],
        },
        {
            "rule_id": "x",
            "type": "argument_constraint",
            "tool": "request.lookup",
            "argument": "scope",
            "allowed_values": "x",
        },
        {"rule_id": "x", "type": "expected_result", "status": "other", "output": None},
        {"rule_id": "x", "type": "expected_result", "status": "completed"},
        {"rule_id": "x", "type": "step_budget", "max_steps": 1.5},
        {"rule_id": "x", "type": "step_budget", "max_steps": "1"},
        {"rule_id": "x", "type": "step_budget", "max_steps": True},
        {"rule_id": "x", "type": "step_budget", "max_steps": -1},
    ],
)
def test_remaining_rule_scalars_are_closed(suite_copy: Path, rule: dict[str, object]) -> None:
    data = read_json(suite_copy)
    data["cases"][0]["rules"] = [rule]
    write_json(suite_copy, data)
    with pytest.raises(replay_error()) as caught:
        production_module("suite").load_suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_SCHEMA_INVALID", str(suite_copy))


@pytest.mark.parametrize(
    ("field", "value"), [("name", ""), ("name", 1), ("baseline_trace", ""), ("candidate_trace", 1)]
)
def test_case_scalars_are_closed(suite_copy: Path, field: str, value: object) -> None:
    data = read_json(suite_copy)
    data["cases"][0][field] = value
    write_json(suite_copy, data)
    with pytest.raises(replay_error()) as caught:
        production_module("suite").load_suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_SCHEMA_INVALID", str(suite_copy))


def test_diff_has_ordered_new_resolved_unchanged_partition(
    monkeypatch: pytest.MonkeyPatch, suite_copy: Path
) -> None:
    module = production_module("report")
    models = production_module("models")
    suite = production_module("suite").load_suite(suite_copy)
    rules = [
        models.RuleResult(name, "forbidden_tool", verdict, "m", {})
        for name, verdict in [
            ("lookup-present", "PASS"),
            ("lookup-before-update", "FAIL"),
            ("lookup-scope", "FAIL"),
        ]
    ]
    candidate = [
        models.RuleResult(name, "forbidden_tool", verdict, "m", {})
        for name, verdict in [
            ("lookup-present", "FAIL"),
            ("lookup-before-update", "PASS"),
            ("lookup-scope", "FAIL"),
        ]
    ]
    values = iter(
        [models.Evaluation("FAIL", tuple(rules)), models.Evaluation("FAIL", tuple(candidate))]
    )
    monkeypatch.setattr(module, "evaluate_trace", lambda *_: next(values))
    report = module.build_report(suite)
    diff = report["cases"][0]["diff"]
    assert diff == {
        "new_failures": ["lookup-present"],
        "resolved_failures": ["lookup-before-update"],
        "unchanged_failures": ["lookup-scope"],
    }
    all_ids = diff["new_failures"] + diff["resolved_failures"] + diff["unchanged_failures"]
    assert len(all_ids) == len(set(all_ids))


@pytest.mark.parametrize("statement", ["import yaml", "import urllib"])
def test_audit_rejects_each_forbidden_import_with_diagnostic(
    tmp_path: Path, statement: str
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "audit_runtime_imports.py"
    shutil.copy(ROOT / "scripts/audit_runtime_imports.py", script)
    runtime = tmp_path / "src/toolcall_replay"
    runtime.mkdir(parents=True)
    (runtime / "bad.py").write_text(statement + "\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = []\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, text=True, capture_output=True, check=False
    )
    assert result.returncode != 0 and "forbidden runtime imports" in result.stderr


def test_second_replace_failure_leaves_parseable_new_json_and_old_html(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = production_module("report")
    output = tmp_path / "out"
    output.mkdir()
    (output / "report.html").write_text("old", encoding="utf-8")
    report = {
        "schema_version": "1.0",
        "suite_id": "suite",
        "verdict": "PASS",
        "digest": "0" * 64,
        "cases": [],
    }
    real = module.os.replace
    count = 0

    def replace(source: str, destination: str) -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("fail")
        real(source, destination)

    monkeypatch.setattr(module.os, "replace", replace)
    with pytest.raises(replay_error()) as caught:
        module.write_reports(report, output)
    assert_replay_error(caught.value, "REPORT_WRITE_FAILED", str(output))
    assert json.loads((output / "report.json").read_text(encoding="utf-8")) == report
    assert (output / "report.html").read_text(encoding="utf-8") == "old" and not list(
        output.glob(".toolcall-replay-*")
    )


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux FD accounting")
def test_atomic_report_closes_descriptor_when_fdopen_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = production_module("report")
    captured: list[int] = []

    def broken_fdopen(descriptor: int, *args: object, **kwargs: object) -> None:
        captured.append(descriptor)
        raise OSError("fdopen failed")

    monkeypatch.setattr(module.os, "fdopen", broken_fdopen)
    before = _fd_count()
    try:
        with pytest.raises(replay_error()) as caught:
            module.write_reports(
                {
                    "schema_version": "1.0",
                    "suite_id": "suite",
                    "verdict": "PASS",
                    "digest": "0" * 64,
                    "cases": [],
                },
                tmp_path / "out",
            )
        assert_replay_error(caught.value, "REPORT_WRITE_FAILED", str(tmp_path / "out"))
        assert _fd_count() == before
    finally:
        for descriptor in captured:
            try:
                os.close(descriptor)
            except OSError:
                pass


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux FD accounting")
def test_trace_loader_closes_descriptor_when_fdopen_fails(
    monkeypatch: pytest.MonkeyPatch, suite_copy: Path
) -> None:
    module = production_module("trace")
    trace_path = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    captured: list[int] = []

    def broken_fdopen(descriptor: int, *args: object, **kwargs: object) -> None:
        captured.append(descriptor)
        raise OSError("fdopen failed")

    monkeypatch.setattr(module.os, "fdopen", broken_fdopen)
    before = _fd_count()
    try:
        with pytest.raises(replay_error()) as caught:
            module.load_trace(trace_path, "bounded-update")
        assert_replay_error(caught.value, "TRACE_NOT_FOUND", str(trace_path))
        assert _fd_count() == before
    finally:
        for descriptor in captured:
            try:
                os.close(descriptor)
            except OSError:
                pass


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux FD accounting")
def test_suite_loader_closes_all_descriptors_when_fdopen_raises_unexpectedly(
    monkeypatch: pytest.MonkeyPatch, suite_copy: Path
) -> None:
    module = production_module("suite")
    real_open = module.os.open
    opened: list[int] = []

    def recording_open(*args: Any, **kwargs: Any) -> int:
        descriptor = cast(int, real_open(*args, **kwargs))
        opened.append(descriptor)
        return descriptor

    def broken_fdopen(descriptor: int, *args: object, **kwargs: object) -> None:
        raise RuntimeError(f"fdopen failed for {descriptor}")

    monkeypatch.setattr(module.os, "open", recording_open)
    monkeypatch.setattr(module.os, "fdopen", broken_fdopen)
    before = _fd_count()
    try:
        with pytest.raises(RuntimeError, match="fdopen failed"):
            module.load_suite(suite_copy)
        assert _fd_count() == before
    finally:
        for descriptor in opened:
            try:
                os.close(descriptor)
            except OSError:
                pass


def test_demo_contract_uses_tempdir_and_artifact_root() -> None:
    text = (ROOT / "scripts/demo.sh").read_text(encoding="utf-8")
    assert "rm -rf" not in text and "mktemp -d" in text and "ARTIFACT_DIR" in text


def test_demo_runs_twice_with_distinct_absolute_artifact_dirs_under_minimal_path(
    tmp_path: Path,
) -> None:
    first, second = (tmp_path / "first").resolve(), (tmp_path / "second").resolve()
    for artifact in (first, second):
        result = subprocess.run(
            ["/bin/sh", "scripts/demo.sh"],
            cwd=ROOT,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(ROOT / "src"),
                "ARTIFACT_DIR": str(artifact),
            },
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert artifact.is_dir()
    assert first != second
