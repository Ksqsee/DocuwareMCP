"""OAuth login for the hosted HTTP server: one personal access code per colleague.

Claude (claude.ai / Claude Desktop connectors) runs the standard MCP OAuth flow
against this server. Our authorize step shows a small page asking for the
person's access code; codes are managed with ``docuware-mcp user add|remove|list``
and stored only as SHA-256 hashes. Removing a user revokes their tokens at once,
because every token check re-reads the users file.

State (registered clients, pending logins, codes, tokens) lives in one SQLite
file so a restart does not log everyone out.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

log = logging.getLogger("docuware_mcp.auth")

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300
LOGIN_TTL = 600

# Claude's OAuth callbacks. Only these may receive codes, so a third party cannot
# register its own client and phish an access code through our login page.
DEFAULT_REDIRECTS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


# --- users file: {"name": "<sha256 of access code>"} ---


def load_users(path: Path) -> Dict[str, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_users(path: Path, users: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, indent=2, sort_keys=True), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def find_user(path: Path, code: str) -> Optional[str]:
    digest = _hash(code.strip())
    match = None
    for name, stored in load_users(path).items():
        if hmac.compare_digest(stored, digest):
            match = name
    return match


# --- persistence ---


class _Store:
    """Tiny key/value table with expiry. Keys holding secrets are stored hashed."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, expires REAL)"
        )
        self._lock = threading.Lock()

    def put(self, key: str, value: Dict[str, Any], ttl: Optional[float]) -> None:
        expires = time.time() + ttl if ttl else None
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", (key, json.dumps(value), expires)
            )

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT value, expires FROM kv WHERE key = ?", (key,)
            ).fetchone()
        if row is None or (row[1] is not None and row[1] < time.time()):
            return None
        return json.loads(row[0])

    def pop(self, key: str) -> Optional[Dict[str, Any]]:
        value = self.get(key)
        self.delete(key)
        return value

    def delete(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE key = ?", (key,))
            self._db.execute("DELETE FROM kv WHERE expires < ?", (time.time(),))


# --- OAuth provider ---


class AccessCodeProvider:
    def __init__(
        self, public_url: str, users_file: Path, db_file: Path, allowed_redirects: List[str]
    ) -> None:
        self.public_url = public_url.rstrip("/")
        self.users_file = users_file
        self.allowed_redirects = set(allowed_redirects)
        self.store = _Store(db_file)

    def _active(self, user: Optional[str]) -> bool:
        return bool(user) and user in load_users(self.users_file)

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        data = self.store.get(f"client:{client_id}")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        bad = [u for u in uris if u not in self.allowed_redirects]
        if bad:
            raise RegistrationError("invalid_redirect_uri", f"Redirect URI not allowed: {bad}")
        assert client_info.client_id
        self.store.put(
            f"client:{client_info.client_id}", client_info.model_dump(mode="json"), None
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if str(params.redirect_uri) not in self.allowed_redirects:
            raise AuthorizeError("invalid_request", "Redirect URI not allowed")
        login_id = secrets.token_urlsafe(32)
        self.store.put(
            f"login:{login_id}",
            {"client_id": client.client_id, "params": params.model_dump(mode="json")},
            LOGIN_TTL,
        )
        return f"{self.public_url}/login?id={login_id}"

    def complete_login(self, login_id: str, access_code: str) -> Optional[str]:
        """Check the access code; on success return the redirect back to the client."""
        user = find_user(self.users_file, access_code)
        if user is None:
            return None
        pending = self.store.pop(f"login:{login_id}")
        if pending is None:
            return None
        params = AuthorizationParams.model_validate(pending["params"])
        code = secrets.token_urlsafe(32)
        self.store.put(
            f"code:{_hash(code)}",
            AuthorizationCode(
                code=code,
                scopes=params.scopes or [],
                expires_at=time.time() + CODE_TTL,
                client_id=pending["client_id"],
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource,
                subject=user,
            ).model_dump(mode="json"),
            CODE_TTL,
        )
        log.info("login ok: user=%s client=%s", user, pending["client_id"])
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        data = self.store.get(f"code:{_hash(authorization_code)}")
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate(data)

    def _issue(
        self, client_id: str, scopes: List[str], user: str, resource: Optional[str]
    ) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        self.store.put(
            f"access:{_hash(access)}",
            AccessToken(
                token=access,
                client_id=client_id,
                scopes=scopes,
                expires_at=now + ACCESS_TTL,
                resource=resource,
                subject=user,
            ).model_dump(mode="json"),
            ACCESS_TTL,
        )
        self.store.put(
            f"refresh:{_hash(refresh)}",
            RefreshToken(
                token=refresh,
                client_id=client_id,
                scopes=scopes,
                expires_at=now + REFRESH_TTL,
                subject=user,
            ).model_dump(mode="json"),
            REFRESH_TTL,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self.store.pop(f"code:{_hash(authorization_code.code)}") is None:
            raise TokenError("invalid_grant", "Authorization code already used")
        if not self._active(authorization_code.subject):
            raise TokenError("invalid_grant", "User no longer has access")
        assert client.client_id and authorization_code.subject
        return self._issue(
            client.client_id,
            authorization_code.scopes,
            authorization_code.subject,
            authorization_code.resource,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        data = self.store.get(f"refresh:{_hash(refresh_token)}")
        if (
            not data
            or data["client_id"] != client.client_id
            or not self._active(data["subject"])
        ):
            return None
        return RefreshToken.model_validate(data)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: List[str]
    ) -> OAuthToken:
        if self.store.pop(f"refresh:{_hash(refresh_token.token)}") is None:
            raise TokenError("invalid_grant", "Refresh token already used")
        assert client.client_id and refresh_token.subject
        return self._issue(
            client.client_id, scopes or refresh_token.scopes, refresh_token.subject, None
        )

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        data = self.store.get(f"access:{_hash(token)}")
        if not data or not self._active(data["subject"]):
            return None
        return AccessToken.model_validate(data)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "access" if isinstance(token, AccessToken) else "refresh"
        self.store.delete(f"{kind}:{_hash(token.token)}")

    async def exchange_identity_assertion(self, client: Any, params: Any) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "Not supported")


# --- login page ---

_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DocuWare – Anmelden</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f4f4f5;margin:0;display:grid;
place-items:center;min-height:100vh}}
form{{background:#fff;padding:2rem;border-radius:12px;max-width:22rem;width:calc(100% - 2rem);
box-shadow:0 1px 4px #0002}}
h1{{font-size:1.25rem;margin:0 0 1rem}}
label{{display:block;margin-bottom:.5rem}}
input{{width:100%;box-sizing:border-box;padding:.6rem;font-size:1rem;margin-bottom:1rem}}
button{{width:100%;padding:.7rem;font-size:1rem;background:#1d4ed8;color:#fff;border:0;
border-radius:8px;cursor:pointer}}
.err{{color:#b91c1c;margin:0 0 1rem}}
</style></head><body>
<form method="post" action="login">
<h1>DocuWare für Claude</h1>
{error}
<input type="hidden" name="id" value="{login_id}">
<label for="code">Ihr persönlicher Zugangscode / Your access code</label>
<input id="code" name="code" type="password" autocomplete="off" required autofocus>
<button type="submit">Anmelden / Sign in</button>
</form></body></html>"""


def _page(login_id: str, error: str = "") -> HTMLResponse:
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(
        _PAGE.format(login_id=html.escape(login_id), error=err),
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )


def setup(server_factory: Any) -> Any:
    """Build the MCP server with OAuth enabled, from environment variables."""
    public_url = os.environ["DW_MCP_PUBLIC_URL"].rstrip("/")
    data_dir = Path(os.environ.get("DW_MCP_DATA_DIR", "/data"))
    redirects = os.environ.get("DW_MCP_ALLOWED_REDIRECTS")
    provider = AccessCodeProvider(
        public_url,
        users_file=users_file_path(),
        db_file=data_dir / "auth.db",
        allowed_redirects=redirects.split(",") if redirects else list(DEFAULT_REDIRECTS),
    )
    server = server_factory(
        auth=AuthSettings(
            issuer_url=public_url,  # type: ignore[arg-type]
            resource_server_url=f"{public_url}{os.environ.get('DW_MCP_PATH', '/mcp')}",  # type: ignore[arg-type]
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
        auth_server_provider=provider,
    )

    @server.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request) -> Response:
        if request.method == "GET":
            return _page(request.query_params.get("id", ""))
        form = await request.form()
        login_id, code = str(form.get("id", "")), str(form.get("code", ""))
        target = provider.complete_login(login_id, code)
        if target is None:
            await asyncio.sleep(1)  # slows guessing; codes are 128-bit anyway
            return _page(
                login_id, "Code ungültig oder Anmeldung abgelaufen. / Invalid or expired."
            )
        return RedirectResponse(target, status_code=302)

    return server


def users_file_path() -> Path:
    default = Path(os.environ.get("DW_MCP_DATA_DIR", "/data")) / "users.json"
    return Path(os.environ.get("DW_MCP_USERS_FILE", default))


def user_command(args: List[str]) -> int:
    """``docuware-mcp user add NAME | remove NAME | list``."""
    path = users_file_path()
    users = load_users(path)
    if args[:1] == ["list"]:
        print("\n".join(sorted(users)) or "(no users)")
        return 0
    if len(args) == 2 and args[0] == "add":
        code = secrets.token_urlsafe(16)
        users[args[1]] = _hash(code)
        save_users(path, users)
        print(f"Access code for {args[1]} (shown once, send it privately):\n\n    {code}\n")
        return 0
    if len(args) == 2 and args[0] == "remove":
        if users.pop(args[1], None) is None:
            print(f"No such user: {args[1]}")
            return 1
        save_users(path, users)
        print(f"Removed {args[1]}; their sessions stop working immediately.")
        return 0
    print("usage: docuware-mcp user add NAME | user remove NAME | user list")
    return 2
