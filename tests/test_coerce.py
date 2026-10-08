"""Tests for the JSON-string fallback coercion of nested tool arguments."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from docuware_mcp.server import (
    FiltersArg,
    OrderByArg,
    _parse_json_if_string,
)


# --- _parse_json_if_string -------------------------------------------------


def test_passthrough_dict() -> None:
    value = {"foo": "bar"}
    assert _parse_json_if_string(value) is value


def test_passthrough_list() -> None:
    value = [{"field": "x", "direction": "asc"}]
    assert _parse_json_if_string(value) is value


def test_passthrough_none() -> None:
    assert _parse_json_if_string(None) is None


def test_parses_json_object_string() -> None:
    assert _parse_json_if_string('{"foo": "bar"}') == {"foo": "bar"}


def test_parses_json_array_string() -> None:
    assert _parse_json_if_string('[{"field": "x"}]') == [{"field": "x"}]


def test_parses_nested_json_string() -> None:
    raw = '{"BELEGART": "Rechnung (eingehend)", "BELEGDATUM": {"gte": "2026-05-08"}}'
    assert _parse_json_if_string(raw) == {
        "BELEGART": "Rechnung (eingehend)",
        "BELEGDATUM": {"gte": "2026-05-08"},
    }


def test_invalid_json_string_raises() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_json_if_string("{not json}")


# --- Pydantic integration via the Annotated type aliases -------------------


def test_filters_arg_accepts_dict() -> None:
    adapter = TypeAdapter(FiltersArg)
    assert adapter.validate_python({"BELEGART": "Rechnung"}) == {"BELEGART": "Rechnung"}


def test_filters_arg_accepts_none() -> None:
    adapter = TypeAdapter(FiltersArg)
    assert adapter.validate_python(None) is None


def test_filters_arg_coerces_json_string() -> None:
    adapter = TypeAdapter(FiltersArg)
    result = adapter.validate_python('{"BELEGART": "Rechnung"}')
    assert result == {"BELEGART": "Rechnung"}


def test_filters_arg_coerces_nested_json_string() -> None:
    adapter = TypeAdapter(FiltersArg)
    result = adapter.validate_python(
        '{"BELEGART": "Rechnung", "BELEGDATUM": {"gte": "2026-05-08"}}'
    )
    assert result == {
        "BELEGART": "Rechnung",
        "BELEGDATUM": {"gte": "2026-05-08"},
    }


def test_filters_arg_rejects_invalid_json_string() -> None:
    adapter = TypeAdapter(FiltersArg)
    with pytest.raises(ValidationError):
        adapter.validate_python("not json at all")


def test_filters_arg_rejects_json_array_string() -> None:
    """A stringified array does not satisfy Dict[str, Any]."""
    adapter = TypeAdapter(FiltersArg)
    with pytest.raises(ValidationError):
        adapter.validate_python('[{"foo": "bar"}]')


def test_order_by_arg_accepts_list() -> None:
    adapter = TypeAdapter(OrderByArg)
    value = [{"field": "BETRAG_BRUTTO", "direction": "desc"}]
    assert adapter.validate_python(value) == value


def test_order_by_arg_coerces_json_string() -> None:
    adapter = TypeAdapter(OrderByArg)
    result = adapter.validate_python('[{"field": "BETRAG_BRUTTO", "direction": "desc"}]')
    assert result == [{"field": "BETRAG_BRUTTO", "direction": "desc"}]


def test_order_by_arg_rejects_json_object_string() -> None:
    """A stringified object does not satisfy List[Dict[...]]."""
    adapter = TypeAdapter(OrderByArg)
    with pytest.raises(ValidationError):
        adapter.validate_python('{"field": "x"}')
