"""The single-node store: codecs, chunks, packs, versions, undo. Run: uv run --with pytest pytest"""

import os
import random

from gieok import chunk, codec
from gieok.store import Store


def _text(n: int, seed: int) -> bytes:
    rnd = random.Random(seed)
    words = ["memory", "design", "Koizler", "Saekdong", "merge", "green", "deploy", "색동", "기억", "review"]
    return " ".join(rnd.choice(words) + str(rnd.randint(0, 99)) for _ in range(n)).encode()


def test_codecs_round_trip():
    data = _text(3000, 1)
    parent = data[:-200] + b" an edit at the end"
    trained = codec.train([_text(200, s) for s in range(40)])
    assert trained
    assert codec.decode(codec.plain(data), "zstd") == data
    assert codec.decode(codec.with_dict(data, trained), "dict", dict_bytes=trained) == data
    assert codec.decode(codec.delta(data, parent), "delta", parent=parent) == data
    assert codec.decode(codec.long_pack(data), "pack") == data
    assert len(codec.delta(data, parent)) < len(codec.plain(data)) / 5  # an edit costs bytes, not a copy


def test_chunk_boundaries_survive_an_insert():
    data = os.urandom(600_000)
    edited = data[:300_000] + b"a few new lines in the middle" + data[300_000:]
    before = {h for h, _ in chunk.split(data)}
    after = [h for h, _ in chunk.split(edited)]
    changed = [h for h in after if h not in before]
    assert len(changed) <= 2 and len(after) > 20


def test_blobs_dedupe_and_rebuild_through_a_sealed_pack(store):
    base = os.urandom(200_000)
    grown = base + os.urandom(20_000)
    d1, w1 = store.put_blob(base)
    d2, w2 = store.put_blob(grown)
    assert w1 >= 200_000 and w2 < 100_000  # the grown file reuses every chunk of the old one
    assert store.put_blob(base) == (d1, 0)
    assert store.seal(force=True)
    assert store.read_blob(d1) == base and store.read_blob(d2) == grown
    assert not list((store.root / "packs").glob("*.staging"))


def test_staging_is_cut_back_after_a_crash(tmp_path):
    s = Store(tmp_path / "n", "a")
    digest, _ = s.put_blob(os.urandom(50_000))
    staging = next((s.root / "packs").glob("*.staging"))
    good = staging.stat().st_size
    with staging.open("ab") as f:
        f.write(b"half-written chunk from a crash")
    s.db.close()
    s2 = Store(tmp_path / "n", "a")
    assert staging.stat().st_size == good
    assert len(s2.read_blob(digest)) == 50_000


def test_versions_use_deltas_and_keep_history(store):
    text = _text(800, 2).decode()
    v1 = store.put("agent/daepyo", "plan", text, author="daepyo", title="The plan")
    v2 = store.put("agent/daepyo", "plan", text + "\nOne more line.", author="daepyo")
    assert store.put("agent/daepyo", "plan", text + "\nOne more line.", author="daepyo") == v2  # unchanged: no-op
    mem = store.get("agent/daepyo", "plan")
    assert store.text(mem.id) == text + "\nOne more line." and store.text(mem.id, v1) == text
    row = store.db.execute("SELECT codec, LENGTH(body) FROM version WHERE id=?", (v2,)).fetchone()
    assert row[0] == "delta" and row[1] < 100
    assert [v.id for v in store.history(mem.id)] == [v1, v2]


def test_delta_chains_stay_short(store):
    text = "line\n" * 400
    for i in range(20):
        text += f"edit {i}\n"
        store.put("shared", "log", text, author="a")
    depths = [r[0] for r in store.db.execute("SELECT depth FROM version ORDER BY hlc")]
    assert max(depths) <= codec.MAX_DELTA_CHAIN
    assert store.text(store.get("shared", "log").id) == text


def test_dictionary_is_trained_and_used(store):
    for i in range(30):
        store.put("agent/daepyo", f"m{i}", _text(150, i).decode(), author="d", kind="project")
    assert store.train_dict("project")
    store.put("agent/daepyo", "new", _text(150, 999).decode(), author="d", kind="project")
    assert store.db.execute("SELECT codec FROM version ORDER BY hlc DESC LIMIT 1").fetchone()[0] == "dict"


def test_forget_and_undo_a_batch(store):
    store.put("agent/saekdong", "palette", "teal and coral", author="saekdong")
    store.put("agent/saekdong", "palette", "merged palette", author="dreamer", batch="dream-1")
    store.put("agent/saekdong", "fresh", "made by the dream", author="dreamer", batch="dream-1")
    assert store.undo_batch("dream-1", author="daepyo") == 2
    assert store.text(store.get("agent/saekdong", "palette").id) == "teal and coral"
    assert store.get("agent/saekdong", "fresh").deleted
    assert store.forget("agent/saekdong", "palette", author="saekdong")
    assert store.memories("agent/saekdong") == []
    assert len(store.memories("agent/saekdong", include_deleted=True)) == 2


def test_every_change_is_an_op_in_clock_order(store):
    store.put("shared", "a", "x", author="t")
    store.put("shared", "a", "y", author="t")
    store.forget("shared", "a", author="t")
    ops = store.db.execute("SELECT type, hlc FROM op ORDER BY seq").fetchall()
    assert [o[0] for o in ops] == ["memory.put", "memory.put", "memory.forget"]
    assert [o[1] for o in ops] == sorted(o[1] for o in ops)
