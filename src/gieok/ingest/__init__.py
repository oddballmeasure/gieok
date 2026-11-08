"""Ingesters: Claude auto-memory files, NOTES.md files, and Claude Code / Codex transcripts.
Everything is scrubbed (scrub.py) before it reaches the store."""

import socket
from dataclasses import dataclass, field
from pathlib import Path

from gieok import scrub


@dataclass
class Report:
    seen: int = 0
    stored: int = 0
    skipped: int = 0
    raw_bytes: int = 0
    new_bytes: int = 0
    redactions: int = 0
    live_secrets: list[str] = field(default_factory=list)  # "<source>: <kind>", for the ping to Monty
    errors: list[str] = field(default_factory=list)

    def add(self, other: "Report") -> "Report":
        for k in ("seen", "stored", "skipped", "raw_bytes", "new_bytes", "redactions"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.live_secrets += other.live_secrets
        self.errors += other.errors
        return self


@dataclass
class Context:
    host: str = field(default_factory=socket.gethostname)
    home: Path = field(default_factory=Path.home)
    known_secrets: list[str] = field(default_factory=list)

    def clean(self, text: str, source: str, report: Report) -> str:
        result = scrub.scrub(text, self.known_secrets)
        report.redactions += result.count
        report.live_secrets += [f"{source}: {kind}" for kind in result.live]
        return result.text


def settings_files(home: Path) -> list[Path]:
    """Settings files whose credential values the scrubber should look for verbatim."""
    names = [home / "oscar" / ".env", home / "saekdong" / ".env", home / "bora-svc" / ".env", home / "haechi" / ".env"]
    return [p for p in names if p.is_file()]
