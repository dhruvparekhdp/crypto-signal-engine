"""Declared strategies: name, semver, lifecycle, schema, owner notes.

The operational gate on strategy_registry.status (incubating / trusted /
paused) is unchanged. `lifecycle` here is research, paper, or retired.

`strategies_payload` is what GET /integration/v1/strategies serves. That
route lives in scheduler/integration_v1.py. On a tree that has the route,
it should return this payload. This module does not import that server
and does not import the order modules.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

LIFECYCLES = frozenset({"research", "paper", "retired"})
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SCHEMA_TYPES = frozenset({"number", "integer", "string", "boolean"})


@dataclass(frozen=True)
class Declaration:
    name: str
    version: str
    status: str
    description: str
    parameter_schema: dict
    owner_notes: str


def validate_declaration(item: Declaration) -> list[str]:
    """Problems with one declaration. Empty means it is usable."""
    errors = []
    if not item.name or not item.name.replace("_", "").isalnum():
        errors.append("name must be a short identifier")
    if not SEMVER.match(item.version or ""):
        errors.append("version must be MAJOR.MINOR.PATCH")
    if item.status not in LIFECYCLES:
        errors.append("status must be research, paper, or retired")
    if not (item.description or "").strip():
        errors.append("description is required")
    schema = item.parameter_schema
    if not isinstance(schema, dict):
        errors.append("parameter schema must be an object")
        return errors
    for key, spec in schema.items():
        if not isinstance(key, str) or not key:
            errors.append("parameter name must be a string")
            continue
        if not isinstance(spec, dict):
            errors.append(f"{key} must be an object")
            continue
        kind = spec.get("type")
        if kind not in SCHEMA_TYPES:
            errors.append(f"{key} type must be number, integer, string, or boolean")
            continue
        if "default" not in spec:
            errors.append(f"{key} needs a default")
            continue
        if not _default_matches(kind, spec["default"]):
            errors.append(f"{key} default does not match {kind}")
    return errors


def _default_matches(kind: str, value) -> bool:
    if kind == "number":
        return type(value) in (int, float) and type(value) is not bool
    if kind == "integer":
        return type(value) is int
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return type(value) is bool
    return False


def _decl(name, version, status, description, schema, notes) -> Declaration:
    item = Declaration(name, version, status, description, schema, notes)
    problems = validate_declaration(item)
    if problems:
        raise ValueError(f"{name}: {'; '.join(problems)}")
    return item


# One declaration per strategy name. 4h and 8h specs share it. Defaults are
# the function defaults in analysis/lab/strategies.py. The swing book passes
# its own params; those stay on the registry row, not in this schema.
DECLARATIONS: dict[str, Declaration] = {
    item.name: item
    for item in (
        _decl(
            "donchian", "1.0.0", "paper",
            "Close beyond the N-bar high or low.",
            {"n": {"type": "integer", "default": 55}},
            "Swing book trades n=100 on 4h and 8h. Lab default is 55.",
        ),
        _decl(
            "keltner_break", "1.0.0", "paper",
            "Close outside the EMA plus or minus k ATR.",
            {"k": {"type": "number", "default": 2.0}},
            "Swing book uses k=2.5 on 4h and k=2.0 on 8h.",
        ),
        _decl(
            "vol_breakout", "1.0.0", "paper",
            "New 30-bar high or low on volume well above normal.",
            {"z": {"type": "number", "default": 2.0}},
            "Swing book uses z=3.0 on 4h and 8h. Lab default is 2.0.",
        ),
        _decl(
            "ichimoku", "1.0.0", "paper",
            "Tenkan/Kijun cross on the right side of the cloud.",
            {},
            "No parameters. Swing book runs it on 4h and 8h.",
        ),
    )
}


def validate_catalog() -> list[str]:
    """Every declared strategy, plus the catalog is not empty."""
    if not DECLARATIONS:
        return ["catalog is empty"]
    found = []
    for item in DECLARATIONS.values():
        found.extend(f"{item.name}: {problem}" for problem in validate_declaration(item))
    return found


def version_for(signal_type: str, timeframe: str = "") -> str:
    """Semver for a swing signal. Empty for anything not in the catalog."""
    del timeframe  # the version is per strategy name, shared across timeframes
    if not (signal_type or "").startswith("swing_"):
        return ""
    item = DECLARATIONS.get(signal_type.removeprefix("swing_"))
    return item.version if item else ""


def apply_declaration(row, strategy_id: str) -> None:
    """Copy the declaration onto a strategy_registry row. No-op if unknown."""
    item = DECLARATIONS.get(strategy_id)
    if item is None:
        return
    row.name = item.name
    row.version = item.version
    row.lifecycle = item.status
    row.description = item.description
    row.parameter_schema = json.dumps(item.parameter_schema, sort_keys=True)
    row.owner_notes = item.owner_notes


def _schema_of(row) -> dict:
    raw = getattr(row, "parameter_schema", "") or ""
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    item = DECLARATIONS.get(getattr(row, "name", "") or "")
    return dict(item.parameter_schema) if item else {}


def strategies_payload(rows=None) -> list[dict]:
    """Rows for /integration/v1/strategies. Declaration fields are always set."""
    if not rows:
        return [
            {
                "name": item.name,
                "version": item.version,
                "status": item.status,
                "description": item.description,
                "parameter_schema": item.parameter_schema,
                "owner_notes": item.owner_notes,
                "spec_key": "",
                "timeframe": "",
                "operational_status": "",
            }
            for item in DECLARATIONS.values()
        ]
    out = []
    for row in rows:
        spec = row.spec_key or ""
        timeframe, _, name = spec.partition("@")
        item = DECLARATIONS.get(row.name or name)
        out.append({
            "name": row.name or (item.name if item else name),
            "version": row.version or (item.version if item else ""),
            "status": row.lifecycle or (item.status if item else "research"),
            "description": row.description or (item.description if item else ""),
            "parameter_schema": _schema_of(row),
            "owner_notes": row.owner_notes or (item.owner_notes if item else ""),
            "spec_key": spec,
            "timeframe": timeframe if name else "",
            "operational_status": row.status or "",
        })
    return out
