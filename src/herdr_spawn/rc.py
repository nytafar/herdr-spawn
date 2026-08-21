"""Resolve the claude.ai Remote Control session id.

This replaces the pane-scrape the PRD originally specified. Claude Code writes the
id to its own session registry at `<sessions_dir>/<pid>.json` as `bridgeSessionId`,
and it lands there *before* `herdr agent start` returns. So there is no race to
lose, no regex to get wrong, and no dependence on pane width -- the scrape breaks
below roughly 123 columns because Ink hard-breaks the banner line.

This does depend on undocumented internals. Every spawn records the observed
`version`; a change there is the early warning that a Claude Code upgrade moved
the schema.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

# The validator inside the Claude Code binary. Note `cse_` as well as `session_`,
# and `_`/`-` inside the body -- a narrower pattern matches every id seen so far
# and then fails in production.
RC_ID_RE = re.compile(r"^(cse_|session_)[A-Za-z0-9_-]{1,128}$")

DEFAULT_BASE_URL = "https://claude.ai/code"

POLL_INTERVAL_S = 0.2
POLL_CEILING_S = 15.0


@dataclass
class RemoteControl:
    session_id: str | None  # Claude Code's local session UUID
    bridge_session_id: str | None  # the claude.ai RC handle
    url: str | None
    pid: int | None
    proc_start: str | None
    version: str | None
    available: bool
    reason: str | None = None


def _proc_start_ticks(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat -- the process start time in clock ticks.

    Pairing this with the pid makes the join pid-recycle-safe. Linux only; macOS
    has no /proc, so a mac host needs a different check entirely.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm can contain spaces and parens, so parse from the last ')'.
    tail = raw[raw.rfind(")") + 2 :].split()
    try:
        return tail[19]
    except IndexError:
        return None


def _transcript_url(sessions_home: Path, cwd: str, session_id: str) -> str | None:
    """Best-effort: pull the fully-formed url out of the transcript.

    The binary picks its base between localhost:4000, a staging host and claude.ai,
    so a locally concatenated url can be wrong. The transcript carries the real one
    in a `bridge_status` record.
    """
    slug = str(cwd).replace("/", "-")
    transcript = sessions_home / "projects" / slug / f"{session_id}.jsonl"
    try:
        with transcript.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "bridge_status" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("subtype") == "bridge_status" and rec.get("url"):
                    return str(rec["url"])
    except OSError:
        return None
    return None


def _read_record(sessions_dir: Path, pid: int) -> dict | None:
    try:
        return json.loads((sessions_dir / f"{pid}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


async def resolve(
    *,
    sessions_dir: Path,
    pid: int,
    expected_session_id: str | None = None,
    cwd: str | None = None,
    ceiling_s: float = POLL_CEILING_S,
) -> RemoteControl:
    """Poll the registry until `bridgeSessionId` appears, or give up cleanly.

    Giving up is a normal outcome, not a failure: `bridgeSessionId` is written only
    when the bridge actually connects, so an offline, unauthenticated or
    rate-limited spawn simply never gets one. The pane is still a working local
    Claude Code session, so the spawn succeeded -- it just has no phone handle.
    """
    proc_start = _proc_start_ticks(pid)
    deadline = asyncio.get_running_loop().time() + ceiling_s
    record: dict | None = None

    while True:
        record = _read_record(sessions_dir, pid)
        if record and record.get("bridgeSessionId"):
            break
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(POLL_INTERVAL_S)

    if not record:
        return RemoteControl(
            session_id=expected_session_id,
            bridge_session_id=None,
            url=None,
            pid=pid,
            proc_start=proc_start,
            version=None,
            available=False,
            reason=f"no session record at {sessions_dir}/{pid}.json after {ceiling_s:g}s",
        )

    version = record.get("version")
    session_id = record.get("sessionId") or expected_session_id

    # Guard against a recycled pid pointing us at somebody else's session.
    rec_start = record.get("procStart")
    if proc_start and rec_start and str(rec_start) != str(proc_start):
        return RemoteControl(
            session_id=expected_session_id,
            bridge_session_id=None,
            url=None,
            pid=pid,
            proc_start=proc_start,
            version=version,
            available=False,
            reason=f"session record procStart {rec_start} != live process {proc_start} (pid reused)",
        )

    if expected_session_id and session_id and session_id != expected_session_id:
        return RemoteControl(
            session_id=expected_session_id,
            bridge_session_id=None,
            url=None,
            pid=pid,
            proc_start=proc_start,
            version=version,
            available=False,
            reason=f"session record is for {session_id}, expected {expected_session_id}",
        )

    bridge = record.get("bridgeSessionId")
    if not bridge:
        return RemoteControl(
            session_id=session_id,
            bridge_session_id=None,
            url=None,
            pid=pid,
            proc_start=proc_start,
            version=version,
            available=False,
            reason=(
                f"Remote Control did not connect within {ceiling_s:g}s "
                "(offline, unauthenticated or rate-limited). The session is running and usable locally."
            ),
        )

    if not RC_ID_RE.match(bridge):
        return RemoteControl(
            session_id=session_id,
            bridge_session_id=None,
            url=None,
            pid=pid,
            proc_start=proc_start,
            version=version,
            available=False,
            reason=f"bridgeSessionId {bridge!r} failed validation",
        )

    url = None
    if cwd and session_id:
        url = _transcript_url(sessions_dir.parent, cwd, session_id)
    if not url:
        url = f"{DEFAULT_BASE_URL}/{bridge}"

    return RemoteControl(
        session_id=session_id,
        bridge_session_id=bridge,
        url=url,
        pid=pid,
        proc_start=proc_start,
        version=version,
        available=True,
    )
