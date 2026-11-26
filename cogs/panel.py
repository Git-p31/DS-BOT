"""
Веб-панель управления музыкой: открывается в браузере на компьютере или телефоне.
Видно, что играет, очередь (можно перетаскивать), поиск, громкость, эффекты, повтор.
Команда /panel присылает ссылку.
"""
from __future__ import annotations

import logging
import random
import re
from pathlib import Path
from typing import Optional

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

import web as webserver
from cogs.music import FILTERS, GuildPlayer, Music, UserError, fetch_tracks, search_tracks, short_error

log = logging.getLogger("panel")

PANEL_HTML = Path(__file__).with_name("panel.html")
PANEL_REQUESTER = "🌐 Веб-панель"


def _plain_requester(guild: discord.Guild, text: str) -> str:
    m = re.fullmatch(r"<@!?(\d+)>", text or "")
    if m:
        member = guild.get_member(int(m.group(1)))
        return member.display_name if member else "?"
    return text or "?"


class Panel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def music(self) -> Music:
        cog = self.bot.get_cog("Music")
        if cog is None:
            raise UserError("Модуль музыки не загружен.")
        return cog  # type: ignore[return-value]

    async def cog_load(self):
        if not webserver.WEB_ENABLED:
            return
        r = webserver.app.router
        r.add_get("/panel", self.page)
        r.add_get("/api/state", self.api_state)
        r.add_get("/api/search", self.api_search)
        r.add_post("/api/action", self.api_action)

    # ── данные ──
    def _guild(self, gid: Optional[str]) -> discord.Guild:
        guilds = self.bot.guilds
        if not guilds:
            raise UserError("Бот не добавлен ни на один сервер.")
        if gid:
            g = self.bot.get_guild(int(gid))
            if not g:
                raise UserError("Сервер не найден.")
            return g
        return guilds[0]

    def _state(self, guild: discord.Guild) -> dict:
        player: Optional[GuildPlayer] = self.music.players.get(guild.id)
        vc = guild.voice_client
        voice_channels = [
            {"id": str(c.id), "name": c.name, "members": len([m for m in c.members if not m.bot])}
            for c in guild.voice_channels
            if c.permissions_for(guild.me).connect
        ]
        state = {
            "guild": {"id": str(guild.id), "name": guild.name},
            "guilds": [{"id": str(g.id), "name": g.name} for g in self.bot.guilds],
            "connected": bool(vc and vc.channel),
            "channel": vc.channel.name if vc and vc.channel else None,
            "voice_channels": voice_channels,
            "filters": [[k, v[0]] for k, v in FILTERS.items()],
            "current": None,
            "queue": [],
            "paused": False,
            "volume": 50,
            "loop": "off",
            "filter": "off",
            "normalize": True,
            "rate": 1.0,
        }
        if player:
            t = player.current
            state.update(
                paused=player.is_paused,
                volume=round(player.volume * 100),
                loop=player.loop_mode,
                filter=player.filter,
                normalize=player.normalize,
                rate=player.rate,
                queue=[
                    {
                        "title": q.title,
                        "url": q.url if q.url.startswith("http") else None,
                        "duration": q.duration,
                        "requester": _plain_requester(guild, q.requester),
                    }
                    for q in player.queue
                ],
            )
            if t:
                state["current"] = {
                    "title": t.title,
                    "url": t.url if t.url.startswith("http") else None,
                    "thumbnail": t.thumbnail,
                    "duration": t.duration,
                    "position": player.position,
                    "uploader": t.uploader,
                    "requester": _plain_requester(guild, t.requester),
                }
        return state

    @staticmethod
    def _error(msg: str, status: int = 400) -> web.Response:
        return web.json_response({"error": msg}, status=status)

    # ── маршруты ──
    async def page(self, request: web.Request):
        return web.Response(text=PANEL_HTML.read_text(encoding="utf-8"), content_type="text/html")

    async def api_state(self, request: web.Request):
        try:
            return web.json_response(self._state(self._guild(request.query.get("guild"))))
        except UserError as e:
            return self._error(str(e))

    async def api_search(self, request: web.Request):
        q = (request.query.get("q") or "").strip()
        if not q:
            return web.json_response([])
        try:
            tracks = await search_tracks(q, PANEL_REQUESTER, count=8)
        except UserError as e:
            return self._error(str(e))
        return web.json_response(
            [{"title": t.title, "url": t.url, "duration": t.duration, "uploader": t.uploader, "thumbnail": t.thumbnail} for t in tracks]
        )

    async def api_action(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return self._error("Плохой запрос")
        try:
            guild = self._guild(data.get("guild"))
            msg = await self._do(guild, data.get("action", ""), data)
            st = self._state(guild)
            if msg:
                st["message"] = msg
            return web.json_response(st)
        except UserError as e:
            return self._error(str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка веб-панели")
            return self._error(f"Ошибка: {short_error(e)}", 500)

    async def _do(self, guild: discord.Guild, action: str, d: dict) -> Optional[str]:
        music = self.music
        vc = guild.voice_client

        if action == "join":
            channel = guild.get_channel(int(d.get("channel_id", 0)))
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                raise UserError("Выбери голосовой канал.")
            if vc is None:
                await channel.connect(self_deaf=True, timeout=20)
            elif vc.channel != channel:
                await vc.move_to(channel)
            music.get_player(guild)
            return f"Подключился к {channel.name}"

        player = music.players.get(guild.id)
        if action in ("play", "playnext"):
            if not vc:
                raise UserError("Сначала подключи бота к голосовому каналу.")
            player = player or music.get_player(guild)
            query = (d.get("query") or "").strip()
            if not query:
                raise UserError("Пустой запрос.")
            tracks, playlist = await fetch_tracks(query, PANEL_REQUESTER)
            player.add(tracks, front=action == "playnext")
            name = f"плейлист «{playlist}» ({len(tracks)})" if playlist else tracks[0].title
            await player.send(f"🌐 Из веб-панели добавлено: **{discord.utils.escape_markdown(name)}**", silent=True)
            return f"Добавлено: {name}"

        if not player or not vc:
            raise UserError("Бот сейчас не в голосовом канале.")

        if action == "toggle":
            if not player.pause():
                player.resume()
        elif action == "skip":
            player.skip()
        elif action == "stop":
            player.stop()
        elif action == "leave":
            await player.destroy()
            return "Бот вышел из канала"
        elif action == "volume":
            player.set_volume(max(1, min(150, int(d.get("value", 50)))) / 100)
        elif action == "loop":
            mode = d.get("mode")
            if mode in ("off", "track", "queue"):
                player.loop_mode = mode
            else:
                player.cycle_loop()
        elif action == "shuffle":
            random.shuffle(player.queue)
        elif action == "clear":
            player.queue.clear()
        elif action == "filter":
            player.set_filter(d.get("name", "off"))
        elif action == "normalize":
            player.set_normalize(bool(d.get("value")))
        elif action == "seek":
            player.seek(int(float(d.get("seconds", 0))))
        elif action in ("remove", "skipto"):
            i = int(d.get("index", -1))
            if not 0 <= i < len(player.queue):
                raise UserError("Нет такого трека в очереди.")
            if action == "remove":
                player.queue.pop(i)
            else:
                skipped = player.queue[:i]
                del player.queue[:i]
                if player.loop_mode == "queue":
                    player.queue.extend(skipped)
                player.skip()
        elif action == "move":
            a, b = int(d.get("from", -1)), int(d.get("to", -1))
            n = len(player.queue)
            if not (0 <= a < n and 0 <= b < n):
                raise UserError("Неверная позиция.")
            player.queue.insert(b, player.queue.pop(a))
        else:
            raise UserError("Неизвестное действие.")

        await player.update_panel()
        return None

    # ── команда ──
    @app_commands.command(name="panel", description="Ссылка на веб-панель управления музыкой (с компьютера или телефона)")
    async def panel_cmd(self, interaction: discord.Interaction):
        if not webserver.WEB_ENABLED:
            await interaction.response.send_message("Веб-панель выключена (WEB_ENABLED=0 в .env).", ephemeral=True)
            return
        link = webserver.url(f"/panel?guild={interaction.guild_id}")
        e = discord.Embed(
            title="🎛️ Веб-панель DS-BOT",
            description=f"**[Открыть панель]({link})**\n\nОчередь, поиск, громкость, эффекты — с компьютера или телефона.",
            color=0x5865F2,
        )
        if "localhost" in webserver.PUBLIC_URL:
            e.set_footer(text="Ссылка работает только на компьютере с ботом. Для телефона укажи PUBLIC_URL в .env")
        await interaction.response.send_message(embed=e, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Panel(bot))
