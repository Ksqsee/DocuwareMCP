"""Archive schema and field-type → operator mapping.

The mapping is a static table held in the MCP server. We deliberately do
not derive it from DocuWare's own ``OperatorTable``: the MCP-facing
operator vocabulary is intentionally smaller than what DW would accept,
so the LLM's mental model stays simple and consistent.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional

import docuware

log = logging.getLogger(__name__)


# DocuWare DWFieldType values observed in the wild are case-insensitive.
# Keys here are lowercased before lookup.
_TYPE_OPERATORS: Dict[str, FrozenSet[str]] = {
    "text": frozenset({"eq", "like", "empty"}),
    "memo": frozenset({"eq", "like", "empty"}),
    "keyword": frozenset({"eq", "like", "empty"}),
    "keywords": frozenset({"eq", "like", "empty"}),
    "numeric": frozenset({"eq", "gte", "lte", "between", "empty"}),
    "int": frozenset({"eq", "gte", "lte", "between", "empty"}),
    "decimal": frozenset({"eq", "gte", "lte", "between", "empty"}),
    "date": frozenset({"eq", "gte", "lte", "between", "empty"}),
    "datetime": frozenset({"eq", "gte", "lte", "between", "empty"}),
}

_DEFAULT_OPERATORS: FrozenSet[str] = frozenset({"eq", "empty"})


def allowed_operators_for_type(dw_type: Optional[str]) -> FrozenSet[str]:
    """Return the operator set the MCP DSL allows on a field of this DW type."""
    if not dw_type:
        return _DEFAULT_OPERATORS
    return _TYPE_OPERATORS.get(dw_type.lower(), _DEFAULT_OPERATORS)


@dataclass(frozen=True)
class FieldSchema:
    name: str
    internal_id: str
    type: Optional[str]
    length: int
    operators: List[str]
    select_list: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ArchiveSchema:
    name: str
    internal_id: str
    fields: List[FieldSchema] = field(default_factory=list)

    def field_by_name(self, name: str) -> FieldSchema:
        cf = name.casefold()
        for f in self.fields:
            if f.name.casefold() == cf or f.internal_id.casefold() == cf:
                return f
        raise KeyError(name)

    def field_names(self) -> List[str]:
        return [f.name for f in self.fields]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "internal_id": self.internal_id,
            "fields": [f.to_dict() for f in self.fields],
        }


def describe_dialog(
    dialog: docuware.SearchDialog, archive_name: str, archive_id: str
) -> ArchiveSchema:
    """Build an ArchiveSchema from a SearchDialog's field definitions."""
    out: List[FieldSchema] = []
    for sf in dialog.fields.values():
        ops = sorted(allowed_operators_for_type(sf.type))
        select_list: Optional[List[str]] = None
        if sf.type and sf.type.lower() in ("keyword", "keywords"):
            try:
                values = sf.values()
                if values:
                    select_list = [str(v) for v in values]
            except Exception as exc:
                log.debug("select_list fetch failed for field %r: %s", sf.id, exc)
        out.append(
            FieldSchema(
                name=sf.name,
                internal_id=sf.id,
                type=sf.type,
                length=sf.length if sf.length is not None else -1,
                operators=ops,
                select_list=select_list,
            )
        )
    return ArchiveSchema(name=archive_name, internal_id=archive_id, fields=out)
