"""MCP server: spawn a coding agent, then follow it.

Three tools -- start one, talk to one we started, list what is running. This
server is meant to be enabled in every chat and every Claude Code session, so its
tool definitions are paid for on every request whether or not it gets used.
Anything that can live in config instead of the schema, does.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shlex
import sys
import tomllib
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from pydantic import Field

from . import config as cfg
from . import rc as rcmod
from . import spaces as spacemod
from .agents import DEFAULT_AGENT, AgentProfile, HostAgents, advertised, discover, discover_hc
from .agents import profile as agent_profile
from .config import AGENT_START_TIMEOUT_DEFAULT, Config, ConfigError, HostConfig
from .hc import ERR_HC_BLOCKED, ERR_HC_FINISHED, ERR_HC_NAME_TAKEN, ERR_HC_NOT_FOUND, Hc
from .herdr import (
    ERR_NAME_TAKEN,
    ERR_PANE_BUSY,
    ERR_PROMPT_STALLED,
    ERR_TIMEOUT,
    ERR_UNAVAILABLE,
    Herdr,
    HerdrError,
)
from .state import SpawnRecord, Store, TeardownGuard, assert_closable

log = logging.getLogger("herdr-spawn")

# Above this, a prompt goes to a file and we inject an @-reference instead.
# Terminal injection of multi-kilobyte text is fragile, and the file doubles as a
# record of what was actually asked.
PROMPT_FILE_THRESHOLD = 2000

# One lock per pane, held across the whole submit+settle cycle. Two `agent prompt`
# calls issued back to back concatenate into a single submitted line and the first
# Enter is lost -- and both calls still return rc=0. For a chat-driven service, two
# messages inside one second is ordinary traffic, not an edge case.
_pane_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _pane_lock(host: str, pane_id: str) -> asyncio.Lock:
    key = (host, pane_id)
    if key not in _pane_locks:
        _pane_locks[key] = asyncio.Lock()
    return _pane_locks[key]


def slugify(text: str, limit: int = 12) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:limit].rstrip("-")) or "task"


def coerce_session_name(raw: str | None, prompt: str, label: str | None) -> str:
    """herdr enforces ^[a-z][a-z0-9_-]{0,31}$ on agent names."""
    if raw:
        candidate = re.sub(r"[^a-z0-9_-]+", "-", raw.lower()).strip("-")
    else:
        stamp = datetime.now().strftime("%m%d-%H%M")
        candidate = f"chat-{slugify(label or prompt)}-{stamp}"
    candidate = re.sub(r"-{2,}", "-", candidate)
    if not candidate or not candidate[0].isalpha():
        candidate = f"c{candidate}"
    candidate = candidate[:32].rstrip("-_")
    if not re.match(cfg.SESSION_NAME_RE, candidate):
        candidate = "chat-" + hashlib.sha256(candidate.encode()).hexdigest()[:8]
    return candidate


def cwd_is_trusted(store: Path, cwd: Path, scheme: str = "claude") -> tuple[bool, str]:
    """Has this directory been through the agent's trust prompt?"""
    try:
        text = store.read_text()
    except OSError as exc:
        return False, f"could not read {store}: {exc}"
    return trust_from_config(text, cwd, source=str(store), scheme=scheme)


def trust_from_config(text: str, cwd: Path, *, source: str, scheme: str = "claude") -> tuple[bool, str]:
    """The trust check itself, over the store's contents.

    Split from the file read so the ssh path can hand over what it fetched, and
    from the format so a second agent can reuse the walk.

    Two stores, same shape: Claude keeps `projects.<path>.hasTrustDialogAccepted`
    in `~/.claude.json`, Codex keeps `projects.<path>.trust_level = "trusted"` in
    `~/.codex/config.toml`.

    Neither is looked up by exact key -- the walk goes up through the parents, so a
    trusted parent covers its children. Checking only the exact key would refuse
    spawns that would have worked fine.
    """
    try:
        data = json.loads(text) if scheme == "claude" else tomllib.loads(text)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        return False, f"could not read {source}: {exc}"

    projects = data.get("projects")
    if not isinstance(projects, dict):
        return False, f"{source} has no projects map"

    for candidate in [cwd, *cwd.parents]:
        entry = projects.get(str(candidate))
        if not isinstance(entry, dict):
            continue
        trusted = (
            entry.get("hasTrustDialogAccepted")
            if scheme == "claude"
            else entry.get("trust_level") == "trusted"
        )
        if trusted:
            return True, str(candidate)
    return False, (
        f"{cwd} is not trusted for {scheme} (no parent up to / is marked trusted in "
        f"{source}). Start it there once by hand and accept the trust prompt, "
        "otherwise the spawned pane just sits on that dialog."
    )


# Where each trust scheme keeps its store, relative to the host's home.
TRUST_STORES = {"claude": ".claude.json", "codex": ".codex/config.toml"}


def trust_store(host: HostConfig, scheme: str) -> str:
    if scheme == "claude":
        return host.claude_config  # configurable; the others are conventional
    return str(Path(host.home) / TRUST_STORES[scheme])


async def _find_inbox(h: Herdr, label: str) -> dict[str, Any] | None:
    """Workspace labels are not unique, so pick deterministically."""
    workspaces = await h.workspace_list()
    matches = [w for w in workspaces if w.get("label") == label]
    if not matches:
        return None
    return sorted(matches, key=lambda w: str(w.get("workspace_id")))[0]


async def _preflight(host: HostConfig, h: Herdr | Hc, cwd: Path, prof: AgentProfile) -> None:
    """Everything that must be true before we create anything at all.

    Ordering matters: a failure after a tab exists leaves an orphaned pane sitting
    on a dialog, which is worse than a clean refusal.

    An agent with no known trust store still gets the cwd existence check; we just
    cannot pre-empt its trust prompt, so the pane tail is the only warning.
    """
    try:
        await h.probe()
    except HerdrError as exc:
        if exc.code == ERR_UNAVAILABLE:
            raise ConfigError(f"{_ui_of(h)} is not reachable on {host.name}: {exc.message}") from exc
        raise ConfigError(f"{_ui_of(h)} probe failed on {host.name}: {exc.code}: {exc.message}") from exc

    if h.is_local:
        if not cwd.exists():
            raise ConfigError(f"cwd {cwd} does not exist on {host.name}")
        if not cwd.is_dir():
            raise ConfigError(f"cwd {cwd} is not a directory on {host.name}")
        if prof.trust:
            store = trust_store(host, prof.trust)
            ok, detail = cwd_is_trusted(Path(store), cwd, scheme=prof.trust)
            if not ok:
                raise ConfigError(detail)
    else:
        await _preflight_remote(host, h, cwd, prof)


def _ui_of(h: Herdr | Hc) -> str:
    return "hc" if isinstance(h, Hc) else "herdr"


# One round trip for both checks. The `__NO_CWD__` marker rather than a non-zero
# exit because the exits here belong to ssh, and conflating "the directory is
# missing" with "the connection failed" costs an afternoon.
_REMOTE_PREFLIGHT = r"""
set -u
test -d {cwd} || {{ echo __NO_CWD__; exit 0; }}
test -n {trust_store} || exit 0
cat {trust_store} 2>/dev/null || echo __NO_STORE__
"""


async def _preflight_remote(host: HostConfig, h: Herdr | Hc, cwd: Path, prof: AgentProfile) -> None:
    """The same cwd and trust checks, on the far side of an ssh connection.

    Worth the round trip: an untrusted cwd does not fail, it hangs -- the pane
    opens and then sits on the trust dialog forever, looking alive.
    """
    store = trust_store(host, prof.trust) if prof.trust else ""
    script = _REMOTE_PREFLIGHT.format(
        cwd=shlex.quote(str(cwd)), trust_store=shlex.quote(store)
    )
    try:
        out = await h.sh(script, timeout=30)
    except HerdrError as exc:
        raise ConfigError(f"could not preflight {host.name} remotely: {exc.message}") from exc

    if out.startswith("__NO_CWD__"):
        raise ConfigError(f"cwd {cwd} does not exist on {host.name}")
    if not prof.trust:
        return
    if out.startswith("__NO_STORE__"):
        raise ConfigError(
            f"could not read {store} on {host.name} — has {prof.kind} ever run there?"
        )
    ok, detail = trust_from_config(
        out, cwd, source=f"{store} on {host.name}", scheme=prof.trust
    )
    if not ok:
        raise ConfigError(detail)


def claude_pid(process_info: dict[str, Any]) -> int | None:
    """Pick the Claude Code process out of a pane's foreground set.

    NOT `foreground_processes[0]`. Claude spawns children -- `caffeinate`, one
    process per configured MCP server -- and they sort ahead of it: on a real
    pane the parent came back *last* of seven. Reading the session registry for a
    child's pid finds no record, so Remote Control reports itself unavailable on a
    spawn whose bridge connected perfectly well.

    The foreground process *group* id is the job leader, which is claude itself.
    Match on `argv0` as a second opinion -- `name` is the version string, not a
    name -- and keep the old behaviour as a last resort.
    """
    procs = process_info.get("foreground_processes") or []
    leader = process_info.get("foreground_process_group_id")
    if isinstance(leader, int) and leader > 0:
        return leader
    for p in procs:
        if str(p.get("argv0") or "").endswith("claude"):
            return p.get("pid")
    return procs[0].get("pid") if procs else None


# A model or effort value becomes an argument to the agent binary. It is passed
# execve-style so it cannot reach a shell, but it must not be able to pose as a
# flag either -- hence a leading alphanumeric and a short, plain alphabet.
_OVERRIDE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]/-]{0,63}$")


def _override_args(prof: AgentProfile, what: str, value: str | None) -> list[str]:
    value = (value or "").strip()
    if not value:
        return []  # the agent's own default
    template = prof.model_args if what == "model" else prof.effort_args
    if not template:
        raise ConfigError(
            f"don't know how to set {what} for {prof.kind!r}; omit it to use that agent's default"
        )
    if not _OVERRIDE_RE.match(value):
        raise ConfigError(f"{what} {value!r} is not a plain model/effort name")
    return [part.format(value) for part in template]


def _agent_args(
    host: HostConfig,
    prof: AgentProfile,
    session_name: str,
    model: str | None = None,
    effort: str | None = None,
) -> list[str]:
    """Args passed verbatim to the agent binary after `--`.

    Built as a list, never a shell string: passthrough is execve-style and a spaced
    argument must survive as a single argv element.

    Per agent, because these flags are not interchangeable -- Claude's
    `--dangerously-skip-permissions` is Codex's
    `--dangerously-bypass-approvals-and-sandbox`, and an agent with no profile gets
    no flags at all rather than someone else's.
    """
    args: list[str] = []
    if prof.remote_control:
        args += ["--remote-control", session_name]
    # Never a tool parameter. An unattended agent with permissions skipped, started
    # from a chat message, is the one combination that can quietly do real damage.
    # A host that asks for it against an agent whose flag we do not know gets
    # nothing, rather than a guess.
    if host.skip_permissions and prof.skip_permissions_flag:
        args.append(prof.skip_permissions_flag)
    if host.sandbox and prof.sandbox_flag:
        args.append(prof.sandbox_flag)
    args += _override_args(prof, "model", model)
    args += _override_args(prof, "effort", effort)
    return args


async def _write_prompt_file(host: HostConfig, h: Herdr | Hc, cwd: Path, session_name: str, prompt: str) -> Path:
    """Drop the handoff brief where the agent will be running, not where we are.

    For an ssh host that means writing it on the far side; `cwd` is a path in the
    remote filesystem and creating it locally would silently produce a file nobody
    reads, in a directory that may not even exist here.

    The content goes over base64-encoded rather than in a heredoc: a prompt is
    arbitrary prose and can contain any delimiter you might pick.
    """
    p = cwd / ".herdr-handoff" / f"{session_name}.md"
    if h.is_local:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(prompt, encoding="utf-8")
        return p

    blob = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
    await h.sh(
        f"set -eu\nmkdir -p {shlex.quote(str(p.parent))}\n"
        f"printf %s {shlex.quote(blob)} | base64 -d > {shlex.quote(str(p))}",
        timeout=30,
    )
    return p


async def spawn(
    conf: Config,
    *,
    host_name: str,
    prompt: str,
    cwd_raw: str | None,
    agent: str = DEFAULT_AGENT,
    label: str | None = None,
    session_name_raw: str | None = None,
    workspace_override: str | None = None,
    wait: bool = False,
    capabilities: dict[str, HostAgents] | None = None,
    ui: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    host = conf.host(host_name)
    store = Store(conf.state_dir)

    if not prompt or not prompt.strip():
        raise ConfigError("prompt is required and cannot be empty")

    agent = (agent or DEFAULT_AGENT).strip().lower()
    caps = (capabilities or {}).get(host_name)
    # The refusal carries the answer rather than just the complaint: a caller that
    # guessed wrong gets that host's actual list back and can retry immediately,
    # which is why the per-host matrix does not need to be in the tool schema.
    if caps and not caps.permits(agent):
        raise ConfigError(caps.refusal(agent, host_name))

    prof = agent_profile(agent)
    cwd = host.resolve_cwd(cwd_raw)
    session_name = coerce_session_name(session_name_raw, prompt, label)
    tab_label = label or session_name
    # Built before anything exists, so a bad model or effort is a clean refusal.
    agent_args = _agent_args(host, prof, session_name, model, effort)

    if host.resolve_ui(ui) == "hc":
        if workspace_override:
            raise ConfigError("`workspace` names a herdr TUI workspace; pass ui=herdr with it")
        hc = Hc(host)
        await _preflight(host, hc, cwd, prof)
        return await _spawn_hc(
            host, hc, store, prof=prof, agent=agent, cwd=cwd, session_name=session_name,
            label=tab_label, prompt=prompt, agent_args=agent_args, wait=wait,
            model=model, effort=effort,
        )

    h = Herdr(host)
    await _preflight(host, h, cwd, prof)

    # --- create the landing place ------------------------------------------
    workspace_id: str
    pane_id: str
    tab_id: str

    if workspace_override:
        if workspace_override in host.protected_workspaces:
            raise ConfigError(
                f"workspace {workspace_override} is on the protected list for {host.name}"
            )
        tab = await h.tab_create(workspace_override, str(cwd), tab_label)
        workspace_id = workspace_override
        pane_id = tab["root_pane"]["pane_id"]
        tab_id = tab["tab"]["tab_id"] if isinstance(tab.get("tab"), dict) else tab.get("tab_id", "")
    else:
        inbox = await _find_inbox(h, host.inbox_label)
        if inbox is None:
            # A fresh workspace comes back complete: workspace, first tab, and that
            # tab's root pane. No separate tab create needed.
            created = await h.workspace_create(host.inbox_label, str(cwd))
            workspace_id = created["workspace"]["workspace_id"]
            pane_id = created["root_pane"]["pane_id"]
            tab_id = created["tab"]["tab_id"]
        else:
            workspace_id = inbox["workspace_id"]
            tab = await h.tab_create(workspace_id, str(cwd), tab_label)
            pane_id = tab["root_pane"]["pane_id"]
            tab_id = tab["tab"]["tab_id"] if isinstance(tab.get("tab"), dict) else tab.get("tab_id", "")

    # From here on, any failure returns the pane rather than cleaning up silently.
    # A visible broken tab beats a vanished one.
    partial = {
        "status": "failed",
        "host": host.name,
        "agent": agent,
        "session_name": session_name,
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "pane_id": pane_id,
        "cwd": str(cwd),
    }

    try:
        started = await h.agent_start(
            session_name,
            pane_id,
            agent_args,
            kind=agent,
            timeout_ms=AGENT_START_TIMEOUT_DEFAULT,
        )
    except HerdrError as exc:
        if exc.code == ERR_NAME_TAKEN:
            session_name = f"{session_name[:28]}-{os.urandom(2).hex()}"
            try:
                started = await h.agent_start(
                    session_name,
                    pane_id,
                    _agent_args(host, prof, session_name, model, effort),
                    kind=agent,
                    timeout_ms=AGENT_START_TIMEOUT_DEFAULT,
                )
            except HerdrError as exc2:
                partial |= {"reason": f"{exc2.code}: {exc2.message}",
                            "pane_tail": await h.agent_read(session_name)}
                return partial
        else:
            tail = ""
            if exc.code not in (ERR_PANE_BUSY,):
                tail = await h.agent_read(session_name)
            partial |= {"reason": f"{exc.code}: {exc.message}", "pane_tail": tail}
            return partial

    session_id = (started.get("agent_session") or {}).get("value")
    argv = started.get("_argv")

    # --- resolve the Remote Control handle ---------------------------------
    # Claude Code only. Nothing else has a claude.ai bridge, so for every other
    # agent there is no session registry to poll and no link to hand back -- and
    # polling one anyway would just burn 15s reaching a foregone conclusion.
    remote = rcmod.RemoteControl(
        session_id=session_id, bridge_session_id=None, url=None, pid=None,
        proc_start=None, version=None, available=False,
        reason=f"{agent} has no Remote Control bridge; continue in the pane",
    )
    if prof.remote_control:
        pid = None
        try:
            pid = claude_pid(await h.pane_process_info(pane_id))
        except HerdrError:
            pass
        if not pid:
            remote = replace(remote, reason="pid could not be resolved from the pane")
        else:
            remote = await _resolve_rc(host, h, pid, session_id, cwd)

    record = SpawnRecord(
        session_name=session_name,
        host=host.name,
        agent=agent,
        workspace_id=workspace_id,
        tab_id=tab_id,
        pane_id=pane_id,
        cwd=str(cwd),
        label=tab_label,
        pid=remote.pid,
        proc_start=remote.proc_start,
        session_id=remote.session_id or session_id,
        bridge_session_id=remote.bridge_session_id,
        url=remote.url,
        claude_version=remote.version,
        argv=argv,
        # The gateway logs tool names but never arguments, so this is the only
        # place the injected prompt is accounted for anywhere on the box.
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        rc_available=remote.available,
        ui="herdr",
        model=model,
        effort=effort,
    )
    store.append(record)

    await h.pane_report_metadata(
        pane_id,
        {"spawn": session_name, **({"rc": remote.bridge_session_id} if remote.bridge_session_id else {})},
    )

    # --- deliver the prompt -------------------------------------------------
    prompt_text = prompt
    handoff_file = None
    if len(prompt) > PROMPT_FILE_THRESHOLD:
        handoff_file = await _write_prompt_file(host, h, cwd, session_name, prompt)
        prompt_text = (
            f"Your handoff brief is at @{handoff_file.relative_to(cwd)} — read it first, "
            "then start on it."
        )

    result = {
        "status": "spawned",
        "host": host.name,
        "agent": agent,
        "ui": "herdr",
        "session_name": session_name,
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "pane_id": pane_id,
        "cwd": str(cwd),
        "session_id": record.session_id,
        "agent_status": started.get("agent_status"),
    }
    if prof.remote_control:
        result["remote_url"] = remote.url
        if not remote.available:
            result["rc_unavailable"] = remote.reason
    else:
        # Say it once, plainly, rather than returning a null `remote_url` that reads
        # like a failure. `herdr agent prompt <session_name>` is how you continue.
        result["continue_with"] = f"herdr agent prompt {session_name}"
    if handoff_file:
        result["handoff_file"] = str(handoff_file)

    async with _pane_lock(host.name, pane_id):
        try:
            prompted = await h.agent_prompt(session_name, prompt_text, wait=wait)
        except HerdrError as exc:
            if exc.code == ERR_PROMPT_STALLED:
                result["status"] = "spawned_prompt_stalled"
                result["reason"] = (
                    "the agent did not register the prompt within 5s. The pane exists "
                    "and you can take over manually."
                )
            elif exc.code == ERR_TIMEOUT:
                result["status"] = "working"
                result["reason"] = "still working when the wait budget ran out"
            else:
                result["status"] = "spawned_prompt_failed"
                result["reason"] = f"{exc.code}: {exc.message}"
            result["pane_tail"] = await h.agent_read(session_name)
            return result

        if wait:
            final = (prompted.get("agent") or {}).get("agent_status")
            # Both `done` and `idle` mean ready for input. A backgrounded spawn
            # settles on `done`, which is precisely the state an explicit
            # `--until idle` would have excluded.
            if final in ("idle", "done"):
                result["status"] = "ready"
            elif final == "blocked":
                result["status"] = "blocked"
                result["reason"] = "the agent is waiting on an approval"
            else:
                result["status"] = "working"
            result["agent_status"] = final

    return result


async def _resolve_rc(
    host: HostConfig, h: Herdr | Hc, pid: int, session_id: str | None, cwd: Path
) -> rcmod.RemoteControl:
    """Read the Remote Control handle from wherever the agent actually runs."""
    if h.is_local:
        return await rcmod.resolve(
            sessions_dir=Path(host.claude_sessions_dir),
            pid=pid,
            expected_session_id=session_id,
            cwd=str(cwd),
        )
    return await rcmod.resolve_remote(
        sh=h.sh,
        sessions_dir=Path(host.claude_sessions_dir),
        pid=pid,
        expected_session_id=session_id,
        cwd=str(cwd),
    )


# --- hc ---------------------------------------------------------------------

# How long `wait=True` holds the call open for the first turn.
HC_WAIT_BUDGET_S = 600


def _hc_status(info: dict[str, Any]) -> str | None:
    """The status word out of `hc get` / `hc ls`, which shape it differently."""
    st = info.get("status")
    if isinstance(st, dict):
        return st.get("state")
    return st if isinstance(st, str) else None


def _hc_agent_pid(info: dict[str, Any], kind: str) -> int | None:
    """The agent's own pid in an hc terminal.

    The terminal's `pid` is whatever hc started, which is the agent itself unless a
    version-manager shim forked instead of exec'ing -- then the real process is the
    foreground one. Prefer the foreground when it is recognisably the agent.
    """
    fg = info.get("foreground") or {}
    argv0 = str((fg.get("argv") or [""])[0])
    if fg.get("pid") and (fg.get("name") == kind or argv0.rsplit("/", 1)[-1] == kind):
        return fg["pid"]
    return info.get("pid") or fg.get("pid")


async def _hc_first_turn(hc: Hc, terminal: str, budget_s: int = HC_WAIT_BUDGET_S) -> dict[str, Any]:
    """Wait for the turn the launch prompt started, not for the agent to boot.

    A fresh agent reports `idle` while it starts up, before it has picked the
    prompt up, and `hc wait` returns on any word but `working`. Taking that first
    idle at face value would report a spawn as finished before it began -- so an
    idle on turn 0 is waited through.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_s
    last: dict[str, Any] = {}
    while True:
        remaining = int(deadline - loop.time())
        if remaining <= 0:
            return last or {"reason": "timeout"}
        last = await hc.wait(terminal, timeout_s=remaining)
        if last.get("reason") == "timeout" or last.get("reason") == "exit":
            return last
        if last.get("status") == "idle" and not last.get("turn"):
            await asyncio.sleep(1)
            continue
        return last


def _apply_wait(result: dict[str, Any], waited: dict[str, Any]) -> None:
    status = waited.get("status")
    result["agent_status"] = status
    if waited.get("reason") == "exit":
        result["status"] = "exited"
        result["reason"] = f"the agent exited (code {waited.get('exit_code')})"
    elif status in ("idle", "done"):
        result["status"] = "ready"
    elif status == "blocked":
        result["status"] = "blocked"
        result["reason"] = "the agent is waiting on an approval"
    elif status == "error":
        result["status"] = "error"
    else:
        result["status"] = "working"
        if waited.get("reason") == "timeout":
            result["reason"] = "still working when the wait budget ran out"


async def _spawn_hc(
    host: HostConfig,
    hc: Hc,
    store: Store,
    *,
    prof: AgentProfile,
    agent: str,
    cwd: Path,
    session_name: str,
    label: str,
    prompt: str,
    agent_args: list[str],
    wait: bool,
    model: str | None,
    effort: str | None,
) -> dict[str, Any]:
    """Start the agent in an hc terminal, in the space its folder belongs to."""
    # --- where it lands -----------------------------------------------------
    # An unreadable layout is not a reason to refuse: the derived id still puts
    # the spawn in its top folder's space, it just cannot find a narrower one.
    space_note = None
    try:
        layout = await hc.spaces()
    except HerdrError as exc:
        layout, space_note = [], f"could not read the app layout ({exc.code}); used the top folder's space"
    space_id, existed = spacemod.pick_space(layout, str(cwd), host.space_roots)

    # --- the prompt ---------------------------------------------------------
    prompt_text = prompt
    handoff_file = None
    if len(prompt) > PROMPT_FILE_THRESHOLD:
        handoff_file = await _write_prompt_file(host, hc, cwd, session_name, prompt)
        prompt_text = (
            f"Your handoff brief is at @{handoff_file.relative_to(cwd)} — read it first, "
            "then start on it."
        )

    def argv_for(name: str) -> list[str]:
        args = [name if a == session_name else a for a in agent_args]
        argv = [host.agent_bin.get(agent, agent), *prof.hc_args, *args]
        if prof.prompt_as_arg:
            # `--` only when needed: a brief that opens with a dash would
            # otherwise be read as a flag.
            argv += ["--", prompt_text] if prompt_text.startswith("-") else [prompt_text]
        return argv

    labels = {"herdr.space": space_id, "herdr-spawn.session": session_name}
    partial = {
        "status": "failed", "host": host.name, "agent": agent, "ui": "hc",
        "session_name": session_name, "space_id": space_id, "cwd": str(cwd),
    }
    try:
        started = await hc.start(session_name, str(cwd), argv_for(session_name), labels)
    except HerdrError as exc:
        if exc.code != ERR_HC_NAME_TAKEN:
            return partial | {"reason": f"{exc.code}: {exc.message}"}
        retry = f"{session_name[:27]}-{os.urandom(2).hex()}"
        labels["herdr-spawn.session"] = retry
        try:
            started = await hc.start(retry, str(cwd), argv_for(retry), labels)
        except HerdrError as exc2:
            return partial | {"reason": f"{exc2.code}: {exc2.message}"}
        session_name = retry

    terminal = str(started.get("address"))

    # --- Remote Control ------------------------------------------------------
    remote = rcmod.RemoteControl(
        session_id=None, bridge_session_id=None, url=None, pid=None,
        proc_start=None, version=None, available=False,
        reason=f"{agent} has no Remote Control bridge; continue in the terminal",
    )
    info: dict[str, Any] = {}
    try:
        info = await hc.get(terminal)
    except HerdrError:
        pass
    if prof.remote_control:
        pid = _hc_agent_pid(info, agent)
        if not pid:
            remote = replace(remote, reason="pid could not be resolved from the terminal")
        else:
            remote = await _resolve_rc(host, hc, pid, None, cwd)

    store.append(SpawnRecord(
        session_name=session_name,
        host=host.name,
        agent=agent,
        cwd=str(cwd),
        label=label,
        pid=remote.pid,
        proc_start=remote.proc_start,
        session_id=remote.session_id,
        bridge_session_id=remote.bridge_session_id,
        url=remote.url,
        claude_version=remote.version,
        argv=started.get("argv") or info.get("argv"),
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        rc_available=remote.available,
        ui="hc",
        terminal=terminal,
        space_id=space_id,
        model=model,
        effort=effort,
    ))

    result: dict[str, Any] = {
        "status": "spawned",
        "host": host.name,
        "agent": agent,
        "ui": "hc",
        "session_name": session_name,
        "terminal": terminal,
        "space_id": space_id,
        "cwd": str(cwd),
        "session_id": remote.session_id,
    }
    if not existed:
        result["space_created"] = True
    if space_note:
        result["space_note"] = space_note
    if prof.remote_control:
        result["remote_url"] = remote.url
        if not remote.available:
            result["rc_unavailable"] = remote.reason
    if handoff_file:
        result["handoff_file"] = str(handoff_file)

    # --- deliver, and optionally wait ----------------------------------------
    try:
        if not prof.prompt_as_arg:
            # No way to hand this binary its prompt at launch, so wait for it to
            # come up and type it. A send to a booting agent can be dropped.
            await hc.wait(terminal, timeout_s=60)
            sent = await hc.send(terminal, prompt_text, wait=wait, timeout_s=HC_WAIT_BUDGET_S)
            if wait:
                _apply_wait(result, sent.get("wait") or {})
        elif wait:
            _apply_wait(result, await _hc_first_turn(hc, terminal))
    except HerdrError as exc:
        result["status"] = "working" if exc.code == ERR_TIMEOUT else "spawned_prompt_failed"
        result["reason"] = f"{exc.code}: {exc.message}"
        result["screen"] = _tidy_screen(await hc.read(terminal))
    return result


# --- following a spawn -------------------------------------------------------

SCREEN_LINES = 30


def _tidy_screen(text: str) -> str:
    """Drop the padding from a terminal read.

    A headless hc terminal is 120 rows tall and a TUI fills them, so a raw read is
    mostly blank lines -- paid for in tokens by whoever asked.
    """
    out: list[str] = []
    for line in (ln.rstrip() for ln in text.splitlines()):
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip("\n")


async def follow_session(
    conf: Config, *, session_name: str, message: str | None = None, wait: bool = False
) -> dict[str, Any]:
    """State and screen of a session we spawned; optionally send it a message first.

    Only sessions in our own record. A chat message that could type into any
    terminal on the machine is a far larger thing than one that can follow up on
    what it started.
    """
    store = Store(conf.state_dir)
    rec = store.latest_by_name((session_name or "").strip())
    if rec is None:
        raise ConfigError(
            f"no spawn named {session_name!r} on record; list_agents shows the ones that are"
        )
    host = conf.host(rec["host"])
    cwd = Path(rec.get("cwd") or host.home)
    message = message if message and message.strip() else None
    out: dict[str, Any] = {"session_name": rec["session_name"], "host": host.name, "agent": rec.get("agent")}

    if rec.get("ui", "herdr") == "hc":
        hc = Hc(host)
        terminal = rec.get("terminal") or rec["session_name"]
        out["terminal"] = terminal
        try:
            info = await hc.get(terminal)
        except HerdrError as exc:
            if exc.code == ERR_HC_NOT_FOUND:
                return out | {"status": "gone", "reason": "the terminal no longer exists"}
            raise
        if info.get("state") != "live":
            return out | {
                "status": "exited", "exit_code": info.get("exit_code"),
                "screen": _tidy_screen(await hc.read(terminal, SCREEN_LINES)),
            }
        if message:
            text = message
            if len(message) > PROMPT_FILE_THRESHOLD:
                stamp = datetime.now().strftime("%H%M%S")
                f = await _write_prompt_file(host, hc, cwd, f"{rec['session_name']}-{stamp}", message)
                text = f"A follow-up message is at @{f.relative_to(cwd)} — read it and act on it."
            try:
                sent = await hc.send(terminal, text, wait=wait, timeout_s=HC_WAIT_BUDGET_S)
                out["sent"] = True
                if sent.get("started") is False:
                    out["note"] = "sent, but the agent did not start working on it"
            except HerdrError as exc:
                if exc.code not in (ERR_HC_BLOCKED, ERR_HC_FINISHED, ERR_TIMEOUT):
                    raise
                out["sent"] = exc.code == ERR_TIMEOUT
                out["note"] = (
                    "still working when the wait budget ran out" if exc.code == ERR_TIMEOUT
                    else f"not sent: {exc.message}"
                )
            info = await hc.get(terminal)
        fg = info.get("foreground") or {}
        st = info.get("status") if isinstance(info.get("status"), dict) else {}
        out |= {"status": _hc_status(info) or "unknown", "turn": info.get("turn"), "cwd": fg.get("cwd")}
        if st.get("msg"):
            out["doing"] = st["msg"]
        out["screen"] = _tidy_screen(await hc.read(terminal, SCREEN_LINES))
        return out

    h = Herdr(host)
    name = rec["session_name"]
    out["pane_id"] = rec.get("pane_id")
    if message:
        async with _pane_lock(host.name, rec.get("pane_id") or name):
            try:
                await h.agent_prompt(name, message, wait=wait)
                out["sent"] = True
            except HerdrError as exc:
                out["sent"] = False
                out["note"] = f"{exc.code}: {exc.message}"
    try:
        info = await h.agent_get(name)
    except HerdrError as exc:
        return out | {"status": "gone", "reason": f"{exc.code}: {exc.message}"}
    out["status"] = info.get("agent_status") or "unknown"
    out["screen"] = _tidy_screen(await h.agent_read(name, SCREEN_LINES))
    return out


# How many of our own herdr-TUI spawns to look up per host. Each costs a CLI call,
# and names in the TUI are cleared when the agent leaves, so old ones only miss.
HERDR_LIST_RECENT = 8


async def _list_host(host: HostConfig, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mine = [r for r in records if r.get("host") == host.name and not r.get("closed_at")]
    out: list[dict[str, Any]] = []
    if host.has_hc:
        by_terminal = {r["terminal"]: r for r in mine if r.get("ui") == "hc" and r.get("terminal")}
        for t in await Hc(host).ls():
            rec = by_terminal.get(t.get("address"))
            # Plain programs report no status; they are not what this is for.
            if not rec and not t.get("status"):
                continue
            row = {
                "host": host.name, "terminal": t.get("address"), "agent": t.get("agent"),
                "status": _hc_status(t) or "unknown", "cwd": t.get("cwd"),
            }
            for k in ("name", "where"):
                if t.get(k):
                    row[k] = t[k]
            if rec:
                row["spawned"] = rec["session_name"]
            out.append(row)
    if host.has_herdr:
        latest: dict[str, dict[str, Any]] = {}
        for r in mine:
            if r.get("ui", "herdr") == "herdr" and r.get("session_name"):
                latest[r["session_name"]] = r
        h = Herdr(host)
        for name, rec in list(latest.items())[-HERDR_LIST_RECENT:]:
            try:
                info = await h.agent_get(name)
            except HerdrError:
                continue  # the agent has left its pane
            out.append({
                "host": host.name, "pane_id": rec.get("pane_id"), "agent": rec.get("agent"),
                "status": info.get("agent_status") or "unknown", "cwd": rec.get("cwd"),
                "name": name, "where": "herdr TUI", "spawned": name,
            })
    return out


async def list_hosts(conf: Config, host_name: str | None = None) -> dict[str, Any]:
    """Agents on the hosts and what they are doing, so a caller can pick one."""
    hosts = [conf.host(host_name)] if host_name else [h for h in conf.hosts.values() if h.enabled]
    records = Store(conf.state_dir).all()
    results = await asyncio.gather(*(_list_host(h, records) for h in hosts), return_exceptions=True)
    agents: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for h, res in zip(hosts, results, strict=True):
        if isinstance(res, BaseException):
            errors[h.name] = str(res)
        else:
            agents += res
    out: dict[str, Any] = {"agents": agents}
    if errors:
        out["errors"] = errors
    return out


# --- MCP wiring ------------------------------------------------------------


def build_server(conf: Config, capabilities: dict[str, HostAgents] | None = None) -> MCPServer:
    server = MCPServer(name="herdr-spawn")
    caps = capabilities or {}

    # This server is enabled in every chat and every Claude Code session, so its
    # tool definition is paid for on every request whether or not it gets used.
    # Descriptions here are load-bearing only: `cwd` states the constraint that
    # would otherwise cause a refused call, and `prompt` sets the quality bar.
    # The rest are self-evident from their names and are left undescribed.
    @server.tool(structured_output=False)
    async def spawn_agent(
        host: str,
        prompt: Annotated[str, Field(description="Full handoff brief: task, constraints, what done looks like.")],
        agent: str = DEFAULT_AGENT,
        cwd: Annotated[
            str | None,
            Field(description="Absolute path under an allowed root. Omit to use the host's default sandbox."),
        ] = None,
        label: str | None = None,
        session_name: str | None = None,
        workspace: str | None = None,
        wait: bool = False,
        ui: Annotated[
            str | None,
            Field(description="hc (default): shows in the Herdr app and on the phone. herdr: the terminal TUI, only when asked for."),
        ] = None,
        model: Annotated[str | None, Field(description="Omit for the agent's default.")] = None,
        effort: Annotated[str | None, Field(description="Omit for the agent's default.")] = None,
    ) -> str:
        """Start a coding agent on one of the configured hosts and hand it a prompt.

        claude returns a claude.ai/code link to continue from the Code tab. Follow
        any spawn with agent_session.
        """
        try:
            result = await spawn(
                conf,
                host_name=host,
                prompt=prompt,
                cwd_raw=cwd,
                agent=agent,
                label=label,
                session_name_raw=session_name,
                workspace_override=workspace,
                wait=wait,
                capabilities=caps,
                ui=ui,
                model=model,
                effort=effort,
            )
        except (ConfigError, TeardownGuard) as exc:
            result = {"status": "refused", "reason": str(exc)}
        except HerdrError as exc:
            result = {"status": "failed", "reason": f"{exc.code}: {exc.message}"}
        log.info("spawn_agent host=%s agent=%s -> %s", host, agent, result.get("status"))
        return json.dumps(result, indent=2)

    @server.tool(structured_output=False)
    async def agent_session(
        session_name: Annotated[str, Field(description="From spawn_agent, or `spawned` in list_agents.")],
        message: str | None = None,
        wait: bool = False,
    ) -> str:
        """Status and screen of an agent started by spawn_agent. With `message`, send it first."""
        try:
            result = await follow_session(
                conf, session_name=session_name, message=message, wait=wait
            )
        except (ConfigError, TeardownGuard) as exc:
            result = {"status": "refused", "reason": str(exc)}
        except HerdrError as exc:
            result = {"status": "failed", "reason": f"{exc.code}: {exc.message}"}
        log.info("agent_session %s sent=%s -> %s", session_name, bool(message), result.get("status"))
        return json.dumps(result, indent=2)

    @server.tool(structured_output=False)
    async def list_agents(host: str | None = None) -> str:
        """Agents on the hosts with their status: working, idle, done, blocked."""
        try:
            result = await list_hosts(conf, host)
        except ConfigError as exc:
            result = {"status": "refused", "reason": str(exc)}
        return json.dumps(result, separators=(",", ":"))

    _advertise_enum(server, "host", conf.host_names)
    _advertise_enum(server, "ui", list(cfg.UIS))
    _advertise_enum(server, "agent", advertised(caps, conf.host_names))
    _slim_advertised_schema(server)
    return server


def _advertise_enum(server: MCPServer, param: str, values: list[str]) -> None:
    """Publish a discovered set as a parameter's enum.

    Neither list is a `Literal` in the signature, so no deployment's machine names
    or installed software live in this source file. Both are what the schema is
    *for*: telling a calling model the small set of values that can work.

    Hosts that are disabled are advertised anyway -- a structured refusal naming
    the missing prerequisite says far more than the value not existing. Agents that
    are not installed anywhere are NOT advertised, because that costs tokens on
    every request forever to describe something that can only ever be refused.
    """
    try:
        for tool in server._tool_manager._tools.values():  # noqa: SLF001
            prop = tool.parameters.get("properties", {}).get(param)
            if isinstance(prop, dict) and values:
                prop["enum"] = values
    except (AttributeError, TypeError) as exc:  # pragma: no cover - defensive
        log.warning("could not advertise %s enum (%s); it stays a free string", param, exc)


def _slim_schema(node: Any) -> Any:
    """Strip annotation-only noise from a generated JSON schema.

    Pydantic emits a `title` for every field and expands `str | None` into
    `anyOf: [{type: string}, {type: null}]`. Neither tells a calling model
    anything it cannot read off the parameter name, and together they are about
    a quarter of this server's per-request cost.

    Collapsing the optional union makes the advertised schema *stricter* than
    what is actually accepted, which is the safe direction: a client that omits
    the parameter still works, and one that explicitly sends null still
    validates.
    """
    if isinstance(node, dict):
        node.pop("title", None)
        variants = node.get("anyOf")
        if isinstance(variants, list) and len(variants) == 2:
            non_null = [v for v in variants if v.get("type") != "null"]
            if len(non_null) == 1 and len(non_null[0]) == 1:
                node.pop("anyOf")
                node.update(non_null[0])
        return {k: _slim_schema(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_slim_schema(v) for v in node]
    return node


def _slim_advertised_schema(server: MCPServer) -> None:
    """Best effort. `parameters` is what clients see; `fn_metadata` is what
    validates incoming arguments, so editing the former cannot desync them.

    Reaching it goes through one private attribute. If a future SDK moves it we
    simply keep the verbose schema -- more tokens, still correct.
    """
    try:
        for tool in server._tool_manager._tools.values():  # noqa: SLF001
            tool.parameters = _slim_schema(tool.parameters)
    except (AttributeError, TypeError) as exc:  # pragma: no cover - defensive
        log.warning("could not slim tool schema (%s); serving the verbose one", exc)


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("HERDR_SPAWN_LOG", "INFO"),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def discover_all(conf: Config) -> dict[str, HostAgents]:
    """Ask every enabled host what it can start, concurrently.

    Once, at startup. Two round trips per host is nothing here and unaffordable per
    request -- and a host that is asleep just comes back `error`, which permits
    everything rather than hiding its agents until the next restart.
    """
    names = [n for n in conf.host_names if conf.hosts[n].enabled]
    results = await asyncio.gather(
        *(
            discover(Herdr(conf.hosts[n])) if conf.hosts[n].has_herdr else discover_hc(Hc(conf.hosts[n]))
            for n in names
        ),
        return_exceptions=True,
    )
    out: dict[str, HostAgents] = {}
    for name, res in zip(names, results, strict=True):
        if isinstance(res, BaseException):
            out[name] = HostAgents(error=f"discovery raised {res!r}")
            log.warning("agent discovery failed on %s: %r", name, res)
        else:
            out[name] = res
    return out


def main() -> None:
    """Entry point. Transport is chosen by config/env, not by the tool surface."""
    _configure_logging()
    conf = cfg.load(os.environ.get("HERDR_SPAWN_CONFIG"))
    caps = asyncio.run(discover_all(conf))
    server = build_server(conf, caps)

    transport = os.environ.get("HERDR_SPAWN_TRANSPORT", "stdio")
    if transport == "stdio":
        server.run("stdio")
        return

    # P2: bind the gateway bridge only. Never 0.0.0.0 -- there is no firewall on
    # this host, so the bind address is the entire security boundary.
    bind = os.environ.get("HERDR_SPAWN_BIND", "127.0.0.1")
    port = int(os.environ.get("HERDR_SPAWN_PORT", "8230"))
    # Stateless: each request stands alone, so nothing breaks if the gateway's
    # mcp-proxy hop drops the Mcp-Session-Id header (the deployed proxy image
    # predates the passthrough fix). A fire-and-forget spawn needs no session
    # continuity anyway.
    log.info("serving streamable-http on %s:%d (stateless)", bind, port)
    server.run("streamable-http", host=bind, port=port, stateless_http=True)


if __name__ == "__main__":
    main()
