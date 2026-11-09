"""Staff HQ settings, from ~/gieok/.env. Missing or malformed keys stop the site at start."""

import ipaddress
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


@dataclass(frozen=True)
class Agent:
    name: str
    harness: str  # "claude" or "codex": how to read its state dir
    state_dir: Path
    service: str | None  # systemd user unit, for health


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int
    public_url: str  # what Discord redirects back to, e.g. http://100.101.1.2:8740
    client_id: str
    client_secret: str
    guild_id: int
    allowed_role_ids: frozenset[int]
    gieok_root: Path
    node: str
    web_db: Path
    staff_db: Path
    agents: tuple[Agent, ...]
    github_owner: str
    webhook_url: str | None
    session_key: bytes

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url.rstrip('/')}/auth/callback"


def check_bind(host: str) -> None:
    """The site serves the tailnet only (and loopback, for tests). Anything else stops it."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise SystemExit(f"GIEOK_WEB_HOST must be an IP address, not {host!r}") from None
    if ip.is_loopback or ip in (TAILNET_V4 if ip.version == 4 else TAILNET_V6):
        return
    raise SystemExit(f"Refusing to serve on {host}: Staff HQ listens on a tailnet address (100.64.0.0/10) only")


def parse_agents(raw: str) -> tuple[Agent, ...]:
    """name:harness:state_dir[:service], comma separated."""
    agents = []
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        parts = entry.split(":")
        if len(parts) not in (3, 4) or parts[1] not in ("claude", "codex"):
            raise SystemExit(f"STAFF_AGENTS entry {entry!r} must look like name:claude|codex:/state/dir[:unit]")
        agents.append(Agent(parts[0], parts[1], Path(parts[2]).expanduser(), parts[3] if len(parts) == 4 else None))
    return tuple(agents)


def _require(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        raise SystemExit(f"Missing required setting {key} in .env")
    return value


def session_key(root: Path) -> bytes:
    """Signs login cookies. Made once and kept beside the store, readable by Monty only."""
    path = root / "web-session.key"
    if not path.exists():
        root.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
    return path.read_bytes()


DEFAULT_AGENTS = ("daepyo:claude:~/oscar/state:oscar.service,"
                  "saekdong:codex:~/saekdong/state:saekdong.service,"
                  "bora:claude:~/bora-agent/state:bora.service")


def load() -> WebConfig:
    load_dotenv()
    root = Path(os.getenv("GIEOK_ROOT", "~/.local/share/gieok")).expanduser()
    host = _require("GIEOK_WEB_HOST")
    check_bind(host)
    port = int(os.getenv("GIEOK_WEB_PORT", "8740"))
    return WebConfig(
        host=host, port=port,
        public_url=os.getenv("GIEOK_WEB_URL", "").strip() or f"http://{host}:{port}",
        client_id=_require("DISCORD_CLIENT_ID"),
        client_secret=_require("DISCORD_CLIENT_SECRET"),
        guild_id=int(_require("GUILD_ID")),
        allowed_role_ids=frozenset(int(r) for r in _require("ALLOWED_ROLE_IDS").split(",") if r.strip()),
        gieok_root=root,
        node=os.getenv("GIEOK_NODE", "oscar"),
        web_db=root / "web.db",
        staff_db=Path(os.getenv("STAFF_DB", "~/.local/state/staff/staff.db")).expanduser(),
        agents=parse_agents(os.getenv("STAFF_AGENTS", DEFAULT_AGENTS)),
        github_owner=os.getenv("GITHUB_OWNER", "oddballmeasure"),
        webhook_url=os.getenv("REPORTS_WEBHOOK_URL", "").strip() or None,
        session_key=session_key(root),
    )
