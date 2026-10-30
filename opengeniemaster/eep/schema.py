"""JSON Schema generation from dataclasses plus a small validator.

Only the subset of JSON Schema needed by tool definitions is supported:
``type``, ``properties``, ``required``, ``items``, ``enum``,
``additionalProperties``, ``anyOf``, ``minimum``/``maximum`` and
``minLength``.
"""

from __future__ import annotations

import dataclasses
import enum
import types
import typing
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

__all__ = ["schema_for", "validate", "SchemaError"]


class SchemaError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def schema_for(tp: Any, *, descriptions: dict[str, str] | None = None) -> dict[str, Any]:
    """Derive a JSON schema for a dataclass or a type annotation."""
    return _schema(tp, descriptions or {}, seen=set())


def _schema(tp: Any, desc: dict[str, str], seen: set) -> dict[str, Any]:
    if tp is Any or tp is object:
        return {}
    origin = get_origin(tp)
    if origin is Union or origin is types.UnionType:
        nullable = type(None) in get_args(tp)
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            sub = _schema(args[0], desc, seen)
        else:
            sub = {"anyOf": [_schema(a, desc, seen) for a in args]}
        return _nullable(sub) if nullable else sub
    if origin is Literal:
        return {"enum": list(get_args(tp))}
    if origin in (list, typing.List, set, frozenset, tuple):
        args = get_args(tp)
        item = args[0] if args else Any
        return {"type": "array", "items": _schema(item, desc, seen)}
    if origin in (dict, typing.Dict):
        args = get_args(tp)
        val = args[1] if len(args) == 2 else Any
        out: dict[str, Any] = {"type": "object"}
        sub = _schema(val, desc, seen)
        if sub:
            out["additionalProperties"] = sub
        return out
    if isinstance(tp, type):
        if dataclasses.is_dataclass(tp):
            if tp in seen:
                return {"type": "object"}
            seen = seen | {tp}
            hints = get_type_hints(tp)
            props = {}
            required = []
            for f in dataclasses.fields(tp):
                if not f.init:
                    continue
                prop = _schema(hints[f.name], desc, seen)
                key = f"{tp.__name__}.{f.name}"
                if key in desc:
                    prop = dict(prop, description=desc[key])
                props[f.name] = prop
                if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                    required.append(f.name)
            out = {"type": "object", "properties": props}
            if required:
                out["required"] = required
            doc = (tp.__doc__ or "").strip()
            if doc and not doc.startswith(tp.__name__ + "("):
                out["description"] = doc.splitlines()[0]
            return out
        if issubclass(tp, enum.Enum):
            return {"type": "string", "enum": [m.value for m in tp]}
        if tp is bool:
            return {"type": "boolean"}
        if tp is int:
            return {"type": "integer"}
        if tp is float:
            return {"type": "number"}
        if tp is str:
            return {"type": "string"}
        if tp is dict:
            return {"type": "object"}
        if tp is list:
            return {"type": "array"}
    return {}


def _nullable(sub: dict[str, Any]) -> dict[str, Any]:
    out = dict(sub)
    if isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
        if "enum" in out:
            out["enum"] = list(out["enum"]) + [None]
        return out
    if "anyOf" in out:
        out["anyOf"] = list(out["anyOf"]) + [{"type": "null"}]
        return out
    return {"anyOf": [out, {"type": "null"}]} if out else out


_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value: Any, schema: dict[str, Any], *, path: str = "$") -> None:
    """Validate ``value`` against ``schema``; raises SchemaError listing all problems."""
    errors: list[str] = []
    _validate(value, schema, path, errors)
    if errors:
        raise SchemaError(errors)


def _validate(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if not schema:
        return
    if "anyOf" in schema:
        for sub in schema["anyOf"]:
            trial: list[str] = []
            _validate(value, sub, path, trial)
            if not trial:
                break
        else:
            errors.append(f"{path}: does not match any allowed schema")
            return
    typ = schema.get("type")
    if typ is not None:
        types_ = typ if isinstance(typ, list) else [typ]
        if not any(_TYPE_CHECKS[t](value) for t in types_ if t in _TYPE_CHECKS):
            errors.append(f"{path}: expected {'/'.join(types_)}, got {type(value).__name__}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} is above maximum {schema['maximum']}")
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        errors.append(f"{path}: string is shorter than {schema['minLength']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required property '{req}'")
        extra = schema.get("additionalProperties", True)
        for key, sub in value.items():
            if key in props:
                _validate(sub, props[key], f"{path}.{key}", errors)
            elif extra is False:
                errors.append(f"{path}: unexpected property '{key}'")
            elif isinstance(extra, dict):
                _validate(sub, extra, f"{path}.{key}", errors)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{i}]", errors)
