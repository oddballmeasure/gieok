"""Where each kind of memory lives. Grants (grants.py) match globs against `<ns>/<key>` paths.

- `agent/daepyo/<file>`: Claude auto-memory of the oscar project (Daepyo's own)
- `agent/<project-slug>/<file>`: every other Claude project's auto-memory
- `agent/<agent>/<key>`: what an agent writes through MCP
- `project/<repo>/NOTES`: a repo's NOTES.md
- `transcripts/<project-slug>/<session>`: raw Claude Code and Codex sessions (cold tier)
- `shared/…`: everyone's; `system/…`: Gieok's own, never written by an agent"""

import re
from pathlib import Path

# Claude project slug -> agent, where the project is an agent's home rather than its own namespace.
AGENT_FOR_SLUG = {"oscar": "daepyo"}
NOTES_KEY = "NOTES"
_HOME = re.compile(r"\A-(?:home|Users)-[^-]+")
_BAD = re.compile(r"[^a-z0-9._-]+")


def slug(project: str) -> str:
    """`-home-monty-koizler` -> `koizler`; `-home-monty` -> `home`; a Codex cwd name -> itself, cleaned."""
    name = _HOME.sub("", project) if project.startswith("-") else project
    name = _BAD.sub("-", name.lower()).strip("-.")
    return re.sub(r"-{2,}", "-", name) or "home"


def memory_ns(project_dir: str) -> str:
    s = slug(project_dir)
    return f"agent/{AGENT_FOR_SLUG.get(s, s)}"


def notes_ns(repo: str) -> str:
    return f"project/{repo}"


def transcript_ns(project: str) -> str:
    return f"transcripts/{slug(project)}"


def transcript_path(ns: str, source: str) -> str:
    return f"{ns}/{Path(source).stem}"


def ingest_target(source: str) -> tuple[str, str] | None:
    """(ns, key) an ingested file belongs at today, from its source path. None if it isn't an ingested file."""
    p = Path(source)
    if p.name == "NOTES.md":
        return notes_ns(p.parent.name), NOTES_KEY
    if p.suffix == ".md" and p.parent.name == "memory":
        return memory_ns(p.parent.parent.name), p.stem
    return None
