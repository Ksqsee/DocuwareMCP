"""Tests for archive resolution and the OCR text budget in the server tools."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import docuware
import pytest

from docuware_mcp import server


def _client(*orgs: SimpleNamespace) -> Any:
    return SimpleNamespace(organizations=list(orgs))


def _org(name: str, *cabinets: dict) -> SimpleNamespace:
    org: Any = SimpleNamespace(name=name, file_cabinets=[])
    org.file_cabinets = [docuware.FileCabinet(c, org) for c in cabinets]
    return org


def test_resolve_by_name_case_insensitive() -> None:
    client = _client(_org("Acme", {"Name": "Archiv", "Id": "fc1"}))
    assert server._resolve_archive(client, "archiv").id == "fc1"


def test_resolve_ambiguous_name_needs_id() -> None:
    client = _client(
        _org("Acme", {"Name": "Archiv", "Id": "fc1"}),
        _org("Beta", {"Name": "Archiv", "Id": "fc2"}),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        server._resolve_archive(client, "Archiv")
    assert server._resolve_archive(client, "fc2").id == "fc2"


def test_resolve_skips_baskets() -> None:
    client = _client(_org("Acme", {"Name": "Inbox", "Id": "b1", "IsBasket": True}))
    with pytest.raises(ValueError, match="not found"):
        server._resolve_archive(client, "Inbox")
    with pytest.raises(ValueError, match="not found"):
        server._resolve_archive(client, "b1")


def _attachment(att_id: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=att_id,
        filename=f"{att_id}.pdf",
        content_type="application/pdf",
        pages=1,
        size=0,
        text=lambda: text,
    )


def test_document_text_budget_spans_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = SimpleNamespace(attachments=[_attachment("a", "x" * 8), _attachment("b", "y" * 8)])
    fc = SimpleNamespace(name="Archiv", id="fc1", get_document=lambda _id: doc)
    monkeypatch.setattr(server, "_get_client", lambda: None)
    monkeypatch.setattr(server, "_resolve_archive", lambda _c, _a: fc)

    out = server.get_document_text("Archiv", "1", max_chars=10)["attachments"]

    assert [(a["text"], a["char_count"], a["truncated"]) for a in out] == [
        ("x" * 8, 8, False),
        ("yy", 8, True),
    ]
