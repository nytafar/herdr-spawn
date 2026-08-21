"""MCP server exposing a single tool: spawn_claude.

One tool, deliberately. This server is meant to be enabled in every chat and every
Claude Code session, so its tool definitions are paid for on every request whether
or not it gets used. Anything that can live in config instead of the schema, does.
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
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from pydantic import Field

from . import config as cfg
from . import rc as rcmod
from .config import AGENT_START_TIMEOUT_DEFAULT, Config, ConfigError, HostConfig
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


def cwd_is_trusted(claude_config: Path, cwd: Path) -> tuple[bool, str]:
    """Has this directory been through Claude's trust dialog?"""
    try:
        text = claude_config.read_text()
    except OSError as exc:
        return False, f"could not read {claude_config}: {exc}"
    return trust_from_config(text, cwd, source=str(claude_config))


def trust_from_config(text: str, cwd: Path, *, source: str) -> tuple[bool, str]:
    """The trust check itself, over the config's contents.

    Split from the file read so the ssh path can hand over what it fetched.

    Claude Code does not do an exact-key lookup -- it walks parent directories, so
    a trusted parent covers its children. Checking only the exact key would refuse
    spawns that would actually have worked fine.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, f"could not read {source}: {exc}"

    projects = data.get("projects")
    if not isinstance(projects, dict):
        return False, f"{source} has no projects map"

    for candidate in [cwd, *cwd.parents]:
        entry = projects.get(str(candidate))
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted"):
            return True, str(candidate)
    return False, (
        f"{cwd} is not trusted by Claude Code (no parent up to / has "
        "hasTrustDialogAccepted). Start Claude there once by hand and accept the "
        "trust dialog, otherwise the spawned pane just sits on that dialog."
    )


async def _find_inbox(h: Herdr, label: str) -> dict[str, Any] | None:
    """Workspace labels are not unique, so pick deterministically."""
    workspaces = await h.workspace_list()
    matches = [w for w in workspaces if w.get("label") == label]
    if not matches:
        return None
    return sorted(matches, key=lambda w: str(w.get("workspace_id")))[0]


async def _preflight(host: HostConfig, h: Herdr, cwd: Path) -> None:
    """Everything that must be true before we create anything at all.

    Ordering matters: a failure after a tab exists leaves an orphaned pane sitting
    on a dialog, which is worse than a clean refusal.
    """
    try:
        await h.probe()
    except HerdrError as exc:
        if exc.code == ERR_UNAVAILABLE:
            raise ConfigError(f"herdr is not reachable on {host.name}: {exc.message}") from exc
        raise ConfigError(f"herdr probe failed on {host.name}: {exc.code}: {exc.message}") from exc

    if host.transport == "socket":
        if not cwd.exists():
            raise ConfigError(f"cwd {cwd} does not exist on {host.name}")
        if not cwd.is_dir():
            raise ConfigError(f"cwd {cwd} is not a directory on {host.name}")
        ok, detail = cwd_is_trusted(Path(host.claude_config), cwd)
        if not ok:
            raise ConfigError(detail)
    else:
        await _preflight_remote(host, h, cwd)


# One round trip for both checks. The `__NO_CWD__` marker rather than a non-zero
# exit because the exits here belong to ssh, and conflating "the directory is
# missing" with "the connection failed" costs an afternoon.
_REMOTE_PREFLIGHT = r"""
set -u
test -d {cwd} || {{ echo __NO_CWD__; exit 0; }}
cat {claude_config} 2>/dev/null || echo __NO_CONFIG__
"""


async def _preflight_remote(host: HostConfig, h: Herdr, cwd: Path) -> None:
    """The same cwd and trust checks, on the far side of an ssh connection.

    Worth the round trip: an untrusted cwd does not fail, it hangs -- the pane
    opens and then sits on the trust dialog forever, looking alive.
    """
    script = _REMOTE_PREFLIGHT.format(
        cwd=shlex.quote(str(cwd)), claude_config=shlex.quote(host.claude_config)
    )
    try:
        out = await h.sh(script, timeout=30)
    except HerdrError as exc:
        raise ConfigError(f"could not preflight {host.name} over ssh: {exc.message}") from exc

    if out.startswith("__NO_CWD__"):
        raise ConfigError(f"cwd {cwd} does not exist on {host.name}")
    if out.startswith("__NO_CONFIG__"):
        raise ConfigError(
            f"could not read {host.claude_config} on {host.name} — has Claude Code ever run there?"
        )
    ok, detail = trust_from_config(out, cwd, source=f"{host.claude_config} on {host.name}")
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


def _agent_args(host: HostConfig, session_name: str) -> list[str]:
    """Args passed verbatim to the claude binary after `--`.

    Built as a list, never a shell string: passthrough is execve-style and a spaced
    argument must survive as a single argv element.
    """
    args = ["--remote-control", session_name]
    # Never a tool parameter. An unattended agent with permissions skipped, started
    # from a chat message, is the one combination that can quietly do real damage.
    if host.skip_permissions:
        args.append("--dangerously-skip-permissions")
    if host.sandbox:
        args.append("--sandbox")
    return args


async def _write_prompt_file(host: HostConfig, h: Herdr, cwd: Path, session_name: str, prompt: str) -> Path:
    """Drop the handoff brief where the agent will be running, not where we are.

    For an ssh host that means writing it on the far side; `cwd` is a path in the
    remote filesystem and creating it locally would silently produce a file nobody
    reads, in a directory that may not even exist here.

    The content goes over base64-encoded rather than in a heredoc: a prompt is
    arbitrary prose and can contain any delimiter you might pick.
    """
    p = cwd / ".herdr-handoff" / f"{session_name}.md"
    if host.transport == "socket":
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
    label: str | None = None,
    session_name_raw: str | None = None,
    workspace_override: str | None = None,
    wait: bool = False,
) -> dict[str, Any]:
    host = conf.host(host_name)
    h = Herdr(host)
    store = Store(conf.state_dir)

    if not prompt or not prompt.strip():
        raise ConfigError("prompt is required and cannot be empty")

    cwd = host.resolve_cwd(cwd_raw)
    session_name = coerce_session_name(session_name_raw, prompt, label)
    tab_label = label or session_name

    await _preflight(host, h, cwd)

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
        "session_name": session_name,
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "pane_id": pane_id,
        "cwd": str(cwd),
    }

    try:
        agent = await h.agent_start(
            session_name,
            pane_id,
            _agent_args(host, session_name),
            timeout_ms=AGENT_START_TIMEOUT_DEFAULT,
        )
    except HerdrError as exc:
        if exc.code == ERR_NAME_TAKEN:
            session_name = f"{session_name[:28]}-{os.urandom(2).hex()}"
            try:
                agent = await h.agent_start(
                    session_name,
                    pane_id,
                    _agent_args(host, session_name),
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

    session_id = (agent.get("agent_session") or {}).get("value")
    argv = agent.get("_argv")

    # --- resolve the Remote Control handle ---------------------------------
    pid = None
    try:
        pid = claude_pid(await h.pane_process_info(pane_id))
    except HerdrError:
        pass

    remote = rcmod.RemoteControl(
        session_id=session_id, bridge_session_id=None, url=None, pid=pid,
        proc_start=None, version=None, available=False,
        reason="pid could not be resolved from the pane",
    )
    if pid and host.transport == "socket":
        remote = await rcmod.resolve(
            sessions_dir=Path(host.claude_sessions_dir),
            pid=pid,
            expected_session_id=session_id,
            cwd=str(cwd),
        )
    elif pid:
        remote = await rcmod.resolve_remote(
            sh=h.sh,
            sessions_dir=Path(host.claude_sessions_dir),
            pid=pid,
            expected_session_id=session_id,
            cwd=str(cwd),
        )

    record = SpawnRecord(
        session_name=session_name,
        host=host.name,
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
        "session_name": session_name,
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "pane_id": pane_id,
        "cwd": str(cwd),
        "session_id": record.session_id,
        "remote_url": remote.url,
        "agent_status": agent.get("agent_status"),
    }
    if handoff_file:
        result["handoff_file"] = str(handoff_file)
    if not remote.available:
        result["rc_unavailable"] = remote.reason

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


# --- MCP wiring ------------------------------------------------------------


def build_server(conf: Config) -> MCPServer:
    server = MCPServer(name="herdr-spawn")

    # This server is enabled in every chat and every Claude Code session, so its
    # tool definition is paid for on every request whether or not it gets used.
    # Descriptions here are load-bearing only: `cwd` states the constraint that
    # would otherwise cause a refused call, and `prompt` sets the quality bar.
    # The rest are self-evident from their names and are left undescribed.
    @server.tool(structured_output=False)
    async def spawn_claude(
        host: str,
        prompt: Annotated[str, Field(description="Full handoff brief: task, constraints, what done looks like.")],
        cwd: Annotated[
            str | None,
            Field(description="Absolute path under an allowed root. Omit to use the host's default sandbox."),
        ] = None,
        label: str | None = None,
        session_name: str | None = None,
        workspace: str | None = None,
        wait: bool = False,
    ) -> str:
        """Start a Claude Code session on one of the configured hosts and hand it a prompt.

        Returns a claude.ai/code link to continue it from the Code tab.
        """
        try:
            result = await spawn(
                conf,
                host_name=host,
                prompt=prompt,
                cwd_raw=cwd,
                label=label,
                session_name_raw=session_name,
                workspace_override=workspace,
                wait=wait,
            )
        except (ConfigError, TeardownGuard) as exc:
            result = {"status": "refused", "reason": str(exc)}
        except HerdrError as exc:
            result = {"status": "failed", "reason": f"{exc.code}: {exc.message}"}
        log.info("spawn_claude host=%s -> %s", host, result.get("status"))
        return json.dumps(result, indent=2)

    _advertise_hosts(server, conf.host_names)
    _slim_advertised_schema(server)
    return server


def _advertise_hosts(server: MCPServer, hosts: list[str]) -> None:
    """Publish the configured host names as the `host` enum.

    They come from config rather than a `Literal` in the signature so that no
    particular deployment's machine names live in this source file. Hosts that
    are *disabled* are advertised too, deliberately: a structured refusal naming
    the missing prerequisite tells a calling model far more than the value simply
    not existing.
    """
    try:
        for tool in server._tool_manager._tools.values():  # noqa: SLF001
            prop = tool.parameters.get("properties", {}).get("host")
            if isinstance(prop, dict):
                prop["enum"] = hosts
    except (AttributeError, TypeError) as exc:  # pragma: no cover - defensive
        log.warning("could not advertise host enum (%s); host stays a free string", exc)


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


def main() -> None:
    """Entry point. Transport is chosen by config/env, not by the tool surface."""
    _configure_logging()
    conf = cfg.load(os.environ.get("HERDR_SPAWN_CONFIG"))
    server = build_server(conf)

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
