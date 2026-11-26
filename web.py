"""
Общий веб-сервер бота: веб-панель управления музыкой (/panel) и кинотеатр (/films).
Настройка в .env: WEB_PORT, PUBLIC_URL, WEB_KEY, WEB_ENABLED.
"""
import logging
import os
from typing import Optional
from urllib.parse import quote

from aiohttp import web

log = logging.getLogger("web")

WEB_ENABLED = os.getenv("WEB_ENABLED", "1") != "0"
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
PUBLIC_URL = (os.getenv("PUBLIC_URL", "").strip() or f"http://localhost:{WEB_PORT}").rstrip("/")
WEB_KEY = os.getenv("WEB_KEY", "").strip()
COOKIE = "dsbot_key"


def key_q(prefix: str = "?") -> str:
    """Добавка к ссылке с паролем (если пароль задан)."""
    return f"{prefix}key={quote(WEB_KEY)}" if WEB_KEY else ""


def url(path: str) -> str:
    return f"{PUBLIC_URL}{path}{key_q('&' if '?' in path else '?')}"


@web.middleware
async def key_middleware(request: web.Request, handler):
    if not WEB_KEY:
        return await handler(request)
    via_query = request.query.get("key") == WEB_KEY
    if not via_query and request.cookies.get(COOKIE) != WEB_KEY:
        return web.Response(
            status=403,
            text="<h2>403 — нужен пароль</h2><p>Открой ссылку из Discord (команда /panel или /film).</p>",
            content_type="text/html",
        )
    resp = await handler(request)
    if via_query:
        resp.set_cookie(COOKIE, WEB_KEY, max_age=60 * 60 * 24 * 60, httponly=True, samesite="Lax")
    return resp


app = web.Application(middlewares=[key_middleware])
_runner: Optional[web.AppRunner] = None


async def _root(request: web.Request):
    raise web.HTTPFound("/panel")


async def start():
    global _runner
    if not WEB_ENABLED:
        log.info("Веб-сервер выключен (WEB_ENABLED=0)")
        return
    app.router.add_get("/", _root)
    _runner = web.AppRunner(app)
    await _runner.setup()
    try:
        await web.TCPSite(_runner, "0.0.0.0", WEB_PORT).start()
    except OSError as e:
        log.error("Не удалось открыть порт %s: %s — смени WEB_PORT в .env", WEB_PORT, e)
        return
    log.info("Веб-панель: %s", url("/panel"))


async def stop():
    if _runner:
        await _runner.cleanup()
