"""Folder browsing for picking a cwd: what a host lets a spawn start in.

The rule is the spawn rule, applied to listing. A path is listed only if it
resolves under `cwd_allowlist` -- fully resolved, so `../..` and symlinks get no
further than they would as a spawn cwd. On top of that, browsing is narrower
than spawning in two ways:

- no dot-folders, neither shown nor descended into (`.git`, `.obsidian`, ...);
- nothing under a host's `list_deny`, for trees whose folder *names* are
  sensitive. A denied folder is left out of its parent's listing and refused as
  a path, checked before and after resolution so a symlink cannot reach it.

Local hosts read the filesystem here. A remote host resolves and lists on its
own side in one round trip, and the answer is checked again here: the far side
says where a path really goes, this side decides whether that is allowed.
"""

from __future__ import annotations

import os
import posixpath
import shlex
from pathlib import Path
from typing import Any

from .config import ConfigError, HostConfig
from .hc import Hc
from .herdr import Herdr

# A listing is for a phone screen, not an inventory.
MAX_ENTRIES = 500


def _norm(path: str) -> str:
    return posixpath.normpath(path)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


class _Rules:
    """The allowlist and deny list of one host, in both lexical and resolved form."""

    def __init__(self, host: HostConfig, local: bool):
        self.host = host
        self.local = local
        if not host.cwd_allowlist:
            raise ConfigError(f"host {host.name!r} has no cwd_allowlist configured; nothing to list")
        self.roots = self._forms(host.cwd_allowlist)
        self.denies = self._forms(host.list_deny)

    def _expand(self, p: str) -> str:
        if p == "~" or p.startswith("~/"):
            p = self.host.home + p[1:]
        return _norm(p)

    def _forms(self, paths: tuple[str, ...]) -> list[str]:
        out: list[str] = []
        for p in paths:
            lex = self._expand(p)
            out.append(lex)
            if self.local:
                res = str(Path(lex).resolve())
                if res != lex:
                    out.append(res)
        return out

    def problem(self, path: str) -> str | None:
        """Why `path` may not be listed, or None if it may."""
        root = next((r for r in self.roots if _under(path, r)), None)
        if root is None:
            return f"{path} is not under an allowed root on {self.host.name}"
        rel = path[len(root):].strip("/")
        if any(part.startswith(".") for part in rel.split("/") if part):
            return f"{path} is inside a dot-folder; those are not listed"
        if any(_under(path, d) for d in self.denies):
            return f"{path} is not listed on {self.host.name}"
        return None

    def check(self, path: str) -> None:
        why = self.problem(path)
        if why:
            raise ConfigError(why)

    def lexical(self, raw: str) -> str:
        """The requested path, made absolute and normalised but not resolved."""
        if any(ch in raw for ch in "\0\n\r"):
            raise ConfigError("path contains control characters")
        raw = raw.strip()
        if raw == "~" or raw.startswith("~/") or raw.startswith("/"):
            return self._expand(raw)
        return _norm(str(self.host._from_vault(raw)))  # noqa: SLF001 - same rule as a spawn cwd


def _driver(host: HostConfig) -> Hc | Herdr:
    return Hc(host) if host.has_hc else Herdr(host)


async def list_folders(host: HostConfig, path: str | None = None) -> dict[str, Any]:
    """With no path: the host's allowed roots and vaults. With one: its sub-folders."""
    drv = _driver(host)
    rules = _Rules(host, drv.is_local)

    if not (path or "").strip():
        return {"host": host.name, "path": None, "parent": None, "folders": _top(host, rules, drv.is_local)}

    lex = rules.lexical(path or "")
    rules.check(lex)  # refuse before touching the filesystem at all
    if drv.is_local:
        resolved, children = _list_local(lex)
    else:
        resolved, children = await _list_remote(drv, lex)
    rules.check(resolved)

    folders = []
    for name, child_resolved in sorted(children, key=lambda c: c[0].lower()):
        if name.startswith("."):
            continue
        if rules.problem(posixpath.join(resolved, name)) or rules.problem(child_resolved):
            continue
        folders.append({"name": name, "path": posixpath.join(resolved, name)})
        if len(folders) >= MAX_ENTRIES:
            break

    parent = posixpath.dirname(resolved)
    return {
        "host": host.name,
        "path": resolved,
        "parent": parent if parent != resolved and rules.problem(parent) is None else None,
        "folders": folders,
        **({"truncated": True} if len(folders) >= MAX_ENTRIES else {}),
    }


def _top(host: HostConfig, rules: _Rules, local: bool) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for root in host.cwd_allowlist:
        p = rules._expand(root)  # noqa: SLF001
        if rules.problem(p) or (local and not Path(p).is_dir()):
            continue
        out.append({"name": posixpath.basename(p) or p, "path": p, "kind": "root"})
    for name, root in sorted(host.vaults.items()):
        p = rules._expand(root)  # noqa: SLF001
        if rules.problem(p) or (local and rules.problem(str(Path(p).resolve()))):
            continue
        out.append({"name": name, "path": p, "kind": "vault"})
    return out


def _list_local(lex: str) -> tuple[str, list[tuple[str, str]]]:
    p = Path(lex)
    try:
        resolved = p.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise ConfigError(f"{lex} does not exist") from None
    if not resolved.is_dir():
        raise ConfigError(f"{lex} is not a directory")
    children: list[tuple[str, str]] = []
    try:
        entries = list(os.scandir(resolved))
    except PermissionError:
        raise ConfigError(f"{lex} is not readable") from None
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if not e.is_dir():  # follows symlinks: a link to a folder is a folder
                continue
            children.append((e.name, str(Path(e.path).resolve())))
        except OSError:
            continue
    return str(resolved), children


# Resolve on the far side, then list. `*` skips dot-entries by itself; a name
# with a tab or newline in it is skipped rather than allowed to forge a line.
_REMOTE_LIST = r"""
cd -P -- {path} 2>/dev/null || {{ echo __NO_DIR__; exit 0; }}
pwd -P
tab=$(printf '\t'); nl='
'
for d in *; do
  case "$d" in *"$tab"*|*"$nl"*) continue ;; esac
  [ -d "$d" ] || continue
  r=$(cd -P -- "$d" 2>/dev/null && pwd -P) || continue
  printf '%s\t%s\n' "$d" "$r"
done
"""


async def _list_remote(drv: Hc | Herdr, lex: str) -> tuple[str, list[tuple[str, str]]]:
    out = await drv.sh(_REMOTE_LIST.format(path=shlex.quote(lex)), timeout=30)
    lines = out.splitlines()
    if not lines or lines[0].startswith("__NO_DIR__"):
        raise ConfigError(f"{lex} does not exist on {drv.host.name}")
    children: list[tuple[str, str]] = []
    for line in lines[1:]:
        name, sep, resolved = line.partition("\t")
        if sep and name and resolved.startswith("/"):
            children.append((name, _norm(resolved)))
    return _norm(lines[0].strip()), children
