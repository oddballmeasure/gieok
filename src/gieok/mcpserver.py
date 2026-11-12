"""`gieok mcp --agent <name>`: a stdio MCP server that gives one agent its granted slice of the store.

Tools: search, get, list, put. There is no forget or delete. Every result passes the agent's read grant
(grants.py), search hits included; put writes only `agent/<agent>/<key>` and only inside the write grant.

Audit: every call, allowed or refused, appends a line to `<home>/audit/<agent>.jsonl`, written in a `finally`
whatever the grant check says. Calls from an agent the grants don't name (or while grants.toml is unusable)
go to `audit/_refused.jsonl`, so a stranger can't create files. A put writes an "intent" line before the
store write and the result line after. Calls FastMCP rejects before a tool runs (bad arguments, unknown
tool) are audited by GieokMCP.call_tool.

Identity: the agent name is whatever `--agent` says. A remote agent must reach this only through a
forced-command ssh key that pins `--agent` (README, "Remote agents")."""

import contextvars
import json
import os
import re
import time
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from gieok import scrub
from gieok import search as search_mod
from gieok.grants import AGENT_NAME, Access, Grant, Refused
from gieok.hlc import wall_ms
from gieok.namespaces import transcript_path
from gieok.store import Forgotten, Memory, Store

PUT_MAX = 32 * 1024
TRANSCRIPT_MAX = 256 * 1024  # characters of a transcript `get` returns
LIST_MAX = 500
SEARCH_MAX = 50
CLIP = 500  # characters of any string the audit log keeps
KEY = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*\Z")
TID = "transcript:"
REFUSED_LOG = "_refused"
# A known token prefix followed by pieces split by spaces: "sk-ant-abc def…" is still a key.
SPLIT_KEY = re.compile(r"(?<![A-Za-z0-9])(?:sk-|gh[pousr]_|github_pat_|xox[abprs]-|AKIA|ASIA|sntry[su]_|[sr]k_(?:live|test)_)"
                       r"[A-Za-z0-9_-]*(?:[ \t ]+[A-Za-z0-9_-]+){1,8}")

INPUT_VALUE = re.compile(r"input_value=.*?, input_type=", re.S)
_audited: contextvars.ContextVar[bool] = contextvars.ContextVar("gieok_audited", default=False)


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


def _clip(value):
    if isinstance(value, str):
        return value if len(value) <= CLIP else value[:CLIP] + f"…[{len(value)} chars]"
    if isinstance(value, dict):
        return {str(k)[:CLIP]: _clip(v) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in value[:20]]
    if value is None or isinstance(value, (bool, float)) or (isinstance(value, int) and abs(value) < 10**18):
        return value
    return _clip(repr(value))


def normalize(text: str) -> str:
    """NFKC, minus format characters (zero-width and the like), so look-alike or split keys can't hide."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text) if unicodedata.category(ch) != "Cf")


def secret_kinds(text: str, known: list[str] = ()) -> Counter:
    """Credential kinds in already-normalized text, including keys broken up by whitespace."""
    kinds = Counter(scrub.scrub(text, known).secrets)
    for m in SPLIT_KEY.finditer(text):
        joined = re.sub(r"\s+", "", m.group(0))
        if re.search(r"\d", joined):  # real keys carry digits; "sk-learn is a library" doesn't
            kinds.update(scrub.scrub(joined).secrets)
    return kinds


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

    def _granted(self) -> bool:
        try:
            self.access.grant()
            return True
        except Refused:
            return False

    def audit(self, tool, args: dict, *, count: int | None = None, error: str | None = None,
              phase: str | None = None) -> None:
        self.audit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = self.agent if AGENT_NAME.match(self.agent) and self._granted() else REFUSED_LOG
        line = {"time": _iso(time.time()), "agent": _clip(self.agent), "node": self.store.node, "tool": _clip(tool),
                "args": _clip(args)}
        if phase:
            line["phase"] = phase
        line.update({"error": _clip(error)} if error is not None else {"count": count})
        data = (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8", errors="replace")
        fd = os.open(self.audit_dir / f"{name}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        _audited.set(True)

    def _run(self, tool: str, logged: dict, fn):
        result, error = None, "interrupted"
        try:
            result = fn(self.access.grant(), logged)
            error = None
            return result
        except Refused as exc:
            error = str(exc)
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            count = len(result) if isinstance(result, list) else 1
            self.audit(tool, logged, count=None if error else count, error=error)

    # Helpers

    @staticmethod
    def _path(mem: Memory) -> str:
        return f"{mem.ns}/{mem.key}"

    def _readable(self, grant: Grant, mem: Memory) -> bool:
        return not mem.deleted and grant.can_read(self._path(mem))

    def _allowed_rids(self, grant: Grant, prefix: str) -> set[int]:
        rows = self.store.db.execute("SELECT rid, ns, key FROM memory WHERE deleted=0")
        return {r["rid"] for r in rows if r["ns"].startswith(prefix) and grant.can_read(f"{r['ns']}/{r['key']}")}

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
        def run(grant: Grant, logged: dict) -> list[dict[str, Any]]:
            n = max(1, min(int(limit), SEARCH_MAX))
            prefix = namespace or ""
            allowed = self._allowed_rids(grant, prefix)
            hits = search_mod.search(self.store, query, n, prefix, self._embed(), allowed=allowed,
                                     pool=max(50, n * 5)) if allowed else []
            out = [{"id": h.memory.id, "path": self._path(h.memory), "title": h.memory.title, "kind": h.memory.kind,
                    "via": h.via, "snippet": h.snippet} for h in hits]
            if grant.transcripts and len(out) < n:
                for t in search_mod.search_transcripts(self.store, query, 200, prefix):
                    path = transcript_path(t["ns"], t["source"])
                    if t["ns"] and grant.can_read(path):
                        out.append({"id": f"{TID}{t['rid']}", "path": path, "title": f"{t['agent']}@{t['host']}",
                                    "kind": "transcript", "via": "fts"})
                        if len(out) == n:
                            break
            return out
        return self._run("search", {"query": query, "limit": limit, "namespace": namespace}, run)

    def get(self, id: str) -> dict[str, Any]:
        def run(grant: Grant, logged: dict) -> dict[str, Any]:
            if id.startswith(TID):
                return self._transcript(grant, id)
            mem = self.store.by_id(id)
            if mem is None or not self._readable(grant, mem):
                raise Refused("no memory with that id that you can read")  # same answer for missing and denied
            head = self.store.db.execute("SELECT author, origin, hlc FROM version WHERE id=?", (mem.head,)).fetchone()
            return {"id": mem.id, "path": self._path(mem), "title": mem.title, "kind": mem.kind, "tier": mem.tier,
                    "text": self.store.text(mem.id), "version": mem.head, "author": head["author"],
                    "node": head["origin"], "time": _iso(wall_ms(head["hlc"]) / 1000)}
        return self._run("get", {"id": id}, run)

    def _transcript(self, grant: Grant, id: str) -> dict[str, Any]:
        rid = id[len(TID):]
        row = self.store.transcript(int(rid)) if rid.isascii() and rid.isdigit() else None
        if row is None or not row["ns"] or not grant.can_read(transcript_path(row["ns"], row["source"])):
            raise Refused("no transcript with that id that you can read")
        text = self.store.read_blob(row["blob"]).decode(errors="replace")
        return {"id": id, "path": transcript_path(row["ns"], row["source"]), "kind": "transcript", "agent": row["agent"],
                "node": row["host"], "text": text[:TRANSCRIPT_MAX], "truncated": len(text) > TRANSCRIPT_MAX}

    def list(self, namespace_prefix: str = "", limit: int = 50) -> list[dict[str, Any]]:
        def run(grant: Grant, logged: dict) -> list[dict[str, Any]]:
            n = max(1, min(int(limit), LIST_MAX))
            out = []
            for m in self.store.memories(namespace_prefix):
                if self._readable(grant, m):
                    out.append({"id": m.id, "path": self._path(m), "title": m.title, "kind": m.kind,
                                "updated": _iso(m.updated)})
                    if len(out) == n:
                        return out
            if grant.transcripts:
                rows = self.store.db.execute("SELECT rid, ns, source, agent, ended FROM transcript WHERE ns LIKE ? "
                                             "ESCAPE '\\' ORDER BY ns, source", (search_mod._like(namespace_prefix),))
                for t in rows:
                    path = transcript_path(t["ns"], t["source"])
                    if t["ns"] and grant.can_read(path):
                        out.append({"id": f"{TID}{t['rid']}", "path": path, "title": t["agent"], "kind": "transcript"})
                        if len(out) == n:
                            break
            return out
        return self._run("list", {"namespace_prefix": namespace_prefix, "limit": limit}, run)

    def put(self, key: str, content: str) -> dict[str, Any]:
        def run(grant: Grant, logged: dict) -> dict[str, Any]:
            if not isinstance(content, str):
                raise Refused("content must be text")
            try:
                size = len(content.encode())
            except UnicodeEncodeError:
                logged["length"] = len(content)
                raise Refused("content is not valid Unicode text (lone surrogates)") from None
            logged["length"] = size
            if not isinstance(key, str) or not KEY.match(key) or len(key) > 200:
                raise Refused("bad key: use letters, digits, '.', '_', '-' and '/' between segments (max 200)")
            path = f"agent/{self.agent}/{key}"
            if not grant.can_write(path):
                raise Refused(f"no write grant for {path}")
            if size > PUT_MAX:
                raise Refused(f"content is {size} bytes; the limit is {PUT_MAX}")
            text = normalize(content)
            kinds = secret_kinds(text, self.known_secrets)
            if kinds:
                listed = ", ".join(f"{k} x{n}" for k, n in sorted(kinds.items()))
                raise Refused(f"refused: the content holds credentials ({listed}). Remove them and put again; "
                              "nothing was stored")
            r = scrub.scrub(text, self.known_secrets)  # PII becomes [redacted]
            ns = f"agent/{self.agent}"
            before = self.store.get(ns, key)
            if before is not None and before.deleted:
                raise Refused("this key was forgotten; use a new key")
            title = next((ln.strip("# ").strip() for ln in r.text.splitlines() if ln.strip()), key)[:120]
            self.audit("put", logged, phase="intent")
            try:
                vid = self.store.put(ns, key, r.text, author=f"agent:{self.agent}", kind="fact", tier="hot",
                                     title=title, revive=False,
                                     reason=f"mcp put by {self.agent} on {self.store.node} at {_iso(time.time())}")
            except Forgotten:
                raise Refused("this key was forgotten; use a new key") from None
            mem = self.store.get(ns, key)
            return {"id": mem.id, "path": path, "version": vid, "bytes": len(r.text.encode()),
                    "pii_redacted": sum(r.pii.values()), "unchanged": before is not None and before.head == vid}
        return self._run("put", {"key": key, "length": None}, run)


class GieokMCP(FastMCP):
    """Audits calls FastMCP fails before any tool body runs: argument validation and unknown tools."""

    def __init__(self, gate: Gate, **kw):
        super().__init__(**kw)
        self.gate = gate

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        token = _audited.set(False)
        try:
            return await super().call_tool(name, arguments)
        except Exception as exc:
            if not _audited.get():
                args = dict(arguments or {})
                if "content" in args:  # never log put content
                    c = args.pop("content")
                    args["length"] = len(c) if isinstance(c, str) else None
                if name == "put" and not isinstance(args.get("key"), (str, type(None))):
                    args["key"] = type(args["key"]).__name__
                # pydantic quotes the rejected value (input_value=...): it may be put content, so hide it
                msg = INPUT_VALUE.sub("input_value=<hidden>, input_type=", str(exc))
                self.gate.audit(name, args, error=f"{type(exc).__name__}: {msg}")
            raise
        finally:
            _audited.reset(token)


def build_server(gate: Gate) -> FastMCP:
    server = GieokMCP(gate, name="gieok", instructions=(
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
