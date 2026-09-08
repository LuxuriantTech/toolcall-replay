from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    ROOT,
    assert_replay_error,
    production_module,
    read_json,
    replay_error,
    write_json,
)


def _suite(path: Path) -> Any:
    return production_module("suite").load_suite(path)


@pytest.mark.parametrize(
    ("rule", "label"),
    [
        ({"rule_id": "x", "type": "call_presence", "min_calls": 0, "max_calls": 1}, "missing-tool"),
        (
            {
                "rule_id": "x",
                "type": "call_presence",
                "tool": "Bad",
                "min_calls": 0,
                "max_calls": 1,
            },
            "tool-pattern",
        ),
        (
            {
                "rule_id": "x",
                "type": "call_presence",
                "tool": "request.lookup",
                "min_calls": True,
                "max_calls": 1,
            },
            "bool-bound",
        ),
        (
            {"rule_id": "x", "type": "call_order", "first_tool": "request.lookup"},
            "missing-order-tool",
        ),
        (
            {
                "rule_id": "x",
                "type": "argument_constraint",
                "tool": "request.lookup",
                "argument": "scope",
            },
            "missing-values",
        ),
        ({"rule_id": "x", "type": "forbidden_tool"}, "missing-forbidden-tool"),
        ({"rule_id": "x", "type": "approval_required"}, "missing-approval-tool"),
        ({"rule_id": "x", "type": "expected_result", "status": "completed"}, "missing-output"),
        ({"rule_id": "x", "type": "step_budget", "max_steps": -1}, "negative-budget"),
        ({"rule_id": "x", "type": "step_budget", "max_steps": True}, "bool-budget"),
    ],
)
def test_all_rule_forms_are_closed_and_type_checked(
    suite_copy: Path, rule: dict[str, object], label: str
) -> None:
    payload = read_json(suite_copy)
    payload["cases"][0]["rules"] = [rule]
    write_json(suite_copy, payload)
    with pytest.raises(replay_error()) as caught:
        _suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_SCHEMA_INVALID", str(suite_copy))


@pytest.mark.parametrize(
    ("field", "value"), [("suite_id", "Bad"), ("case_id", "Bad"), ("rule_id", "Bad")]
)
def test_identifier_patterns_are_enforced(suite_copy: Path, field: str, value: str) -> None:
    payload = read_json(suite_copy)
    target: dict[str, object] = payload if field == "suite_id" else payload["cases"][0]
    if field == "rule_id":
        target = payload["cases"][0]["rules"][0]
    target[field] = value
    write_json(suite_copy, payload)
    with pytest.raises(replay_error()) as caught:
        _suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_SCHEMA_INVALID", str(suite_copy))


@pytest.mark.parametrize(
    "mutation",
    [
        "step-bool",
        "tool-pattern",
        "case-pattern",
        "approval-empty",
        "approval-number",
        "approval-event-empty",
    ],
)
def test_trace_scalar_contracts_are_strict(suite_copy: Path, mutation: str) -> None:
    path = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if mutation == "step-bool":
        events[0]["step"] = True
    elif mutation == "tool-pattern":
        events[0]["tool"] = "Bad"
    elif mutation == "case-pattern":
        events[0]["case_id"] = "Bad"
    elif mutation == "approval-empty":
        events[2]["approval_id"] = ""
    elif mutation == "approval-number":
        events[2]["approval_id"] = 4
    else:
        events[1]["approval_id"] = ""
    path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")
    with pytest.raises(replay_error()) as caught:
        production_module("trace").load_trace(path, "bounded-update")
    assert_replay_error(caught.value, "TRACE_SCHEMA_INVALID", str(path))


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
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def test_incomplete_known_rule_is_cli_contract_error_without_traceback_or_artifacts(
    suite_copy: Path, tmp_path: Path
) -> None:
    payload = read_json(suite_copy)
    payload["cases"][0]["rules"] = [
        {"rule_id": "broken", "type": "call_presence", "min_calls": 0, "max_calls": 1}
    ]
    write_json(suite_copy, payload)
    output = tmp_path / "out"
    result = _run_cli(suite_copy, output)
    assert result.returncode == 2 and "ERROR code=SUITE_SCHEMA_INVALID" in result.stderr
    assert "Traceback" not in result.stderr and not output.exists()


def test_report_renders_before_output_directory_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = {
        "schema_version": "1.0",
        "suite_id": "suite",
        "verdict": "PASS",
        "digest": "0" * 64,
        "cases": [],
    }
    module = production_module("report")

    def broken_html(_: object) -> Iterator[str]:
        raise OSError("render fail")
        yield ""

    monkeypatch.setattr(module, "_html_fragments", broken_html)
    output = tmp_path / "absent"
    with pytest.raises(replay_error()) as caught:
        module.write_reports(report, output)
    assert_replay_error(caught.value, "REPORT_WRITE_FAILED", str(output))
    assert not output.exists()


def test_demo_does_not_require_console_script_on_minimal_path() -> None:
    result = subprocess.run(
        ["/bin/sh", "scripts/demo.sh"],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
