"""Folder browsing rules, the web listener's token check, and its route surface."""

from __future__ import annotations

import asyncio
import json
import shlex
import stat
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from herdr_spawn import config as cfg
from herdr_spawn import folders
from herdr_spawn import web
from herdr_spawn.config import ConfigError, HostConfig
from herdr_spawn.server import op_list_folders

TOKEN = "t" * 40


def host(tmp_path: Path, **kw) -> HostConfig:
    base = dict(
        name="test",
        enabled=True,
        transport="socket",
        herdr_bin="/nonexistent/herdr",
        hc_bin="/nonexistent/hc",
        cwd_allowlist=(str(tmp_path / "allowed"),),
    )
    base.update(kw)
    return HostConfig(**base)


def ls(h: HostConfig, path: str | None = None) -> dict:
    return asyncio.run(folders.list_folders(h, path))


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "allowed"
    for d in ("a/deep", "b", ".hidden", "secret/inner", "reg/clients/acme"):
        (root / d).mkdir(parents=True)
    (root / "file.txt").write_text("x")
    (tmp_path / "outside").mkdir()
    (tmp_path / "allowed-evil").mkdir()
    return root


# --- folder rules ------------------------------------------------------------------


def test_top_level_is_roots_and_vaults(tmp_path, tree):
    h = host(tmp_path, vaults={"notes": str(tree / "a")})
    res = ls(h)
    assert res["path"] is None
    assert [(f["kind"], f["path"]) for f in res["folders"]] == [
        ("root", str(tree)), ("vault", str(tree / "a"))]


def test_lists_directories_only_without_dot_folders(tmp_path, tree):
    res = ls(host(tmp_path), str(tree))
    assert [f["name"] for f in res["folders"]] == ["a", "b", "reg", "secret"]
    assert res["parent"] is None  # the root's parent is outside the allowlist


def test_refuses_paths_outside_the_allowlist(tmp_path, tree):
    h = host(tmp_path)
    for bad in (str(tmp_path / "outside"), str(tmp_path / "allowed-evil"), "/"):
        with pytest.raises(ConfigError, match="not under an allowed root"):
            ls(h, bad)


def test_refuses_traversal(tmp_path, tree):
    with pytest.raises(ConfigError, match="not under an allowed root"):
        ls(host(tmp_path), str(tree / "a" / ".." / ".." / "outside"))


def test_refuses_and_hides_a_symlink_escape(tmp_path, tree):
    (tree / "escape").symlink_to(tmp_path / "outside")
    h = host(tmp_path)
    assert "escape" not in [f["name"] for f in ls(h, str(tree))["folders"]]
    with pytest.raises(ConfigError, match="not under an allowed root"):
        ls(h, str(tree / "escape"))


def test_refuses_dot_folders(tmp_path, tree):
    with pytest.raises(ConfigError, match="dot-folder"):
        ls(host(tmp_path), str(tree / ".hidden"))


def test_deny_list_hides_and_refuses(tmp_path, tree):
    h = host(tmp_path, list_deny=(str(tree / "reg" / "clients"), str(tree / "secret")))
    assert [f["name"] for f in ls(h, str(tree))["folders"]] == ["a", "b", "reg"]
    assert ls(h, str(tree / "reg"))["folders"] == []
    for bad in (tree / "reg" / "clients", tree / "reg" / "clients" / "acme", tree / "secret" / "inner",
                tree / "reg" / "clients" / ".." / "clients"):
        with pytest.raises(ConfigError, match="not listed"):
            ls(h, str(bad))


def test_deny_list_cannot_be_reached_through_a_symlink(tmp_path, tree):
    (tree / "b" / "shortcut").symlink_to(tree / "reg" / "clients")
    h = host(tmp_path, list_deny=(str(tree / "reg" / "clients"),))
    assert ls(h, str(tree / "b"))["folders"] == []
    with pytest.raises(ConfigError, match="not listed"):
        ls(h, str(tree / "b" / "shortcut"))


def test_vault_relative_path_lists_and_cannot_leave(tmp_path, tree):
    h = host(tmp_path, vaults={"v": str(tree)})
    assert [f["name"] for f in ls(h, "v/a")["folders"]] == ["deep"]
    with pytest.raises(ConfigError, match="not under an allowed root"):
        ls(h, "v/../outside")


def test_remote_listing_is_checked_again_on_this_side(tmp_path):
    """The far side says where a path goes; this side decides if that is allowed."""
    log = tmp_path / "hc.log"
    out = "/srv/ok\nsite\t/srv/ok/site\nlink\t/etc\n.git\t/srv/ok/.git\n"
    hc = tmp_path / "hc"
    hc.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {shlex.quote(str(log))}\nprintf {shlex.quote(out)}\n')
    hc.chmod(hc.stat().st_mode | stat.S_IEXEC)
    h = host(tmp_path, transport="hc", hc_machine="far", hc_bin=str(hc), cwd_allowlist=("/srv/ok",))
    res = ls(h, "/srv/ok")
    assert [f["name"] for f in res["folders"]] == ["site"]
    assert "--on" in log.read_text() and "far" in log.read_text()


def test_remote_resolution_outside_the_allowlist_is_refused(tmp_path):
    hc = tmp_path / "hc"
    hc.write_text("#!/bin/sh\nprintf '/etc\\n'\n")
    hc.chmod(hc.stat().st_mode | stat.S_IEXEC)
    h = host(tmp_path, transport="hc", hc_machine="far", hc_bin=str(hc), cwd_allowlist=("/srv/ok",))
    with pytest.raises(ConfigError, match="not under an allowed root"):
        ls(h, "/srv/ok/link")


def test_op_folds_refusals_into_the_result(tmp_path, tree):
    conf = cfg.Config(hosts={"test": host(tmp_path)}, state_dir=tmp_path / "state")
    res = asyncio.run(op_list_folders(conf, host="test", path=str(tmp_path / "outside")))
    assert res["status"] == "refused"


def test_list_deny_is_read_from_config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[hosts.x]\nenabled = true\ncwd_allowlist = ["/a"]\nlist_deny = ["/a/b"]\n')
    assert cfg.load(p).hosts["x"].list_deny == ("/a/b",)


# --- token -------------------------------------------------------------------------


def _token_file(tmp_path: Path, text: str = TOKEN, mode: int = 0o600) -> Path:
    p = tmp_path / "web-token"
    p.write_text(text + "\n")
    p.chmod(mode)
    return p


def test_token_file_rules(tmp_path):
    with pytest.raises(web.WebConfigError, match="no token file"):
        web.load_token(tmp_path / "missing")
    with pytest.raises(web.WebConfigError, match="chmod 600"):
        web.load_token(_token_file(tmp_path, mode=0o644))
    with pytest.raises(web.WebConfigError, match="at least"):
        web.load_token(_token_file(tmp_path, text="short"))
    assert web.load_token(_token_file(tmp_path)) == TOKEN.encode()


def test_token_compare():
    tok = TOKEN.encode()
    assert web.token_ok(f"Bearer {TOKEN}", tok)
    assert web.token_ok(f"bearer {TOKEN}", tok)
    for bad in (None, "", TOKEN, f"Basic {TOKEN}", f"Bearer {TOKEN}x", f"Bearer {TOKEN[:-1]}", "Bearer "):
        assert not web.token_ok(bad, tok)


def test_no_token_file_means_no_listener(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_SPAWN_WEB_PORT", "0")
    monkeypatch.setenv("HERDR_SPAWN_WEB_TOKEN_FILE", str(tmp_path / "missing"))
    conf = cfg.Config(hosts={"test": host(tmp_path)}, state_dir=tmp_path / "state")
    assert web.prepare(conf, {}) is None


def test_no_port_means_no_listener(monkeypatch):
    monkeypatch.delenv("HERDR_SPAWN_WEB_PORT", raising=False)
    assert web.web_settings() is None


def test_wildcard_bind_is_refused(monkeypatch):
    monkeypatch.setenv("HERDR_SPAWN_WEB_PORT", "8232")
    monkeypatch.setenv("HERDR_SPAWN_WEB_BIND", "127.0.0.1,0.0.0.0")
    with pytest.raises(web.WebConfigError, match="0.0.0.0"):
        web.web_settings()


# --- the web app -------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, tree) -> TestClient:
    h = host(tmp_path, list_deny=(str(tree / "secret"),))
    conf = cfg.Config(hosts={"test": h}, state_dir=tmp_path / "state")
    return TestClient(web.build_web_app(conf, {}, TOKEN.encode()))


AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Herdr-Spawn": "1"}


def test_every_api_route_needs_the_token(client):
    calls = [("get", "/api/meta"), ("get", "/api/agents"), ("get", "/api/folders?host=test"),
             ("post", "/api/spawn"), ("get", "/api/sessions/x"), ("post", "/api/sessions/x"),
             ("get", "/api/nope"), ("get", "/api")]
    for method, path in calls:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
            r = getattr(client, method)(path, headers=headers)
            assert r.status_code == 401, (method, path, headers)
            assert r.headers["cache-control"] == "no-store"


def test_spawn_without_token_never_reaches_spawn(client, monkeypatch):
    from herdr_spawn import server
    called = []
    monkeypatch.setattr(server, "spawn", lambda *a, **k: called.append(1))
    r = client.post("/api/spawn", json={"host": "test", "prompt": "x"})
    assert r.status_code == 401 and not called


def test_mcp_is_not_served(client):
    for path in ("/mcp", "/mcp/", "/api/mcp"):
        assert client.post(path, headers=AUTH, json={}).status_code == 404
        assert client.get(path, headers=AUTH).status_code == 404


def test_page_is_public_and_api_answers_with_the_token(client, tree):
    page = client.get("/")
    assert page.status_code == 200 and "herdr-spawn" in page.text
    assert "content-security-policy" in page.headers
    assert client.get("/sw.js").status_code == 200
    assert client.get("/manifest.webmanifest").status_code == 200

    meta = client.get("/api/meta", headers=AUTH).json()
    assert meta["hosts"][0]["name"] == "test" and meta["default_agent"] == "claude"

    r = client.get("/api/folders", params={"host": "test", "path": str(tree)}, headers=AUTH)
    assert r.status_code == 200 and "secret" not in [f["name"] for f in r.json()["folders"]]
    r = client.get("/api/folders", params={"host": "test", "path": str(tree / "secret")}, headers=AUTH)
    assert r.status_code == 422 and r.json()["status"] == "refused"


def test_spawn_body_is_validated(client):
    assert client.post("/api/spawn", headers=AUTH, json={"host": "test"}).status_code == 400
    assert client.post("/api/spawn", headers=AUTH, json={"host": "test", "prompt": "x", "evil": 1}).status_code == 400
    assert client.post("/api/spawn", headers=AUTH, json=["x"]).status_code == 400
    r = client.post("/api/spawn", headers=AUTH, json={"host": "test", "prompt": "x", "cwd": "/etc"})
    assert r.status_code == 422 and "not under an allowed root" in json.loads(r.text)["reason"]


# --- auth off, and the cross-site guard -------------------------------------------


def test_auth_none_serves_without_a_token_but_must_be_asked_for(tmp_path, monkeypatch):
    conf = cfg.Config(hosts={"test": host(tmp_path)}, state_dir=tmp_path / "state")
    open_client = TestClient(web.build_web_app(conf, {}, None))
    assert open_client.get("/api/meta").status_code == 200

    # A missing token file alone never turns auth off.
    monkeypatch.setenv("HERDR_SPAWN_WEB_PORT", "0")
    monkeypatch.setenv("HERDR_SPAWN_WEB_TOKEN_FILE", str(tmp_path / "missing"))
    assert web.prepare(conf, {}) is None
    monkeypatch.setenv("HERDR_SPAWN_WEB_AUTH", "nope")
    assert web.prepare(conf, {}) is None
    monkeypatch.setenv("HERDR_SPAWN_WEB_AUTH", "none")
    prepared = web.prepare(conf, {})
    assert prepared is not None
    for s in prepared[1]:
        s.close()


def test_writes_need_the_custom_header_even_with_auth_off(tmp_path, monkeypatch):
    """Without it, any page open in a browser on the same network could spawn."""
    from herdr_spawn import server as srv

    called = []

    async def fake_spawn(*a, **kw):
        called.append(kw)
        return {"status": "spawned"}

    monkeypatch.setattr(srv, "spawn", fake_spawn)
    conf = cfg.Config(hosts={"test": host(tmp_path)}, state_dir=tmp_path / "state")
    c = TestClient(web.build_web_app(conf, {}, None))
    body = {"host": "test", "prompt": "x"}
    assert c.post("/api/spawn", json=body).status_code == 403
    assert not called
    assert c.post("/api/spawn", json=body, headers={"X-Herdr-Spawn": "1"}).status_code == 200
    assert called
