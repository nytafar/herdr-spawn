from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import stat
from pathlib import Path

import pytest

from herdr_spawn import agents
from herdr_spawn import config as cfg
from herdr_spawn import rc as rcmod
from herdr_spawn.config import ConfigError, HostConfig
from herdr_spawn.herdr import Herdr, HerdrError
from herdr_spawn.server import (
    _agent_args,
    claude_pid,
    coerce_session_name,
    cwd_is_trusted,
    trust_from_config,
)
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


# --- ssh transport ---------------------------------------------------------


def ssh_host(tmp_path: Path, **kw) -> HostConfig:
    return host(
        tmp_path,
        transport="ssh",
        ssh_target="me@remote",
        herdr_bin="/opt/homebrew/bin/herdr",
        home="/Users/me",
        **kw,
    )


def test_ssh_argv_is_one_quoted_remote_command(tmp_path):
    """ssh joins its trailing args into a single string for the REMOTE shell, so
    the remote side has to be quoted here or a spaced argument splits in two."""
    argv = Herdr(ssh_host(tmp_path))._argv(["agent", "prompt", "n", "fix the login bug"])
    assert argv[0] == "ssh"
    assert argv[-2] == "me@remote"
    assert argv[-1] == "/opt/homebrew/bin/herdr agent prompt n 'fix the login bug'"


def test_ssh_argv_survives_hostile_prompt_text(tmp_path):
    """A prompt is arbitrary prose. Quotes, newlines, $ and backticks must reach
    the remote herdr as one argument and must not reach the remote SHELL at all."""
    nasty = "don't `rm -rf /` $HOME \"x\"\nsecond line; echo pwned"
    argv = Herdr(ssh_host(tmp_path))._argv(["agent", "prompt", "n", nasty])
    assert shlex.split(argv[-1])[-1] == nasty


def test_ssh_argv_carries_identity_and_socket(tmp_path):
    h = ssh_host(tmp_path, ssh_identity="/k/id", ssh_socket_path="/Users/me/.config/herdr/herdr.sock")
    argv = Herdr(h)._argv(["workspace", "list"])
    assert "-i" in argv and "/k/id" in argv
    assert argv[-1].startswith("env HERDR_SOCKET_PATH=/Users/me/.config/herdr/herdr.sock ")


def test_ssh_without_target_is_unavailable_not_a_crash(tmp_path):
    h = host(tmp_path, transport="ssh", ssh_target=None)
    with pytest.raises(HerdrError) as e:
        Herdr(h)._argv(["workspace", "list"])
    assert e.value.code == "HERDR_UNAVAILABLE"


def test_local_argv_is_unquoted_and_direct(tmp_path):
    """The local path must stay execve-style: no shell, no quoting layer."""
    argv = Herdr(host(tmp_path, herdr_bin="/bin/herdr"))._argv(["agent", "prompt", "n", "a b"])
    assert argv == ["/bin/herdr", "agent", "prompt", "n", "a b"]


def test_ssh_exit_255_reports_the_connection_not_herdr(tmp_path):
    """ssh gives up with 255. Reporting that as a herdr failure sends you
    debugging a herdr server that was never contacted."""
    binp = _fake_herdr(tmp_path, 'echo "Host key verification failed." >&2\nexit 255\n')
    h = Herdr(ssh_host(tmp_path))
    h._ssh_prefix = lambda: [binp]  # type: ignore[method-assign]
    with pytest.raises(HerdrError) as e:
        asyncio.run(h.run(["workspace", "list"]))
    assert e.value.code == "HERDR_UNAVAILABLE"
    assert "ssh to me@remote failed" in e.value.message


def test_ssh_exit_127_names_the_missing_binary(tmp_path):
    binp = _fake_herdr(tmp_path, 'echo "herdr: command not found" >&2\nexit 127\n')
    h = Herdr(ssh_host(tmp_path))
    h._ssh_prefix = lambda: [binp]  # type: ignore[method-assign]
    with pytest.raises(HerdrError) as e:
        asyncio.run(h.run(["workspace", "list"]))
    assert e.value.code == "HERDR_UNAVAILABLE"
    assert "/opt/homebrew/bin/herdr not found" in e.value.message


# --- remote RC resolution --------------------------------------------------


def _remote_out(procstart: str, record: dict | None) -> str:
    body = json.dumps(record) if record is not None else ""
    return f"---PROCSTART---\n{procstart}\n---RECORD---\n{body}\n"


def test_remote_rc_happy_path(tmp_path):
    rec = {"sessionId": "u", "bridgeSessionId": "session_abc", "pid": 9,
           "procStart": "Mon Aug 10 12:28:51 2026", "version": "2.1.226"}

    async def sh(script, timeout=None):
        return _remote_out("Mon Aug 10 12:28:51 2026", rec)

    out = asyncio.run(rcmod.resolve_remote(sh=sh, sessions_dir=tmp_path, pid=9, expected_session_id="u"))
    assert out.available is True
    assert out.url == "https://claude.ai/code/session_abc"
    assert out.version == "2.1.226"


def test_remote_rc_catches_a_recycled_pid(tmp_path):
    """macOS procStart is a ctime string, not clock ticks -- the guard has to work
    on both, which is why the remote probe picks its format per platform."""
    rec = {"sessionId": "u", "bridgeSessionId": "session_abc", "pid": 9,
           "procStart": "Mon Aug 10 12:28:51 2026"}

    async def sh(script, timeout=None):
        return _remote_out("Tue Aug 11 09:00:00 2026", rec)

    out = asyncio.run(rcmod.resolve_remote(sh=sh, sessions_dir=tmp_path, pid=9))
    assert out.available is False
    assert "pid reused" in (out.reason or "")


def test_remote_rc_missing_record_is_not_a_failure(tmp_path):
    async def sh(script, timeout=None):
        return _remote_out("", None)

    out = asyncio.run(rcmod.resolve_remote(sh=sh, sessions_dir=tmp_path, pid=9, ceiling_s=0.01))
    assert out.available is False
    assert "no session record" in (out.reason or "")


def test_remote_rc_survives_an_unreachable_host(tmp_path):
    """Losing the ssh connection while reading the registry costs the RC link, not
    the spawn: the pane is already running."""

    async def sh(script, timeout=None):
        raise HerdrError(code="HERDR_UNAVAILABLE", message="ssh died", argv=[], returncode=255)

    out = asyncio.run(rcmod.resolve_remote(sh=sh, sessions_dir=tmp_path, pid=9))
    assert out.available is False
    assert "could not read the remote session registry" in (out.reason or "")


def test_remote_poll_script_quotes_its_inputs():
    """The script is composed by us, but a sessions_dir with a space still has to
    survive -- /Users/<name>/Library paths are one rename away from having one."""
    script = rcmod._REMOTE_POLL.format(
        sessions_dir=shlex.quote("/Users/a b/.claude/sessions"), pid="9", iterations=5
    )
    assert "D='/Users/a b/.claude/sessions'" in script
    assert "TZ=UTC ps" in script  # the record is UTC; ps prints local time


# --- which process is claude -----------------------------------------------


def test_claude_pid_ignores_children_that_sort_first():
    """Observed on a real pane: claude came back LAST of seven, behind caffeinate
    and one node per MCP server. `foreground_processes[0]` picks caffeinate, whose
    pid has no session record -- so RC reports unavailable on a working spawn."""
    info = {
        "foreground_process_group_id": 35927,
        "foreground_processes": [
            {"pid": 40013, "argv0": "caffeinate", "name": "caffeinate"},
            {"pid": 36459, "argv0": "node", "name": "node"},
            {"pid": 35927, "argv0": "claude", "name": "2.1.238"},
        ],
    }
    assert claude_pid(info) == 35927


def test_claude_pid_falls_back_to_argv0_then_first():
    no_leader = {
        "foreground_processes": [
            {"pid": 1, "argv0": "caffeinate"},
            {"pid": 2, "argv0": "/Users/me/.local/bin/claude"},
        ]
    }
    assert claude_pid(no_leader) == 2
    assert claude_pid({"foreground_processes": [{"pid": 7, "argv0": "sh"}]}) == 7
    assert claude_pid({}) is None


def test_remote_poll_tolerates_a_missing_record():
    """No record yet is the ordinary 'RC never connected' outcome. If cat's rc=1
    escapes, that becomes a remote-shell error and buries the real reason."""
    assert "|| true" in rcmod._REMOTE_POLL.split("---RECORD---")[1]


# --- remote preflight ------------------------------------------------------


def test_remote_preflight_distinguishes_missing_cwd_from_untrusted(tmp_path):
    from herdr_spawn.server import _preflight_remote

    h = Herdr(ssh_host(tmp_path))
    claude = agents.profile("claude")

    async def missing(script, timeout=None):
        return "__NO_CWD__\n"

    h.sh = missing  # type: ignore[method-assign]
    with pytest.raises(ConfigError, match="does not exist"):
        asyncio.run(_preflight_remote(h.host, h, Path("/Users/me/scratch"), claude))

    async def untrusted(script, timeout=None):
        return json.dumps({"projects": {"/Users/me/other": {"hasTrustDialogAccepted": True}}})

    h.sh = untrusted  # type: ignore[method-assign]
    with pytest.raises(ConfigError, match="not trusted"):
        asyncio.run(_preflight_remote(h.host, h, Path("/Users/me/scratch"), claude))

    async def trusted(script, timeout=None):
        return json.dumps({"projects": {"/Users/me": {"hasTrustDialogAccepted": True}}})

    h.sh = trusted  # type: ignore[method-assign]
    asyncio.run(_preflight_remote(h.host, h, Path("/Users/me/scratch"), claude))  # parent trust covers it


def test_remote_preflight_skips_trust_for_an_agent_with_no_store(tmp_path):
    """An agent we have no profile for is still startable -- we just cannot
    pre-empt its trust prompt, so the cwd check runs and nothing else."""
    from herdr_spawn.server import _preflight_remote

    h = Herdr(ssh_host(tmp_path))
    seen = {}

    async def sh(script, timeout=None):
        seen["script"] = script
        return ""

    h.sh = sh  # type: ignore[method-assign]
    asyncio.run(_preflight_remote(h.host, h, Path("/Users/me/scratch"), agents.profile("opencode")))
    assert "__NO_CWD__" in seen["script"]  # cwd still checked
    assert "test -n ''" in seen["script"]  # ...and the trust read short-circuits


# --- agent profiles --------------------------------------------------------


def test_only_claude_gets_remote_control_args(tmp_path):
    """--remote-control is Claude's flag. Passing it to codex is an instant crash,
    and codex has no bridge to advertise anyway."""
    h = host(tmp_path)
    assert _agent_args(h, agents.profile("claude"), "s1") == ["--remote-control", "s1"]
    assert _agent_args(h, agents.profile("codex"), "s1") == []
    assert _agent_args(h, agents.profile("opencode"), "s1") == []


def test_skip_permissions_uses_each_agents_own_flag(tmp_path):
    """These are not interchangeable, and an agent whose flag we do not know gets
    NOTHING rather than someone else's dangerous flag."""
    h = host(tmp_path, skip_permissions=True)
    assert "--dangerously-skip-permissions" in _agent_args(h, agents.profile("claude"), "s")
    assert "--dangerously-bypass-approvals-and-sandbox" in _agent_args(h, agents.profile("codex"), "s")
    assert _agent_args(h, agents.profile("opencode"), "s") == []


def test_codex_trust_is_toml_and_walks_parents():
    toml = '[projects."/Users/me"]\ntrust_level = "trusted"\n'
    ok, via = trust_from_config(toml, Path("/Users/me/scratch/x"), source="cfg", scheme="codex")
    assert ok and via == "/Users/me"
    untrusted = '[projects."/Users/me"]\ntrust_level = "untrusted"\n'
    ok, _ = trust_from_config(untrusted, Path("/Users/me/scratch"), source="cfg", scheme="codex")
    assert not ok


# --- discovery and the advertised enum -------------------------------------


def test_kind_enum_is_parsed_from_herdr_help():
    """Hardcoding this list goes stale: 0.8.2 added a kind 0.8.0 did not have."""
    helptext = (
        "      --kind <KIND>\n          Supported agent kind and canonical executable\n"
        "          \n          [possible values: pi, claude, codex, opencode, maki]\n"
    )
    m = agents.KIND_ENUM_RE.search(helptext)
    assert [k.strip() for k in m.group(1).split(",")] == ["pi", "claude", "codex", "opencode", "maki"]


def test_sweep_ends_with_exit_zero(tmp_path):
    """A `for` loop's status is its LAST command, so a sweep whose final kind is
    absent exits 127 and the whole probe reads as a broken shell."""
    async def run_text(args, timeout=None):
        return "[possible values: claude, codex, maki]"

    async def sh(script, timeout=None):
        assert script.rstrip().endswith("exit 0")
        return "claude\n"

    h = Herdr(host(tmp_path))
    h.run_text, h.sh = run_text, sh  # type: ignore[method-assign]
    caps = asyncio.run(agents.discover(h))
    assert caps.available == ("claude",)
    assert caps.kinds == ("claude", "codex", "maki")


def test_undiscovered_host_permits_everything():
    """A laptop that was closed at startup must not have its agents hidden until
    the next restart -- let the spawn through and return the real error."""
    unknown = agents.HostAgents(error="ssh down")
    assert unknown.permits("codex") and unknown.permits("anything")
    known = agents.HostAgents(kinds=("claude", "codex"), installed=("claude",), available=("claude",))
    assert known.permits("claude") and not known.permits("codex")


def test_refusal_names_what_is_actually_available():
    """The refusal carries the answer, which is why the per-host matrix does not
    need to be in the schema."""
    caps = agents.HostAgents(kinds=("claude", "codex", "pi"), installed=("claude", "pi"),
                             available=("claude", "pi"))
    assert "Available there: claude, pi" in caps.refusal("codex", "mac")
    # A kind this herdr cannot drive at all is a different problem, said differently.
    assert "cannot start 'dsh'" in caps.refusal("dsh", "mac")


def test_advertised_enum_is_the_union_of_enabled_hosts_only():
    caps = {
        "xl": agents.HostAgents(kinds=("claude", "codex"), available=("claude", "codex")),
        "mac": agents.HostAgents(kinds=("claude", "pi"), available=("claude", "pi")),
        "off": agents.HostAgents(kinds=("droid",), available=("droid",)),
    }
    assert agents.advertised(caps, ["xl", "mac"]) == ["claude", "codex", "pi"]
    # claude survives even when nothing was discovered: it is the default.
    assert agents.advertised({}, ["xl"]) == ["claude"]


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


# --- hc: the default landing place -------------------------------------------

from herdr_spawn import spaces  # noqa: E402
from herdr_spawn.hc import Hc  # noqa: E402
from herdr_spawn.server import (  # noqa: E402
    _hc_agent_pid,
    _hc_first_turn,
    _tidy_screen,
    follow_session,
    spawn,
)

HOME = "/home/me"
LAYOUT = [
    {"id": "sp-home", "name": "sysadmin", "folder": HOME},
    {"id": "sp-notes", "name": "notes", "folder": f"{HOME}/notes"},
    {"id": "sp-notes2", "name": "", "folder": f"{HOME}/notes"},
    {"id": "sp-code", "name": "herdr", "folder": f"{HOME}/code"},
    {"id": "sp-repo", "name": "livesync", "folder": f"{HOME}/code/livesync"},
    {"id": "sp-named", "name": "wiki", "folder": "/elsewhere"},
]


def test_top_folder_is_first_component_below_a_root():
    assert spaces.top_folder(f"{HOME}/code/x/y", (HOME,)) == f"{HOME}/code"
    assert spaces.top_folder("/var/www/site.no/htdocs/wp", (HOME, "/var/www")) == "/var/www/site.no"
    # Outside every root, and a root itself, are their own top folder.
    assert spaces.top_folder("/srv/thing", (HOME,)) == "/srv/thing"
    assert spaces.top_folder(HOME, (HOME,)) == HOME


def test_a_repo_with_its_own_space_wins_over_its_top_folder():
    assert spaces.pick_space(LAYOUT, f"{HOME}/code/livesync/src", (HOME,)) == ("sp-repo", True)
    assert spaces.pick_space(LAYOUT, f"{HOME}/code/other", (HOME,)) == ("sp-code", True)


def test_space_at_a_root_is_never_matched():
    """A space opened at ~ contains everything, so it says nothing."""
    sid, existed = spaces.pick_space(LAYOUT, f"{HOME}/scratch/x", (HOME,))
    assert sid != "sp-home" and existed is False


def test_tie_prefers_the_space_named_after_its_folder():
    assert spaces.pick_space(LAYOUT, f"{HOME}/notes/registry", (HOME,)) == ("sp-notes", True)


def test_top_folder_name_matches_a_space_made_elsewhere():
    assert spaces.pick_space(LAYOUT, f"{HOME}/wiki/2026", (HOME,)) == ("sp-named", True)


def test_new_space_id_is_stable_per_top_folder_and_app_shaped():
    a, _ = spaces.pick_space([], f"{HOME}/scratch/a", (HOME,))
    b, _ = spaces.pick_space([], f"{HOME}/scratch/b/c", (HOME,))
    other, _ = spaces.pick_space([], f"{HOME}/play", (HOME,))
    assert a == b != other
    assert re.fullmatch(r"sp[0-9a-f]{32}", a)
    # Once the app has adopted it, the same spawn finds it as existing.
    assert spaces.pick_space([{"id": a, "name": "", "folder": ""}], f"{HOME}/scratch/z", (HOME,)) == (a, True)


def test_ui_defaults_to_hc_and_herdr_only_on_request(tmp_path):
    h = host(tmp_path)
    assert h.resolve_ui(None) == "hc"
    assert h.resolve_ui("herdr") == "herdr"
    with pytest.raises(ConfigError):
        h.resolve_ui("tmux")


def test_ui_request_a_host_cannot_serve_is_refused_not_swapped(tmp_path):
    hc_only = host(tmp_path, transport="hc", hc_machine="web")
    assert hc_only.resolve_ui(None) == "hc"
    with pytest.raises(ConfigError, match="no herdr TUI"):
        hc_only.resolve_ui("herdr")

    plain_ssh = ssh_host(tmp_path)
    assert plain_ssh.resolve_ui(None) == "herdr"  # only the default falls back
    with pytest.raises(ConfigError, match="hc machine"):
        plain_ssh.resolve_ui("hc")


def test_model_and_effort_use_each_agents_own_flags(tmp_path):
    h = host(tmp_path)
    assert _agent_args(h, agents.profile("claude"), "s", "opus", "high") == [
        "--remote-control", "s", "--model", "opus", "--effort", "high",
    ]
    assert _agent_args(h, agents.profile("codex"), "s", "gpt-x", "low") == [
        "--model", "gpt-x", "-c", "model_reasoning_effort=low",
    ]
    # Omitted means the agent's own default: no flag at all.
    assert _agent_args(h, agents.profile("claude"), "s", None, "") == ["--remote-control", "s"]


def test_override_is_refused_for_unknown_agents_and_flag_shaped_values(tmp_path):
    h = host(tmp_path)
    with pytest.raises(ConfigError, match="don't know how to set model"):
        _agent_args(h, agents.profile("opencode"), "s", "big", None)
    for bad in ("--dangerously-skip-permissions", "a b", "x;y", "-m"):
        with pytest.raises(ConfigError):
            _agent_args(h, agents.profile("claude"), "s", bad, None)
        with pytest.raises(ConfigError):
            _agent_args(h, agents.profile("claude"), "s", None, bad)


def _fake_hc(tmp_path: Path, body: str) -> str:
    """A stand-in hc that logs each call's argv on one line, unit-separated."""
    p = tmp_path / "hc"
    log = tmp_path / "hc.log"
    p.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do printf "%s\\037" "$a" >> {shlex.quote(str(log))}; done\n'
        f'printf "\\n" >> {shlex.quote(str(log))}\n' + body
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _hc_calls(tmp_path: Path) -> list[list[str]]:
    return [c.split("\x1f")[:-1] for c in (tmp_path / "hc.log").read_text().splitlines() if c]


def test_hc_structured_error_is_parsed(tmp_path):
    bin_ = _fake_hc(tmp_path, """echo '{"error":{"kind":"name_taken","message":"taken","hint":"pick another"}}' >&2; exit 1\n""")
    hc = Hc(host(tmp_path, hc_bin=bin_))
    with pytest.raises(HerdrError) as ei:
        asyncio.run(hc.run(["run"]))
    assert ei.value.code == "name_taken" and "pick another" in ei.value.message


def test_hc_missing_binary_reports_unavailable(tmp_path):
    hc = Hc(host(tmp_path, hc_bin=str(tmp_path / "nope")))
    with pytest.raises(HerdrError) as ei:
        asyncio.run(hc.run(["ls"]))
    assert ei.value.code == "HERDR_UNAVAILABLE"


def test_hc_ls_keeps_only_this_hosts_terminals(tmp_path):
    bin_ = _fake_hc(tmp_path, """echo '{"terminals":[{"address":"t1"},{"address":"web/t2"},{"address":"other/t3"}]}'\n""")
    local = asyncio.run(Hc(host(tmp_path, hc_bin=bin_)).ls())
    remote = asyncio.run(Hc(host(tmp_path, hc_bin=bin_, hc_machine="web")).ls())
    assert [t["address"] for t in local] == ["t1"]
    assert [t["address"] for t in remote] == ["web/t2"]


def test_hc_start_never_takes_a_parent_and_keeps_the_prompt_one_argument(tmp_path):
    bin_ = _fake_hc(tmp_path, """echo '{"address":"web/t9"}'\n""")
    hc = Hc(host(tmp_path, hc_bin=bin_, hc_machine="web"))
    prompt = "fix it; rm -rf $HOME && echo 'done'"
    asyncio.run(hc.start("s1", "/srv/x", ["claude", "--model", "opus", prompt], {"herdr.space": "sp1"}))
    (call,) = _hc_calls(tmp_path)
    assert "--no-parent" in call and "--bg" in call
    assert call[call.index("--on") + 1] == "web"
    assert call[call.index("--label") + 1] == "herdr.space=sp1"
    assert call[call.index("--") + 1 :] == ["claude", "--model", "opus", prompt]


def test_first_turn_waits_through_the_startup_idle(tmp_path, monkeypatch):
    """A booting agent says `idle` before it picks the prompt up. That is not done."""
    replies = [
        {"reason": "status", "status": "idle", "turn": 0},
        {"reason": "status", "status": "done", "turn": 1},
    ]

    class FakeHc:
        async def wait(self, terminal, timeout_s=600):
            return replies.pop(0)

    async def no_sleep(_):
        return None

    monkeypatch.setattr("herdr_spawn.server.asyncio.sleep", no_sleep)
    out = asyncio.run(_hc_first_turn(FakeHc(), "t1"))
    assert out["status"] == "done" and not replies


def test_agent_pid_prefers_a_foreground_that_is_the_agent():
    shim = {"pid": 10, "foreground": {"pid": 22, "name": "claude", "argv": ["/x/claude"]}}
    direct = {"pid": 10, "foreground": {"pid": 10, "name": "node", "argv": ["node"]}}
    assert _hc_agent_pid(shim, "claude") == 22
    assert _hc_agent_pid(direct, "claude") == 10
    assert _hc_agent_pid({}, "claude") is None


def test_tidy_screen_drops_tui_padding():
    assert _tidy_screen("a   \n\n\n\n\nb\n\n\n") == "a\n\nb"


def _conf(tmp_path: Path, h: HostConfig) -> cfg.Config:
    return cfg.Config(hosts={h.name: h}, state_dir=tmp_path / "state")


def test_follow_refuses_a_session_we_did_not_spawn(tmp_path):
    with pytest.raises(ConfigError, match="no spawn named"):
        asyncio.run(follow_session(_conf(tmp_path, host(tmp_path)), session_name="someone-elses"))


def test_workspace_override_belongs_to_the_tui(tmp_path):
    h = host(tmp_path)
    with pytest.raises(ConfigError, match="ui=herdr"):
        asyncio.run(spawn(
            _conf(tmp_path, h), host_name=h.name, prompt="x", cwd_raw=str(tmp_path / "allowed"),
            workspace_override="w9",
        ))


def test_hc_spawn_end_to_end_against_a_fake_core(tmp_path):
    """Space picked from the layout, labels set, prompt as the last argument, and
    the record written so the follow-up tool can find it."""
    scratch = tmp_path / "allowed"
    scratch.mkdir()
    doc = {"jsonrpc": "2.0", "id": 1, "result": {"value": {"spaces": [
        {"id": "sp-scratch", "name": "", "folder": str(scratch)}]}, "version": 3}}
    body = f"""
case "$1" in
  server) echo '{{}}' ;;
  connect) cat >/dev/null; echo {shlex.quote(json.dumps(doc))} ;;
  run) echo '{{"address":"t7","argv":["opencode"]}}' ;;
  get) echo '{{"address":"t7","state":"live","pid":4242,"turn":1,"status":{{"state":"done","msg":"m"}},"foreground":{{"cwd":"/x"}}}}' ;;
  wait) echo '{{"reason":"status","status":"idle","turn":0}}' ;;
  send) echo '{{"turn":1,"started":true}}' ;;
  read) printf 'hello\\n\\n\\n\\n' ;;
esac
"""
    bin_ = _fake_hc(tmp_path, body)
    h = host(tmp_path, hc_bin=bin_, space_roots=(str(tmp_path),),
             agent_bin={"opencode": "/opt/oc/opencode"})
    conf = _conf(tmp_path, h)
    # opencode: no trust store to preflight, no Remote Control, prompt typed in.
    res = asyncio.run(spawn(conf, host_name=h.name, prompt="do the thing", cwd_raw=str(scratch),
                            agent="opencode", session_name_raw="job1"))
    assert res["status"] == "spawned" and res["ui"] == "hc"
    assert res["terminal"] == "t7" and res["space_id"] == "sp-scratch"
    assert "space_created" not in res and "remote_url" not in res

    calls = _hc_calls(tmp_path)
    run = next(c for c in calls if c[0] == "run")
    assert "herdr.space=sp-scratch" in run and run[run.index("--") + 1 :] == ["/opt/oc/opencode"]
    send = next(c for c in calls if c[0] == "send")
    assert send[-2:] == ["t7", "do the thing"]

    rec = Store(conf.state_dir).latest_by_name("job1")
    assert rec["ui"] == "hc" and rec["terminal"] == "t7"

    followed = asyncio.run(follow_session(conf, session_name="job1", message="and then?"))
    assert followed["status"] == "done" and followed["sent"] is True and followed["screen"] == "hello"


def test_old_records_without_ui_are_herdr_panes(tmp_path):
    store = Store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"session_name":"old","host":"local","pane_id":"w1:p1"}\n')
    assert store.latest_by_name("old").get("ui", "herdr") == "herdr"
    assert SpawnRecord(session_name="n", host="h").ui == "herdr"


def test_config_accepts_an_hc_only_host(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[hosts.web]\nenabled = true\ntransport = "hc"\nhc_machine = "web"\nhome = "/root"\n'
        'cwd_allowlist = ["/var/www"]\nspace_roots = ["/var/www"]\n'
    )
    h = cfg.load(p).host("web")
    assert (h.has_hc, h.has_herdr, h.space_roots) == (True, False, ("/var/www",))
    p.write_text('[hosts.web]\ntransport = "hc"\nui = "tmux"\n')
    with pytest.raises(ConfigError):
        cfg.load(p)


def test_remote_poll_pins_the_locale_for_ps():
    """A Norwegian mac prints `tor. 12 nov.`; Claude Code wrote `Thu Nov 12`."""
    assert "LC_ALL=C TZ=UTC ps -o lstart=" in rcmod._REMOTE_POLL
