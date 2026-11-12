"""The prompt library: parsing, the tool's answers, MCP prompts and the API."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from herdr_spawn import config as cfg
from herdr_spawn import prompts, web
from herdr_spawn.config import ConfigError, HostConfig
from herdr_spawn.server import build_server, op_prompt_examples

TOKEN = "t" * 40


def _write(folder: Path, name: str, meta: str, body: str = "Do the thing.\n\nDetails:\n") -> None:
    (folder / f"{name}.md").write_text(f"---\n{meta}\n---\n{body}")


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    d.mkdir()
    _write(d, "research", "kind: prompt\ngroup: template\ntitle: Research\ndescription: Survey it.\nmodel: opus\neffort: high")
    _write(d, "stop-and-ask", "kind: prompt\ngroup: snippet\ntitle: \"Stop and ask\"\ndescription: Stop.", "Stop and ask.\n")
    _write(d, "go-ahead", "kind: prompt\ngroup: followup\ntitle: Go ahead\ndescription: x", "Go ahead, ")
    _write(d, "prompts", "kind: note\ntitle: Folder note")              # not a prompt
    _write(d, "weird", "kind: prompt\ngroup: nonsense\ntitle: Weird")   # unknown group
    _write(d, "Bad_Name", "kind: prompt\ngroup: template\ntitle: Bad")   # name not allowed
    (d / "empty.md").write_text("---\nkind: prompt\n---\n\n")
    return d


def _conf(tmp_path: Path, lib: Path | None) -> cfg.Config:
    h = HostConfig(name="test", enabled=True, transport="socket", cwd_allowlist=(str(tmp_path),))
    return cfg.Config(hosts={"test": h}, state_dir=tmp_path / "state", prompts_dir=lib)


def test_library_keeps_only_valid_prompts_templates_first(lib):
    docs = prompts.load(lib)
    assert [(d.name, d.group) for d in docs] == [
        ("research", "template"), ("stop-and-ask", "snippet"), ("go-ahead", "followup")]
    r = docs[0]
    assert (r.title, r.model, r.effort, r.agent) == ("Research", "opus", "high", None)
    assert r.body == "Do the thing.\n\nDetails:\n"
    assert docs[1].title == "Stop and ask"
    assert docs[2].body == "Go ahead, "  # a prefill to keep typing after


def test_get_refuses_names_that_could_leave_the_folder(lib, tmp_path):
    (tmp_path / "secret.md").write_text("---\nkind: prompt\n---\nleak\n")
    for bad in ("../secret", "..", "/etc/passwd", "", "Research"):
        assert prompts.get(lib, bad) is None
    assert prompts.get(lib, "research").name == "research"


def test_tool_index_has_no_bodies_and_a_name_gives_the_text(tmp_path, lib):
    conf = _conf(tmp_path, lib)
    index = op_prompt_examples(conf)
    assert [p["name"] for p in index["prompts"]] == ["research", "stop-and-ask", "go-ahead"]
    assert all("body" not in p for p in index["prompts"])
    assert op_prompt_examples(conf, name="research")["body"].startswith("Do the thing.")
    assert op_prompt_examples(conf, name="nope")["status"] == "refused"
    assert op_prompt_examples(_conf(tmp_path, None))["status"] == "refused"


def test_mcp_lists_templates_live_and_serves_their_text(tmp_path, lib):
    server = build_server(_conf(tmp_path, lib))
    names = [p.name for p in asyncio.run(server.list_prompts())]
    assert names == ["research"]
    _write(lib, "review", "kind: prompt\ngroup: template\ntitle: Review")
    assert [p.name for p in asyncio.run(server.list_prompts())] == ["research", "review"]
    got = asyncio.run(server.get_prompt("research"))
    assert got.messages[0].content.text.startswith("Do the thing.")
    with pytest.raises(ValueError):
        asyncio.run(server.get_prompt("stop-and-ask"))  # snippets are not MCP prompts


def test_tool_only_exists_with_a_library(tmp_path, lib):
    def tools(conf):
        return set(build_server(conf)._tool_manager._tools)  # noqa: SLF001
    assert "prompt_examples" in tools(_conf(tmp_path, lib))
    assert "prompt_examples" not in tools(_conf(tmp_path, None))


def test_api_serves_the_library_with_bodies_behind_the_token(tmp_path, lib):
    client = TestClient(web.build_web_app(_conf(tmp_path, lib), {}, TOKEN.encode()))
    assert client.get("/api/prompts").status_code == 401
    r = client.get("/api/prompts", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()["prompts"][0]["body"].startswith("Do the thing.")
    empty = TestClient(web.build_web_app(_conf(tmp_path, None), {}, TOKEN.encode()))
    assert empty.get("/api/prompts", headers={"Authorization": f"Bearer {TOKEN}"}).json() == {"prompts": []}


def test_prompts_dir_is_read_from_config_and_must_exist(tmp_path, lib):
    p = tmp_path / "config.toml"
    p.write_text(f'[service]\nprompts_dir = "{lib}"\n[hosts.x]\nenabled = true\ncwd_allowlist = ["/a"]\n')
    assert cfg.load(p).prompts_dir == lib
    p.write_text(f'[service]\nprompts_dir = "{tmp_path / "missing"}"\n[hosts.x]\ncwd_allowlist = ["/a"]\n')
    with pytest.raises(ConfigError, match="prompts_dir"):
        cfg.load(p)


def test_variants_group_by_family_in_their_order(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    _write(d, "research-web", "kind: prompt\nfamily: Research\nvariant: Web\norder: 2\ntitle: Research (Web)")
    _write(d, "research-github", "kind: prompt\nfamily: Research\nvariant: GitHub\norder: 1\ntitle: Research (GitHub)\ndescription: with /github-explore: repos")
    _write(d, "review", "kind: prompt\ntitle: Review")
    _write(d, "odd-order", "kind: prompt\ntitle: Zed\norder: soon")
    docs = prompts.load(d)
    assert [x.name for x in docs] == ["research-github", "research-web", "review", "odd-order"]
    gh = docs[0]
    assert (gh.family, gh.variant, gh.order) == ("Research", "GitHub", 1)
    assert gh.description == "with /github-explore: repos"  # colons after the first are kept
    assert docs[3].order == 100
    titles = [p.title for p in asyncio.run(build_server(_conf(tmp_path, d)).list_prompts())]
    assert titles[:2] == ["Research: GitHub", "Research: Web"]
