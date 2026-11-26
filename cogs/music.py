"""
Музыка: воспроизведение из YouTube / SoundCloud / прямых ссылок / файлов,
очередь, повтор, перемешивание, громкость, перемотка, панель с кнопками.
"""
from __future__ import annotations

import asyncio
import functools
import html
import json
import logging
import os
import random
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from typing import Optional

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("music")



def find_ffmpeg() -> str:
    """FFMPEG_PATH из .env → ffmpeg в PATH → ffmpeg.exe рядом с ботом → из пакета imageio-ffmpeg."""
    env = os.getenv("FFMPEG_PATH", "").strip()
    if env:
        return env
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    local = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ffmpeg.exe")
    if os.path.isfile(local):
        return local
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


FFMPEG = find_ffmpeg()
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "300"))      # сек. без музыки → выйти из канала
ALONE_TIMEOUT = int(os.getenv("ALONE_TIMEOUT", "60"))     # сек. один в канале → выйти
MAX_PLAYLIST = int(os.getenv("MAX_PLAYLIST", "200"))
DEFAULT_VOLUME = max(1, min(150, int(os.getenv("DEFAULT_VOLUME", "50"))))
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()

_YTDL_BASE = {
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}
if COOKIES_FILE:
    _YTDL_BASE["cookiefile"] = COOKIES_FILE

# Быстрый список (плейлисты не раскрываются полностью — только названия и ссылки)
YTDL_LIST = {**_YTDL_BASE, "extract_flat": "in_playlist", "noplaylist": False, "playlistend": MAX_PLAYLIST}
# Получение реального аудиопотока прямо перед воспроизведением (ссылки на поток быстро протухают)
YTDL_STREAM = {**_YTDL_BASE, "format": "bestaudio/best", "noplaylist": True}

NORMALIZE_DEFAULT = os.getenv("NORMALIZE", "1") != "0"     # выравнивание громкости по умолчанию
VOICE_STATUS = os.getenv("VOICE_STATUS", "1") != "0"         # писать трек в статус голосового канала
NORMALIZE_AF = "dynaudnorm=f=150:g=15:p=0.9"

# ключ: (название, фильтр ffmpeg, во сколько раз быстрее идёт время трека)
FILTERS: dict[str, tuple[str, Optional[str], float]] = {
    "off": ("Без эффектов", None, 1.0),
    "bass": ("🔊 Bass boost", "bass=g=10:f=110:w=0.6", 1.0),
    "nightcore": ("🌸 Nightcore", "aresample=48000,asetrate=60000,aresample=48000", 1.25),
    "slowed": ("🌙 Slowed + reverb", "aresample=48000,asetrate=40800,aresample=48000,aecho=0.8:0.88:80:0.35", 0.85),
    "vaporwave": ("🌊 Vaporwave", "aresample=48000,asetrate=38400,aresample=48000", 0.8),
    "8d": ("🎧 8D-звук", "apulsator=hz=0.09", 1.0),
    "speed": ("⏩ Быстрее ×1.25", "atempo=1.25", 1.25),
    "slow": ("🐢 Медленнее ×0.8", "atempo=0.8", 0.8),
    "karaoke": ("🎤 Караоке (убрать голос)", "pan=stereo|c0=c0-c1|c1=c1-c0", 1.0),
}

LOOP_LABELS = {"off": "выкл", "track": "🔂 трек", "queue": "🔁 очередь"}
BAD_TITLES = {"[Private video]", "[Deleted video]", "[Приватное видео]", "[Удалённое видео]"}


# ─────────────────────────── вспомогательное ───────────────────────────

class UserError(Exception):
    """Ошибка, которую можно просто показать пользователю."""


def is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


def fmt_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return "LIVE"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(text: str) -> int:
    text = text.strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}", text):
        total = 0
        for part in text.split(":"):
            total = total * 60 + int(part)
        return total
    raise UserError("Не понял время. Пиши так: `90`, `1:30` или `1:02:03`.")


def progress_bar(pos: float, dur: Optional[float], length: int = 16) -> str:
    if not dur:
        return "🔴 Прямой эфир"
    filled = int(length * min(pos / dur, 1))
    return "▬" * filled + "🔘" + "▬" * (length - filled)


def short_error(err: Exception) -> str:
    text = str(err).replace("ERROR: ", "")
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return (text[:300] + "…") if len(text) > 300 else text


def _thumb(info: dict) -> Optional[str]:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = info.get("thumbnails") or []
    return thumbs[-1].get("url") if thumbs else None


@dataclass
class Track:
    url: str                       # ссылка на страницу (по ней каждый раз получаем свежий поток)
    title: str
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None
    requester: str = "?"
    direct: bool = False           # прямая ссылка на файл (вложение Discord)

    @property
    def label(self) -> str:
        name = discord.utils.escape_markdown(self.title)
        return f"[{name}]({self.url})" if is_url(self.url) else f"**{name}**"

    @classmethod
    def from_info(cls, info: dict, requester: str) -> Optional["Track"]:
        url = info.get("webpage_url") or info.get("url")
        if not url:
            return None
        if not is_url(url) and info.get("ie_key") == "Youtube":
            url = f"https://www.youtube.com/watch?v={url}"
        title = info.get("title") or url
        if title in BAD_TITLES:
            return None
        return cls(
            url=url,
            title=title,
            duration=info.get("duration"),
            thumbnail=_thumb(info),
            uploader=info.get("uploader") or info.get("channel"),
            requester=requester,
        )


def _ytdl_extract(opts: dict, query: str) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(query, download=False)


async def run_ytdl(opts: dict, query: str) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(_ytdl_extract, opts, query))


async def fetch_tracks(query: str, requester: str) -> tuple[list[Track], Optional[str]]:
    """Возвращает (треки, название_плейлиста_или_None)."""
    q = query if is_url(query) else f"ytsearch1:{query}"
    try:
        info = await run_ytdl(YTDL_LIST, q)
    except yt_dlp.utils.DownloadError as e:
        if "DRM" in str(e) and is_url(query):
            return await drm_fallback(query, requester)
        raise UserError(f"Не удалось загрузить: {short_error(e)}")
    if not info:
        raise UserError("Ничего не найдено.")

    if "entries" in info:
        tracks = [t for e in info["entries"] if e and (t := Track.from_info(e, requester))]
        if not tracks:
            raise UserError("Ничего не найдено.")
        playlist = info.get("title") if is_url(query) and len(tracks) > 1 else None
        return tracks[:MAX_PLAYLIST], playlist

    track = Track.from_info(info, requester)
    if not track:
        raise UserError("Это видео недоступно.")
    return [track], None


# ── Spotify / Apple Music / Яндекс Музыка: защищены DRM → ищем те же песни на YouTube ──

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def _meta(page: str, prop: str) -> Optional[str]:
    m = re.search(rf'<meta[^>]+(?:property|name)="{prop}"[^>]+content="([^"]*)"', page) or \
        re.search(rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{prop}"', page)
    return html.unescape(m.group(1)).strip() if m else None


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def _lazy(query: str, title: str, requester: str) -> Track:
    # Трек ищется на YouTube только перед воспроизведением — так длинные плейлисты добавляются мгновенно
    return Track(url=f"ytsearch1:{query}", title=title, requester=requester)


async def drm_fallback(url: str, requester: str) -> tuple[list[Track], Optional[str]]:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(headers=_BROWSER_HEADERS, timeout=timeout) as s:
            # Плейлист/альбом Spotify → список треков со встраиваемой страницы
            m = re.search(r"open\.spotify\.com/(?:intl-[\w-]+/)?(playlist|album)/(\w+)", url)
            if m:
                async with s.get(f"https://open.spotify.com/embed/{m.group(1)}/{m.group(2)}") as r:
                    page = await r.text()
                nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
                items = _find_key(json.loads(nd.group(1)), "trackList") if nd else None
                tracks = []
                for it in items or []:
                    title, artist = it.get("title"), it.get("subtitle", "")
                    if title:
                        tracks.append(_lazy(f"{artist} {title}", f"{artist} — {title}" if artist else title, requester))
                if tracks:
                    name = _find_key(json.loads(nd.group(1)), "name") or "Плейлист Spotify"
                    return tracks[:MAX_PLAYLIST], f"{name} (найдено на YouTube)"

            # Один трек: берём название и исполнителя из мета-тегов страницы
            async with s.get(url) as r:
                page = await r.text()
    except Exception as e:  # noqa: BLE001
        raise UserError(f"Сайт защищён DRM, и не получилось прочитать название песни: {short_error(e)}")

    title = _meta(page, "og:title") or ""
    desc = _meta(page, "og:description") or ""
    artist = _meta(page, "music:musician_description") or ""
    if not artist and "open.spotify.com" in url and "·" in desc:
        artist = desc.split("·")[0].strip()           # "Исполнитель · Альбом · Song · 2020"
    title = re.sub(r"\s*[|–-]\s*(Spotify|Apple Music|Яндекс Музыка|Yandex Music|Deezer).*$", "", title, flags=re.I)
    if not title:
        raise UserError("Этот сайт защищён DRM. Напиши название песни вместо ссылки.")
    query = f"{artist} {title}".strip()
    return [_lazy(query, f"{artist} — {title}" if artist else title, requester)], None


async def search_tracks(query: str, requester: str, count: int = 5) -> list[Track]:
    try:
        info = await run_ytdl(YTDL_LIST, f"ytsearch{count}:{query}")
    except yt_dlp.utils.DownloadError as e:
        raise UserError(f"Поиск не удался: {short_error(e)}")
    return [t for e in (info or {}).get("entries", []) if e and (t := Track.from_info(e, requester))]


LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def _ffmpeg_log():
    os.makedirs(LOG_DIR, exist_ok=True)
    return open(os.path.join(LOG_DIR, "ffmpeg.log"), "ab", buffering=0)


class CountingSource(discord.AudioSource):
    """Обёртка, которая считает, сколько звука реально пришло от FFmpeg (для диагностики)."""

    def __init__(self, inner: discord.AudioSource, logf=None):
        self.inner = inner
        self.logf = logf
        self.bytes_read = 0

    def read(self) -> bytes:
        data = self.inner.read()
        self.bytes_read += len(data)
        return data

    def is_opus(self) -> bool:
        return self.inner.is_opus()

    def cleanup(self):
        self.inner.cleanup()
        if self.logf:
            try:
                self.logf.close()
            except Exception:  # noqa: BLE001
                pass


# ─────────────────────────── плеер одного сервера ───────────────────────────

class GuildPlayer:
    def __init__(self, cog: "Music", guild: discord.Guild):
        self.cog = cog
        self.bot = cog.bot
        self.guild = guild
        self.queue: list[Track] = []
        self.current: Optional[Track] = None
        self.loop_mode = "off"
        self.volume = DEFAULT_VOLUME / 100
        self.text_channel: Optional[discord.abc.Messageable] = None
        self.panel_msg: Optional[discord.Message] = None

        self._queue_event = asyncio.Event()
        self._next = asyncio.Event()
        self._skip = False
        self._replay_seek: Optional[int] = None
        self.started_at = 0.0
        self.paused_at: Optional[float] = None
        self.seek_offset = 0.0
        self.rate = 1.0
        self.filter = "off"
        self.normalize = NORMALIZE_DEFAULT
        self._status_ok = True
        self.task = self.bot.loop.create_task(self._player_loop())

    # ── состояние ──
    @property
    def vc(self) -> Optional[discord.VoiceClient]:
        return self.guild.voice_client  # type: ignore[return-value]

    @property
    def position(self) -> float:
        if not self.current:
            return 0.0
        now = self.paused_at or time.monotonic()
        return max(0.0, self.seek_offset + (now - self.started_at) * self.rate)

    @property
    def is_paused(self) -> bool:
        return bool(self.vc and self.vc.is_paused())

    def add(self, tracks: list[Track], front: bool = False):
        if front:
            self.queue[0:0] = tracks
        else:
            self.queue.extend(tracks)
        self._queue_event.set()

    # ── управление ──
    def pause(self) -> bool:
        if self.vc and self.vc.is_playing():
            self.vc.pause()
            self.paused_at = time.monotonic()
            return True
        return False

    def resume(self) -> bool:
        if self.vc and self.vc.is_paused():
            self.vc.resume()
            if self.paused_at:
                self.started_at += time.monotonic() - self.paused_at
            self.paused_at = None
            return True
        return False

    def skip(self):
        self._skip = True
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()

    def stop(self):
        self.queue.clear()
        self.loop_mode = "off"
        self.current = None
        self._skip = True
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()

    def seek(self, seconds: int):
        if not self.current:
            raise UserError("Сейчас ничего не играет.")
        if self.current.duration and seconds >= self.current.duration:
            raise UserError(f"Трек длится всего {fmt_time(self.current.duration)}.")
        self._replay_seek = max(0, seconds)
        if self.vc:
            self.vc.stop()

    def set_volume(self, value: float):
        self.volume = max(0.01, min(1.5, value))
        if self.vc and isinstance(self.vc.source, discord.PCMVolumeTransformer):
            self.vc.source.volume = self.volume

    def _restart_current(self):
        """Перезапустить текущий трек с того же места (нужно, чтобы применить эффект)."""
        if self.current and self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self._replay_seek = max(1, int(self.position))
            self.vc.stop()

    def set_filter(self, name: str):
        if name not in FILTERS:
            raise UserError("Нет такого эффекта.")
        if name != self.filter:
            self.filter = name
            self._restart_current()

    def set_normalize(self, on: bool):
        if on != self.normalize:
            self.normalize = on
            self._restart_current()

    async def set_status(self, text: Optional[str]):
        """Название трека в статусе голосового канала."""
        vc = self.vc
        if not VOICE_STATUS or not self._status_ok or not vc or not vc.channel:
            return
        try:
            await self.bot.http.edit_voice_channel_status(text[:480] if text else None, channel_id=vc.channel.id)
        except discord.Forbidden:
            self._status_ok = False
            log.warning("Нет права «Устанавливать статус голосового канала» — статус с треком выключен")
        except discord.HTTPException as e:
            log.debug("Не удалось поставить статус канала: %s", e)

    def cycle_loop(self) -> str:
        order = ["off", "track", "queue"]
        self.loop_mode = order[(order.index(self.loop_mode) + 1) % 3]
        return self.loop_mode

    # ── основной цикл ──
    def _after(self, error: Optional[Exception]):
        if error:
            log.error("Ошибка воспроизведения: %s", error)
        self.bot.loop.call_soon_threadsafe(self._next.set)

    async def _wait_for_queue(self):
        while not self.queue:
            self._queue_event.clear()
            await self._queue_event.wait()

    async def _player_loop(self):
        await self.bot.wait_until_ready()
        try:
            while True:
                self._next.clear()
                seek = 0

                if self._replay_seek is not None and self.current:
                    track, seek = self.current, self._replay_seek
                    self._replay_seek = None
                elif self.loop_mode == "track" and self.current and not self._skip:
                    track = self.current
                else:
                    if self.loop_mode == "queue" and self.current:
                        self.queue.append(self.current)
                    self.current = None
                    if not self.queue:
                        await self._disable_panel()
                        await self.set_status(None)
                        try:
                            await asyncio.wait_for(self._wait_for_queue(), timeout=IDLE_TIMEOUT)
                        except asyncio.TimeoutError:
                            await self.send("💤 Очередь давно пуста — выхожу из канала.")
                            await self.destroy()
                            return
                    track = self.queue.pop(0)
                self._skip = False

                vc = self.vc
                if not vc or not vc.is_connected():
                    await self.destroy()
                    return

                try:
                    source = await self._make_source(track, seek)
                except Exception as e:  # noqa: BLE001
                    log.warning("Не удалось получить поток %s: %s", track.url, e)
                    await self.send(f"⚠️ Не получилось включить **{discord.utils.escape_markdown(track.title)}**: {short_error(e)}")
                    self.current = None
                    continue

                self.current = track
                vc.play(source, after=self._after)
                self.started_at = time.monotonic()
                self.seek_offset = seek
                self.paused_at = None
                self.rate = FILTERS[self.filter][2]

                if seek:
                    await self.update_panel()
                else:
                    await self.send_panel()
                    await self.set_status(f"🎵 {track.title}")

                await self._next.wait()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("Плеер упал")
            await self.send("❌ Внутренняя ошибка плеера, перезапусти воспроизведение командой /play.")
            await self.destroy()

    async def _make_source(self, track: Track, seek: int) -> discord.AudioSource:
        if track.direct:
            stream_url, headers = track.url, {}
        else:
            info = await run_ytdl(YTDL_STREAM, track.url)
            if info and "entries" in info:
                info = next((e for e in info["entries"] if e), None)
            if not info or not info.get("url"):
                raise UserError("не найден аудиопоток")
            stream_url = info["url"]
            headers = info.get("http_headers") or {}
            # уточняем данные трека полной информацией
            if not is_url(track.url) and info.get("webpage_url"):
                track.url = info["webpage_url"]   # трек из Spotify нашли на YouTube — запоминаем ссылку
            track.duration = info.get("duration") or track.duration
            track.thumbnail = _thumb(info) or track.thumbnail
            track.uploader = info.get("uploader") or track.uploader

        before = ["-nostdin"]
        if stream_url.startswith("http"):
            before += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        ua = headers.get("User-Agent")
        if ua:
            before += ["-user_agent", ua]
        if seek:
            before += ["-ss", str(seek)]
        chain = [f for f in (FILTERS[self.filter][1], NORMALIZE_AF if self.normalize else None) if f]
        options = ["-vn"] + (["-af", ",".join(chain)] if chain else [])
        logf = _ffmpeg_log()
        audio = discord.FFmpegPCMAudio(
            stream_url,
            executable=FFMPEG,
            before_options=" ".join(shlex.quote(x) for x in before),
            options=" ".join(shlex.quote(x) for x in options),
            stderr=logf,
        )
        audio = CountingSource(audio, logf)
        self.bot.loop.create_task(self._report_audio(audio, track))
        return discord.PCMVolumeTransformer(audio, volume=self.volume)

    async def _report_audio(self, src: "CountingSource", track: Track):
        await asyncio.sleep(6)
        if self.current is not track:
            return
        if src.bytes_read == 0:
            log.warning("FFmpeg за 6 сек не выдал ни байта звука для %s — смотри logs/ffmpeg.log", track.url)
            await self.send("⚠️ FFmpeg не отдаёт звук. Ошибки записаны в `logs/ffmpeg.log` — пришли их разработчику.")
        else:
            conn = getattr(self.vc, "_connection", None)
            sess = getattr(conn, "dave_session", None)
            log.info(
                "Звук идёт: получено %.1f сек аудио (%s) | шифрование DAVE: версия=%s, готово=%s",
                src.bytes_read / 192000, track.title,
                getattr(conn, "dave_protocol_version", "?"), getattr(sess, "ready", None),
            )

    # ── сообщения / панель ──
    async def send(self, content: str = None, **kwargs) -> Optional[discord.Message]:
        if not self.text_channel:
            return None
        try:
            return await self.text_channel.send(content, **kwargs)
        except discord.HTTPException:
            return None

    def now_embed(self) -> discord.Embed:
        t = self.current
        if not t:
            return discord.Embed(title="⏹️ Ничего не играет", description="Добавь музыку командой `/play`", color=0x2B2D31)
        status = "⏸️ Пауза" if self.is_paused else "▶️ Сейчас играет"
        e = discord.Embed(title=status, description=f"**{t.label}**", color=0x1DB954)
        e.add_field(
            name="Прогресс",
            value=f"{progress_bar(self.position, t.duration)}\n`{fmt_time(self.position)} / {fmt_time(t.duration)}`",
            inline=False,
        )
        if t.uploader:
            e.add_field(name="Автор", value=discord.utils.escape_markdown(t.uploader))
        e.add_field(name="Заказал", value=t.requester)
        e.add_field(name="Громкость", value=f"{round(self.volume * 100)}%")
        e.add_field(name="Повтор", value=LOOP_LABELS[self.loop_mode])
        if self.filter != "off":
            e.add_field(name="Эффект", value=FILTERS[self.filter][0])
        if self.queue:
            nxt = self.queue[0]
            e.add_field(name=f"Далее (в очереди {len(self.queue)})", value=nxt.label[:1000], inline=False)
        if t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        return e

    async def send_panel(self):
        await self._disable_panel(delete=True)
        self.panel_msg = await self.send(embed=self.now_embed(), view=PlayerControls(self))

    async def update_panel(self):
        if self.panel_msg:
            try:
                await self.panel_msg.edit(embed=self.now_embed(), view=PlayerControls(self))
            except discord.HTTPException:
                self.panel_msg = None

    async def _disable_panel(self, delete: bool = False):
        msg, self.panel_msg = self.panel_msg, None
        if not msg:
            return
        try:
            if delete:
                await msg.delete()
            else:
                await msg.edit(embed=self.now_embed(), view=None)
        except discord.HTTPException:
            pass

    async def destroy(self):
        self.cog.players.pop(self.guild.id, None)
        self.queue.clear()
        self.current = None
        await self._disable_panel()
        await self.set_status(None)
        vc = self.vc
        if vc:
            try:
                await vc.disconnect(force=True)
            except Exception:  # noqa: BLE001
                pass
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()


# ─────────────────────────── кнопки ───────────────────────────

async def _check_same_channel(interaction: discord.Interaction, player: GuildPlayer) -> bool:
    member = interaction.user
    vc = player.vc
    if not vc or not isinstance(member, discord.Member) or not member.voice or member.voice.channel != vc.channel:
        await interaction.response.send_message("🔇 Управлять можно, только находясь в одном голосовом канале с ботом.", ephemeral=True)
        return False
    return True


class PlayerControls(discord.ui.View):
    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player
        self.pause_btn.emoji = "▶️" if player.is_paused else "⏸️"
        self.loop_btn.style = discord.ButtonStyle.success if player.loop_mode != "off" else discord.ButtonStyle.secondary
        self.loop_btn.emoji = "🔂" if player.loop_mode == "track" else "🔁"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.player.guild.id not in self.player.cog.players:
            await interaction.response.send_message("Плеер уже выключен.", ephemeral=True)
            return False
        return await _check_same_channel(interaction, self.player)

    async def _redraw(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.player.now_embed(), view=PlayerControls(self.player))

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def pause_btn(self, interaction: discord.Interaction, _):
        if not self.player.pause():
            self.player.resume()
        await self._redraw(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.primary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, _):
        self.player.skip()
        await interaction.response.send_message(f"⏭️ {interaction.user.mention} пропустил трек.", silent=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, _):
        self.player.stop()
        await interaction.response.edit_message(embed=self.player.now_embed(), view=None)
        self.player.panel_msg = None

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=0)
    async def loop_btn(self, interaction: discord.Interaction, _):
        self.player.cycle_loop()
        await self._redraw(interaction)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle_btn(self, interaction: discord.Interaction, _):
        random.shuffle(self.player.queue)
        await self._redraw(interaction)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, _):
        self.player.set_volume(round(self.player.volume - 0.1, 2))
        await self._redraw(interaction)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction: discord.Interaction, _):
        self.player.set_volume(round(self.player.volume + 0.1, 2))
        await self._redraw(interaction)

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_btn(self, interaction: discord.Interaction, _):
        await self._redraw(interaction)

    @discord.ui.select(
        placeholder="🎛️ Эффект звука…",
        row=2,
        options=[discord.SelectOption(label=v[0], value=k) for k, v in FILTERS.items()],
    )
    async def filter_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.player.set_filter(select.values[0])
        await self._redraw(interaction)

    @discord.ui.button(label="Очередь", emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def queue_btn(self, interaction: discord.Interaction, _):
        view = QueueView(self.player)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


class QueueView(discord.ui.View):
    PER_PAGE = 10

    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=180)
        self.player = player
        self.page = 0
        self._update_buttons()

    @property
    def pages(self) -> int:
        return max(1, (len(self.player.queue) + self.PER_PAGE - 1) // self.PER_PAGE)

    def _update_buttons(self):
        self.page = min(self.page, self.pages - 1)
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.pages - 1

    def embed(self) -> discord.Embed:
        p = self.player
        e = discord.Embed(title="📜 Очередь", color=0x5865F2)
        if p.current:
            e.add_field(name="Сейчас играет", value=f"{p.current.label} `{fmt_time(p.current.duration)}`"[:1024], inline=False)
        if not p.queue:
            e.description = "Очередь пуста."
        else:
            start = self.page * self.PER_PAGE
            lines = [
                f"`{i}.` {t.label[:90]} `{fmt_time(t.duration)}` — {t.requester}"
                for i, t in enumerate(p.queue[start:start + self.PER_PAGE], start=start + 1)
            ]
            e.description = "\n".join(lines)[:4000]
        total = sum(t.duration or 0 for t in p.queue)
        e.set_footer(text=f"Стр. {self.page + 1}/{self.pages} • треков: {len(p.queue)} • всего {fmt_time(total)} • повтор: {LOOP_LABELS[p.loop_mode]}")
        return e

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)


class SearchSelect(discord.ui.Select):
    def __init__(self, cog: "Music", tracks: list[Track]):
        self.cog = cog
        self.tracks = tracks
        options = [
            discord.SelectOption(
                label=t.title[:100],
                description=f"{(t.uploader or '')[:80]} • {fmt_time(t.duration)}"[:100],
                value=str(i),
                emoji="🎵",
            )
            for i, t in enumerate(tracks)
        ]
        super().__init__(placeholder="Выбери трек…", options=options, min_values=1, max_values=len(options))

    async def callback(self, interaction: discord.Interaction):
        chosen = [self.tracks[int(v)] for v in self.values]
        await interaction.response.defer()
        try:
            player = await self.cog.ensure_player(interaction)
        except UserError as e:
            await interaction.edit_original_response(content=str(e), view=None)
            return
        player.add(chosen)
        names = "\n".join(f"• {t.label}" for t in chosen)
        await interaction.edit_original_response(content=f"✅ Добавлено:\n{names}"[:2000], view=None)
        if player.text_channel and len(chosen):
            await player.send(f"➕ {interaction.user.mention} добавил(а) в очередь: {len(chosen)} шт.", silent=True)


# ─────────────────────────── команды ───────────────────────────

class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self._alone_tasks: dict[int, asyncio.Task] = {}

    async def cog_unload(self):
        for p in list(self.players.values()):
            await p.destroy()

    def get_player(self, guild: discord.Guild) -> GuildPlayer:
        player = self.players.get(guild.id)
        if not player:
            player = GuildPlayer(self, guild)
            self.players[guild.id] = player
        return player

    async def ensure_player(self, interaction: discord.Interaction) -> GuildPlayer:
        """Подключает бота к голосовому каналу пользователя и возвращает плеер."""
        member = interaction.user
        if not interaction.guild or not isinstance(member, discord.Member):
            raise UserError("Команда работает только на сервере.")
        if not member.voice or not member.voice.channel:
            raise UserError("🔇 Сначала зайди в голосовой канал.")
        channel = member.voice.channel
        vc = interaction.guild.voice_client
        try:
            if vc is None:
                await channel.connect(self_deaf=True, timeout=20)
            elif vc.channel != channel:
                others = [m for m in vc.channel.members if not m.bot]
                if (vc.is_playing() or vc.is_paused()) and others:
                    raise UserError(f"Я уже играю в {vc.channel.mention}. Зайди туда.")
                await vc.move_to(channel)
        except asyncio.TimeoutError:
            raise UserError("Не получилось подключиться к голосовому каналу (таймаут).")
        except discord.ClientException as e:
            raise UserError(f"Не получилось подключиться: {e}")
        player = self.get_player(interaction.guild)
        if interaction.channel:
            player.text_channel = interaction.channel
        return player

    def player_for_control(self, interaction: discord.Interaction) -> GuildPlayer:
        player = self.players.get(interaction.guild_id or 0)
        if not player or not player.vc:
            raise UserError("Я сейчас ничего не играю.")
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or member.voice.channel != player.vc.channel:
            raise UserError("🔇 Нужно быть в одном голосовом канале с ботом.")
        return player

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        if isinstance(original, UserError):
            msg = str(original)
        elif isinstance(error, app_commands.CheckFailure):
            msg = "У тебя нет прав на эту команду."
        else:
            log.exception("Ошибка команды", exc_info=original)
            msg = f"❌ Ошибка: {short_error(original)}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    # ── добавление ──
    async def _enqueue(self, interaction: discord.Interaction, query: str, front: bool):
        await interaction.response.defer(thinking=True)
        player = await self.ensure_player(interaction)
        tracks, playlist = await fetch_tracks(query.strip(), interaction.user.mention)
        player.add(tracks, front=front)

        if playlist:
            e = discord.Embed(
                title="📃 Плейлист добавлен",
                description=f"**{discord.utils.escape_markdown(playlist)}** — {len(tracks)} треков",
                color=0x5865F2,
            )
        else:
            t = tracks[0]
            pos = 1 if front else len(player.queue)
            starting = player.current is None and pos == 1
            e = discord.Embed(
                title="▶️ Включаю" if starting else ("⏭️ Будет следующим" if front else "➕ Добавлено в очередь"),
                description=f"**{t.label}**",
                color=0x1DB954,
            )
            e.add_field(name="Длительность", value=fmt_time(t.duration))
            if not starting:
                e.add_field(name="Позиция", value=str(pos))
            if t.thumbnail:
                e.set_thumbnail(url=t.thumbnail)
        await interaction.followup.send(embed=e)

    @app_commands.command(name="play", description="Включить музыку: ссылка (YouTube, SoundCloud, плейлист…) или название")
    @app_commands.describe(query="Ссылка или название песни")
    async def play(self, interaction: discord.Interaction, query: str):
        await self._enqueue(interaction, query, front=False)

    @app_commands.command(name="playnext", description="Поставить трек следующим в очереди")
    @app_commands.describe(query="Ссылка или название песни")
    async def playnext(self, interaction: discord.Interaction, query: str):
        await self._enqueue(interaction, query, front=True)

    @app_commands.command(name="playfile", description="Включить свой аудио- или видеофайл (звук)")
    @app_commands.describe(file="mp3, wav, ogg, flac, m4a, mp4…")
    async def playfile(self, interaction: discord.Interaction, file: discord.Attachment):
        ctype = file.content_type or ""
        if not (ctype.startswith("audio") or ctype.startswith("video")):
            raise UserError("Это не аудио- и не видеофайл.")
        await interaction.response.defer(thinking=True)
        player = await self.ensure_player(interaction)
        track = Track(url=file.url, title=file.filename, requester=interaction.user.mention, direct=True)
        player.add([track])
        await interaction.followup.send(f"📁 Добавлен файл **{discord.utils.escape_markdown(file.filename)}**")

    @app_commands.command(name="search", description="Найти на YouTube и выбрать из 5 вариантов")
    @app_commands.describe(query="Что ищем")
    async def search(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        tracks = await search_tracks(query, interaction.user.mention)
        if not tracks:
            raise UserError("Ничего не найдено.")
        view = discord.ui.View(timeout=120)
        view.add_item(SearchSelect(self, tracks))
        await interaction.followup.send(f"🔎 Результаты по запросу **{discord.utils.escape_markdown(query)}**:", view=view, ephemeral=True)

    # ── управление ──
    @app_commands.command(name="join", description="Позвать бота в свой голосовой канал")
    async def join(self, interaction: discord.Interaction):
        player = await self.ensure_player(interaction)
        await interaction.response.send_message(f"👋 Я в {player.vc.channel.mention}")

    @app_commands.command(name="pause", description="Пауза")
    async def pause(self, interaction: discord.Interaction):
        player = self.player_for_control(interaction)
        if not player.pause():
            raise UserError("Уже на паузе или ничего не играет.")
        await interaction.response.send_message("⏸️ Пауза.")
        await player.update_panel()

    @app_commands.command(name="resume", description="Продолжить")
    async def resume(self, interaction: discord.Interaction):
        player = self.player_for_control(interaction)
        if not player.resume():
            raise UserError("Не на паузе.")
        await interaction.response.send_message("▶️ Продолжаем.")
        await player.update_panel()

    @app_commands.command(name="skip", description="Пропустить текущий трек")
    async def skip(self, interaction: discord.Interaction):
        player = self.player_for_control(interaction)
        if not player.current:
            raise UserError("Нечего пропускать.")
        title = player.current.title
        player.skip()
        await interaction.response.send_message(f"⏭️ Пропущено: **{discord.utils.escape_markdown(title)}**")

    @app_commands.command(name="skipto", description="Перейти к треку под номером в очереди")
    @app_commands.describe(position="Номер трека в /queue")
    async def skipto(self, interaction: discord.Interaction, position: app_commands.Range[int, 1]):
        player = self.player_for_control(interaction)
        if position > len(player.queue):
            raise UserError(f"В очереди только {len(player.queue)} треков.")
        skipped = player.queue[: position - 1]
        del player.queue[: position - 1]
        if player.loop_mode == "queue":
            player.queue.extend(skipped)
        player.skip()
        await interaction.response.send_message(f"⏩ Перехожу к **{discord.utils.escape_markdown(player.queue[0].title)}**")

    @app_commands.command(name="stop", description="Остановить музыку и очистить очередь")
    async def stop(self, interaction: discord.Interaction):
        player = self.player_for_control(interaction)
        player.stop()
        await interaction.response.send_message("⏹️ Остановлено, очередь очищена.")

    @app_commands.command(name="leave", description="Выгнать бота из голосового канала")
    async def leave(self, interaction: discord.Interaction):
        player = self.player_for_control(interaction)
        await player.destroy()
        await interaction.response.send_message("👋 Пока!")

    @app_commands.command(name="nowplaying", description="Что сейчас играет (панель с кнопками)")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id or 0)
        if not player or not player.current:
            raise UserError("Сейчас ничего не играет.")
        old, player.panel_msg = player.panel_msg, None
        await interaction.response.send_message(embed=player.now_embed(), view=PlayerControls(player))
        player.panel_msg = await interaction.original_response()
        if old:
            try:
                await old.delete()
            except discord.HTTPException:
                pass

    @app_commands.command(name="queue", description="Показать очередь")
    async def queue(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id or 0)
        if not player:
            raise UserError("Очередь пуста.")
        view = QueueView(player)
        await interaction.response.send_message(embed=view.embed(), view=view)

    @app_commands.command(name="volume", description="Громкость 1–150 %")
    @app_commands.describe(level="Громкость в процентах")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 150]):
        player = self.player_for_control(interaction)
        player.set_volume(level / 100)
        await interaction.response.send_message(f"🔊 Громкость: **{level}%**")
        await player.update_panel()

    @app_commands.command(name="loop", description="Режим повтора")
    @app_commands.describe(mode="Что повторять")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Выключить", value="off"),
        app_commands.Choice(name="Текущий трек", value="track"),
        app_commands.Choice(name="Всю очередь", value="queue"),
    ])
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        player = self.player_for_control(interaction)
        player.loop_mode = mode.value
        await interaction.response.send_message(f"Повтор: **{LOOP_LABELS[mode.value]}**")
        await player.update_panel()

    @app_commands.command(name="shuffle", description="Перемешать очередь")
    async def shuffle(self, interaction: discord.Interaction):
        player = self.player_for_control(interaction)
        if len(player.queue) < 2:
            raise UserError("Нечего перемешивать.")
        random.shuffle(player.queue)
        await interaction.response.send_message(f"🔀 Перемешано ({len(player.queue)} треков).")

    @app_commands.command(name="remove", description="Убрать трек из очереди")
    @app_commands.describe(position="Номер трека в /queue")
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1]):
        player = self.player_for_control(interaction)
        if position > len(player.queue):
            raise UserError(f"В очереди только {len(player.queue)} треков.")
        t = player.queue.pop(position - 1)
        await interaction.response.send_message(f"🗑️ Убрано: **{discord.utils.escape_markdown(t.title)}**")

    @app_commands.command(name="move", description="Переместить трек в очереди")
    @app_commands.describe(from_pos="Откуда (номер)", to_pos="Куда (номер)")
    async def move(self, interaction: discord.Interaction, from_pos: app_commands.Range[int, 1], to_pos: app_commands.Range[int, 1]):
        player = self.player_for_control(interaction)
        n = len(player.queue)
        if from_pos > n or to_pos > n:
            raise UserError(f"В очереди только {n} треков.")
        t = player.queue.pop(from_pos - 1)
        player.queue.insert(to_pos - 1, t)
        await interaction.response.send_message(f"↕️ **{discord.utils.escape_markdown(t.title)}** теперь на позиции {to_pos}.")

    @app_commands.command(name="clear", description="Очистить очередь (текущий трек доиграет)")
    async def clear(self, interaction: discord.Interaction):
        player = self.player_for_control(interaction)
        count = len(player.queue)
        player.queue.clear()
        await interaction.response.send_message(f"🧹 Очередь очищена ({count} треков).")

    @app_commands.command(name="filter", description="Эффект звука: bass boost, nightcore, slowed, 8D…")
    @app_commands.describe(effect="Какой эффект включить")
    @app_commands.choices(effect=[app_commands.Choice(name=v[0], value=k) for k, v in FILTERS.items()])
    async def filter(self, interaction: discord.Interaction, effect: app_commands.Choice[str]):
        player = self.player_for_control(interaction)
        player.set_filter(effect.value)
        await interaction.response.send_message(f"🎛️ Эффект: **{FILTERS[effect.value][0]}**")

    @app_commands.command(name="normalize", description="Выравнивание громкости (тихие и громкие треки звучат одинаково)")
    @app_commands.describe(enabled="Включить или выключить")
    async def normalize(self, interaction: discord.Interaction, enabled: bool):
        player = self.player_for_control(interaction)
        player.set_normalize(enabled)
        await interaction.response.send_message(f"🎚️ Выравнивание громкости: **{'включено' if enabled else 'выключено'}**")

    @app_commands.command(name="seek", description="Перемотать трек, например 1:30")
    @app_commands.describe(time="Время: 90, 1:30 или 1:02:03")
    async def seek(self, interaction: discord.Interaction, time: str):
        player = self.player_for_control(interaction)
        seconds = parse_time(time)
        player.seek(seconds)
        await interaction.response.send_message(f"⏩ Перемотано на `{fmt_time(seconds)}`")

    # ── автоотключение ──
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        if self.bot.user and member.id == self.bot.user.id:
            if before.channel and after.channel is None:  # бота отключили/кикнули
                player = self.players.get(guild.id)
                if player:
                    await player.destroy()
            return

        vc = guild.voice_client
        if not vc or not vc.channel:
            return
        humans = [m for m in vc.channel.members if not m.bot]
        task = self._alone_tasks.pop(guild.id, None)
        if task:
            task.cancel()
        if not humans:
            self._alone_tasks[guild.id] = asyncio.create_task(self._leave_if_alone(guild))

    async def _leave_if_alone(self, guild: discord.Guild):
        try:
            await asyncio.sleep(ALONE_TIMEOUT)
        except asyncio.CancelledError:
            return
        vc = guild.voice_client
        if vc and vc.channel and not [m for m in vc.channel.members if not m.bot]:
            player = self.players.get(guild.id)
            if player:
                await player.send("👋 Все ушли — я тоже выхожу.")
                await player.destroy()
            else:
                await vc.disconnect(force=True)
        self._alone_tasks.pop(guild.id, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
