"""Claude Code and Codex transcripts: the cold tier.

A transcript is stored whole (scrubbed), as deduplicated chunks. Only the conversation text (user and
assistant messages, not tool output) goes into the search index, which keeps the index small. A live
session that grew since the last pass re-chunks to the same chunks plus a new tail, so re-ingest costs
only the new bytes."""

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from gieok.chunk import digest
from gieok.ingest import Context, Report
from gieok.ingest.memory import agent_for
from gieok.namespaces import transcript_ns
from gieok.store import Store

INDEX_LIMIT = 400_000  # characters of message text indexed per transcript


def _ts(value) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _texts(content) -> Iterable[str]:
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "input_text", "output_text"):
                yield block.get("text") or ""


def claude_text(lines: list[dict]) -> str:
    out = []
    for o in lines:
        if o.get("type") in ("user", "assistant") and isinstance(o.get("message"), dict):
            out.extend(t for t in _texts(o["message"].get("content")) if t.strip())
    return "\n".join(out)


def codex_text(lines: list[dict]) -> str:
    out = []
    for o in lines:
        p = o.get("payload") or {}
        if o.get("type") == "response_item" and p.get("type") == "message" and p.get("role") in ("user", "assistant"):
            out.extend(t for t in _texts(p.get("content")) if t.strip())
    return "\n".join(out)


def _parse(text: str) -> list[dict]:
    lines = []
    for line in text.splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict):
            lines.append(o)
    return lines


def _one(store: Store, ctx: Context, path: Path, harness: str, agent: str, project: str, report: Report) -> None:
    st = path.stat()
    report.seen += 1
    if store.transcript_known(str(path), st.st_size, st.st_mtime):
        report.skipped += 1
        return
    raw = path.read_bytes()
    report.raw_bytes += len(raw)
    before = report.redactions
    clean = ctx.clean(raw.decode(errors="replace"), str(path), report)
    lines = _parse(clean)
    broken = len(_parse(raw.decode(errors="replace"))) - len(lines)
    if broken:  # a redaction must never break a JSON line; count it so verify and the report show it
        report.errors.append(f"{path}: {broken} JSON lines broken by redaction")
    text = (claude_text(lines) if harness == "claude" else codex_text(lines))[:INDEX_LIMIT]
    stamps = [t for t in (_ts(o.get("timestamp")) for o in lines) if t]
    report.new_bytes += store.put_transcript(
        source=str(path), host=ctx.host, agent=agent, project=project, ns=transcript_ns(project), harness=harness, data=clean.encode(),
        source_hash=digest(raw), source_size=st.st_size, source_mtime=st.st_mtime, redactions=report.redactions - before,
        text=text, started=min(stamps) if stamps else None, ended=max(stamps) if stamps else None)
    report.stored += 1
    store.seal()  # compresses the open pack once it reaches the target size


def ingest_claude(store: Store, ctx: Context, projects: Path | None = None, limit: int | None = None) -> Report:
    report = Report()
    root = projects or ctx.home / ".claude" / "projects"
    files = sorted(root.glob("*/*.jsonl")) + sorted(root.glob("*/*/subagents/*.jsonl"))
    for path in files[:limit]:
        project = path.relative_to(root).parts[0]
        try:
            _one(store, ctx, path, "claude", agent_for(project), project, report)
        except Exception as exc:  # one bad file must not stop the pass
            report.errors.append(f"{path}: {exc}")
    return report


def ingest_codex(store: Store, ctx: Context, sessions: Path | None = None, limit: int | None = None) -> Report:
    report = Report()
    root = sessions or ctx.home / ".codex" / "sessions"
    for path in sorted(root.glob("**/*.jsonl"))[:limit]:
        try:
            first = json.loads(path.open().readline() or "{}")
        except (json.JSONDecodeError, OSError):
            first = {}
        cwd = str((first.get("payload") or {}).get("cwd") or "")
        agent = "saekdong" if "/saekdong" in cwd else "codex"
        try:
            _one(store, ctx, path, "codex", agent, Path(cwd).name or "codex", report)
        except Exception as exc:
            report.errors.append(f"{path}: {exc}")
    return report
