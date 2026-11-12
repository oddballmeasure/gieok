"""`gieok mcp --agent <name>`: a stdio MCP server that gives one agent its granted slice of the store.

Tools: search, get, list, put. There is no forget or delete. Every result passes the agent's read grant
(grants.py), search hits included; put writes only `agent/<agent>/<key>` and only inside the write grant.
Every call, allowed or refused, appends one line to `<home>/audit/<agent>.jsonl`."""

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from gieok import scrub
from gieok import search as search_mod
from gieok.grants import AGENT_NAME, Access, Grant, Refused
from gieok.hlc import wall_ms
from gieok.namespaces import transcript_path
from gieok.store import Memory, Store

PUT_MAX = 32 * 1024
TRANSCRIPT_MAX = 256 * 1024  # characters of a transcript `get` returns
LIST_MAX = 500
SEARCH_MAX = 50
KEY = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*\Z")
TID = "transcript:"


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


class Gate:
    """The four tools, checked against the grant and audited. FastMCP wraps these (build_server)."""

    def __init__(self, store: Store, agent: str, home: Path, *, search: str = "fts", known_secrets: list[str] = ()):
        self.store = store
        self.agent = agent
        self.access = Access(Path(home) / "grants.toml", agent)
        self.audit_dir = Path(home) / "audit"
        self.search_mode = search
        self.known_secrets = list(known_secrets)
        self._embedder = None
        self._embedder_failed = False

    # Audit

    def _audit(self, tool: str, args: dict, *, count: int | None = None, error: str | None = None) -> None:
        self.audit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = self.agent if AGENT_NAME.match(self.agent) else "_invalid"
        line = {"time": _iso(time.time()), "agent": self.agent, "node": self.store.node, "tool": tool, "args": args}
        line.update({"error": error} if error is not None else {"count": count})
        fd = os.open(self.audit_dir / f"{name}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        finally:
            os.close(fd)

    def _run(self, tool: str, logged: dict, fn):
        try:
            result = fn(self.access.grant())
        except Refused as exc:
            self._audit(tool, logged, error=str(exc))
            raise
        except Exception as exc:
            self._audit(tool, logged, error=f"{type(exc).__name__}: {exc}")
            raise
        self._audit(tool, logged, count=len(result) if isinstance(result, list) else 1)
        return result

    # Helpers

    @staticmethod
    def _path(mem: Memory) -> str:
        return f"{mem.ns}/{mem.key}"

    def _readable(self, grant: Grant, mem: Memory) -> bool:
        return not mem.deleted and grant.can_read(self._path(mem))

    def _embed(self):
        if self.search_mode != "hybrid" or self._embedder_failed:
            return None
        if self._embedder is None:
            if not self.store.db.execute("SELECT 1 FROM vec LIMIT 1").fetchone():
                return None
            try:
                from gieok.embed import Embedder
                self._embedder = Embedder()  # ~750 MB, once per process
            except Exception:
                self._embedder_failed = True
                return None
        return self._embedder

    # Tools

    def search(self, query: str, limit: int = 10, namespace: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), SEARCH_MAX))
        prefix = namespace or ""

        def run(grant: Grant) -> list[dict[str, Any]]:
            hits = search_mod.search(self.store, query, limit, prefix, self._embed(),
                                     allow=lambda m: self._readable(grant, m), pool=max(200, limit * 20))
            out = [{"id": h.memory.id, "path": self._path(h.memory), "title": h.memory.title, "kind": h.memory.kind,
                    "score": round(h.score, 4), "via": h.via, "snippet": h.snippet} for h in hits]
            if grant.transcripts and len(out) < limit:
                for t in search_mod.search_transcripts(self.store, query, 200, prefix):
                    path = transcript_path(t["ns"], t["source"])
                    if t["ns"] and grant.can_read(path):
                        out.append({"id": f"{TID}{t['rid']}", "path": path, "title": f"{t['agent']}@{t['host']}",
                                    "kind": "transcript", "via": "fts"})
                        if len(out) == limit:
                            break
            return out
        return self._run("search", {"query": query, "limit": limit, "namespace": namespace}, run)

    def get(self, id: str) -> dict[str, Any]:
        def run(grant: Grant) -> dict[str, Any]:
            if id.startswith(TID):
                return self._transcript(grant, id)
            mem = self.store.by_id(id)
            if mem is None or not self._readable(grant, mem):
                raise Refused(f"no memory {id} you can read")  # same answer for missing and denied
            head = self.store.db.execute("SELECT author, origin, hlc FROM version WHERE id=?", (mem.head,)).fetchone()
            return {"id": mem.id, "path": self._path(mem), "title": mem.title, "kind": mem.kind, "tier": mem.tier,
                    "text": self.store.text(mem.id), "version": mem.head, "author": head["author"],
                    "node": head["origin"], "time": _iso(wall_ms(head["hlc"]) / 1000)}
        return self._run("get", {"id": id}, run)

    def _transcript(self, grant: Grant, id: str) -> dict[str, Any]:
        rid = id[len(TID):]
        row = self.store.transcript(int(rid)) if rid.isdigit() else None
        if row is None or not row["ns"] or not grant.can_read(transcript_path(row["ns"], row["source"])):
            raise Refused(f"no transcript {id} you can read")
        text = self.store.read_blob(row["blob"]).decode(errors="replace")
        return {"id": id, "path": transcript_path(row["ns"], row["source"]), "kind": "transcript", "agent": row["agent"],
                "node": row["host"], "text": text[:TRANSCRIPT_MAX], "truncated": len(text) > TRANSCRIPT_MAX}

    def list(self, namespace_prefix: str = "", limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), LIST_MAX))

        def run(grant: Grant) -> list[dict[str, Any]]:
            out = []
            for m in self.store.memories(namespace_prefix):
                if self._readable(grant, m):
                    out.append({"id": m.id, "path": self._path(m), "title": m.title, "kind": m.kind,
                                "updated": _iso(m.updated)})
                    if len(out) == limit:
                        return out
            if grant.transcripts:
                rows = self.store.db.execute("SELECT rid, ns, source, agent, ended FROM transcript WHERE ns LIKE ? "
                                             "ESCAPE '\\' ORDER BY ns, source", (search_mod._like(namespace_prefix),))
                for t in rows:
                    path = transcript_path(t["ns"], t["source"])
                    if t["ns"] and grant.can_read(path):
                        out.append({"id": f"{TID}{t['rid']}", "path": path, "title": t["agent"], "kind": "transcript"})
                        if len(out) == limit:
                            break
            return out
        return self._run("list", {"namespace_prefix": namespace_prefix, "limit": limit}, run)

    def put(self, key: str, content: str) -> dict[str, Any]:
        size = len(content.encode())

        def run(grant: Grant) -> dict[str, Any]:
            if not KEY.match(key) or len(key) > 200:
                raise Refused("bad key: use letters, digits, '.', '_', '-' and '/' between segments (max 200)")
            path = f"agent/{self.agent}/{key}"
            if not grant.can_write(path):
                raise Refused(f"no write grant for {path}")
            if size > PUT_MAX:
                raise Refused(f"content is {size} bytes; the limit is {PUT_MAX}")
            r = scrub.scrub(content, self.known_secrets)
            if r.secrets:
                kinds = ", ".join(f"{k} x{n}" for k, n in sorted(r.secrets.items()))
                raise Refused(f"refused: the content holds credentials ({kinds}). Remove them and put again; "
                              "nothing was stored")
            ns = f"agent/{self.agent}"
            before = self.store.get(ns, key)
            title = next((ln.strip("# ").strip() for ln in r.text.splitlines() if ln.strip()), key)[:120]
            vid = self.store.put(ns, key, r.text, author=f"agent:{self.agent}", kind="fact", tier="hot", title=title,
                                 reason=f"mcp put by {self.agent} on {self.store.node} at {_iso(time.time())}")
            mem = self.store.get(ns, key)
            return {"id": mem.id, "path": path, "version": vid, "bytes": len(r.text.encode()),
                    "pii_redacted": sum(r.pii.values()),
                    "unchanged": before is not None and before.head == vid and not before.deleted}
        return self._run("put", {"key": key, "length": size}, run)


def build_server(gate: Gate) -> FastMCP:
    server = FastMCP("gieok", instructions=(
        f"Gieok (기억), the staff's shared memory. You are {gate.agent}. Paths are <namespace>/<key>: "
        "agent/<name>/... (one agent's memory), project/<repo>/NOTES (a repo's NOTES.md), shared/... (everyone's). "
        f"You see only what your grant allows. put writes agent/{gate.agent}/<key> as a new version; nothing is "
        "ever deleted. Never put credentials: a put that holds one is refused."))

    @server.tool(structured_output=True)
    def search(query: str, limit: int = 10, namespace: str | None = None) -> list[dict[str, Any]]:
        """Search the memories you may read. `namespace` is a path prefix, like "agent/daepyo" or "project/"."""
        return gate.search(query, limit, namespace)

    @server.tool(structured_output=True)
    def get(id: str) -> dict[str, Any]:
        """Read one memory (the current version) by the id that search or list returned."""
        return gate.get(id)

    @server.tool(name="list", structured_output=True)
    def list_(namespace_prefix: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """List memories under a path prefix, like "project/" or "agent/daepyo/"."""
        return gate.list(namespace_prefix, limit)

    @server.tool(structured_output=True)
    def put(key: str, content: str) -> dict[str, Any]:
        """Write your own memory agent/<you>/<key> as a new version (at most 32 KB). Same content is a no-op."""
        return gate.put(key, content)

    return server
