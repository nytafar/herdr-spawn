"""Which space a spawn joins in the Herdr app.

A spawn belongs with the work it is about, not in a catch-all inbox. So it joins
the space of the folder it runs in: the repo's own space if there is one, else the
space of its *top folder* -- `notes`, `code`, or on a web host the site under
`/var/www` -- which is created on first use.

Pure functions over the layout document; no I/O here.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any


def top_folder(cwd: str, roots: tuple[str, ...]) -> str:
    """The first path component below a root: `/home/me/code/x/y` -> `/home/me/code`.

    A cwd outside every root, or a root itself, is its own top folder.
    """
    path = PurePosixPath(cwd)
    best: PurePosixPath | None = None
    for raw in roots:
        root = PurePosixPath(raw)
        if path != root and path.is_relative_to(root) and (best is None or len(root.parts) > len(best.parts)):
            best = root
    if best is None:
        return str(path)
    return str(best / path.relative_to(best).parts[0])


def new_space_id(top: str) -> str:
    """A stable id for a top folder's space, in the app's own `sp<32 hex>` shape.

    Derived rather than random so that two spawns into a folder with no space yet
    converge on one space instead of making one each: the app creates a space for
    a label it has not seen, and joins it for one it has.
    """
    return "sp" + hashlib.sha256(f"herdr-spawn:{top}".encode()).hexdigest()[:32]


def pick_space(spaces: list[dict[str, Any]], cwd: str, roots: tuple[str, ...]) -> tuple[str, bool]:
    """Return (space id, whether it exists already).

    In order:
      1. the deepest space whose folder is the cwd or one of its parents, down to
         the top folder -- so a repo with its own space wins over `code`;
      2. a space named after the top folder;
      3. a new space for the top folder.

    A space at a root itself (one opened at `~`) contains everything and so says
    nothing about where this belongs; it is never matched.
    """
    path = PurePosixPath(cwd)
    top = PurePosixPath(top_folder(cwd, roots))
    top_name = top.name

    best: tuple[int, int, str] | None = None
    for s in spaces:
        folder = s.get("folder")
        if not folder or not s.get("id"):
            continue
        f = PurePosixPath(folder)
        if not (path == f or path.is_relative_to(f)) or not (f == top or f.is_relative_to(top)):
            continue
        # Deeper wins; among equals, the one actually named after its folder.
        rank = (len(f.parts), 1 if s.get("name") == f.name else 0, str(s["id"]))
        if best is None or rank[:2] > best[:2]:
            best = rank
    if best:
        return best[2], True

    for s in spaces:
        if s.get("id") and s.get("name") and s.get("name") == top_name:
            return str(s["id"]), True

    sid = new_space_id(str(top))
    return sid, any(s.get("id") == sid for s in spaces)
