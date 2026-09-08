from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from toolcall_replay.jsonutil import JsonValue, json_node_count
from toolcall_replay.report import build_report
from toolcall_replay.suite import load_suite
from toolcall_replay.trace import MAX_TRACE_JSON_NODES
from toolcall_replay_lab.adapter import (
    MAX_REQUEST_BYTES,
    MAX_UPLOAD_BYTES,
    LabError,
    ReplayLabAdapter,
)

from conftest import ROOT


def test_adapter_lists_the_two_closed_synthetic_scenarios() -> None:
    catalog = ReplayLabAdapter().list_scenarios()

    assert catalog["schema_version"] == "1.0"
    assert [scenario["id"] for scenario in catalog["scenarios"]] == [
        "approved-update",
        "risky-broad-update",
    ]
    assert catalog["scenarios"][0]["tone"] == "safe"
    assert catalog["scenarios"][1]["tone"] == "caution"
    assert [scenario["expected_verdict"] for scenario in catalog["scenarios"]] == [
        "PASS",
        "FAIL",
    ]
    assert catalog["limits"] == {
        "max_upload_bytes": 1_000_000,
        "max_events": 2_000,
        "max_json_nodes": 50_000,
        "max_json_depth": 63,
    }


def _request(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def test_builtin_safe_scenario_uses_the_real_report_and_trace_events() -> None:
    response = ReplayLabAdapter().replay_bytes(
        _request({"scenario_id": "approved-update", "source": "builtin"})
    )

    assert response["schema_version"] == "1.0"
    assert response["scenario"]["id"] == "approved-update"
    assert response["provenance"] == {
        "baseline": "versioned synthetic fixture",
        "candidate": "versioned approved synthetic fixture",
        "kind": "builtin",
    }
    assert response["report"]["verdict"] == "PASS"
    assert response["report"]["cases"][0]["diff"] == {
        "new_failures": [],
        "resolved_failures": [],
        "unchanged_failures": [],
    }
    assert response["timeline"]["baseline"] == response["timeline"]["candidate"]
    assert [event["step"] for event in response["timeline"]["candidate"]] == [1, 2, 3, 4, 5]
    assert response["determinism"] == {
        "verified": True,
        "digest": response["report"]["digest"],
        "runs_compared": 2,
    }


def test_builtin_risky_scenario_exposes_exact_engine_divergences() -> None:
    response = ReplayLabAdapter().replay_bytes(
        _request({"scenario_id": "risky-broad-update", "source": "builtin"})
    )

    assert response["report"]["verdict"] == "FAIL"
    assert response["report"]["cases"][0]["diff"]["new_failures"] == [
        "lookup-scope",
        "no-directory-export",
        "update-needs-approval",
    ]
    candidate = response["timeline"]["candidate"]
    assert candidate[0]["arguments"] == {"scope": "all"}
    assert candidate[1]["tool"] == "directory.export"
    assert "approval_id" not in candidate[3]

    expected = build_report(load_suite(ROOT / "examples/synthetic-suite.json"))
    assert response["report"] == expected


def _events(path: Path) -> list[dict[str, JsonValue]]:
    return [
        cast(dict[str, JsonValue], json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_upload_replaces_only_the_candidate_then_uses_the_real_engine() -> None:
    safe_events = _events(ROOT / "examples/traces/bounded-update.baseline.jsonl")
    response = ReplayLabAdapter().replay_bytes(
        _request(
            {
                "scenario_id": "risky-broad-update",
                "source": "upload",
                "trace": {"schema_version": "1.0", "events": safe_events},
            }
        )
    )

    assert response["scenario"]["id"] == "risky-broad-update"
    assert response["imported"] is True
    assert response["provenance"] == {
        "baseline": "versioned synthetic fixture",
        "candidate": "local bounded upload",
        "kind": "upload",
    }
    assert response["report"]["verdict"] == "PASS"
    assert response["timeline"]["candidate"] == safe_events
    assert [rule["rule_id"] for rule in response["report"]["cases"][0]["candidate"]["rules"]] == [
        "lookup-present",
        "lookup-before-update",
        "lookup-scope",
        "no-directory-export",
        "update-needs-approval",
        "result-completed",
        "within-step-budget",
    ]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ([], "REQUEST_SCHEMA_INVALID"),
        ({"scenario_id": True, "source": "builtin"}, "REQUEST_SCHEMA_INVALID"),
        ({"scenario_id": "approved-update", "source": 1}, "REQUEST_SCHEMA_INVALID"),
        ({"scenario_id": "unknown", "source": "builtin"}, "SCENARIO_NOT_FOUND"),
        (
            {"scenario_id": "approved-update", "source": "builtin", "rules": []},
            "REQUEST_SCHEMA_INVALID",
        ),
        (
            {"scenario_id": "approved-update", "source": "upload"},
            "REQUEST_SCHEMA_INVALID",
        ),
        (
            {
                "scenario_id": "approved-update",
                "source": "upload",
                "trace": {"schema_version": "1.0", "events": [], "rules": []},
            },
            "REQUEST_SCHEMA_INVALID",
        ),
        (
            {
                "scenario_id": "approved-update",
                "source": "upload",
                "trace": {"schema_version": "2.0", "events": []},
            },
            "REQUEST_SCHEMA_INVALID",
        ),
        (
            {
                "scenario_id": "approved-update",
                "source": "upload",
                "trace": {"schema_version": "1.0", "events": "not-a-list"},
            },
            "REQUEST_SCHEMA_INVALID",
        ),
        (
            {
                "scenario_id": "approved-update",
                "source": "upload",
                "trace": {"schema_version": "1.0", "events": []},
            },
            "TRACE_EMPTY",
        ),
    ],
)
def test_request_shape_is_closed(payload: object, code: str) -> None:
    with pytest.raises(LabError) as caught:
        ReplayLabAdapter().replay_bytes(_request(payload))

    assert caught.value.code == code
    assert len(caught.value.message) <= 256


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b'[{"key":1,"key":2}]',
        b'{"scenario_id":"approved-update","scenario_id":"risky-broad-update","source":"builtin"}',
        b'{"scenario_id":"approved-update","source":"upload","trace":{"schema_version":"1.0","events":[{"kind":"result","kind":"tool_call"}]}}',
        b"\xff",
    ],
)
def test_malformed_or_duplicate_json_is_rejected_before_materialization(body: bytes) -> None:
    with pytest.raises(LabError) as caught:
        ReplayLabAdapter().replay_bytes(body)

    assert caught.value.code == "REQUEST_JSON_INVALID"
    assert "toolcall-replay-lab-" not in caught.value.message


def test_body_size_limit_is_inclusive_and_limit_plus_one_is_rejected() -> None:
    valid = _request({"scenario_id": "approved-update", "source": "builtin"})

    at_limit = valid + (b" " * (MAX_REQUEST_BYTES - len(valid)))
    assert ReplayLabAdapter().replay_bytes(at_limit)["report"]["verdict"] == "PASS"

    with pytest.raises(LabError) as caught:
        ReplayLabAdapter().replay_bytes(at_limit + b" ")
    assert caught.value.code == "REQUEST_TOO_LARGE"
    assert caught.value.status == 413


def test_upload_at_trace_limit_is_not_rejected_for_small_envelope_overhead() -> None:
    event = {
        "schema_version": "1.0",
        "case_id": "bounded-update",
        "step": 1,
        "kind": "result",
        "status": "completed",
        "output": {"padding": "x" * 999_800},
    }
    body = _request(
        {
            "scenario_id": "approved-update",
            "source": "upload",
            "trace": {"schema_version": "1.0", "events": [event]},
        }
    )
    assert len(body) > MAX_UPLOAD_BYTES

    response = ReplayLabAdapter().replay_bytes(body)

    assert response["imported"] is True
    assert response["timeline"]["candidate"][0] == event


def test_empty_or_engine_invalid_upload_is_bounded_and_has_no_local_path() -> None:
    invalid_event = {
        "schema_version": "1.0",
        "case_id": "bounded-update",
        "step": 1,
        "kind": "result",
        "status": "completed",
        "output": {},
        "unexpected": True,
    }
    with pytest.raises(LabError) as caught:
        ReplayLabAdapter().replay_bytes(
            _request(
                {
                    "scenario_id": "approved-update",
                    "source": "upload",
                    "trace": {"schema_version": "1.0", "events": [invalid_event]},
                }
            )
        )

    assert caught.value.code == "TRACE_SCHEMA_INVALID"
    assert caught.value.status == 422
    assert "toolcall-replay-lab-" not in caught.value.message
    assert "Traceback" not in caught.value.message
    assert len(caught.value.message.encode("utf-8")) <= 256


def test_upload_event_limit_plus_one_is_rejected_by_the_engine() -> None:
    with pytest.raises(LabError) as caught:
        ReplayLabAdapter().replay_bytes(
            _request(
                {
                    "scenario_id": "approved-update",
                    "source": "upload",
                    "trace": {
                        "schema_version": "1.0",
                        "events": [{} for _ in range(2_001)],
                    },
                }
            )
        )

    assert caught.value.code == "TRACE_EVENT_LIMIT_EXCEEDED"


def test_trace_node_limit_is_inclusive_and_rejected_before_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event: dict[str, JsonValue] = {
        "schema_version": "1.0",
        "case_id": "bounded-update",
        "step": 1,
        "kind": "result",
        "status": "completed",
        "output": [],
    }
    output = cast(list[JsonValue], event["output"])
    output.extend(None for _ in range(MAX_TRACE_JSON_NODES - json_node_count(event)))
    assert json_node_count(event) == MAX_TRACE_JSON_NODES
    request = {
        "scenario_id": "approved-update",
        "source": "upload",
        "trace": {"schema_version": "1.0", "events": [event]},
    }

    assert ReplayLabAdapter().replay_bytes(_request(request))["imported"] is True
    output.append(None)

    def unexpected_materialization(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("oversized node graph reached temporary materialization")

    monkeypatch.setattr(
        "toolcall_replay_lab.adapter.tempfile.TemporaryDirectory", unexpected_materialization
    )
    with pytest.raises(LabError) as caught:
        ReplayLabAdapter().replay_bytes(_request(request))

    assert caught.value.code == "TRACE_NODE_LIMIT_EXCEEDED"
    assert caught.value.status == 422


@pytest.mark.parametrize(
    "unsafe_number",
    [
        9_007_199_254_740_992,
        -9_007_199_254_740_992,
        9_007_199_254_740_992.0,
    ],
)
@pytest.mark.parametrize("field", ["arguments", "output"])
def test_upload_rejects_nested_unsafe_integers_before_materialization(
    unsafe_number: int | float,
    field: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _events(ROOT / "examples/traces/bounded-update.baseline.jsonl")
    target = cast(
        dict[str, JsonValue],
        events[0]["arguments"] if field == "arguments" else events[-1]["output"],
    )
    target["nested"] = {"values": [unsafe_number]}
    request = {
        "scenario_id": "approved-update",
        "source": "upload",
        "trace": {"schema_version": "1.0", "events": events},
    }

    def unexpected_materialization(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("unsafe number reached temporary materialization")

    monkeypatch.setattr(
        "toolcall_replay_lab.adapter.tempfile.TemporaryDirectory", unexpected_materialization
    )
    with pytest.raises(LabError) as caught:
        ReplayLabAdapter().replay_bytes(_request(request))

    assert caught.value.code == "REQUEST_NUMBER_INVALID"
    assert caught.value.status == 400
    assert len(caught.value.message.encode("utf-8")) <= 256


def test_upload_accepts_javascript_safe_integer_boundaries() -> None:
    events = _events(ROOT / "examples/traces/bounded-update.baseline.jsonl")
    arguments = cast(dict[str, JsonValue], events[0]["arguments"])
    output = cast(dict[str, JsonValue], events[-1]["output"])
    arguments["positive_boundary"] = 9_007_199_254_740_991
    output["negative_boundary"] = -9_007_199_254_740_991

    response = ReplayLabAdapter().replay_bytes(
        _request(
            {
                "scenario_id": "approved-update",
                "source": "upload",
                "trace": {"schema_version": "1.0", "events": events},
            }
        )
    )

    assert response["imported"] is True
    assert response["timeline"]["candidate"][0]["arguments"]["positive_boundary"] == (
        9_007_199_254_740_991
    )
    assert response["timeline"]["candidate"][-1]["output"]["negative_boundary"] == (
        -9_007_199_254_740_991
    )


def test_upload_depth_limit_matches_the_browser_trace_root_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _events(ROOT / "examples/traces/bounded-update.baseline.jsonl")
    boundary: JsonValue = 0
    for _ in range(60):
        boundary = [boundary]
    events[-1]["output"] = boundary
    request = {
        "scenario_id": "approved-update",
        "source": "upload",
        "trace": {"schema_version": "1.0", "events": events},
    }

    assert ReplayLabAdapter().replay_bytes(_request(request))["imported"] is True

    events[-1]["output"] = [boundary]

    def unexpected_materialization(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("over-depth request reached temporary materialization")

    monkeypatch.setattr(
        "toolcall_replay_lab.adapter.tempfile.TemporaryDirectory", unexpected_materialization
    )
    with pytest.raises(LabError) as caught:
        ReplayLabAdapter().replay_bytes(_request(request))

    assert caught.value.code == "REQUEST_JSON_INVALID"
    assert caught.value.status == 400
