# Staff HQ template contract

The design is built against this contract. Templates live in `templates/`, static files (CSS, htmx, chart helper) in
`static/`. No build step: plain CSS and a little JS. Change the markup and classes freely, but keep every name below
and every form field name, because the backend and the tests depend on them.

## Every page
- `user`: `{name, role, csrf}`. `role` is `owner` (Monty) or `reader`.
- `is_owner`: show edit and delete controls only when this is true. The server also refuses them for readers.
- `nav`: the current section: `overview`, `work`, `reports`, `memory`, `transcripts`, `system` or `agents`.
- `site`: `{node, refreshed}`. `refreshed` is when the work index was last rebuilt (a unix time).
- Filters: `ago` (timestamp → "5m ago"), `when` (timestamp → "2026-11-09 14:05"), `fmt_metric(metric)` (value →
  "30 min", "–", "3").
- Every POST form carries `<input type="hidden" name="csrf" value="{{ user.csrf }}">`. `base.html` sets the
  `X-CSRF-Token` header for htmx requests on `<body>`.
- Transcripts are owner-only. Hide the nav link for readers.

## Shared objects
- **Work item** (`item`): `source, agent, kind (task|session|scheduled|pr), project, title, outcome
  (done|merged|failed|error|missed|skipped|review), started, finished, link, detail, took_s`.
- **Agent status** (`a`, `agent`): `name, harness (claude|codex), role, here (state on this host), service,
  service_state (active|inactive|failed|unknown), heartbeat, healthy, current, usage`.
  - `current`: a list of `{kind: task|session, id?, state?, project?, pr?, title, updated?, thread?}`.
  - `usage`: a list of `{label, used (0..1), resets_at}`.
- **Memory** (`memory`, `m`, `h.memory`): `id, ns, key, kind, tier (hot|warm), title, tags, head, deleted, created,
  updated`.
- **Version** (`v`): `id, parent, author, origin, size, reason, batch`.
- **Report** (`report`, `r`): `id, name, filters {agents, projects, kinds, days}, metrics, schedule (""|daily|weekly),
  last_post`.

## Pages
| Template | Route | Context |
|---|---|---|
| `overview.html` | `GET /` | `staff` (agent statuses), `done_today` (work items), `last` ({agent: work item or None}) |
| `agent.html` | `GET /agents/{name}` | `agent`, `recent` (work items), `week` (a report result for the last 7 days) |
| `work.html` | `GET /work` | `items`, `filters {agents, projects, kinds, days}`, `choices {agents, projects, kinds}` |
| `work_rows.html` | the same route with the `HX-Request` header | the same context; the fragment that htmx swaps into `#work-rows` |
| `reports.html` | `GET /reports` | `reports`, `webhook` (bool: scheduled posting is configured) |
| `report_form.html` | `GET /reports/new`, `GET /reports/{id}/edit` | `report`, `error`, `metrics {key: label}`, `schedules`, `choices` |
| `report.html` | `GET /reports/{id}` | `report`, `result`, `metrics`, `preview` (the Discord post text) |
| `memory.html` | `GET /memory?q=&ns=` | `query`, `ns`, `hits`, `mode` (hybrid\|keyword), `spaces`, `listing`, `hybrid_ready` |
| `memory_results.html` | the same route with the `HX-Request` header | the same context; swapped into `#memory-results` |
| `memory_view.html` | `GET /memory/{id}?v=` | `memory`, `history` (newest first), `version`, `text`, `diff` (unified diff lines), `conflicts` |
| `transcripts.html` | `GET /transcripts?q=` | `rows` (`rid, source, agent, host, project, started, ended, …`), `query` |
| `transcript_view.html` | `GET /transcripts/{rid}` | `transcript` (row), `body`. The body is streamed: the template is cut at `{{ body }}`, so keep it inside one element |
| `system.html` | `GET /system` | `stats` (`Store.stats()`), `nodes` (`{name, role, lag_s, here}`) |

- A **search hit** (`h`): `memory, score, snippet, via` (`fts`, `vec` or `fts+vec`).
- A **report result**: `since, until, count, totals {metric: value}, agents {agent: {metric: value}}, per_day
  {YYYY-MM-DD: {agent: n}}, items` (up to 50 work items).

## Forms (field names are fixed)
- `POST /reports` and `POST /reports/{id}`: `name`, and repeated `agent`, `project`, `kind` and `metric`, plus `days`
  and `schedule`.
- `POST /reports/{id}/post`, `POST /reports/{id}/delete`: only `csrf`.
- `POST /memory/{id}/edit`: `title`, `text`, `reason`. `POST /memory/{id}/forget`: `reason`.
