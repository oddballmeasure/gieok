"""Per-agent grants, read from `<home>/grants.toml`. Monty edits that file by hand; nothing in Gieok writes it.

    [agents.jangin]
    read = ["agent/daepyo/**", "agent/jangin/**", "project/**", "shared/**"]
    write = ["agent/jangin/**"]
    transcripts = false

Globs match `<ns>/<key>` paths: `*` and `?` stay inside one path segment, `**` crosses segments.
`transcripts/**` is readable only with `transcripts = true`, whatever the read globs say.
Fail closed: a missing, unreadable, malformed, group- or world-writable file, or one owned by another
user, refuses every agent. So does an agent the file doesn't name. The file is re-read when it changes,
so a revoked grant takes effect on the next call."""

import os
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

AGENT_NAME = re.compile(r"\A[a-z][a-z0-9-]{0,31}\Z")
FIELDS = {"read", "write", "transcripts"}


class Refused(Exception):
    """A call the grants don't allow. The message is safe to show the agent."""


@lru_cache(maxsize=256)
def _compile(glob: str) -> re.Pattern:
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, globs: tuple[str, ...]) -> bool:
    return any(_compile(g).match(path) for g in globs)


@dataclass(frozen=True)
class Grant:
    agent: str
    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()
    transcripts: bool = False

    def can_read(self, path: str) -> bool:
        if path.startswith("transcripts/") and not self.transcripts:
            return False
        return matches(path, self.read)

    def can_write(self, path: str) -> bool:
        return not path.startswith(("transcripts/", "system/")) and matches(path, self.write)


def _strings(agent: str, name: str, value) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise Refused(f"grants.toml: agents.{agent}.{name} must be a list of non-empty strings")
    return tuple(value)


def parse(text: str) -> dict[str, Grant]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Refused(f"grants.toml does not parse: {exc}") from None
    agents = data.get("agents", {})
    if set(data) - {"agents"} or not isinstance(agents, dict):
        raise Refused("grants.toml: only an [agents.<name>] table per agent is allowed")
    out = {}
    for name, g in agents.items():
        if not AGENT_NAME.match(name) or not isinstance(g, dict) or set(g) - FIELDS:
            raise Refused(f"grants.toml: bad entry agents.{name}")
        transcripts = g.get("transcripts", False)
        if not isinstance(transcripts, bool):
            raise Refused(f"grants.toml: agents.{name}.transcripts must be true or false")
        out[name] = Grant(name, _strings(name, "read", g.get("read", [])), _strings(name, "write", g.get("write", [])),
                          transcripts)
    return out


class Access:
    """One agent's view of grants.toml, re-read whenever the file changes."""

    def __init__(self, path: Path, agent: str):
        self.path = Path(path)
        self.agent = agent
        self._seen: tuple | None = None
        self._grants: dict[str, Grant] | Refused = Refused("grants not loaded")

    def _load(self) -> dict[str, Grant]:
        try:
            st = self.path.stat()
        except OSError:
            raise Refused(f"no grants file at {self.path}") from None
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_mode, st.st_uid)
        if stamp != self._seen:
            try:
                if st.st_uid not in (os.getuid(), 0):
                    raise Refused("grants.toml is owned by another user")
                if st.st_mode & 0o022:
                    raise Refused("grants.toml is group- or world-writable")
                self._grants = parse(self.path.read_text())
            except Refused as exc:
                self._grants = exc
            except OSError as exc:
                self._grants = Refused(f"grants.toml unreadable: {exc.strerror}")
            self._seen = stamp
        if isinstance(self._grants, Refused):
            raise self._grants
        return self._grants

    def grant(self) -> Grant:
        if not AGENT_NAME.match(self.agent):
            raise Refused("bad agent name")
        grant = self._load().get(self.agent)
        if grant is None:
            raise Refused(f"agent {self.agent!r} has no grant")
        return grant
