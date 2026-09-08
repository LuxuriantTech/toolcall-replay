from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterator
from html import escape as escape_html
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


def _write_argument_suite(
    root: Path,
    allowed_values: list[Any],
    observed_values: list[Any],
    *,
    rule_count: int = 1,
) -> Path:
    trace = root / "amplification.jsonl"
    events: list[dict[str, Any]] = [
        {
            "schema_version": "1.0",
            "case_id": "amplification",
            "step": step,
            "kind": "tool_call",
            "tool": "request.lookup",
            "arguments": {"scope": value},
        }
        for step, value in enumerate(observed_values, 1)
    ]
    events.append(
        {
            "schema_version": "1.0",
            "case_id": "amplification",
            "step": len(events) + 1,
            "kind": "result",
            "status": "completed",
            "output": None,
        }
    )
    trace.write_text(
        "\n".join(json.dumps(event, separators=(",", ":")) for event in events) + "\n",
        encoding="utf-8",
    )
    suite = root / "suite.json"
    write_json(
        suite,
        {
            "schema_version": "1.0",
            "suite_id": "amplification",
            "cases": [
                {
                    "case_id": "amplification",
                    "name": "Synthetic amplification boundary",
                    "baseline_trace": trace.name,
                    "candidate_trace": trace.name,
                    "rules": [
                        {
                            "rule_id": f"scope-{index}",
                            "type": "argument_constraint",
                            "tool": "request.lookup",
                            "argument": "scope",
                            "allowed_values": allowed_values,
                        }
                        for index in range(rule_count)
                    ],
                }
            ],
        },
    )
    return suite


def _write_approval_suite(root: Path, payload: str, *, rules_per_type: int = 32) -> Path:
    trace = root / "approval-amplification.jsonl"
    events = [
        {
            "schema_version": "1.0",
            "case_id": "approval-amplification",
            "step": 1,
            "kind": "approval",
            "approval_id": "approval-1",
            "status": "granted",
            "tool": "request.update",
            "arguments": {"payload": payload},
        },
        {
            "schema_version": "1.0",
            "case_id": "approval-amplification",
            "step": 2,
            "kind": "tool_call",
            "tool": "request.update",
            "arguments": {"payload": payload},
            "approval_id": "approval-1",
        },
        {
            "schema_version": "1.0",
            "case_id": "approval-amplification",
            "step": 3,
            "kind": "result",
            "status": "completed",
            "output": {"done": True},
        },
    ]
    trace.write_text(
        "\n".join(json.dumps(event, separators=(",", ":")) for event in events) + "\n",
        encoding="utf-8",
    )
    suite = root / "approval-suite.json"
    rules = [
        {
            "rule_id": f"approval-{index}",
            "type": "approval_required",
            "tool": "request.update",
        }
        for index in range(rules_per_type)
    ] + [
        {
            "rule_id": f"result-{index}",
            "type": "expected_result",
            "status": "completed",
            "output": {"done": True},
        }
        for index in range(rules_per_type)
    ]
    write_json(
        suite,
        {
            "schema_version": "1.0",
            "suite_id": "approval-amplification",
            "cases": [
                {
                    "case_id": "approval-amplification",
                    "name": "Synthetic approval amplification boundary",
                    "baseline_trace": trace.name,
                    "candidate_trace": trace.name,
                    "rules": rules,
                }
            ],
        },
    )
    return suite


def _run_main(
    suite: Path,
    output: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    result = production_module("cli").main(["evaluate", str(suite), "--output-dir", str(output)])
    captured = capsys.readouterr()
    return result, captured.out, captured.err


def _report_json_bytes(report: dict[str, Any]) -> bytes:
    return (
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


def test_json_node_count_includes_containers_keys_and_scalar_values() -> None:
    count = production_module("jsonutil").json_node_count
    assert count({"a": [1, {"b": None}]}) == 7


def test_suite_node_limit_is_inclusive(suite_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jsonutil = production_module("jsonutil")
    suite_module = production_module("suite")
    nodes = jsonutil.json_node_count(read_json(suite_copy))

    monkeypatch.setattr(suite_module, "MAX_SUITE_JSON_NODES", nodes, raising=False)
    assert suite_module.load_suite(suite_copy).suite_id == "synthetic-internal-request"

    monkeypatch.setattr(suite_module, "MAX_SUITE_JSON_NODES", nodes - 1)
    with pytest.raises(replay_error()) as caught:
        suite_module.load_suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_NODE_LIMIT_EXCEEDED", str(suite_copy))


def test_trace_node_limit_is_cumulative_and_inclusive(
    suite_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jsonutil = production_module("jsonutil")
    trace_module = production_module("trace")
    trace = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    nodes = sum(jsonutil.json_node_count(row) for row in rows)

    monkeypatch.setattr(trace_module, "MAX_TRACE_JSON_NODES", nodes, raising=False)
    assert len(trace_module.load_trace(trace, "bounded-update").events) == 5

    monkeypatch.setattr(trace_module, "MAX_TRACE_JSON_NODES", nodes - 1)
    with pytest.raises(replay_error()) as caught:
        trace_module.load_trace(trace, "bounded-update")
    assert_replay_error(
        caught.value,
        "TRACE_NODE_LIMIT_EXCEEDED",
        f"{trace}:line=5",
    )


def test_case_element_limit_is_inclusive(suite_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite_module = production_module("suite")
    payload = read_json(suite_copy)
    duplicate = copy.deepcopy(payload["cases"][0])
    duplicate["case_id"] = "bounded-update-copy"
    duplicate["name"] = "Synthetic copy"
    for trace_key in ("baseline_trace", "candidate_trace"):
        source = suite_copy.parent / duplicate[trace_key]
        destination = source.with_name(source.name.replace("bounded-update", "bounded-update-copy"))
        destination.write_text(
            source.read_text(encoding="utf-8").replace(
                '"case_id":"bounded-update"',
                '"case_id":"bounded-update-copy"',
            ),
            encoding="utf-8",
        )
        duplicate[trace_key] = str(destination.relative_to(suite_copy.parent))
    payload["cases"].append(duplicate)
    write_json(suite_copy, payload)

    monkeypatch.setattr(suite_module, "MAX_SUITE_CASES", 2, raising=False)
    assert len(suite_module.load_suite(suite_copy).cases) == 2

    monkeypatch.setattr(suite_module, "MAX_SUITE_CASES", 1)
    with pytest.raises(replay_error()) as caught:
        suite_module.load_suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_ELEMENT_LIMIT_EXCEEDED", str(suite_copy))


def test_rule_element_limit_is_inclusive(suite_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite_module = production_module("suite")
    payload = read_json(suite_copy)
    duplicate = copy.deepcopy(payload["cases"][0]["rules"][0])
    duplicate["rule_id"] = "lookup-present-copy"
    payload["cases"][0]["rules"].append(duplicate)
    write_json(suite_copy, payload)

    monkeypatch.setattr(suite_module, "MAX_RULES_PER_CASE", 8, raising=False)
    assert len(suite_module.load_suite(suite_copy).cases[0].rules) == 8

    monkeypatch.setattr(suite_module, "MAX_RULES_PER_CASE", 7)
    with pytest.raises(replay_error()) as caught:
        suite_module.load_suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_ELEMENT_LIMIT_EXCEEDED", str(suite_copy))


def test_allowed_value_element_limit_is_inclusive(
    suite_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite_module = production_module("suite")
    payload = read_json(suite_copy)
    payload["cases"][0]["rules"][2]["allowed_values"].append("team:other")
    write_json(suite_copy, payload)

    monkeypatch.setattr(suite_module, "MAX_ALLOWED_VALUES_PER_RULE", 2, raising=False)
    assert suite_module.load_suite(suite_copy).cases[0].rules[2].rule_id == "lookup-scope"

    monkeypatch.setattr(suite_module, "MAX_ALLOWED_VALUES_PER_RULE", 1)
    with pytest.raises(replay_error()) as caught:
        suite_module.load_suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_ELEMENT_LIMIT_EXCEEDED", str(suite_copy))


def test_trace_event_limit_is_inclusive(suite_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trace_module = production_module("trace")
    trace = suite_copy.parent / "traces/bounded-update.baseline.jsonl"

    monkeypatch.setattr(trace_module, "MAX_TRACE_EVENTS", 5, raising=False)
    assert len(trace_module.load_trace(trace, "bounded-update").events) == 5

    monkeypatch.setattr(trace_module, "MAX_TRACE_EVENTS", 4)
    with pytest.raises(replay_error()) as caught:
        trace_module.load_trace(trace, "bounded-update")
    assert_replay_error(caught.value, "TRACE_EVENT_LIMIT_EXCEEDED", str(trace))


def test_suite_schema_exposes_the_element_limits() -> None:
    suite_module = production_module("suite")
    schema = json.loads((ROOT / "schemas/suite.schema.json").read_text(encoding="utf-8"))
    cases = schema["properties"]["cases"]
    rules = cases["items"]["properties"]["rules"]
    argument_rule = rules["items"]["oneOf"][2]

    assert cases["maxItems"] == suite_module.MAX_SUITE_CASES
    assert rules["maxItems"] == suite_module.MAX_RULES_PER_CASE
    assert (
        argument_rule["properties"]["allowed_values"]["maxItems"]
        == suite_module.MAX_ALLOWED_VALUES_PER_RULE
    )


def test_node_limit_is_a_bounded_cli_error_without_reports(
    suite_copy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    jsonutil = production_module("jsonutil")
    suite_module = production_module("suite")
    limit = jsonutil.json_node_count(read_json(suite_copy)) - 1
    monkeypatch.setattr(suite_module, "MAX_SUITE_JSON_NODES", limit, raising=False)
    output = tmp_path / "out"

    code, stdout, stderr = _run_main(suite_copy, output, capsys)

    assert code == 2
    assert stdout == ""
    assert stderr == (
        f"ERROR code=SUITE_NODE_LIMIT_EXCEEDED location={suite_copy} "
        f"message=suite exceeds {limit}-node limit\n"
    )
    assert len(stderr.encode("utf-8")) < 2_048
    assert "Traceback" not in stderr
    assert not output.exists()


def test_element_limit_is_a_bounded_cli_error_without_reports(
    suite_copy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    trace_module = production_module("trace")
    monkeypatch.setattr(trace_module, "MAX_TRACE_EVENTS", 4, raising=False)
    trace = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    output = tmp_path / "out"

    code, stdout, stderr = _run_main(suite_copy, output, capsys)

    assert code == 2
    assert stdout == ""
    assert stderr == (
        f"ERROR code=TRACE_EVENT_LIMIT_EXCEEDED location={trace} "
        "message=trace exceeds 4-event limit\n"
    )
    assert len(stderr.encode("utf-8")) < 2_048
    assert "Traceback" not in stderr
    assert not output.exists()


def test_reduced_product_poc_is_rejected_before_any_pairwise_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    suite = _write_argument_suite(tmp_path, list(range(5_000)), [-1] * 500)
    rules_module = production_module("rules")

    def forbidden_pairwise_comparison(*_: object) -> bool:
        raise AssertionError("pairwise canonical comparison was reached")

    monkeypatch.setattr(
        rules_module, "canonical_equal", forbidden_pairwise_comparison, raising=False
    )
    output = tmp_path / "out"

    code, stdout, stderr = _run_main(suite, output, capsys)

    assert code == 2
    assert stdout == ""
    assert stderr == (
        "ERROR code=EVALUATION_BUDGET_EXCEEDED location=amplification "
        "message=evaluation exceeds 1000000-operation limit\n"
    )
    assert len(stderr.encode("utf-8")) < 2_048
    assert "Traceback" not in stderr
    assert not output.exists()


def test_evaluation_operation_limit_is_inclusive_for_direct_and_shared_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite_path = _write_argument_suite(tmp_path, [1, 2, 3], [-1, -2])
    suite = production_module("suite").load_suite(suite_path)
    case = suite.cases[0]
    trace = case.baseline_trace
    rules_module = production_module("rules")
    report_module = production_module("report")

    assert rules_module.evaluation_operation_count(case, trace) == 9
    monkeypatch.setattr(rules_module, "MAX_EVALUATION_OPERATIONS", 9, raising=False)
    assert rules_module.evaluate_trace(case, trace).verdict == "FAIL"
    monkeypatch.setattr(rules_module, "MAX_EVALUATION_OPERATIONS", 8)
    with pytest.raises(replay_error()) as direct:
        rules_module.evaluate_trace(case, trace)
    assert_replay_error(direct.value, "EVALUATION_BUDGET_EXCEEDED", case.case_id)

    monkeypatch.setattr(rules_module, "MAX_EVALUATION_OPERATIONS", 18)
    assert report_module.build_report(suite)["verdict"] == "FAIL"
    monkeypatch.setattr(rules_module, "MAX_EVALUATION_OPERATIONS", 17)
    with pytest.raises(replay_error()) as shared:
        report_module.build_report(suite)
    assert_replay_error(shared.value, "EVALUATION_BUDGET_EXCEEDED", case.case_id)


def test_evaluation_output_node_limit_is_inclusive_for_direct_and_shared_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite_path = _write_argument_suite(tmp_path, [1, 2, 3], [-1, -2])
    suite = production_module("suite").load_suite(suite_path)
    case = suite.cases[0]
    trace = case.baseline_trace
    rules_module = production_module("rules")
    report_module = production_module("report")

    assert rules_module.evaluation_output_node_count(case, trace) == 45
    monkeypatch.setattr(rules_module, "MAX_EVALUATION_OUTPUT_NODES", 45, raising=False)
    assert rules_module.evaluate_trace(case, trace).verdict == "FAIL"
    monkeypatch.setattr(rules_module, "MAX_EVALUATION_OUTPUT_NODES", 44)
    with pytest.raises(replay_error()) as direct:
        rules_module.evaluate_trace(case, trace)
    assert_replay_error(direct.value, "EVALUATION_OUTPUT_LIMIT_EXCEEDED", case.case_id)

    monkeypatch.setattr(rules_module, "MAX_EVALUATION_OUTPUT_NODES", 90)
    assert report_module.build_report(suite)["verdict"] == "FAIL"
    monkeypatch.setattr(rules_module, "MAX_EVALUATION_OUTPUT_NODES", 89)
    with pytest.raises(replay_error()) as shared:
        report_module.build_report(suite)
    assert_replay_error(shared.value, "EVALUATION_OUTPUT_LIMIT_EXCEEDED", case.case_id)


def test_repeated_observations_are_rejected_before_evidence_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    suite = _write_argument_suite(tmp_path, [0], [-1] * 100, rule_count=64)
    rules_module = production_module("rules")

    def forbidden_materialization(*_: object) -> object:
        raise AssertionError("evidence materialization was reached")

    monkeypatch.setattr(rules_module, "thaw_json", forbidden_materialization)
    output = tmp_path / "out"

    code, stdout, stderr = _run_main(suite, output, capsys)

    assert code == 2
    assert stdout == ""
    assert stderr == (
        "ERROR code=EVALUATION_OUTPUT_LIMIT_EXCEEDED location=amplification "
        "message=evaluation output exceeds 10000-node limit\n"
    )
    assert len(stderr.encode("utf-8")) < 2_048
    assert "Traceback" not in stderr
    assert not output.exists()


def test_nested_allowed_values_count_toward_the_output_node_budget(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    suite = _write_argument_suite(tmp_path, [list(range(12_000))], [])
    output = tmp_path / "out"

    code, stdout, stderr = _run_main(suite, output, capsys)

    assert code == 2
    assert stdout == ""
    assert stderr == (
        "ERROR code=EVALUATION_OUTPUT_LIMIT_EXCEEDED location=amplification "
        "message=evaluation output exceeds 10000-node limit\n"
    )
    assert "Traceback" not in stderr
    assert not output.exists()


def test_logical_report_digest_is_bounded_before_monolithic_serialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    suite = _write_argument_suite(tmp_path, [0], ["x" * 100], rule_count=64)
    report_module = production_module("report")
    monkeypatch.setattr(report_module, "MAX_LOGICAL_REPORT_BYTES", 1_000, raising=False)

    def forbidden_monolithic_digest(*_: object) -> bytes:
        raise AssertionError("monolithic canonical report serialization was reached")

    monkeypatch.setattr(
        report_module, "canonical_bytes", forbidden_monolithic_digest, raising=False
    )
    output = tmp_path / "out"

    code, stdout, stderr = _run_main(suite, output, capsys)

    assert code == 2
    assert stdout == ""
    assert stderr == (
        "ERROR code=REPORT_TOO_LARGE location=amplification "
        "message=logical report exceeds 1000-byte limit\n"
    )
    assert "Traceback" not in stderr
    assert not output.exists()


def test_allowed_values_are_canonicalized_once_when_the_rule_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jsonutil = production_module("jsonutil")
    suite_module = production_module("suite")
    allowed = [{"b": 2, "a": 1}, 1, True]
    suite_path = _write_argument_suite(tmp_path, allowed, [None])
    calls: list[Any] = []

    def counted(value: Any) -> bytes:
        calls.append(value)
        return cast(bytes, jsonutil.canonical_bytes(value))

    monkeypatch.setattr(suite_module, "canonical_bytes", counted, raising=False)
    suite = suite_module.load_suite(suite_path)

    assert calls == allowed
    assert len(suite.cases[0].rules[0].allowed_value_keys) == 3


def test_each_observed_value_is_canonicalized_once_across_matching_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jsonutil = production_module("jsonutil")
    suite_path = _write_argument_suite(tmp_path, [1, 2], [None, -2], rule_count=2)
    suite = production_module("suite").load_suite(suite_path)
    case = suite.cases[0]
    rules_module = production_module("rules")
    calls: list[Any] = []

    def counted(value: Any) -> bytes:
        calls.append(value)
        return cast(bytes, jsonutil.canonical_bytes(value))

    def forbidden_pairwise_comparison(*_: object) -> bool:
        raise AssertionError("pairwise canonical comparison was reached")

    monkeypatch.setattr(rules_module, "canonical_bytes", counted, raising=False)
    monkeypatch.setattr(
        rules_module, "canonical_equal", forbidden_pairwise_comparison, raising=False
    )

    evaluation = rules_module.evaluate_trace(case, case.baseline_trace)

    assert evaluation.verdict == "FAIL"
    assert calls == [None, -2]


def test_approval_arguments_and_result_are_canonicalized_once_per_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite_path = _write_approval_suite(tmp_path, "x" * 10_000)
    suite = production_module("suite").load_suite(suite_path)
    case = suite.cases[0]
    rules_module = production_module("rules")
    jsonutil = production_module("jsonutil")
    calls: list[Any] = []

    def counted(value: Any) -> bytes:
        calls.append(jsonutil.thaw_json(value))
        return cast(bytes, jsonutil.canonical_bytes(value))

    def forbidden_pairwise_comparison(*_: object) -> bool:
        raise AssertionError("repeated pairwise canonical comparison was reached")

    monkeypatch.setattr(rules_module, "canonical_bytes", counted, raising=False)
    monkeypatch.setattr(
        rules_module, "canonical_equal", forbidden_pairwise_comparison, raising=False
    )

    evaluation = rules_module.evaluate_trace(case, case.baseline_trace)

    assert evaluation.verdict == "PASS"
    assert calls == [
        {"payload": "x" * 10_000},
        {"payload": "x" * 10_000},
        {"done": True},
    ]


def test_indexed_argument_equality_preserves_object_order_and_scalar_type_semantics(
    tmp_path: Path,
) -> None:
    suite_path = _write_argument_suite(
        tmp_path,
        [{"a": 1, "b": 2}, 1],
        [{"b": 2, "a": 1}, True],
    )
    suite = production_module("suite").load_suite(suite_path)
    result = production_module("rules").evaluate_trace(
        suite.cases[0], suite.cases[0].baseline_trace
    )

    assert result.verdict == "FAIL"
    assert result.rules[0].evidence["violating_steps"] == [2]


def test_report_byte_limit_is_inclusive(
    suite_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = production_module("suite").load_suite(suite_copy)
    report_module = production_module("report")
    report = report_module.build_report(suite)
    json_bytes = _report_json_bytes(report)
    html_bytes = report_module._html(report).encode("utf-8")
    limit = len(json_bytes) + len(html_bytes)
    monkeypatch.setattr(report_module, "MAX_REPORT_BYTES", limit, raising=False)
    output = tmp_path / "out"

    report_module.write_reports(report, output)

    assert (output / "report.json").read_bytes() == json_bytes
    assert (output / "report.html").read_bytes() == html_bytes

    monkeypatch.setattr(report_module, "MAX_REPORT_BYTES", limit - 1)
    rejected = tmp_path / "rejected"
    with pytest.raises(replay_error()) as caught:
        report_module.write_reports(report, rejected)
    assert_replay_error(caught.value, "REPORT_TOO_LARGE", str(rejected))
    assert not rejected.exists()


def test_json_report_over_limit_is_rejected_before_html_and_publication(
    suite_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = production_module("suite").load_suite(suite_copy)
    report_module = production_module("report")
    report = report_module.build_report(suite)
    limit = len(_report_json_bytes(report)) - 1
    monkeypatch.setattr(report_module, "MAX_REPORT_BYTES", limit, raising=False)

    def unexpected_html(_: object) -> Iterator[str]:
        raise AssertionError("HTML rendering was reached after JSON exceeded its limit")
        yield ""

    monkeypatch.setattr(report_module, "_html_fragments", unexpected_html)
    output = tmp_path / "out"

    with pytest.raises(replay_error()) as caught:
        report_module.write_reports(report, output)

    assert_replay_error(caught.value, "REPORT_TOO_LARGE", str(output))
    assert not output.exists()


def test_json_serialization_stops_when_the_publication_budget_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_module = production_module("report")
    monkeypatch.setattr(report_module, "MAX_REPORT_BYTES", 3, raising=False)
    consumed: list[str] = []

    class RecordingEncoder:
        def __init__(self, **_: object) -> None:
            pass

        def iterencode(self, _: object) -> Iterator[str]:
            consumed.append("first")
            yield "abc"
            consumed.append("over-limit")
            yield "d"
            raise AssertionError("serializer consumed content after the limit")

    def unexpected_html(_: object) -> Iterator[str]:
        raise AssertionError("HTML rendering was reached after JSON exceeded its limit")
        yield ""

    monkeypatch.setattr(report_module.json, "JSONEncoder", RecordingEncoder)
    monkeypatch.setattr(report_module, "_html_fragments", unexpected_html)
    output = tmp_path / "out"

    with pytest.raises(replay_error()) as caught:
        report_module.write_reports({}, output)

    assert_replay_error(caught.value, "REPORT_TOO_LARGE", str(output))
    assert consumed == ["first", "over-limit"]
    assert not output.exists()


def test_html_rendering_stops_when_the_remaining_pair_budget_is_exhausted(
    suite_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = production_module("suite").load_suite(suite_copy)
    report_module = production_module("report")
    report = report_module.build_report(suite)
    json_size = len(_report_json_bytes(report))
    monkeypatch.setattr(report_module, "MAX_REPORT_BYTES", json_size + 3, raising=False)
    consumed: list[str] = []

    def fragments(_: object) -> Iterator[str]:
        consumed.append("first")
        yield "abc"
        consumed.append("over-limit")
        yield "d"
        raise AssertionError("HTML renderer consumed content after the remaining limit")

    monkeypatch.setattr(report_module, "_html_fragments", fragments, raising=False)
    output = tmp_path / "out"

    with pytest.raises(replay_error()) as caught:
        report_module.write_reports(report, output)

    assert_replay_error(caught.value, "REPORT_TOO_LARGE", str(output))
    assert consumed == ["first", "over-limit"]
    assert not output.exists()


def test_html_fragment_boundary_preserves_escaping_unicode_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary_value = "a" * 4_094 + "&<'\"" + "é😀" + "&" * 4_097
    suite_path = _write_argument_suite(tmp_path, [boundary_value], [boundary_value])
    suite = production_module("suite").load_suite(suite_path)
    report_module = production_module("report")
    report = report_module.build_report(suite)
    escaped_inputs: list[str] = []

    def recording_escape(value: str, quote: bool = True) -> str:
        escaped_inputs.append(value)
        return escape_html(value, quote=quote)

    monkeypatch.setattr(report_module.html, "escape", recording_escape)
    rendered = report_module._html(report)
    evidence = report["cases"][0]["baseline"]["rules"][0]["evidence"]
    expected_evidence = escape_html(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    )
    logical = {key: value for key, value in report.items() if key != "digest"}
    expected_digest = hashlib.sha256(
        json.dumps(
            logical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    assert max(map(len, escaped_inputs)) == 4_096
    assert rendered.count(expected_evidence) == 2
    assert r"&amp;&lt;&#x27;\&quot;é😀" in rendered
    assert report["digest"] == expected_digest


def test_html_report_over_limit_preserves_existing_artifacts(
    suite_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = production_module("suite").load_suite(suite_copy)
    report_module = production_module("report")
    report = report_module.build_report(suite)
    limit = len(_report_json_bytes(report))
    monkeypatch.setattr(report_module, "MAX_REPORT_BYTES", limit, raising=False)
    monkeypatch.setattr(report_module, "_html_fragments", lambda _: iter(("x" * (limit + 1),)))
    output = tmp_path / "out"
    output.mkdir()
    old_json = output / "report.json"
    old_html = output / "report.html"
    old_json.write_bytes(b"old-json")
    old_html.write_bytes(b"old-html")

    with pytest.raises(replay_error()) as caught:
        report_module.write_reports(report, output)

    assert_replay_error(caught.value, "REPORT_TOO_LARGE", str(output))
    assert old_json.read_bytes() == b"old-json"
    assert old_html.read_bytes() == b"old-html"
    assert not list(output.glob(".toolcall-replay-*"))


def test_report_limit_is_a_bounded_cli_error_without_reports(
    suite_copy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report_module = production_module("report")
    monkeypatch.setattr(report_module, "MAX_REPORT_BYTES", 1, raising=False)
    output = tmp_path / "out"

    code, stdout, stderr = _run_main(suite_copy, output, capsys)

    assert code == 2
    assert stdout == ""
    assert stderr == (
        f"ERROR code=REPORT_TOO_LARGE location={output} message=report exceeds 1-byte limit\n"
    )
    assert len(stderr.encode("utf-8")) < 2_048
    assert "Traceback" not in stderr
    assert not output.exists()
