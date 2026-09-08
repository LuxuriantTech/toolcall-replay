from __future__ import annotations

from dataclasses import dataclass

from .jsonutil import FrozenJsonObject, JsonValue


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    type: str
    data: FrozenJsonObject
    allowed_value_keys: frozenset[bytes] | None = None
    expected_output_key: bytes | None = None


@dataclass(frozen=True, slots=True)
class Event:
    step: int
    kind: str
    data: FrozenJsonObject


@dataclass(frozen=True, slots=True)
class Trace:
    events: tuple[Event, ...]


@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    name: str
    baseline_trace: Trace
    candidate_trace: Trace
    rules: tuple[Rule, ...]


@dataclass(frozen=True, slots=True)
class Suite:
    suite_id: str
    cases: tuple[Case, ...]


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    type: str
    verdict: str
    message: str
    evidence: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Evaluation:
    verdict: str
    rules: tuple[RuleResult, ...]
