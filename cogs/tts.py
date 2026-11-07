"""読み上げ（TTS）。/tts join で今のVCに入り、このテキストチャンネルの投稿を読み上げる。
音声エンジン:
  - gtts（既定・設定不要）: Google翻訳の音声。インターネット接続のみで動く
  - voicevox: 環境変数 VOICEVOX_URL（例: http://localhost:50021）にVOICEVOXエンジンがあれば使える（ずんだもん等）
設定は Redis（tts:<サーバーID> と ttsdict:<サーバーID>）に保存。
必要: pip install "discord.py[voice]" gTTS ／ ffmpeg（PATHに置くか FFMPEG_PATH で指定）"""
from __future__ import annotations

import asyncio
import io
import os
import re

import discord
import httpx
from discord import app_commands
from discord.ext import commands

import redis_kv as kv

FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")
VOICEVOX_URL = os.getenv("VOICEVOX_URL", "").rstrip("/")
MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "80"))
SPEAKERS = {"ずんだもん": 3, "四国めたん": 2, "春日部つむぎ": 8, "雨晴はう": 10, "波音リツ": 9,
            "玄野武宏": 11, "白上虎太郎": 12, "青山龍星": 13, "冥鳴ひまり": 14, "九州そら": 16}

try:
    from gtts import gTTS
except ImportError:
    gTTS = None


class Session:
    def __init__(self, text_channel_id: int):
        self.text_channels = {text_channel_id}
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=30)
        self.task: asyncio.Task | None = None


class TTS(commands.Cog):
    group = app_commands.Group(name="tts", description="テキストチャンネルの投稿をVCで読み上げます", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions: dict[int, Session] = {}
        self.http = httpx.AsyncClient(timeout=20)

    async def cog_unload(self):
        for gid in list(self.sessions):
            await self._stop(gid)
        await self.http.aclose()

    async def _settings(self, gid: int) -> dict:
        return await kv.get_json(self.bot, f"tts:{gid}", {}) or {}

    # ---------- 文章の整形 ----------
    async def _clean(self, message: discord.Message, cfg: dict) -> str:
        t = message.clean_content  # メンションは @名前 に変換済み
        t = re.sub(r"```.*?```", "コード省略", t, flags=re.S)
        t = re.sub(r"https?://\S+", "URL省略", t)
        t = re.sub(r"<a?:(\w+):\d+>", r"\1", t)
        t = re.sub(r"\|\|.*?\|\|", "ネタバレ", t, flags=re.S)
        t = re.sub(r"[*_~`>|]", "", t)
        t = re.sub(r"w{3,}$|ｗ{3,}$", "わらわら", t)
        for k, v in (await kv.hgetall(self.bot, f"ttsdict:{message.guild.id}")).items():
            t = t.replace(k, str(v))
        if message.attachments:
            t += " 添付ファイル"
        t = t.strip()
        if len(t) > MAX_CHARS:
            t = t[:MAX_CHARS] + " 以下略"
        if t and cfg.get("read_name", True):
            t = f"{message.author.display_name}、{t}"
        return t

    # ---------- 音声の生成 ----------
    async def _synth(self, text: str, cfg: dict) -> tuple[bytes, bool]:
        """(音声データ, WAVかどうか)"""
        if cfg.get("engine") == "voicevox" and VOICEVOX_URL:
            sp = int(cfg.get("speaker", 3))
            q = await self.http.post(f"{VOICEVOX_URL}/audio_query", params={"text": text, "speaker": sp})
            q.raise_for_status()
            query = q.json()
            query["speedScale"] = float(cfg.get("speed", 1.1))
            r = await self.http.post(f"{VOICEVOX_URL}/synthesis", params={"speaker": sp}, json=query)
            r.raise_for_status()
            return r.content, True
        if gTTS is None:
            raise RuntimeError("gTTS が入っていません（pip install gTTS）")

        def make():
            buf = io.BytesIO()
            gTTS(text=text, lang="ja").write_to_fp(buf)
            return buf.getvalue()
        return await asyncio.to_thread(make), False

    async def _worker(self, guild: discord.Guild):
        sess = self.sessions[guild.id]
        while True:
            text = await sess.queue.get()
            vc = guild.voice_client
            if vc is None or not vc.is_connected():
                continue
            if getattr(self.bot, "music_active", lambda _: False)(guild.id):
                continue  # 音楽再生中は読み上げない
            try:
                data, _ = await self._synth(text, await self._settings(guild.id))
            except Exception as e:
                print(f"読み上げ音声の生成に失敗: {type(e).__name__}: {e}")
                continue
            done = asyncio.Event()
            src = discord.FFmpegPCMAudio(io.BytesIO(data), pipe=True, executable=FFMPEG,
                                         options="-loglevel error")
            while vc.is_playing():
                await asyncio.sleep(0.2)
            try:
                vc.play(src, after=lambda _e: self.bot.loop.call_soon_threadsafe(done.set))
            except discord.ClientException:
                continue
            await done.wait()

    async def _stop(self, gid: int):
        sess = self.sessions.pop(gid, None)
        if sess and sess.task:
            sess.task.cancel()
        g = self.bot.get_guild(gid)
        if g and g.voice_client and not getattr(self.bot, "music_active", lambda _: False)(gid):
            await g.voice_client.disconnect(force=True)

    # ---------- イベント ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        sess = self.sessions.get(message.guild.id)
        if not sess or message.channel.id not in sess.text_channels:
            return
        if message.content.startswith((";", "//", "!")):  # 読み上げない合図
            return
        text = await self._clean(message, await self._settings(message.guild.id))
        if text:
            try:
                sess.queue.put_nowait(text)
            except asyncio.QueueFull:
                pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        g = member.guild
        if g.id not in self.sessions or g.voice_client is None:
            return
        ch = g.voice_client.channel
        if ch and not [m for m in ch.members if not m.bot]:  # 誰もいなくなったら退出
            await self._stop(g.id)

    # ---------- コマンド ----------
    @group.command(name="join", description="あなたのいるVCに参加し、このチャンネルの投稿を読み上げます")
    async def join(self, interaction: discord.Interaction):
        voice = getattr(interaction.user, "voice", None)
        if not voice or not voice.channel:
            await interaction.response.send_message("先にボイスチャンネルに入ってください。", ephemeral=True)
            return
        await interaction.response.defer()
        g = interaction.guild
        try:
            if g.voice_client is None:
                await voice.channel.connect(self_deaf=True, timeout=20)
            elif g.voice_client.channel != voice.channel:
                await g.voice_client.move_to(voice.channel)
        except Exception as e:
            await interaction.followup.send(f"❌ VCに接続できませんでした（{type(e).__name__}）。"
                                            "discord.py[voice] と ffmpeg が必要です。")
            return
        sess = self.sessions.get(g.id)
        if sess is None:
            sess = self.sessions[g.id] = Session(interaction.channel_id)
            sess.task = asyncio.create_task(self._worker(g))
        else:
            sess.text_channels.add(interaction.channel_id)
        cfg = await self._settings(g.id)
        engine = "VOICEVOX" if cfg.get("engine") == "voicevox" and VOICEVOX_URL else "Google"
        await interaction.followup.send(
            f"🔊 {voice.channel.mention} で {interaction.channel.mention} の投稿を読み上げます（音声: {engine}）。\n"
            "先頭に `;` を付けたメッセージは読み上げません。")
        await sess.queue.put("読み上げを開始します")

    @group.command(name="leave", description="読み上げを終了してVCから退出します")
    async def leave(self, interaction: discord.Interaction):
        if interaction.guild_id not in self.sessions:
            await interaction.response.send_message("読み上げ中ではありません。", ephemeral=True)
            return
        await self._stop(interaction.guild_id)
        await interaction.response.send_message("👋 読み上げを終了しました。")

    @group.command(name="channel", description="読み上げるテキストチャンネルを追加・削除します")
    @app_commands.describe(action="追加 / 削除", channel="対象チャンネル（省略で今のチャンネル）")
    @app_commands.choices(action=[app_commands.Choice(name="追加", value="add"), app_commands.Choice(name="削除", value="remove")])
    async def channel(self, interaction: discord.Interaction, action: str, channel: discord.TextChannel | None = None):
        sess = self.sessions.get(interaction.guild_id)
        if not sess:
            await interaction.response.send_message("先に `/tts join` を実行してください。", ephemeral=True)
            return
        ch = channel or interaction.channel
        (sess.text_channels.add if action == "add" else sess.text_channels.discard)(ch.id)
        names = " ".join(f"<#{c}>" for c in sess.text_channels) or "なし"
        await interaction.response.send_message(f"✅ 読み上げ対象: {names}", ephemeral=True)

    @group.command(name="settings", description="読み上げの設定（声・名前の読み上げ・速度）を変更します")
    @app_commands.describe(engine="音声エンジン", speaker="VOICEVOXの話者", read_name="発言者の名前も読むか",
                           speed="VOICEVOXの速さ（0.5〜2.0）")
    @app_commands.choices(engine=[app_commands.Choice(name="Google（設定不要）", value="gtts"),
                                  app_commands.Choice(name="VOICEVOX（VOICEVOX_URL が必要）", value="voicevox")],
                          speaker=[app_commands.Choice(name=n, value=i) for n, i in SPEAKERS.items()])
    @app_commands.default_permissions(manage_guild=True)
    async def settings(self, interaction: discord.Interaction, engine: str | None = None, speaker: int | None = None,
                       read_name: bool | None = None, speed: app_commands.Range[float, 0.5, 2.0] | None = None):
        cfg = await self._settings(interaction.guild_id)
        for k, v in (("engine", engine), ("speaker", speaker), ("read_name", read_name), ("speed", speed)):
            if v is not None:
                cfg[k] = v
        await kv.set_json(self.bot, f"tts:{interaction.guild_id}", cfg)
        sp = next((n for n, i in SPEAKERS.items() if i == cfg.get("speaker", 3)), "ずんだもん")
        note = "" if VOICEVOX_URL or cfg.get("engine") != "voicevox" else "\n⚠️ VOICEVOX_URL が未設定のため、Googleの音声で読み上げます。"
        await interaction.response.send_message(
            f"✅ エンジン: {cfg.get('engine', 'gtts')} / 話者: {sp} / 名前の読み上げ: "
            f"{'する' if cfg.get('read_name', True) else 'しない'} / 速さ: {cfg.get('speed', 1.1)}{note}", ephemeral=True)

    @group.command(name="dict", description="読み方の辞書を登録・削除・一覧表示します")
    @app_commands.describe(action="操作", word="単語", reading="読み方（追加のとき）")
    @app_commands.choices(action=[app_commands.Choice(name="追加", value="add"), app_commands.Choice(name="削除", value="remove"),
                                  app_commands.Choice(name="一覧", value="list")])
    async def dict_(self, interaction: discord.Interaction, action: str, word: str | None = None,
                    reading: str | None = None):
        k = f"ttsdict:{interaction.guild_id}"
        if action == "list":
            d = await kv.hgetall(self.bot, k)
            text = "\n".join(f"・{a} → {b}" for a, b in list(d.items())[:50]) or "登録はありません。"
            await interaction.response.send_message(text, ephemeral=True)
            return
        if not word or (action == "add" and not reading):
            await interaction.response.send_message("単語（と読み方）を入力してください。", ephemeral=True)
            return
        if action == "add":
            await kv.hset(self.bot, k, word[:50], reading[:50])
        else:
            await kv.hdel(self.bot, k, word[:50])
        await interaction.response.send_message(f"✅ 辞書を{'登録' if action == 'add' else '削除'}しました: {word}",
                                                ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TTS(bot))
