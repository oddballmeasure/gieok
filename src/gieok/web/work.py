"""The agents' finished work, normalized into one table so pages and reports can query it.

Sources, all read-only:
- the staff bus (~/.local/state/staff/staff.db): tasks, with review rounds from their events
- each agent's state dir: store.json thread records plus activity/<thread>.log timestamps
  (Claude agents) or the same layout (Saekdong on Codex); scheduled runs in oscar.db
- GitHub: merged PRs on the owner's repos, attributed by the footer each agent writes

A work item is one finished (or failed) piece of work: a staff task, a session, a scheduled run,
a merged PR. The index refreshes every few minutes; nothing here writes to an agent's files."""

import asyncio
import json
import logging
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from gieok.web.config import Agent

log = logging.getLogger(__name__)
KINDS = ("task", "session", "scheduled", "pr")
SCHEMA = """
CREATE TABLE IF NOT EXISTS work_item (
    source TEXT PRIMARY KEY, agent TEXT NOT NULL, kind TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL, outcome TEXT NOT NULL, started REAL, finished REAL, link TEXT, detail TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS work_by_finished ON work_item (finished);
"""
STAMP = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] ([^:]+):")


@dataclass
class WorkItem:
    source: str  # unique, e.g. "staff:12", "daepyo:thread:1569…", "pr:oddballmeasure/oscar#8"
    agent: str
    kind: str
    project: str
    title: str
    outcome: str  # done, failed, merged, error, …
    started: float | None
    finished: float | None
    link: str | None
    detail: dict

    @property
    def took_s(self) -> float | None:
        return self.finished - self.started if self.started and self.finished else None


def ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def thread_url(guild_id: int, thread_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{thread_id}"


# Adapters

def staff_tasks(staff_db: Path, guild_id: int) -> list[WorkItem]:
    """Finished staff tasks (done, failed) and tasks waiting in review."""
    if not staff_db.exists():
        return []
    db = ro(staff_db)
    try:
        rounds = {r["task_id"]: r["n"] for r in db.execute(
            "SELECT task_id, COUNT(*) n FROM events WHERE kind='changes' GROUP BY task_id")}
        items = []
        for t in db.execute("SELECT * FROM tasks WHERE state IN ('done', 'failed', 'review')"):
            title = (t["brief"].strip().splitlines() or ["(no brief)"])[0][:200]
            link = t["pr_url"] or (thread_url(guild_id, t["thread_id"]) if t["thread_id"] else None)
            items.append(WorkItem(f"staff:{t['id']}", t["assignee"], "task", t["repo"], title, t["state"],
                                  t["created"], t["updated"] if t["state"] != "review" else None, link,
                                  {"review_rounds": rounds.get(t["id"], 0), "assigned_by": t["assigned_by"],
                                   "pr": t["pr_url"], "verdict": t["verdict"]}))
        return items
    finally:
        db.close()


def _stamp(text: str) -> float:
    return time.mktime(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timetuple())


def scan_activity(path: Path) -> tuple[float | None, float | None, int, bool]:
    """(first entry, last `done`, turns done, any error) from an activity log."""
    first = last_done = None
    turns, error = 0, False
    with path.open(errors="replace") as f:
        for line in f:
            m = STAMP.match(line)
            if not m:
                continue
            ts, kind = _stamp(m.group(1)), m.group(2)
            first = first or ts
            if kind == "done":
                last_done, turns = ts, turns + 1
                error = error or "error" in line.lower()
    return first, last_done, turns, error


def sessions(agent: Agent, guild_id: int, cache: dict | None = None) -> list[WorkItem]:
    """One item per Discord thread the agent worked in, finished at its last completed turn.
    Staff-task threads are left out: the task itself is the work item."""
    store = agent.state_dir / "store.json"
    if not store.exists():
        return []
    cache = cache if cache is not None else {}
    items = []
    for key, rec in json.loads(store.read_text()).items():
        if rec.get("task_id"):
            continue
        log = agent.state_dir / "activity" / f"{key}.log"
        if not log.exists():
            continue
        mtime = log.stat().st_mtime
        hit = cache.get(log)
        if hit is None or hit[0] != mtime:
            hit = cache[log] = (mtime, scan_activity(log))
        first, last_done, turns, error = hit[1]
        if not turns:
            continue
        kind = "scheduled" if rec.get("job") else "session"
        title = (rec.get("title") or "").strip().splitlines()[0][:200] if rec.get("title") else "(untitled)"
        items.append(WorkItem(f"{agent.name}:thread:{key}", agent.name, kind, rec.get("project", ""), title,
                              "error" if error else "done", first, last_done, thread_url(guild_id, int(key)),
                              {"turns": turns, "job": rec.get("job"), "busy": rec.get("busy", False)}))
    return items


def scheduled_runs(agent: Agent) -> list[WorkItem]:
    """Runs that ended badly (error, failed, missed, skipped). Good runs already show as their sessions."""
    path = agent.state_dir / "oscar.db"
    if not path.exists():
        return []
    db = ro(path)
    try:
        return [WorkItem(f"{agent.name}:run:{r['id']}", agent.name, "scheduled", "", f"Scheduled run: {r['job']}",
                         r["outcome"], r["started"], r["started"], None, {"trigger": r["trigger"], "job": r["job"]})
                for r in db.execute("SELECT * FROM runs WHERE outcome IN ('error', 'failed', 'missed', 'skipped')")]
    finally:
        db.close()


def pr_author(body: str, branch: str) -> str:
    """Who opened a PR, from the footer each agent writes (they all share Monty's GitHub account)."""
    if "Opened by Saekdong" in body or branch.startswith("saekdong/"):
        return "saekdong"
    if branch.startswith("cstack/") or "Made with [cstack]" in body:
        return "cstack"
    if "Cardinal" in body:
        return "cardinal"
    if "Generated with [Claude Code]" in body:
        return "daepyo"
    return "monty"


def pull_requests(raw: list[dict]) -> list[WorkItem]:
    """From `gh search prs --merged --json ...` output."""
    items = []
    for pr in raw:
        repo = pr["repository"]["nameWithOwner"]
        items.append(WorkItem(f"pr:{repo}#{pr['number']}", pr_author(pr.get("body") or "", ""),
                              "pr", repo.split("/")[-1], pr["title"][:200], "merged",
                              _iso(pr.get("createdAt")), _iso(pr.get("closedAt")), pr["url"], {"repo": repo}))
    return items


def _iso(text: str | None) -> float | None:
    return datetime.fromisoformat(text).timestamp() if text else None


async def fetch_prs(owner: str, limit: int = 300) -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        "gh", "search", "prs", "--owner", owner, "--merged", "--limit", str(limit),
        "--json", "number,title,url,repository,createdAt,closedAt,body", stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    return json.loads(out or b"[]")


# The index

class WorkIndex:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def replace(self, prefix: str, items: list[WorkItem]) -> None:
        """Swap every item from one source family (e.g. "staff:") for a fresh read."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("DELETE FROM work_item WHERE source LIKE ?", (prefix + "%",))
            self.db.executemany(
                "INSERT OR REPLACE INTO work_item VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(i.source, i.agent, i.kind, i.project, i.title, i.outcome, i.started, i.finished, i.link,
                  json.dumps(i.detail)) for i in items])
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def query(self, agents: list[str] = (), projects: list[str] = (), kinds: list[str] = (),
              since: float | None = None, until: float | None = None, limit: int = 500,
              finished_only: bool = True) -> list[WorkItem]:
        where, args = [], []
        for col, values in (("agent", agents), ("project", projects), ("kind", kinds)):
            if values:
                where.append(f"{col} IN ({', '.join('?' * len(values))})")
                args.extend(values)
        if finished_only:
            where.append("finished IS NOT NULL")
        if since is not None:
            where.append("finished >= ?")
            args.append(since)
        if until is not None:
            where.append("finished < ?")
            args.append(until)
        sql = "SELECT * FROM work_item" + (f" WHERE {' AND '.join(where)}" if where else "")
        rows = self.db.execute(sql + " ORDER BY finished DESC LIMIT ?", (*args, limit))
        return [WorkItem(**{**dict(r), "detail": json.loads(r["detail"])}) for r in rows]

    def values(self, col: str) -> list[str]:
        if col not in ("agent", "project", "kind"):
            raise ValueError(col)
        return [r[0] for r in self.db.execute(f"SELECT DISTINCT {col} FROM work_item WHERE {col} != '' ORDER BY 1")]


class Refresher:
    """Rebuilds the index from every source; the site runs it every REFRESH_S."""
    REFRESH_S = 300

    def __init__(self, index: WorkIndex, cfg, fetch=fetch_prs):
        self.index, self.cfg, self.fetch = index, cfg, fetch
        self._activity_cache: dict = {}
        self.last: float | None = None

    async def run_once(self) -> None:
        cfg = self.cfg
        self.index.replace("staff:", staff_tasks(cfg.staff_db, cfg.guild_id))
        for agent in cfg.agents:
            self.index.replace(f"{agent.name}:thread:", sessions(agent, cfg.guild_id, self._activity_cache))
            self.index.replace(f"{agent.name}:run:", scheduled_runs(agent))
        prs = await self.fetch(cfg.github_owner)
        if prs:  # gh down or logged out: keep the last good read
            self.index.replace("pr:", pull_requests(prs))
        self.last = time.time()

    async def loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("work refresh failed")
            await asyncio.sleep(self.REFRESH_S)


def as_dict(item: WorkItem) -> dict:
    return {**asdict(item), "took_s": item.took_s}
