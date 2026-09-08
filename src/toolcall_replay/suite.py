from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import ReplayError
from .jsonutil import canonical_bytes, freeze_object, json_integer, json_node_count, loads_strict
from .models import Case, Rule, Suite, Trace
from .trace import FileFingerprint, _file_fingerprint, _load_trace_descriptor

RULE_TYPES = {
    "call_presence",
    "call_order",
    "argument_constraint",
    "forbidden_tool",
    "approval_required",
    "expected_result",
    "step_budget",
}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]*$")
TOOL = re.compile(r"^[a-z][a-z0-9_.-]*$")
DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
FILE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
MAX_SUITE_BYTES = 1_000_000
MAX_TOTAL_TRACE_BYTES = 8_000_000
MAX_SUITE_JSON_NODES = 50_000
MAX_SUITE_CASES = 32
MAX_RULES_PER_CASE = 64
MAX_ALLOWED_VALUES_PER_RULE = 10_000


@dataclass(frozen=True, slots=True)
class TraceTarget:
    path: Path
    fingerprint: FileFingerprint


def _schema(location: Path) -> ReplayError:
    return ReplayError("SUITE_SCHEMA_INVALID", str(location), "invalid suite schema")


def _suite_too_large(location: Path) -> ReplayError:
    return ReplayError(
        "SUITE_TOO_LARGE",
        str(location),
        f"suite exceeds {MAX_SUITE_BYTES}-byte limit",
    )


def _trace_path(root: Path, raw: str, location: Path) -> TraceTarget:
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReplayError("SUITE_TRACE_PATH_INVALID", str(location), "invalid trace path")
    candidate = root / relative
    try:
        try:
            resolved = candidate.resolve(strict=True)
        except RuntimeError as error:
            raise ReplayError(
                "SUITE_TRACE_PATH_INVALID", str(location), "invalid trace path"
            ) from error
        if os.path.commonpath((str(root), str(resolved))) != str(root):
            raise ValueError("trace path escapes suite root")
        status = os.stat(resolved, follow_symlinks=False)
    except (OSError, ValueError):
        raise ReplayError("SUITE_TRACE_PATH_INVALID", str(location), "invalid trace path") from None
    if not stat.S_ISREG(status.st_mode):
        raise ReplayError("SUITE_TRACE_PATH_INVALID", str(location), "invalid trace path")
    return TraceTarget(resolved, _file_fingerprint(status))


def _load_confined_trace(
    root: Path,
    root_descriptor: int,
    target: TraceTarget,
    location: Path,
    case_id: str,
) -> Trace:
    parts = target.path.relative_to(root).parts
    directory_descriptor: int | None = None
    trace_descriptor: int | None = None
    try:
        directory_descriptor = os.dup(root_descriptor)
        for part in parts[:-1]:
            next_descriptor = os.open(part, DIRECTORY_OPEN_FLAGS, dir_fd=directory_descriptor)
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        trace_descriptor = os.open(parts[-1], FILE_OPEN_FLAGS, dir_fd=directory_descriptor)
        status = os.fstat(trace_descriptor)
        if not stat.S_ISREG(status.st_mode) or _file_fingerprint(status) != target.fingerprint:
            raise OSError("trace is not a regular file")
    except (OSError, ValueError):
        if trace_descriptor is not None:
            os.close(trace_descriptor)
        raise ReplayError("SUITE_TRACE_PATH_INVALID", str(location), "invalid trace path") from None
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)
    assert trace_descriptor is not None
    return _load_trace_descriptor(
        trace_descriptor,
        target.path,
        case_id,
        expected_fingerprint=target.fingerprint,
        changed_code="SUITE_TRACE_PATH_INVALID",
        changed_message="invalid trace path",
    )


def _read_suite_source(path: Path) -> tuple[str, Path, int]:
    root_descriptor: int | None = None
    suite_descriptor: int | None = None
    try:
        try:
            resolved = path.resolve(strict=True)
        except RuntimeError as error:
            raise ReplayError("SUITE_NOT_FOUND", str(path), "suite not found") from error
        root = resolved.parent
        root_status = os.stat(root, follow_symlinks=False)
        suite_status = os.stat(resolved, follow_symlinks=False)
        if not stat.S_ISDIR(root_status.st_mode) or not stat.S_ISREG(suite_status.st_mode):
            raise OSError("suite source is not a regular file")
        root_descriptor = os.open(root, DIRECTORY_OPEN_FLAGS)
        opened_root = os.fstat(root_descriptor)
        if not stat.S_ISDIR(opened_root.st_mode) or (opened_root.st_dev, opened_root.st_ino) != (
            root_status.st_dev,
            root_status.st_ino,
        ):
            raise OSError("suite root changed during open")
        suite_descriptor = os.open(resolved.name, FILE_OPEN_FLAGS, dir_fd=root_descriptor)
        opened_suite = os.fstat(suite_descriptor)
        fingerprint = _file_fingerprint(suite_status)
        if not stat.S_ISREG(opened_suite.st_mode) or _file_fingerprint(opened_suite) != fingerprint:
            raise OSError("suite source changed during open")
        if opened_suite.st_size > MAX_SUITE_BYTES:
            raise _suite_too_large(path)
        source = os.fdopen(suite_descriptor, "rb")
        suite_descriptor = None
        with source:
            content = source.read(MAX_SUITE_BYTES + 1)
            final_fingerprint = _file_fingerprint(os.fstat(source.fileno()))
        if final_fingerprint != fingerprint:
            raise OSError("suite source changed during read")
        if len(content) > MAX_SUITE_BYTES:
            raise _suite_too_large(path)
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        if suite_descriptor is not None:
            os.close(suite_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)
        raise ReplayError("SUITE_JSON_INVALID", str(path), "invalid JSON") from error
    except (OSError, ValueError) as error:
        if suite_descriptor is not None:
            os.close(suite_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)
        raise ReplayError("SUITE_NOT_FOUND", str(path), "suite not found") from error
    except BaseException:
        if suite_descriptor is not None:
            os.close(suite_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)
        raise
    assert root_descriptor is not None
    return text, root, root_descriptor


def _build_suite(path: Path, text: str, root: Path, root_descriptor: int) -> Suite:
    value = loads_strict(text, "SUITE_JSON_INVALID", str(path))
    if json_node_count(value) > MAX_SUITE_JSON_NODES:
        raise ReplayError(
            "SUITE_NODE_LIMIT_EXCEEDED",
            str(path),
            f"suite exceeds {MAX_SUITE_JSON_NODES}-node limit",
        )
    if not isinstance(value, dict):
        raise _schema(path)
    if "schema_version" not in value:
        raise _schema(path)
    if value.get("schema_version") != "1.0":
        raise ReplayError("SUITE_VERSION_UNSUPPORTED", str(path), "unsupported suite version")
    suite_id = value.get("suite_id")
    if (
        set(value) != {"schema_version", "suite_id", "cases"}
        or not isinstance(suite_id, str)
        or not IDENTIFIER.fullmatch(suite_id)
    ):
        raise _schema(path)
    raw_cases = value["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise _schema(path)
    if len(raw_cases) > MAX_SUITE_CASES:
        raise ReplayError(
            "SUITE_ELEMENT_LIMIT_EXCEEDED",
            str(path),
            f"suite exceeds {MAX_SUITE_CASES}-case limit",
        )
    pending: list[tuple[str, str, str, str, tuple[Rule, ...]]] = []
    case_ids: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict) or set(raw) != {
            "case_id",
            "name",
            "baseline_trace",
            "candidate_trace",
            "rules",
        }:
            raise _schema(path)
        case_id = raw["case_id"]
        name = raw["name"]
        baseline = raw["baseline_trace"]
        candidate = raw["candidate_trace"]
        raw_rules = raw["rules"]
        if (
            not isinstance(case_id, str)
            or not case_id
            or not isinstance(name, str)
            or not name
            or not isinstance(baseline, str)
            or not baseline
            or not isinstance(candidate, str)
            or not candidate
            or not IDENTIFIER.fullmatch(case_id)
            or case_id in case_ids
            or not isinstance(raw_rules, list)
            or not raw_rules
        ):
            raise _schema(path)
        if len(raw_rules) > MAX_RULES_PER_CASE:
            raise ReplayError(
                "SUITE_ELEMENT_LIMIT_EXCEEDED",
                str(path),
                f"case exceeds {MAX_RULES_PER_CASE}-rule limit",
            )
        case_ids.add(case_id)
        rules: list[Rule] = []
        rule_ids: set[str] = set()
        for rule in raw_rules:
            if not isinstance(rule, dict):
                raise _schema(path)
            rule_type = rule.get("type")
            if not isinstance(rule_type, str):
                raise _schema(path)
            if rule_type not in RULE_TYPES:
                raise ReplayError("SUITE_UNKNOWN_RULE", str(path), "unknown rule type")
            rule_id = rule.get("rule_id")
            if (
                not isinstance(rule_id, str)
                or not IDENTIFIER.fullmatch(rule_id)
                or rule_id in rule_ids
            ):
                raise _schema(path)
            rule_ids.add(rule_id)
            required = {"rule_id", "type"}
            allowed_value_keys: frozenset[bytes] | None = None
            expected_output_key: bytes | None = None
            if rule_type == "call_presence":
                required |= {"tool", "min_calls", "max_calls"}
            elif rule_type == "call_order":
                required |= {"first_tool", "then_tool"}
            elif rule_type == "argument_constraint":
                required |= {"tool", "argument", "allowed_values"}
            elif rule_type in {"forbidden_tool", "approval_required"}:
                required |= {"tool"}
            elif rule_type == "expected_result":
                required |= {"status", "output"}
            else:
                required |= {"max_steps"}
            if set(rule) != required:
                raise _schema(path)
            tool = rule.get("tool")
            if rule_type in {
                "call_presence",
                "argument_constraint",
                "forbidden_tool",
                "approval_required",
            } and (not isinstance(tool, str) or not TOOL.fullmatch(tool)):
                raise _schema(path)
            if rule_type == "call_order":
                first_tool = rule["first_tool"]
                then_tool = rule["then_tool"]
                if (
                    not isinstance(first_tool, str)
                    or not TOOL.fullmatch(first_tool)
                    or not isinstance(then_tool, str)
                    or not TOOL.fullmatch(then_tool)
                ):
                    raise _schema(path)
            if rule_type == "call_presence":
                min_calls = json_integer(rule["min_calls"])
                max_calls = json_integer(rule["max_calls"])
                if (
                    min_calls is None
                    or max_calls is None
                    or min_calls < 0
                    or max_calls < 0
                    or min_calls > max_calls
                ):
                    raise _schema(path)
            if rule_type == "argument_constraint":
                argument = rule["argument"]
                allowed_values = rule["allowed_values"]
                if (
                    not isinstance(argument, str)
                    or not argument
                    or not isinstance(allowed_values, list)
                    or not allowed_values
                ):
                    raise _schema(path)
                if len(allowed_values) > MAX_ALLOWED_VALUES_PER_RULE:
                    raise ReplayError(
                        "SUITE_ELEMENT_LIMIT_EXCEEDED",
                        str(path),
                        f"rule exceeds {MAX_ALLOWED_VALUES_PER_RULE}-allowed-value limit",
                    )
                allowed_value_keys = frozenset(canonical_bytes(value) for value in allowed_values)
            if rule_type == "expected_result":
                status = rule["status"]
                if not isinstance(status, str) or status not in {"completed", "failed"}:
                    raise _schema(path)
                expected_output_key = canonical_bytes(rule["output"])
            if rule_type == "step_budget":
                max_steps = json_integer(rule["max_steps"])
                if max_steps is None or max_steps < 0:
                    raise _schema(path)
            normalized = dict(rule)
            if rule_type == "call_presence":
                normalized["min_calls"] = min_calls
                normalized["max_calls"] = max_calls
            elif rule_type == "step_budget":
                normalized["max_steps"] = max_steps
            rules.append(
                Rule(
                    rule_id,
                    rule_type,
                    freeze_object(normalized),
                    allowed_value_keys,
                    expected_output_key,
                )
            )
        pending.append((case_id, name, baseline, candidate, tuple(rules)))
    resolved_cases = [
        (
            case_id,
            name,
            _trace_path(root, baseline, path),
            _trace_path(root, candidate, path),
            rules,
        )
        for case_id, name, baseline, candidate, rules in pending
    ]
    total_trace_bytes = sum(
        baseline.fingerprint[3] + candidate.fingerprint[3]
        for _, _, baseline, candidate, _ in resolved_cases
    )
    if total_trace_bytes > MAX_TOTAL_TRACE_BYTES:
        raise ReplayError(
            "SUITE_TRACE_BUDGET_EXCEEDED",
            str(path),
            f"suite traces exceed {MAX_TOTAL_TRACE_BYTES}-byte limit",
        )
    cases = [
        Case(
            case_id,
            name,
            _load_confined_trace(root, root_descriptor, baseline, path, case_id),
            _load_confined_trace(root, root_descriptor, candidate, path, case_id),
            rules,
        )
        for case_id, name, baseline, candidate, rules in resolved_cases
    ]
    return Suite(suite_id, tuple(cases))


def load_suite(path: Path) -> Suite:
    text, root, root_descriptor = _read_suite_source(path)
    try:
        return _build_suite(path, text, root, root_descriptor)
    finally:
        os.close(root_descriptor)
