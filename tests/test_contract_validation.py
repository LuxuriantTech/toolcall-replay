from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import assert_replay_error, production_module, read_json, replay_error, write_json


def test_missing_suite_version_is_schema_error(suite_copy: Path) -> None:
    data = read_json(suite_copy)
    del data["schema_version"]
    write_json(suite_copy, data)
    with pytest.raises(replay_error()) as caught:
        production_module("suite").load_suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_SCHEMA_INVALID", str(suite_copy))


@pytest.mark.parametrize(("index", "field"), [(1, "status"), (4, "status"), (0, "kind")])
def test_trace_non_string_discriminators_are_schema_errors(
    suite_copy: Path, index: int, field: str
) -> None:
    path = suite_copy.parent / "traces/bounded-update.baseline.jsonl"
    values = [json.loads(x) for x in path.read_text().splitlines()]
    values[index][field] = []
    path.write_text("\n".join(json.dumps(x) for x in values) + "\n")
    with pytest.raises(replay_error()) as caught:
        production_module("trace").load_trace(path, "bounded-update")
    assert_replay_error(caught.value, "TRACE_SCHEMA_INVALID", str(path))


def test_expected_result_non_string_status_is_schema_error(suite_copy: Path) -> None:
    data = read_json(suite_copy)
    data["cases"][0]["rules"][5]["status"] = []
    write_json(suite_copy, data)
    with pytest.raises(replay_error()) as caught:
        production_module("suite").load_suite(suite_copy)
    assert_replay_error(caught.value, "SUITE_SCHEMA_INVALID", str(suite_copy))
