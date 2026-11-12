"""Phase 2 namespace backfill. Idempotent: run it as often as you like.

Phase 1 put every Claude project's auto-memory in `agent/daepyo` and NOTES.md files in
`shared/project/<repo>/notes`. Each ingested version records `imported from <path>`, so the head version
says where the memory belongs now (namespaces.ingest_target). Transcripts get `transcripts/<slug>`."""

from dataclasses import dataclass, field

from gieok.namespaces import ingest_target, transcript_ns
from gieok.store import Store

PREFIX = "imported from "


@dataclass
class Backfill:
    moved: list[str] = field(default_factory=list)  # "old -> new"
    conflicts: list[str] = field(default_factory=list)
    transcripts: int = 0


def backfill(store: Store) -> Backfill:
    out = Backfill()
    rows = store.db.execute("SELECT m.id, m.ns, m.key, v.author, v.reason FROM memory m JOIN version v ON v.id=m.head "
                            "WHERE v.author LIKE 'ingest%' ORDER BY m.rid").fetchall()
    for r in rows:
        if not r["reason"].startswith(PREFIX):
            continue
        target = ingest_target(r["reason"][len(PREFIX):])
        if target is None or target == (r["ns"], r["key"]):
            continue
        old, new = f"{r['ns']}/{r['key']}", "/".join(target)
        if store.get(*target) is not None:
            out.conflicts.append(f"{old} -> {new} (target exists)")
            continue
        store.move(r["id"], *target, author="migrate", reason="Phase 2 namespaces")
        out.moved.append(f"{old} -> {new}")
    todo = store.db.execute("SELECT rid, project FROM transcript WHERE ns=''").fetchall()
    if todo:
        with store.tx() as db:
            db.executemany("UPDATE transcript SET ns=? WHERE rid=?", [(transcript_ns(t["project"]), t["rid"]) for t in todo])
            store._op(db, "transcript.ns", {"count": len(todo), "reason": "Phase 2 namespaces"})
        out.transcripts = len(todo)
    return out
