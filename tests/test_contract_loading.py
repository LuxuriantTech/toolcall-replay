from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from conftest import assert_replay_error, production_module, read_json, replay_error, write_json

if TYPE_CHECKING:
    from toolcall_replay.models import Suite


def _load(path: Path) -> Suite:
    return cast("Suite", production_module("suite").load_suite(path))


def test_load_suite_exposes_declared_case_rules_and_relative_traces(suite_copy: Path) -> None:
    suite = _load(suite_copy)
    assert suite.suite_id == "synthetic-internal-request"
    assert [case.case_id for case in suite.cases] == ["bounded-update"]
    assert [rule.rule_id for rule in suite.cases[0].rules] == [
        "lookup-present",
        "lookup-before-update",
        "lookup-scope",
        "no-directory-export",
        "update-needs-approval",
        "result-completed",
        "within-step-budget",
    ]


@pytest.mark.parametrize(
    "text",
    [
        '{"schema_version":"1.0","schema_version":"1.0"}',
        '{"schema_version":"1.0","suite_id":"valid-id","cases":[],"n":NaN}',
        '{"schema_version":"1.0","suite_id":"valid-id","cases":[],"n":1e400}',
        '{"schema_version":"1.0","suite_id":"\\ud800","cases":[]}',
    ],
)
def test_load_suite_rejects_noncanonical_json_with_typed_error(text: str, tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(replay_error()) as caught:
        _load(path)
    assert_replay_error(caught.value, "SUITE_JSON_INVALID", str(path))


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"schema_version": "1.0", "suite_id": "valid-id"}, "SUITE_SCHEMA_INVALID"),
        (
            {"schema_version": "1.0", "suite_id": "valid-id", "cases": [], "extra": True},
            "SUITE_SCHEMA_INVALID",
        ),
        (
            {"schema_version": "2.0", "suite_id": "valid-id", "cases": []},
            "SUITE_VERSION_UNSUPPORTED",
        ),
        ({"schema_version": "1.0", "suite_id": "valid-id", "cases": []}, "SUITE_SCHEMA_INVALID"),
    ],
)
def test_load_suite_restores_required_schema_and_version_gates(
    tmp_path: Path, payload: dict[str, object], code: str
) -> None:
    path = tmp_path / "contract.json"
    write_json(path, payload)
    with pytest.raises(replay_error()) as caught:
        _load(path)
    assert_replay_error(caught.value, code, str(path))


@pytest.mark.parametrize(
    "mutation", ["duplicate-case", "duplicate-rule", "bad-bounds", "empty-allowed"]
)
def test_load_suite_rejects_semantic_schema_violations(suite_copy: Path, mutation: str) -> None:
    payload = read_json(suite_copy)
    case = payload["cases"][0]
    if mutation == "duplicate-case":
        payload["cases"].append({**case, "name": "distinct synthetic name"})
    elif mutation == "duplicate-rule":
        case["rules"].append({**case["rules"][0]})
    elif mutation == "bad-bounds":
        case["rules"][0]["min_calls"] = 2
        case["rules"][0]["max_calls"] = 1
    else:
        case["rules"][2]["allowed_values"] = []
    write_json(suite_copy, payload)
    with pytest.raises(replay_error()) as caught:
        _load(suite_copy)
    assert_replay_error(caught.value, "SUITE_SCHEMA_INVALID", str(suite_copy))


@pytest.mark.parametrize(
    "trace_name",
    [
        "/tmp/outside.jsonl",
        "../outside.jsonl",
        "traces/../traces/bounded-update.baseline.jsonl",
    ],
)
def test_load_suite_refuses_absolute_or_escaping_trace_path(
    suite_copy: Path, trace_name: str
) -> None:
    payload = read_json(suite_copy)
    payload["cases"][0]["baseline_trace"] = trace_name
    write_json(suite_copy, payload)
    with pytest.raises(replay_error()) as caught:
        _load(suite_copy)
    assert_replay_error(caught.value, "SUITE_TRACE_PATH_INVALID", str(suite_copy))


def test_unknown_rule_is_refused_before_an_unreadable_trace(suite_copy: Path) -> None:
    payload = read_json(suite_copy)
    payload["cases"][0]["rules"][0]["type"] = "not-a-rule"
    payload["cases"][0]["baseline_trace"] = "missing.jsonl"
    write_json(suite_copy, payload)
    with pytest.raises(replay_error()) as caught:
        _load(suite_copy)
    assert_replay_error(caught.value, "SUITE_UNKNOWN_RULE", str(suite_copy))


def test_missing_suite_and_non_utf8_suite_are_typed(suite_copy: Path, tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(replay_error()) as caught:
        _load(missing)
    assert_replay_error(caught.value, "SUITE_NOT_FOUND", str(missing))
    suite_copy.write_bytes(b"\xff")
    with pytest.raises(replay_error()) as caught:
        _load(suite_copy)
    assert_replay_error(caught.value, "SUITE_JSON_INVALID", str(suite_copy))


@pytest.mark.skipif(os.name == "nt", reason="symlink containment contract is POSIX-tested")
def test_symlink_resolved_outside_suite_is_refused(suite_copy: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text("synthetic", encoding="utf-8")
    link = suite_copy.parent / "traces/outside-link.jsonl"
    link.symlink_to(outside)
    payload = read_json(suite_copy)
    payload["cases"][0]["baseline_trace"] = "traces/outside-link.jsonl"
    write_json(suite_copy, payload)
    with pytest.raises(replay_error()) as caught:
        _load(suite_copy)
    assert_replay_error(caught.value, "SUITE_TRACE_PATH_INVALID", str(suite_copy))


def test_missing_and_nonregular_trace_paths_are_refused(suite_copy: Path) -> None:
    payload = read_json(suite_copy)
    payload["cases"][0]["baseline_trace"] = "traces/missing.jsonl"
    write_json(suite_copy, payload)
    with pytest.raises(replay_error()) as caught:
        _load(suite_copy)
    assert_replay_error(caught.value, "SUITE_TRACE_PATH_INVALID", str(suite_copy))
    directory = suite_copy.parent / "traces/not-a-file"
    directory.mkdir()
    payload["cases"][0]["baseline_trace"] = "traces/not-a-file"
    write_json(suite_copy, payload)
    with pytest.raises(replay_error()) as caught:
        _load(suite_copy)
    assert_replay_error(caught.value, "SUITE_TRACE_PATH_INVALID", str(suite_copy))
