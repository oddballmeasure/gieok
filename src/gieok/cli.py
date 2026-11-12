"""`gieok`: the command line for one node's store."""

import argparse
import json
import os
import socket
import sys
from pathlib import Path

from gieok import search as search_mod
from gieok.chunk import digest
from gieok.ingest import Context, Report, settings_files
from gieok.ingest.memory import ingest_claude_memory, ingest_notes
from gieok.ingest.transcripts import ingest_claude, ingest_codex
from gieok.store import Store

DEFAULT_HOME = Path(os.getenv("GIEOK_HOME", "~/.local/share/gieok")).expanduser()


def _mb(n: int) -> str:
    return f"{(n or 0) / 1e6:.1f} MB"


def _ratio(raw: int, stored: int) -> str:
    return f"{raw / stored:.1f}x" if stored else "-"


def _backfill(store: Store, verbose: bool = False) -> None:
    from gieok.migrate import backfill
    r = backfill(store)
    if r.moved or r.transcripts or r.conflicts or verbose:
        print(f"namespaces: {len(r.moved)} memories moved, {r.transcripts} transcripts tagged, "
              f"{len(r.conflicts)} conflicts", flush=True)
    for line in (r.moved if verbose else []) + r.conflicts:
        print("  ", line)


def cmd_migrate(store: Store, args) -> int:
    _backfill(store, verbose=True)
    return 0


def cmd_ingest(store: Store, args) -> int:
    _backfill(store)
    ctx = Context(host=store.node)
    from gieok import scrub
    ctx.known_secrets = scrub.known_values(settings_files(ctx.home))
    every = not (args.memory or args.notes or args.claude or args.codex)
    total = Report()
    steps = [("memory", args.memory, lambda: ingest_claude_memory(store, ctx)),
             ("notes", args.notes, lambda: ingest_notes(store, ctx)),
             ("codex", args.codex, lambda: ingest_codex(store, ctx, limit=args.limit)),
             ("claude", args.claude, lambda: ingest_claude(store, ctx, limit=args.limit))]
    for name, wanted, run in steps:
        if not (every or wanted):
            continue
        r = run()
        total.add(r)
        print(f"{name}: {r.seen} seen, {r.stored} stored, {r.skipped} unchanged, {_mb(r.raw_bytes)} read, "
              f"{_mb(r.new_bytes)} new chunk bytes, {r.redactions} redactions, {len(r.errors)} errors", flush=True)
    for kind in ("user", "feedback", "project", "reference", "fact", "notes"):
        if store.db.execute("SELECT COUNT(*) FROM dict WHERE kind=?", (kind,)).fetchone()[0] == 0:
            store.train_dict(kind)
    if args.seal:
        store.seal(force=True)
    for line in total.errors[:20]:
        print("error:", line, file=sys.stderr)
    if total.live_secrets:
        print(f"LIVE-LOOKING SECRETS redacted in {len(total.live_secrets)} places:", file=sys.stderr)
        for line in sorted(set(total.live_secrets))[:50]:
            print("  ", line, file=sys.stderr)
    return 0


def cmd_stats(store: Store, args) -> int:
    s = store.stats()
    if args.json:
        print(json.dumps(s, indent=1))
        return 0
    cold = s["packs_stored"] + s["staging"]
    print(f"memories   {s['memories']} ({s['versions']} versions) · raw {_mb(s['memory_raw'])} -> stored "
          f"{_mb(s['memory_stored'])} ({_ratio(s['memory_raw'], s['memory_stored'])}) · {s['dicts']} dicts "
          f"{_mb(s['dict_bytes'])}")
    print(f"transcripts {s['transcripts']} · raw {_mb(s['transcript_raw'])} -> unique {_mb(s['unique_bytes'])} "
          f"({s['unique_chunks']} chunks) -> packs {_mb(s['packs_stored'])} + staging {_mb(s['staging'])} "
          f"({_ratio(s['transcript_raw'], cold)} so far)")
    print(f"database   {_mb(s['db_bytes'])} · total on disk {_mb(cold + s['db_bytes'])}")
    return 0


def cmd_search(store: Store, args) -> int:
    if args.transcripts:
        for t in search_mod.search_transcripts(store, args.query, args.k):
            print(f"#{t['rid']} {t['agent']}@{t['host']} {t['project']} {t['source']}")
        return 0
    embedder = None
    if not args.no_vec and store.db.execute("SELECT 1 FROM vec LIMIT 1").fetchone():
        from gieok.embed import Embedder
        embedder = Embedder()
    for h in search_mod.search(store, args.query, args.k, args.ns, embedder):
        print(f"{h.score:.4f} [{h.via}] {h.memory.ns}/{h.memory.key} · {h.memory.title[:80]}\n    {h.snippet}")
    return 0


def cmd_get(store: Store, args) -> int:
    mem = store.get(args.ns, args.key)
    if mem is None:
        print("no such memory", file=sys.stderr)
        return 1
    if args.history:
        for v in store.history(mem.id):
            print(f"{v.id} {v.author} {v.size} B {v.reason}{' batch ' + v.batch if v.batch else ''}")
        return 0
    print(store.text(mem.id))
    return 0


def cmd_put(store: Store, args) -> int:
    print(store.put(args.ns, args.key, sys.stdin.read(), author=args.author, kind=args.kind, tier=args.tier,
                    title=args.title or "", reason=args.reason or ""))
    return 0


def cmd_verify(store: Store, args) -> int:
    """Rebuild every version and transcript and check its hash; unscrubbed transcripts must match the source."""
    bad = 0
    for (vid,) in store.db.execute("SELECT id FROM version").fetchall():
        try:
            store._version_bytes(vid)
        except Exception as exc:
            bad += 1
            print("version", vid, exc)
    rows = store.db.execute("SELECT rid, source, blob, source_hash, redactions FROM transcript").fetchall()
    matched = 0
    for r in rows:
        try:
            data = store.read_blob(r["blob"])
        except Exception as exc:
            bad += 1
            print("transcript", r["source"], exc)
            continue
        if r["redactions"] == 0:
            if digest(data) != r["source_hash"]:
                bad += 1
                print("transcript", r["source"], "does not rebuild to its source")
            else:
                matched += 1
    print(f"verified: {len(rows)} transcripts ({matched} byte-identical to source, "
          f"{sum(1 for r in rows if r['redactions'])} redacted), {bad} problems")
    return 1 if bad else 0


def cmd_embed(store: Store, args) -> int:
    from gieok.embed import Embedder, embed_missing
    print(f"embedded {embed_missing(store, Embedder())} memories")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gieok")
    p.add_argument("--home", type=Path, default=DEFAULT_HOME)
    p.add_argument("--node", default=os.getenv("GIEOK_NODE") or socket.gethostname())
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest", help="import memories, notes and transcripts from this host")
    for flag in ("memory", "notes", "claude", "codex"):
        i.add_argument(f"--{flag}", action="store_true")
    i.add_argument("--limit", type=int)
    i.add_argument("--seal", action="store_true", help="compress the open pack even if it is small")
    s = sub.add_parser("stats")
    s.add_argument("--json", action="store_true")
    q = sub.add_parser("search")
    q.add_argument("query")
    q.add_argument("-k", type=int, default=10)
    q.add_argument("--ns", default="")
    q.add_argument("--no-vec", action="store_true")
    q.add_argument("--transcripts", action="store_true")
    g = sub.add_parser("get")
    g.add_argument("ns")
    g.add_argument("key")
    g.add_argument("--history", action="store_true")
    w = sub.add_parser("put", help="write a memory from stdin")
    w.add_argument("ns")
    w.add_argument("key")
    w.add_argument("--author", required=True)
    w.add_argument("--kind", default="fact")
    w.add_argument("--tier", default="hot", choices=("hot", "warm"))
    w.add_argument("--title")
    w.add_argument("--reason")
    sub.add_parser("verify")
    sub.add_parser("migrate", help="move Phase 1 rows into the Phase 2 namespaces (idempotent)")
    sub.add_parser("embed", help="embed memories that have no vector yet (loads the model, ~750 MB)")
    sub.add_parser("seal", help="compress the open pack now")
    args = p.parse_args(argv)
    store = Store(args.home, args.node)
    if args.cmd == "seal":
        print(store.seal(force=True) or "nothing to seal")
        return 0
    return {"ingest": cmd_ingest, "stats": cmd_stats, "search": cmd_search, "get": cmd_get, "put": cmd_put,
            "verify": cmd_verify, "embed": cmd_embed, "migrate": cmd_migrate}[args.cmd](store, args)


if __name__ == "__main__":
    sys.exit(main())
