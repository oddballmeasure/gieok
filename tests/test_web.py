"""Staff HQ: login and roles, the tailnet-only bind, work adapters, reports, and the pages.
Run: uv run --with pytest pytest"""

import asyncio
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gieok.store import Store
from gieok.web import app as webapp
from gieok.web import auth, work
from gieok.web import reports as rep
from gieok.web.config import Agent, WebConfig, check_bind

GUILD = 954


def staff_db(path: Path) -> Path:
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE employees (name TEXT PRIMARY KEY, role TEXT NOT NULL, service TEXT, channel_id INTEGER, heartbeat REAL,
                            user_id INTEGER);
    CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, assignee TEXT NOT NULL, assigned_by TEXT NOT NULL,
        brief TEXT NOT NULL, repo TEXT NOT NULL, done_condition TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
        thread_id INTEGER, branch TEXT, pr_url TEXT, head_sha TEXT, reviewed_sha TEXT, verdict TEXT,
        created REAL NOT NULL, updated REAL NOT NULL, room_id INTEGER);
    CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, ts REAL NOT NULL,
                         kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '');""")
    now = time.time()
    db.execute("INSERT INTO employees VALUES ('saekdong', 'design lead', 'saekdong.service', 1, ?, 42)", (now,))
    db.execute("INSERT INTO tasks (assignee, assigned_by, brief, repo, state, thread_id, pr_url, created, updated) "
               "VALUES ('saekdong', 'daepyo', 'Audit /plans\nmore', 'koizler', 'done', 7, 'https://github.com/o/k/pull/1', ?, ?)",
               (now - 7200, now - 3600))
    db.execute("INSERT INTO tasks (assignee, assigned_by, brief, repo, state, created, updated) "
               "VALUES ('saekdong', 'daepyo', 'Pricing mockup', 'koizler', 'working', ?, ?)", (now - 600, now - 60))
    db.executemany("INSERT INTO events (task_id, ts, kind, text) VALUES (1, ?, 'changes', 'fix contrast')",
                   [(now - 5000,), (now - 4000,)])
    db.commit()
    db.close()
    return path


def agent_dir(root: Path) -> Path:
    state = root / "oscar-state"
    (state / "activity").mkdir(parents=True)
    store = {"111": {"project": "oscar", "title": "Build the staff room\nmore", "busy": False},
             "444": {"project": "oscar", "title": "Live right now", "busy": True},
             "222": {"project": "koizler", "title": "Nightly check", "job": "nightly"},
             "333": {"project": "oscar", "title": "never answered"}}
    (state / "store.json").write_text(json.dumps(store))
    stamp = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    now = time.time()
    (state / "activity" / "111.log").write_text(
        f"[{stamp(now - 3000)}] you: build it\n    more\n[{stamp(now - 2000)}] tool Bash: ls\n"
        f"[{stamp(now - 1000)}] done: ✅ done · 1 turns\n")
    (state / "activity" / "222.log").write_text(f"[{stamp(now - 900)}] you: run\n[{stamp(now - 800)}] done: ✅ done\n")
    (state / "activity" / "333.log").write_text(f"[{stamp(now - 900)}] you: hello\n")
    db = sqlite3.connect(state / "oscar.db")
    db.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, job TEXT NOT NULL, trigger TEXT NOT NULL, outcome TEXT NOT NULL, "
               "started REAL NOT NULL, scheduled_for REAL, target_id INTEGER, link TEXT)")
    db.execute("INSERT INTO runs (job, trigger, outcome, started) VALUES ('nightly', 'schedule', 'done', ?)", (now - 900,))
    db.execute("INSERT INTO runs (job, trigger, outcome, started) VALUES ('nightly', 'schedule', 'error', ?)", (now - 100,))
    db.commit()
    db.close()
    (state / "usage.json").write_text(json.dumps({"windows": {"five_hour": {"utilization": 0.3, "resetsAt": 1}}}))
    return state


@pytest.fixture
def cfg(tmp_path):
    return WebConfig(host="127.0.0.1", port=0, public_url="http://127.0.0.1:8740", client_id="c", client_secret="s",
                     guild_id=GUILD, allowed_role_ids=frozenset({5}), gieok_root=tmp_path / "node", node="oscar",
                     web_db=tmp_path / "web.db", staff_db=staff_db(tmp_path / "staff.db"),
                     agents=(Agent("daepyo", "claude", agent_dir(tmp_path), None),
                             Agent("saekdong", "codex", tmp_path / "missing", None)),
                     github_owner="o", webhook_url=None, session_key=b"k" * 32)


def cookie(cfg, role):
    return {auth.COOKIE: auth.sign(cfg.session_key, {"uid": "1", "name": "Monty" if role == "owner" else "Reader",
                                                     "role": role, "csrf": "tok", "exp": time.time() + 60})}


def with_client(cfg, fn):
    async def go():
        store = Store(cfg.gieok_root, cfg.node)
        app = webapp.make_app(cfg, store=store, background=False)

        async def no_github(owner):
            return []
        app["refresher"].fetch = no_github
        await app["refresher"].run_once()
        async with TestClient(TestServer(app)) as client:
            return await fn(client, store)
    return asyncio.run(go())


# Bind and auth

def test_only_tailnet_and_loopback_addresses_are_served():
    for ok in ("100.101.2.3", "127.0.0.1", "fd7a:115c:a1e0::1"):
        check_bind(ok)
    for bad in ("0.0.0.0", "::", "192.168.1.5", "8.8.8.8", "localhost"):
        with pytest.raises(SystemExit):
            check_bind(bad)


def test_roles_from_discord():
    allowed = frozenset({5})
    assert auth.role_for([{"id": str(GUILD), "owner": True}], None, GUILD, allowed) == "owner"
    assert auth.role_for([{"id": str(GUILD)}], {"roles": ["5"]}, GUILD, allowed) == "reader"
    assert auth.role_for([{"id": str(GUILD)}], {"roles": ["6"]}, GUILD, allowed) is None
    assert auth.role_for([{"id": "1", "owner": True}], None, GUILD, allowed) is None


def test_session_cookies_resist_tampering_and_expire():
    token = auth.sign(b"k", {"role": "reader", "exp": time.time() + 60})
    assert auth.verify(b"k", token)["role"] == "reader"
    _, mac = token.rsplit(".", 1)
    forged = auth.sign(b"other", {"role": "owner", "exp": time.time() + 60}).rsplit(".", 1)[0] + "." + mac
    assert auth.verify(b"k", forged) is None and auth.verify(b"x", token) is None
    assert auth.verify(b"k", auth.sign(b"k", {"exp": time.time() - 1})) is None


def test_strangers_are_sent_to_login_and_posts_refused(cfg):
    async def fn(client, store):
        r = await client.get("/", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"].startswith("/auth/login")
        r = await client.post("/reports", data={"name": "x"}, allow_redirects=False)
        assert r.status == 401
        assert (await client.get("/healthz")).status == 200
    with_client(cfg, fn)


def test_a_reader_reads_but_never_edits_or_sees_transcripts(cfg):
    async def fn(client, store):
        store.put("shared", "fonts", "Pretendard for Korean", author="daepyo", title="Fonts")
        mem = store.get("shared", "fonts")
        c = cookie(cfg, "reader")
        for path in ("/", "/work", "/reports", "/memory", f"/memory/{mem.id}", "/system", "/agents/daepyo"):
            r = await client.get(path, cookies=c)
            assert r.status == 200, path
        assert (await client.get("/transcripts", cookies=c)).status == 403
        r = await client.post(f"/memory/{mem.id}/edit", data={"text": "x", "csrf": "tok"}, cookies=c)
        assert r.status == 403
        r = await client.post(f"/memory/{mem.id}/forget", data={"csrf": "tok"}, cookies=c)
        assert r.status == 403 and not store.get("shared", "fonts").deleted
        page = await (await client.get(f"/memory/{mem.id}", cookies=c)).text()
        assert "Save new version" not in page
    with_client(cfg, fn)


def test_posts_need_the_csrf_token(cfg):
    async def fn(client, store):
        store.put("shared", "a", "one", author="daepyo")
        mem = store.get("shared", "a")
        r = await client.post(f"/memory/{mem.id}/edit", data={"text": "two"}, cookies=cookie(cfg, "owner"))
        assert r.status == 403 and store.text(mem.id) == "one"
    with_client(cfg, fn)


def test_monty_edits_a_memory_and_sees_the_diff_in_history(cfg):
    async def fn(client, store):
        store.put("shared", "a", "line one\nline two", author="daepyo", title="A")
        mem = store.get("shared", "a")
        c = cookie(cfg, "owner")
        r = await client.post(f"/memory/{mem.id}/edit", data={"text": "line one\nline 2", "csrf": "tok", "reason": "fix"},
                              cookies=c, allow_redirects=False)
        assert r.status == 303
        assert [v.author for v in store.history(mem.id)] == ["daepyo", "monty"]
        page = await (await client.get(f"/memory/{mem.id}", cookies=c)).text()
        assert "-line two" in page and "+line 2" in page and "fix" in page
        await client.post(f"/memory/{mem.id}/forget", data={"csrf": "tok"}, cookies=c, allow_redirects=False)
        assert store.get("shared", "a").deleted
    with_client(cfg, fn)


# Work

def test_staff_tasks_carry_review_rounds_and_skip_open_work(cfg):
    items = work.staff_tasks(cfg.staff_db, GUILD)
    assert [(i.source, i.outcome, i.detail["review_rounds"]) for i in items] == [("staff:1", "done", 2)]
    assert items[0].title == "Audit /plans" and items[0].link == "https://github.com/o/k/pull/1"


def test_sessions_come_from_store_and_activity_logs(cfg):
    items = {i.source: i for i in work.sessions(cfg.agents[0], GUILD)}
    assert set(items) == {"daepyo:thread:111", "daepyo:thread:222"}  # 333 never finished a turn
    s = items["daepyo:thread:111"]
    assert (s.kind, s.title, s.detail["turns"]) == ("session", "Build the staff room", 1)
    assert 1900 < s.took_s < 2100 and s.link == f"https://discord.com/channels/{GUILD}/111"
    assert items["daepyo:thread:222"].kind == "scheduled"
    assert [r.outcome for r in work.scheduled_runs(cfg.agents[0])] == ["error"]
    assert work.sessions(cfg.agents[1], GUILD) == []  # not on this host


def test_pr_authors_come_from_footers():
    assert work.pr_author("x\n🤖 Generated with [Claude Code](https://claude.com/claude-code)", "") == "daepyo"
    assert work.pr_author("🎨 Opened by Saekdong (Codex). Daepyo reviews", "") == "saekdong"
    assert work.pr_author("🤖 Generated with [Claude Code]\n<sub>Made with [cstack](…)</sub>", "") == "cstack"
    assert work.pr_author("plain", "") == "monty"


# Reports

def _items():
    now = time.time()
    mk = lambda src, agent, kind, outcome, took, **d: work.WorkItem(
        src, agent, kind, "koizler", src, outcome, now - 100 - took, now - 100, None, d)
    return [mk("a", "daepyo", "pr", "merged", 600), mk("b", "saekdong", "task", "done", 7200, review_rounds=2),
            mk("c", "daepyo", "session", "error", 60), mk("d", "daepyo", "session", "done", 1800)]


def test_report_metrics():
    out = rep.compute(_items(), list(rep.METRICS), None, time.time())
    assert out["totals"] == {"items_done": 3, "prs_merged": 1, "failures": 1, "review_rounds": 2, "time_to_done": 1800}
    assert out["agents"]["saekdong"]["review_rounds"] == 2 and out["count"] == 4
    assert rep.fmt("time_to_done", 1800) == "30 min" and rep.fmt("time_to_done", 7200) == "2.0 h"


def test_scheduled_reports_fire_once_per_period():
    monday_10 = datetime(2026, 11, 9, 10, 0).timestamp()  # a Monday
    r = rep.Report(1, "Weekly", schedule="weekly")
    assert rep.due(r, monday_10)
    r.last_post = monday_10
    assert not rep.due(r, monday_10 + 3600) and not rep.due(r, monday_10 + 6 * 86400)
    assert rep.due(r, monday_10 + 7 * 86400)
    d = rep.Report(2, "Daily", schedule="daily", last_post=datetime(2026, 11, 9, 9, 30).timestamp())
    assert not rep.due(d, datetime(2026, 11, 10, 8, 59).timestamp())
    assert rep.due(d, datetime(2026, 11, 10, 9, 0).timestamp())
    assert not rep.due(rep.Report(3, "Never"), monday_10)


def test_summary_fits_discord():
    many = [work.WorkItem(f"s{i}", f"agent-with-a-long-name-{i}", "task", "p", "t", "done", 1, 2, None, {})
            for i in range(200)]
    text = rep.summary(rep.Report(9, "Big"), rep.compute(many, list(rep.METRICS), None, 3), "http://x")
    assert len(text) <= rep.DISCORD_MAX


def test_post_due_posts_each_due_report_once(tmp_path):
    index = work.WorkIndex(tmp_path / "w.db")
    index.replace("", _items())
    reports = rep.Reports(index.db)
    rid = reports.save(rep.Report(None, "Weekly", {"days": 7}, ["items_done"], "weekly"))
    reports.save(rep.Report(None, "Manual", {"days": 7}, ["items_done"], ""))
    sent = []

    async def sender(url, text):
        sent.append(text)
    now = datetime(2026, 11, 9, 10, 0).timestamp()
    assert asyncio.run(rep.post_due(reports, index, "https://hook", "http://x", now, sender)) == [rid]
    assert asyncio.run(rep.post_due(reports, index, "https://hook", "http://x", now + 60, sender)) == []
    assert len(sent) == 1 and "Weekly" in sent[0]
    assert asyncio.run(rep.post_due(reports, index, None, "http://x", now + 8 * 86400, sender)) == []


def test_report_validation():
    with pytest.raises(ValueError):
        rep.validate(rep.Report(None, " "))
    with pytest.raises(ValueError):
        rep.validate(rep.Report(None, "x", metrics=["nope"]))
    with pytest.raises(ValueError):
        rep.validate(rep.Report(None, "x", schedule="hourly"))


def test_monty_saves_a_report_and_a_reader_views_it(cfg):
    async def fn(client, store):
        r = await client.post("/reports", data={"csrf": "tok", "name": "Saekdong weekly", "agent": "saekdong",
                                                "days": "7", "metric": ["items_done", "review_rounds"],
                                                "schedule": "weekly"}, cookies=cookie(cfg, "owner"),
                              allow_redirects=False)
        assert r.status == 303
        page = await (await client.get(r.headers["Location"], cookies=cookie(cfg, "reader"))).text()
        assert "Saekdong weekly" in page and "Post to Discord now" not in page
        bad = await client.post("/reports", data={"csrf": "tok", "name": "", "metric": "items_done"},
                                cookies=cookie(cfg, "owner"))
        assert bad.status == 400
        assert (await client.post("/reports", data={"csrf": "tok", "name": "x", "metric": "items_done"},
                                  cookies=cookie(cfg, "reader"))).status == 403
    with_client(cfg, fn)


def test_pages_show_the_fixture_work(cfg):
    async def fn(client, store):
        c = cookie(cfg, "owner")
        page = await (await client.get("/work?days=7", cookies=c)).text()
        assert "Audit /plans" in page and "Build the staff room" in page
        rows = await (await client.get("/work?agent=saekdong&days=7", cookies=c, headers={"HX-Request": "true"})).text()
        assert "Audit /plans" in rows and "Build the staff room" not in rows and "<header>" not in rows
        overview = await (await client.get("/", cookies=c)).text()
        assert "Pricing mockup" in overview and "not on this host" in overview and "Claude 5h" in overview
        assert "Live right now" in overview
        assert "Live right now" in await (await client.get("/agents/daepyo", cookies=c)).text()
    with_client(cfg, fn)
