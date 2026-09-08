from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from .errors import ReplayError
from .jsonutil import freeze_object, json_integer, json_node_count, loads_strict
from .models import Event, Trace

IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]*$")
TOOL = re.compile(r"^[a-z][a-z0-9_.-]*$")
type FileFingerprint = tuple[int, int, int, int, int, int]
MAX_TRACE_BYTES = 1_000_000
MAX_TRACE_JSON_NODES = 50_000
MAX_TRACE_EVENTS = 2_000


def _file_fingerprint(status: os.stat_result) -> FileFingerprint:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _parse_trace(text: str, path: Path, case_id: str) -> Trace:
    lines = text.splitlines()
    if len(lines) > MAX_TRACE_EVENTS:
        raise ReplayError(
            "TRACE_EVENT_LIMIT_EXCEEDED",
            str(path),
            f"trace exceeds {MAX_TRACE_EVENTS}-event limit",
        )
    events: list[Event] = []
    approvals: set[str] = set()
    nodes = 0
    for index, line in enumerate(lines, 1):
        location = f"{path}:line={index}"
        if not line:
            raise ReplayError("TRACE_SCHEMA_INVALID", str(path), "blank trace line")
        try:
            raw = loads_strict(line, "TRACE_JSON_INVALID", location)
        except ReplayError as error:
            if isinstance(error.__cause__, json.JSONDecodeError) and index == len(lines):
                raise ReplayError("TRACE_TRUNCATED", location, "truncated trace") from error
            raise
        nodes += json_node_count(raw)
        if nodes > MAX_TRACE_JSON_NODES:
            raise ReplayError(
                "TRACE_NODE_LIMIT_EXCEEDED",
                location,
                f"trace exceeds {MAX_TRACE_JSON_NODES}-node limit",
            )
        if not isinstance(raw, dict):
            raise ReplayError("TRACE_SCHEMA_INVALID", str(path), "invalid trace event")
        raw_case_id = raw.get("case_id")
        if (
            raw.get("schema_version") != "1.0"
            or not isinstance(raw_case_id, str)
            or raw_case_id != case_id
            or not IDENTIFIER.fullmatch(raw_case_id)
        ):
            raise ReplayError("TRACE_SCHEMA_INVALID", str(path), "invalid trace event")
        step = json_integer(raw.get("step"))
        kind = raw.get("kind")
        if (
            step is None
            or step < 1
            or step != index
            or not isinstance(kind, str)
            or kind not in {"tool_call", "approval", "result"}
        ):
            raise ReplayError("TRACE_SCHEMA_INVALID", str(path), "invalid trace event")
        if kind == "tool_call":
            tool = raw.get("tool")
            arguments = raw.get("arguments")
            approval_id = raw.get("approval_id")
            if (
                set(raw)
                - {"schema_version", "case_id", "step", "kind", "tool", "arguments", "approval_id"}
                or not {"schema_version", "case_id", "step", "kind", "tool", "arguments"}
                <= set(raw)
                or not isinstance(tool, str)
                or not TOOL.fullmatch(tool)
                or not isinstance(arguments, dict)
                or ("approval_id" in raw and (not isinstance(approval_id, str) or not approval_id))
            ):
                raise ReplayError("TRACE_SCHEMA_INVALID", str(path), "invalid trace event")
        elif kind == "approval":
            required = {
                "schema_version",
                "case_id",
                "step",
                "kind",
                "approval_id",
                "status",
                "tool",
                "arguments",
            }
            approval_id = raw.get("approval_id")
            status = raw.get("status")
            tool = raw.get("tool")
            arguments = raw.get("arguments")
            if (
                set(raw) != required
                or not isinstance(approval_id, str)
                or not approval_id
                or approval_id in approvals
                or not isinstance(status, str)
                or status not in {"granted", "denied"}
                or not isinstance(tool, str)
                or not TOOL.fullmatch(tool)
                or not isinstance(arguments, dict)
            ):
                raise ReplayError("TRACE_SCHEMA_INVALID", str(path), "invalid trace event")
            approvals.add(approval_id)
        else:
            status = raw.get("status")
            if (
                set(raw)
                != {
                    "schema_version",
                    "case_id",
                    "step",
                    "kind",
                    "status",
                    "output",
                }
                or not isinstance(status, str)
                or status not in {"completed", "failed"}
            ):
                raise ReplayError("TRACE_SCHEMA_INVALID", str(path), "invalid trace event")
        normalized = dict(raw)
        normalized["step"] = step
        events.append(Event(step, kind, freeze_object(normalized)))
    if (
        not events
        or events[-1].kind != "result"
        or sum(event.kind == "result" for event in events) != 1
    ):
        raise ReplayError("TRACE_SCHEMA_INVALID", str(path), "result must be final and unique")
    return Trace(tuple(events))


def _load_trace_descriptor(
    descriptor: int,
    path: Path,
    case_id: str,
    *,
    expected_fingerprint: FileFingerprint | None = None,
    changed_code: str = "TRACE_NOT_FOUND",
    changed_message: str = "trace not found",
) -> Trace:
    descriptor_owned = True
    try:
        initial_status = os.fstat(descriptor)
        if not stat.S_ISREG(initial_status.st_mode):
            raise OSError("trace is not a regular file")
        if initial_status.st_size > MAX_TRACE_BYTES:
            raise ReplayError(
                "TRACE_TOO_LARGE",
                str(path),
                f"trace exceeds {MAX_TRACE_BYTES}-byte limit",
            )
        source = os.fdopen(descriptor, "rb")
        descriptor_owned = False
        with source:
            content = source.read(MAX_TRACE_BYTES + 1)
            final_fingerprint = _file_fingerprint(os.fstat(source.fileno()))
        if expected_fingerprint is not None and final_fingerprint != expected_fingerprint:
            raise ReplayError(changed_code, str(path), changed_message)
        if len(content) > MAX_TRACE_BYTES:
            raise ReplayError(
                "TRACE_TOO_LARGE",
                str(path),
                f"trace exceeds {MAX_TRACE_BYTES}-byte limit",
            )
        text = content.decode("utf-8")
    except OSError as error:
        raise ReplayError("TRACE_NOT_FOUND", str(path), "trace not found") from error
    except UnicodeDecodeError as error:
        raise ReplayError("TRACE_ENCODING_INVALID", str(path), "invalid trace encoding") from error
    finally:
        if descriptor_owned:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return _parse_trace(text, path, case_id)


def load_trace(path: Path, case_id: str) -> Trace:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor: int | None = None
    fingerprint: FileFingerprint | None = None
    try:
        descriptor = os.open(path, flags)
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise OSError("trace is not a regular file")
        fingerprint = _file_fingerprint(status)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise ReplayError("TRACE_NOT_FOUND", str(path), "trace not found") from error
    assert fingerprint is not None
    return _load_trace_descriptor(descriptor, path, case_id, expected_fingerprint=fingerprint)
