"""One Gieok node's store: gieok.db (SQLite, WAL) plus packs/ of immutable, long-window zstd packs.

Tiers:
- hot and warm: memories, versioned. Each version body is one zstd frame inline in the database:
  dict-compressed (a trained dictionary per kind) or a delta against its parent version.
- cold: transcripts. A transcript is a blob: an ordered list of content-defined chunks. Each chunk is
  stored once, whoever wrote it, first in the open pack's staging file and then in a sealed pack.

Every change is also an op in `op`, ordered by a hybrid logical clock, so Phase 3 can replicate it."""

import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import blake3

from gieok import chunk, codec
from gieok.hlc import Clock
from gieok.ids import ulid

PACK_TARGET = 64_000_000
TIERS = ("hot", "warm", "cold")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS memory (
    rid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, ns TEXT NOT NULL, key TEXT NOT NULL,
    kind TEXT NOT NULL, tier TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
    head TEXT, deleted INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL, updated INTEGER NOT NULL,
    UNIQUE (ns, key)) STRICT;
CREATE TABLE IF NOT EXISTS version (
    id TEXT PRIMARY KEY, memory TEXT NOT NULL, parent TEXT, hlc INTEGER NOT NULL, origin TEXT NOT NULL,
    author TEXT NOT NULL, codec TEXT NOT NULL, dict INTEGER, depth INTEGER NOT NULL DEFAULT 0,
    body BLOB NOT NULL, size INTEGER NOT NULL, hash BLOB NOT NULL, reason TEXT NOT NULL DEFAULT '',
    batch TEXT) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS version_by_memory ON version (memory, hlc);
CREATE TABLE IF NOT EXISTS dict (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, bytes BLOB NOT NULL,
    created INTEGER NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS pack (id TEXT PRIMARY KEY, raw INTEGER NOT NULL, stored INTEGER NOT NULL,
    sealed INTEGER NOT NULL) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS chunk (hash BLOB PRIMARY KEY, pack TEXT, off INTEGER NOT NULL, len INTEGER NOT NULL)
    STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS blob (hash BLOB PRIMARY KEY, size INTEGER NOT NULL, chunks BLOB NOT NULL)
    STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS transcript (
    rid INTEGER PRIMARY KEY, source TEXT NOT NULL UNIQUE, host TEXT NOT NULL, agent TEXT NOT NULL,
    project TEXT NOT NULL, harness TEXT NOT NULL, blob BLOB NOT NULL, source_hash BLOB NOT NULL,
    source_size INTEGER NOT NULL, source_mtime REAL NOT NULL, redactions INTEGER NOT NULL,
    started INTEGER, ended INTEGER, ingested INTEGER NOT NULL, ns TEXT NOT NULL DEFAULT '') STRICT;
CREATE TABLE IF NOT EXISTS op (
    seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, hlc INTEGER NOT NULL, origin TEXT NOT NULL,
    type TEXT NOT NULL, body TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS vec (rid INTEGER PRIMARY KEY, bits BLOB NOT NULL, q8 BLOB NOT NULL) STRICT;
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(title, body, content='', contentless_delete=1,
    tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(body, content='', contentless_delete=1,
    tokenize='unicode61 remove_diacritics 2');
"""


@dataclass
class Memory:
    id: str
    ns: str
    key: str
    kind: str
    tier: str
    title: str
    tags: list[str]
    head: str | None
    deleted: bool
    created: int
    updated: int
    rid: int


@dataclass
class Version:
    id: str
    memory: str
    parent: str | None
    hlc: int
    origin: str
    author: str
    size: int
    reason: str
    batch: str | None


class Store:
    def __init__(self, root: Path, node: str):
        self.root = Path(root)
        self.node = node
        (self.root / "packs").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "gieok.db", isolation_level=None, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._upgrade()
        self.clock = Clock(int(self._meta("hlc") or 0))
        self._lock = threading.RLock()
        self._pack_cache: OrderedDict[str, bytes] = OrderedDict()
        self._open_pack = self._meta("open_pack") or self._new_pack()
        self._recover_staging()

    # Plumbing

    def _upgrade(self) -> None:
        """Additive schema changes for stores made by older versions. Backfills live in migrate.py."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(transcript)")}
        if "ns" not in cols:  # Phase 2: transcripts get a namespace so grants can match them
            self.db.execute("ALTER TABLE transcript ADD COLUMN ns TEXT NOT NULL DEFAULT ''")

    def _meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value))

    @contextmanager
    def tx(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self._set_meta("hlc", str(self.clock.last))
            self.db.execute("COMMIT")

    def _op(self, db, type_: str, body: dict) -> None:
        db.execute("INSERT INTO op (id, hlc, origin, type, body) VALUES (?, ?, ?, ?, ?)",
                   (ulid(), self.clock.tick(), self.node, type_, json.dumps(body, separators=(",", ":"))))

    # Packs

    def _new_pack(self) -> str:
        pack = ulid()
        self.db.execute("INSERT OR IGNORE INTO pack (id, raw, stored, sealed) VALUES (?, 0, 0, 0)", (pack,))
        self._set_meta("open_pack", pack)
        return pack

    def _staging(self, pack: str) -> Path:
        return self.root / "packs" / f"{pack}.staging"

    def _recover_staging(self) -> None:
        """After a crash, cut the staging file back to what the database knows about.

        Under the write lock: another process (an ingest) appends to staging only inside its own write
        transaction, so once we hold the lock, bytes past the known end really are crash leftovers."""
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._open_pack = self._meta("open_pack") or self._open_pack
                path = self._staging(self._open_pack)
                known = self.db.execute("SELECT COALESCE(MAX(off + len), 0) FROM chunk WHERE pack=?",
                                        (self._open_pack,)).fetchone()[0]
                if path.exists() and path.stat().st_size > known:
                    with path.open("r+b") as f:
                        f.truncate(known)
            finally:
                self.db.execute("COMMIT")

    def _put_chunk(self, db, f, digest: bytes, data: bytes) -> int:
        """Store a chunk once, appending to the open staging file `f`. Returns the bytes newly written."""
        if db.execute("SELECT 1 FROM chunk WHERE hash=?", (digest,)).fetchone():
            return 0
        off = f.tell()
        f.write(data)
        db.execute("INSERT INTO chunk (hash, pack, off, len) VALUES (?, ?, ?, ?)", (digest, self._open_pack, off, len(data)))
        db.execute("UPDATE pack SET raw=raw+? WHERE id=?", (len(data), self._open_pack))
        return len(data)

    def seal(self, force: bool = False) -> str | None:
        """Compress the open pack into packs/<id>.pack once it's big enough (or when forced)."""
        with self._lock:
            path = self._staging(self._open_pack)
            size = path.stat().st_size if path.exists() else 0
            if size == 0 or (size < PACK_TARGET and not force):
                return None
            pack = self._open_pack
            stored = codec.long_pack(path.read_bytes())
            final = self.root / "packs" / f"{pack}.pack"
            tmp = final.with_suffix(".tmp")
            tmp.write_bytes(stored)
            os.replace(tmp, final)
            with self.tx() as db:
                db.execute("UPDATE pack SET sealed=1, stored=? WHERE id=?", (len(stored), pack))
                self._open_pack = self._new_pack()
                self._op(db, "pack.seal", {"pack": pack, "stored": len(stored)})
            path.unlink()
            return pack

    def _pack_bytes(self, pack: str, sealed: bool) -> bytes:
        if not sealed:
            return self._staging(pack).read_bytes()
        if pack in self._pack_cache:
            self._pack_cache.move_to_end(pack)
            return self._pack_cache[pack]
        data = codec.decode((self.root / "packs" / f"{pack}.pack").read_bytes(), "pack")
        self._pack_cache[pack] = data
        while len(self._pack_cache) > 2:  # each decoded pack is ~64 MB
            self._pack_cache.popitem(last=False)
        return data

    def read_chunk(self, digest: bytes) -> bytes:
        row = self.db.execute("SELECT c.pack, c.off, c.len, p.sealed FROM chunk c JOIN pack p ON p.id=c.pack "
                              "WHERE c.hash=?", (digest,)).fetchone()
        if row is None:
            raise KeyError(f"missing chunk {digest.hex()}")
        if not row["sealed"]:
            with self._staging(row["pack"]).open("rb") as f:
                f.seek(row["off"])
                data = f.read(row["len"])
        else:
            data = self._pack_bytes(row["pack"], True)[row["off"]: row["off"] + row["len"]]
        if chunk.digest(data) != digest:
            raise IOError(f"chunk {digest.hex()} fails its hash")
        return data

    # Blobs (cold tier)

    def put_blob(self, data: bytes) -> tuple[bytes, int]:
        """Store bytes as deduplicated chunks. Returns (blob hash, bytes newly written)."""
        digest = chunk.digest(data)
        written = 0
        with self.tx() as db:
            if db.execute("SELECT 1 FROM blob WHERE hash=?", (digest,)).fetchone():
                return digest, 0
            hashes = []
            with self._staging(self._open_pack).open("ab") as f:
                for h, piece in chunk.split(data):
                    written += self._put_chunk(db, f, h, piece)
                    hashes.append(h)
                f.flush()
                os.fsync(f.fileno())  # chunks are on disk before the database says they exist
            db.execute("INSERT INTO blob (hash, size, chunks) VALUES (?, ?, ?)", (digest, len(data), b"".join(hashes)))
        return digest, written

    def read_blob(self, digest: bytes) -> bytes:
        row = self.db.execute("SELECT size, chunks FROM blob WHERE hash=?", (digest,)).fetchone()
        if row is None:
            raise KeyError(f"missing blob {digest.hex()}")
        raw = row["chunks"]
        data = b"".join(self.read_chunk(raw[i: i + 32]) for i in range(0, len(raw), 32))
        if chunk.digest(data) != digest:
            raise IOError(f"blob {digest.hex()} fails its hash")
        return data

    # Memories (hot and warm tiers)

    def _dict_for(self, db, kind: str) -> tuple[int, bytes] | None:
        row = db.execute("SELECT id, bytes FROM dict WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        return (row["id"], row["bytes"]) if row else None

    def train_dict(self, kind: str) -> int | None:
        """Train (or retrain) the dictionary for a kind from its current heads."""
        samples = [self.text(m.id).encode() for m in self.memories(kind=kind)]
        trained = codec.train(samples)
        if trained is None:
            return None
        with self.tx() as db:
            cur = db.execute("INSERT INTO dict (kind, bytes, created) VALUES (?, ?, ?)", (kind, trained, int(time.time())))
            self._op(db, "dict.add", {"id": cur.lastrowid, "kind": kind, "hash": blake3.blake3(trained).hexdigest()})
            return cur.lastrowid

    def _encode(self, db, kind: str, data: bytes, parent: sqlite3.Row | None) -> tuple[str, int | None, int, bytes]:
        """(codec, dict id, chain depth, body): a delta when the parent is close, else dict or plain zstd."""
        options = []
        d = self._dict_for(db, kind)
        options.append(("dict", d[0], 0, codec.with_dict(data, d[1])) if d else ("zstd", None, 0, codec.plain(data)))
        if parent is not None and parent["depth"] < codec.MAX_DELTA_CHAIN:
            options.append(("delta", None, parent["depth"] + 1, codec.delta(data, self._version_bytes(parent["id"]))))
        return min(options, key=lambda o: len(o[3]))

    def _version_bytes(self, version_id: str) -> bytes:
        row = self.db.execute("SELECT codec, dict, body, parent, hash FROM version WHERE id=?", (version_id,)).fetchone()
        dict_bytes = None
        if row["codec"] == "dict":
            dict_bytes = self.db.execute("SELECT bytes FROM dict WHERE id=?", (row["dict"],)).fetchone()[0]
        parent = self._version_bytes(row["parent"]) if row["codec"] == "delta" else None
        data = codec.decode(row["body"], row["codec"], dict_bytes, parent)
        if blake3.blake3(data).digest() != row["hash"]:
            raise IOError(f"version {version_id} fails its hash")
        return data

    def put(self, ns: str, key: str, text: str, *, author: str, kind: str = "fact", tier: str = "hot",
            title: str = "", tags: list[str] = (), reason: str = "", batch: str | None = None) -> str:
        """Write a memory (new, or a new version of ns/key). Returns the version id. No-op if unchanged."""
        if tier not in ("hot", "warm"):
            raise ValueError("memories live in the hot or warm tier")
        data = text.encode()
        digest = blake3.blake3(data).digest()
        now = int(time.time())
        with self.tx() as db:
            mem = db.execute("SELECT * FROM memory WHERE ns=? AND key=?", (ns, key)).fetchone()
            parent = db.execute("SELECT * FROM version WHERE id=?", (mem["head"],)).fetchone() if mem and mem["head"] else None
            if parent is not None and parent["hash"] == digest and not mem["deleted"] and \
                    (not title or title == mem["title"]):
                return parent["id"]
            if mem is None:
                mem_id = ulid()
                cur = db.execute("INSERT INTO memory (id, ns, key, kind, tier, title, tags, created, updated) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (mem_id, ns, key, kind, tier, title, json.dumps(list(tags)), now, now))
                rid = cur.lastrowid
            else:
                mem_id, rid = mem["id"], mem["rid"]
                db.execute("DELETE FROM memory_fts WHERE rowid=?", (rid,))
            codec_name, dict_id, depth, body = self._encode(db, kind, data, parent)
            vid = ulid()
            hlc = self.clock.tick()
            db.execute("INSERT INTO version (id, memory, parent, hlc, origin, author, codec, dict, depth, body, size, hash, "
                       "reason, batch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (vid, mem_id, parent["id"] if parent else None, hlc, self.node, author, codec_name, dict_id, depth,
                        body, len(data), digest, reason, batch))
            db.execute("UPDATE memory SET head=?, title=COALESCE(NULLIF(?, ''), title), tags=?, kind=?, tier=?, deleted=0, "
                       "updated=? WHERE id=?", (vid, title, json.dumps(list(tags)), kind, tier, now, mem_id))
            db.execute("INSERT INTO memory_fts (rowid, title, body) VALUES (?, ?, ?)",
                       (rid, title or (mem["title"] if mem else ""), text))
            db.execute("DELETE FROM vec WHERE rid=?", (rid,))  # re-embedded by the embed worker
            self._op(db, "memory.put", {"memory": mem_id, "ns": ns, "key": key, "version": vid, "author": author,
                                        "batch": batch})
        return vid

    def forget(self, ns: str, key: str, *, author: str, reason: str = "", batch: str | None = None) -> bool:
        """Tombstone a memory. History stays, so it can be restored."""
        with self.tx() as db:
            mem = db.execute("SELECT * FROM memory WHERE ns=? AND key=? AND deleted=0", (ns, key)).fetchone()
            if mem is None:
                return False
            db.execute("UPDATE memory SET deleted=1, updated=? WHERE id=?", (int(time.time()), mem["id"]))
            db.execute("DELETE FROM memory_fts WHERE rowid=?", (mem["rid"],))
            db.execute("DELETE FROM vec WHERE rid=?", (mem["rid"],))
            self._op(db, "memory.forget", {"memory": mem["id"], "author": author, "reason": reason, "batch": batch})
        return True

    def move(self, memory_id: str, ns: str, key: str, *, author: str, reason: str = "") -> None:
        """Give a memory a new namespace and key. History, search index and vector stay with it."""
        with self.tx() as db:
            mem = db.execute("SELECT ns, key FROM memory WHERE id=?", (memory_id,)).fetchone()
            db.execute("UPDATE memory SET ns=?, key=? WHERE id=?", (ns, key, memory_id))
            self._op(db, "memory.move", {"memory": memory_id, "from": f"{mem['ns']}/{mem['key']}", "ns": ns,
                                         "key": key, "author": author, "reason": reason})

    def by_id(self, memory_id: str) -> Memory | None:
        row = self.db.execute("SELECT * FROM memory WHERE id=?", (memory_id,)).fetchone()
        return self._memory(row) if row else None

    def _memory(self, row: sqlite3.Row) -> Memory:
        return Memory(id=row["id"], ns=row["ns"], key=row["key"], kind=row["kind"], tier=row["tier"],
                      title=row["title"], tags=json.loads(row["tags"]), head=row["head"], deleted=bool(row["deleted"]),
                      created=row["created"], updated=row["updated"], rid=row["rid"])

    def get(self, ns: str, key: str) -> Memory | None:
        row = self.db.execute("SELECT * FROM memory WHERE ns=? AND key=?", (ns, key)).fetchone()
        return self._memory(row) if row else None

    def by_rid(self, rid: int) -> Memory | None:
        row = self.db.execute("SELECT * FROM memory WHERE rid=?", (rid,)).fetchone()
        return self._memory(row) if row else None

    def memories(self, ns_prefix: str = "", kind: str | None = None, include_deleted: bool = False) -> list[Memory]:
        sql, args = "SELECT * FROM memory WHERE ns LIKE ? ESCAPE '\\'", [ns_prefix.replace("%", "\\%").replace("_", "\\_") + "%"]
        if kind:
            sql, args = sql + " AND kind=?", [*args, kind]
        if not include_deleted:
            sql += " AND deleted=0"
        return [self._memory(r) for r in self.db.execute(sql + " ORDER BY ns, key", args)]

    def text(self, memory_id: str, version_id: str | None = None) -> str:
        if version_id is None:
            version_id = self.db.execute("SELECT head FROM memory WHERE id=?", (memory_id,)).fetchone()[0]
        return self._version_bytes(version_id).decode()

    def history(self, memory_id: str) -> list[Version]:
        rows = self.db.execute("SELECT id, memory, parent, hlc, origin, author, size, reason, batch FROM version "
                               "WHERE memory=? ORDER BY hlc", (memory_id,))
        return [Version(**dict(r)) for r in rows]

    def undo_batch(self, batch: str, *, author: str) -> int:
        """Revert every memory a batch touched to the version before the batch. Returns memories reverted."""
        rows = self.db.execute("SELECT DISTINCT memory FROM version WHERE batch=?", (batch,)).fetchall()
        undo = f"undo-{batch}"
        n = 0
        for (mem_id,) in rows:
            versions = self.history(mem_id)
            before = [v for v in versions if v.batch != batch and v.hlc < min(x.hlc for x in versions if x.batch == batch)]
            mem = self.db.execute("SELECT * FROM memory WHERE id=?", (mem_id,)).fetchone()
            if before:
                self.put(mem["ns"], mem["key"], self.text(mem_id, before[-1].id), author=author, kind=mem["kind"],
                         tier=mem["tier"], reason=f"undo batch {batch}", batch=undo)
            else:  # the batch created it
                self.forget(mem["ns"], mem["key"], author=author, reason=f"undo batch {batch}", batch=undo)
            n += 1
        return n

    # Transcripts (cold tier)

    def transcript_known(self, source: str, size: int, mtime: float) -> bool:
        row = self.db.execute("SELECT source_size, source_mtime FROM transcript WHERE source=?", (source,)).fetchone()
        return bool(row) and row["source_size"] == size and row["source_mtime"] == mtime

    def put_transcript(self, *, source: str, host: str, agent: str, project: str, harness: str, data: bytes,
                       ns: str = "",
                       source_hash: bytes, source_size: int, source_mtime: float, redactions: int, text: str,
                       started: int | None, ended: int | None) -> int:
        """Store a (scrubbed) transcript. Re-ingesting a grown file reuses every unchanged chunk."""
        digest, written = self.put_blob(data)
        with self.tx() as db:
            row = db.execute("SELECT rid FROM transcript WHERE source=?", (source,)).fetchone()
            if row:
                db.execute("UPDATE transcript SET blob=?, source_hash=?, source_size=?, source_mtime=?, redactions=?, "
                           "started=?, ended=?, ingested=?, ns=COALESCE(NULLIF(?, ''), ns) WHERE rid=?",
                           (digest, source_hash, source_size, source_mtime, redactions, started, ended, int(time.time()),
                            ns, row["rid"]))
                rid = row["rid"]
                db.execute("DELETE FROM transcript_fts WHERE rowid=?", (rid,))
            else:
                cur = db.execute("INSERT INTO transcript (source, host, agent, project, harness, blob, source_hash, "
                                 "source_size, source_mtime, redactions, started, ended, ingested, ns) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (source, host, agent, project, harness, digest, source_hash, source_size, source_mtime,
                                  redactions, started, ended, int(time.time()), ns))
                rid = cur.lastrowid
            db.execute("INSERT INTO transcript_fts (rowid, body) VALUES (?, ?)", (rid, text))
            self._op(db, "transcript.put", {"source": source, "blob": digest.hex(), "host": host, "agent": agent})
        return written

    def transcript(self, rid: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM transcript WHERE rid=?", (rid,)).fetchone()

    # Space

    def stats(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0] or 0  # noqa: E731
        packs_stored = sum(p.stat().st_size for p in (self.root / "packs").glob("*.pack"))
        staging = sum(p.stat().st_size for p in (self.root / "packs").glob("*.staging"))
        db_bytes = sum(p.stat().st_size for p in self.root.glob("gieok.db*"))
        return {
            "memories": q("SELECT COUNT(*) FROM memory WHERE deleted=0"),
            "versions": q("SELECT COUNT(*) FROM version"),
            "memory_raw": q("SELECT SUM(size) FROM version"),
            "memory_stored": q("SELECT SUM(LENGTH(body)) FROM version"),
            "dicts": q("SELECT COUNT(*) FROM dict"), "dict_bytes": q("SELECT SUM(LENGTH(bytes)) FROM dict"),
            "transcripts": q("SELECT COUNT(*) FROM transcript"),
            "transcript_raw": q("SELECT SUM(source_size) FROM transcript"),
            "unique_chunks": q("SELECT COUNT(*) FROM chunk"), "unique_bytes": q("SELECT SUM(len) FROM chunk"),
            "packs_sealed": q("SELECT COUNT(*) FROM pack WHERE sealed=1"), "packs_stored": packs_stored,
            "staging": staging, "db_bytes": db_bytes,
        }
