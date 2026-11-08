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
uv run --with pytest pytest
```

Plan: `~/.claude/plans/great-let-s-work-out-temporal-rossum.md`. State: `NOTES.md`.
