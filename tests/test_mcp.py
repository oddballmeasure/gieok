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


def test_audit_lines(store, home):
    g = gate(store, home)
    g.search("deploy", 5)
    g.put("k", "hello there")
    with pytest.raises(Refused):
        g.get(store.get("agent/saekdong", "private-deploy").id)
    lines = audit(home)
    assert [x["tool"] for x in lines] == ["search", "put", "get"]
    assert lines[0]["args"] == {"query": "deploy", "limit": 5, "namespace": None} and lines[0]["count"] >= 1
    assert lines[1]["args"] == {"key": "k", "length": 11} and "content" not in json.dumps(lines[1])
    assert "error" in lines[2] and all(x["agent"] == "jangin" and x["time"] for x in lines)


def test_unknown_agent_refused(store, home):
    g = gate(store, home, "mallory")
    for call in (lambda: g.search("deploy"), lambda: g.list(""), lambda: g.put("x", "y"),
                 lambda: g.get(store.get("project/oscar", "NOTES").id)):
        with pytest.raises(Refused, match="no grant"):
            call()
    assert len(audit(home, "mallory")) == 4 and store.get("agent/mallory", "x") is None


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
    assert [x["tool"] for x in audit(home)] == ["put", "search", "get", "get", "put"]


def test_cli_refuses_unknown_agent(store, home, capsys):
    from gieok import cli
    assert cli.main(["--home", str(home), "--node", "test-node", "mcp", "--agent", "mallory"]) == 2
    assert "no grant" in capsys.readouterr().err
    assert audit(home, "mallory")[-1]["tool"] == "start"
