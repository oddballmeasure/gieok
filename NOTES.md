# NOTES: Gieok (기억)

## CURRENT POSITION (2026-11-12)
Phase 2 minimal (MCP + grants + audit, for jangin) BUILT on branch `phase2-mcp` in worktree ~/gieok-wt/mcp.
Not pushed, no PR (Daepyo/Monty decide). Plan: ~/.claude/plans/great-let-s-work-out-temporal-rossum.md.
- DONE (committed): namespaces + `gieok migrate` backfill; `grants.py` (grants.toml, fail closed); `mcpserver.py`
  (`gieok mcp --agent <name> [--search fts|hybrid]`, tools search/get/list/put, audit JSONL); `gieok refresh` +
  `deploy/gieok-refresh.{service,timer}` (written, NOT installed); staging recovery under the write lock; README.
  57 tests green at first; review round 1 (11-12, CHANGES) fixed: node pinned in mcp mode, audit in `finally`,
  put intent line, `_refused.jsonl` for ungranted agents, SDK-level rejections audited (pydantic input values hidden),
  500-char clipping, NFKC + Cf strip + split-key check before scrub, rank-after-filter search with no score,
  forgotten keys never revived (checked in the store tx), ASCII-digit transcript ids. 70 tests green.
  Reviewer probes: /tmp/probe/{p,c,s}.py (c.py: 6 procs x 60 puts -> 720 audit lines, 0 bad).
  Review round 2 (CHANGES) fixed: split-key scan is a bounded linear pass (was a quadratic regex, 12.7 s on
  "sk-"*10000; now ~0.01 s), any whitespace separates pieces; put stores the ORIGINAL text (normalized text is
  scanned only; PII visible only after normalizing refuses the put); SDK rejection errors are a fixed string with
  type + field names; audit strings are scrubbed. Probes /tmp/probe/p2.py, p3.py. 88 tests green.
  Review round 3 (last, 3 LOW) fixed: put key scanned for credentials; scrub.py EMAIL anchored to the start of a
  run (the quadratic one was EMAIL, not PHONE/IPV4: `\b...+@` restarted at every position) and the private-key
  body stops at the next "-----" (possessive, <= 16 KB); PII check compares spans via a per-char NFKC origin map
  (zero-width inside an email refuses). 122 tests green. Probe /tmp/probe/p4.py.
  MERGE NOTE: the other person's uncommitted scrub.py in ~/gieok edits the same EMAIL line (adds `\\` to the
  same lookbehind). It conflicts textually; resolve by taking their line (a superset of this fix).
  Next action: review done; push/PR only when told.
- DONE on the real store (~/.local/share/gieok), 2026-11-12: backup at ~/.local/share/gieok.bak-2026-11-12;
  `gieok migrate` moved 7 NOTES rows (shared/project/<r>/notes -> project/<r>/NOTES), tagged 2192 transcripts,
  0 conflicts; `gieok refresh` once; `deploy/grants.toml` installed to grants.toml (0644); jangin searches over real
  stdio MCP returned only agent/daepyo + project/ hits, no transcripts; `gieok verify` 0 problems.
- NOT DONE / NEXT:
  1. Review, push, PR, merge (until merged, do NOT run `gieok ingest` from ~/gieok main: the old code writes
     `shared/project/...` again; `migrate` would then report conflicts for those rows).
  2. Monty installs the timer: `cp deploy/gieok-refresh.* ~/.config/systemd/user/ && systemctl --user daemon-reload
     && systemctl --user enable --now gieok-refresh.timer`.
  3. Jangin reaches the server ONLY via a forced-command key in oscar's authorized_keys (README "Remote agents"):
     `restrict,command="/home/monty/oscar/deploy/jangin-link.sh" ssh-ed25519 AAAA... jangin@host`. The wrapper (built
     in oscar) runs `gieok mcp --agent jangin --search fts` on `SSH_ORIGINAL_COMMAND=gieok-mcp`; it pins `--agent` and
     passes no `--home`. His MCP client runs `ssh -i <key> monty@<oscar> gieok-mcp`.
  4. Rest of Phase 2 per plan: per-principal tokens, signed grant changes, forget/audit/curate permissions, overseer
     and dreamer grants, `system/audit` in the store, write-through export of Claude auto-memory.

## Decisions
- Codec choice per version: smallest of dict/zstd vs delta (delta chains capped at 8).
- Dictionaries trained per kind after ingest (codec.MIN_TRAIN=24 samples). Kinds with fewer stay plain zstd.
- Transcripts: whole file re-ingested when size/mtime change; CDC makes that cost only new chunks.
- Index only user/assistant message text of transcripts (tool output not indexed), capped 400k chars per transcript.
- Scrub false positives fixed after a masked survey of real hits (/tmp/gieok-bench-scripts/hits.py):
  assigned-secret skips `$VAR`, `<placeholder>`, calls; card pattern anchored (decimals and UUID pieces broke JSON).
  Monty's own email in every system prompt is redacted (redact-everything rule).
- Embedding model peaks ~750 MB RSS: only short-lived processes load it. MCP defaults to `--search fts`.
- Phase 2 (11-12): grants match `<ns>/<key>` paths. Memory ns = `agent/<slug>` from the Claude project dir
  (namespaces.AGENT_FOR_SLUG maps oscar -> daepyo); transcripts get a `ns` column (`transcripts/<slug>`), backfilled
  from `project`. The backfill moves memories by the `imported from <path>` reason of their head version.
- Grants are a plain TOML file Monty owns (not signed yet): wrong owner or group/world-writable -> refuse all.
  Re-read on change. `transcripts/**` needs `transcripts = true` regardless of read globs. Writes to `transcripts/`
  and `system/` are never allowed.
- put: scrub first; any credential refuses the put (kinds named, value never echoed, not in audit); PII is stored
  redacted. Provenance: author `agent:<name>`, version origin = node, reason `mcp put by <agent> on <node> at <iso>`.
- get of a missing id and of a denied id give the same answer (no existence oracle). No delete tool.
- Identity is `--agent` as given: safe only behind the forced-command ssh key. mcp mode ignores --node/GIEOK_NODE.
- Audit file is `<agent>.jsonl` only for an agent the current grants name; otherwise `_refused.jsonl`.
- Known limits (README "Limits"): encoded secrets pass the scrubber; bm25 IDF is corpus-wide; JSON the SDK transport
  can't parse never reaches gieok (not audited, nothing stored).

## Gotchas
- Daepyo's Bash guard blocks command text containing the dotenv name; write such files with the Write tool.
- cstack hooks block `git ls-files` at a repo root and sleep loops.
- FastMCP needs `structured_output=True` and `dict[str, Any]` return types for structured results.
- A Claude project dir that slugs to an agent name (say a future `~/jangin` checkout) lands its auto-memory in that
  agent's namespace, where that agent can read and write it.
- Many processes open the store (web, MCP sessions, timer, ingest). Each has its own HLC clock from `meta`.
