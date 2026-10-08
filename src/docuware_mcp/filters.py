"""Filter DSL → docuware-client conditions translation.

Operators (v1):

- bare value (or ``{"eq": v}``): exact match
- ``{"like": "*pattern*"}``: wildcard match (``*`` and ``?``)
- ``{"gte": v}`` / ``{"lte": v}``: ≥ / ≤ inclusive
- ``{"between": [low, high]}``: low ≤ field ≤ high (use ``null`` for open bound)
- ``{"empty": true}`` (or value ``null``): field is empty

Combinator (top-level): ``"AND"`` (default) or ``"OR"``. Flat — no nested groups.

Limitations (per design — see docuware-mcp-design-decisions.md):

- No ``gt`` / ``lt`` (DW only has inclusive ranges)
- No ``ne``, ``in``, ``regex`` (would require backend-fakery)
- No nested boolean groups (DW backend has one global combinator)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, FrozenSet, List, Set, Tuple

import docuware

from docuware_mcp.schema import ArchiveSchema, FieldSchema


class FilterValidationError(ValueError):
    """Raised when the filter DSL contains an invalid construct."""


_DW_METACHARS = "()*?"


def _escape_dw(value: str, *, escape_wildcards: bool) -> str:
    """Escape DocuWare metacharacters in a string value.

    ``*`` and ``?`` are escaped only when ``escape_wildcards`` is True. Already
    backslash-escaped sequences are left alone, so the function is idempotent.
    """
    chars = _DW_METACHARS if escape_wildcards else "()"
    out: List[str] = []
    i = 0
    n = len(value)
    while i < n:
        c = value[i]
        if c == "\\" and i + 1 < n and value[i + 1] in _DW_METACHARS:
            out.append(c)
            out.append(value[i + 1])
            i += 2
            continue
        if c in chars:
            out.append("\\")
        out.append(c)
        i += 1
    return "".join(out)


def _coerce(value: Any, fld: FieldSchema) -> Any:
    """Coerce a JSON value to the type matching the field's DW type.

    Strings remain strings on text-like fields; ISO strings become ``date``,
    ``datetime``, ``int``, or ``float`` on the typed fields. Failures raise
    :class:`FilterValidationError` with an LLM-actionable message.
    """
    if value is None:
        return None

    ftype = (fld.type or "").lower()

    if ftype == "date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise FilterValidationError(
                    f"Field {fld.name!r} is type Date — value {value!r} is not a "
                    f"valid ISO date (YYYY-MM-DD)"
                ) from exc
        raise FilterValidationError(
            f"Field {fld.name!r} is type Date — expected ISO date string, got "
            f"{type(value).__name__}"
        )

    if ftype == "datetime":
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError as exc:
                raise FilterValidationError(
                    f"Field {fld.name!r} is type DateTime — value {value!r} is not "
                    f"valid ISO 8601"
                ) from exc
        raise FilterValidationError(
            f"Field {fld.name!r} is type DateTime — expected ISO datetime string, got "
            f"{type(value).__name__}"
        )

    if ftype in ("numeric", "int"):
        if isinstance(value, bool):
            raise FilterValidationError(f"Field {fld.name!r} is type Numeric — got bool")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError as exc:
                raise FilterValidationError(
                    f"Field {fld.name!r} is type Numeric — value {value!r} is not an integer"
                ) from exc
        raise FilterValidationError(
            f"Field {fld.name!r} is type Numeric — expected integer, got {type(value).__name__}"
        )

    if ftype == "decimal":
        if isinstance(value, bool):
            raise FilterValidationError(f"Field {fld.name!r} is type Decimal — got bool")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError as exc:
                raise FilterValidationError(
                    f"Field {fld.name!r} is type Decimal — value {value!r} is not a number"
                ) from exc
        raise FilterValidationError(
            f"Field {fld.name!r} is type Decimal — expected number, got {type(value).__name__}"
        )

    # Text, Memo, Keyword, unknown: pass strings through, stringify others.
    return value if isinstance(value, str) else str(value)


def _format(value: Any, *, escape_wildcards: bool) -> Any:
    """Apply DW metacharacter escaping; pass non-strings through unchanged.

    The returned value is suitable for the dict form of
    :meth:`docuware.SearchDialog.search` when called with
    ``quote=QuoteMode.NONE`` (i.e. the client does no further escaping).
    """
    if value is None:
        return None
    if isinstance(value, str):
        return _escape_dw(value, escape_wildcards=escape_wildcards)
    return value


def _translate_one(fld: FieldSchema, spec: Any) -> Any:
    """Translate one ``(field, spec)`` pair to the value form expected by DW."""
    allowed = set(fld.operators)

    # Shorthand: null value means "field is empty"
    if spec is None:
        if "empty" not in allowed:
            raise FilterValidationError(
                f"Field {fld.name!r} (type={fld.type}) does not support 'empty' check"
            )
        return None

    # Operator dict
    if isinstance(spec, dict):
        if len(spec) != 1:
            raise FilterValidationError(
                f"Field {fld.name!r}: filter must be a single-operator dict, got "
                f"keys {list(spec.keys())}"
            )
        op, raw = next(iter(spec.items()))
        if op not in allowed:
            raise FilterValidationError(
                f"Field {fld.name!r} (type={fld.type}) does not support operator {op!r}. "
                f"Allowed: {', '.join(sorted(allowed))}"
            )

        if op == "empty":
            return None

        if op == "eq":
            return _format(_coerce(raw, fld), escape_wildcards=True)

        if op == "like":
            if not isinstance(raw, str):
                raise FilterValidationError(
                    f"Field {fld.name!r}: 'like' requires a string value, got "
                    f"{type(raw).__name__}"
                )
            return _format(raw, escape_wildcards=False)

        if op == "gte":
            return [_format(_coerce(raw, fld), escape_wildcards=True), None]

        if op == "lte":
            return [None, _format(_coerce(raw, fld), escape_wildcards=True)]

        if op == "between":
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                raise FilterValidationError(
                    f"Field {fld.name!r}: 'between' requires a 2-element list "
                    f"[low, high] (use null for an open bound)"
                )
            low, high = raw
            return [
                _format(_coerce(low, fld), escape_wildcards=True) if low is not None else None,
                _format(_coerce(high, fld), escape_wildcards=True)
                if high is not None
                else None,
            ]

        # Unreachable: 'allowed' check above filtered unsupported operators.
        raise FilterValidationError(f"Internal: operator {op!r} accepted but not implemented")

    # Bare value = eq
    if "eq" not in allowed:
        raise FilterValidationError(
            f"Field {fld.name!r} (type={fld.type}) does not support 'eq'"
        )
    return _format(_coerce(spec, fld), escape_wildcards=True)


def build_conditions(filters: Dict[str, Any], schema: ArchiveSchema) -> Dict[str, Any]:
    """Translate the filter DSL to a dict for ``SearchDialog.search()``.

    The returned dict must be passed to docuware-client with
    ``quote=docuware.QuoteMode.NONE`` because all values have already been
    escaped according to their per-operator wildcard intent.
    """
    if not isinstance(filters, dict):
        raise FilterValidationError(f"filters must be an object, got {type(filters).__name__}")

    out: Dict[str, Any] = {}
    for fname, spec in filters.items():
        try:
            fld = schema.field_by_name(fname)
        except KeyError:
            raise FilterValidationError(
                f"Unknown field {fname!r}. Available: {', '.join(schema.field_names())}"
            ) from None
        if fld.internal_id in out:
            raise FilterValidationError(
                f"Field {fname!r} resolves to {fld.internal_id!r} which already has "
                f"a condition (multiple conditions on the same field are not supported)"
            )
        out[fld.internal_id] = _translate_one(fld, spec)
    return out


_ORDER_DIRECTIONS: FrozenSet = frozenset({"asc", "desc", "default"})


def parse_order_by(order_by: Any, schema: ArchiveSchema) -> List[Tuple[str, str]]:
    """Validate the ``order_by`` spec and resolve fields to internal IDs.

    Accepts a list of ``{"field": str, "direction": "asc"|"desc"|"default"}``
    dicts (``direction`` optional, defaults to ``"asc"``) and returns a list
    of ``(internal_id, direction)`` tuples ready for ``SearchDialog.search()``.
    """
    if order_by is None:
        return []
    if not isinstance(order_by, list):
        raise FilterValidationError(
            f"order_by must be a list of {{field, direction}} dicts, got "
            f"{type(order_by).__name__}"
        )

    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for i, entry in enumerate(order_by):
        if not isinstance(entry, dict):
            raise FilterValidationError(
                f"order_by[{i}] must be a dict with keys 'field' and 'direction', "
                f"got {type(entry).__name__}"
            )
        fname = entry.get("field")
        if not isinstance(fname, str) or not fname:
            raise FilterValidationError(f"order_by[{i}].field must be a non-empty string")
        direction = entry.get("direction") or "asc"
        if not isinstance(direction, str):
            raise FilterValidationError(f"order_by[{i}].direction must be a string")
        norm = direction.strip().lower()
        if norm not in _ORDER_DIRECTIONS:
            raise FilterValidationError(
                f"order_by[{i}].direction must be one of "
                f"{', '.join(sorted(_ORDER_DIRECTIONS))}, got {direction!r}"
            )
        try:
            fld = schema.field_by_name(fname)
        except KeyError:
            raise FilterValidationError(
                f"order_by[{i}]: unknown field {fname!r}. Available: "
                f"{', '.join(schema.field_names())}"
            ) from None
        if fld.internal_id in seen:
            raise FilterValidationError(
                f"order_by[{i}]: field {fname!r} (id={fld.internal_id!r}) already "
                f"appears earlier in order_by"
            )
        seen.add(fld.internal_id)
        out.append((fld.internal_id, norm))
    return out


def parse_combinator(value: str) -> docuware.Operation:
    upper = (value or "AND").upper()
    if upper == "AND":
        return docuware.Operation.AND
    if upper == "OR":
        return docuware.Operation.OR
    raise FilterValidationError(f"combinator must be 'AND' or 'OR', got {value!r}")
