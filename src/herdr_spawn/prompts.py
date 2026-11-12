"""A prompt library: starting points for the briefs handed to spawned agents.

The library is a folder of markdown files the site keeps outside this repo
(`[service] prompts_dir`), one prompt per file. It is read on every request, so
an edit shows up at once in the web UI, in the `prompt_examples` tool and in the
MCP prompt list, without a restart.

    ---
    kind: prompt
    group: template          # template | snippet | followup
    title: Research: what exists
    description: One line, shown in lists.
    agent: claude            # optional spawn defaults
    model: opus
    effort: high
    family: Research         # optional: variants of one prompt share a family,
    variant: GitHub          #   and the web UI toggles between them
    order: 1                 #   in this order (lowest first)
    ---
    The prompt itself. Plain text; a labelled line followed by an empty line
    wherever the details of one task go.

Files without `kind: prompt` (a folder note, a README) are skipped, and so is
anything malformed: a library is edited by hand and one bad file must not hide
the rest.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("herdr-spawn.prompts")

GROUPS = ("template", "snippet", "followup")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_FILE_BYTES = 64 * 1024
MAX_FILES = 500
_SPAWN_KEYS = ("agent", "model", "effort")


@dataclass(frozen=True)
class PromptDoc:
    name: str
    group: str
    title: str
    description: str
    body: str
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    family: str | None = None
    variant: str | None = None
    order: int = 100

    def summary(self) -> dict[str, Any]:
        """Everything but the body: what a list shows."""
        return {k: v for k, v in asdict(self).items() if k != "body" and v is not None}

    def full(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Flat `key: value` frontmatter and the body. Nested YAML is not needed here."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if not sep or line[:1].isspace() or line.lstrip().startswith("#"):
            continue
        value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key.strip()] = value
    return meta, text[end + 5:]


def parse(path: Path) -> PromptDoc | None:
    name = path.stem
    if not NAME_RE.match(name):
        return None
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            log.warning("prompt %s is over %d bytes; skipped", path, MAX_FILE_BYTES)
            return None
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("could not read prompt %s: %s", path, exc)
        return None
    if meta.get("kind") != "prompt":
        return None
    group = meta.get("group", "template")
    if group not in GROUPS:
        log.warning("prompt %s has unknown group %r; skipped", path, group)
        return None
    body = body.strip("\n")
    if not body.strip():
        return None
    try:
        order = int(meta.get("order", 100))
    except ValueError:
        order = 100
    return PromptDoc(
        name=name,
        group=group,
        title=meta.get("title") or name,
        description=meta.get("description", ""),
        body=body + ("" if body.endswith(" ") else "\n"),
        **{k: (meta.get(k) or None) for k in _SPAWN_KEYS},
        family=meta.get("family") or None,
        variant=meta.get("variant") or None,
        order=order,
    )


def load(folder: Path | None) -> list[PromptDoc]:
    """Every prompt in the folder: templates first, then by family or title, then order."""
    if folder is None or not folder.is_dir():
        return []
    docs = []
    for path in sorted(folder.glob("*.md"))[:MAX_FILES]:
        if path.is_file() and (doc := parse(path)):
            docs.append(doc)
    return sorted(docs, key=lambda d: (GROUPS.index(d.group), (d.family or d.title).lower(), d.order, d.title.lower()))


def get(folder: Path | None, name: str) -> PromptDoc | None:
    if folder is None or not NAME_RE.match(name or ""):
        return None
    path = folder / f"{name}.md"
    return parse(path) if path.is_file() else None
