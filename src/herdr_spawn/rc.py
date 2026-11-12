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
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
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

    Pairing this with the pid makes the join pid-recycle-safe. Linux only -- macOS
    has no /proc; see `_REMOTE_POLL` for the per-platform version used over ssh.
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


def _evaluate(
    record: dict | None,
    *,
    sessions_dir: Path,
    pid: int,
    proc_start: str | None,
    expected_session_id: str | None,
    cwd: str | None,
    ceiling_s: float,
    read_transcript: bool = True,
) -> RemoteControl:
    """Turn a session record into a verdict. Shared by the local and ssh paths.

    Every early return here is a *successful spawn without a phone handle*, not a
    failure -- see `resolve`.
    """
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
    if read_transcript and cwd and session_id:
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

    return _evaluate(
        record,
        sessions_dir=sessions_dir,
        pid=pid,
        proc_start=proc_start,
        expected_session_id=expected_session_id,
        cwd=cwd,
        ceiling_s=ceiling_s,
    )


# The same poll, done on the far side of an ssh connection.
#
# It has to be one round trip, not a poll loop of them: a fresh ssh handshake every
# 200ms for 15s is both slow and rude to the remote sshd. So the loop runs remotely
# and returns once, in two labelled sections.
#
# `LC_ALL=C` is load-bearing too: under a non-English locale ps prints day and
# month names in that language ("tor. 12 nov."), which never equals the English
# string Claude Code wrote, and every good spawn reads as a recycled pid.
#
# `TZ=UTC` on the ps is load-bearing. Claude Code writes macOS `procStart` as a UTC
# `ctime` string, while `ps -o lstart=` prints local time -- so anywhere off UTC the
# two differ by the offset and the pid-recycle guard rejects every good spawn. On
# Linux the same field is clock ticks out of /proc, parsed from the last ')' because
# a process name can itself contain spaces and parens.
_REMOTE_POLL = r"""
set -u
D={sessions_dir}; P={pid}; N={iterations}
i=0
while [ "$i" -lt "$N" ]; do
  if [ -f "$D/$P.json" ] && grep -q bridgeSessionId "$D/$P.json" 2>/dev/null; then break; fi
  i=$((i+1)); sleep 0.2
done
echo "---PROCSTART---"
if [ -r "/proc/$P/stat" ]; then
  sed 's/.*) //' "/proc/$P/stat" | cut -d' ' -f20
else
  LC_ALL=C TZ=UTC ps -o lstart= -p "$P" 2>/dev/null | sed 's/  *$//'
fi
echo "---RECORD---"
# `|| true`: no record yet is the ordinary "Remote Control never connected"
# outcome. Letting cat's rc=1 escape turns that into a remote-shell error and
# buries the real reason.
cat "$D/$P.json" 2>/dev/null || true
"""

_REMOTE_TRANSCRIPT = r"""
set -u
grep -h -m1 bridge_status {transcript} 2>/dev/null || true
"""


async def resolve_remote(
    *,
    sh: Callable[..., Awaitable[str]],
    sessions_dir: Path,
    pid: int,
    expected_session_id: str | None = None,
    cwd: str | None = None,
    ceiling_s: float = POLL_CEILING_S,
) -> RemoteControl:
    """`resolve`, for a host reached over ssh. `sh` runs a snippet there."""
    script = _REMOTE_POLL.format(
        sessions_dir=shlex.quote(str(sessions_dir)),
        pid=shlex.quote(str(pid)),
        iterations=int(ceiling_s / POLL_INTERVAL_S) or 1,
    )
    try:
        out = await sh(script, timeout=ceiling_s + 30)
    except Exception as exc:  # noqa: BLE001 - any remote read failure is "no handle"
        return RemoteControl(
            session_id=expected_session_id, bridge_session_id=None, url=None, pid=pid,
            proc_start=None, version=None, available=False,
            reason=f"could not read the remote session registry: {exc}",
        )

    proc_start_raw, _, record_raw = out.partition("---RECORD---")
    proc_start = proc_start_raw.partition("---PROCSTART---")[2].strip() or None
    try:
        record = json.loads(record_raw.strip()) if record_raw.strip() else None
    except json.JSONDecodeError:
        record = None

    # The transcript lookup needs the session id, which only the record carries --
    # hence a second hop, and only when there is something to look up.
    out_rc = _evaluate(
        record, sessions_dir=sessions_dir, pid=pid, proc_start=proc_start,
        expected_session_id=expected_session_id, cwd=cwd, ceiling_s=ceiling_s,
        read_transcript=False,
    )
    if out_rc.available and cwd and out_rc.session_id:
        url = await _remote_transcript_url(sh, sessions_dir.parent, cwd, out_rc.session_id)
        if url:
            out_rc = replace(out_rc, url=url)
    return out_rc


async def _remote_transcript_url(
    sh: Callable[..., Awaitable[str]], sessions_home: Path, cwd: str, session_id: str
) -> str | None:
    """Best effort, same as the local read: the binary picks its own base url
    between localhost, staging and claude.ai, so the transcript is the authority."""
    slug = str(cwd).replace("/", "-")
    transcript = sessions_home / "projects" / slug / f"{session_id}.jsonl"
    try:
        line = await sh(_REMOTE_TRANSCRIPT.format(transcript=shlex.quote(str(transcript))), timeout=20)
        rec = json.loads(line.strip()) if line.strip() else {}
    except Exception:  # noqa: BLE001 - never fail a spawn over a cosmetic url
        return None
    return str(rec["url"]) if rec.get("subtype") == "bridge_status" and rec.get("url") else None
