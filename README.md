# herdr-spawn

Hand a prompt you composed in a chat to a **real Claude Code session** on one of your
own machines, with Remote Control on, and get back a link that opens it in the Code tab.

One tool. `spawn_claude`.

The chat does what a chat is good at — thinking the task through with you — and then
the actual work happens in a real terminal, on a real checkout, with your real
credentials, on the machine that has them. You keep the conversation; the agent keeps
the filesystem.

Built on [herdr](https://herdr.dev), a terminal workspace manager for AI coding agents,
which supplies the workspaces, tabs, panes and the `agent start` / `agent prompt` verbs
this drives.

---

## What it does

```
spawn_claude(host, prompt, [cwd], [label], [session_name], [workspace], [wait])
  -> {status, session_name, workspace_id, tab_id, pane_id, cwd,
      session_id, remote_url, agent_status}
```

1. **Preflight** — host enabled, `cwd` resolves under an allowed root, `cwd` already
   trusted by Claude Code, herdr answering on that host.
2. Find or create the `inbox` workspace, `--no-focus`.
3. Create a tab there and start `claude --remote-control <session_name>` in its pane.
4. Resolve the claude.ai Remote Control id.
5. Submit the prompt, under a per-pane lock.
6. Append a record to `~/.local/state/herdr-spawn/spawns.jsonl`.

A spawn never steals focus. Nothing is ever closed that this service did not create.

Prompts over 2 000 characters are written to `<cwd>/.herdr-handoff/<session>.md` and
handed over as an `@`-reference instead — terminal injection of multi-kilobyte text is
fragile, and the file doubles as a record of what was actually asked.

---

## Install

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/), herdr, and Claude Code.

```sh
git clone https://github.com/nytafar/herdr-spawn.git ~/mcp/herdr-spawn
cd ~/mcp/herdr-spawn
uv sync

mkdir -p ~/.config/herdr-spawn
cp config.toml.example ~/.config/herdr-spawn/config.toml
$EDITOR ~/.config/herdr-spawn/config.toml      # at minimum: paths + cwd_allowlist
```

`~/.config/herdr-spawn/config.toml` is the **only** place your machine names, addresses
and paths live. It sits outside the repo on purpose, and the repo's `.gitignore` also
refuses a stray copy of it.

Then either use it locally over stdio:

```sh
claude mcp add herdr-spawn -- uv run --directory ~/mcp/herdr-spawn python -m herdr_spawn.server
```

or run it as a service and put it behind a gateway — see below.

### Before a host will work

Two things must already be true on any host you enable, and neither is something this
service can do for you:

- **herdr is running there.** `herdr workspace list` must print JSON.
- **the cwd is trusted by Claude Code.** Start Claude there once by hand and accept the
  trust dialog. Otherwise the spawned pane just sits on that dialog forever, looking
  alive and doing nothing. Preflight checks this and refuses rather than letting it
  happen.

### Remote hosts, over ssh

A `transport = "ssh"` host runs the herdr CLI on the far side. There is no socket
forwarding and no agent installed over there — if this works, so does everything else:

```sh
ssh -o BatchMode=yes <ssh_target> /path/to/herdr workspace list
```

`BatchMode=yes` is the real test: authentication must be non-interactive, because
nothing here can answer a passphrase or a host-key prompt. Point `ssh_target` at a
`Host` alias from your `~/.ssh/config` and let ssh resolve the address, identity and
options — an alias whose host key you have already accepted fails *closed* if it ever
stops resolving, which is what you want from a target that starts agents.

Everything else follows: `herdr_bin` and `home` describe the **remote** filesystem
(`/opt/homebrew/bin/herdr` and `/Users/you` on a mac), and `cwd_allowlist` is checked
against remote paths. Long prompts are written to the handoff file on the remote side
too. Both preflight checks and the Remote Control lookup happen over the same
connection, so a remote spawn returns the same `claude.ai/code` link a local one does.

ssh's own failures are reported as themselves — `255` as a connection failure naming
the target, `127` as a missing `herdr_bin` — rather than as a herdr error, so you are
not sent debugging a herdr server that was never contacted.

---

## Configure

Every `[hosts.<name>]` table becomes a value of the tool's `host` parameter — the enum
is built from your config at startup, so nothing about your machines is in the source.
Hosts you define but leave `enabled = false` are still advertised, deliberately: a
refusal that names the missing prerequisite is more useful to a calling model than the
value simply not existing.

The settings that matter:

| Key | Why it matters |
|---|---|
| `cwd_allowlist` | **The** blast-radius control. A spawn's cwd must resolve under one of these roots — fully resolved, so `…/scratch-evil` does not pass a `…/scratch` root and symlinks cannot walk out. |
| `default_cwd` | Where a spawn lands when the caller names no cwd. Must itself be under `cwd_allowlist`; a misconfigured one is refused, not trusted. A host without it refuses any spawn that names no cwd. |
| `protected_workspaces` | Never closed, never spawned into. |
| `skip_permissions` | Per-host only, never a tool parameter. An unattended agent with permissions skipped, started from a chat message, is the one combination that can quietly do real damage. |

See [`config.toml.example`](config.toml.example) for the full annotated set.

---

## Run it as a service

```sh
cp deploy/service.env.example ~/.config/herdr-spawn/service.env
$EDITOR ~/.config/herdr-spawn/service.env          # port, bind, docker network

cp deploy/herdr-spawn.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now herdr-spawn
loginctl enable-linger "$USER"                     # survive logout and reboot
```

It must run as **your own uid**, via `systemctl --user` — not as a system unit and not
as a dedicated service user. The herdr socket is `srw-------` inside a `0700` home and
`~/.claude/sessions` is `drwx------`; no other uid can reach either. The unit uses
systemd's `%h` specifier throughout, so it carries no absolute paths.

`deploy/run.sh` binds a single private address. Set `HERDR_SPAWN_BIND` explicitly, or
set `HERDR_SPAWN_DOCKER_NETWORK` and let it discover that network's bridge IP at start
(compose files rarely pin the IPAM subnet, so a hardcoded address can go stale). If
discovery fails it falls back to **loopback**, never `0.0.0.0` — this tool starts
autonomous coding agents, so anything that can reach the port can start one, and on a
host without a firewall the bind address is the entire security boundary.

---

## Putting it behind an MCP gateway

This is the part that turns it from a local tool into something a chat on your phone can
call. The pattern below is written against
[agentic-community/mcp-gateway-registry](https://github.com/agentic-community/mcp-gateway-registry)
— a self-hosted registry that fronts many backend MCP servers behind one OAuth-gated
origin, so each backend ends up at `https://gateway.example.com/<path>/mcp` — but steps
1, 2 and 6 apply to any HTTP MCP gateway.

The shape to keep in mind: **your backend never faces the internet.** It binds a private
address the gateway can reach, and the gateway owns TLS, OAuth and per-group
authorisation at the edge.

### 1. Speak streamable HTTP, statelessly

```sh
HERDR_SPAWN_TRANSPORT=streamable-http HERDR_SPAWN_BIND=<bridge-ip> HERDR_SPAWN_PORT=8230
```

Run **stateless**. Gateways commonly proxy each MCP request independently, and an older
proxy hop may drop the `Mcp-Session-Id` header entirely — a stateless server does not
care. A fire-and-forget spawn needs no session continuity anyway.

### 2. Bind where the gateway can reach you, and nowhere else

If the gateway runs in Docker and your server does not, bind that compose network's
**bridge gateway IP**:

```sh
docker network ls                                   # find the network name
docker network inspect <network> -f '{{(index .IPAM.Config 0).Gateway}}'
```

Containers on that network reach it; the LAN and your VPN do not. Verify from both
sides — the second command is the one that matters:

```sh
docker exec <a-gateway-container> curl -sf http://<bridge-ip>:8230/mcp -X POST \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"c","version":"0"}}}'
curl -sf --max-time 5 http://<your-lan-ip>:8230/mcp     # MUST fail
```

If your server runs in Docker too, skip all of this and just join it to the gateway's
network — then the target is `http://<container-name>:8230/mcp`.

### 3. Get an admin token

The registry authenticates management calls with an OAuth2 client-credentials (M2M)
token from its identity provider — Keycloak by default. The bootstrap script writes the
client secret to `.oauth-tokens/<realm>-m2m.json` in the gateway checkout.

Note the ceiling before you plan around it: **an M2M client can register and toggle, but
not patch or remove** — those return 403. Fixing a bad record means editing the
registry's MongoDB directly and restarting the registry container to reload. Get it
right the first time.

### 4. Register the backend

Write a registration JSON:

```json
{
  "server_name": "herdr-spawn",
  "description": "Start a Claude Code session on one of my hosts and hand it a prompt.",
  "path": "/herdr-spawn",
  "proxy_pass_url": "http://<bridge-ip>:8230/mcp",
  "auth_scheme": "none",
  "tags": ["agents", "automation"],
  "num_tools": 1
}
```

```sh
cd <gateway-checkout>
uv run python api/registry_management.py register --config /path/to/herdr-spawn.json
uv run python api/registry_management.py list          # confirm it is there
```

Three details that each cost an hour if you get them wrong:

- **`proxy_pass_url` must end in `/mcp`.** The registry's health check only initialises
  an MCP session when it does; without it the server registers and then sits permanently
  unhealthy with no useful error.
- **Leave `mcp_endpoint` unset.** Setting it yields a broken relative path.
- **`auth_scheme: "none"` means "no auth *between gateway and backend*"** — which is
  correct here, because the backend is only reachable from the gateway. The edge OAuth is
  still fully in force. It does *not* mean the tool is public.

### 5. Enable it, and grant it explicitly

Registration is not enablement:

```sh
curl -X POST https://gateway.example.com/api/servers/toggle \
  -H "Authorization: Bearer $TOKEN" \
  -d 'path=/herdr-spawn' -d 'new_state=enabled'
```

Then authorise it in `auth_server/scopes.yml`. Read your existing entries before assuming
you need to add one — a group with `server: '*'`, `tools: ['all']` already grants every
new server the moment it registers, including this one. **That is worth a deliberate
decision for a tool that starts autonomous agents**, not a default you inherit. Give it
its own block if you want it narrower:

```yaml
my-group:
- server: herdr-spawn
  methods: [tools/list, tools/call]
  tools: [spawn_claude]
```

### 6. Verify the edge, both ways

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://gateway.example.com/herdr-spawn/mcp   # expect 401
curl -s https://gateway.example.com/herdr-spawn/mcp -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

The unauthenticated 401 is the half people skip, and it is the half that matters.

### 7. Audit what the gateway does not

Gateways typically log tool **names** but not **arguments** — so the prompt a caller
actually sent is nowhere in the gateway's logs. `spawns.jsonl` is the only record of it
on the box, and even there only as a SHA-256 (the prompt body itself is kept only when it
was large enough to be written to a handoff file). Know which of those you want before
you need them.

### One last thing to check on your own gateway

Look at what else the gateway publishes on the host. An internal proxy or admin port
published on `0.0.0.0` can be an unauthenticated path straight to your backend, right
past the OAuth edge you just configured. Enumerate with `ss -ltn`, and confirm each
published port either requires auth or is bound to loopback — **before** you point a
tool that starts agents at it.

---

## Things that are the way they are for a reason

**No `--until` on `agent prompt`.** Passing it *replaces* herdr's default match set
(`idle`, `done`, `blocked`) rather than adding to it, dropping `done` — which is
exactly the state a backgrounded spawn settles on. The documented
`--until idle --until blocked` hangs until the full timeout while the agent sits
there having already answered. Bare `--wait` returns in seconds.

**A lock per pane, held across submit+settle.** Two `agent prompt` calls issued
back to back concatenate into one submitted line and lose the first Enter —
and both still return rc=0. For a chat-driven service, two messages inside one
second is ordinary traffic. The tests assert two rapid prompts produce two turns.

**The remote poll runs remotely.** Waiting for `bridgeSessionId` takes up to 15 s at
200 ms intervals. Doing that from this side would be 75 ssh handshakes; the loop is sent
over and returns once. Same reason the two preflight checks share a single round trip.
And the prompt reaches the remote `herdr` through `shlex.join`, so arbitrary prose —
quotes, backticks, `$`, newlines — arrives as one argument and never reaches the remote
*shell* as syntax.

**No pane scraping for the Remote Control id.** Claude Code writes
`bridgeSessionId` to `~/.claude/sessions/<pid>.json` roughly 1.2 s *before*
`herdr agent start` even returns. Scraping the banner works at default pane width and
fails below ~123 columns, where Ink hard-breaks the line. The registry read has no
race, no regex, and no width dependence — but it is undocumented internals, so every
spawn records the observed Claude Code `version` and the resolver warns when it changes.

**The pane's Claude process is not `foreground_processes[0]`.** Claude spawns children —
`caffeinate`, one process per configured MCP server — and they sort ahead of it: on a
real pane it came back *last of seven*, behind `caffeinate`. Reading the session
registry for a child's pid finds no record, so Remote Control reports itself unavailable
on a spawn whose bridge connected perfectly well. The foreground process *group* id is
the job leader, which is claude. (`argv0` is the second opinion; `name` is no help — it
holds the version string.)

**`TZ=UTC` on the remote `ps`.** The pid-recycle guard compares the session record's
`procStart` against the live process. On macOS that field is a `ctime` string rather than
Linux clock ticks — and Claude Code writes it in **UTC** while `ps -o lstart=` prints
**local** time. Anywhere off UTC the two differ by exactly the offset, and the guard
rejects every good spawn with "pid reused". The remote probe also picks its format per
platform: `/proc/<pid>/stat` where that exists, `ps` where it does not.

**RC failing is not the spawn failing.** `bridgeSessionId` only appears once the
bridge connects. Offline, unauthenticated or rate-limited, it never does — and
`agent start` still reports `interactive_ready`. That returns `rc_unavailable` with
the pane details rather than failing: a local Claude Code session with no phone
handle is still a working session.

**cwd is optional, and defaults to a per-host sandbox.** Omit it and the spawn lands in
that host's `default_cwd`, so a caller that doesn't know your layout doesn't have to
guess — and a guess just gets refused. The default must itself sit under
`cwd_allowlist`; a misconfigured one outside it is still refused rather than trusted.
An explicit cwd is validated the same way, and a host with no `default_cwd` refuses a
spawn that names none.

**Absolute path to the herdr binary, `HOME` always set.** A systemd `--user` unit's
PATH has no `~/.local/bin`, and with a stripped environment herdr silently falls
back to `/tmp/herdr/herdr.sock` and reports `server_not_running` — which blames the
wrong thing entirely and sends you debugging a healthy server.

**`rc=2` is plain text.** herdr returns JSON on stdout at rc=0 and JSON on stderr at
rc=1, but rc=2 (e.g. an unsupported `--kind`) prints a bare sentence. A wrapper that
unconditionally `json.loads(stderr)` crashes there instead of reporting the problem.

**The advertised tool schema is slimmed after generation.** This server is meant to be
enabled in every chat and every Claude Code session, so its definition is paid for on
every request. Pydantic's per-field `title` and `anyOf: [T, null]` expansion cost ~a
quarter of that and tell a calling model nothing. Stripping them takes the tool from
357 to **172 tokens**. It edits `parameters` (what clients see), never `fn_metadata`
(what validates), so the two cannot desync — and if a future SDK moves the attribute,
it logs and serves the verbose schema instead.

---

## Layout

```
src/herdr_spawn/
  config.py   host registry, cwd allowlist, path-traversal-safe resolution
  herdr.py    CLI wrapper; the three return conventions
  rc.py       Remote Control id resolution (registry poll, pid+procStart join)
  state.py    append-only JSONL + teardown guard
  server.py   preflight, spawn sequence, MCP wiring
deploy/
  herdr-spawn.service    systemd --user unit (%h throughout, no absolute paths)
  service.env.example    site-specific settings -> ~/.config/herdr-spawn/service.env
  run.sh                 bind discovery + launch
```

## Tests

```sh
uv run pytest -q
```

The ones that matter: `…-evil` must not pass a `…/scratch` root, symlinks must not
escape, `rc=2` must not crash the wrapper, `--until` must never be sent, the teardown
guard must refuse tabs it did not create and any protected workspace, and
RC-unavailable must not be treated as failure.

## Licence

MIT
