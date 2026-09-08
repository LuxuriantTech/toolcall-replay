from __future__ import annotations

import json
from typing import Any, cast

import jsonschema
import pytest

from conftest import ROOT


def _schema(name: str) -> dict[str, Any]:
    path = ROOT / "schemas" / name
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "name", ["suite.schema.json", "trace-event.schema.json", "report.schema.json"]
)
def test_schema_is_valid_draft_2020_12(name: str) -> None:
    schema = _schema(name)
    validator = jsonschema.validators.validator_for(schema)
    validator.check_schema(schema)
    assert validator is jsonschema.Draft202012Validator


def test_synthetic_suite_and_trace_events_conform_to_schemas() -> None:
    suite = json.loads((ROOT / "examples/synthetic-suite.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(_schema("suite.schema.json")).validate(suite)

    trace_validator = jsonschema.Draft202012Validator(_schema("trace-event.schema.json"))
    for path in sorted((ROOT / "examples/traces").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            trace_validator.validate(json.loads(line))
