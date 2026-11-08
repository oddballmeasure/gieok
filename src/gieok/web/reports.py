"""Saved reports over the work index: filters, metrics, an optional daily or weekly Discord post.

A report keeps relative ranges ("last 7 days"), so a scheduled one always covers the period that
just ended. Scheduled posts go through one Discord webhook (REPORTS_WEBHOOK_URL), at 09:00 local:
daily ones every day, weekly ones on Mondays. Each fires once per period, even across restarts."""

import json
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import aiohttp

from gieok.web.work import KINDS, WorkIndex, WorkItem

METRICS = {
    "items_done": "Items finished",
    "prs_merged": "PRs merged",
    "failures": "Failures",
    "review_rounds": "Review rounds",
    "time_to_done": "Median time to done",
}
SCHEDULES = ("", "daily", "weekly")
GOOD, BAD = ("done", "merged"), ("failed", "error", "missed", "skipped")
POST_HOUR = 9
DISCORD_MAX = 2000
SCHEMA = """
CREATE TABLE IF NOT EXISTS report (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, filters TEXT NOT NULL, metrics TEXT NOT NULL,
    schedule TEXT NOT NULL DEFAULT '', last_post REAL, created REAL NOT NULL, updated REAL NOT NULL);
"""


@dataclass
class Report:
    id: int | None
    name: str
    filters: dict = field(default_factory=dict)  # agents, projects, kinds (lists), days (int; 0 = all time)
    metrics: list[str] = field(default_factory=lambda: list(METRICS))
    schedule: str = ""
    last_post: float | None = None


def validate(r: Report) -> Report:
    if not r.name.strip():
        raise ValueError("A report needs a name.")
    if bad := [m for m in r.metrics if m not in METRICS]:
        raise ValueError(f"Unknown metrics: {', '.join(bad)}")
    if not r.metrics:
        raise ValueError("Pick at least one metric.")
    if r.schedule not in SCHEDULES:
        raise ValueError("Schedule is daily, weekly, or none.")
    if bad := [k for k in r.filters.get("kinds", []) if k not in KINDS]:
        raise ValueError(f"Unknown kinds: {', '.join(bad)}")
    days = int(r.filters.get("days") or 0)
    if days < 0 or days > 3650:
        raise ValueError("Days is between 0 (all time) and 3650.")
    r.filters["days"] = days
    return r


class Reports:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(SCHEMA)

    def _row(self, row) -> Report:
        return Report(row["id"], row["name"], json.loads(row["filters"]), json.loads(row["metrics"]), row["schedule"],
                      row["last_post"])

    def all(self) -> list[Report]:
        return [self._row(r) for r in self.db.execute("SELECT * FROM report ORDER BY name")]

    def get(self, report_id: int) -> Report | None:
        row = self.db.execute("SELECT * FROM report WHERE id=?", (report_id,)).fetchone()
        return self._row(row) if row else None

    def save(self, r: Report) -> int:
        validate(r)
        now = time.time()
        if r.id is None:
            cur = self.db.execute("INSERT INTO report (name, filters, metrics, schedule, created, updated) "
                                  "VALUES (?, ?, ?, ?, ?, ?)",
                                  (r.name.strip(), json.dumps(r.filters), json.dumps(r.metrics), r.schedule, now, now))
            return cur.lastrowid
        self.db.execute("UPDATE report SET name=?, filters=?, metrics=?, schedule=?, updated=? WHERE id=?",
                        (r.name.strip(), json.dumps(r.filters), json.dumps(r.metrics), r.schedule, now, r.id))
        return r.id

    def delete(self, report_id: int) -> None:
        self.db.execute("DELETE FROM report WHERE id=?", (report_id,))

    def mark_posted(self, report_id: int, ts: float) -> None:
        self.db.execute("UPDATE report SET last_post=? WHERE id=?", (ts, report_id))


# Running a report

def window(r: Report, now: float) -> tuple[float | None, float]:
    days = int(r.filters.get("days") or 0)
    return (now - days * 86400 if days else None), now


def run(index: WorkIndex, r: Report, now: float | None = None) -> dict:
    now = now or time.time()
    since, until = window(r, now)
    items = index.query(r.filters.get("agents", []), r.filters.get("projects", []), r.filters.get("kinds", []),
                        since, until, limit=100000)
    return compute(items, r.metrics, since, until)


def _metrics(items: list[WorkItem], wanted: list[str]) -> dict:
    out = {}
    if "items_done" in wanted:
        out["items_done"] = sum(i.outcome in GOOD for i in items)
    if "prs_merged" in wanted:
        out["prs_merged"] = sum(i.kind == "pr" and i.outcome == "merged" for i in items)
    if "failures" in wanted:
        out["failures"] = sum(i.outcome in BAD for i in items)
    if "review_rounds" in wanted:
        out["review_rounds"] = sum(int(i.detail.get("review_rounds") or 0) for i in items if i.kind == "task")
    if "time_to_done" in wanted:
        took = [i.took_s for i in items if i.outcome in GOOD and i.took_s is not None and i.took_s >= 0]
        out["time_to_done"] = statistics.median(took) if took else None
    return out


def compute(items: list[WorkItem], wanted: list[str], since: float | None, until: float) -> dict:
    """Totals, the same per agent, and finished-per-day series for charts."""
    by_agent = defaultdict(list)
    per_day = defaultdict(Counter)
    for i in items:
        by_agent[i.agent].append(i)
        if i.finished:
            per_day[datetime.fromtimestamp(i.finished).date().isoformat()][i.agent] += 1
    return {"since": since, "until": until, "count": len(items), "totals": _metrics(items, wanted),
            "agents": {a: _metrics(v, wanted) for a, v in sorted(by_agent.items())},
            "per_day": {d: dict(c) for d, c in sorted(per_day.items())},
            "items": items[:50]}


def fmt(metric: str, value) -> str:
    if value is None:
        return "–"
    if metric == "time_to_done":
        h = value / 3600
        return f"{value / 60:.0f} min" if h < 1 else f"{h:.1f} h" if h < 48 else f"{h / 24:.1f} d"
    return str(value)


def summary(r: Report, result: dict, site_url: str = "") -> str:
    """The Discord post: totals, then one line per agent. Always under Discord's 2000 characters."""
    days = int(r.filters.get("days") or 0)
    span = f"last {days} days" if days else "all time"
    lines = [f"📊 **{r.name}** · {span}"]
    lines.append(" · ".join(f"{METRICS[m]}: **{fmt(m, result['totals'].get(m))}**" for m in r.metrics))
    for agent, vals in result["agents"].items():
        lines.append(f"- {agent.title()}: " + ", ".join(f"{METRICS[m].lower()} {fmt(m, vals.get(m))}" for m in r.metrics))
    if site_url and r.id:
        lines.append(f"-# {site_url.rstrip('/')}/reports/{r.id}")
    text = "\n".join(lines)
    return text if len(text) <= DISCORD_MAX else text[:DISCORD_MAX - 1] + "…"


# Schedule

def period_start(schedule: str, now: float) -> float | None:
    """The start of the current posting period (09:00 today, or 09:00 this Monday), or None."""
    if not schedule:
        return None
    at = datetime.fromtimestamp(now).replace(hour=POST_HOUR, minute=0, second=0, microsecond=0)
    if schedule == "weekly":
        at -= timedelta(days=at.weekday())
    if at.timestamp() > now:
        at -= timedelta(days=7 if schedule == "weekly" else 1)
    return at.timestamp()


def due(r: Report, now: float) -> bool:
    start = period_start(r.schedule, now)
    return start is not None and (r.last_post is None or r.last_post < start)


async def post(webhook_url: str, text: str) -> None:
    async with aiohttp.ClientSession() as http, \
            http.post(webhook_url, json={"content": text, "allowed_mentions": {"parse": []}}) as resp:
        resp.raise_for_status()


async def post_due(reports: Reports, index: WorkIndex, webhook_url: str | None, site_url: str,
                   now: float | None = None, sender=post) -> list[int]:
    """Post every scheduled report whose period has started. Returns the ids posted."""
    now = now or time.time()
    posted = []
    if not webhook_url:
        return posted
    for r in reports.all():
        if due(r, now):
            await sender(webhook_url, summary(r, run(index, r, now), site_url))
            reports.mark_posted(r.id, now)
            posted.append(r.id)
    return posted
