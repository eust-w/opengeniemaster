"""Dataclass <-> JSON-compatible conversion using only the standard library.

Protocol objects are plain dataclasses. ``to_dict`` produces JSON-ready data
(enums become their values, ``None`` fields are dropped by default) and
``from_dict`` rebuilds typed objects from that data using type hints. Unknown
keys are ignored so that newer peers can add fields without breaking older
ones.
"""

from __future__ import annotations

import dataclasses
import enum
import types
import typing
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

__all__ = ["SerdeError", "to_dict", "from_dict"]


class SerdeError(ValueError):
    """Raised when data cannot be converted to the requested type."""


_HINTS: dict[type, dict[str, Any]] = {}


def _hints(cls: type) -> dict[str, Any]:
    hints = _HINTS.get(cls)
    if hints is None:
        hints = get_type_hints(cls)
        _HINTS[cls] = hints
    return hints


def to_dict(obj: Any, *, drop_none: bool = True) -> Any:
    """Convert dataclasses/enums/containers recursively to JSON-ready data."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            value = getattr(obj, f.name)
            if value is None and drop_none:
                continue
            out[f.name] = to_dict(value, drop_none=drop_none)
        return out
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, dict):
        return {str(k): to_dict(v, drop_none=drop_none) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted(to_dict(v, drop_none=drop_none) for v in obj)
    if isinstance(obj, (list, tuple)):
        return [to_dict(v, drop_none=drop_none) for v in obj]
    return obj


def from_dict(cls: Any, data: Any) -> Any:
    """Build an instance of ``cls`` (usually a dataclass) from JSON-like data."""
    name = getattr(cls, "__name__", str(cls))
    return _convert(cls, data, name)


def _convert(tp: Any, value: Any, path: str) -> Any:
    if tp is Any or tp is object:
        return value
    origin = get_origin(tp)

    if origin is Union or origin is types.UnionType:
        args = get_args(tp)
        if value is None:
            if type(None) in args:
                return None
            raise SerdeError(f"{path}: null is not allowed")
        errors = []
        for arg in args:
            if arg is type(None):
                continue
            try:
                return _convert(arg, value, path)
            except SerdeError as exc:
                errors.append(str(exc))
        raise SerdeError(f"{path}: value {value!r} matches no allowed type ({'; '.join(errors)})")

    if origin is Literal:
        if value in get_args(tp):
            return value
        raise SerdeError(f"{path}: {value!r} is not one of {get_args(tp)!r}")

    if value is None:
        raise SerdeError(f"{path}: null is not allowed")

    if origin in (list, typing.List, set, frozenset, tuple):
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise SerdeError(f"{path}: expected a list, got {type(value).__name__}")
        args = get_args(tp)
        if origin is tuple and args and args[-1] is not Ellipsis:
            if len(args) != len(value):
                raise SerdeError(f"{path}: expected {len(args)} items, got {len(value)}")
            return tuple(_convert(a, v, f"{path}[{i}]") for i, (a, v) in enumerate(zip(args, value)))
        item_tp = args[0] if args else Any
        items = [_convert(item_tp, v, f"{path}[{i}]") for i, v in enumerate(value)]
        if origin in (set, frozenset):
            return origin(items)
        if origin is tuple:
            return tuple(items)
        return items

    if origin in (dict, typing.Dict):
        if not isinstance(value, dict):
            raise SerdeError(f"{path}: expected an object, got {type(value).__name__}")
        args = get_args(tp)
        val_tp = args[1] if len(args) == 2 else Any
        return {str(k): _convert(val_tp, v, f"{path}.{k}") for k, v in value.items()}

    if isinstance(tp, type):
        if dataclasses.is_dataclass(tp):
            if isinstance(value, tp):
                return value
            if not isinstance(value, dict):
                raise SerdeError(f"{path}: expected an object for {tp.__name__}, got {type(value).__name__}")
            hints = _hints(tp)
            kwargs = {}
            for f in dataclasses.fields(tp):
                if not f.init:
                    continue
                if f.name in value:
                    kwargs[f.name] = _convert(hints[f.name], value[f.name], f"{path}.{f.name}")
                elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                    raise SerdeError(f"{path}: missing required field '{f.name}'")
            return tp(**kwargs)
        if issubclass(tp, enum.Enum):
            if isinstance(value, tp):
                return value
            try:
                return tp(value)
            except ValueError as exc:
                allowed = [m.value for m in tp]
                raise SerdeError(f"{path}: {value!r} is not one of {allowed}") from exc
        if tp is bool:
            if isinstance(value, bool):
                return value
            raise SerdeError(f"{path}: expected a boolean, got {value!r}")
        if tp is int:
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            if isinstance(value, float) and value.is_integer():
                return int(value)
            raise SerdeError(f"{path}: expected an integer, got {value!r}")
        if tp is float:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            raise SerdeError(f"{path}: expected a number, got {value!r}")
        if tp is str:
            if isinstance(value, str):
                return value
            raise SerdeError(f"{path}: expected a string, got {value!r}")
        if tp is dict:
            if isinstance(value, dict):
                return value
            raise SerdeError(f"{path}: expected an object, got {type(value).__name__}")
        if tp is list:
            if isinstance(value, list):
                return value
            raise SerdeError(f"{path}: expected a list, got {type(value).__name__}")
        if isinstance(value, tp):
            return value
        raise SerdeError(f"{path}: expected {tp.__name__}, got {type(value).__name__}")

    return value
