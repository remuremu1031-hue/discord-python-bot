"""霊夢BOT 録音（VC）

ボイスチャンネルの音声を、話した人ごとに別々のファイル（Ogg Opus）で録音する。
- Discord の音声は 2026年3月から全て end-to-end 暗号化（DAVE）されている。
  受信と復号には、次のライブラリが必要（requirements.txt）:
      discord.py[voice]==2.7.1
      davey>=0.1.6,<0.2
      discord-ext-voice-recv（DAVE の復号に対応した版）
- 受け取った Opus をそのまま Ogg に詰めるので、libopus や ffmpeg は使わない（軽い）。
- 録音中は VC のチャットで必ず知らせ、録音されたくない人はボタンで自分を除外できる。

コマンド: /record start /record stop /record status
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import struct
import threading
import time
import zipfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:  # 音声の受信（任意の追加ライブラリ）
    from discord.ext import voice_recv
    RECV_ERROR = ""
except Exception as e:  # 入っていなくても Bot 全体は動かす
    voice_recv = None
    RECV_ERROR = f"{type(e).__name__}: {e}"

try:
    import davey  # noqa: F401  DAVE（音声の暗号化）
    DAVE_OK = True
except Exception:
    DAVE_OK = False

REC_DIR = Path(os.getenv("RECORD_DIR", "/tmp/reimu_recordings"))
LINK_TTL = 24 * 3600          # ダウンロードリンクの有効期限
FRAME = 0.02                  # Discord の Opus は 1パケット 20ms
SILENCE = b"\xf8\xff\xfe"     # 20ms の無音（Discord が使うもの）
EMPTY_STOP = 60               # 誰もいなくなってから自動で止めるまで（秒）
DEFAULTS = {"enabled": False, "max_min": 60, "role": "", "out": ""}


def missing_reason() -> str:
    """録音に必要なものが足りないとき、その理由を返す。"""
    if voice_recv is None:
        return ("音声を受信するライブラリ（discord-ext-voice-recv）が入っていません。"
                f"requirements.txt に追加してください。（{RECV_ERROR[:120]}）")
    if not DAVE_OK:
        return "音声の暗号化（DAVE）を扱うライブラリ（davey）が入っていません。requirements.txt に追加してください。"
    return ""


# ------------------------------------------------------------------ Ogg Opus の書き出し
def _crc_table():
    t = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
        t.append(r & 0xFFFFFFFF)
    return t


_CRC = _crc_table()


def _ogg_crc(data: bytes) -> int:
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFFFFFF) ^ _CRC[((c >> 24) & 0xFF) ^ b]
    return c


class OggOpusWriter:
    """Opus パケットを、デコードせずに Ogg ファイルへ書き出す。"""

    def __init__(self, path: Path):
        self.f = open(path, "wb")
        self.serial = secrets.randbits(32)
        self.seq = 0
        self.granule = 0
        self.pending: list[bytes] = []
        self.segs = 0
        head = b"OpusHead" + struct.pack("<BBHIhB", 1, 2, 312, 48000, 0, 0)
        tags_vendor = b"ReimuBOT"
        tags = b"OpusTags" + struct.pack("<I", len(tags_vendor)) + tags_vendor + struct.pack("<I", 0)
        self._page([head], 0x02, 0)
        self._page([tags], 0x00, 0)

    def _page(self, packets: list[bytes], flags: int, granule: int) -> None:
        lacing = bytearray()
        for p in packets:
            n = len(p)
            while n >= 255:
                lacing.append(255)
                n -= 255
            lacing.append(n)
        hdr = struct.pack("<4sBBqIIIB", b"OggS", 0, flags, granule, self.serial, self.seq, 0, len(lacing))
        body = bytes(lacing) + b"".join(packets)
        crc = _ogg_crc(hdr + body)
        hdr = hdr[:22] + struct.pack("<I", crc) + hdr[26:]
        self.f.write(hdr + body)
        self.seq += 1

    def write(self, pkt: bytes) -> None:
        need = len(pkt) // 255 + 1
        if self.segs + need > 255 or len(self.pending) >= 50:
            self._page(self.pending, 0x00, self.granule)
            self.pending, self.segs = [], 0
        self.pending.append(pkt)
        self.segs += need
        self.granule += 960  # 20ms（48kHz）

    def close(self) -> None:
        if self.f.closed:
            return
        self._page(self.pending or [SILENCE], 0x04, self.granule + (0 if self.pending else 960))
        self.pending = []
        self.f.close()


class Track:
    """一人分の録音。無音の間も時間がずれないように、無音で埋める。"""

    def __init__(self, path: Path, start: float):
        self.path = path
        self.start = start
        self.w = OggOpusWriter(path)
        self.frames = 0
        self.spoke = 0

    def push(self, opus: bytes, now: float) -> None:
        expected = int((now - self.start) / FRAME)
        gap = expected - self.frames
        if gap > 2:  # 40ms 以上あいたら無音で埋める
            for _ in range(gap):
                self.w.write(SILENCE)
            self.frames += gap
        self.w.write(opus)
        self.frames += 1
        self.spoke += 1

    def close(self) -> None:
        try:
            self.w.close()
        except Exception:
            pass


class Recording:
    def __init__(self, guild: discord.Guild, channel, out_channel, starter: discord.Member, max_sec: int):
        self.guild = guild
        self.channel = channel
        self.out = out_channel
        self.starter = starter
        self.max_sec = max_sec
        self.start = time.time()
        self.dir = REC_DIR / f"{guild.id}_{int(self.start)}_{secrets.token_hex(3)}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tracks: dict[int, Track] = {}
        self.names: dict[int, str] = {}
        self.optout: set[int] = set()
        self.lock = threading.Lock()
        self.vc = None
        self.empty_since: float | None = None
        self.notified: set[int] = set()
        self.stopping = False

    def push(self, user, opus: bytes) -> None:
        if user is None or not opus or getattr(user, "bot", False):
            return
        uid = user.id
        with self.lock:
            if uid in self.optout or self.stopping:
                return
            tr = self.tracks.get(uid)
            if tr is None:
                safe = re.sub(r"[^\w\-]+", "_", getattr(user, "display_name", str(uid)))[:32] or "user"
                tr = Track(self.dir / f"{safe}_{uid}.ogg", self.start)
                self.tracks[uid] = tr
                self.names[uid] = getattr(user, "display_name", str(uid))
            tr.push(opus, time.time())

    def drop_user(self, uid: int) -> None:
        with self.lock:
            self.optout.add(uid)
            tr = self.tracks.pop(uid, None)
        if tr:
            tr.close()
            try:
                tr.path.unlink()
            except OSError:
                pass

    def close_all(self) -> None:
        with self.lock:
            self.stopping = True
            for tr in self.tracks.values():
                tr.close()


def _fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}時間{sec % 3600 // 60:02d}分" if sec >= 3600 else f"{sec // 60}分{sec % 60:02d}秒"


class OptoutView(discord.ui.View):
    def __init__(self, gid: int):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="自分の声を録音しない", emoji="🙅", style=discord.ButtonStyle.secondary,
                                        custom_id=f"xrec:optout:{gid}"))


class Recorder(commands.Cog):
    """VC の録音。"""

    group = app_commands.Group(name="record", description="ボイスチャンネルを録音します（話した人ごとのファイル）",
                               guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active: dict[int, Recording] = {}
        self.links: dict[str, tuple[Path, float, int, str]] = {}

    # ---------------- 設定 ----------------
    def _all(self) -> dict:
        try:
            return self.bot.store_load("record_config") or {}
        except Exception:
            return {}

    def cfg(self, gid: int) -> dict:
        c = dict(DEFAULTS)
        c.update(self._all().get(str(gid), {}))
        return c

    def save(self, gid: int, c: dict) -> None:
        data = self._all()
        data[str(gid)] = {k: c.get(k, v) for k, v in DEFAULTS.items()}
        self.bot.store_save("record_config", data)

    def module_state(self, gid: int) -> tuple[bool, str]:
        if missing_reason():
            return False, "（ライブラリ未導入）"
        c = self.cfg(gid)
        rec = self.active.get(gid)
        return bool(c["enabled"]), (f"（録音中 {_fmt(time.time() - rec.start)}）" if rec else "")

    def toggle(self, gid: int, on: bool) -> str:
        if on and missing_reason():
            raise ValueError("録音に必要なライブラリが入っていません（運営者向け: requirements.txt を確認してください）")
        c = self.cfg(gid)
        c["enabled"] = on
        self.save(gid, c)
        return "有効" if on else "無効"

    # ---------------- 起動・終了 ----------------
    async def cog_load(self):
        REC_DIR.mkdir(parents=True, exist_ok=True)
        app = getattr(self.bot, "web_app", None)
        if app is not None and not any(getattr(r, "path", "") == "/recordings/{token}" for r in app.routes):
            app.add_api_route("/recordings/{token}", self.download, methods=["GET"])
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()
        for gid in list(self.active):
            try:
                await self.finish(gid, "Botの再読み込み")
            except Exception:
                pass

    async def download(self, token: str):
        from fastapi.responses import FileResponse, HTMLResponse
        item = self.links.get(token) if re.fullmatch(r"[A-Za-z0-9_\-]{20,64}", token or "") else None
        if not item or item[1] < time.time() or not item[0].exists():
            return HTMLResponse("<h1>404</h1><p>リンクの有効期限が切れたか、ファイルがありません。</p>", status_code=404)
        path, _exp, _gid, name = item
        return FileResponse(path, media_type="application/zip", filename=name,
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    # ---------------- 定期処理（上限時間・無人・期限切れリンク） ----------------
    @tasks.loop(seconds=15)
    async def tick(self):
        now = time.time()
        for gid, rec in list(self.active.items()):
            try:
                vc = rec.vc
                if vc is None or not vc.is_connected():
                    await self.finish(gid, "VCから切断されたため")
                    continue
                if now - rec.start >= rec.max_sec:
                    await self.finish(gid, "上限の時間になったため")
                    continue
                humans = [m for m in vc.channel.members if not m.bot]
                if humans:
                    rec.empty_since = None
                elif rec.empty_since is None:
                    rec.empty_since = now
                elif now - rec.empty_since >= EMPTY_STOP:
                    await self.finish(gid, "VCに誰もいなくなったため")
            except Exception as e:
                print(f"録音の確認でエラー（{gid}）: {type(e).__name__}: {e}")
        for tok, (path, exp, _g, _n) in list(self.links.items()):
            if exp < now:
                self.links.pop(tok, None)
                shutil.rmtree(path.parent, ignore_errors=True)

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ---------------- 開始・停止 ----------------
    def can_use(self, member: discord.Member) -> bool:
        if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
            return True
        role = self.cfg(member.guild.id).get("role")
        return bool(role) and any(str(r.id) == str(role) for r in member.roles)

    async def begin(self, guild: discord.Guild, channel, member: discord.Member, out_channel, minutes: int | None) -> str:
        """録音を始める。結果の文章を返す（失敗は ValueError）。"""
        why = missing_reason()
        if why:
            raise ValueError(why)
        c = self.cfg(guild.id)
        if not c["enabled"]:
            raise ValueError("このサーバーでは録音が無効です。ダッシュボードの「📺 配信・VC」→「録音」で有効にしてください。")
        if guild.id in self.active:
            raise ValueError("すでに録音中です。`/record stop` で止めてから始めてください。")
        if channel is None:
            raise ValueError("録音するボイスチャンネルを指定するか、VCに参加してから実行してください。")
        me = guild.me
        perms = channel.permissions_for(me)
        if not (perms.view_channel and perms.connect):
            raise ValueError(f"{channel.mention} に接続する権限がBotにありません。")
        if guild.voice_client is not None:
            raise ValueError("Botはすでに別のボイスチャンネルに接続しています。")
        max_min = int(minutes or c["max_min"] or 60)
        max_min = max(1, min(max_min, 180))
        rec = Recording(guild, channel, out_channel, member, max_min * 60)
        try:
            vc = await channel.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False, self_mute=True, timeout=20)
        except Exception as e:
            shutil.rmtree(rec.dir, ignore_errors=True)
            raise ValueError(f"ボイスチャンネルに接続できませんでした（{type(e).__name__}）")
        rec.vc = vc

        class Sink(voice_recv.AudioSink):
            def wants_opus(self) -> bool:
                return True

            def write(self, user, data) -> None:
                try:
                    rec.push(user, getattr(data, "opus", None))
                except Exception:
                    pass

            def cleanup(self) -> None:
                pass

        try:
            vc.listen(Sink())
        except Exception as e:
            await vc.disconnect(force=True)
            shutil.rmtree(rec.dir, ignore_errors=True)
            raise ValueError(f"録音を開始できませんでした（{type(e).__name__}）")
        self.active[guild.id] = rec
        rec.notified = {m.id for m in channel.members}
        try:  # VC のチャットで、全員に録音を知らせる
            await channel.send(
                f"🔴 **このボイスチャンネルを録音しています**（開始: {member.mention}・最長 {max_min}分）\n"
                "録音されたくない人は、下のボタンを押すと、あなたの声は保存されません。",
                view=OptoutView(guild.id), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
        return f"🔴 {channel.mention} の録音を始めました（最長 {max_min}分）。止めるときは `/record stop`。"

    async def finish(self, gid: int, reason: str = "") -> str:
        rec = self.active.pop(gid, None)
        if rec is None:
            return "録音していません。"
        vc = rec.vc
        try:
            if vc is not None:
                try:
                    vc.stop_listening()
                except Exception:
                    pass
                await vc.disconnect(force=True)
        except Exception:
            pass
        rec.close_all()
        dur = time.time() - rec.start
        files = [t for t in rec.tracks.values() if t.path.exists() and t.spoke > 0]
        try:
            await rec.channel.send(f"⏹️ 録音を終了しました（{_fmt(dur)}）。", allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass
        if not files:
            shutil.rmtree(rec.dir, ignore_errors=True)
            await self._post(rec, f"⏹️ {rec.channel.mention} の録音を終了しました（{_fmt(dur)}）"
                                  + (f"・{reason}" if reason else "") + "\n話した人がいなかったため、ファイルはありません。")
            return "録音を終了しました（音声はありませんでした）。"
        stamp = time.strftime("%Y%m%d_%H%M", time.gmtime(rec.start + 9 * 3600))  # 日本時間
        zname = f"recording_{stamp}.zip"
        zpath = rec.dir / zname
        info = [f"サーバー: {rec.guild.name}", f"ボイスチャンネル: {rec.channel.name}",
                f"開始: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(rec.start + 9 * 3600))}（日本時間）",
                f"長さ: {_fmt(dur)}", f"開始した人: {rec.starter.display_name}", "",
                "話した人ごとのファイル（Ogg Opus）です。全員のファイルは同じ時刻から始まるので、",
                "編集ソフト（Audacity など）で並べると、そのまま合わせて聞けます。", ""]
        info += [f"- {rec.names.get(uid, uid)}: {t.path.name}" for uid, t in rec.tracks.items() if t in files]

        def build():
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:  # Opus は圧縮済み
                for t in files:
                    z.write(t.path, t.path.name)
                z.writestr("README.txt", "\n".join(info))
            for t in files:
                try:
                    t.path.unlink()
                except OSError:
                    pass
        await asyncio.to_thread(build)
        size = zpath.stat().st_size
        head = (f"⏹️ {rec.channel.mention} の録音が完成しました（{_fmt(dur)}・{len(files)}人分・{size / 1048576:.1f}MB）"
                + (f"\n-# 終了理由: {reason}" if reason else ""))
        limit = getattr(rec.guild, "filesize_limit", 10 * 1024 * 1024) - 256 * 1024
        if size <= limit:
            ok = await self._post(rec, head, file=discord.File(zpath, filename=zname))
            if ok:
                shutil.rmtree(rec.dir, ignore_errors=True)
                return "録音を終了し、ファイルを送信しました。"
        tok = secrets.token_urlsafe(24)
        self.links[tok] = (zpath, time.time() + LINK_TTL, gid, zname)
        base = ""
        try:
            base = (self.bot.public_base_url() or "").rstrip("/")
        except Exception:
            pass
        url = f"{base}/recordings/{tok}" if base else f"/recordings/{tok}"
        await self._post(rec, head + f"\n📥 ファイルが大きいため、ダウンロードリンクにしました（24時間有効）:\n{url}\n"
                                      "-# Botが再起動すると、期限内でもダウンロードできなくなります。早めに保存してください。")
        return "録音を終了しました。ダウンロードリンクを送信しました。"

    async def _post(self, rec: Recording, text: str, file: discord.File | None = None) -> bool:
        dest = rec.out or rec.channel
        try:
            kw = {"file": file} if file else {}
            await dest.send(text, allowed_mentions=discord.AllowedMentions.none(), **kw)
            return True
        except discord.HTTPException as e:
            print(f"録音の送信に失敗（{rec.guild.id}）: {e}")
            return False

    # ---------------- 途中で入ってきた人にも知らせる・ボタン ----------------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        rec = self.active.get(member.guild.id)
        if rec is None or member.bot or after.channel is None or after.channel.id != rec.channel.id:
            return
        if member.id in rec.notified:
            return
        rec.notified.add(member.id)
        try:
            await rec.channel.send(f"🔴 {member.mention} このボイスチャンネルは録音中です。録音されたくない場合は、下のボタンを押してください。",
                                   view=OptoutView(member.guild.id),
                                   allowed_mentions=discord.AllowedMentions(users=[member]))
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("xrec:optout:"):
            return
        rec = self.active.get(interaction.guild_id or 0)
        if rec is None:
            await interaction.response.send_message("いまは録音していません。", ephemeral=True)
            return
        rec.drop_user(interaction.user.id)
        await interaction.response.send_message("🙅 あなたの声は録音しません（ここまでの分も削除しました）。", ephemeral=True)

    # ---------------- コマンド ----------------
    @group.command(name="start", description="ボイスチャンネルの録音を始めます")
    @app_commands.describe(channel="録音するボイスチャンネル（省略するとあなたがいるVC）",
                           minutes="最長の録音時間（分・1〜180）", output="録音ファイルを送るチャンネル（省略すると設定のチャンネル）")
    async def start_cmd(self, interaction: discord.Interaction,
                        channel: discord.VoiceChannel | discord.StageChannel | None = None,
                        minutes: app_commands.Range[int, 1, 180] | None = None,
                        output: discord.TextChannel | None = None):
        m = interaction.user
        if not self.can_use(m):
            await interaction.response.send_message("録音を始める権限がありません（サーバー管理権限、または設定されたロールが必要です）。", ephemeral=True)
            return
        if channel is None and m.voice and m.voice.channel:
            channel = m.voice.channel
        c = self.cfg(interaction.guild_id)
        out = output or (interaction.guild.get_channel(int(c["out"])) if str(c.get("out") or "").isdigit() else None) \
            or interaction.channel
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            msg = await self.begin(interaction.guild, channel, m, out, minutes)
        except ValueError as e:
            msg = f"❌ {e}"
        await interaction.followup.send(msg, ephemeral=True)

    @group.command(name="stop", description="録音を止めて、ファイルを送信します")
    async def stop_cmd(self, interaction: discord.Interaction):
        rec = self.active.get(interaction.guild_id)
        if rec is None:
            await interaction.response.send_message("いまは録音していません。", ephemeral=True)
            return
        if interaction.user.id != rec.starter.id and not self.can_use(interaction.user):
            await interaction.response.send_message("録音を止める権限がありません。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await self.finish(interaction.guild_id, f"{interaction.user.display_name} が停止")
        await interaction.followup.send(msg, ephemeral=True)

    @group.command(name="status", description="録音の状況を表示します")
    async def status_cmd(self, interaction: discord.Interaction):
        why = missing_reason()
        rec = self.active.get(interaction.guild_id)
        c = self.cfg(interaction.guild_id)
        if rec:
            text = (f"🔴 録音中：{rec.channel.mention}（{_fmt(time.time() - rec.start)} / 最長 {rec.max_sec // 60}分）\n"
                    f"話した人: {len(rec.tracks)}人・録音しない人: {len(rec.optout)}人")
        else:
            text = "⏹️ いまは録音していません。"
        text += f"\n設定: {'有効' if c['enabled'] else '無効'}・最長 {c['max_min']}分"
        if why:
            text += f"\n⚠️ {why}"
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ---------------- ダッシュボード ----------------
    def render_card(self, gid: int, guild: discord.Guild, H) -> str:
        c = self.cfg(gid)
        why = missing_reason()
        rec = self.active.get(gid)
        roles = '<option value="">（サーバー管理権限を持つ人だけ）</option>' + "".join(
            H.opt(str(r.id), "@" + r.name, c.get("role") or "") for r in reversed(guild.roles)
            if not r.is_default() and not r.managed)
        status = ""
        if why:
            status = f'<div class="flash ng">{H.e(why)}</div>'
        elif rec:
            status = (f'<div class="flash">🔴 録音中：{H.e(rec.channel.name)}（{_fmt(time.time() - rec.start)}・{len(rec.tracks)}人）</div>'
                      + H.form("rec_stop", "", "録音を止める", cls="btn danger", need="admin"))
        body = (status + H.form("rec_save",
                                H.chk("enabled", c["enabled"], "録音を使えるようにする")
                                + H.lbl("録音を始められるロール") + f'<select name="role">{roles}</select>'
                                + H.lbl("録音ファイルを送るチャンネル") + H.sel("out", H.channel_options(c.get("out") or None, none_label="（コマンドを使ったチャンネル）"))
                                + H.lbl("最長の録音時間（分・1〜180）") + f'<input type="number" name="max_min" min="1" max="180" value="{int(c["max_min"])}">',
                                "保存する", need="admin"))
        return H.card("🎙️ 録音", body, need="admin",
                      note="`/record start` で開始、`/record stop` で終了します。話した人ごとのファイル（Ogg）をまとめたZIPを送ります。"
                           "録音中はVCのチャットで全員に知らせ、録音されたくない人はボタンで自分を除外できます。"
                           "誰もいなくなって1分たつか、最長時間になると自動で止まります。")

    async def dashboard_op(self, op: str, f: dict, guild: discord.Guild, P) -> str:
        if op == "rec_stop":
            return await self.finish(guild.id, "ダッシュボードから停止")
        c = self.cfg(guild.id)
        on = f.get("enabled") == "1"
        if on and missing_reason():
            raise ValueError("録音に必要なライブラリが入っていないため、有効にできません")
        role = (f.get("role") or "").strip()
        if role and not (role.isdigit() and guild.get_role(int(role))):
            raise ValueError("ロールが見つかりません")
        out = P.pick_channel(f.get("out"))
        mm = (f.get("max_min") or "60").strip()
        if not mm.isdigit() or not 1 <= int(mm) <= 180:
            raise ValueError("最長の録音時間は 1〜180 分で指定してください")
        c.update({"enabled": on, "role": role, "out": str(out.id) if out else "", "max_min": int(mm)})
        self.save(guild.id, c)
        return f"録音の設定を保存しました（{'有効' if on else '無効'}・最長 {mm}分）"


async def setup(bot: commands.Bot):
    await bot.add_cog(Recorder(bot))
