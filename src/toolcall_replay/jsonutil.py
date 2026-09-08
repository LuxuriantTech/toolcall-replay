from __future__ import annotations

import json
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import TypeAlias, cast

from .errors import ReplayError

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
FrozenJsonValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | tuple["FrozenJsonValue", ...]
    | Mapping[str, "FrozenJsonValue"]
)
FrozenJsonObject: TypeAlias = Mapping[str, FrozenJsonValue]
MAX_JSON_DEPTH = 64


class _JsonDepthError(ValueError):
    pass


def _constant(value: str) -> None:
    raise ValueError(f"non-finite constant {value}")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite float")
    return number


def _pairs(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _validate_structure(value: JsonValue) -> None:
    pending: list[tuple[JsonValue, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise _JsonDepthError("maximum JSON depth exceeded")
        if isinstance(current, str):
            current.encode("utf-8")
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
        elif isinstance(current, dict):
            for key, item in current.items():
                key.encode("utf-8")
                pending.append((item, depth + 1))


def loads_strict(text: str, code: str, location: str) -> JsonValue:
    try:
        value = json.loads(
            text, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float
        )
        _validate_structure(value)
        return cast(JsonValue, value)
    except _JsonDepthError as error:
        raise ReplayError(
            code,
            location,
            f"JSON exceeds {MAX_JSON_DEPTH}-level nesting limit",
        ) from error
    except (RecursionError, UnicodeEncodeError, ValueError, json.JSONDecodeError) as error:
        raise ReplayError(code, location, "invalid JSON") from error


def json_integer(value: JsonValue) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def json_node_count(value: JsonValue | FrozenJsonValue) -> int:
    nodes = 0
    pending: list[JsonValue | FrozenJsonValue] = [value]
    while pending:
        current = pending.pop()
        nodes += 1
        if isinstance(current, (list, tuple)):
            pending.extend(current)
        elif isinstance(current, Mapping):
            nodes += len(current)
            pending.extend(current.values())
    return nodes


def freeze_json(value: JsonValue) -> FrozenJsonValue:
    if isinstance(value, list):
        return tuple(freeze_json(item) for item in value)
    if isinstance(value, dict):
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    return value


def freeze_object(value: dict[str, JsonValue]) -> FrozenJsonObject:
    return MappingProxyType({key: freeze_json(item) for key, item in value.items()})


def thaw_json(value: JsonValue | FrozenJsonValue) -> JsonValue:
    if isinstance(value, (list, tuple)):
        return [thaw_json(item) for item in value]
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    return value


def canonical_bytes(value: JsonValue | FrozenJsonValue) -> bytes:
    return json.dumps(
        thaw_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_equal(left: JsonValue | FrozenJsonValue, right: JsonValue | FrozenJsonValue) -> bool:
    return canonical_bytes(left) == canonical_bytes(right)
