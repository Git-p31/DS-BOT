"""
DS-BOT — Discord-бот для музыки, видео и фильмов.
Запуск: python bot.py  (или start.bat)
"""
import logging
import os
import shutil
import sys

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("GUILD_ID", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ds-bot")

import dave_fix  # noqa: E402
import web  # noqa: E402

dave_fix.apply()

EXTENSIONS = ("cogs.music", "cogs.watch", "cogs.films", "cogs.panel")


class DSBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

    async def setup_hook(self):
        for ext in EXTENSIONS:
            await self.load_extension(ext)
            log.info("Загружен модуль %s", ext)

        await web.start()

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Команды синхронизированы с сервером %s (%d шт.) — появятся сразу", GUILD_ID, len(synced))
        else:
            synced = await self.tree.sync()
            log.info("Глобальные команды синхронизированы (%d шт.) — могут появляться до часа", len(synced))

    async def close(self):
        await web.stop()
        await super().close()

    async def on_ready(self):
        log.info("Бот запущен как %s (id %s)", self.user, self.user.id)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/play"))


bot = DSBot()


@bot.tree.command(name="help", description="Список команд бота")
async def help_cmd(interaction: discord.Interaction):
    e = discord.Embed(title="🎵 DS-BOT — команды", color=0x5865F2)
    e.add_field(
        name="Музыка",
        value=(
            "`/play` — включить трек / плейлист (ссылка или название)\n"
            "`/playnext` — поставить следующим\n"
            "`/playfile` — включить свой аудио/видео-файл\n"
            "`/search` — поиск с выбором из 5 вариантов\n"
            "`/pause` `/resume` `/skip` `/stop` `/leave` `/join`\n"
            "`/nowplaying` — что играет сейчас (с кнопками)"
        ),
        inline=False,
    )
    e.add_field(
        name="Очередь",
        value=(
            "`/queue` — показать очередь\n"
            "`/remove` `/move` `/skipto` `/clear` `/shuffle`\n"
            "`/loop` — повтор (выкл / трек / очередь)\n"
            "`/volume` — громкость 1–150 %\n"
            "`/filter` — эффекты: bass boost, nightcore, slowed, 8D…\n"
            "`/normalize` — выравнивание громкости\n"
            "`/seek` — перемотка (например `1:30`)"
        ),
        inline=False,
    )
    e.add_field(
        name="Видео и фильмы",
        value=(
            "`/watch` — «Смотреть вместе» YouTube в голосовом канале\n"
            "`/video` — найти видео и скинуть в чат\n"
            "`/films` — список фильмов с компьютера\n"
            "`/film` — ссылка на плеер для фильма"
        ),
        inline=False,
    )
    e.add_field(
        name="Веб-панель",
        value=(
            "`/panel` — управление музыкой из браузера или с телефона"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=e, ephemeral=True)


_lock_file = None


def single_instance() -> bool:
    """Не даёт запустить вторую копию бота (иначе кнопки и команды «не отвечают»)."""
    global _lock_file
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.lock")
    _lock_file = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            _lock_file.seek(0)
            msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def main():
    if not single_instance():
        print("❌ Бот уже запущен в другом окне! Закрой старое окно (или python.exe в диспетчере задач) и запусти снова.")
        sys.exit(2)
    if not TOKEN:
        print("❌ Не указан DISCORD_TOKEN. Открой файл .env и вставь токен бота.")
        sys.exit(2)
    from cogs.music import FFMPEG as ffmpeg
    log.info("FFmpeg: %s", ffmpeg)
    if not shutil.which(ffmpeg) and not os.path.isfile(ffmpeg):
        log.warning("⚠️ FFmpeg не найден! Музыка играть не будет. Запусти install.bat или укажи FFMPEG_PATH в .env")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
