"""docuware-mcp — MCP server exposing a DocuWare DMS to LLM-based agents."""

from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import time
from typing import (
    Annotated,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    ParamSpec,
    Set,
    Tuple,
    TypeVar,
)

import docuware
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BeforeValidator

from docuware_mcp import __version__
from docuware_mcp.filters import (
    FilterValidationError,
    build_conditions,
    parse_combinator,
    parse_order_by,
)
from docuware_mcp.schema import ArchiveSchema, describe_dialog

log = logging.getLogger("docuware_mcp")


_SCHEMA_TTL_SECONDS = 300.0

_client: Optional[docuware.Client] = None
_schema_cache: Dict[str, Tuple[float, ArchiveSchema]] = {}


def _verify_cert_from_env() -> bool:
    raw = os.environ.get("DW_VERIFY_CERT")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _get_client() -> docuware.Client:
    global _client
    if _client is None:
        verify = _verify_cert_from_env()
        if not verify:
            log.warning("DW_VERIFY_CERT disabled — TLS certificate not verified")
        creds_file = os.environ.get("DW_CREDENTIALS_FILE")
        client_id = os.environ.get("DW_CLIENT_ID")
        if creds_file:
            log.info("Connecting to DocuWare with credentials from %s", creds_file)
            _client = docuware.connect(credentials_file=creds_file, verify_certificate=verify)
        elif client_id:
            log.info("Connecting to DocuWare via client_credentials grant")
            _client = docuware.connect(
                authenticator=docuware.ClientCredentialsAuthenticator(
                    client_id=client_id,
                    client_secret=os.environ.get("DW_CLIENT_SECRET", ""),
                ),
                verify_certificate=verify,
            )
        else:
            log.info("Connecting to DocuWare via docuware.connect()")
            _client = docuware.connect(verify_certificate=verify)
    return _client


def _resolve_archive(client: docuware.Client, name_or_id: str) -> docuware.FileCabinet:
    """Resolve an archive across all organizations by ID or display name.

    Baskets are excluded — this server exposes archives only.
    """
    candidates: List[docuware.FileCabinet] = []
    cf = name_or_id.casefold()
    for org in client.organizations:
        for fc in org.file_cabinets:
            if not isinstance(fc, docuware.FileCabinet) or fc.is_basket:
                continue
            if fc.id == name_or_id:
                return fc
            if fc.name.casefold() == cf:
                candidates.append(fc)
    if not candidates:
        raise ValueError(f"Archive not found: {name_or_id!r}")
    if len(candidates) > 1:
        names = [f"{c.name} [id={c.id}, org={c.organization.name}]" for c in candidates]
        raise ValueError(
            f"Archive name {name_or_id!r} is ambiguous across organizations. "
            f"Use the internal ID instead. Candidates: {names}"
        )
    return candidates[0]


def _get_search_dialog(fc: docuware.FileCabinet) -> docuware.SearchDialog:
    dlg = fc.search_dialog(required=True)
    if not isinstance(dlg, docuware.SearchDialog):
        raise RuntimeError(
            f"Archive {fc.name!r} returned unexpected dialog type {type(dlg).__name__}"
        )
    return dlg


def _get_schema(client: docuware.Client, archive: str) -> ArchiveSchema:
    now = time.monotonic()
    cached = _schema_cache.get(archive)
    if cached and (now - cached[0]) < _SCHEMA_TTL_SECONDS:
        return cached[1]
    fc = _resolve_archive(client, archive)
    dlg = _get_search_dialog(fc)
    schema = describe_dialog(dlg, fc.name, fc.id)
    _schema_cache[archive] = (now, schema)
    _schema_cache[fc.id] = (now, schema)
    log.info(
        "Loaded schema for archive %r [id=%s]: %d fields", fc.name, fc.id, len(schema.fields)
    )
    return schema


def _fields_to_dict(
    field_values: Any, allowed_ids: Optional[Set[str]] = None
) -> Dict[str, Any]:
    """Render a list of FieldValue objects to a ``{name: value}`` dict.

    If ``allowed_ids`` is given, only fields whose internal ID is in the set
    are emitted. This is used to suppress DocuWare system metadata that isn't
    part of the archive's search-dialog schema (and thus not described to
    callers via :func:`describe_archive`).
    """
    out: Dict[str, Any] = {}
    for fv in field_values or []:
        fid = getattr(fv, "id", None)
        if allowed_ids is not None and fid not in allowed_ids:
            continue
        name = getattr(fv, "name", None) or fid
        if not name:
            continue
        value = getattr(fv, "value", None)
        if value is not None and hasattr(value, "isoformat"):
            value = value.isoformat()
        out[name] = value
    return out


def _extract_doc_id(field_values: Iterable[Any]) -> Optional[str]:
    """Pull the DWDOCID value out of a FieldValue list as a string."""
    for fv in field_values or []:
        if getattr(fv, "id", None) == "DWDOCID":
            value = getattr(fv, "value", None)
            return str(value) if value is not None else None
    return None


# --- argument coercion ---


def _parse_json_if_string(value: Any) -> Any:
    """Tolerate stringified JSON for nested object/array tool arguments.

    Some LLM clients emit nested tool arguments as JSON-encoded strings
    instead of structured objects. When that happens, this validator
    silently parses the string so the call still succeeds. All other
    inputs pass through unchanged.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"expected a JSON object or array; got a string that is not valid JSON: {exc}"
            ) from None
    return value


FiltersArg = Annotated[
    Optional[Dict[str, Any]],
    BeforeValidator(_parse_json_if_string),
]

OrderByArg = Annotated[
    Optional[List[Dict[str, str]]],
    BeforeValidator(_parse_json_if_string),
]


# --- MCP server ---

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _surface_tool_errors(fn: Callable[_P, _R]) -> Callable[_P, _R]:
    """Re-raise expected failures as ToolError so their text reaches the model.

    The MCP SDK forwards only ToolError messages to the caller; any other
    exception is reported as a generic crash with its text kept server-side.
    Validation problems and DocuWare API errors are the caller's to fix, so
    they must arrive with their message intact.
    """

    @functools.wraps(fn)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return fn(*args, **kwargs)
        except (ValueError, docuware.DocuwareClientException) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


server = MCPServer("docuware-mcp", version=__version__)


@server.tool()
@_surface_tool_errors
def list_archives() -> List[Dict[str, str]]:
    """List archives accessible to the configured DocuWare service account.

    Analogous to ``SHOW DATABASES``. Returns each archive's display name,
    internal ID, and the organization it belongs to. Baskets are excluded.
    """
    client = _get_client()
    out: List[Dict[str, str]] = []
    for org in client.organizations:
        for fc in org.file_cabinets:
            if fc.is_basket:
                continue
            out.append(
                {
                    "name": fc.name,
                    "id": fc.id,
                    "organization": org.name,
                }
            )
    log.info("list_archives → %d archives", len(out))
    return out


@server.tool()
@_surface_tool_errors
def describe_archive(archive: str) -> Dict[str, Any]:
    """Describe an archive's schema: fields, types, and allowed operators.

    Analogous to ``DESCRIBE TABLE``. The ``operators`` list per field tells you
    which operators are valid in :func:`search`'s ``filters`` for that field.
    Keyword fields with a defined value list also expose ``select_list``.

    Args:
        archive: Display name or internal ID of the archive.
    """
    client = _get_client()
    schema = _get_schema(client, archive)
    return schema.to_dict()


@server.tool()
@_surface_tool_errors
def search(
    archive: str,
    filters: FiltersArg = None,
    combinator: str = "AND",
    order_by: OrderByArg = None,
    limit: int = 25,
    offset: int = 0,
) -> Dict[str, Any]:
    """Search documents in an archive using the structured filter DSL.

    IMPORTANT: `filters` and `order_by` are structured arguments. Pass them
    as a JSON object/array, NOT as a JSON-encoded string. Correct:
    ``{"BELEGART": "Rechnung"}``; incorrect: ``"{\\"BELEGART\\": \\"Rechnung\\"}"``.
    Stringified JSON is accepted as a fallback for buggy clients but
    indicates a malformed tool call.

    `filters` and `order_by` play distinct roles: `filters` selects which
    records are eligible (bounds, ranges, exact matches); `order_by` with
    `limit` chooses which of those eligible records to return (ranking
    and cutoff). Express any bound as a filter, even one phrased
    qualitatively — bounds map to `gte` / `lte` / `between` on numeric
    and date fields, or `like` on text. A sort plus a small limit is not
    a substitute for a bound: it ranks every record in the archive and
    rarely matches what was actually meant.

    Args:
        archive: Display name or internal ID of the archive.
        filters: JSON object mapping field names to either a bare value
            (= ``eq``) or a single-operator dict like ``{"gte": 100}``.
            Supported operators: ``eq``, ``like``, ``gte``, ``lte``,
            ``between``, ``empty``. Use :func:`describe_archive` to see
            which operators each field accepts. MUST be an object literal,
            not a stringified JSON.
        combinator: How multiple conditions are combined: ``"AND"`` (default)
            or ``"OR"``. DocuWare does not support mixed AND/OR in one query.
        order_by: Server-side ranking. JSON array of
            ``{"field": str, "direction": str}`` dicts; ``direction`` is
            ``"asc"`` (default), ``"desc"``, or ``"default"`` (archive's
            natural order). Multi-field is supported — entries are applied
            in order, with later fields acting only as tie-breakers for
            earlier ones. MUST be an array literal, not a stringified JSON.
        limit: Maximum results to return (1–200, default 25).
        offset: Number of results to skip (client-side slicing).

    Returns:
        A dict with ``items`` (list of result dicts containing ``id``,
        ``title``, ``content_type``, ``fields``), ``count`` (server-reported
        total when known), ``limit``, and ``offset``.
    """
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    client = _get_client()
    schema = _get_schema(client, archive)
    fc = _resolve_archive(client, archive)

    if not filters:
        raise ValueError(
            "search currently requires at least one filter condition. "
            "Match-everything is not yet implemented in v1."
        )

    try:
        conditions = build_conditions(filters, schema)
        sort_spec = parse_order_by(order_by, schema)
    except FilterValidationError as exc:
        raise ValueError(str(exc)) from None

    op = parse_combinator(combinator)
    dlg = _get_search_dialog(fc)

    log.info(
        "search archive=%r [id=%s] filters=%s combinator=%s order_by=%s limit=%d offset=%d",
        fc.name,
        fc.id,
        list(filters.keys()),
        combinator,
        sort_spec,
        limit,
        offset,
    )

    result_iter = dlg.search(
        conditions,
        operation=op,
        order_by=sort_spec or None,
        quote=docuware.QuoteMode.NONE,
    )
    allowed_ids = {f.internal_id for f in schema.fields}

    items: List[Dict[str, Any]] = []
    skipped = 0
    for item in result_iter:
        if skipped < offset:
            skipped += 1
            continue
        if len(items) >= limit:
            break
        items.append(
            {
                "id": _extract_doc_id(item.fields),
                "title": item.title,
                "content_type": item.content_type,
                "fields": _fields_to_dict(item.fields, allowed_ids=allowed_ids),
            }
        )

    return {
        "items": items,
        "count": getattr(result_iter, "count", None),
        "limit": limit,
        "offset": offset,
    }


def _attachment_summary(att: Any) -> Dict[str, Any]:
    return {
        "attachment_id": str(getattr(att, "id", "")),
        "filename": getattr(att, "filename", None),
        "content_type": getattr(att, "content_type", None),
        "pages": getattr(att, "pages", None),
        "size": getattr(att, "size", None),
    }


@server.tool()
@_surface_tool_errors
def get_document(archive: str, document_id: str) -> Dict[str, Any]:
    """Fetch a single document's metadata by primary-key ID.

    Returns index field values, title, content type, and a list of
    attachments (DocuWare "sections"). Does not return file content — use
    :func:`get_document_text` for OCR fulltext.

    Args:
        archive: Display name or internal ID of the archive.
        document_id: DocuWare document ID (DWDOCID).
    """
    client = _get_client()
    schema = _get_schema(client, archive)
    fc = _resolve_archive(client, archive)
    doc = fc.get_document(document_id)
    allowed_ids = {f.internal_id for f in schema.fields}
    return {
        "id": str(getattr(doc, "id", document_id)),
        "title": getattr(doc, "title", None),
        "content_type": getattr(doc, "content_type", None),
        "fields": _fields_to_dict(getattr(doc, "fields", None), allowed_ids=allowed_ids),
        "attachments": [_attachment_summary(a) for a in getattr(doc, "attachments", []) or []],
    }


@server.tool()
@_surface_tool_errors
def get_document_text(
    archive: str,
    document_id: str,
    attachment_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch the OCR fulltext of a document.

    Returns one ``text`` block per attachment. By default all attachments
    of the document are returned; pass ``attachment_id`` to fetch only one.
    Attachments without OCR (cabinet not fulltext-indexed, document not yet
    processed) appear with ``text: null`` and a populated ``error`` field
    — one missing attachment does not fail the whole call.

    Args:
        archive: Display name or internal ID of the archive.
        document_id: DocuWare document ID (DWDOCID).
        attachment_id: If given, return text only for this attachment.
            See :func:`get_document` for the attachment list.

    Returns:
        Dict with ``document_id`` and ``attachments`` (list of
        ``{attachment_id, filename, content_type, pages, char_count, text}``;
        ``error`` is set instead of ``text`` when OCR is unavailable).
    """
    client = _get_client()
    fc = _resolve_archive(client, archive)
    doc = fc.get_document(document_id)

    attachments = list(getattr(doc, "attachments", []) or [])
    if attachment_id is not None:
        attachments = [a for a in attachments if str(getattr(a, "id", "")) == attachment_id]
        if not attachments:
            raise ValueError(
                f"Document {document_id!r} has no attachment with id {attachment_id!r}"
            )

    results: List[Dict[str, Any]] = []
    for att in attachments:
        entry = _attachment_summary(att)
        try:
            text = att.text()
        except docuware.DataError as exc:
            entry["text"] = None
            entry["char_count"] = 0
            entry["error"] = str(exc)
        else:
            entry["text"] = text
            entry["char_count"] = len(text)
        results.append(entry)

    log.info(
        "get_document_text archive=%r [id=%s] doc=%s attachments=%d",
        fc.name,
        fc.id,
        document_id,
        len(results),
    )

    return {
        "document_id": str(document_id),
        "attachments": results,
    }


@server.tool()
def status() -> Dict[str, Any]:
    """Connection health: organizations and visible archive count.

    Useful as a first call to verify credentials and surface what the
    configured service account can actually see.
    """
    try:
        client = _get_client()
        orgs_info = []
        archive_count = 0
        for org in client.organizations:
            archives = [fc for fc in org.file_cabinets if not fc.is_basket]
            archive_count += len(archives)
            orgs_info.append(
                {
                    "name": org.name,
                    "id": org.id,
                    "archive_count": len(archives),
                }
            )
        return {
            "connected": True,
            "organizations": orgs_info,
            "archive_count": archive_count,
        }
    except Exception as exc:
        log.exception("status check failed")
        return {"connected": False, "error": str(exc)}


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docuware-mcp",
        description="MCP server exposing a DocuWare DMS to LLM-based agents.",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over Streamable HTTP instead of stdio (for clients like Open WebUI).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("DW_MCP_HOST", "127.0.0.1"),
        help="HTTP bind address (default: 127.0.0.1, env: DW_MCP_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("DW_MCP_PORT", "8765")),
        help="HTTP port (default: 8765, env: DW_MCP_PORT).",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("DW_MCP_PATH", "/mcp"),
        help="HTTP mount path (default: /mcp, env: DW_MCP_PATH).",
    )
    return parser


def main() -> None:
    """Entry point — run the MCP server.

    Default transport is stdio (for Claude Desktop / Claude Code). Pass ``--http``
    to serve Streamable HTTP for browser-based clients like Open WebUI.
    """
    args = _build_arg_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.http:
            log.info(
                "Serving Streamable HTTP on http://%s:%d%s",
                args.host,
                args.port,
                args.path,
            )
            server.run(
                transport="streamable-http",
                host=args.host,
                port=args.port,
                streamable_http_path=args.path,
                stateless_http=True,
            )
        else:
            server.run()
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")


if __name__ == "__main__":
    main()
