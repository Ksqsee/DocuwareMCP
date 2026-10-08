"""Unit tests for the filter DSL → docuware-client translation."""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

import docuware
import pytest

from docuware_mcp.filters import (
    FilterValidationError,
    _coerce,
    _escape_dw,
    build_conditions,
    parse_combinator,
    parse_order_by,
)
from docuware_mcp.schema import (
    ArchiveSchema,
    FieldSchema,
    allowed_operators_for_type,
)


# --- fixtures --------------------------------------------------------------


def _make_field(
    name: str,
    type_: Optional[str],
    *,
    internal_id: Optional[str] = None,
    select_list: Optional[List[str]] = None,
) -> FieldSchema:
    return FieldSchema(
        name=name,
        internal_id=internal_id or name.upper(),
        type=type_,
        length=255,
        operators=sorted(allowed_operators_for_type(type_)),
        select_list=select_list,
    )


@pytest.fixture
def schema() -> ArchiveSchema:
    return ArchiveSchema(
        name="Invoices",
        internal_id="INVOICES",
        fields=[
            _make_field("Subject", "Text", internal_id="SUBJECT"),
            _make_field("Amount", "Decimal", internal_id="AMOUNT"),
            _make_field("Count", "Numeric", internal_id="COUNT"),
            _make_field("Issued", "Date", internal_id="ISSUED"),
            _make_field("Created", "DateTime", internal_id="CREATED"),
            _make_field(
                "Status", "Keyword", internal_id="STATUS", select_list=["open", "paid"]
            ),
        ],
    )


# --- _escape_dw ------------------------------------------------------------


class TestEscapeDw:
    def test_plain_text_passthrough(self) -> None:
        assert _escape_dw("hello world", escape_wildcards=True) == "hello world"

    def test_parens_always_escaped(self) -> None:
        assert _escape_dw("foo(bar)", escape_wildcards=False) == r"foo\(bar\)"
        assert _escape_dw("foo(bar)", escape_wildcards=True) == r"foo\(bar\)"

    def test_wildcards_escaped_when_requested(self) -> None:
        assert _escape_dw("a*b?c", escape_wildcards=True) == r"a\*b\?c"

    def test_wildcards_passthrough_for_like(self) -> None:
        assert _escape_dw("a*b?c", escape_wildcards=False) == "a*b?c"

    def test_already_escaped_is_idempotent(self) -> None:
        # Pre-escaped wildcards must not be double-escaped.
        assert _escape_dw(r"a\*b", escape_wildcards=True) == r"a\*b"
        assert _escape_dw(r"foo\(x\)", escape_wildcards=False) == r"foo\(x\)"

    def test_empty_string(self) -> None:
        assert _escape_dw("", escape_wildcards=True) == ""

    def test_trailing_backslash_preserved(self) -> None:
        # A trailing backslash isn't a valid escape sequence — must not crash.
        assert _escape_dw("foo\\", escape_wildcards=True) == "foo\\"

    def test_backslash_before_non_metachar(self) -> None:
        # \x is not a DW escape, so the backslash is just a regular char.
        assert _escape_dw(r"a\b", escape_wildcards=True) == r"a\b"


# --- _coerce ---------------------------------------------------------------


class TestCoerceDate:
    def test_iso_string(self) -> None:
        fld = _make_field("Issued", "Date")
        assert _coerce("2026-01-15", fld) == date(2026, 1, 15)

    def test_date_passthrough(self) -> None:
        fld = _make_field("Issued", "Date")
        d = date(2026, 1, 15)
        assert _coerce(d, fld) is d

    def test_datetime_truncated_to_date(self) -> None:
        fld = _make_field("Issued", "Date")
        assert _coerce(datetime(2026, 1, 15, 12, 30), fld) == date(2026, 1, 15)

    def test_invalid_string_raises(self) -> None:
        fld = _make_field("Issued", "Date")
        with pytest.raises(FilterValidationError, match="ISO date"):
            _coerce("15.01.2026", fld)

    def test_wrong_type_raises(self) -> None:
        fld = _make_field("Issued", "Date")
        with pytest.raises(FilterValidationError, match="ISO date string"):
            _coerce(42, fld)


class TestCoerceDateTime:
    def test_iso_string(self) -> None:
        fld = _make_field("Created", "DateTime")
        assert _coerce("2026-01-15T12:30:00", fld) == datetime(2026, 1, 15, 12, 30)

    def test_datetime_passthrough(self) -> None:
        fld = _make_field("Created", "DateTime")
        dt = datetime(2026, 1, 15, 12, 30)
        assert _coerce(dt, fld) is dt

    def test_date_promoted_to_midnight(self) -> None:
        fld = _make_field("Created", "DateTime")
        assert _coerce(date(2026, 1, 15), fld) == datetime(2026, 1, 15, 0, 0)

    def test_invalid_string_raises(self) -> None:
        fld = _make_field("Created", "DateTime")
        with pytest.raises(FilterValidationError, match="ISO 8601"):
            _coerce("not-a-date", fld)


class TestCoerceNumeric:
    def test_int_passthrough(self) -> None:
        fld = _make_field("Count", "Numeric")
        assert _coerce(42, fld) == 42

    def test_string_parses(self) -> None:
        fld = _make_field("Count", "Numeric")
        assert _coerce("42", fld) == 42

    def test_bool_rejected(self) -> None:
        # bool is a subclass of int — must be explicitly rejected.
        fld = _make_field("Count", "Numeric")
        with pytest.raises(FilterValidationError, match="bool"):
            _coerce(True, fld)

    def test_decimal_string_rejected(self) -> None:
        fld = _make_field("Count", "Numeric")
        with pytest.raises(FilterValidationError, match="not an integer"):
            _coerce("3.14", fld)

    def test_int_alias(self) -> None:
        fld = _make_field("Count", "Int")
        assert _coerce("7", fld) == 7


class TestCoerceDecimal:
    def test_int_to_float(self) -> None:
        fld = _make_field("Amount", "Decimal")
        result = _coerce(42, fld)
        assert isinstance(result, float)
        assert result == 42.0

    def test_float_passthrough(self) -> None:
        fld = _make_field("Amount", "Decimal")
        assert _coerce(3.14, fld) == 3.14

    def test_string_parses(self) -> None:
        fld = _make_field("Amount", "Decimal")
        assert _coerce("3.14", fld) == 3.14

    def test_bool_rejected(self) -> None:
        fld = _make_field("Amount", "Decimal")
        with pytest.raises(FilterValidationError, match="bool"):
            _coerce(False, fld)

    def test_invalid_string_raises(self) -> None:
        fld = _make_field("Amount", "Decimal")
        with pytest.raises(FilterValidationError, match="not a number"):
            _coerce("abc", fld)


class TestCoerceText:
    def test_string_passthrough(self) -> None:
        fld = _make_field("Subject", "Text")
        assert _coerce("hello", fld) == "hello"

    def test_non_string_stringified(self) -> None:
        fld = _make_field("Subject", "Text")
        assert _coerce(42, fld) == "42"

    def test_keyword_treated_as_text(self) -> None:
        fld = _make_field("Status", "Keyword")
        assert _coerce("open", fld) == "open"

    def test_unknown_type_treated_as_text(self) -> None:
        fld = _make_field("Mystery", "SomethingNew")
        assert _coerce("x", fld) == "x"

    def test_none_passthrough(self) -> None:
        fld = _make_field("Subject", "Text")
        assert _coerce(None, fld) is None


# --- build_conditions: bare values & eq -----------------------------------


class TestBuildConditionsEq:
    def test_bare_value_is_eq(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Subject": "Hello"}, schema)
        assert out == {"SUBJECT": "Hello"}

    def test_explicit_eq(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Subject": {"eq": "Hello"}}, schema)
        assert out == {"SUBJECT": "Hello"}

    def test_bare_value_escapes_wildcards(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Subject": "a*b"}, schema)
        assert out == {"SUBJECT": r"a\*b"}

    def test_eq_coerces_typed_value(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Issued": "2026-01-15"}, schema)
        assert out == {"ISSUED": date(2026, 1, 15)}

    def test_eq_decimal_coerces_to_float(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Amount": 100}, schema)
        assert out == {"AMOUNT": 100.0}

    def test_field_lookup_is_case_insensitive(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"subject": "x"}, schema)
        assert out == {"SUBJECT": "x"}

    def test_field_resolves_by_internal_id(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"AMOUNT": 5}, schema)
        assert out == {"AMOUNT": 5.0}


# --- build_conditions: like ------------------------------------------------


class TestBuildConditionsLike:
    def test_like_preserves_wildcards(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Subject": {"like": "*invoice*"}}, schema)
        assert out == {"SUBJECT": "*invoice*"}

    def test_like_still_escapes_parens(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Subject": {"like": "(draft)*"}}, schema)
        assert out == {"SUBJECT": r"\(draft\)*"}

    def test_like_requires_string(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="'like' requires a string"):
            build_conditions({"Subject": {"like": 42}}, schema)

    def test_like_rejected_on_numeric(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="does not support operator 'like'"):
            build_conditions({"Amount": {"like": "*"}}, schema)


# --- build_conditions: gte / lte / between --------------------------------


class TestBuildConditionsRange:
    def test_gte(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Amount": {"gte": 100}}, schema)
        assert out == {"AMOUNT": [100.0, None]}

    def test_lte(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Amount": {"lte": 100}}, schema)
        assert out == {"AMOUNT": [None, 100.0]}

    def test_between_closed(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Issued": {"between": ["2026-01-01", "2026-01-31"]}}, schema)
        assert out == {"ISSUED": [date(2026, 1, 1), date(2026, 1, 31)]}

    def test_between_open_low(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Issued": {"between": [None, "2026-01-31"]}}, schema)
        assert out == {"ISSUED": [None, date(2026, 1, 31)]}

    def test_between_open_high(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Amount": {"between": [10, None]}}, schema)
        assert out == {"AMOUNT": [10.0, None]}

    def test_between_requires_two_elements(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="2-element list"):
            build_conditions({"Amount": {"between": [1, 2, 3]}}, schema)

    def test_between_requires_list(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="2-element list"):
            build_conditions({"Amount": {"between": "1,2"}}, schema)

    def test_range_rejected_on_text(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="does not support operator 'gte'"):
            build_conditions({"Subject": {"gte": "x"}}, schema)


# --- build_conditions: empty ----------------------------------------------


class TestBuildConditionsEmpty:
    def test_explicit_empty_true(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Subject": {"empty": True}}, schema)
        assert out == {"SUBJECT": None}

    def test_null_shorthand(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Subject": None}, schema)
        assert out == {"SUBJECT": None}

    def test_empty_works_on_numeric(self, schema: ArchiveSchema) -> None:
        out = build_conditions({"Amount": None}, schema)
        assert out == {"AMOUNT": None}


# --- build_conditions: error paths ----------------------------------------


class TestBuildConditionsErrors:
    def test_unknown_field(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="Unknown field 'Bogus'"):
            build_conditions({"Bogus": "x"}, schema)

    def test_unknown_field_lists_available(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="Subject"):
            build_conditions({"Bogus": "x"}, schema)

    def test_multi_key_dict_rejected(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="single-operator dict"):
            build_conditions({"Amount": {"gte": 1, "lte": 10}}, schema)

    def test_unsupported_operator(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="does not support operator 'ne'"):
            build_conditions({"Amount": {"ne": 1}}, schema)

    def test_duplicate_field_via_alias_rejected(self, schema: ArchiveSchema) -> None:
        # Same field referenced once by name, once by internal_id.
        with pytest.raises(FilterValidationError, match="already has a condition"):
            build_conditions({"Subject": "a", "SUBJECT": "b"}, schema)

    def test_filters_must_be_dict(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="filters must be an object"):
            build_conditions([("Subject", "x")], schema)  # type: ignore[arg-type]


# --- parse_combinator ------------------------------------------------------


class TestParseCombinator:
    def test_and(self) -> None:
        assert parse_combinator("AND") is docuware.Operation.AND

    def test_or(self) -> None:
        assert parse_combinator("OR") is docuware.Operation.OR

    def test_lowercase(self) -> None:
        assert parse_combinator("and") is docuware.Operation.AND
        assert parse_combinator("or") is docuware.Operation.OR

    def test_empty_defaults_to_and(self) -> None:
        assert parse_combinator("") is docuware.Operation.AND

    def test_invalid_raises(self) -> None:
        with pytest.raises(FilterValidationError, match="combinator must be"):
            parse_combinator("XOR")


# --- parse_order_by --------------------------------------------------------


class TestParseOrderBy:
    def test_none_returns_empty(self, schema: ArchiveSchema) -> None:
        assert parse_order_by(None, schema) == []

    def test_empty_list_returns_empty(self, schema: ArchiveSchema) -> None:
        assert parse_order_by([], schema) == []

    def test_single_field_default_direction(self, schema: ArchiveSchema) -> None:
        out = parse_order_by([{"field": "Issued"}], schema)
        assert out == [("ISSUED", "asc")]

    def test_explicit_direction(self, schema: ArchiveSchema) -> None:
        out = parse_order_by([{"field": "Issued", "direction": "desc"}], schema)
        assert out == [("ISSUED", "desc")]

    def test_multi_field_preserves_order(self, schema: ArchiveSchema) -> None:
        out = parse_order_by(
            [
                {"field": "Issued", "direction": "desc"},
                {"field": "Amount", "direction": "asc"},
            ],
            schema,
        )
        assert out == [("ISSUED", "desc"), ("AMOUNT", "asc")]

    def test_direction_case_insensitive(self, schema: ArchiveSchema) -> None:
        out = parse_order_by([{"field": "Issued", "direction": "DESC"}], schema)
        assert out == [("ISSUED", "desc")]

    def test_field_resolves_by_internal_id(self, schema: ArchiveSchema) -> None:
        out = parse_order_by([{"field": "ISSUED"}], schema)
        assert out == [("ISSUED", "asc")]

    def test_default_direction_allowed(self, schema: ArchiveSchema) -> None:
        out = parse_order_by([{"field": "Issued", "direction": "default"}], schema)
        assert out == [("ISSUED", "default")]

    def test_unknown_field_raises(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="unknown field 'Bogus'"):
            parse_order_by([{"field": "Bogus"}], schema)

    def test_invalid_direction_raises(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="direction must be one of"):
            parse_order_by([{"field": "Issued", "direction": "sideways"}], schema)

    def test_duplicate_field_raises(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="already appears"):
            parse_order_by(
                [
                    {"field": "Issued", "direction": "asc"},
                    {"field": "ISSUED", "direction": "desc"},
                ],
                schema,
            )

    def test_non_list_raises(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="must be a list"):
            parse_order_by({"field": "Issued"}, schema)

    def test_non_dict_entry_raises(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="must be a dict"):
            parse_order_by(["Issued"], schema)

    def test_missing_field_raises(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="field must be a non-empty string"):
            parse_order_by([{"direction": "asc"}], schema)

    def test_empty_field_raises(self, schema: ArchiveSchema) -> None:
        with pytest.raises(FilterValidationError, match="field must be a non-empty string"):
            parse_order_by([{"field": ""}], schema)
