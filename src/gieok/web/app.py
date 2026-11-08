"""Staff HQ: the aiohttp app. Server-rendered Jinja templates, htmx in the browser, no build step.

Every page's template context is documented in web/CONTEXT.md: that is the contract the design
(Saekdong's templates and CSS) is built against. Routes never act on an agent."""

import asyncio
import difflib
import html
import logging
import time
from pathlib import Path

import jinja2
from aiohttp import web

from gieok import search as gsearch
from gieok.store import Store
from gieok.web import agents as agents_mod
from gieok.web import auth
from gieok.web import reports as rep
from gieok.web.config import WebConfig, check_bind
from gieok.web.embedq import QueryEmbedder, available
from gieok.web.work import KINDS, Refresher, WorkIndex

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
STREAM_CHUNK = 64 * 1024
READER_TIERS = ("hot", "warm")  # read-only members see memories and digests, never raw transcripts


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60}m ago" if s < 7200 else f"{s // 3600}h ago" if s < 172800 else f"{s // 86400}d ago"


def when(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "–"


def make_env() -> jinja2.Environment:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True,
                             undefined=jinja2.StrictUndefined)
    env.filters.update(ago=ago, when=when, fmt_metric=lambda value, metric: rep.fmt(metric, value))
    return env


def render(request: web.Request, name: str, status: int = 200, **ctx) -> web.Response:
    """Every page gets `user` ({name, role, csrf}), `is_owner`, `nav` (the current section) and `site`."""
    u = request["user"]
    body = request.app["env"].get_template(name).render(
        user={"name": u["name"], "role": u["role"], "csrf": u["csrf"]}, is_owner=u["role"] == "owner",
        site={"node": request.app["cfg"].node, "refreshed": request.app["refresher"].last}, **ctx)
    return web.Response(text=body, content_type="text/html", status=status)


def _ints(q, key) -> list[str]:
    return [v for v in q.getall(key, []) if v]


# Overview and agents

async def overview(request: web.Request) -> web.Response:
    cfg, index = request.app["cfg"], request.app["index"]
    staff = await agents_mod.statuses(cfg.agents, cfg.staff_db)
    today = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    done_today = index.query(since=today, limit=100)
    last = {s.name: (index.query([s.name], limit=1) or [None])[0] for s in staff}
    return render(request, "overview.html", nav="overview", staff=staff, done_today=done_today, last=last)


async def agent_page(request: web.Request) -> web.Response:
    cfg, index = request.app["cfg"], request.app["index"]
    name = request.match_info["name"]
    staff = [s for s in await agents_mod.statuses(cfg.agents, cfg.staff_db) if s.name == name]
    if not staff:
        raise web.HTTPNotFound(text=f"No agent {name}.")
    recent = index.query([name], limit=50)
    week = rep.compute(index.query([name], since=time.time() - 7 * 86400, limit=100000), list(rep.METRICS),
                       time.time() - 7 * 86400, time.time())
    return render(request, "agent.html", nav="agents", agent=staff[0], recent=recent, week=week)


async def work_page(request: web.Request) -> web.Response:
    index, q = request.app["index"], request.query
    days = int(q.get("days") or 7)
    filters = {"agents": _ints(q, "agent"), "projects": _ints(q, "project"), "kinds": _ints(q, "kind"), "days": days}
    since = time.time() - days * 86400 if days else None
    items = index.query(filters["agents"], filters["projects"], filters["kinds"], since, limit=500)
    choices = {"agents": index.values("agent"), "projects": index.values("project"), "kinds": list(KINDS)}
    tpl = "work_rows.html" if request.headers.get("HX-Request") else "work.html"
    return render(request, tpl, nav="work", items=items, filters=filters, choices=choices)


# Reports

def _report_from_form(form, report_id: int | None) -> rep.Report:
    return rep.Report(report_id, str(form.get("name", "")),
                      {"agents": form.getall("agent", []), "projects": form.getall("project", []),
                       "kinds": form.getall("kind", []), "days": int(form.get("days") or 0)},
                      form.getall("metric", []), str(form.get("schedule", "")))


async def reports_list(request: web.Request) -> web.Response:
    reports = request.app["reports"].all()
    return render(request, "reports.html", nav="reports", reports=reports, webhook=bool(request.app["cfg"].webhook_url))


async def report_form(request: web.Request) -> web.Response:
    report_id = request.match_info.get("id")
    report = request.app["reports"].get(int(report_id)) if report_id else rep.Report(None, "")
    if report is None:
        raise web.HTTPNotFound()
    index = request.app["index"]
    return render(request, "report_form.html", nav="reports", report=report, error=None, metrics=rep.METRICS,
                  schedules=rep.SCHEDULES, choices={"agents": index.values("agent"),
                                                    "projects": index.values("project"), "kinds": list(KINDS)})


@auth.owner_only
async def report_save(request: web.Request) -> web.Response:
    form = await request.post()
    report_id = request.match_info.get("id")
    report = _report_from_form(form, int(report_id) if report_id else None)
    try:
        saved = request.app["reports"].save(report)
    except ValueError as exc:
        index = request.app["index"]
        return render(request, "report_form.html", status=400, nav="reports", report=report, error=str(exc),
                      metrics=rep.METRICS, schedules=rep.SCHEDULES,
                      choices={"agents": index.values("agent"), "projects": index.values("project"),
                               "kinds": list(KINDS)})
    raise web.HTTPSeeOther(f"/reports/{saved}")


async def report_view(request: web.Request) -> web.Response:
    report = request.app["reports"].get(int(request.match_info["id"]))
    if report is None:
        raise web.HTTPNotFound()
    result = rep.run(request.app["index"], report)
    return render(request, "report.html", nav="reports", report=report, result=result, metrics=rep.METRICS,
                  preview=rep.summary(report, result, request.app["cfg"].public_url))


@auth.owner_only
async def report_post_now(request: web.Request) -> web.Response:
    app = request.app
    report = app["reports"].get(int(request.match_info["id"]))
    if report is None:
        raise web.HTTPNotFound()
    if not app["cfg"].webhook_url:
        raise web.HTTPConflict(text="REPORTS_WEBHOOK_URL is not set.")
    await rep.post(app["cfg"].webhook_url, rep.summary(report, rep.run(app["index"], report), app["cfg"].public_url))
    raise web.HTTPSeeOther(f"/reports/{report.id}")


@auth.owner_only
async def report_delete(request: web.Request) -> web.Response:
    request.app["reports"].delete(int(request.match_info["id"]))
    raise web.HTTPSeeOther("/reports")


# Memory

def _visible(request: web.Request, mem) -> bool:
    return auth.is_owner(request) or mem.tier in READER_TIERS


def _memory_by_id(store: Store, memory_id: str):
    row = store.db.execute("SELECT * FROM memory WHERE id=?", (memory_id,)).fetchone()
    return store._memory(row) if row else None


async def memory_home(request: web.Request) -> web.Response:
    store, q = request.app["store"], request.query
    query, ns = q.get("q", "").strip(), q.get("ns", "")
    hits, mode = [], "keyword"
    if query:
        vec = await request.app["embedder"].embed(query)
        mode = "hybrid" if vec is not None else "keyword"
        hits = gsearch.search(store, query, k=30, ns_prefix=ns, embedder=(lambda _: [vec]) if vec is not None else None)
        hits = [h for h in hits if _visible(request, h.memory)]
    spaces = sorted({"/".join(m.ns.split("/")[:2]) for m in store.memories() if _visible(request, m)})
    listing = [] if query else [m for m in store.memories(ns) if _visible(request, m)][:300]
    tpl = "memory_results.html" if request.headers.get("HX-Request") else "memory.html"
    return render(request, tpl, nav="memory", query=query, ns=ns, hits=hits, mode=mode, spaces=spaces,
                  listing=listing, hybrid_ready=available())


async def memory_view(request: web.Request) -> web.Response:
    store = request.app["store"]
    mem = _memory_by_id(store, request.match_info["id"])
    if mem is None or not _visible(request, mem):
        raise web.HTTPNotFound()
    history = store.history(mem.id)
    version = request.query.get("v") or mem.head
    current = next((v for v in history if v.id == version), None)
    if current is None:
        raise web.HTTPNotFound()
    text = store.text(mem.id, current.id)
    parent_text = store.text(mem.id, current.parent) if current.parent else ""
    diff = list(difflib.unified_diff(parent_text.splitlines(), text.splitlines(), "before", "after", lineterm=""))
    return render(request, "memory_view.html", nav="memory", memory=mem, history=list(reversed(history)),
                  version=current, text=text, diff=diff, conflicts=[])  # conflicts arrive with sync (Phase 3)


@auth.owner_only
async def memory_edit(request: web.Request) -> web.Response:
    store = request.app["store"]
    mem = _memory_by_id(store, request.match_info["id"])
    if mem is None:
        raise web.HTTPNotFound()
    form = await request.post()
    text = str(form.get("text", "")).replace("\r\n", "\n")
    if not text.strip():
        raise web.HTTPBadRequest(text="Empty memory. Use forget to remove it.")
    store.put(mem.ns, mem.key, text, author="monty", kind=mem.kind, tier=mem.tier,
              title=str(form.get("title") or mem.title), tags=mem.tags, reason=str(form.get("reason") or "edited in Staff HQ"))
    raise web.HTTPSeeOther(f"/memory/{mem.id}")


@auth.owner_only
async def memory_forget(request: web.Request) -> web.Response:
    store = request.app["store"]
    mem = _memory_by_id(store, request.match_info["id"])
    if mem is None:
        raise web.HTTPNotFound()
    form = await request.post()
    store.forget(mem.ns, mem.key, author="monty", reason=str(form.get("reason") or "forgotten in Staff HQ"))
    raise web.HTTPSeeOther(f"/memory/{mem.id}")


# Transcripts (Monty only)

@auth.owner_only
async def transcripts(request: web.Request) -> web.Response:
    store, query = request.app["store"], request.query.get("q", "").strip()
    if query:
        rows = gsearch.search_transcripts(store, query, k=50)
    else:
        rows = [dict(r) for r in store.db.execute(
            "SELECT rid, source, agent, host, project, harness, started, ended, source_size, redactions FROM transcript "
            "ORDER BY COALESCE(ended, started) DESC LIMIT 200")]
    return render(request, "transcripts.html", nav="transcripts", rows=rows, query=query)


@auth.owner_only
async def transcript_view(request: web.Request) -> web.StreamResponse:
    """Streams the decompressed transcript inside the page shell, a chunk at a time."""
    store = request.app["store"]
    row = store.transcript(int(request.match_info["rid"]))
    if row is None:
        raise web.HTTPNotFound()
    head, tail = request.app["env"].get_template("transcript_view.html").render(
        user=request["user"], is_owner=True, nav="transcripts", site={"node": request.app["cfg"].node,
                                                                      "refreshed": request.app["refresher"].last},
        transcript=dict(row), body="\x00").split("\x00")
    resp = web.StreamResponse(headers={"Content-Type": "text/html; charset=utf-8"})
    await resp.prepare(request)
    await resp.write(head.encode())
    data = await asyncio.to_thread(store.read_blob, row["blob"])
    for i in range(0, len(data), STREAM_CHUNK):
        await resp.write(html.escape(data[i:i + STREAM_CHUNK].decode(errors="replace")).encode())
    await resp.write(tail.encode())
    await resp.write_eof()
    return resp


# System

async def system(request: web.Request) -> web.Response:
    store = request.app["store"]
    stats = store.stats()
    nodes = [{"name": request.app["cfg"].node, "role": "hub", "lag_s": 0, "here": True}]  # one node until Phase 3
    return render(request, "system.html", nav="system", stats=stats, nodes=nodes)


async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# The app

async def _background(app: web.Application):
    tasks = [asyncio.create_task(app["refresher"].loop(), name="work-refresh"),
             asyncio.create_task(_report_loop(app), name="reports")]
    yield
    for t in tasks:
        t.cancel()
    app["embedder"].close()


async def _report_loop(app: web.Application) -> None:
    while True:
        try:
            await rep.post_due(app["reports"], app["index"], app["cfg"].webhook_url, app["cfg"].public_url)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduled reports failed")
        await asyncio.sleep(60)


def make_app(cfg: WebConfig, store: Store | None = None, background: bool = True) -> web.Application:
    app = web.Application(middlewares=[auth.middleware], client_max_size=4 * 1024 * 1024)
    app["cfg"] = cfg
    app["store"] = store or Store(cfg.gieok_root, cfg.node)
    app["index"] = index = WorkIndex(cfg.web_db)
    app["reports"] = rep.Reports(index.db)
    app["refresher"] = Refresher(index, cfg)
    app["embedder"] = QueryEmbedder()
    app["env"] = make_env()
    if background:
        app.cleanup_ctx.append(_background)
    app.add_routes(auth.routes() + [
        web.get("/healthz", healthz),
        web.get("/", overview),
        web.get("/agents/{name}", agent_page),
        web.get("/work", work_page),
        web.get("/reports", reports_list),
        web.get("/reports/new", report_form),
        web.post("/reports", report_save),
        web.get("/reports/{id:\\d+}", report_view),
        web.get("/reports/{id:\\d+}/edit", report_form),
        web.post("/reports/{id:\\d+}", report_save),
        web.post("/reports/{id:\\d+}/post", report_post_now),
        web.post("/reports/{id:\\d+}/delete", report_delete),
        web.get("/memory", memory_home),
        web.get("/memory/{id}", memory_view),
        web.post("/memory/{id}/edit", memory_edit),
        web.post("/memory/{id}/forget", memory_forget),
        web.get("/transcripts", transcripts),
        web.get("/transcripts/{rid:\\d+}", transcript_view),
        web.get("/system", system),
    ])
    app.router.add_static("/static/", HERE / "static")
    return app


def main() -> None:
    from gieok.web.config import load

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load()
    check_bind(cfg.host)
    web.run_app(make_app(cfg), host=cfg.host, port=cfg.port, access_log=None)


if __name__ == "__main__":
    main()
