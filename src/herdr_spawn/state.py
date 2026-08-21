"""Durable record of what was spawned, and the guard that keeps teardown honest.

Both upstream sources are ephemeral, in different ways:

  * Claude Code deletes `<sessions_dir>/<pid>.json` the moment the process exits,
    taking the Remote Control url with it.
  * herdr agent names are explicitly not durable -- a name follows the current pane
    occupant and is cleared when that agent exits, is released, or is replaced.

So without our own record there is no answering "what did I spawn yesterday, and
did it land". The file is also what backs the teardown guard: we only ever close a
tab whose id we wrote here ourselves.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SpawnRecord:
    session_name: str
    host: str
    workspace_id: str
    tab_id: str
    pane_id: str
    cwd: str
    label: str
    created_at: str = field(default_factory=_now)
    pid: int | None = None
    proc_start: str | None = None
    session_id: str | None = None
    bridge_session_id: str | None = None
    url: str | None = None
    claude_version: str | None = None
    argv: list[str] | None = None
    prompt_sha256: str | None = None
    caller: str | None = None
    rc_available: bool = False
    closed_at: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


class Store:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).expanduser()
        self.path = self.state_dir / "spawns.jsonl"

    def _ensure(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass

    def append(self, rec: SpawnRecord) -> None:
        self._ensure()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(rec.to_json() + "\n")

    def all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def latest_by_tab(self, host: str, tab_id: str) -> dict[str, Any] | None:
        match = [r for r in self.all() if r.get("host") == host and r.get("tab_id") == tab_id]
        return match[-1] if match else None

    def owns_tab(self, host: str, tab_id: str) -> bool:
        """True only if this service created that tab itself."""
        return self.latest_by_tab(host, tab_id) is not None

    def mark_closed(self, host: str, tab_id: str) -> None:
        rec = self.latest_by_tab(host, tab_id)
        if rec is None:
            return
        rec = dict(rec)
        rec["closed_at"] = _now()
        self._ensure()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def active_names(self, host: str) -> set[str]:
        seen: dict[str, dict[str, Any]] = {}
        for r in self.all():
            if r.get("host") == host and r.get("session_name"):
                seen[r["session_name"]] = r
        return {n for n, r in seen.items() if not r.get("closed_at")}


class TeardownGuard(Exception):
    pass


def assert_closable(
    *,
    store: Store,
    host: str,
    tab_id: str,
    workspace_id: str,
    protected_workspaces: tuple[str, ...],
) -> None:
    """Refuse to close anything we did not create.

    Two belts. herdr's `workspace close` takes a bare id with no confirmation and
    no --force, and the live workspaces on this host sit right next to anything we
    make -- one mistyped id destroys real work. This service never closes a
    workspace at all; it closes tabs, and only its own.
    """
    if workspace_id in protected_workspaces:
        raise TeardownGuard(
            f"refusing to touch workspace {workspace_id} on {host}: it is on the protected list"
        )
    if not store.owns_tab(host, tab_id):
        raise TeardownGuard(
            f"refusing to close tab {tab_id} on {host}: herdr-spawn has no record of creating it"
        )
