"""
Фильмы с твоего компьютера.

Боты Discord НЕ умеют транслировать видео в голосовой канал (Go Live / демонстрация
экрана доступна только людям). Поэтому бот поднимает маленький веб-сервер с плеером:
команда /film даёт ссылку, фильм открывается в браузере с перемоткой.
Смотреть вместе: один человек открывает ссылку и включает «Демонстрацию экрана»
в голосовом канале, либо каждый открывает ссылку сам.

Настройка в .env: FILMS_DIR (+ общие WEB_PORT, PUBLIC_URL, WEB_KEY — см. web.py).
"""
from __future__ import annotations

import hashlib
import html
import logging
import mimetypes
import os
from pathlib import Path
from typing import Optional
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

import web as webserver
from web import PUBLIC_URL, key_q

log = logging.getLogger("films")

FILMS_DIR = os.getenv("FILMS_DIR", "").strip()

VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".ogv", ".avi"}
BROWSER_OK = {".mp4", ".webm", ".m4v", ".ogv", ".mov"}  # mkv/avi браузеры часто не играют

mimetypes.add_type("video/mp4", ".m4v")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/webm", ".webm")


def scan_films() -> dict[str, Path]:
    """id → путь. id стабилен (хеш относительного пути)."""
    root = Path(FILMS_DIR)
    result: dict[str, Path] = {}
    if not FILMS_DIR or not root.is_dir():
        return result
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in VIDEO_EXT:
            rel = p.relative_to(root).as_posix()
            fid = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:10]
            result[fid] = p
    return result


def nice_name(p: Path) -> str:
    return p.stem.replace(".", " ").replace("_", " ")


def watch_url(fid: str) -> str:
    return f"{PUBLIC_URL}/watch/{fid}{key_q()}"


PAGE_CSS = """
body{margin:0;background:#0f0f12;color:#eee;font-family:Segoe UI,Arial,sans-serif}
a{color:#8ab4ff;text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 16px}
video{width:100%;max-height:80vh;background:#000;border-radius:8px}
li{padding:6px 0;border-bottom:1px solid #222;list-style:none}
ul{padding:0}.muted{color:#888;font-size:13px}
"""


def _page(title: str, body: str) -> web.Response:
    doc = (
        f"<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head>"
        f"<body><div class='wrap'>{body}</div></body></html>"
    )
    return web.Response(text=doc, content_type="text/html")


async def index_handler(request: web.Request):
    films = scan_films()
    items = "".join(
        f"<li><a href='/watch/{fid}{key_q()}'>🎬 {html.escape(nice_name(p))}</a> "
        f"<span class='muted'>{p.stat().st_size / 1024 ** 3:.2f} ГБ</span></li>"
        for fid, p in films.items()
    ) or "<li>Фильмов нет. Положи видео в папку FILMS_DIR.</li>"
    return _page("Фильмы", f"<h1>🎞️ Фильмы</h1><ul>{items}</ul>")


async def watch_handler(request: web.Request):
    fid = request.match_info["fid"]
    p = scan_films().get(fid)
    if not p:
        raise web.HTTPNotFound(text="Фильм не найден")
    warn = "" if p.suffix.lower() in BROWSER_OK else (
        "<p class='muted'>⚠️ Формат " + html.escape(p.suffix) +
        " браузер может не воспроизвести. Лучше mp4 (H.264 + AAC).</p>"
    )
    body = (
        f"<p><a href='/films'>← все фильмы</a> · <a href='/panel'>🎵 музыка</a></p><h1>{html.escape(nice_name(p))}</h1>"
        f"<video controls autoplay preload='metadata' src='/file/{fid}{key_q()}'></video>{warn}"
    )
    return _page(nice_name(p), body)


async def file_handler(request: web.Request):
    fid = request.match_info["fid"]
    p = scan_films().get(fid)
    if not p:
        raise web.HTTPNotFound()
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    # FileResponse сам поддерживает Range-запросы → работает перемотка
    return web.FileResponse(p, headers={"Content-Type": ctype})


class Films(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.enabled = False

    async def cog_load(self):
        if not FILMS_DIR:
            log.info("FILMS_DIR не задан — раздел фильмов выключен")
            return
        if not Path(FILMS_DIR).is_dir():
            log.warning("Папка FILMS_DIR не найдена: %s", FILMS_DIR)
            return
        if not webserver.WEB_ENABLED:
            log.warning("Фильмы не работают: веб-сервер выключен (WEB_ENABLED=0)")
            return
        router = webserver.app.router
        router.add_get("/films", index_handler)
        router.add_get("/watch/{fid}", watch_handler)
        router.add_get("/file/{fid}", file_handler)
        self.enabled = True
        log.info("Кинотеатр: %s  (папка %s)", webserver.url("/films"), FILMS_DIR)

    def _require_enabled(self):
        if not self.enabled:
            raise app_commands.AppCommandError(
                "Раздел фильмов не настроен. Укажи папку FILMS_DIR в файле .env и перезапусти бота."
            )

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        msg = str(original)
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    async def film_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        cur = current.lower()
        out = []
        for fid, p in scan_films().items():
            name = nice_name(p)
            if cur in name.lower():
                out.append(app_commands.Choice(name=name[:100], value=fid))
            if len(out) >= 25:
                break
        return out

    @app_commands.command(name="films", description="Список фильмов с компьютера")
    @app_commands.describe(search="Поиск по названию (необязательно)")
    async def films(self, interaction: discord.Interaction, search: Optional[str] = None):
        self._require_enabled()
        films = scan_films()
        if search:
            films = {k: v for k, v in films.items() if search.lower() in nice_name(v).lower()}
        if not films:
            await interaction.response.send_message("Фильмы не найдены.", ephemeral=True)
            return
        lines = [f"🎬 [{discord.utils.escape_markdown(nice_name(p))}]({watch_url(fid)})" for fid, p in list(films.items())[:30]]
        e = discord.Embed(title="🎞️ Фильмы", description="\n".join(lines)[:4000], color=0xE50914)
        e.set_footer(text=f"Всего: {len(films)} • полный список: {PUBLIC_URL}/films")
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="film", description="Получить ссылку на просмотр фильма")
    @app_commands.describe(name="Начни вводить название")
    @app_commands.autocomplete(name=film_autocomplete)
    async def film(self, interaction: discord.Interaction, name: str):
        self._require_enabled()
        films = scan_films()
        fid = name if name in films else next((k for k, v in films.items() if name.lower() in nice_name(v).lower()), None)
        if not fid:
            await interaction.response.send_message("Такой фильм не найден. Посмотри /films.", ephemeral=True)
            return
        p = films[fid]
        e = discord.Embed(
            title=f"🎬 {nice_name(p)}",
            url=watch_url(fid),
            description=(
                f"**[▶️ Открыть плеер]({watch_url(fid)})**\n\n"
                "**Как смотреть вместе:** один открывает ссылку в браузере, заходит в голосовой канал "
                "и включает **Демонстрацию экрана** (выбрать окно браузера, со звуком)."
            ),
            color=0xE50914,
        )
        e.set_footer(text=f"{p.suffix.lower()} • {p.stat().st_size / 1024 ** 3:.2f} ГБ")
        await interaction.response.send_message(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(Films(bot))
