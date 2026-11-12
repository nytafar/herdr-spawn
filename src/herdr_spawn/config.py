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


UIS = ("hc", "herdr")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class HostConfig:
    name: str
    enabled: bool
    # How the herdr TUI is reached: "socket" | "ssh", or "hc" for a host that has
    # no herdr TUI at all and is only ever driven through herdr-core.
    transport: str
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
    # --- herdr-core (hc) ----------------------------------------------------
    # Where a spawn lands unless the caller asks otherwise: "hc" (herdr-core, so
    # the session shows up in the Herdr app and its mobile client) or "herdr" (a
    # pane in the herdr TUI).
    ui: str = "hc"
    # hc always runs on the machine this service runs on, even for a remote host:
    # the local core relays to the far one. So this path is local, unlike herdr_bin.
    hc_bin: str = ""
    # The host's name in `hc machine ls`. None means this machine.
    hc_machine: str | None = None
    # A spawn joins the space of its top folder: the first path component below
    # one of these roots. Defaults to the host's home, so ~/code/x lands in `code`.
    space_roots: tuple[str, ...] = ()
    # Which binary to start for an agent under hc, when the bare name is wrong on
    # this host -- typically a stale copy earlier on the PATH that hc's terminals
    # get. An old agent may not report its status, and then nothing can tell when
    # it has finished.
    agent_bin: dict[str, str] = field(default_factory=dict)
    # Obsidian vaults on this host, name -> root. A caller holding a note path
    # from a vault tool knows it relative to the vault, not where the vault sits
    # on disk; this lets it pass `<vault>/<path>` as the cwd.
    vaults: dict[str, str] = field(default_factory=dict)
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
        if not self.hc_bin:
            object.__setattr__(self, "hc_bin", str(Path.home() / ".local" / "bin" / "hc"))
        if not self.space_roots:
            object.__setattr__(self, "space_roots", (self.home,))

    @property
    def has_herdr(self) -> bool:
        return self.transport in ("socket", "ssh")

    @property
    def has_hc(self) -> bool:
        """An ssh host is only reachable through hc if it is also an hc machine."""
        return self.transport != "ssh" or bool(self.hc_machine)

    def resolve_ui(self, requested: str | None) -> str:
        """Which UI a spawn uses: what the caller asked for, else the host default.

        An explicit request the host cannot serve is refused rather than quietly
        swapped for the other one -- the caller asked to watch it somewhere
        specific. Only the *default* falls back.
        """
        want = (requested or "").strip().lower()
        if want and want not in UIS:
            raise ConfigError(f"ui must be one of {', '.join(UIS)}, got {requested!r}")
        if want == "herdr" and not self.has_herdr:
            raise ConfigError(f"host {self.name!r} has no herdr TUI; it is reached through hc only")
        if want == "hc" and not self.has_hc:
            raise ConfigError(f"host {self.name!r} is not an hc machine (set hc_machine); use ui=herdr")
        if want:
            return want
        if self.ui == "hc" and self.has_hc:
            return "hc"
        if self.has_herdr:
            return "herdr"
        return "hc"

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
            candidate = self._from_vault(raw)
        # A note is not somewhere an agent can start, but its folder is. `.md` is
        # judged by name because a remote host's files cannot be looked at from
        # here; anything that is a file on this machine counts too.
        if candidate.suffix.lower() == ".md" or candidate.is_file():
            candidate = candidate.parent

        # The allowlist check below runs on the resolved path, so a vault-relative
        # `../..` or a symlink gets no further than an absolute one would.
        resolved = candidate.resolve()
        for root in self.cwd_allowlist:
            root_resolved = Path(root).expanduser().resolve()
            if resolved == root_resolved or resolved.is_relative_to(root_resolved):
                return resolved

        raise ConfigError(
            f"cwd {resolved} is not under an allowed root on {self.name}. "
            f"Allowed: {', '.join(self.cwd_allowlist)}"
        )


    def _from_vault(self, raw: str) -> Path:
        """Turn `<vault>/<path>` (or, with one vault, just `<path>`) into a path."""
        if not self.vaults:
            raise ConfigError(f"cwd must be an absolute path, got {raw!r}")
        first, _, rest = raw.strip("/").partition("/")
        if first in self.vaults:
            return Path(self.vaults[first]) / rest
        if len(self.vaults) == 1:
            return Path(next(iter(self.vaults.values()))) / raw.strip("/")
        raise ConfigError(
            f"relative cwd {raw!r} must start with a vault name on {self.name}: "
            f"{', '.join(sorted(self.vaults))}"
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
        if transport not in ("socket", "ssh", "hc"):
            raise ConfigError(
                f"host {name!r}: transport must be 'socket', 'ssh' or 'hc', got {transport!r}"
            )
        ui = h.get("ui", "hc")
        if ui not in UIS:
            raise ConfigError(f"host {name!r}: ui must be 'hc' or 'herdr', got {ui!r}")
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
            ui=ui,
            hc_bin=h.get("hc_bin", ""),
            hc_machine=h.get("hc_machine"),
            space_roots=tuple(h.get("space_roots", ())),
            agent_bin={str(k): str(v) for k, v in (h.get("agent_bin") or {}).items()},
            vaults={str(k): str(v) for k, v in (h.get("vaults") or {}).items()},
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
