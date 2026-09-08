from __future__ import annotations

from pathlib import Path

import pytest

from conftest import assert_replay_error, production_module, read_json, replay_error, write_json


@pytest.mark.parametrize(
    "rule",
    [
        {
            "rule_id": "x",
            "type": "call_presence",
            "tool": "request.lookup",
            "min_calls": 0,
            "max_calls": 1,
            "extra": True,
        },
        {
            "rule_id": "x",
            "type": "call_order",
            "first_tool": "request.lookup",
            "then_tool": "request.update",
            "extra": True,
        },
        {
            "rule_id": "x",
            "type": "argument_constraint",
            "tool": "request.lookup",
            "argument": "scope",
            "allowed_values": ["team:synthetic"],
            "extra": True,
        },
        {"rule_id": "x", "type": "forbidden_tool", "tool": "directory.export", "extra": True},
        {"rule_id": "x", "type": "approval_required", "tool": "request.update", "extra": True},
        {
            "rule_id": "x",
            "type": "expected_result",
            "status": "completed",
            "output": None,
            "extra": True,
        },
        {"rule_id": "x", "type": "step_budget", "max_steps": 1, "extra": True},
    ],
)
def test_rule_types_reject_additional_properties(suite_copy: Path, rule: dict[str, object]) -> None:
    payload = read_json(suite_copy)
    payload["cases"][0]["rules"] = [rule]
    write_json(suite_copy, payload)
    with pytest.raises(replay_error()) as caught:
        production_module("suite").load_suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_SCHEMA_INVALID", str(suite_copy))
