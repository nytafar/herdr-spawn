"""Thin wrapper over `hc`, the herdr-core CLI.

herdr-core runs programs in terminals that outlive whatever started them, and the
Herdr app and its mobile client show the ones labelled into a space. That is why
it is the default landing place: a spawn started from a chat can be followed from
a phone, which a pane in the herdr TUI cannot.

Same reasoning as herdr.py for shelling out: the CLI absorbs protocol drift. The
one exception is the layout document, which has no CLI verb and is read through
`hc connect` (the JSON-RPC API over stdin/stdout).

Conventions, simpler than herdr's:

    rc=0   -> JSON on stdout (`read` and `exec` print text)
    rc!=0  -> {"error": {"kind", "message", "hint"}} on stderr

hc always runs on this machine. A remote host is an hc *machine*: the local core
relays, and the terminal's address comes back as `machine/t7`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .config import HostConfig
from .herdr import ERR_TIMEOUT, ERR_UNAVAILABLE, HerdrError

ERR_HC_NAME_TAKEN = "name_taken"
ERR_HC_BLOCKED = "blocked"
ERR_HC_NOT_FOUND = "not_found"
ERR_HC_FINISHED = "finished"

# The app's layout document, one per machine's core.
LAYOUT_DOC = "herdr-app"


class Hc:
    """Invokes the hc CLI for one host."""

    def __init__(self, host: HostConfig):
        self.host = host

    @property
    def machine(self) -> str | None:
        return self.host.hc_machine

    @property
    def is_local(self) -> bool:
        """Whether the host's filesystem is this machine's."""
        return not self.machine

    def _on(self) -> list[str]:
        return ["--on", self.machine] if self.machine else []

    async def _exec(
        self, args: list[str], timeout: float | None, stdin: bytes | None = None
    ) -> tuple[int, str, str]:
        argv = [self.host.hc_bin, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise HerdrError(
                code=ERR_UNAVAILABLE,
                message=f"{self.host.hc_bin} not found while reaching {self.host.name}",
                argv=argv,
                returncode=-1,
            ) from exc
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise HerdrError(
                code=ERR_TIMEOUT,
                message=f"hc {' '.join(args[:1])} exceeded {timeout}s locally",
                argv=argv,
                returncode=-1,
            ) from None
        return (
            proc.returncode if proc.returncode is not None else -1,
            out_b.decode("utf-8", "replace"),
            err_b.decode("utf-8", "replace"),
        )

    def _error(self, args: list[str], rc: int, out: str, err: str) -> HerdrError:
        code, message = "hc_failed", err.strip() or out.strip() or "no output"
        try:
            payload = json.loads(err)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            e = payload["error"]
            code = e.get("kind") or code
            message = e.get("message") or message
            if e.get("hint"):
                message = f"{message} ({e['hint']})"
        if code in ("unavailable", "machine_unknown"):
            code = ERR_UNAVAILABLE
        return HerdrError(
            code=code, message=message, argv=[self.host.hc_bin, *args], returncode=rc, raw=err or out
        )

    async def run(self, args: list[str], timeout: float | None = 30) -> dict[str, Any]:
        """Run an hc subcommand and return its parsed JSON."""
        rc, out, err = await self._exec(args, timeout)
        if rc != 0:
            raise self._error(args, rc, out, err)
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise HerdrError(
                code="unparseable_output",
                message=f"hc returned rc=0 but stdout was not JSON: {out[:400]}",
                argv=[self.host.hc_bin, *args],
                returncode=0,
                raw=out,
            ) from exc

    async def sh(self, script: str, timeout: float = 30) -> str:
        """Run a small shell snippet on the host and return stdout.

        Same contract as `Herdr.sh`: only for reads with no hc verb, and `script`
        is composed by this package, never from tool arguments. `hc exec` passes
        the program's own exit code through, so a nonzero one is the script's.
        """
        if self.is_local:
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", script,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            rc, out, err = proc.returncode or 0, out_b.decode("utf-8", "replace"), err_b.decode("utf-8", "replace")
        else:
            rc, out, err = await self._exec(
                ["exec", *self._on(), "--", "sh", "-c", script], timeout
            )
        if rc != 0:
            raise HerdrError(
                code="remote_shell_failed",
                message=err.strip() or f"rc={rc}",
                argv=["sh", "-c", script],
                returncode=rc,
            )
        return out

    async def rpc(self, method: str, params: dict[str, Any], timeout: float = 20) -> Any:
        """One JSON-RPC call through `hc connect`, for what the CLI has no verb for."""
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n"
        rc, out, err = await self._exec(["connect"], timeout, stdin=req.encode())
        for line in out.splitlines():
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict) or msg.get("id") != 1:
                continue
            if "error" in msg:
                e = msg["error"] or {}
                kind = (e.get("data") or {}).get("kind") or "hc_failed"
                raise HerdrError(
                    code=kind, message=str(e.get("message") or kind),
                    argv=["connect", method], returncode=rc,
                )
            return msg.get("result")
        raise HerdrError(
            code=ERR_UNAVAILABLE,
            message=f"no reply to {method} from hc connect: {err.strip() or out.strip()[:200] or 'empty'}",
            argv=["connect", method],
            returncode=rc,
        )

    # --- reachability -------------------------------------------------------

    async def probe(self) -> None:
        """Prove the core answers -- and, for a remote host, that its line is up.

        `server status` never starts a core, so a stopped one is reported rather
        than quietly launched from a service that has no business owning it.
        """
        await self.run(["server", "status"], timeout=10)
        if not self.machine:
            return
        res = await self.run(["machine", "ls"], timeout=15)
        for m in res.get("machines", []):
            if m.get("name") == self.machine:
                if m.get("state") == "connected":
                    return
                raise HerdrError(
                    code=ERR_UNAVAILABLE,
                    message=f"hc machine {self.machine!r} is {m.get('state')}",
                    argv=["machine", "ls"], returncode=0,
                )
        raise HerdrError(
            code=ERR_UNAVAILABLE,
            message=f"hc knows no machine named {self.machine!r} (see `hc machine ls`)",
            argv=["machine", "ls"], returncode=0,
        )

    # --- reads --------------------------------------------------------------

    async def spaces(self) -> list[dict[str, Any]]:
        """The spaces in this host's layout, as the app last wrote them."""
        params: dict[str, Any] = {"name": LAYOUT_DOC}
        if self.machine:
            params["on"] = self.machine
        res = await self.rpc("doc.get", params)
        value = (res or {}).get("value") or {}
        spaces = value.get("spaces") if isinstance(value, dict) else None
        return [s for s in spaces or [] if isinstance(s, dict)]

    async def ls(self) -> list[dict[str, Any]]:
        """Live terminals on this host only. `hc ls` lists every machine's."""
        res = await self.run(["ls"], timeout=20)
        out = []
        for t in res.get("terminals", []):
            machine, sep, _ = str(t.get("address", "")).rpartition("/")
            if (machine if sep else None) == self.machine:
                out.append(t)
        return out

    async def get(self, terminal: str) -> dict[str, Any]:
        return await self.run(["get", terminal], timeout=15)

    async def read(self, terminal: str, lines: int = 20) -> str:
        try:
            rc, out, _ = await self._exec(["read", terminal, "--lines", str(lines)], 20)
        except HerdrError:
            return ""
        return out if rc == 0 else ""

    # --- writes -------------------------------------------------------------

    async def start(
        self, name: str, cwd: str, argv: list[str], labels: dict[str, str]
    ) -> dict[str, Any]:
        """Start a program in a new terminal and come back at once.

        `--no-parent` always: started from inside a terminal (stdio mode in a
        Claude Code session), the spawn would otherwise be that terminal's child
        and end when it does. `--` keeps argv verbatim, so a prompt that is one
        long multi-line string stays one argument instead of becoming a shell line.
        """
        args = ["run", "--bg", "--json", "--no-parent", "--name", name, "--cwd", cwd, *self._on()]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        return await self.run([*args, "--", *argv], timeout=90)

    async def send(self, terminal: str, text: str, wait: bool = False, timeout_s: int = 600) -> dict[str, Any]:
        args = ["send", "--json", terminal, text]
        if wait:
            args += ["--wait", "--timeout", str(timeout_s)]
        return await self.run(args, timeout=(timeout_s + 30) if wait else 60)

    async def wait(self, terminal: str, timeout_s: int = 600) -> dict[str, Any]:
        return await self.run(["wait", "--json", terminal, "--timeout", str(timeout_s)], timeout=timeout_s + 30)
