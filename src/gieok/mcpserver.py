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
# Known token prefixes. A key split by whitespace ("sk-ant-abc def…", or across lines) is still a key.
# The prefix regex has no quantifiers and every later step is bounded, so the scan stays linear (split_keys).
KEY_PREFIX = re.compile(r"(?<![A-Za-z0-9])(?:sk-|gh[pousr]_|github_pat_|xox[abprs]-|AKIA|ASIA|sntry[su]_|[sr]k_(?:live|test)_)")
PIECE = re.compile(r"[A-Za-z0-9_-]{1,256}")
GAP = re.compile(r"\s{1,16}")
MAX_PIECES = 9
FIELDS = {"key", "content", "id", "query", "limit", "namespace", "namespace_prefix"}
_audited: contextvars.ContextVar[bool] = contextvars.ContextVar("gieok_audited", default=False)


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


def _clip(value):
    if isinstance(value, str):  # scrubbed too: a credential in a query or an id never reaches the audit log
        short = value if len(value) <= CLIP else value[:CLIP] + f"…[{len(value)} chars]"
        return scrub.scrub(short).text
    if isinstance(value, dict):
        return {_clip(str(k)): _clip(v) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in value[:20]]
    if value is None or isinstance(value, (bool, float)) or (isinstance(value, int) and abs(value) < 10**18):
        return value
    return _clip(repr(value))


def normalize_map(text: str) -> tuple[str, list[int] | None]:
    """(scan text, origin): each character NFKC-normalized, format characters (zero-width and the like)
    dropped, so look-alike or split keys can't hide. origin[j] is the index in `text` that scan character j
    came from (None when nothing changed). Expansions longer than 3 characters (like U+FDFA) are kept as
    they were: they can't spell an ASCII key, and keeping them bounds the scan text.
    For scanning only: it changes Korean jamo, ①, ligatures and ZWJ, so it is never stored."""
    if text.isascii():
        return text, None
    out, origin = [], []
    for i, ch in enumerate(text):
        if ch.isascii():
            out.append(ch)
            origin.append(i)
            continue
        n = unicodedata.normalize("NFKC", ch)
        for c in (ch if len(n) > 3 else n):
            if unicodedata.category(c) != "Cf":
                out.append(c)
                origin.append(i)
    return "".join(out), origin


def normalize(text: str) -> str:
    return normalize_map(text)[0]


def pii_spans(text: str) -> list[tuple[int, int]]:
    """Where the scrubber's PII patterns match in text (the spans it would redact)."""
    spans = [m.span(2) for m in scrub.PII_FIELD.finditer(text) if m.group(2) != scrub.REDACTED]
    for pattern in (scrub.EMAIL, scrub.STRIPE_CUSTOMER, scrub.IPV4, scrub.PHONE):
        spans += [m.span() for m in pattern.finditer(text)]
    for m in scrub.CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if scrub._luhn(digits) and len(set(digits)) > 1:
            spans.append(m.span())
    return spans


def uncovered_pii(original: str, scanned: str, origin: list[int]) -> bool:
    """True if some PII the normalized scan finds is not wholly inside what redacting the original removes:
    "jo\\u200bhn.smith@example.com" redacts only "hn.smith@…" in the original and would leave "jo" behind."""
    covered = bytearray(len(original))
    for s, e in pii_spans(original):
        covered[s:e] = b"\x01" * (e - s)
    for s, e in pii_spans(scanned):
        if e > s and not all(covered[origin[s]: origin[e - 1] + 1]):
            return True
    return False


def split_keys(text: str) -> list[str]:
    """Candidates rejoined from a key prefix plus up to 8 more pieces separated by any whitespace. Linear:
    each step is bounded, and the scan resumes after the span it just read."""
    out, pos = [], 0
    while (m := KEY_PREFIX.search(text, pos)) is not None:
        end = m.end()
        first = PIECE.match(text, end)
        end = first.end() if first else end
        pieces = [text[m.start():end]]
        while len(pieces) < MAX_PIECES and (gap := GAP.match(text, end)) and (piece := PIECE.match(text, gap.end())):
            pieces.append(piece.group(0))
            end = piece.end()
        if len(pieces) > 1:
            joined = "".join(pieces)
            if any(ch.isdigit() for ch in joined):  # real keys carry digits; "sk-learn is a library" doesn't
                out.append(joined)
        pos = max(end, m.start() + 1)
    return out


def scan(text: str, known: list[str] = ()) -> tuple[Counter, scrub.Result]:
    """(credential kinds, the scrub result) for text, counting keys broken up by whitespace too."""
    result = scrub.scrub(text, known)
    kinds = Counter(result.secrets)
    joined = split_keys(text)
    if joined:  # one scrub pass over every candidate; pieces hold no whitespace, so lines can't merge
        kinds.update(scrub.scrub("\n".join(joined)).secrets)
    return kinds, result


def secret_kinds(text: str, known: list[str] = ()) -> Counter:
    return scan(text, known)[0]


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
            # Scan the normalized text, store the original: NFKC would corrupt Korean jamo, ①, ligatures, ZWJ.
            key_kinds = secret_kinds(key) | secret_kinds(re.sub(r"[/.]", "", key))
            if key_kinds:  # the key is stored and audited too: it must not carry a credential either
                raise Refused(f"refused: the key holds credentials ({', '.join(sorted(key_kinds))}); "
                              "nothing was stored")
            scanned, origin = normalize_map(content)
            kinds, seen = scan(scanned, self.known_secrets)
            r = seen  # when normalizing changed nothing, the scan result is the original's
            if scanned != content:
                more, r = scan(content, self.known_secrets)
                kinds |= more
            if kinds:
                listed = ", ".join(f"{k} x{n}" for k, n in sorted(kinds.items()))
                raise Refused(f"refused: the content holds credentials ({listed}). Remove them and put again; "
                              "nothing was stored")
            # r: the original, with PII as [redacted]. PII the normalized scan finds must be wholly inside the
            # original's redacted spans, or some of it would be stored in clear.
            if scanned != content and uncovered_pii(content, scanned, origin):
                raise Refused("refused: the content holds personal data written with look-alike or invisible "
                              "characters. Write it plainly (it will be redacted) or leave it out; nothing was stored")
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


def rejection(exc: Exception) -> str:
    """A fixed message for a call the SDK refused: the error type and the field names only. Never the
    message text, which quotes the rejected input."""
    cause, depth = exc, 0
    while cause is not None and not callable(getattr(cause, "errors", None)) and depth < 10:
        cause, depth = cause.__cause__ or cause.__context__, depth + 1
    if cause is not None and not callable(getattr(cause, "errors", None)):
        cause = None
    if cause is None:
        return f"{type(exc).__name__}: call rejected before the tool ran"
    fields = set()
    try:
        for err in cause.errors():
            loc = err.get("loc") or ("?",)
            fields.add(str(loc[0]) if str(loc[0]) in FIELDS else "?")
    except Exception:
        fields.add("?")
    return f"{type(cause).__name__}: invalid arguments ({', '.join(sorted(fields)) or '?'})"


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
                raw = arguments if isinstance(arguments, dict) else {}
                # Known argument names only, simple values only: other names and nested values are caller text
                args = {k: (v if isinstance(v, (str, int, float, bool, type(None))) else type(v).__name__)
                        for k, v in raw.items() if k in FIELDS and k != "content"}
                if "content" in raw:  # never log put content
                    args["length"] = len(raw["content"]) if isinstance(raw["content"], str) else None
                extra = sum(1 for k in raw if k not in FIELDS)
                if extra:
                    args["unknown_args"] = extra
                self.gate.audit(name, args, error=rejection(exc))
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
