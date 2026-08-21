"""Thin wrapper over the herdr CLI.

Shelling out rather than speaking the unix socket is deliberate: the socket works
(newline-delimited JSON, no handshake) and `herdr api schema` publishes all 90
methods, but talking to it directly pins us to protocol 19, while the CLI absorbs
protocol drift for free.

Three return conventions, all of which show up in practice:

    rc=0  -> JSON on stdout
    rc=1  -> JSON on stderr, carrying .error.code
    rc=2  -> PLAIN TEXT on stderr, no JSON at all
             (e.g. `--kind claudecode` prints a bare
              "unsupported interactive agent kind: claudecode")

A wrapper that unconditionally json.loads(stderr) crashes on the third one.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from .config import HostConfig, herdr_env


@dataclass
class HerdrError(Exception):
    code: str
    message: str
    argv: list[str]
    returncode: int
    raw: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"


# Error codes we branch on by name. Anything else surfaces verbatim.
ERR_UNAVAILABLE = "HERDR_UNAVAILABLE"
ERR_INVALID_NAME = "invalid_agent_name"
ERR_NAME_TAKEN = "agent_name_taken"
ERR_INVALID_TIMEOUT = "invalid_agent_timeout"
ERR_PANE_BUSY = "agent_pane_busy"
ERR_PANE_NOT_FOUND = "agent_pane_not_found"
ERR_PROMPT_STALLED = "agent_prompt_stalled"
ERR_TIMEOUT = "timeout"


class Herdr:
    """Invokes the herdr CLI for one host."""

    def __init__(self, host: HostConfig):
        self.host = host

    def _base_argv(self) -> list[str]:
        if self.host.transport == "ssh":
            # Remote hosts are driven by forwarding their herdr socket and running
            # the LOCAL binary against it -- `herdr --remote` rejects subcommands
            # outright, and a bare `herdr` is not on PATH over non-interactive ssh.
            # The forward itself is set up out of band; here we just point at it.
            return [self.host.herdr_bin]
        return [self.host.herdr_bin]

    async def run(self, args: list[str], timeout: float | None = None) -> dict[str, Any]:
        """Run a herdr subcommand and return the parsed JSON result.

        `args` is always a list. Never build a shell string: the AGENT_ARGS
        passthrough depends on argv elements surviving verbatim, spaces included.
        """
        argv = self._base_argv() + args
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=herdr_env(self.host),
            )
        except FileNotFoundError as exc:
            raise HerdrError(
                code=ERR_UNAVAILABLE,
                message=f"herdr binary not found at {self.host.herdr_bin} on {self.host.name}",
                argv=argv,
                returncode=-1,
            ) from exc

        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise HerdrError(
                code=ERR_TIMEOUT,
                message=f"herdr {' '.join(args[:2])} exceeded {timeout}s locally",
                argv=argv,
                returncode=-1,
            ) from None

        out = out_b.decode("utf-8", "replace")
        err = err_b.decode("utf-8", "replace")

        if proc.returncode == 0:
            try:
                return json.loads(out)
            except json.JSONDecodeError as exc:
                raise HerdrError(
                    code="unparseable_output",
                    message=f"herdr returned rc=0 but stdout was not JSON: {out[:400]}",
                    argv=argv,
                    returncode=0,
                    raw=out,
                ) from exc

        # rc=1 is structured; rc=2 (and anything else) may well not be.
        code, message = "herdr_failed", err.strip() or out.strip() or "no output"
        try:
            payload = json.loads(err)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            err_obj = payload.get("error")
            if isinstance(err_obj, dict):
                code = err_obj.get("code") or code
                message = err_obj.get("message") or message

        if "connection refused" in message.lower() or "server_not_running" in message:
            code = ERR_UNAVAILABLE

        raise HerdrError(
            code=code,
            message=message,
            argv=argv,
            returncode=proc.returncode if proc.returncode is not None else -1,
            raw=err or out,
        )

    async def run_text(self, args: list[str], timeout: float | None = None) -> str:
        """For subcommands that emit raw terminal text rather than JSON.

        `herdr agent read` and `herdr pane read` are the ones that matter.
        """
        argv = self._base_argv() + args
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=herdr_env(self.host),
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            raise HerdrError(
                code="herdr_failed",
                message=err_b.decode("utf-8", "replace").strip(),
                argv=argv,
                returncode=proc.returncode or -1,
            )
        return out_b.decode("utf-8", "replace")

    # --- reachability -------------------------------------------------------

    async def probe(self) -> dict[str, Any]:
        """Cheapest proof the herdr *server* is actually answering.

        A service answering is not the same as its dependency being alive, so this
        talks to the server over the socket rather than merely checking the binary.
        """
        return await self.run(["workspace", "list"], timeout=10)

    # --- reads --------------------------------------------------------------

    async def workspace_list(self) -> list[dict[str, Any]]:
        res = await self.run(["workspace", "list"], timeout=15)
        return res.get("result", {}).get("workspaces", [])

    async def pane_list(self) -> list[dict[str, Any]]:
        res = await self.run(["pane", "list"], timeout=15)
        return res.get("result", {}).get("panes", [])

    async def pane_process_info(self, pane_id: str) -> dict[str, Any]:
        res = await self.run(["pane", "process-info", "--pane", pane_id], timeout=15)
        return res.get("result", {}).get("process_info", {})

    async def agent_get(self, name: str) -> dict[str, Any]:
        res = await self.run(["agent", "get", name], timeout=15)
        return res.get("result", {}).get("agent", {})

    async def agent_read(self, name: str, lines: int = 20) -> str:
        try:
            return await self.run_text(["agent", "read", name, "--lines", str(lines)], timeout=15)
        except HerdrError:
            return ""

    # --- writes -------------------------------------------------------------

    async def workspace_create(self, label: str, cwd: str) -> dict[str, Any]:
        """Create a workspace. Returns the full result, which already carries the
        workspace, its first tab, and that tab's root pane -- so for a fresh inbox
        there is no separate `tab create` call to make."""
        res = await self.run(
            ["workspace", "create", "--label", label, "--cwd", cwd, "--no-focus"],
            timeout=30,
        )
        return res.get("result", {})

    async def tab_create(self, workspace_id: str, cwd: str, label: str) -> dict[str, Any]:
        res = await self.run(
            [
                "tab", "create",
                "--workspace", workspace_id,
                "--cwd", cwd,
                "--label", label,
                "--no-focus",
            ],
            timeout=30,
        )
        result = res.get("result", {})
        if result.get("type") != "tab_created":
            raise HerdrError(
                code="unexpected_response",
                message=f"tab create returned type={result.get('type')!r}",
                argv=["tab", "create"],
                returncode=0,
            )
        return result

    async def agent_start(
        self,
        name: str,
        pane_id: str,
        agent_args: list[str],
        kind: str = "claude",
        timeout_ms: int = 60_000,
    ) -> dict[str, Any]:
        """Start an interactive agent in an existing pane.

        Everything after `--` reaches the agent binary verbatim, execve-style.
        Returns the `agent` object -- note the fields live at .result.agent.*,
        one level deeper than you might expect.
        """
        args = [
            "agent", "start", name,
            "--kind", kind,
            "--pane", pane_id,
            "--timeout", str(timeout_ms),
        ]
        if agent_args:
            args.append("--")
            args.extend(agent_args)
        res = await self.run(args, timeout=timeout_ms / 1000 + 30)
        result = res.get("result", {})
        agent = result.get("agent", {})
        # argv comes back as free provenance -- worth keeping.
        agent["_argv"] = result.get("argv")
        return agent

    async def agent_prompt(
        self, name: str, text: str, wait: bool, timeout_ms: int = 600_000
    ) -> dict[str, Any]:
        """Submit a prompt.

        No `--until`. Passing it REPLACES the defaults (idle, done, blocked) rather
        than adding to them, which drops `done` -- and `done` is exactly the state a
        backgrounded spawn reaches. The result is a wait that never matches and
        always burns the full timeout while the agent sits there, already finished.
        """
        args = ["agent", "prompt", name, text]
        if wait:
            args += ["--wait", "--timeout", str(timeout_ms)]
        res = await self.run(args, timeout=(timeout_ms / 1000 + 30) if wait else 60)
        return res.get("result", {})

    async def tab_close(self, tab_id: str) -> None:
        await self.run(["tab", "close", tab_id], timeout=20)

    async def pane_report_metadata(self, pane_id: str, tokens: dict[str, str], ttl_ms: int = 0) -> None:
        """Badge a pane with provenance. Display-only and best-effort."""
        args = ["pane", "report-metadata", pane_id, "--source", "herdr-spawn"]
        for k, v in tokens.items():
            args += ["--token", f"{k}={v}"]
        if ttl_ms:
            args += ["--ttl-ms", str(ttl_ms)]
        try:
            await self.run(args, timeout=10)
        except HerdrError:
            pass  # never fail a spawn over a cosmetic badge
