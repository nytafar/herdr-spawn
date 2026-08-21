"""Host registry and path validation.

Hosts are named explicitly — there is no magic "local" value. Whether a host is
reached over a local socket or an SSH-forwarded one is a transport detail in the
registry, not something the caller picks.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("~/.config/herdr-spawn/config.toml").expanduser()

# herdr's own rule for agent names, confirmed against 0.8.0.
SESSION_NAME_RE = r"^[a-z][a-z0-9_-]{0,31}$"

# `agent start --timeout` is bounded on BOTH ends: >3000 and <=300000.
# Anything outside that is a hard `invalid_agent_timeout`, checked *after* pane
# resolution, so we validate client-side or we get the wrong error back.
AGENT_START_TIMEOUT_MIN = 3001
AGENT_START_TIMEOUT_MAX = 300_000
AGENT_START_TIMEOUT_DEFAULT = 60_000


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class HostConfig:
    name: str
    enabled: bool
    transport: str  # "socket" | "ssh"
    herdr_bin: str = ""
    socket_path: str | None = None
    ssh_target: str | None = None
    ssh_identity: str | None = None
    ssh_socket_path: str | None = None  # remote herdr socket, for -L forwarding
    home: str = field(default_factory=lambda: str(Path.home()))
    claude_sessions_dir: str = ""
    claude_config: str = ""
    cwd_allowlist: tuple[str, ...] = ()
    # Where a spawn lands when the caller doesn't name a cwd. Must itself be
    # under cwd_allowlist. Keep it a well-understood sandbox (scratch), never a
    # broad home or a root dir -- a default is only as safe as what it points at.
    default_cwd: str | None = None
    protected_workspaces: tuple[str, ...] = ()
    inbox_label: str = "inbox"
    skip_permissions: bool = False
    sandbox: bool = False
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        """Fill the paths that are always derivable from the host's home.

        They hang off the *host's* home, not the machine running this service --
        a remote host may be a mac under /Users/<name>. Deriving them keeps every
        such path optional in config and keeps no particular machine's layout in
        this source file.
        """
        if not self.claude_sessions_dir:
            object.__setattr__(self, "claude_sessions_dir", str(Path(self.home) / ".claude" / "sessions"))
        if not self.claude_config:
            object.__setattr__(self, "claude_config", str(Path(self.home) / ".claude.json"))
        if not self.herdr_bin:
            # Never a bare "herdr": a systemd --user unit's PATH has no ~/.local/bin.
            object.__setattr__(self, "herdr_bin", str(Path(self.home) / ".local" / "bin" / "herdr"))

    def resolve_cwd(self, raw: str | None) -> Path:
        """Resolve `raw` (or the host default) and prove it sits under an allowed root.

        Uses `is_relative_to` on fully-resolved paths, so `~/scratch-evil` does not
        pass a `~/scratch` root the way a string prefix check would, and symlinks
        cannot walk out of an allowed tree.
        """
        candidate_raw = (raw or "").strip() or self.default_cwd
        if not candidate_raw:
            raise ConfigError(
                f"cwd is required on {self.name} (no default_cwd configured). "
                f"Allowed roots: {', '.join(self.cwd_allowlist) or '(none configured)'}"
            )
        raw = candidate_raw
        if not self.cwd_allowlist:
            raise ConfigError(f"host {self.name!r} has no cwd_allowlist configured; refusing every spawn")

        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ConfigError(f"cwd must be an absolute path, got {raw!r}")

        resolved = candidate.resolve()
        for root in self.cwd_allowlist:
            root_resolved = Path(root).expanduser().resolve()
            if resolved == root_resolved or resolved.is_relative_to(root_resolved):
                return resolved

        raise ConfigError(
            f"cwd {resolved} is not under an allowed root on {self.name}. "
            f"Allowed: {', '.join(self.cwd_allowlist)}"
        )


@dataclass(frozen=True)
class Config:
    hosts: dict[str, HostConfig]
    state_dir: Path = field(default=Path("~/.local/state/herdr-spawn").expanduser())

    def host(self, name: str) -> HostConfig:
        h = self.hosts.get(name)
        if h is None:
            known = ", ".join(sorted(self.hosts)) or "(none)"
            raise ConfigError(f"unknown host {name!r}. Known hosts: {known}")
        if not h.enabled:
            reason = h.unavailable_reason or "disabled in config"
            raise ConfigError(f"host {name!r} is not enabled: {reason}")
        return h

    @property
    def host_names(self) -> list[str]:
        return sorted(self.hosts)


def load(path: Path | str | None = None) -> Config:
    p = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        raise ConfigError(
            f"no config at {p}. Copy config.toml.example there and edit the cwd allowlist."
        )
    with p.open("rb") as fh:
        raw = tomllib.load(fh)

    service = raw.get("service", {})
    state_dir = Path(service.get("state_dir", "~/.local/state/herdr-spawn")).expanduser()

    hosts: dict[str, HostConfig] = {}
    for name, h in (raw.get("hosts") or {}).items():
        transport = h.get("transport", "socket")
        if transport not in ("socket", "ssh"):
            raise ConfigError(f"host {name!r}: transport must be 'socket' or 'ssh', got {transport!r}")
        hosts[name] = HostConfig(
            name=name,
            enabled=bool(h.get("enabled", False)),
            transport=transport,
            herdr_bin=h.get("herdr_bin", ""),
            socket_path=h.get("socket_path"),
            ssh_target=h.get("ssh_target"),
            ssh_identity=h.get("ssh_identity"),
            ssh_socket_path=h.get("ssh_socket_path"),
            home=h.get("home") or str(Path.home()),
            claude_sessions_dir=h.get("claude_sessions_dir", ""),
            claude_config=h.get("claude_config", ""),
            cwd_allowlist=tuple(h.get("cwd_allowlist", ())),
            default_cwd=h.get("default_cwd"),
            protected_workspaces=tuple(h.get("protected_workspaces", ())),
            inbox_label=h.get("inbox_label", "inbox"),
            skip_permissions=bool(h.get("skip_permissions", False)),
            sandbox=bool(h.get("sandbox", False)),
            unavailable_reason=h.get("unavailable_reason"),
        )

    if not hosts:
        raise ConfigError(f"{p} defines no [hosts.*] tables")
    return Config(hosts=hosts, state_dir=state_dir)


def herdr_env(host: HostConfig) -> dict[str, str]:
    """Environment for invoking the herdr CLI.

    HOME must be set explicitly. With a stripped environment herdr falls back to
    /tmp/herdr/herdr.sock and reports `server_not_running`, which blames the wrong
    thing entirely and sends you debugging a healthy server.
    """
    env = dict(os.environ)
    env["HOME"] = host.home
    env.setdefault("PATH", f"{host.home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    if host.socket_path:
        env["HERDR_SOCKET_PATH"] = host.socket_path
    # Never inherit an ambient pane identity into a spawn decision.
    for leaked in ("HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID", "HERDR_ENV"):
        env.pop(leaked, None)
    return env
