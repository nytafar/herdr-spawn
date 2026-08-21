from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from pathlib import Path

import pytest

from herdr_spawn import config as cfg
from herdr_spawn import rc as rcmod
from herdr_spawn.config import ConfigError, HostConfig
from herdr_spawn.herdr import Herdr, HerdrError
from herdr_spawn.server import coerce_session_name, cwd_is_trusted
from herdr_spawn.state import SpawnRecord, Store, TeardownGuard, assert_closable


def host(tmp_path: Path, **kw) -> HostConfig:
    base = dict(
        name="test",
        enabled=True,
        transport="socket",
        herdr_bin="/nonexistent/herdr",
        cwd_allowlist=(str(tmp_path / "allowed"),),
        protected_workspaces=("w9", "wK"),
    )
    base.update(kw)
    return HostConfig(**base)


# --- cwd allowlist ---------------------------------------------------------


def test_cwd_allows_root_and_children(tmp_path):
    root = tmp_path / "allowed"
    (root / "sub").mkdir(parents=True)
    h = host(tmp_path)
    assert h.resolve_cwd(str(root)) == root.resolve()
    assert h.resolve_cwd(str(root / "sub")) == (root / "sub").resolve()


def test_cwd_rejects_sibling_with_shared_prefix(tmp_path):
    """`/x/scratch-evil` must not pass an `/x/scratch` root.

    A string prefix check would let this through.
    """
    (tmp_path / "allowed").mkdir()
    evil = tmp_path / "allowed-evil"
    evil.mkdir()
    with pytest.raises(ConfigError, match="not under an allowed root"):
        host(tmp_path).resolve_cwd(str(evil))


def test_cwd_rejects_traversal(tmp_path):
    (tmp_path / "allowed").mkdir()
    with pytest.raises(ConfigError, match="not under an allowed root"):
        host(tmp_path).resolve_cwd(str(tmp_path / "allowed" / ".." / ".."))


def test_cwd_rejects_symlink_escape(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "escape"
    link.symlink_to(outside)
    with pytest.raises(ConfigError, match="not under an allowed root"):
        host(tmp_path).resolve_cwd(str(link))


def test_cwd_required_and_absolute(tmp_path):
    (tmp_path / "allowed").mkdir()
    h = host(tmp_path)
    with pytest.raises(ConfigError, match="cwd is required"):
        h.resolve_cwd("")
    with pytest.raises(ConfigError, match="absolute"):
        h.resolve_cwd("relative/path")


def test_empty_allowlist_refuses_everything(tmp_path):
    h = host(tmp_path, cwd_allowlist=())
    with pytest.raises(ConfigError, match="no cwd_allowlist"):
        h.resolve_cwd(str(tmp_path))


def test_default_cwd_used_when_omitted(tmp_path):
    """Omitting cwd lands in the host's default sandbox; None and '' both work."""
    root = tmp_path / "allowed"
    root.mkdir()
    h = host(tmp_path, default_cwd=str(root))
    assert h.resolve_cwd(None) == root.resolve()
    assert h.resolve_cwd("") == root.resolve()
    # An explicit cwd still overrides the default.
    (root / "sub").mkdir()
    assert h.resolve_cwd(str(root / "sub")) == (root / "sub").resolve()


def test_default_cwd_must_be_under_allowlist(tmp_path):
    """A misconfigured default outside the allowlist is still refused, not trusted."""
    (tmp_path / "allowed").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    h = host(tmp_path, default_cwd=str(outside))
    with pytest.raises(ConfigError, match="not under an allowed root"):
        h.resolve_cwd(None)


# --- session names ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,prompt,label",
    [
        (None, "Fix the login bug", None),
        (None, "x", None),
        (None, "!!!!", None),
        ("UPPER Case!!", "p", None),
        ("9leading", "p", None),
        ("-" * 60, "p", None),
        ("a" * 200, "p", None),
        ("", "p", "Some Label"),
    ],
)
def test_session_names_always_satisfy_herdr(raw, prompt, label):
    name = coerce_session_name(raw, prompt, label)
    assert re.match(cfg.SESSION_NAME_RE, name), name


# --- trust preflight -------------------------------------------------------


def test_trust_walks_parents(tmp_path):
    conf = tmp_path / ".claude.json"
    conf.write_text(json.dumps({"projects": {"/home/x": {"hasTrustDialogAccepted": True}}}))
    ok, via = cwd_is_trusted(conf, Path("/home/x/deep/nested"))
    assert ok and via == "/home/x"


def test_trust_denies_untrusted(tmp_path):
    conf = tmp_path / ".claude.json"
    conf.write_text(json.dumps({"projects": {"/home/x": {"hasTrustDialogAccepted": True}}}))
    ok, _ = cwd_is_trusted(conf, Path("/home/y"))
    assert not ok


def test_trust_denies_when_flag_false(tmp_path):
    conf = tmp_path / ".claude.json"
    conf.write_text(json.dumps({"projects": {"/home/x": {"hasTrustDialogAccepted": False}}}))
    ok, _ = cwd_is_trusted(conf, Path("/home/x"))
    assert not ok


def test_trust_handles_missing_file(tmp_path):
    ok, detail = cwd_is_trusted(tmp_path / "nope.json", Path("/home/x"))
    assert not ok and "could not read" in detail


# --- RC id validation ------------------------------------------------------


@pytest.mark.parametrize("good", ["session_01371E7E576DcZjd6hNQ4Y8H", "cse_abc-123_XYZ", "session_a"])
def test_rc_id_accepts(good):
    assert rcmod.RC_ID_RE.match(good)


@pytest.mark.parametrize("bad", ["", "sess_abc", "session_", "session_!!", "x" * 200, "session_" + "x" * 200])
def test_rc_id_rejects(bad):
    assert not rcmod.RC_ID_RE.match(bad)


def test_rc_id_accepts_underscores_and_dashes_in_body():
    """The narrower `[A-Za-z0-9]+` pattern matches every id seen so far and then
    fails later on one that happens to contain `_` or `-`."""
    assert rcmod.RC_ID_RE.match("session_01AB-cd_EF")


# --- herdr CLI wrapper -----------------------------------------------------


def _fake_herdr(tmp_path: Path, body: str) -> str:
    p = tmp_path / "fake-herdr"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_rc1_structured_error_is_parsed(tmp_path):
    binp = _fake_herdr(tmp_path, 'echo \'{"error":{"code":"agent_name_taken","message":"nope"}}\' >&2\nexit 1\n')
    h = Herdr(host(tmp_path, herdr_bin=binp))
    with pytest.raises(HerdrError) as e:
        asyncio.run(h.run(["agent", "start"]))
    assert e.value.code == "agent_name_taken"
    assert e.value.message == "nope"


def test_rc2_plaintext_stderr_does_not_crash(tmp_path):
    """rc=2 prints bare text, not JSON. A wrapper that always json.loads(stderr)
    raises JSONDecodeError here instead of reporting the real problem."""
    binp = _fake_herdr(tmp_path, 'echo "unsupported interactive agent kind: claudecode" >&2\nexit 2\n')
    h = Herdr(host(tmp_path, herdr_bin=binp))
    with pytest.raises(HerdrError) as e:
        asyncio.run(h.run(["agent", "start"]))
    assert e.value.returncode == 2
    assert "unsupported interactive agent kind" in e.value.message


def test_missing_binary_reports_unavailable(tmp_path):
    h = Herdr(host(tmp_path, herdr_bin=str(tmp_path / "absent")))
    with pytest.raises(HerdrError) as e:
        asyncio.run(h.run(["workspace", "list"]))
    assert e.value.code == "HERDR_UNAVAILABLE"


def test_rc0_non_json_is_reported_clearly(tmp_path):
    binp = _fake_herdr(tmp_path, 'echo "not json"\nexit 0\n')
    h = Herdr(host(tmp_path, herdr_bin=binp))
    with pytest.raises(HerdrError) as e:
        asyncio.run(h.run(["workspace", "list"]))
    assert e.value.code == "unparseable_output"


def test_agent_prompt_never_passes_until(tmp_path):
    """`--until` REPLACES the default idle|done|blocked set rather than adding to
    it, dropping `done` -- the state a backgrounded spawn actually reaches."""
    binp = _fake_herdr(tmp_path, 'echo "{\\"result\\":{\\"argv\\":[\\"$@\\"]}}"\nexit 0\n')
    h = Herdr(host(tmp_path, herdr_bin=binp))
    captured = {}

    async def spy(args, timeout=None):
        captured["args"] = args
        return {"result": {}}

    h.run = spy  # type: ignore[method-assign]
    asyncio.run(h.agent_prompt("n", "text", wait=True))
    assert "--until" not in captured["args"]
    assert "--wait" in captured["args"]


def test_herdr_env_sets_home_and_strips_pane_identity(tmp_path):
    os.environ["HERDR_PANE_ID"] = "w1:p1"
    env = cfg.herdr_env(host(tmp_path, home="/home/someone", socket_path="/tmp/s.sock"))
    assert env["HOME"] == "/home/someone"
    assert env["HERDR_SOCKET_PATH"] == "/tmp/s.sock"
    assert "HERDR_PANE_ID" not in env
    os.environ.pop("HERDR_PANE_ID", None)


# --- state + teardown guard ------------------------------------------------


def _rec(**kw) -> SpawnRecord:
    base = dict(
        session_name="s1", host="host-a", workspace_id="w0",
        tab_id="w0:t3", pane_id="w0:p3", cwd="/tmp", label="l",
    )
    base.update(kw)
    return SpawnRecord(**base)


def test_guard_allows_own_tab(tmp_path):
    s = Store(tmp_path)
    s.append(_rec())
    assert_closable(store=s, host="host-a", tab_id="w0:t3", workspace_id="w0", protected_workspaces=("w9",))


def test_guard_refuses_unowned_tab(tmp_path):
    s = Store(tmp_path)
    with pytest.raises(TeardownGuard, match="no record of creating it"):
        assert_closable(store=s, host="host-a", tab_id="w0:t9", workspace_id="w0", protected_workspaces=("w9",))


def test_guard_refuses_protected_workspace_even_if_recorded(tmp_path):
    s = Store(tmp_path)
    s.append(_rec(workspace_id="w9", tab_id="w9:t1"))
    with pytest.raises(TeardownGuard, match="protected list"):
        assert_closable(store=s, host="host-a", tab_id="w9:t1", workspace_id="w9", protected_workspaces=("w9",))


def test_guard_is_host_scoped(tmp_path):
    """A tab id recorded on one host must not authorise closing it on another."""
    s = Store(tmp_path)
    s.append(_rec(host="host-b"))
    with pytest.raises(TeardownGuard):
        assert_closable(store=s, host="host-a", tab_id="w0:t3", workspace_id="w0", protected_workspaces=())


def test_active_names_excludes_closed(tmp_path):
    s = Store(tmp_path)
    s.append(_rec(session_name="a"))
    s.append(_rec(session_name="b", tab_id="w0:t4"))
    s.mark_closed("host-a", "w0:t4")
    assert s.active_names("host-a") == {"a"}


def test_store_survives_corrupt_lines(tmp_path):
    s = Store(tmp_path)
    s.append(_rec())
    s.path.write_text(s.path.read_text() + "{not json\n")
    assert len(s.all()) == 1


# --- rc resolution ---------------------------------------------------------


def test_rc_absent_bridge_is_not_a_failure(tmp_path):
    """Remote Control not connecting is a normal outcome: the pane is still a
    working local session, so the spawn succeeded without a phone handle."""
    (tmp_path / "1234.json").write_text(json.dumps({"sessionId": "u", "pid": 1234, "version": "2.1.220"}))
    out = asyncio.run(rcmod.resolve(sessions_dir=tmp_path, pid=1234, ceiling_s=0.01))
    assert out.available is False
    assert out.bridge_session_id is None
    assert out.version == "2.1.220"
    assert "did not connect" in (out.reason or "") or "no session record" in (out.reason or "")


def test_rc_rejects_mismatched_session_id(tmp_path):
    (tmp_path / "1234.json").write_text(
        json.dumps({"sessionId": "other", "bridgeSessionId": "session_abc", "pid": 1234})
    )
    out = asyncio.run(
        rcmod.resolve(sessions_dir=tmp_path, pid=1234, expected_session_id="mine", ceiling_s=0.01)
    )
    assert out.available is False
    assert "expected mine" in (out.reason or "")


def test_rc_rejects_invalid_bridge_id(tmp_path):
    (tmp_path / "1234.json").write_text(json.dumps({"sessionId": "u", "bridgeSessionId": "bogus!!", "pid": 1234}))
    out = asyncio.run(rcmod.resolve(sessions_dir=tmp_path, pid=1234, ceiling_s=0.01))
    assert out.available is False
    assert "failed validation" in (out.reason or "")


def test_rc_happy_path_builds_url(tmp_path):
    (tmp_path / "1234.json").write_text(
        json.dumps({"sessionId": "u", "bridgeSessionId": "session_abc123", "pid": 1234, "version": "2.1.220"})
    )
    out = asyncio.run(rcmod.resolve(sessions_dir=tmp_path, pid=1234, ceiling_s=0.01))
    assert out.available is True
    assert out.url == "https://claude.ai/code/session_abc123"
