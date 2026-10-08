"""End-to-end test of the access-code OAuth login, as Claude's connector runs it."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.mcpserver import MCPServer
from starlette.testclient import TestClient

from docuware_mcp import auth

CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PUBLIC = "https://mcp.example.com"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> Any:
    monkeypatch.setenv("DW_MCP_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("DW_MCP_DATA_DIR", str(tmp_path))
    assert auth.user_command(["add", "anna"]) == 0
    code = re.search(r"^\s{4}(\S+)$", capsys.readouterr().out, re.M)
    assert code
    server = auth.setup(lambda **kw: MCPServer("t", **kw))
    app = server.streamable_http_app(stateless_http=True, host="0.0.0.0")
    with TestClient(app, base_url=PUBLIC) as c:
        c.access_code = code.group(1)  # type: ignore[attr-defined]
        yield c


def _register(c: Any, redirect: str = CALLBACK) -> Any:
    return c.post(
        "/register",
        json={"redirect_uris": [redirect], "token_endpoint_auth_method": "none"},
    )


def _login(c: Any, access_code: str) -> Any:
    client_id = _register(c).json()["client_id"]
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    r = c.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
        follow_redirects=False,
    )
    login_id = parse_qs(urlparse(r.headers["location"]).query)["id"][0]
    r = c.post("/login", data={"id": login_id, "code": access_code}, follow_redirects=False)
    return r, client_id, verifier


def _tokens(c: Any) -> Any:
    r, client_id, verifier = _login(c, c.access_code)
    assert r.status_code == 302 and r.headers["location"].startswith(CALLBACK)
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["state"] == ["xyz"]
    return c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    ).json()


def _call_mcp(c: Any, token: str | None) -> int:
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    return c.post("/mcp", json=body, headers=headers).status_code


def test_full_login_gives_working_token(client: Any) -> None:
    tokens = _tokens(client)
    assert _call_mcp(client, None) == 401
    assert _call_mcp(client, tokens["access_token"]) == 200


def test_wrong_code_is_rejected(client: Any) -> None:
    r, _, _ = _login(client, "wrong")
    assert r.status_code == 200 and "ungültig" in r.text


def test_foreign_redirect_cannot_register(client: Any) -> None:
    assert _register(client, "https://evil.example/cb").status_code == 400


def test_removing_user_revokes_tokens(client: Any) -> None:
    tokens = _tokens(client)
    assert auth.user_command(["remove", "anna"]) == 0
    assert _call_mcp(client, tokens["access_token"]) == 401
