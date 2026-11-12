"""What differs between one coding agent and the next, and what is available where.

herdr drives all of its supported agents identically -- start one in a pane, submit
a prompt, read the state back. This module is the part it does *not* normalise:

  * the flags each binary takes (they are not interchangeable, and the
    skip-permissions flag in particular must never be guessed),
  * where each one records that a directory is trusted,
  * which of them can hand back a claude.ai Remote Control link. Exactly one can.

Nothing here is a list of *your* machines. Which of these are actually installed is
discovered per host at startup, not declared.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .herdr import Herdr, HerdrError

log = logging.getLogger("herdr-spawn")

DEFAULT_AGENT = "claude"

# `herdr agent start --help` prints its supported kinds as a clap enum. Parsing that
# rather than hardcoding a list is deliberate: the set grows between herdr releases
# (0.8.2 added Qwen Code detection), and a stale hardcoded copy would advertise
# agents this herdr cannot start, or hide ones it can.
KIND_ENUM_RE = re.compile(r"\[possible values:\s*([^\]]+)\]")

# A non-interactive ssh PATH is famously short, and every one of these agents
# installs somewhere that is not on it. This is the same trap the herdr binary
# itself hit: a probe that reports "not installed" for something plainly present.
_PATH_PREFIX = 'PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"'


@dataclass(frozen=True)
class AgentProfile:
    """How to launch one kind, and what it can do once launched."""

    kind: str
    # Only Claude Code has a claude.ai bridge. For everyone else there is no
    # session registry, no bridgeSessionId, and nothing to link to -- so the spawn
    # returns pane coordinates and you continue in the terminal.
    remote_control: bool = False
    skip_permissions_flag: str | None = None
    sandbox_flag: str | None = None
    # Which trust store to preflight against, if any. An untrusted directory does
    # not fail -- the agent opens and sits on a dialog forever, looking alive.
    trust: str | None = None
    # How this binary takes a model and an effort override; `{}` is the value.
    # Empty means unknown, and an override is then refused rather than dropped:
    # a spawn that silently ran on the default model is the worse surprise.
    model_args: tuple[str, ...] = ()
    effort_args: tuple[str, ...] = ()
    # Whether the first prompt can ride along as the last argument. It is the
    # reliable delivery under hc -- text typed at an agent that is still booting
    # can be dropped -- so it is used wherever the binary is known to take one.
    prompt_as_arg: bool = False
    # Extra flags needed only when the agent runs in an hc terminal.
    hc_args: tuple[str, ...] = ()


PROFILES: dict[str, AgentProfile] = {
    "claude": AgentProfile(
        kind="claude",
        remote_control=True,
        skip_permissions_flag="--dangerously-skip-permissions",
        sandbox_flag="--sandbox",
        trust="claude",
        model_args=("--model", "{}"),
        effort_args=("--effort", "{}"),
        prompt_as_arg=True,
    ),
    "codex": AgentProfile(
        kind="codex",
        # Verified against the installed binary's --help, not assumed. Getting a
        # flag this dangerous wrong is not the kind of mistake you find later.
        skip_permissions_flag="--dangerously-bypass-approvals-and-sandbox",
        trust="codex",
        model_args=("--model", "{}"),
        effort_args=("-c", "model_reasoning_effort={}"),
        prompt_as_arg=True,
        # Without it Codex hands its commands to a daemon outside the terminal,
        # where hc can neither show nor stop them.
        hc_args=("--no-daemon",),
    ),
}


def profile(kind: str) -> AgentProfile:
    """Known kinds get their quirks; the rest get the neutral default.

    An unknown kind is still perfectly startable -- herdr knows how to drive it and
    we simply pass no flags. That beats refusing every agent nobody has written a
    profile for yet.
    """
    return PROFILES.get(kind) or AgentProfile(kind=kind)


@dataclass(frozen=True)
class HostAgents:
    """What one host can actually start, as observed rather than declared."""

    kinds: tuple[str, ...] = ()  # what this herdr supports
    installed: tuple[str, ...] = ()  # what is on PATH there
    available: tuple[str, ...] = ()  # the intersection: what will actually run
    # Set when discovery could not run (host asleep, ssh down). Availability is
    # then *unknown*, which is not the same as empty -- see `permits`.
    error: str | None = None

    @property
    def known(self) -> bool:
        return self.error is None

    def permits(self, kind: str) -> bool:
        """Undiscovered hosts permit everything, on purpose.

        A laptop that happened to be closed when the service started must not have
        its agents hidden until the next restart. Let the spawn through and let the
        real error come back from the real attempt.
        """
        return not self.known or kind in self.available

    def refusal(self, kind: str, host_name: str) -> str:
        if kind not in self.kinds and self.kinds:
            return (
                f"herdr on {host_name} cannot start {kind!r}. "
                f"It supports: {', '.join(sorted(self.kinds))}"
            )
        return (
            f"{kind!r} is not installed on {host_name}. "
            f"Available there: {', '.join(self.available) or '(none discovered)'}"
        )


async def discover(h: Herdr) -> HostAgents:
    """Ask a host what it can start. One `--help` parse and one PATH sweep.

    Runs at startup and is cached: this is two round trips per host, which is fine
    once and wasteful per request. The cost that actually matters is the advertised
    schema, and none of this appears there -- only the union of what came back.
    """
    try:
        helptext = await h.run_text(["agent", "start", "--help"], timeout=30)
    except (HerdrError, OSError, TimeoutError) as exc:
        return HostAgents(error=f"could not read supported kinds: {exc}")

    match = KIND_ENUM_RE.search(helptext)
    if not match:
        return HostAgents(error="could not parse supported kinds from `agent start --help`")
    kinds = tuple(k.strip() for k in match.group(1).split(",") if k.strip())

    # The trailing `exit 0` is load-bearing: the loop's status is the LAST
    # `command -v`, so a sweep whose final kind happens not to be installed exits
    # 127 and the whole probe reads as a shell failure rather than a clean answer.
    sweep = (
        f'{_PATH_PREFIX}\n'
        f'for k in {" ".join(kinds)}; do command -v "$k" >/dev/null 2>&1 && echo "$k"; done\n'
        "exit 0\n"
    )
    try:
        out = await h.sh(sweep, timeout=45)
    except (HerdrError, OSError, TimeoutError) as exc:
        return HostAgents(kinds=kinds, error=f"could not probe installed agents: {exc}")

    installed = tuple(line.strip() for line in out.splitlines() if line.strip())
    available = tuple(k for k in kinds if k in installed)
    log.info("discovered on %s: %d supported, %d available (%s)",
             h.host.name, len(kinds), len(available), ", ".join(available) or "none")
    return HostAgents(kinds=kinds, installed=installed, available=available)


# With no herdr to ask, these are the binaries worth looking for on an hc-only
# host. Anything else there is still startable -- hc runs any program -- it just
# is not advertised.
HC_KINDS = ("claude", "codex", "opencode", "pi", "gemini", "amp")


async def discover_hc(hc) -> HostAgents:  # noqa: ANN001 - Hc; imported lazily to avoid a cycle
    """`discover`, for a host that has no herdr TUI: only the PATH sweep."""
    sweep = (
        f'{_PATH_PREFIX}\n'
        f'for k in {" ".join(HC_KINDS)}; do command -v "$k" >/dev/null 2>&1 && echo "$k"; done\n'
        "exit 0\n"
    )
    try:
        out = await hc.sh(sweep, timeout=45)
    except (HerdrError, OSError, TimeoutError) as exc:
        return HostAgents(error=f"could not probe installed agents: {exc}")
    installed = tuple(line.strip() for line in out.splitlines() if line.strip())
    log.info("discovered on %s via hc: %s", hc.host.name, ", ".join(installed) or "none")
    return HostAgents(kinds=HC_KINDS, installed=installed, available=installed)


def advertised(by_host: dict[str, HostAgents], enabled: list[str]) -> list[str]:
    """The `agent` enum: the union of what enabled hosts can actually start.

    Deliberately not all 21 kinds herdr supports. This definition is paid for on
    every request forever, and advertising agents nobody has installed costs tokens
    on every one of them to describe something that can only ever be refused. The
    union is truthful, and it shrinks and grows with the machines.

    `claude` is always in it: it is the default, and a host that failed discovery
    must still be callable.
    """
    out = {DEFAULT_AGENT}
    for name in enabled:
        caps = by_host.get(name)
        if caps and caps.known:
            out.update(caps.available)
    return sorted(out)
