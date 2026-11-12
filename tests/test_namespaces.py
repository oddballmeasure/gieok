"""Phase 2 namespaces: where ingest puts things, and the backfill of a Phase 1 store."""

import json
import sqlite3

from gieok.ingest import Context
from gieok.ingest.memory import ingest_claude_memory, ingest_notes
from gieok.ingest.transcripts import ingest_claude
from gieok.migrate import backfill
from gieok.namespaces import memory_ns, slug, transcript_ns
from gieok.store import Store


def test_slugs():
    assert slug("-home-monty-koizler") == "koizler"
    assert slug("-home-monty") == "home"
    assert slug("-home-monty--cardinal-runs-02bb-context") == "cardinal-runs-02bb-context"
    assert slug("-tmp-cc-smoke") == "tmp-cc-smoke"
    assert memory_ns("-home-monty-oscar") == "agent/daepyo"
    assert memory_ns("-home-monty-bora-agent") == "agent/bora-agent"
    assert transcript_ns("-home-monty-koizler") == "transcripts/koizler"


def test_ingest_namespaces(store, tmp_path):
    projects = tmp_path / ".claude" / "projects"
    for d, name in (("-home-monty-oscar", "green"), ("-home-monty-koizler", "deploy")):
        (projects / d / "memory").mkdir(parents=True)
        (projects / d / "memory" / f"{name}.md").write_text(f"about {name}\n")
    (projects / "-home-monty-koizler" / "s1.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "hi"}}))
    (tmp_path / "oscar").mkdir()
    (tmp_path / "oscar" / "NOTES.md").write_text("notes\n")
    ctx = Context(host="oscar", home=tmp_path)
    ingest_claude_memory(store, ctx)
    ingest_notes(store, ctx)
    ingest_claude(store, ctx)
    assert sorted(f"{m.ns}/{m.key}" for m in store.memories()) == [
        "agent/daepyo/green", "agent/koizler/deploy", "project/oscar/NOTES"]
    assert store.db.execute("SELECT ns FROM transcript").fetchone()[0] == "transcripts/koizler"


def test_backfill_moves_phase1_rows(tmp_path):
    s = Store(tmp_path / "n", "test")
    mem = "/home/monty/.claude/projects"
    s.put("agent/daepyo", "green", "x", author="ingest:daepyo", reason=f"imported from {mem}/-home-monty-oscar/memory/green.md")
    s.put("agent/daepyo", "deploy", "y", author="ingest:daepyo",
          reason=f"imported from {mem}/-home-monty-koizler/memory/deploy.md")
    s.put("shared/project/oscar", "notes", "z", author="ingest", reason="imported from /home/monty/oscar/NOTES.md")
    s.put("agent/jangin", "own", "w", author="agent:jangin", reason="mcp put")
    s.put_transcript(source="/x/-home-monty-oscar/a.jsonl", host="h", agent="daepyo", project="-home-monty-oscar",
                     harness="claude", data=b"{}", source_hash=b"0" * 32, source_size=2, source_mtime=0.0, redactions=0,
                     text="hello", started=None, ended=None)
    s.db.execute("UPDATE transcript SET ns=''")  # as a Phase 1 store left it
    r = backfill(s)
    assert sorted(r.moved) == ["agent/daepyo/deploy -> agent/koizler/deploy",
                               "shared/project/oscar/notes -> project/oscar/NOTES"]
    assert r.transcripts == 1 and not r.conflicts
    paths = sorted(f"{m.ns}/{m.key}" for m in s.memories())
    assert paths == ["agent/daepyo/green", "agent/jangin/own", "agent/koizler/deploy", "project/oscar/NOTES"]
    assert s.text(s.get("project/oscar", "NOTES").id) == "z"
    again = backfill(s)
    assert (again.moved, again.transcripts) == ([], 0)
    s.db.close()


def test_old_store_gets_the_column(tmp_path):
    root = tmp_path / "n"
    root.mkdir()
    db = sqlite3.connect(root / "gieok.db")
    db.execute("CREATE TABLE transcript (rid INTEGER PRIMARY KEY, source TEXT NOT NULL UNIQUE, host TEXT NOT NULL, "
               "agent TEXT NOT NULL, project TEXT NOT NULL, harness TEXT NOT NULL, blob BLOB NOT NULL, "
               "source_hash BLOB NOT NULL, source_size INTEGER NOT NULL, source_mtime REAL NOT NULL, "
               "redactions INTEGER NOT NULL, started INTEGER, ended INTEGER, ingested INTEGER NOT NULL) STRICT")
    db.close()
    s = Store(root, "test")
    assert "ns" in {r[1] for r in s.db.execute("PRAGMA table_info(transcript)")}
    s.db.close()
