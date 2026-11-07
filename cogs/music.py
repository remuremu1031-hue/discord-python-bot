"""音楽再生。YouTube等のURL・検索ワードから再生し、キューで管理する（yt-dlp 使用）。
音量・ループ設定は Redis（music:<サーバーID>）に保存。キューは再起動で消える（配信URLが短時間で失効するため）。
必要: pip install "discord.py[voice]" yt-dlp ／ ffmpeg
※ Render 等のデータセンターからは、YouTube に「ボットでないことを確認」と拒否されることがあります。
  その場合は YTDLP_COOKIES（cookies.txt のパス）を設定してください。"""
from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

import redis_kv as kv

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
IDLE_LEAVE = int(os.getenv("MUSIC_IDLE_SECONDS", "300"))
YDL_OPTS = {"format": "bestaudio/best", "noplaylist": False, "quiet": True, "no_warnings": True,
            "default_search": "ytsearch", "extract_flat": False, "playlistend": 25,
            "source_address": "0.0.0.0"}
if os.getenv("YTDLP_COOKIES"):
    YDL_OPTS["cookiefile"] = os.getenv("YTDLP_COOKIES")


@dataclass
class Track:
    title: str
    url: str          # 再生ページのURL（再生直前に配信URLを取り直す）
    duration: int
    requester: int
    thumb: str | None = None


def _fmt(sec: int) -> str:
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


class Player:
    def __init__(self):
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.loop = "off"  # off / one / all
        self.volume = 0.5
        self.channel_id: int | None = None
        self.next = asyncio.Event()
        self.task: asyncio.Task | None = None


class Music(commands.Cog):
    group = app_commands.Group(name="music", description="VCで音楽を再生します", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, Player] = {}
        bot.music_active = lambda gid: bool(self.players.get(gid) and self.players[gid].current)

    async def cog_unload(self):
        for gid in list(self.players):
            await self._destroy(gid)

    async def _player(self, gid: int) -> Player:
        if gid not in self.players:
            p = Player()
            cfg = await kv.get_json(self.bot, f"music:{gid}", {}) or {}
            p.volume = float(cfg.get("volume", 0.5))
            p.loop = cfg.get("loop", "off")
            self.players[gid] = p
        return self.players[gid]

    async def _save(self, gid: int, p: Player):
        await kv.set_json(self.bot, f"music:{gid}", {"volume": p.volume, "loop": p.loop})

    # ---------- 取得 ----------
    @staticmethod
    def _extract(query: str, flat: bool) -> dict:
        opts = dict(YDL_OPTS)
        if flat:
            opts["extract_flat"] = "in_playlist"
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)

    async def _search(self, query: str, requester: int) -> list[Track]:
        info = await asyncio.to_thread(self._extract, query, True)
        entries = info.get("entries") or [info]
        out = []
        for e in entries[:25]:
            if not e:
                continue
            url = e.get("webpage_url") or e.get("url")
            if url and not url.startswith("http"):
                url = f"https://www.youtube.com/watch?v={url}"
            out.append(Track(title=e.get("title") or "不明", url=url, duration=int(e.get("duration") or 0),
                             requester=requester, thumb=(e.get("thumbnails") or [{}])[-1].get("url") or e.get("thumbnail")))
        return out

    async def _stream_url(self, t: Track) -> str:
        info = await asyncio.to_thread(self._extract, t.url, False)
        if "entries" in info:
            info = info["entries"][0]
        t.duration = int(info.get("duration") or t.duration)
        t.thumb = info.get("thumbnail") or t.thumb
        return info["url"]

    # ---------- 再生ループ ----------
    async def _loop(self, guild: discord.Guild):
        p = self.players[guild.id]
        while True:
            p.next.clear()
            if not p.queue:
                p.current = None
                try:
                    await asyncio.wait_for(self._wait_queue(p), IDLE_LEAVE)
                except asyncio.TimeoutError:
                    await self._announce(guild, "⏹️ しばらく再生がなかったため退出しました。")
                    await self._destroy(guild.id)
                    return
            t = p.queue.pop(0)
            p.current = t
            vc = guild.voice_client
            if vc is None:
                await self._destroy(guild.id)
                return
            try:
                stream = await self._stream_url(t)
            except Exception as e:
                await self._announce(guild, f"⚠️ 「{t.title}」を再生できませんでした（{type(e).__name__}）。スキップします。")
                continue
            src = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(stream, executable=FFMPEG, before_options=FFMPEG_BEFORE, options="-vn -loglevel error"),
                volume=p.volume)
            while vc.is_playing():  # 読み上げが流れていたら終わるのを待つ
                await asyncio.sleep(0.2)
            vc.play(src, after=lambda _e: self.bot.loop.call_soon_threadsafe(p.next.set))
            e = discord.Embed(title="🎵 再生中", description=f"[{t.title}]({t.url})", color=0xEC4899)
            e.add_field(name="長さ", value=_fmt(t.duration) if t.duration else "ライブ/不明")
            e.add_field(name="リクエスト", value=f"<@{t.requester}>")
            if t.thumb:
                e.set_thumbnail(url=t.thumb)
            await self._announce(guild, embed=e)
            await p.next.wait()
            if p.loop == "one":
                p.queue.insert(0, t)
            elif p.loop == "all":
                p.queue.append(t)

    @staticmethod
    async def _wait_queue(p: Player):
        while not p.queue:
            await asyncio.sleep(1)

    async def _announce(self, guild: discord.Guild, text: str | None = None, embed=None):
        p = self.players.get(guild.id)
        ch = guild.get_channel(p.channel_id) if p and p.channel_id else None
        if ch:
            try:
                await ch.send(content=text, embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    async def _destroy(self, gid: int):
        p = self.players.pop(gid, None)
        if p and p.task and p.task is not asyncio.current_task():
            p.task.cancel()
        g = self.bot.get_guild(gid)
        if g and g.voice_client:
            await g.voice_client.disconnect(force=True)

    async def _ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        voice = getattr(interaction.user, "voice", None)
        if not voice or not voice.channel:
            await interaction.followup.send("先にボイスチャンネルに入ってください。")
            return None
        vc = interaction.guild.voice_client
        try:
            if vc is None:
                vc = await voice.channel.connect(self_deaf=True, timeout=20)
            elif vc.channel != voice.channel:
                await vc.move_to(voice.channel)
        except Exception as e:
            await interaction.followup.send(f"❌ VCに接続できませんでした（{type(e).__name__}）。")
            return None
        return vc

    def _same_vc(self, interaction: discord.Interaction) -> bool:
        vc = interaction.guild.voice_client
        voice = getattr(interaction.user, "voice", None)
        return bool(vc and voice and voice.channel == vc.channel)

    # ---------- コマンド ----------
    @group.command(name="play", description="曲を再生・キューに追加します（URL または 検索ワード）")
    @app_commands.describe(query="YouTube等のURL（プレイリスト可・最大25曲）か、検索ワード")
    async def play(self, interaction: discord.Interaction, query: str):
        if yt_dlp is None:
            await interaction.response.send_message("yt-dlp が入っていません（pip install yt-dlp）。", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        vc = await self._ensure_voice(interaction)
        if vc is None:
            return
        try:
            tracks = await self._search(query, interaction.user.id)
        except Exception as e:
            msg = str(e)
            hint = "（YouTubeに拒否されました。YTDLP_COOKIES の設定が必要な場合があります）" if "Sign in" in msg else ""
            await interaction.followup.send(f"❌ 曲を取得できませんでした{hint}。")
            return
        if not tracks:
            await interaction.followup.send("見つかりませんでした。")
            return
        p = await self._player(interaction.guild_id)
        p.channel_id = interaction.channel_id
        p.queue.extend(tracks)
        if p.task is None or p.task.done():
            p.task = asyncio.create_task(self._loop(interaction.guild))
        if len(tracks) == 1:
            await interaction.followup.send(f"➕ キューに追加: **{tracks[0].title}**（{len(p.queue)}番目）")
        else:
            await interaction.followup.send(f"➕ {len(tracks)} 曲をキューに追加しました。")

    @group.command(name="skip", description="今の曲をスキップします")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()) or not self._same_vc(interaction):
            await interaction.response.send_message("同じVCで再生中のときに使えます。", ephemeral=True)
            return
        vc.stop()
        await interaction.response.send_message("⏭️ スキップしました。")

    @group.command(name="pause", description="一時停止します")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing() and self._same_vc(interaction):
            vc.pause()
            await interaction.response.send_message("⏸️ 一時停止しました。")
        else:
            await interaction.response.send_message("再生中ではありません。", ephemeral=True)

    @group.command(name="resume", description="再生を再開します")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused() and self._same_vc(interaction):
            vc.resume()
            await interaction.response.send_message("▶️ 再開しました。")
        else:
            await interaction.response.send_message("一時停止中ではありません。", ephemeral=True)

    @group.command(name="stop", description="再生を止め、キューを空にして退出します")
    async def stop(self, interaction: discord.Interaction):
        if not self._same_vc(interaction):
            await interaction.response.send_message("同じVCにいるときに使えます。", ephemeral=True)
            return
        await self._destroy(interaction.guild_id)
        await interaction.response.send_message("⏹️ 停止して退出しました。")

    @group.command(name="queue", description="キューを表示します")
    async def queue(self, interaction: discord.Interaction):
        p = self.players.get(interaction.guild_id)
        if not p or (not p.current and not p.queue):
            await interaction.response.send_message("キューは空です。", ephemeral=True)
            return
        lines = [f"`{i}.` {t.title[:60]}（{_fmt(t.duration) if t.duration else '-'}）" for i, t in enumerate(p.queue[:15], 1)]
        e = discord.Embed(title="📜 キュー", color=0xEC4899,
                          description=(f"**再生中:** {p.current.title}\n\n" if p.current else "") + ("\n".join(lines) or "（次の曲はありません）"))
        e.set_footer(text=f"全{len(p.queue)}曲 ／ ループ: {p.loop} ／ 音量: {int(p.volume * 100)}%")
        await interaction.response.send_message(embed=e)

    @group.command(name="now", description="再生中の曲を表示します")
    async def now(self, interaction: discord.Interaction):
        p = self.players.get(interaction.guild_id)
        if not p or not p.current:
            await interaction.response.send_message("再生中の曲はありません。", ephemeral=True)
            return
        await interaction.response.send_message(f"🎵 再生中: [{p.current.title}]({p.current.url})")

    @group.command(name="volume", description="音量を変更します（0〜150%）")
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 150]):
        p = await self._player(interaction.guild_id)
        p.volume = percent / 100
        vc = interaction.guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = p.volume
        await self._save(interaction.guild_id, p)
        await interaction.response.send_message(f"🔊 音量を {percent}% にしました。")

    @group.command(name="loop", description="ループ再生を設定します")
    @app_commands.choices(mode=[app_commands.Choice(name="オフ", value="off"), app_commands.Choice(name="1曲ループ", value="one"),
                                app_commands.Choice(name="キュー全体ループ", value="all")])
    async def loop(self, interaction: discord.Interaction, mode: str):
        p = await self._player(interaction.guild_id)
        p.loop = mode
        await self._save(interaction.guild_id, p)
        await interaction.response.send_message(f"🔁 ループ: {dict(off='オフ', one='1曲', all='キュー全体')[mode]}")

    @group.command(name="shuffle", description="キューをシャッフルします")
    async def shuffle(self, interaction: discord.Interaction):
        p = self.players.get(interaction.guild_id)
        if not p or len(p.queue) < 2:
            await interaction.response.send_message("シャッフルする曲がありません。", ephemeral=True)
            return
        random.shuffle(p.queue)
        await interaction.response.send_message("🔀 シャッフルしました。")

    @group.command(name="remove", description="キューから曲を削除します")
    @app_commands.describe(number="キューの番号（/music queue で確認）")
    async def remove(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 500]):
        p = self.players.get(interaction.guild_id)
        if not p or number > len(p.queue):
            await interaction.response.send_message("その番号の曲はありません。", ephemeral=True)
            return
        t = p.queue.pop(number - 1)
        await interaction.response.send_message(f"🗑️ 削除しました: {t.title}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        g = member.guild
        if g.id not in self.players or g.voice_client is None:
            return
        if not [m for m in g.voice_client.channel.members if not m.bot]:
            await asyncio.sleep(60)  # 1分待っても誰も戻らなければ退出
            vc = g.voice_client
            if vc and not [m for m in vc.channel.members if not m.bot]:
                await self._announce(g, "👋 VCに誰もいなくなったため退出しました。")
                await self._destroy(g.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
