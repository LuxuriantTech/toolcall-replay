from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, cast

from toolcall_replay.errors import ReplayError
from toolcall_replay.jsonutil import (
    MAX_JSON_DEPTH,
    JsonValue,
    json_integer,
    json_node_count,
    loads_strict,
    thaw_json,
)
from toolcall_replay.report import build_report
from toolcall_replay.suite import load_suite
from toolcall_replay.trace import MAX_TRACE_BYTES, MAX_TRACE_EVENTS, MAX_TRACE_JSON_NODES

MAX_UPLOAD_BYTES = MAX_TRACE_BYTES
MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 50_000
MAX_UPLOAD_EVENTS = MAX_TRACE_EVENTS
MAX_UPLOAD_JSON_DEPTH = MAX_JSON_DEPTH - 1
MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991

_SCENARIOS = (
    {
        "id": "approved-update",
        "name": "Approved update",
        "description": "A bounded synthetic request with prior approval.",
        "tone": "safe",
        "expected_verdict": "PASS",
    },
    {
        "id": "risky-broad-update",
        "name": "Risky broad update",
        "description": "A synthetic candidate with three reviewable divergences.",
        "tone": "caution",
        "expected_verdict": "FAIL",
    },
)
_SCENARIO_ROOT = Path(__file__).with_name("scenarios")


class LabError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _request_error(code: str, message: str, status: int = 400) -> LabError:
    return LabError(code, message[:256], status)


def _validate_request_numbers(value: JsonValue) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        integer = json_integer(current)
        if integer is not None and abs(integer) > MAX_SAFE_JSON_INTEGER:
            raise _request_error(
                "REQUEST_NUMBER_INVALID",
                "Request integers must be within JavaScript's safe integer range.",
            )
        if isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, dict):
            pending.extend(current.values())


def _parse_request(body: bytes) -> tuple[str, str, list[JsonValue] | None]:
    if len(body) > MAX_REQUEST_BYTES:
        raise _request_error(
            "REQUEST_TOO_LARGE",
            f"Request exceeds the {MAX_REQUEST_BYTES}-byte limit.",
            413,
        )
    try:
        text = body.decode("utf-8")
        raw = loads_strict(text, "REQUEST_JSON_INVALID", "request")
    except (UnicodeDecodeError, ReplayError) as error:
        raise _request_error(
            "REQUEST_JSON_INVALID", "Request body must be strict UTF-8 JSON."
        ) from error
    _validate_request_numbers(raw)
    if not isinstance(raw, dict):
        raise _request_error("REQUEST_SCHEMA_INVALID", "Request body must be a JSON object.")
    scenario_id = raw.get("scenario_id")
    source = raw.get("source")
    if not isinstance(scenario_id, str) or not isinstance(source, str):
        raise _request_error("REQUEST_SCHEMA_INVALID", "Scenario and source must be strings.")
    if source == "builtin":
        if set(raw) != {"scenario_id", "source"}:
            raise _request_error("REQUEST_SCHEMA_INVALID", "Built-in request fields are invalid.")
        return scenario_id, source, None
    if source != "upload" or set(raw) != {"scenario_id", "source", "trace"}:
        raise _request_error("REQUEST_SCHEMA_INVALID", "Upload request fields are invalid.")
    trace = raw.get("trace")
    if (
        not isinstance(trace, dict)
        or set(trace) != {"schema_version", "events"}
        or trace.get("schema_version") != "1.0"
        or not isinstance(trace.get("events"), list)
    ):
        raise _request_error("REQUEST_SCHEMA_INVALID", "Uploaded trace envelope is invalid.")
    events = cast(list[JsonValue], trace["events"])
    if not events:
        raise _request_error("TRACE_EMPTY", "Uploaded trace must contain at least one event.", 422)
    if len(events) > MAX_UPLOAD_EVENTS:
        raise _request_error(
            "TRACE_EVENT_LIMIT_EXCEEDED",
            f"Uploaded trace exceeds the {MAX_UPLOAD_EVENTS}-event limit.",
            422,
        )
    nodes = 0
    for event in events:
        nodes += json_node_count(event)
        if nodes > MAX_TRACE_JSON_NODES:
            raise _request_error(
                "TRACE_NODE_LIMIT_EXCEEDED",
                f"Uploaded trace exceeds the {MAX_TRACE_JSON_NODES}-node limit.",
                422,
            )
    return scenario_id, source, events


def _candidate_bytes(events: list[JsonValue]) -> bytes:
    content = bytearray()
    for event in events:
        chunk = (
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        if len(chunk) > MAX_UPLOAD_BYTES - len(content):
            raise _request_error(
                "TRACE_TOO_LARGE",
                f"Uploaded trace exceeds the {MAX_UPLOAD_BYTES}-byte limit.",
                422,
            )
        content.extend(chunk)
    return bytes(content)


class ReplayLabAdapter:
    """Translate the local HTTP contract into replay-engine calls."""

    def list_scenarios(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "scenarios": [dict(scenario) for scenario in _SCENARIOS],
            "limits": {
                "max_upload_bytes": MAX_UPLOAD_BYTES,
                "max_events": MAX_UPLOAD_EVENTS,
                "max_json_nodes": MAX_TRACE_JSON_NODES,
                "max_json_depth": MAX_UPLOAD_JSON_DEPTH,
            },
        }

    def replay_bytes(self, body: bytes) -> dict[str, Any]:
        scenario_id, source, events = _parse_request(body)
        scenario = next((item for item in _SCENARIOS if item["id"] == scenario_id), None)
        if scenario is None:
            raise _request_error("SCENARIO_NOT_FOUND", "Requested scenario does not exist.", 404)

        try:
            with tempfile.TemporaryDirectory(prefix="toolcall-replay-lab-") as temporary:
                root = Path(temporary)
                shutil.copyfile(_SCENARIO_ROOT / "suite.json", root / "suite.json")
                shutil.copyfile(_SCENARIO_ROOT / "baseline.jsonl", root / "baseline.jsonl")
                if events is None:
                    shutil.copyfile(
                        _SCENARIO_ROOT / f"{scenario_id}.jsonl", root / "candidate.jsonl"
                    )
                else:
                    (root / "candidate.jsonl").write_bytes(_candidate_bytes(events))
                suite = load_suite(root / "suite.json")
                report = build_report(suite)
                repeated = build_report(suite)
                if report != repeated:
                    raise _request_error(
                        "DETERMINISM_CHECK_FAILED",
                        "The replay did not produce identical reports.",
                        500,
                    )
                case = suite.cases[0]
                timeline = {
                    "baseline": [thaw_json(event.data) for event in case.baseline_trace.events],
                    "candidate": [thaw_json(event.data) for event in case.candidate_trace.events],
                }
        except ReplayError as error:
            raise _request_error(error.code, error.message, 422) from error
        return {
            "schema_version": "1.0",
            "scenario": dict(scenario),
            "provenance": {
                "baseline": "versioned synthetic fixture",
                "candidate": (
                    f"versioned {scenario_id.removesuffix('-update')} synthetic fixture"
                    if source == "builtin"
                    else "local bounded upload"
                ),
                "kind": source,
            },
            "imported": source == "upload",
            "report": report,
            "timeline": timeline,
            "determinism": {
                "verified": True,
                "digest": report["digest"],
                "runs_compared": 2,
            },
        }
