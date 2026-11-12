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
- **Reads:** every result passes the read grant, search hits included. `transcripts/**` needs `transcripts = true`.
- **Writes:** `put` writes `agent/<agent>/<key>` as a new version, only inside the write grant, at most 32 KB. The
  scrubber runs first: credentials refuse the whole put (the reply names the kinds, never the value); customer PII is
  stored as `[redacted]`. The version records the agent, the node and the time.
- **Audit:** one JSON line per call, allowed or refused, in `~/.local/share/gieok/audit/<agent>.jsonl`: time, agent,
  node, tool, args (for `put` only the key and the length) and the result count or the error.
- **Search:** `--search fts` (the default) never loads the embedding model; use it for remote agents and small hosts.
  `--search hybrid` loads it once per process, on the first search, if vectors exist.

```
uv run --project ~/gieok gieok mcp --agent jangin --search fts
```

## Staff HQ (web UI)
Memory, the agents' finished work, and saved reports, served on the tailnet address only, with Discord login.
The guild owner edits; members with `ALLOWED_ROLE_IDS` read (no transcripts). The site never acts on an agent.
Settings: `.env.example`. Template contract for the design: `src/gieok/web/CONTEXT.md`.

```
uv run gieok-web               # or ./deploy/install-web.sh for the systemd user service
```

Plan: `~/.claude/plans/great-let-s-work-out-temporal-rossum.md`. State: `NOTES.md`.
