from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from .errors import ReplayError
from .jsonutil import (
    FrozenJsonObject,
    FrozenJsonValue,
    JsonValue,
    canonical_bytes,
    json_node_count,
    thaw_json,
)
from .models import Case, Evaluation, Event, Rule, RuleResult, Trace

MAX_EVALUATION_OPERATIONS = 1_000_000
MAX_EVALUATION_OUTPUT_NODES = 10_000


@dataclass(slots=True)
class EvaluationBudget:
    limit: int
    output_limit: int
    used: int = 0
    output_used: int = 0

    def consume(self, operations: int, location: str) -> None:
        if operations > self.limit - self.used:
            raise ReplayError(
                "EVALUATION_BUDGET_EXCEEDED",
                location,
                f"evaluation exceeds {self.limit}-operation limit",
            )
        self.used += operations

    def consume_output(self, nodes: int, location: str) -> None:
        if nodes > self.output_limit - self.output_used:
            raise ReplayError(
                "EVALUATION_OUTPUT_LIMIT_EXCEEDED",
                location,
                f"evaluation output exceeds {self.output_limit}-node limit",
            )
        self.output_used += nodes


def evaluation_budget() -> EvaluationBudget:
    return EvaluationBudget(MAX_EVALUATION_OPERATIONS, MAX_EVALUATION_OUTPUT_NODES)


def _call_index(trace: Trace) -> dict[str, list[Event]]:
    calls: dict[str, list[Event]] = {}
    for event in trace.events:
        if event.kind == "tool_call":
            calls.setdefault(str(event.data["tool"]), []).append(event)
    return calls


def _operation_count(case: Case, trace: Trace, calls: dict[str, list[Event]]) -> int:
    operations = len(case.rules) * len(trace.events)
    for rule in case.rules:
        if rule.type == "argument_constraint":
            allowed = cast(tuple[FrozenJsonValue, ...], rule.data["allowed_values"])
            operations += len(allowed) * len(calls.get(str(rule.data["tool"]), ()))
    return operations


def evaluation_operation_count(case: Case, trace: Trace) -> int:
    return _operation_count(case, trace, _call_index(trace))


def _output_node_count(case: Case, calls: dict[str, list[Event]]) -> int:
    nodes = 5
    observed_nodes: dict[tuple[int, str], int] = {}
    for rule in case.rules:
        data = rule.data
        nodes += 10
        if rule.type == "call_presence":
            nodes += 11 + len(calls.get(str(data["tool"]), ()))
        elif rule.type == "call_order":
            nodes += 9
        elif rule.type == "argument_constraint":
            matching_calls = calls.get(str(data["tool"]), ())
            allowed = cast(tuple[FrozenJsonValue, ...], data["allowed_values"])
            argument = str(data["argument"])
            observation_nodes = 0
            for call in matching_calls:
                arguments = cast(FrozenJsonObject, call.data["arguments"])
                if argument not in arguments:
                    observation_nodes += 5
                    continue
                key = (call.step, argument)
                if key not in observed_nodes:
                    observed_nodes[key] = json_node_count(arguments[argument])
                observation_nodes += 6 + observed_nodes[key]
            nodes += 10 + json_node_count(allowed) + observation_nodes + len(matching_calls)
        elif rule.type == "forbidden_tool":
            nodes += 5 + len(calls.get(str(data["tool"]), ()))
        elif rule.type == "approval_required":
            nodes += 5 + (5 * len(calls.get(str(data["tool"]), ())))
        elif rule.type == "expected_result":
            nodes += 7
        else:
            nodes += 5
    return nodes


def evaluation_output_node_count(case: Case, trace: Trace) -> int:
    return _output_node_count(case, _call_index(trace))


def _result(rule: Rule, verdict: str, message: str, evidence: dict[str, JsonValue]) -> RuleResult:
    return RuleResult(rule.rule_id, rule.type, verdict, message, evidence)


def _approval(
    rule: Rule,
    calls: dict[str, list[Event]],
    approvals: dict[str, Event],
    argument_keys: dict[int, bytes],
) -> RuleResult:
    tool = str(rule.data["tool"])
    consumed: set[str] = set()
    violations: list[dict[str, JsonValue]] = []
    for event in calls.get(tool, ()):
        data = event.data
        identifier = data.get("approval_id")
        reason: str | None = None
        if not isinstance(identifier, str):
            reason = "missing_reference"
        elif identifier not in approvals:
            reason = "unknown_approval"
        else:
            approval = approvals[identifier]
            if approval.step >= event.step:
                reason = "approval_not_prior"
            elif identifier in consumed:
                reason = "approval_reused"
            elif approval.data["status"] != "granted":
                reason = "approval_denied"
            elif approval.data["tool"] != tool:
                reason = "tool_mismatch"
            else:
                approval_key = argument_keys.get(approval.step)
                if approval_key is None:
                    approval_key = canonical_bytes(approval.data["arguments"])
                    argument_keys[approval.step] = approval_key
                call_key = argument_keys.get(event.step)
                if call_key is None:
                    call_key = canonical_bytes(data["arguments"])
                    argument_keys[event.step] = call_key
                if approval_key != call_key:
                    reason = "arguments_mismatch"
                else:
                    consumed.add(identifier)
        if reason is not None:
            violations.append({"step": event.step, "reason": reason})
    return _result(
        rule,
        "PASS" if not violations else "FAIL",
        "required approvals are valid"
        if not violations
        else "required approval missing or invalid",
        {"tool": tool, "violations": cast(JsonValue, violations)},
    )


def evaluate_trace(
    case: Case,
    trace: Trace,
    budget: EvaluationBudget | None = None,
) -> Evaluation:
    calls_by_tool = _call_index(trace)
    active_budget = budget if budget is not None else evaluation_budget()
    active_budget.consume(_operation_count(case, trace, calls_by_tool), case.case_id)
    active_budget.consume_output(_output_node_count(case, calls_by_tool), case.case_id)
    approvals = {
        str(event.data["approval_id"]): event for event in trace.events if event.kind == "approval"
    }
    argument_keys: dict[int, bytes] = {}
    observed_value_keys: dict[tuple[int, str], bytes] = {}
    observed_values: dict[tuple[int, str], JsonValue] = {}
    result_output_key: bytes | None = None
    results: list[RuleResult] = []
    for rule in case.rules:
        data = rule.data
        if rule.type == "call_presence":
            tool = str(data["tool"])
            calls = calls_by_tool.get(tool, ())
            count = len(calls)
            min_calls = cast(int, data["min_calls"])
            max_calls = cast(int, data["max_calls"])
            ok = min_calls <= count <= max_calls
            results.append(
                _result(
                    rule,
                    "PASS" if ok else "FAIL",
                    "call count within bounds" if ok else "call count outside bounds",
                    {
                        "tool": tool,
                        "observed_count": count,
                        "min_calls": min_calls,
                        "max_calls": max_calls,
                        "steps": cast(JsonValue, [call.step for call in calls]),
                    },
                )
            )
        elif rule.type == "call_order":
            first_tool = str(data["first_tool"])
            then_tool = str(data["then_tool"])
            first = calls_by_tool.get(first_tool, ())
            then = calls_by_tool.get(then_tool, ())
            a = first[0].step if first else None
            b = then[0].step if then else None
            ok = a is not None and b is not None and a < b
            results.append(
                _result(
                    rule,
                    "PASS" if ok else "FAIL",
                    "required call order observed" if ok else "required call order not observed",
                    {
                        "first_tool": first_tool,
                        "then_tool": then_tool,
                        "first_tool_step": a,
                        "then_tool_step": b,
                    },
                )
            )
        elif rule.type == "argument_constraint":
            tool = str(data["tool"])
            calls = calls_by_tool.get(tool, ())
            argument = str(data["argument"])
            allowed = cast(tuple[FrozenJsonValue, ...], data["allowed_values"])
            permitted = rule.allowed_value_keys
            if permitted is None:
                permitted = frozenset(canonical_bytes(value) for value in allowed)
            observations: list[dict[str, JsonValue]] = []
            bad: list[int] = []
            for call in calls:
                arguments = cast(FrozenJsonObject, call.data["arguments"])
                present = argument in arguments
                observation: dict[str, JsonValue] = {"step": call.step, "present": present}
                if present:
                    value = arguments[argument]
                    key = (call.step, argument)
                    if key not in observed_values:
                        observed_values[key] = thaw_json(value)
                    observed_value = observed_values[key]
                    observation["value"] = observed_value
                    observed_key = observed_value_keys.get(key)
                    if observed_key is None:
                        observed_key = canonical_bytes(value)
                        observed_value_keys[key] = observed_key
                    if observed_key not in permitted:
                        bad.append(call.step)
                else:
                    bad.append(call.step)
                observations.append(observation)
            ok = bool(calls) and not bad
            results.append(
                _result(
                    rule,
                    "PASS" if ok else "FAIL",
                    "tool arguments are within allowed values"
                    if ok
                    else "tool arguments violate allowed values",
                    {
                        "tool": tool,
                        "argument": argument,
                        "allowed_values": thaw_json(data["allowed_values"]),
                        "observations": cast(JsonValue, observations),
                        "violating_steps": cast(JsonValue, bad),
                    },
                )
            )
        elif rule.type == "forbidden_tool":
            tool = str(data["tool"])
            calls = calls_by_tool.get(tool, ())
            steps = [call.step for call in calls]
            ok = not steps
            results.append(
                _result(
                    rule,
                    "PASS" if ok else "FAIL",
                    "forbidden tool not called" if ok else "forbidden tool called",
                    {"tool": tool, "violating_steps": cast(JsonValue, steps)},
                )
            )
        elif rule.type == "approval_required":
            results.append(_approval(rule, calls_by_tool, approvals, argument_keys))
        elif rule.type == "expected_result":
            result = trace.events[-1].data
            if result_output_key is None:
                result_output_key = canonical_bytes(result["output"])
            expected_output_key = rule.expected_output_key
            if expected_output_key is None:
                expected_output_key = canonical_bytes(data["output"])
            matches = result_output_key == expected_output_key
            expected_status = str(data["status"])
            observed_status = str(result["status"])
            ok = observed_status == expected_status and matches
            results.append(
                _result(
                    rule,
                    "PASS" if ok else "FAIL",
                    "expected result observed" if ok else "expected result not observed",
                    {
                        "expected_status": expected_status,
                        "observed_status": observed_status,
                        "output_matches": matches,
                    },
                )
            )
        else:
            count = len(trace.events)
            max_steps = cast(int, data["max_steps"])
            ok = count <= max_steps
            results.append(
                _result(
                    rule,
                    "PASS" if ok else "FAIL",
                    "step budget respected" if ok else "step budget exceeded",
                    {"observed_steps": count, "max_steps": max_steps},
                )
            )
    return Evaluation(
        "PASS" if all(item.verdict == "PASS" for item in results) else "FAIL", tuple(results)
    )
