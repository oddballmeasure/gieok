"""Claude Code auto-memory directories and NOTES.md files: the hot tier."""

import re
from pathlib import Path

from gieok.ingest import Context, Report
from gieok.namespaces import NOTES_KEY, memory_ns, notes_ns
from gieok.store import Store

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
# Claude project dir name -> agent, for transcripts. Daepyo runs every other Claude session on the oscar host.
# Memory namespaces come from namespaces.memory_ns instead.
AGENTS = {"-home-monty-oscar": "daepyo", "-home-monty-bora": "bora", "-home-monty-bora-svc": "bora",
          "-home-monty-haechi": "haechi", "-home-monty-saekdong": "saekdong"}


def agent_for(project_dir: str, default: str = "daepyo") -> str:
    return AGENTS.get(project_dir, default)


def _front(text: str) -> dict[str, str]:
    m = FRONTMATTER.match(text)
    out = {}
    if m:
        for line in m.group(1).splitlines():
            key, sep, value = line.strip().partition(":")
            if sep and value.strip():
                out[key.strip()] = value.strip()
    return out


def ingest_claude_memory(store: Store, ctx: Context, projects: Path | None = None) -> Report:
    report = Report()
    root = projects or ctx.home / ".claude" / "projects"
    for path in sorted(root.glob("*/memory/*.md")):
        report.seen += 1
        ns = memory_ns(path.parent.parent.name)
        agent = ns.split("/", 1)[1]
        raw = path.read_text()
        report.raw_bytes += len(raw.encode())
        text = ctx.clean(raw, str(path), report)
        front = _front(raw)
        kind = front.get("type") or ("index" if path.name == "MEMORY.md" else "fact")
        title = front.get("description") or front.get("name") or path.stem
        before = store.get(ns, path.stem)
        vid = store.put(ns, path.stem, text, author=f"ingest:{agent}", kind=kind, title=title[:200],
                        reason=f"imported from {path}")
        if before is None or before.head != vid:
            report.stored += 1
        else:
            report.skipped += 1
    return report


def ingest_notes(store: Store, ctx: Context, roots: list[Path] | None = None) -> Report:
    report = Report()
    for path in sorted(roots or ctx.home.glob("*/NOTES.md")):
        report.seen += 1
        raw = path.read_text()
        report.raw_bytes += len(raw.encode())
        repo = path.parent.name
        before = store.get(notes_ns(repo), NOTES_KEY)
        vid = store.put(notes_ns(repo), NOTES_KEY, ctx.clean(raw, str(path), report), author="ingest",
                        kind="notes", title=f"{repo} NOTES.md", reason=f"imported from {path}")
        if before is None or before.head != vid:
            report.stored += 1
        else:
            report.skipped += 1
    return report
