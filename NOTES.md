# NOTES: Gieok (기억)

## CURRENT POSITION (2026-11-08)
Phase 1 (single-node core) IN PROGRESS. Plan: ~/.claude/plans/great-let-s-work-out-temporal-rossum.md (Phase 0
results, access model and all guardrail decisions are there).
- DONE: store (schema, packs, versions with dict/delta codecs, blobs, ops/HLC, forget, batch undo), FastCDC chunking,
  scrub (secrets + PII), ingesters (Claude memory, NOTES.md, Claude + Codex transcripts), FTS5 contentless search,
  embeddings (fastembed MiniLM-L12 multilingual, 1-bit + int8), hybrid RRF search, CLI, 19 tests green. Repo PUBLIC (Monty 11-08), MIT.
- RUNNING: first full real ingest on oscar into ~/.local/share/gieok (log /tmp/gieok-bench-out/ingest.log).
- NEXT: check stats vs Phase 0 (expect ~51 MB packs for 700 MB), verify, `gieok embed`, search demo, then the
  columnar uuid/timestamp experiment (keep only if >=10% gain), push repo oddballmeasure/gieok (private), report the
  Phase 1 gate.
- Phase 2 after the gate: MCP server, access model (tokens, grants, audit, signed grant changes), write-through export
  of Claude auto-memory.

## Decisions
- Codec choice per version: smallest of dict/zstd vs delta (delta chains capped at 8).
- Dictionaries trained per kind after ingest (codec.MIN_TRAIN=24 samples). Kinds with fewer stay plain zstd.
- Transcripts: whole file re-ingested when size/mtime change; CDC makes that cost only new chunks.
- Index only user/assistant message text of transcripts (tool output not indexed), capped 400k chars per transcript.
- Scrub false positives fixed after a masked survey of real hits (/tmp/gieok-bench-scripts/hits.py):
  assigned-secret skips `$VAR`, `<placeholder>`, calls; card pattern anchored (decimals and UUID pieces broke JSON).
  Monty's own email in every system prompt is redacted (redact-everything rule).
- Embedding model peaks ~750 MB RSS: only short-lived processes load it.

## Gotchas
- Daepyo's Bash guard blocks command text containing the dotenv name; write such files with the Write tool.
- cstack hooks block `git ls-files` at a repo root and sleep loops.
