# Gieok (기억)

The staff's shared memory store: space-optimized, distributed across hosts, with a web UI (later phases).
One node per host keeps `gieok.db` (SQLite, WAL) and `packs/` (immutable zstd packs).

| Tier | Holds | Stored as |
|---|---|---|
| hot / warm | memories, notes, digests | versions inline in SQLite: zstd-19 with a trained dictionary per kind, or a delta against the parent |
| cold | Claude Code and Codex transcripts | 16 KB content-defined chunks, deduplicated, in 64 MB long-window zstd packs |

Everything is scrubbed at ingest on the origin host: credentials become `[secret:<kind>]`, customer PII `[redacted]`.

```
uv sync --extra embed
uv run gieok ingest            # memories, NOTES.md, Codex sessions, Claude transcripts
uv run gieok stats             # raw vs stored per tier
uv run gieok embed             # vectors for memories (loads a ~750 MB model; short-lived)
uv run gieok search "who handles design"
uv run gieok verify            # rebuild everything and check hashes
uv run gieok refresh           # re-ingest memories and NOTES.md only (deploy/gieok-refresh.timer runs it hourly)
uv run gieok migrate           # move Phase 1 rows into the namespaces below (idempotent; ingest runs it too)
uv run --with pytest pytest
```

## Namespaces
Every memory has a path `<namespace>/<key>`:

| Path | Holds |
|---|---|
| `agent/daepyo/<file>` | Claude auto-memory of the oscar project |
| `agent/<project-slug>/<file>` | auto-memory of every other Claude project (`-home-monty-koizler` -> `koizler`) |
| `agent/<agent>/<key>` | what an agent writes through MCP |
| `project/<repo>/NOTES` | a repo's NOTES.md |
| `shared/...` | everyone's |
| `transcripts/<project-slug>/<session>` | raw Claude Code and Codex sessions (cold tier) |

## MCP server and grants
`gieok mcp --agent <name>` is a stdio MCP server (official `mcp` SDK) with four tools: `search(query, limit,
namespace)`, `get(id)`, `list(namespace_prefix, limit)` and `put(key, content)`. There is no delete.

- **Grants** live in `~/.local/share/gieok/grants.toml` (template: `deploy/grants.toml`). Per agent: `read` and
  `write` globs over paths (`*` stays in a segment, `**` crosses segments) and `transcripts = true|false`. The file must
  belong to the user running the server and must not be group- or world-writable. An agent the file doesn't name, or a
  bad file, gets every call refused. Edits apply on the next call. Nothing in Gieok writes this file.
- **Reads:** every result passes the read grant. Search restricts its candidates to readable memories before it
  ranks them, so denied memories never move a rank or take a pool slot. Hits carry no score. `transcripts/**` needs
  `transcripts = true`.
- **Writes:** `put` writes `agent/<agent>/<key>` as a new version, only inside the write grant, at most 32 KB. The
  content is NFKC-normalized and stripped of format characters (zero-width and the like) first. Then the scrubber
  runs, and also checks known token prefixes whose pieces are split by whitespace. Credentials refuse the whole put:
  the reply names the kinds, never the value. Customer PII is stored as `[redacted]`. The version records the agent,
  the node and the time. A key Monty forgot stays forgotten: a put on it is refused ("this key was forgotten; use a
  new key").
- **Audit:** one JSON line per call, allowed or refused, written in a `finally` whatever the grant check says, in
  `~/.local/share/gieok/audit/<agent>.jsonl`. Each line has the time, agent, node, tool, args (for `put` only the key
  and the length, never the content) and the result count or the error. Strings are cut at 500 characters. A `put`
  logs an `"phase":"intent"` line before the store write and the result line after it. Calls from an agent
  `grants.toml` doesn't name (or while the file is unusable) go to `audit/_refused.jsonl`, so they can't create new
  files. Calls the SDK rejects before a tool runs (bad argument types, unknown tool names) are audited too, with the
  rejected values hidden.
- **Search:** `--search fts` (the default) never loads the embedding model; use it for remote agents and small hosts.
  `--search hybrid` loads it once per process, on the first search, if vectors exist.
- **Node:** in `mcp` mode the node recorded in versions and audit lines is always the real hostname; `--node` and
  `GIEOK_NODE` are ignored.

```
uv run --project ~/gieok gieok mcp --agent jangin --search fts
```

### Remote agents
The server trusts `--agent` and `--home`, so a remote agent must never get a shell. It reaches `gieok mcp` only
through an ssh key with a forced command. The command pins `--agent` and passes no `--home`. On oscar the wrapper is
oscar's `deploy/jangin-link.sh`: it runs `gieok mcp --agent jangin --search fts` when `SSH_ORIGINAL_COMMAND` is
`gieok-mcp`, and refuses anything else. The line in `~/.ssh/authorized_keys` (one line):

```
restrict,command="/home/monty/oscar/deploy/jangin-link.sh" ssh-ed25519 AAAAC3Nza...<jangin's public key> jangin@<his host>
```

`restrict` turns off port, agent and X11 forwarding and the pty. Jangin's MCP client then runs
`ssh -i <his key> monty@<oscar> gieok-mcp` as a stdio server.

### Limits
- The scrubber matches patterns. It can't detect secrets that are base64-, hex- or otherwise encoded, reversed or
  rotated. An agent that means to smuggle a key can.
- Ranking runs only over allowed memories, but FTS5's bm25 still uses corpus-wide term statistics, so denied
  memories have a tiny, indirect effect on the order of allowed hits.
- A request that is not valid JSON-RPC (for example a lone UTF-16 surrogate in a JSON string) is dropped by the SDK's
  stdio transport before any tool runs. Gieok never sees it, so it isn't audited. Nothing is stored either.
- Every agent runs as Unix user `monty`, so the grants file and audit log protect against mistakes and a confined
  agent, not against code running as `monty` on oscar.

## Staff HQ (web UI)
Memory, the agents' finished work, and saved reports, served on the tailnet address only, with Discord login.
The guild owner edits; members with `ALLOWED_ROLE_IDS` read (no transcripts). The site never acts on an agent.
Settings: `.env.example`. Template contract for the design: `src/gieok/web/CONTEXT.md`.

```
uv run gieok-web               # or ./deploy/install-web.sh for the systemd user service
```

Plan: `~/.claude/plans/great-let-s-work-out-temporal-rossum.md`. State: `NOTES.md`.
