"""Ingest from Claude/Codex layouts, re-ingest of a grown session, and hybrid search."""

import hashlib
import json

import numpy as np

from gieok import search
from gieok.ingest import Context
from gieok.ingest.memory import ingest_claude_memory, ingest_notes
from gieok.ingest.transcripts import ingest_claude, ingest_codex

MEMORY = """---
name: saekdong
description: Saekdong (색동) design lead on Codex app-server
metadata:
  type: project
---
Saekdong is hire #1: design lead running on Codex. Daepyo reviews her PRs.
"""


def _home(tmp_path):
    mem = tmp_path / ".claude" / "projects" / "-home-monty-oscar" / "memory"
    mem.mkdir(parents=True)
    (mem / "saekdong.md").write_text(MEMORY)
    (mem / "merge-only-when-green.md").write_text("---\nname: merge\ndescription: Merge only when CI is green\n"
                                                  "---\nGate gh pr merge on gh pr checks exit 0.\n")
    (tmp_path / "koizler").mkdir()
    (tmp_path / "koizler" / "NOTES.md").write_text("# NOTES\nCURRENT POSITION: UI issues 141-146 rolling.\n")
    return Context(host="oscar", home=tmp_path)


def test_memory_and_notes_land_in_their_namespaces(store, tmp_path):
    ctx = _home(tmp_path)
    r = ingest_claude_memory(store, ctx)
    assert (r.seen, r.stored) == (2, 2)
    m = store.get("agent/daepyo", "saekdong")
    assert m.kind == "project" and "design lead" in m.title
    assert ingest_claude_memory(store, ctx).skipped == 2  # unchanged files make no new versions
    ingest_notes(store, ctx)
    assert "141-146" in store.text(store.get("shared/project/koizler", "notes").id)


def _session(n: int) -> str:
    return "\n".join(json.dumps({"type": "user" if i % 2 else "assistant", "uuid": f"u{i}",
                                 "timestamp": f"2026-11-08T20:{i % 60:02d}:00Z",
                                 "message": {"content": [{"type": "text", "text": f"step {i} " + "detail " * 40},
                                                         {"type": "tool_result", "content": "SECRET_TOOL_OUTPUT"}]}})
                     for i in range(n)) + "\n"


def test_transcripts_rebuild_and_grow_cheaply(store, tmp_path):
    projects = tmp_path / ".claude" / "projects" / "-home-monty-koizler"
    projects.mkdir(parents=True)
    path = projects / "s.jsonl"
    path.write_text(_session(400))
    ctx = Context(host="oscar", home=tmp_path)
    first = ingest_claude(store, ctx)
    assert first.stored == 1
    assert ingest_claude(store, ctx).skipped == 1
    path.write_text(_session(420))  # the live session grew
    grown = ingest_claude(store, ctx)
    assert grown.stored == 1 and grown.new_bytes < first.new_bytes / 5
    row = store.db.execute("SELECT * FROM transcript").fetchone()
    assert row["agent"] == "daepyo" and row["project"] == "-home-monty-koizler"
    assert hashlib.sha256(store.read_blob(row["blob"])).digest() == hashlib.sha256(path.read_bytes()).digest()
    assert search.search_transcripts(store, "step 410")
    assert not search.search_transcripts(store, "SECRET_TOOL_OUTPUT")  # tool output isn't indexed


def test_codex_sessions_are_attributed_by_cwd(store, tmp_path):
    day = tmp_path / ".codex" / "sessions" / "2026" / "11" / "08"
    day.mkdir(parents=True)
    meta = {"type": "session_meta", "payload": {"cwd": "/home/monty/saekdong-wt/koizler-t3"}}
    msg = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                "content": [{"type": "output_text", "text": "tightened the grid"}]}}
    (day / "rollout.jsonl").write_text(json.dumps(meta) + "\n" + json.dumps(msg) + "\n")
    ingest_codex(store, Context(host="oscar", home=tmp_path))
    row = store.db.execute("SELECT agent, harness FROM transcript").fetchone()
    assert tuple(row) == ("saekdong", "codex")
    assert search.search_transcripts(store, "grid")


class FakeEmbedder:
    """Deterministic stand-in for the model: a vector per word, so related texts share directions."""

    def __call__(self, texts):
        out = []
        for t in texts:
            v = np.zeros(384, dtype=np.float32)
            for w in search.WORD.findall(t.lower()):
                rnd = np.random.default_rng(int(hashlib.md5(w.encode()).hexdigest()[:8], 16))
                v += rnd.standard_normal(384).astype(np.float32)
            out.append(v / max(np.linalg.norm(v), 1e-9))
        return np.array(out)


def test_hybrid_search_ranks_and_fuses(store, tmp_path):
    from gieok.embed import embed_missing

    ingest_claude_memory(store, _home(tmp_path))
    store.put("shared", "deploy", "Koizler deploys from /opt/koizler on koizler-prod.", author="t", title="Deploy")
    embed_missing(store, FakeEmbedder())
    hits = search.search(store, "design lead", embedder=FakeEmbedder())
    assert hits[0].memory.key == "saekdong" and hits[0].via == "fts+vec"
    assert search.search(store, "merge green")[0].memory.key == "merge-only-when-green"
    assert search.search(store, "design", ns_prefix="shared") == []
    store.forget("agent/daepyo", "saekdong", author="t")
    assert all(h.memory.key != "saekdong" for h in search.search(store, "design lead", embedder=FakeEmbedder()))
