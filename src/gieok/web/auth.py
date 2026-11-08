"""Discord OAuth2 login and the two roles.

owner: the guild owner (Monty). Edits memories and reports, sees transcripts and activity logs.
reader: a guild member holding one of ALLOWED_ROLE_IDS. Reads everything else.
Anyone else is refused. Sessions are HMAC-signed cookies; every POST must carry the session's
CSRF token (form field `csrf` or header X-CSRF-Token)."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

API = "https://discord.com/api/v10"
SCOPES = "identify guilds guilds.members.read"
COOKIE = "hq_session"
STATE_COOKIE = "hq_oauth_state"
SESSION_S = 7 * 24 * 3600
PUBLIC = ("/auth/", "/static/", "/healthz")


def sign(key: bytes, payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def verify(key: bytes, token: str | None, now: float | None = None) -> dict | None:
    if not token or "." not in token:
        return None
    body, mac = token.rsplit(".", 1)
    if not hmac.compare_digest(mac, hmac.new(key, body.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    return payload if payload.get("exp", 0) > (now or time.time()) else None


def role_for(guilds: list[dict], member: dict | None, guild_id: int, allowed: frozenset[int]) -> str | None:
    """owner, reader, or None, from Discord's /users/@me/guilds and the guild member object."""
    home = next((g for g in guilds if int(g.get("id", 0)) == guild_id), None)
    if home is None:
        return None
    if home.get("owner"):
        return "owner"
    roles = {int(r) for r in (member or {}).get("roles", [])}
    return "reader" if roles & allowed else None


def user(request: web.Request) -> dict:
    return request["user"]


def is_owner(request: web.Request) -> bool:
    return request["user"]["role"] == "owner"


def owner_only(handler):
    async def wrapped(request: web.Request):
        if not is_owner(request):
            raise web.HTTPForbidden(text="Monty only.")
        return await handler(request)
    return wrapped


@web.middleware
async def middleware(request: web.Request, handler):
    if request.path.startswith(PUBLIC):
        return await handler(request)
    cfg = request.app["cfg"]
    session = verify(cfg.session_key, request.cookies.get(COOKIE))
    if session is None:
        if request.method != "GET":
            raise web.HTTPUnauthorized(text="Log in first.")
        raise web.HTTPFound("/auth/login?" + urlencode({"next": request.path_qs}))
    if request.method == "POST":
        form = await request.post()
        token = request.headers.get("X-CSRF-Token") or form.get("csrf")
        if not token or not hmac.compare_digest(str(token), session["csrf"]):
            raise web.HTTPForbidden(text="Bad or missing CSRF token.")
    request["user"] = session
    return await handler(request)


async def login(request: web.Request) -> web.Response:
    cfg = request.app["cfg"]
    state = secrets.token_urlsafe(24)
    nxt = request.query.get("next", "/")
    query = urlencode({"client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri, "response_type": "code",
                       "scope": SCOPES, "state": state, "prompt": "none"})
    resp = web.HTTPFound(f"https://discord.com/oauth2/authorize?{query}")
    resp.set_cookie(STATE_COOKIE, sign(cfg.session_key, {"state": state, "next": nxt if nxt.startswith("/") else "/",
                                                          "exp": time.time() + 600}),
                    httponly=True, samesite="Lax", max_age=600)
    raise resp


async def _discord(session: aiohttp.ClientSession, token: str, path: str):
    async with session.get(f"{API}{path}", headers={"Authorization": f"Bearer {token}"}) as r:
        if r.status == 404:
            return None
        r.raise_for_status()
        return await r.json()


async def callback(request: web.Request) -> web.Response:
    cfg = request.app["cfg"]
    pending = verify(cfg.session_key, request.cookies.get(STATE_COOKIE))
    if pending is None or not hmac.compare_digest(pending["state"], request.query.get("state", "")):
        raise web.HTTPBadRequest(text="Login expired. Try again.")
    if "code" not in request.query:
        raise web.HTTPForbidden(text="Discord login was cancelled.")
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{API}/oauth2/token", data={
                "grant_type": "authorization_code", "code": request.query["code"], "redirect_uri": cfg.redirect_uri,
                "client_id": cfg.client_id, "client_secret": cfg.client_secret}) as r:
            if r.status != 200:
                raise web.HTTPForbidden(text="Discord refused the login.")
            token = (await r.json())["access_token"]
        me = await _discord(http, token, "/users/@me")
        guilds = await _discord(http, token, "/users/@me/guilds") or []
        member = await _discord(http, token, f"/users/@me/guilds/{cfg.guild_id}/member")
    role = role_for(guilds, member, cfg.guild_id, cfg.allowed_role_ids)
    if role is None:
        raise web.HTTPForbidden(text="This site is for Monty's staff only.")
    session = {"uid": me["id"], "name": me.get("global_name") or me["username"], "role": role,
               "csrf": secrets.token_urlsafe(24), "exp": time.time() + SESSION_S}
    resp = web.HTTPFound(pending["next"])
    resp.set_cookie(COOKIE, sign(cfg.session_key, session), httponly=True, samesite="Lax", max_age=SESSION_S)
    resp.del_cookie(STATE_COOKIE)
    raise resp


async def logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/auth/bye")
    resp.del_cookie(COOKIE)
    raise resp


async def bye(request: web.Request) -> web.Response:
    return web.Response(text="Logged out.", content_type="text/plain")


def routes() -> list:
    return [web.get("/auth/login", login), web.get("/auth/callback", callback),
            web.get("/auth/logout", logout), web.get("/auth/bye", bye)]
