"""Each agent's health, current work and usage, read from files and the staff bus. Read-only."""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from gieok.web.config import Agent
from gieok.web.work import ro

STALE_S = 180  # the staff bus's own threshold (staff.py STALE_S)


@dataclass
class Status:
    name: str
    harness: str
    role: str
    here: bool  # its state dir is on this host
    service: str | None
    service_state: str  # active, inactive, failed, unknown
    heartbeat: float | None
    healthy: bool
    current: list[dict]  # open staff tasks and busy threads
    usage: list[dict]  # {"label", "used" (0..1), "resets_at"}


async def service_state(unit: str | None) -> str:
    if not unit:
        return "unknown"
    try:
        proc = await asyncio.create_subprocess_exec("systemctl", "--user", "is-active", unit,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
    except OSError:
        return "unknown"
    return out.decode().strip() or "unknown"


def claude_usage(state_dir: Path) -> list[dict]:
    path = state_dir / "usage.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    names = {"five_hour": "5h", "seven_day": "week"}
    return [{"label": f"Claude {names.get(k, k)}", "used": w.get("utilization"), "resets_at": w.get("resetsAt")}
            for k, w in (data.get("windows") or {}).items() if w.get("utilization") is not None]


def codex_usage(state_dir: Path) -> list[dict]:
    path = state_dir / "limits.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    out = []
    for key in ("primary", "secondary"):
        w = data.get(key) or {}
        if w.get("usedPercent") is None:
            continue
        mins = w.get("windowDurationMins") or 0
        label = "week" if mins >= 7 * 24 * 60 else f"{mins // 60}h" if mins else key
        out.append({"label": f"Codex {label}", "used": w["usedPercent"] / 100, "resets_at": w.get("resetsAt")})
    return out


def busy_threads(state_dir: Path) -> list[dict]:
    store = state_dir / "store.json"
    if not store.exists():
        return []
    return [current("session", (r.get("title") or "").splitlines()[0][:120] if r.get("title") else "(untitled)",
                    project=r.get("project"), thread=int(k)) for k, r in json.loads(store.read_text()).items()
            if r.get("busy")]


def current(kind: str, title: str, **fields) -> dict:
    """One in-progress item, with every key the templates read (CONTEXT.md)."""
    return {"kind": kind, "title": title, "id": None, "state": None, "project": None, "pr": None, "updated": None,
            "thread": None, **fields}


def bus_rows(staff_db: Path) -> tuple[dict, dict]:
    """({name: employee row}, {name: [open tasks]})."""
    if not staff_db.exists():
        return {}, {}
    db = ro(staff_db)
    try:
        people = {r["name"]: dict(r) for r in db.execute("SELECT * FROM employees")}
        open_ = {}
        for t in db.execute("SELECT id, assignee, state, repo, brief, pr_url, updated FROM tasks "
                            "WHERE state IN ('queued', 'claimed', 'working', 'blocked', 'review') ORDER BY id"):
            open_.setdefault(t["assignee"], []).append(
                current("task", (t["brief"].strip().splitlines() or [""])[0][:120], id=t["id"], state=t["state"],
                        project=t["repo"], pr=t["pr_url"], updated=t["updated"]))
        return people, open_
    finally:
        db.close()


async def statuses(agents: tuple[Agent, ...], staff_db: Path, now: float | None = None) -> list[Status]:
    now = now or time.time()
    people, open_ = bus_rows(staff_db)
    out = []
    for a in agents:
        row = people.get(a.name, {})
        beat = row.get("heartbeat")
        state = await service_state(a.service)
        here = a.state_dir.is_dir()
        usage = (claude_usage if a.harness == "claude" else codex_usage)(a.state_dir) if here else []
        out.append(Status(a.name, a.harness, row.get("role", ""), here, a.service, state, beat,
                          bool(beat and now - beat < STALE_S) or (not row and state == "active"),
                          open_.get(a.name, []) + (busy_threads(a.state_dir) if here else []), usage))
    return out
