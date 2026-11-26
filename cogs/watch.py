"""
Видео: совместный просмотр YouTube прямо в голосовом канале (активность Discord
«Watch Together») и поиск видео со ссылкой в чат.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs.music import UserError, fmt_time, search_tracks, short_error

log = logging.getLogger("watch")

# Официальная активность Discord «Watch Together» (YouTube)
WATCH_TOGETHER_APP_ID = 880218394199220334


async def create_watch_invite(member: discord.Member) -> discord.Invite:
    if not member.voice or not member.voice.channel:
        raise UserError("🔇 Сначала зайди в голосовой канал — там и будем смотреть.")
    channel = member.voice.channel
    try:
        return await channel.create_invite(
            max_age=6 * 3600,
            target_type=discord.InviteTarget.embedded_application,
            target_application_id=WATCH_TOGETHER_APP_ID,
            reason=f"Watch Together для {member}",
        )
    except discord.Forbidden:
        raise UserError("У бота нет права «Создавать приглашения» в этом канале.")
    except discord.HTTPException as e:
        raise UserError(f"Discord не дал создать активность: {short_error(e)}")


class WatchTogetherButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Смотреть вместе в войсе", emoji="📺", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            return
        try:
            invite = await create_watch_invite(interaction.user)
        except UserError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await interaction.response.send_message(
            f"📺 **Watch Together** в {invite.channel.mention}\nНажми, чтобы присоединиться: {invite.url}\n"
            f"_Внутри активности вставь ссылку на видео — все увидят его синхронно._"
        )


class VideoSelect(discord.ui.Select):
    def __init__(self, tracks):
        self.tracks = tracks
        options = [
            discord.SelectOption(
                label=t.title[:100],
                description=f"{(t.uploader or '')[:80]} • {fmt_time(t.duration)}"[:100],
                value=str(i),
                emoji="🎬",
            )
            for i, t in enumerate(tracks)
        ]
        super().__init__(placeholder="Выбери видео…", options=options)

    async def callback(self, interaction: discord.Interaction):
        t = self.tracks[int(self.values[0])]
        view = discord.ui.View(timeout=600)
        view.add_item(WatchTogetherButton())
        await interaction.response.edit_message(content="✅ Отправил видео в чат.", view=None)
        # голая ссылка → Discord сам покажет встроенный плеер YouTube
        if interaction.channel:
            await interaction.channel.send(f"🎬 {interaction.user.mention} предлагает посмотреть:\n{t.url}", view=view)


class Watch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        msg = str(original) if isinstance(original, UserError) else f"❌ Ошибка: {short_error(original)}"
        if not isinstance(original, UserError):
            log.exception("Ошибка команды", exc_info=original)
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="watch", description="Смотреть YouTube вместе прямо в голосовом канале (Watch Together)")
    async def watch(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            raise UserError("Команда работает только на сервере.")
        await interaction.response.defer()
        invite = await create_watch_invite(interaction.user)
        e = discord.Embed(
            title="📺 Watch Together",
            description=(
                f"Совместный просмотр YouTube в {invite.channel.mention}.\n\n"
                f"**[▶️ Нажми, чтобы присоединиться]({invite.url})**\n\n"
                "Внутри активности можно искать видео, ставить очередь, паузу — "
                "у всех участников видео идёт синхронно."
            ),
            color=0xFF0000,
        )
        await interaction.followup.send(content=invite.url, embed=e)

    @app_commands.command(name="video", description="Найти видео на YouTube и отправить в чат")
    @app_commands.describe(query="Название видео или ссылка")
    async def video(self, interaction: discord.Interaction, query: str):
        query = query.strip()
        view = discord.ui.View(timeout=600)
        view.add_item(WatchTogetherButton())
        if query.startswith(("http://", "https://")):
            await interaction.response.send_message(f"🎬 {query}", view=view)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        tracks = await search_tracks(query, interaction.user.mention, count=5)
        if not tracks:
            raise UserError("Ничего не найдено.")
        pick = discord.ui.View(timeout=120)
        pick.add_item(VideoSelect(tracks))
        await interaction.followup.send(f"🔎 Видео по запросу **{discord.utils.escape_markdown(query)}**:", view=pick, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Watch(bot))
