from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import ROOT, read_json, write_json


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


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX owner permissions")
def test_unreadable_suite_is_a_stable_cli_error_without_traceback(
    suite_copy: Path, tmp_path: Path
) -> None:
    suite_copy.chmod(0)
    try:
        result = _run_cli(suite_copy, tmp_path / "out")
    finally:
        suite_copy.chmod(0o600)
    assert result.returncode == 2
    assert "ERROR code=SUITE_NOT_FOUND" in result.stderr
    assert "Traceback" not in result.stderr


def test_nul_in_trace_path_is_a_stable_cli_contract_error(suite_copy: Path, tmp_path: Path) -> None:
    payload = read_json(suite_copy)
    payload["cases"][0]["baseline_trace"] = "traces/bad\u0000.jsonl"
    write_json(suite_copy, payload)
    result = _run_cli(suite_copy, tmp_path / "out")
    assert result.returncode == 2
    assert "ERROR code=SUITE_TRACE_PATH_INVALID" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="symlink loop contract is POSIX-tested")
def test_suite_symlink_loop_is_a_bounded_cli_error_without_artifacts(tmp_path: Path) -> None:
    suite = tmp_path / "suite-loop.json"
    suite.symlink_to(suite.name)
    output = tmp_path / "out"

    result = _run_cli(suite, output)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        f"ERROR code=SUITE_NOT_FOUND location={suite} message=suite not found\n"
    )
    assert result.stderr.count("\n") == 1
    assert len(result.stderr.encode("utf-8")) < 2_048
    assert "Traceback" not in result.stderr
    assert not output.exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink loop contract is POSIX-tested")
def test_trace_symlink_loop_is_a_bounded_cli_error_without_artifacts(
    suite_copy: Path, tmp_path: Path
) -> None:
    trace = suite_copy.parent / "traces/trace-loop.jsonl"
    trace.symlink_to(trace.name)
    payload = read_json(suite_copy)
    payload["cases"][0]["baseline_trace"] = "traces/trace-loop.jsonl"
    write_json(suite_copy, payload)
    output = tmp_path / "out"

    result = _run_cli(suite_copy, output)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        f"ERROR code=SUITE_TRACE_PATH_INVALID location={suite_copy} message=invalid trace path\n"
    )
    assert result.stderr.count("\n") == 1
    assert len(result.stderr.encode("utf-8")) < 2_048
    assert "Traceback" not in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("target", ["suite", "trace"])
def test_deeply_nested_json_is_rejected_without_recursion_traceback(
    suite_copy: Path, tmp_path: Path, target: str
) -> None:
    nested = "[" * 1500 + "0" + "]" * 1500
    if target == "suite":
        suite_copy.write_text(
            '{"schema_version":"1.0","suite_id":"deep","cases":[],"extra":' + nested + "}",
            encoding="utf-8",
        )
        expected = "SUITE_JSON_INVALID"
    else:
        trace = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
        trace.write_text(
            '{"schema_version":"1.0","case_id":"bounded-update","step":1,'
            '"kind":"result","status":"completed","output":' + nested + "}\n",
            encoding="utf-8",
        )
        expected = "TRACE_JSON_INVALID"
    result = _run_cli(suite_copy, tmp_path / "out")
    assert result.returncode == 2
    assert f"ERROR code={expected}" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="symlink exchange contract is POSIX-tested")
def test_resolved_trace_target_is_not_changed_by_later_symlink_exchange(
    suite_copy: Path, tmp_path: Path
) -> None:
    from toolcall_replay.report import build_report
    from toolcall_replay.suite import load_suite

    traces = suite_copy.parent / "traces"
    link = traces / "baseline-link.jsonl"
    link.symlink_to(traces / "bounded-update.baseline.jsonl")
    payload = read_json(suite_copy)
    payload["cases"][0]["baseline_trace"] = "traces/baseline-link.jsonl"
    write_json(suite_copy, payload)
    suite = load_suite(suite_copy)

    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        (traces / "bounded-update.candidate.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    link.unlink()
    link.symlink_to(outside)

    report: Any = build_report(suite)
    assert report["cases"][0]["baseline"]["verdict"] == "PASS"


@pytest.mark.skipif(os.name == "nt", reason="symlink exchange contract is POSIX-tested")
def test_loaded_trace_is_not_changed_by_later_resolved_target_exchange(
    suite_copy: Path, tmp_path: Path
) -> None:
    from toolcall_replay.report import build_report
    from toolcall_replay.suite import load_suite

    target = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        (suite_copy.parent / "traces/bounded-update.candidate.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    suite = load_suite(suite_copy)
    target.unlink()
    target.symlink_to(outside)

    report: Any = build_report(suite)
    assert report["cases"][0]["baseline"]["verdict"] == "PASS"


def test_integral_json_numbers_are_normalized_for_integer_control_fields(
    suite_copy: Path,
) -> None:
    from toolcall_replay.suite import load_suite
    from toolcall_replay.trace import load_trace

    payload = read_json(suite_copy)
    payload["cases"][0]["rules"][0]["min_calls"] = 0.0
    payload["cases"][0]["rules"][0]["max_calls"] = 1.0
    write_json(suite_copy, payload)
    suite = load_suite(suite_copy)
    presence = suite.cases[0].rules[0]
    assert type(presence.data["min_calls"]) is int
    assert type(presence.data["max_calls"]) is int

    trace_path = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    lines = trace_path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines, 1):
        lines[index - 1] = line.replace(f'"step":{index}', f'"step":{index}.0')
    trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    trace = load_trace(trace_path, "bounded-update")
    assert [event.step for event in trace.events] == [1, 2, 3, 4, 5]
    assert all(type(event.step) is int for event in trace.events)


def test_all_trace_paths_are_rejected_before_any_trace_is_parsed(
    suite_copy: Path, tmp_path: Path
) -> None:
    baseline = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    baseline.write_text('{"schema_version":"1.0"', encoding="utf-8")
    payload = read_json(suite_copy)
    payload["cases"][0]["candidate_trace"] = "../outside.jsonl"
    write_json(suite_copy, payload)

    result = _run_cli(suite_copy, tmp_path / "out")
    assert result.returncode == 2
    assert "ERROR code=SUITE_TRACE_PATH_INVALID" in result.stderr
    assert "TRACE_TRUNCATED" not in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="FIFO substitution contract is POSIX-tested")
def test_fifo_substituted_immediately_before_open_is_rejected_without_blocking(
    suite_copy: Path,
) -> None:
    script = """
import os
import sys
from pathlib import Path

import toolcall_replay.suite as suite_module
from toolcall_replay.errors import ReplayError
from toolcall_replay.trace import load_trace

suite_path = Path(sys.argv[1])
target = suite_path.parent / "traces/bounded-update.baseline.jsonl"
real_open = os.open
swapped = False

def swapping_open(path, flags, *args, **kwargs):
    global swapped
    if not swapped and path == target.name and kwargs.get("dir_fd") is not None:
        target.unlink()
        os.mkfifo(target)
        swapped = True
    return real_open(path, flags, *args, **kwargs)

suite_module.os.open = swapping_open
try:
    suite_module.load_suite(suite_path)
except ReplayError as error:
    print("suite=" + error.code)
else:
    raise SystemExit("substituted FIFO was accepted")
try:
    load_trace(target, "bounded-update")
except ReplayError as error:
    print("direct=" + error.code)
else:
    raise SystemExit("direct trace loader accepted FIFO")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(suite_copy)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
        timeout=2,
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "suite=SUITE_TRACE_PATH_INVALID",
        "direct=TRACE_NOT_FOUND",
    ]
    assert result.stderr == ""


@pytest.mark.skipif(os.name == "nt", reason="regular-file exchange contract is POSIX-tested")
@pytest.mark.parametrize("change", ["replace", "rewrite"])
def test_regular_file_changed_after_path_validation_is_rejected(
    suite_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    import toolcall_replay.suite as suite_module
    from toolcall_replay.errors import ReplayError

    baseline = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    candidate = suite_copy.parent / "traces/bounded-update.candidate.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(candidate.read_bytes())
    original_trace_path = suite_module._trace_path
    resolved_calls = 0

    def swapping_trace_path(root: Path, raw: str, location: Path) -> Any:
        nonlocal resolved_calls
        target = original_trace_path(root, raw, location)
        resolved_calls += 1
        if resolved_calls == 2:
            if change == "replace":
                os.replace(replacement, baseline)
            else:
                baseline.write_bytes(replacement.read_bytes())
        return target

    monkeypatch.setattr(suite_module, "_trace_path", swapping_trace_path)
    with pytest.raises(ReplayError) as caught:
        suite_module.load_suite(suite_copy)
    assert caught.value.code == "SUITE_TRACE_PATH_INVALID"
    assert resolved_calls == 2


def test_control_characters_in_error_locations_are_escaped_to_one_line(tmp_path: Path) -> None:
    missing = tmp_path / "missing\n\x1b[31mINJECTED=1.json"
    result = _run_cli(missing, tmp_path / "out")
    assert result.returncode == 2
    assert result.stderr.count("\n") == 1
    assert "missing\\n\\u001b[31mINJECTED=1.json" in result.stderr
    assert "\n\x1b[31mINJECTED=1.json" not in result.stderr


def test_loaded_suite_models_are_deeply_immutable(suite_copy: Path) -> None:
    from toolcall_replay.report import build_report
    from toolcall_replay.suite import load_suite

    suite = load_suite(suite_copy)
    case = suite.cases[0]
    event_data: Any = case.baseline_trace.events[0].data
    rule_data: Any = case.rules[3].data
    allowed_values: Any = case.rules[2].data["allowed_values"]

    with pytest.raises(TypeError):
        event_data["arguments"]["scope"] = "all"
    with pytest.raises(TypeError):
        rule_data["tool"] = "request.lookup"
    with pytest.raises(AttributeError):
        allowed_values.append("all")

    report: Any = build_report(suite)
    baseline_failures = [
        rule["rule_id"]
        for rule in report["cases"][0]["baseline"]["rules"]
        if rule["verdict"] == "FAIL"
    ]
    assert baseline_failures == []


@pytest.mark.skipif(os.name == "nt", reason="suite-root exchange contract is POSIX-tested")
def test_suite_root_substituted_after_suite_read_is_rejected(
    suite_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import toolcall_replay.suite as suite_module
    from toolcall_replay.errors import ReplayError

    current = suite_copy.parent
    moved = tmp_path / "original-moved"
    alternate = tmp_path / "alternate"
    shutil.copytree(current, alternate)
    alternate_baseline = alternate / "traces/bounded-update.baseline.jsonl"
    alternate_candidate = alternate / "traces/bounded-update.candidate.jsonl"
    alternate_baseline.write_bytes(alternate_candidate.read_bytes())
    original_loads: Any = getattr(suite_module, "loads_strict")
    swapped = False

    def swapping_loads(text: str, code: str, location: str) -> Any:
        nonlocal swapped
        value = original_loads(text, code, location)
        if not swapped:
            current.rename(moved)
            alternate.rename(current)
            swapped = True
        return value

    monkeypatch.setattr(suite_module, "loads_strict", swapping_loads)
    with pytest.raises(ReplayError) as caught:
        suite_module.load_suite(suite_copy)
    assert caught.value.code == "SUITE_TRACE_PATH_INVALID"
    assert swapped is True
