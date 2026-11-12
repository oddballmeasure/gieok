"""Phase 2 access: grants on search/get/list/put, the scrub refusal, the audit log, and an MCP round trip."""

import json
import os

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from gieok.grants import Refused, matches
from gieok.mcpserver import Gate, build_server

GRANTS = """
[agents.jangin]
read = ["agent/daepyo/**", "agent/jangin/**", "project/**", "shared/**"]
write = ["agent/jangin/**"]
transcripts = false

[agents.viewer]
read = ["project/**"]

[agents.overseer]
read = ["**"]
transcripts = true
"""
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.fixture
def home(store, tmp_path):
    home = tmp_path / "node"
    (home / "grants.toml").write_text(GRANTS)
    os.chmod(home / "grants.toml", 0o644)
    put = lambda ns, key, text: store.put(ns, key, text, author="ingest")  # noqa: E731
    put("agent/daepyo", "koizler-prod-deploy", "Koizler prod deploy is live at koizler.com from /opt/koizler")
    put("agent/daepyo", "merge-only-when-green", "Merge only when green: gate gh pr merge on gh pr checks")
    put("agent/saekdong", "private-deploy", "Saekdong private deploy notes, merge only when green")
    put("agent/kkum", "dream", "kkum deploy dream merge green")
    put("project/oscar", "NOTES", "oscar NOTES: deploy and merge only when green")
    put("shared/team", "rules", "shared rule: merge only when green before deploy")
    for i in range(30):  # decoys outside jangin's grant that also match
        put("agent/saekdong", f"decoy-{i}", f"deploy deploy merge only when green decoy {i}")
    store.put_transcript(source="/p/-home-monty-oscar/s1.jsonl", host="oscar", agent="daepyo",
                         project="-home-monty-oscar", ns="transcripts/oscar", harness="claude", data=b'{"a":1}',
                         source_hash=b"0" * 32, source_size=7, source_mtime=0.0, redactions=0,
                         text="Monty private chat: merge only when green, koizler prod deploy", started=None, ended=None)
    return home


def gate(store, home, agent="jangin"):
    return Gate(store, agent, home)


def audit(home, agent="jangin"):
    return [json.loads(line) for line in (home / "audit" / f"{agent}.jsonl").read_text().splitlines()]


def allowed(path):
    return path.startswith(("agent/daepyo/", "agent/jangin/", "project/", "shared/"))


def test_globs():
    assert matches("agent/daepyo/x", ("agent/daepyo/**",))
    assert matches("agent/daepyo/a/b", ("agent/daepyo/**",))
    assert not matches("agent/daepyo-evil/x", ("agent/daepyo/**",))
    assert not matches("agent/daepyo/a/b", ("agent/daepyo/*",))
    assert not matches("agent/daepyox", ("agent/daepyo*/x",))


@pytest.mark.parametrize("query", ["merge only when green", "koizler prod deploy", "deploy", "Monty private chat"])
def test_search_returns_only_granted(store, home, query):
    hits = gate(store, home).search(query, limit=50)
    assert all(allowed(h["path"]) for h in hits), [h["path"] for h in hits]
    assert not any(h["kind"] == "transcript" or h["path"].startswith("transcripts/") for h in hits)
    if query != "Monty private chat":
        assert any(h["path"].startswith("agent/daepyo/") for h in hits)


def test_search_namespace_and_starvation(store, home):
    g = gate(store, home)
    assert [h["path"] for h in g.search("deploy", namespace="agent/saekdong")] == []
    top = g.search("merge only when green", limit=3)
    assert len(top) == 3 and all(allowed(h["path"]) for h in top)  # 30 denied decoys don't starve the result


def test_overseer_sees_transcripts(store, home):
    hits = {h["path"]: h for h in gate(store, home, "overseer").search("Monty private chat")}
    assert "transcripts/oscar/s1" in hits
    assert gate(store, home, "overseer").get(hits["transcripts/oscar/s1"]["id"])["text"] == '{"a":1}'


def test_get_and_list_filtered(store, home):
    g = gate(store, home)
    mine = store.get("agent/daepyo", "merge-only-when-green")
    got = g.get(mine.id)
    assert got["path"] == "agent/daepyo/merge-only-when-green" and "gh pr checks" in got["text"]
    for ns, key in (("agent/saekdong", "private-deploy"), ("agent/kkum", "dream")):
        with pytest.raises(Refused):
            g.get(store.get(ns, key).id)
    with pytest.raises(Refused):
        g.get("transcript:1")
    with pytest.raises(Refused):
        g.get("no-such-id")
    listed = [m["path"] for m in g.list("", 500)]
    assert listed and all(allowed(p) for p in listed)
    assert "agent/daepyo/koizler-prod-deploy" in listed and "project/oscar/NOTES" in listed
    assert g.list("agent/saekdong") == [] and g.list("transcripts/") == []
    assert len(g.list("", 2)) == 2


def test_put_inside_write_grant_only(store, home):
    g = gate(store, home)
    r = g.put("notes/today", "# Jangin today\nshipped the thing")
    assert r["path"] == "agent/jangin/notes/today"
    got = g.get(r["id"])
    assert got["text"].endswith("shipped the thing") and got["author"] == "agent:jangin" and got["node"] == "test-node"
    assert "mcp put by jangin on test-node at 20" in store.history(r["id"])[-1].reason
    assert g.put("notes/today", "# Jangin today\nshipped the thing")["unchanged"]
    for bad in ("../daepyo/x", "/abs", "a//b", "", "a/../b"):
        with pytest.raises(Refused, match="bad key"):
            g.put(bad, "x")
    with pytest.raises(Refused, match="no write grant"):
        gate(store, home, "viewer").put("x", "y")
    with pytest.raises(Refused, match="limit"):
        g.put("big", "x" * (32 * 1024 + 1))
    assert store.get("agent/daepyo", "x") is None and store.get("agent/viewer", "x") is None


def test_put_refuses_secrets_without_echoing(store, home):
    g = gate(store, home)
    with pytest.raises(Refused) as exc:
        g.put("creds", f"the token is {GH} ok")
    assert "github-token" in str(exc.value) and GH not in str(exc.value)
    assert store.get("agent/jangin", "creds") is None
    line = audit(home)[-1]
    assert line["tool"] == "put" and line["args"] == {"key": "creds", "length": len(f"the token is {GH} ok")}
    assert GH not in json.dumps(audit(home))
    g2 = Gate(store, "jangin", home, known_secrets=["s3cr3t-value-123"])
    with pytest.raises(Refused, match="known-value"):
        g2.put("k", "here s3cr3t-value-123")


def test_put_redacts_pii(store, home):
    r = gate(store, home).put("contact", "customer wrote from jane.doe@example.org")
    assert r["pii_redacted"] == 1
    assert "example.org" not in store.text(r["id"])


def results(lines):
    return [x for x in lines if x.get("phase") != "intent"]


def test_audit_lines(store, home):
    g = gate(store, home)
    g.search("deploy", 5)
    g.put("k", "hello there")
    with pytest.raises(Refused):
        g.get(store.get("agent/saekdong", "private-deploy").id)
    lines = audit(home)
    assert [(x["tool"], x.get("phase")) for x in lines] == [("search", None), ("put", "intent"), ("put", None),
                                                           ("get", None)]
    assert lines[0]["args"] == {"query": "deploy", "limit": 5, "namespace": None} and lines[0]["count"] >= 1
    assert lines[1]["args"] == lines[2]["args"] == {"key": "k", "length": 11}
    assert "content" not in json.dumps(lines[1:3]) and lines[2]["count"] == 1
    assert "error" in lines[3] and all(x["agent"] == "jangin" and x["time"] for x in lines)


def test_put_intent_logged_before_the_write(store, home, monkeypatch):
    g = gate(store, home)
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(store, "put", boom)
    with pytest.raises(OSError):
        g.put("k", "hello")
    lines = audit(home)
    assert [x.get("phase") for x in lines] == ["intent", None] and "disk full" in lines[1]["error"]


def test_audit_clips_and_survives_bad_text(store, home):
    g = gate(store, home)
    g.search("x" * 100_000)
    with pytest.raises(Refused, match="not valid Unicode"):
        g.put("k", "a\ud800b")
    try:
        g.search("a\ud800b")  # SQLite may take it or refuse it; either way it is audited
    except Exception:
        pass
    with pytest.raises(Refused):
        g.get("y" * 100_000)
    lines = audit(home)
    assert len(lines) == 4 and all(len(json.dumps(x)) < 2000 for x in lines)
    assert lines[0]["args"]["query"].endswith("[100000 chars]") and lines[1]["args"]["length"] == 3
    assert store.get("agent/jangin", "k") is None


def test_unknown_agent_refused_and_logged_apart(store, home):
    g = gate(store, home, "mallory")
    for call in (lambda: g.search("deploy"), lambda: g.list(""), lambda: g.put("x", "y"),
                 lambda: g.get(store.get("project/oscar", "NOTES").id)):
        with pytest.raises(Refused, match="no grant"):
            call()
    assert not (home / "audit" / "mallory.jsonl").exists() and store.get("agent/mallory", "x") is None
    lines = audit(home, "_refused")
    assert len(lines) == 4 and {x["agent"] for x in lines} == {"mallory"}
    with pytest.raises(Refused):
        gate(store, home, "../evil").search("x")
    assert sorted(p.name for p in (home / "audit").iterdir()) == ["_refused.jsonl"]


def test_bad_grants_file_refuses_all(store, home):
    g = gate(store, home)
    os.chmod(home / "grants.toml", 0o666)
    with pytest.raises(Refused, match="writable"):
        g.search("deploy")
    os.chmod(home / "grants.toml", 0o644)
    assert g.search("deploy")
    (home / "grants.toml").write_text('[agents.jangin]\nread = "agent/**"\n')
    with pytest.raises(Refused):
        g.search("deploy")
    (home / "grants.toml").write_text("[agents.someone]\nread = []\n")  # revoked: picked up without a restart
    with pytest.raises(Refused, match="no grant"):
        g.search("deploy")
    assert [x.get("error", "")[:8] for x in audit(home, "_refused")] == ["grants.t", "grants.t", "agent 'j"]


@pytest.mark.parametrize("name,content,kind", [
    ("zero-width", "sk-ant-abcdefghij\u200bklmnopqrstuvwxyz0123", "anthropic-key"),
    ("fullwidth", "\uff53\uff4b-ant-abcdefghijklmnopqrstuvwxyz0123", "anthropic-key"),
    ("inner-space", "sk-ant-abcdefghij klmnopqrstuvwxyz0123", "anthropic-key"),
    ("split-github", "ghp_a1B2c3D4e5F6g7H8 i9J0k1L2m3N4o5P6q7R8", "github-token"),
    ("soft-hyphen", "ghp_a1B2c3D4e5F6g7H8\u00adi9J0k1L2m3N4o5P6q7R8", "github-token"),
])
def test_put_refuses_disguised_secrets(store, home, name, content, kind):
    with pytest.raises(Refused, match=kind):
        gate(store, home).put(name, f"note: {content} end")
    assert store.get("agent/jangin", name) is None


def test_put_keeps_ordinary_prose(store, home):
    g = gate(store, home)
    for i, text in enumerate(["sk-learn is a library for machine learning in python",
                              "ghp_ tokens are 40 characters long", "password: see the vault for it"]):
        assert g.put(f"prose-{i}", text)["path"] == f"agent/jangin/prose-{i}"


def test_put_never_revives_a_forgotten_key(store, home):
    g = gate(store, home)
    g.put("old", "first")
    store.forget("agent/jangin", "old", author="monty")
    with pytest.raises(Refused, match="this key was forgotten; use a new key"):
        g.put("old", "again")
    assert store.get("agent/jangin", "old").deleted
    assert g.put("old-2", "again")["path"] == "agent/jangin/old-2"


def test_forgotten_check_holds_inside_the_store(store):
    from gieok.store import Forgotten
    store.put("agent/jangin", "k", "a", author="x")
    store.forget("agent/jangin", "k", author="monty")
    with pytest.raises(Forgotten):
        store.put("agent/jangin", "k", "b", author="x", revive=False)
    assert store.get("agent/jangin", "k").deleted


def test_transcript_ids_ascii_digits_only(store, home):
    g = gate(store, home, "overseer")
    for bad in ("transcript:\u00b2", "transcript:\u0661", "transcript:-1", "transcript:"):
        with pytest.raises(Refused, match="no transcript"):
            g.get(bad)
    assert g.get("transcript:1")["path"] == "transcripts/oscar/s1"


def test_search_ranks_only_allowed_docs(store, home):
    g = gate(store, home)
    before = [h["path"] for h in g.search("deploy merge green", limit=50)]
    assert all("score" not in h for h in g.search("deploy"))
    for i in range(200):  # denied documents that would crowd any pool
        store.put("agent/saekdong", f"flood-{i}", "deploy merge green " * 5, author="ingest")
    after = [h["path"] for h in g.search("deploy merge green", limit=50)]
    assert set(after) == set(before) and all(allowed(p) for p in after)
    assert g.search("deploy", namespace="agent/saekdong") == []


def test_mcp_round_trip(store, home):
    async def main():
        async with create_connected_server_and_client_session(build_server(gate(store, home))) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert tools == {"search", "get", "list", "put"}
            put = await client.call_tool("put", {"key": "hello", "content": "jangin was here"})
            assert not put.isError and put.structuredContent["path"] == "agent/jangin/hello"
            found = await client.call_tool("search", {"query": "merge only when green", "limit": 20})
            paths = [h["path"] for h in found.structuredContent["result"]]
            assert "agent/daepyo/merge-only-when-green" in paths and all(allowed(p) for p in paths)
            got = await client.call_tool("get", {"id": put.structuredContent["id"]})
            assert got.structuredContent["text"] == "jangin was here"
            denied = await client.call_tool("get", {"id": store.get("agent/saekdong", "private-deploy").id})
            assert denied.isError
            secret = await client.call_tool("put", {"key": "s", "content": GH})
            assert secret.isError and GH not in secret.content[0].text
    anyio.run(main)
    assert [x["tool"] for x in results(audit(home))] == ["put", "search", "get", "get", "put"]


def test_mcp_audits_rejected_calls(store, home):
    async def main():
        async with create_connected_server_and_client_session(build_server(gate(store, home))) as client:
            for name, args in [("put", {"key": "x", "content": ["SENTINEL-" + GH]}), ("put", {"key": "x"}),
                               ("nope", {"a": 1}),
                               ("get", {"id": ["a"]}), ("search", {"query": "deploy", "limit": "lots"})]:
                assert (await client.call_tool(name, args)).isError
    anyio.run(main)
    lines = audit(home)
    assert [x["tool"] for x in lines] == ["put", "put", "nope", "get", "search"]
    assert all("error" in x for x in lines) and "SENTINEL" not in json.dumps(lines) and GH not in json.dumps(lines)
    assert lines[0]["args"] == {"key": "x", "length": None}


def test_cli_refuses_unknown_agent_and_pins_node(store, home, capsys):
    import socket
    from gieok import cli
    assert cli.main(["--home", str(home), "--node", "spoofed", "mcp", "--agent", "mallory"]) == 2
    assert "no grant" in capsys.readouterr().err
    line = audit(home, "_refused")[-1]
    assert line["tool"] == "start" and line["node"] == socket.gethostname() != "spoofed"


ADVERSARIAL = {
    "prefix-run": "sk-" * 11000,
    "prefix-then-pieces": "sk-" + "a " * 17000,
    "prefix-space-digit": "sk- 1" * 7000,
    "newline-pieces": "sk-a1\n" * 6000,
    "github-pieces": "ghp_1 " * 6000,
    "mixed-whitespace": "sk-ant-1 \n\t" * 4000,
    "one-long-piece": "sk-" + "a1" * 16000,
    "wide-gap": "sk-1" + " " * 32000 + "a",
    "max-pieces": ("ghp_a1 " + "b2 " * 8) * 1500,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_put_scan_is_linear(store, home, name):
    import time
    g = gate(store, home)
    content = ADVERSARIAL[name][:32 * 1024]
    t = time.perf_counter()
    try:
        g.put(f"dos-{name}", content)
    except Refused:
        pass
    assert time.perf_counter() - t < 0.2


@pytest.mark.parametrize("sep", ["\n", "\r\n", "\t", " \n  ", " "])
def test_put_refuses_keys_split_by_any_whitespace(store, home, sep):
    with pytest.raises(Refused, match="anthropic-key"):
        gate(store, home).put("split", f"key: sk-ant-abcdefghij{sep}klmnopqrstuvwxyz0123 ok")


def test_put_stores_the_original_text(store, home):
    g = gate(store, home)
    text = "ㅋㅋ 한국어 ① ﬁ Ａ 👩‍💻 café"
    r = g.put("unicode", text)
    assert store.text(r["id"]) == text and g.get(r["id"])["text"] == text
    r2 = g.put("unicode-pii", "ㅋㅋ mail jane.doe@example.org ①")
    assert store.text(r2["id"]) == "ㅋㅋ mail [redacted] ①"


def test_put_refuses_disguised_pii(store, home):
    with pytest.raises(Refused, match="personal data"):
        gate(store, home).put("pii", "mail ｊａｎｅ@ｅｘａｍｐｌｅ.org")
    assert store.get("agent/jangin", "pii") is None


def test_audit_scrubs_logged_strings(store, home):
    g = gate(store, home)
    g.search(f"find {GH} please")
    with pytest.raises(Refused):
        g.get(f"id-{GH}")
    text = (home / "audit" / "jangin.jsonl").read_text()
    assert GH not in text and "[secret:github-token]" in text


def test_mcp_rejection_messages_hold_no_input(store, home):
    async def main():
        async with create_connected_server_and_client_session(build_server(gate(store, home))) as client:
            for name, args in [("put", {"key": "m2", "content": {"a": f"x, input_type=zz {GH}"}}),
                               ("put", {"key": "m4", "content": 5, GH: 1}),
                               ("put", {"key": {"x": GH}, "content": "ok"}),
                               ("get", {"id": 5})]:
                assert (await client.call_tool(name, args)).isError
    anyio.run(main)
    lines = audit(home)
    blob = json.dumps(lines)
    assert GH not in blob and "R8" not in blob and "input_value" not in blob
    assert [x["error"] for x in lines] == ["ValidationError: invalid arguments (content)",
                                           "ValidationError: invalid arguments (content)",
                                           "ValidationError: invalid arguments (key)",
                                           "ValidationError: invalid arguments (id)"]
    assert lines[1]["args"] == {"key": "m4", "length": None, "unknown_args": 1}
    assert lines[2]["args"] == {"key": "dict", "length": 2}
