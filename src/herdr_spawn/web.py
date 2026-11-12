"""The web UI's listener: a small PWA and a JSON API over the same operations.

A second listener in the same process as the MCP endpoint, but its own Starlette
app: it serves the static page and `/api`, and has no `/mcp` route at all. Every
`/api` call goes through the `op_*` functions the MCP tools use, so the two
surfaces cannot drift apart.

The API starts coding agents, so by default it needs a bearer token, read from a
file at start (mode 600, at least 32 characters); without one the listener refuses
to start and the MCP endpoint carries on alone.

`HERDR_SPAWN_WEB_AUTH=none` turns the token off, for a listener whose network is
the access control -- a private VPN only its owner is on. That is a decision to
state out loud, never the result of a missing file. Either way every write needs
the `X-Herdr-Spawn` header, which a page on another site cannot send: without it,
any web page open in a browser that can reach this listener could start an agent.
"""

from __future__ import annotations

import hmac
import logging
import os
import socket
import stat
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .agents import HostAgents
from .config import Config

log = logging.getLogger("herdr-spawn.web")

STATIC_DIR = Path(__file__).parent / "web"
DEFAULT_TOKEN_FILE = "~/.config/herdr-spawn/web-token"
MIN_TOKEN_LEN = 32

# Shown on every response. The page renders agent screens, which are arbitrary
# text from a terminal: it uses textContent throughout, and this is the backstop.
_SECURITY_HEADERS = [
    (b"content-security-policy",
     b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
     b"connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; "
     b"form-action 'none'; frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]


class WebConfigError(Exception):
    pass


def load_token(path: Path) -> bytes:
    """The shared bearer token, or a refusal that says what is wrong with the file."""
    try:
        st = path.stat()
    except FileNotFoundError:
        raise WebConfigError(f"no token file at {path}") from None
    if not stat.S_ISREG(st.st_mode):
        raise WebConfigError(f"{path} is not a regular file")
    if st.st_mode & 0o077:
        raise WebConfigError(f"{path} must be readable by its owner only (chmod 600), is {oct(st.st_mode & 0o777)}")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < MIN_TOKEN_LEN or any(c.isspace() for c in token):
        raise WebConfigError(f"the token in {path} must be one word of at least {MIN_TOKEN_LEN} characters")
    return token.encode("utf-8")


def token_ok(header: str | None, token: bytes) -> bool:
    """Constant-time check of an `Authorization: Bearer <token>` header."""
    if not header:
        return False
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip().encode("utf-8"), token)


CSRF_HEADER = b"x-herdr-spawn"


def _is_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


class _Guard:
    """Bearer auth on /api, no caching of /api, security headers on everything."""

    def __init__(self, app: ASGIApp, token: bytes | None):
        self.app = app
        self.token = token  # None: auth is off, by explicit configuration

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        api = _is_api(scope["path"])

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [h for h in message.get("headers", []) if h[0].lower() != b"cache-control"] if api \
                    else list(message.get("headers", []))
                headers += _SECURITY_HEADERS
                if api:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        if not api and scope["method"] not in ("GET", "HEAD"):
            # Outside /api there are only static files. Anything else -- a POST to
            # /mcp included -- does not exist here.
            await JSONResponse({"status": "not_found"}, status_code=404)(scope, receive, send_with_headers)
            return
        req_headers = dict(scope.get("headers") or [])
        if api and self.token is not None:
            auth = req_headers.get(b"authorization")
            if not token_ok(auth.decode("latin-1") if auth else None, self.token):
                resp = JSONResponse({"status": "unauthorized"}, status_code=401,
                                    headers={"www-authenticate": "Bearer"})
                await resp(scope, receive, send_with_headers)
                return
        if api and scope["method"] not in ("GET", "HEAD") and CSRF_HEADER not in req_headers:
            # A cross-site form or fetch cannot add a custom header without a CORS
            # preflight, and this app answers none.
            resp = JSONResponse({"status": "forbidden", "reason": "missing X-Herdr-Spawn header"},
                                status_code=403)
            await resp(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)


def _http_status(result: dict[str, Any]) -> int:
    return {"refused": 422, "failed": 502}.get(str(result.get("status")), 200)


def _opt_str(body: dict[str, Any], key: str) -> str | None:
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"{key} must be a string")
    return v.strip() or None


async def _json_body(request: Request, allowed: set[str]) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        raise ValueError("body must be JSON") from None
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    unknown = set(body) - allowed
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    if "wait" in body and not isinstance(body["wait"], bool):
        raise ValueError("wait must be true or false")
    return body


def _bad(msg: str) -> JSONResponse:
    return JSONResponse({"status": "refused", "reason": msg}, status_code=400)


SPAWN_FIELDS = {"host", "prompt", "agent", "cwd", "label", "session_name", "workspace",
                "wait", "ui", "model", "effort"}


def build_web_app(conf: Config, caps: dict[str, HostAgents], token: bytes | None) -> ASGIApp:
    # Imported here so server.py can import this module without a cycle.
    from . import server as srv

    async def meta(request: Request) -> JSONResponse:
        return JSONResponse(srv.ui_meta(conf, caps))

    async def agents(request: Request) -> JSONResponse:
        res = await srv.op_list_agents(conf, host=request.query_params.get("host") or None)
        return JSONResponse(res, status_code=_http_status(res))

    async def folders(request: Request) -> JSONResponse:
        host = request.query_params.get("host")
        if not host:
            return _bad("host is required")
        res = await srv.op_list_folders(conf, host=host, path=request.query_params.get("path") or None)
        return JSONResponse(res, status_code=_http_status(res))

    async def spawn(request: Request) -> JSONResponse:
        try:
            body = await _json_body(request, SPAWN_FIELDS)
            kw = {k: _opt_str(body, k) for k in SPAWN_FIELDS - {"wait"}}
        except ValueError as exc:
            return _bad(str(exc))
        if not kw["host"] or not kw["prompt"]:
            return _bad("host and prompt are required")
        res = await srv.op_spawn(
            conf, caps, host=kw["host"], prompt=kw["prompt"], agent=kw["agent"] or srv.DEFAULT_AGENT,
            cwd=kw["cwd"], label=kw["label"], session_name=kw["session_name"],
            workspace=kw["workspace"], wait=bool(body.get("wait", False)), ui=kw["ui"],
            model=kw["model"], effort=kw["effort"],
        )
        return JSONResponse(res, status_code=_http_status(res))

    async def session(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        message, wait = None, False
        if request.method == "POST":
            try:
                body = await _json_body(request, {"message", "wait"})
                message = _opt_str(body, "message")
            except ValueError as exc:
                return _bad(str(exc))
            if not message:
                return _bad("message is required")
            wait = bool(body.get("wait", False))
        res = await srv.op_session(conf, session_name=name, message=message, wait=wait)
        return JSONResponse(res, status_code=_http_status(res))

    async def api_404(request: Request) -> JSONResponse:
        return JSONResponse({"status": "not_found"}, status_code=404)

    api = [
        Route("/meta", meta, methods=["GET"]),
        Route("/agents", agents, methods=["GET"]),
        Route("/folders", folders, methods=["GET"]),
        Route("/spawn", spawn, methods=["POST"]),
        Route("/sessions/{name}", session, methods=["GET", "POST"]),
        Route("/{rest:path}", api_404, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]),
    ]
    app = Starlette(routes=[
        Mount("/api", routes=api),
        Mount("/", StaticFiles(directory=STATIC_DIR, html=True)),
    ])
    return _Guard(app, token)


# --- the listener -------------------------------------------------------------


def web_settings() -> tuple[int, list[str], Path] | None:
    """Port, bind addresses and token file from the environment; None when off."""
    raw_port = os.environ.get("HERDR_SPAWN_WEB_PORT", "").strip()
    if not raw_port:
        return None
    try:
        port = int(raw_port)
    except ValueError:
        raise WebConfigError(f"HERDR_SPAWN_WEB_PORT is not a port: {raw_port!r}") from None
    binds = [b.strip() for b in os.environ.get("HERDR_SPAWN_WEB_BIND", "127.0.0.1").split(",") if b.strip()]
    for b in binds:
        # Same rule as the MCP listener: never every interface.
        if b in ("0.0.0.0", "::", "*"):
            raise WebConfigError(f"refusing to bind the web listener to {b}; name each address")
    token_file = Path(os.environ.get("HERDR_SPAWN_WEB_TOKEN_FILE") or DEFAULT_TOKEN_FILE).expanduser()
    return port, binds or ["127.0.0.1"], token_file


def bind_sockets(binds: list[str], port: int) -> list[socket.socket]:
    """Bind every address up front, so a taken port is a clean refusal at start."""
    socks: list[socket.socket] = []
    try:
        for addr in binds:
            fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
            s = socket.socket(fam, socket.SOCK_STREAM)
            socks.append(s)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((addr, port))
            s.listen(128)
            s.set_inheritable(True)
    except OSError:
        for s in socks:
            s.close()
        raise
    return socks


def prepare(conf: Config, caps: dict[str, HostAgents]) -> tuple[ASGIApp, list[socket.socket]] | None:
    """The web app and its bound sockets, or None (logged) when it must not start.

    A misconfigured web listener never takes the MCP endpoint down with it.
    """
    try:
        settings = web_settings()
        if settings is None:
            return None
        port, binds, token_file = settings
        auth = os.environ.get("HERDR_SPAWN_WEB_AUTH", "token").strip().lower()
        if auth not in ("token", "none"):
            raise WebConfigError(f"HERDR_SPAWN_WEB_AUTH must be 'token' or 'none', got {auth!r}")
        token = load_token(token_file) if auth == "token" else None
        socks = bind_sockets(binds, port)
    except (WebConfigError, OSError) as exc:
        log.error("web listener not started: %s", exc)
        return None
    log.info("serving the web UI on %s port %d (%s)", ", ".join(binds), port,
             "bearer token" if token else "NO AUTH: the network is the access control")
    return build_web_app(conf, caps, token), socks


async def serve(app: ASGIApp, socks: list[socket.socket]) -> None:
    import uvicorn

    config = uvicorn.Config(app, log_level="info", server_header=False, proxy_headers=False)
    try:
        await uvicorn.Server(config).serve(sockets=socks)
    except Exception:  # noqa: BLE001 - the MCP listener must outlive this one
        log.exception("web listener stopped")
