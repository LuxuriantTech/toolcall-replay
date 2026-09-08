from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import pytest

from conftest import ROOT, read_json, write_json


MAX_TESTED_JSON_DEPTH = 64
NestedField = Literal[
    "suite_allowed_values",
    "suite_expected_output",
    "trace_tool_arguments",
    "trace_approval_arguments",
    "trace_result_output",
]


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


def _maximum_depth(value: Any) -> int:
    maximum = 0
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        maximum = max(maximum, depth)
        if isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
        elif isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
    return maximum


def _suite_rule(payload: dict[str, Any], rule_type: str) -> dict[str, Any]:
    rules = payload["cases"][0]["rules"]
    return next(rule for rule in rules if rule["type"] == rule_type)


def _prepare_nested_input(suite: Path, field: NestedField, target_depth: int) -> tuple[str, str]:
    nested: Any = "leaf"
    if field.startswith("suite_"):
        payload = read_json(suite)
        rule_type = "argument_constraint" if field == "suite_allowed_values" else "expected_result"
        rule = _suite_rule(payload, rule_type)

        def assign(value: Any) -> None:
            if field == "suite_allowed_values":
                rule["allowed_values"] = [value]
            else:
                rule["output"] = value

        assign(nested)
        while _maximum_depth(payload) < target_depth:
            nested = [nested]
            assign(nested)
        assert _maximum_depth(payload) == target_depth
        write_json(suite, payload)
        return "SUITE_JSON_INVALID", str(suite)

    trace = suite.parent / "traces/bounded-update.baseline.jsonl"
    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    row_index = {
        "trace_tool_arguments": 0,
        "trace_approval_arguments": 1,
        "trace_result_output": 4,
    }[field]

    def assign_trace(value: Any) -> None:
        if field == "trace_result_output":
            rows[row_index]["output"] = value
        else:
            rows[row_index]["arguments"]["nested"] = value

    assign_trace(nested)
    while _maximum_depth(rows[row_index]) < target_depth:
        nested = [nested]
        assign_trace(nested)
    assert _maximum_depth(rows[row_index]) == target_depth
    trace.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows)
        + "\n",
        encoding="utf-8",
    )
    return "TRACE_JSON_INVALID", f"{trace}:line={row_index + 1}"


NESTED_FIELDS: tuple[NestedField, ...] = (
    "suite_allowed_values",
    "suite_expected_output",
    "trace_tool_arguments",
    "trace_approval_arguments",
    "trace_result_output",
)


@pytest.mark.parametrize("field", NESTED_FIELDS)
def test_nested_json_at_depth_limit_remains_valid(
    suite_copy: Path, tmp_path: Path, field: NestedField
) -> None:
    _prepare_nested_input(suite_copy, field, MAX_TESTED_JSON_DEPTH)
    output = tmp_path / "out"

    result = _run_cli(suite_copy, output)

    assert result.returncode == 1
    assert result.stderr == ""
    assert (output / "report.json").is_file()
    assert (output / "report.html").is_file()


@pytest.mark.parametrize("field", NESTED_FIELDS)
@pytest.mark.parametrize("target_depth", [MAX_TESTED_JSON_DEPTH + 1, 600])
def test_nested_json_over_depth_limit_is_a_bounded_contract_error(
    suite_copy: Path,
    tmp_path: Path,
    field: NestedField,
    target_depth: int,
) -> None:
    code, location = _prepare_nested_input(suite_copy, field, target_depth)
    output = tmp_path / "out"

    result = _run_cli(suite_copy, output)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        f"ERROR code={code} location={location} "
        f"message=JSON exceeds {MAX_TESTED_JSON_DEPTH}-level nesting limit\n"
    )
    assert len(result.stderr.encode("utf-8")) < 2_048
    assert "Traceback" not in result.stderr
    assert not output.exists()
