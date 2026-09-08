from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from conftest import assert_replay_error, production_module, replay_error

if TYPE_CHECKING:
    from toolcall_replay.models import Trace


def _load(path: Path) -> Trace:
    return cast("Trace", production_module("trace").load_trace(path, "bounded-update"))


def _replace_last(path: Path, line: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = line
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_load_trace_preserves_order_and_requires_unique_final_result(suite_copy: Path) -> None:
    trace = _load(suite_copy.parent / "traces/bounded-update.baseline.jsonl")
    assert [event.step for event in trace.events] == [1, 2, 3, 4, 5]
    assert trace.events[-1].kind == "result"


@pytest.mark.parametrize(
    "last_line",
    [
        '{"schema_version":"1.0","case_id":"bounded-update","step":5',
        '{"a":{"b":1}',
    ],
)
def test_only_syntax_error_in_final_nonempty_record_is_truncated(
    suite_copy: Path, last_line: str
) -> None:
    path = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    _replace_last(path, last_line)
    with pytest.raises(replay_error()) as caught:
        _load(path)
    assert_replay_error(caught.value, "TRACE_TRUNCATED", f"{path}:line=5")


@pytest.mark.parametrize(
    "last_line",
    [
        '{"schema_version":"1.0","case_id":"bounded-update","case_id":"bounded-update","step":5,"kind":"result","status":"completed","output":{}}',
        '{"schema_version":"1.0","case_id":"bounded-update","step":5,"kind":"result","status":"completed","output":NaN}',
        '{"schema_version":"1.0","case_id":"bounded-update","step":5,"kind":"result","status":"completed","output":1e400}',
        '{"schema_version":"1.0","case_id":"bounded-update","step":5,"kind":"result","status":"completed","output":"\\ud800"}',
    ],
)
def test_invalid_json_forms_even_on_final_line_are_not_truncated(
    suite_copy: Path, last_line: str
) -> None:
    path = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    _replace_last(path, last_line)
    with pytest.raises(replay_error()) as caught:
        _load(path)
    assert_replay_error(caught.value, "TRACE_JSON_INVALID", str(path) + ":line=5")


@pytest.mark.parametrize(
    "mutation",
    [
        "gap",
        "wrong-case",
        "no-result",
        "two-results",
        "duplicate-approval",
        "unknown-field",
        "blank-line",
    ],
)
def test_load_trace_rejects_structural_contract_errors(suite_copy: Path, mutation: str) -> None:
    path = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    if mutation == "gap":
        lines[2] = lines[2].replace('"step":3', '"step":4')
    elif mutation == "wrong-case":
        lines[-1] = lines[-1].replace("bounded-update", "wrong-case")
    elif mutation == "no-result":
        lines.pop()
    elif mutation == "two-results":
        lines.insert(-1, lines[-1].replace('"step":5', '"step":5'))
        lines[-1] = lines[-1].replace('"step":5', '"step":6')
    elif mutation == "duplicate-approval":
        lines.insert(
            2,
            '{"schema_version":"1.0","case_id":"bounded-update","step":3,"kind":"approval","approval_id":"update-1","status":"granted","tool":"request.update","arguments":{"scope":"team:synthetic","status":"updated"}}',
        )
        for index in range(3, len(lines)):
            step = index + 1
            lines[index] = lines[index].replace(f'"step":{step - 1}', f'"step":{step}')
    elif mutation == "unknown-field":
        lines[0] = lines[0][:-1] + ',"extra":true}'
    else:
        lines.insert(2, "")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(replay_error()) as caught:
        _load(path)
    assert_replay_error(caught.value, "TRACE_SCHEMA_INVALID", str(path))


def test_syntax_error_before_final_line_is_json_invalid(suite_copy: Path) -> None:
    path = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = '{"schema_version":"1.0"'
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(replay_error()) as caught:
        _load(path)
    assert_replay_error(caught.value, "TRACE_JSON_INVALID", str(path) + ":line=2")


def test_non_utf8_trace_is_encoding_invalid(suite_copy: Path) -> None:
    path = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    path.write_bytes(b"\xff")
    with pytest.raises(replay_error()) as caught:
        _load(path)
    assert_replay_error(caught.value, "TRACE_ENCODING_INVALID", str(path))


def test_missing_trace_is_not_found(suite_copy: Path) -> None:
    path = suite_copy.parent / "traces/missing.jsonl"
    with pytest.raises(replay_error()) as caught:
        _load(path)
    assert_replay_error(caught.value, "TRACE_NOT_FOUND", str(path))
