from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path
from typing import Any, Protocol, cast

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def production_module(name: str) -> Any:
    try:
        return importlib.import_module(f"toolcall_replay.{name}")
    except ModuleNotFoundError as error:
        pytest.fail(f"production module toolcall_replay.{name} is missing: {error}")


def replay_error() -> type[Exception]:
    return cast(type[Exception], production_module("errors").ReplayError)


class ReplayErrorLike(Protocol):
    code: str
    location: str


def assert_replay_error(error: Exception, code: str, location: str) -> None:
    typed_error = cast(ReplayErrorLike, error)
    assert typed_error.code == code
    assert typed_error.location == location


@pytest.fixture
def suite_copy(tmp_path: Path) -> Path:
    destination = tmp_path / "suite"
    shutil.copytree(EXAMPLES, destination)
    return destination / "synthetic-suite.json"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
