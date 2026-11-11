"""霊夢BOT 拡張モジュール（ダッシュボードの「モジュール」と連携）。

  保護      : OCR画像検閲（Google Cloud Vision。環境変数 GOOGLE_VISION_API_KEY）
  参加・交流: イベント（参加ボタン・開始前リマインド・Discordのイベント登録）／アンケート・日程調整（ボタン投票）
  運営・管理: 翻訳（DeepL。環境変数 DEEPL_API_KEY）／自動メッセージ／週次レポート／FAQ／スレッド管理
  配信・通知: YouTube Push（WebSub＋RSSの予備確認）／代理送信（テキスト・埋め込み・Webhook、任意メンション）
  VC・保存  : VC連動公開（VC参加中だけ聞き専チャンネルを表示）／チャンネルHTML保存／録音（recorder.py）

設定は bot.store_load("extras_config")[サーバーID]、投票・イベントは "extras_polls" / "extras_events"。
ダッシュボード（guild_dashboard.py）からは render() と dashboard_op() を呼ぶ。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import discord
import httpx
from discord import app_commands
from discord.ext import commands, tasks

JST = timezone(timedelta(hours=9))
DEEPL_KEY = os.getenv("DEEPL_API_KEY", "")
VISION_KEY = os.getenv("GOOGLE_VISION_API_KEY", "")
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
HUB = "https://pubsubhubbub.appspot.com/subscribe"
INVITE_RE = re.compile(r"(discord\.gg|discord(app)?\.com/invite)/\w+", re.I)
FLAG_LANG = {"🇯🇵": "JA", "🇺🇸": "EN-US", "🇬🇧": "EN-GB", "🇰🇷": "KO", "🇨🇳": "ZH-HANS", "🇹🇼": "ZH-HANT",
             "🇫🇷": "FR", "🇩🇪": "DE", "🇪🇸": "ES", "🇮🇹": "IT", "🇷🇺": "RU", "🇵🇹": "PT-PT", "🇧🇷": "PT-BR",
             "🇮🇩": "ID", "🇹🇷": "TR", "🇺🇦": "UK", "🇳🇱": "NL", "🇵🇱": "PL", "🇸🇪": "SV", "🇩🇰": "DA",
             "🇫🇮": "FI", "🇬🇷": "EL", "🇨🇿": "CS", "🇭🇺": "HU", "🇷🇴": "RO", "🇧🇬": "BG", "🇸🇰": "SK",
             "🇸🇮": "SL", "🇪🇪": "ET", "🇱🇻": "LV", "🇱🇹": "LT", "🇳🇴": "NB", "🇸🇦": "AR", "🇻🇳": "VI"}
LANG_NAME = {"JA": "日本語", "EN-US": "英語（米）", "EN-GB": "英語（英）", "KO": "韓国語", "ZH-HANS": "中国語（簡体）",
             "ZH-HANT": "中国語（繁体）", "FR": "フランス語", "DE": "ドイツ語", "ES": "スペイン語", "IT": "イタリア語",
             "RU": "ロシア語", "PT-PT": "ポルトガル語", "PT-BR": "ポルトガル語（ブラジル）", "ID": "インドネシア語",
             "TR": "トルコ語", "UK": "ウクライナ語", "NL": "オランダ語", "PL": "ポーランド語", "SV": "スウェーデン語",
             "DA": "デンマーク語", "FI": "フィンランド語", "EL": "ギリシャ語", "CS": "チェコ語", "HU": "ハンガリー語",
             "RO": "ルーマニア語", "BG": "ブルガリア語", "SK": "スロバキア語", "SL": "スロベニア語", "ET": "エストニア語",
             "LV": "ラトビア語", "LT": "リトアニア語", "NB": "ノルウェー語", "AR": "アラビア語", "VI": "ベトナム語"}
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
DEFAULTS = {
    "ocr": {"enabled": False, "words": [], "invites": True, "action": "delete", "timeout": 0},
    "translate": {"enabled": False, "channels": {}, "reaction": True},
    "automsg": {"enabled": True, "items": []},
    "report": {"enabled": False, "channel": None, "weekday": 0, "time": "09:00", "last": "", "last_members": None},
    "faq": {"enabled": True, "items": [], "autoreply": False},
    "threads": {"enabled": False, "channels": {}},
    "youtube": {"enabled": True, "subs": []},
    "vclink": {"enabled": True, "pairs": {}},
}
# ダッシュボードの操作ごとに必要な権限
EXT_NEED = {"x_ocr": "admin", "x_event": "manage_guild", "x_poll": "manage_guild", "x_poll_close": "manage_guild",
            "x_event_delete": "manage_guild", "x_translate": "manage_guild", "x_automsg_add": "manage_guild",
            "x_automsg_del": "manage_guild", "x_report": "manage_guild", "x_faq_add": "manage_guild",
            "x_faq_del": "manage_guild", "x_faq_panel": "manage_guild", "x_faq_set": "manage_guild",
            "x_threads": "manage_guild", "x_youtube_add": "manage_guild", "x_youtube_del": "manage_guild",
            "x_say": "manage_guild", "x_vclink": "admin", "x_html": "admin"}


def _now() -> datetime:
    return datetime.now(JST)


def _bar(n: int, total: int, width: int = 12) -> str:
    filled = round(width * n / total) if total else 0
    return "▰" * filled + "▱" * (width - filled)


def _parse_dt(raw: str) -> datetime:
    raw = (raw or "").strip().replace("/", "-").replace("T", " ")
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except ValueError:
        raise ValueError("日時は「2026-12-24 20:00」の形で入力してください")


class Extras(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.http = httpx.AsyncClient(timeout=20, headers={"User-Agent": "ReimuBot/1.0"})
        self._stats: dict[int, dict] = {}
        self._yt_seen: set[str] = set()
        self._yt_boot = False
        self._ocr_rate: dict[int, list[float]] = {}
        self._faq_cool: dict[tuple[int, int], float] = {}
        self._yt_secret = hashlib.sha256((os.getenv("DISCORD_CLIENT_SECRET", "") + "yt-websub").encode()).hexdigest()

    async def cog_load(self):
        self.minute_loop.start()
        self.yt_loop.start()
        app = getattr(self.bot, "web_app", None)
        if app is not None and not getattr(app, "_ext_routes", False):
            app.add_api_route("/youtube/websub", self.websub_get, methods=["GET"])
            app.add_api_route("/youtube/websub", self.websub_post, methods=["POST"])
            app._ext_routes = True
        st = self.bot.store_load("extras_stats")
        self._stats = {int(k): v for k, v in st.items()}

    async def cog_unload(self):
        self.minute_loop.cancel()
        self.yt_loop.cancel()
        self._save_stats()
        await self.http.aclose()

    # ================= 設定 =================
    def cfg(self, gid: int) -> dict:
        raw = self.bot.store_load("extras_config").get(str(gid), {})
        out = {}
        for k, d in DEFAULTS.items():
            out[k] = {**json.loads(json.dumps(d)), **(raw.get(k) or {})}
        return out

    def save(self, gid: int, cfg: dict) -> None:
        data = self.bot.store_load("extras_config")
        data[str(gid)] = cfg
        self.bot.store_save("extras_config", data)

    def _suspended(self, gid: int) -> bool:
        return getattr(self.bot, "is_suspended", lambda _: False)(gid)

    def module_states(self, gid: int) -> dict:
        """モジュール一覧のカード用（有効か・説明の補足）。"""
        c = self.cfg(gid)
        polls = [p for p in self.bot.store_load("extras_polls").values() if p.get("guild") == gid and not p.get("closed")]
        events = [e for e in self.bot.store_load("extras_events").values() if e.get("guild") == gid and not e.get("done")]
        return {
            "ocr": (c["ocr"]["enabled"], "" if VISION_KEY else "（APIキー未設定）"),
            "events": (bool(events), f"（予定 {len(events)}件）"),
            "polls": (bool(polls), f"（受付中 {len(polls)}件）"),
            "translate": (c["translate"]["enabled"], "" if DEEPL_KEY else "（APIキー未設定）"),
            "automsg": (c["automsg"]["enabled"] and bool(c["automsg"]["items"]), f"（{len(c['automsg']['items'])}件）"),
            "report": (c["report"]["enabled"] and bool(c["report"]["channel"]), ""),
            "faq": (c["faq"]["enabled"] and bool(c["faq"]["items"]), f"（{len(c['faq']['items'])}件）"),
            "threads": (c["threads"]["enabled"] and bool(c["threads"]["channels"]), ""),
            "youtube": (c["youtube"]["enabled"] and bool(c["youtube"]["subs"]), f"（{len(c['youtube']['subs'])}チャンネル）"),
            "vclink": (c["vclink"]["enabled"] and bool(c["vclink"]["pairs"]), f"（{len(c['vclink']['pairs'])}組）"),
        }

    def toggle(self, gid: int, key: str, on: bool) -> str:
        c = self.cfg(gid)
        if key not in c:
            raise ValueError("このモジュールは切り替えられません")
        if on and key == "ocr" and not VISION_KEY:
            raise ValueError("OCRを使うには、運営者が GOOGLE_VISION_API_KEY を設定する必要があります")
        if on and key == "translate" and not DEEPL_KEY:
            raise ValueError("翻訳を使うには、運営者が DEEPL_API_KEY を設定する必要があります")
        if on and key == "report" and not c["report"]["channel"]:
            raise ValueError("先にレポートの送信先を設定してください")
        c[key]["enabled"] = on
        self.save(gid, c)
        return "有効" if on else "無効"

    # ================= 統計（週次レポート用） =================
    def _st(self, gid: int) -> dict:
        s = self._stats.setdefault(gid, {})
        for k, v in (("msgs", 0), ("joins", 0), ("leaves", 0), ("ch", {}), ("users", {}), ("vc_min", 0)):
            s.setdefault(k, v)
        return s

    def _save_stats(self):
        self.bot.store_save("extras_stats", {str(k): v for k, v in self._stats.items()})

    @commands.Cog.listener()
    async def on_member_join(self, member):
        self._st(member.guild.id)["joins"] += 1

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload):
        self._st(payload.guild_id)["leaves"] += 1

    # ================= メッセージ（統計・スレッド・翻訳・FAQ・OCR） =================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        gid = message.guild.id
        s = self._st(gid)
        s["msgs"] += 1
        s["ch"][str(message.channel.id)] = s["ch"].get(str(message.channel.id), 0) + 1
        s["users"][str(message.author.id)] = s["users"].get(str(message.author.id), 0) + 1
        c = self.cfg(gid)
        if c["ocr"]["enabled"] and VISION_KEY and message.attachments:
            if await self._ocr_check(message, c["ocr"]):
                return  # 削除した
        th = c["threads"]
        if th["enabled"] and str(message.channel.id) in th["channels"] and isinstance(message.channel, discord.TextChannel):
            name = re.sub(r"\s+", " ", message.content or "").strip()[:60] or f"{message.author.display_name} のスレッド"
            try:
                await message.create_thread(name=name, auto_archive_duration=int(th["channels"][str(message.channel.id)]))
            except discord.HTTPException:
                pass
        tr = c["translate"]
        if tr["enabled"] and DEEPL_KEY and str(message.channel.id) in tr["channels"] and message.content:
            await self._translate_reply(message, tr["channels"][str(message.channel.id)])
        faq = c["faq"]
        if faq["enabled"] and faq["autoreply"] and message.content:
            text = message.content.lower()
            for i, item in enumerate(faq["items"]):
                kws = [k.lower() for k in item.get("keywords", []) if k]
                if kws and any(k in text for k in kws):
                    key = (message.channel.id, i)
                    if time.time() - self._faq_cool.get(key, 0) < 120:
                        break
                    self._faq_cool[key] = time.time()
                    e = discord.Embed(title=f"❓ {item['q']}", description=item["a"][:3500], color=0x3B82F6)
                    e.set_footer(text="よくある質問からの自動回答")
                    try:
                        await message.reply(embed=e, mention_author=False)
                    except discord.HTTPException:
                        pass
                    break

    # ---------- OCR ----------
    async def _ocr_text(self, data: bytes) -> str:
        body = {"requests": [{"image": {"content": base64.b64encode(data).decode()},
                              "features": [{"type": "TEXT_DETECTION"}]}]}
        r = await self.http.post(f"https://vision.googleapis.com/v1/images:annotate?key={VISION_KEY}", json=body)
        r.raise_for_status()
        res = (r.json().get("responses") or [{}])[0]
        return (res.get("fullTextAnnotation") or {}).get("text", "")

    async def _ocr_check(self, message: discord.Message, oc: dict) -> bool:
        perms = message.author.guild_permissions
        if perms.administrator or perms.manage_messages:
            return False
        now = time.time()
        rate = [t for t in self._ocr_rate.get(message.guild.id, []) if now - t < 60]
        words = [w.lower().replace(" ", "") for w in oc.get("words", []) if w]
        if not words and not oc.get("invites"):
            return False
        for a in message.attachments[:4]:
            if not (a.content_type or "").startswith("image/") or a.size > 5_000_000:
                continue
            if len(rate) >= 20:  # 1サーバー1分20枚まで（APIの無料枠を守る）
                break
            rate.append(now)
            try:
                text = await self._ocr_text(await a.read())
            except Exception as e:
                print(f"OCRに失敗: {type(e).__name__}")
                continue
            flat = text.lower().replace(" ", "").replace("\n", "")
            hit = next((w for w in words if w in flat), None)
            if hit is None and oc.get("invites") and INVITE_RE.search(text.replace(" ", "")):
                hit = "招待リンク"
            if hit:
                self._ocr_rate[message.guild.id] = rate
                return await self._ocr_action(message, oc, hit)
        self._ocr_rate[message.guild.id] = rate
        return False

    async def _ocr_action(self, message, oc, hit) -> bool:
        deleted = False
        if oc.get("action") == "delete":
            try:
                await message.delete()
                deleted = True
            except discord.HTTPException:
                pass
        if int(oc.get("timeout") or 0) > 0:
            try:
                await message.author.timeout(timedelta(minutes=int(oc["timeout"])), reason=f"OCR画像検閲: {hit}")
            except discord.HTTPException:
                pass
        e = discord.Embed(title="🔍 OCR画像検閲", color=0xF59E0B,
                          description=f"画像内に禁止ワード「{hit}」を検出しました。")
        e.add_field(name="送信者", value=f"{message.author.mention}（`{message.author.id}`）")
        e.add_field(name="チャンネル", value=message.channel.mention)
        e.add_field(name="対処", value="削除" if deleted else "記録のみ")
        fn = getattr(self.bot, "guild_log", None)
        if fn:
            await fn(message.guild, e)
        if deleted:
            try:
                await message.channel.send(f"{message.author.mention} 画像に使用できない内容が含まれていたため、削除しました。",
                                           delete_after=8, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass
        return deleted

    # ---------- 翻訳 ----------
    async def translate(self, text: str, target: str) -> tuple[str, str]:
        if not DEEPL_KEY:
            raise RuntimeError("DEEPL_API_KEY が未設定です")
        host = "api-free.deepl.com" if DEEPL_KEY.endswith(":fx") else "api.deepl.com"
        r = await self.http.post(f"https://{host}/v2/translate", headers={"Authorization": f"DeepL-Auth-Key {DEEPL_KEY}"},
                                 data={"text": text[:3000], "target_lang": target})
        r.raise_for_status()
        t = r.json()["translations"][0]
        return t["text"], t.get("detected_source_language", "?")

    async def _translate_reply(self, message: discord.Message, target: str, *, force: bool = False):
        try:
            out, src = await self.translate(message.content, target)
        except Exception as e:
            print(f"翻訳に失敗: {type(e).__name__}")
            return
        if not force and (src.upper() == target.split("-")[0].upper() or out.strip() == message.content.strip()):
            return  # すでに目的の言語
        e = discord.Embed(description=out[:4000], color=0x3B82F6)
        e.set_author(name=f"🌐 {LANG_NAME.get(src.upper(), src)} → {LANG_NAME.get(target, target)}",
                     icon_url=message.author.display_avatar.url)
        try:
            await message.reply(embed=e, mention_author=False)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.user_id == getattr(self.bot.user, "id", 0):
            return
        target = FLAG_LANG.get(str(payload.emoji))
        if not target or not DEEPL_KEY:
            return
        c = self.cfg(payload.guild_id)
        if not (c["translate"]["enabled"] and c["translate"]["reaction"]):
            return
        ch = self.bot.get_channel(payload.channel_id)
        if ch is None:
            return
        try:
            msg = await ch.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        if msg.content:
            await self._translate_reply(msg, target, force=True)

    @app_commands.command(name="translate", description="文章を翻訳します（DeepL）")
    @app_commands.describe(text="翻訳する文章", target="翻訳先の言語")
    @app_commands.choices(target=[app_commands.Choice(name=v, value=k) for k, v in list(LANG_NAME.items())[:25]])
    async def translate_cmd(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 2000], target: str = "JA"):
        if not DEEPL_KEY:
            await interaction.response.send_message("翻訳機能は、運営者がAPIキーを設定するまで使えません。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            out, src = await self.translate(text, target)
        except Exception:
            await interaction.followup.send("翻訳に失敗しました。時間をおいてお試しください。")
            return
        await interaction.followup.send(embed=discord.Embed(
            title=f"🌐 {LANG_NAME.get(src.upper(), src)} → {LANG_NAME.get(target, target)}", description=out[:4000], color=0x3B82F6))

    # ================= アンケート・日程調整 =================
    def _poll_embed(self, p: dict) -> discord.Embed:
        votes = p.get("votes", {})
        counts = [0] * len(p["options"])
        for sel in votes.values():
            for i in sel:
                if 0 <= i < len(counts):
                    counts[i] += 1
        total_voters = len(votes)
        lines = []
        top = max(counts) if counts else 0
        for i, (opt, n) in enumerate(zip(p["options"], counts)):
            mark = "🏆 " if p.get("closed") and n == top and n > 0 else ""
            lines.append(f"**{i + 1}. {mark}{opt}**\n`{_bar(n, max(total_voters, 1))}` {n}票")
        title = ("📅 日程調整：" if p.get("kind") == "schedule" else "📊 ") + p["question"]
        e = discord.Embed(title=title + ("（締め切り）" if p.get("closed") else ""), description="\n".join(lines),
                          color=0x6B7280 if p.get("closed") else 0x3B82F6)
        foot = f"投票者 {total_voters}人 ・ {'複数選択できます' if p.get('multi') else '1つだけ選べます（押し直しで変更）'}"
        if p.get("deadline") and not p.get("closed"):
            foot += " ・ 締め切り " + datetime.fromtimestamp(p["deadline"], JST).strftime("%m/%d %H:%M")
        e.set_footer(text=foot)
        return e

    def _poll_view(self, pid: str, p: dict) -> discord.ui.View:
        v = discord.ui.View(timeout=None)
        for i, opt in enumerate(p["options"][:20]):
            v.add_item(discord.ui.Button(label=f"{i + 1}. {opt}"[:80], style=discord.ButtonStyle.secondary,
                                         custom_id=f"xpoll:{pid}:{i}", disabled=bool(p.get("closed")), row=i // 4))
        return v

    async def create_poll(self, channel: discord.TextChannel, question: str, options: list[str], *, multi: bool,
                          kind: str = "poll", hours: float = 0, by: int = 0) -> str:
        options = [o.strip()[:70] for o in options if o.strip()][:20]
        if len(options) < 2:
            raise ValueError("選択肢は2つ以上入力してください")
        pid = secrets.token_hex(4)
        p = {"guild": channel.guild.id, "channel": channel.id, "question": question[:200], "options": options,
             "multi": multi, "kind": kind, "votes": {}, "closed": False, "by": by,
             "deadline": time.time() + hours * 3600 if hours else None}
        msg = await channel.send(embed=self._poll_embed(p), view=self._poll_view(pid, p))
        p["message"] = msg.id
        data = self.bot.store_load("extras_polls")
        data[pid] = p
        self.bot.store_save("extras_polls", data)
        return pid

    async def close_poll(self, pid: str) -> dict | None:
        data = self.bot.store_load("extras_polls")
        p = data.get(pid)
        if not p or p.get("closed"):
            return p
        p["closed"] = True
        self.bot.store_save("extras_polls", data)
        ch = self.bot.get_channel(p["channel"])
        if ch:
            try:
                await ch.get_partial_message(p["message"]).edit(embed=self._poll_embed(p), view=self._poll_view(pid, p))
            except discord.HTTPException:
                pass
        return p

    poll = app_commands.Group(name="poll", description="アンケート・日程調整", guild_only=True,
                              default_permissions=discord.Permissions(manage_guild=True))

    @poll.command(name="create", description="アンケートを作成します（ボタンで投票）")
    @app_commands.describe(question="質問", options="選択肢（カンマ区切り・最大20）", multi="複数選択を許可する",
                           hours="締め切りまでの時間（0で手動）")
    async def poll_create(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 200], options: str,
                          multi: bool = False, hours: app_commands.Range[float, 0, 720] = 0):
        try:
            await self.create_poll(interaction.channel, question, re.split(r"[,、\n]", options), multi=multi, hours=hours,
                                   by=interaction.user.id)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message("✅ アンケートを作成しました。", ephemeral=True)

    @poll.command(name="schedule", description="日程調整（候補日時の投票）を作成します")
    @app_commands.describe(title="イベント名", candidates="候補日時（カンマ区切り 例: 12/24 20:00, 12/25 21:00）",
                           hours="締め切りまでの時間（0で手動）")
    async def poll_schedule(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 200], candidates: str,
                            hours: app_commands.Range[float, 0, 720] = 0):
        try:
            await self.create_poll(interaction.channel, title, re.split(r"[,、\n]", candidates), multi=True,
                                   kind="schedule", hours=hours, by=interaction.user.id)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message("✅ 日程調整を作成しました（参加できる日時を何個でも選べます）。", ephemeral=True)

    # ================= イベント =================
    def _event_embed(self, ev: dict) -> discord.Embed:
        start = datetime.fromtimestamp(ev["start"], JST)
        e = discord.Embed(title=f"🎉 {ev['title']}", description=ev.get("description") or "",
                          color=0x6B7280 if ev.get("done") else 0x3B82F6)
        e.add_field(name="🗓️ 日時", value=f"{start:%Y/%m/%d}（{WEEKDAYS[start.weekday()]}） {start:%H:%M}〜"
                                         f"\n<t:{int(ev['start'])}:R>", inline=True)
        if ev.get("location"):
            e.add_field(name="📍 場所", value=ev["location"][:200], inline=True)
        ps = ev.get("participants", [])
        e.add_field(name=f"🙋 参加者（{len(ps)}人）", value=" ".join(f"<@{u}>" for u in ps[:40]) or "まだいません", inline=False)
        e.set_footer(text=f"開始{ev.get('remind', 30)}分前に、参加者へお知らせします")
        return e

    def _event_view(self, eid: str, done: bool) -> discord.ui.View:
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="参加する", emoji="🙋", style=discord.ButtonStyle.primary,
                                     custom_id=f"xev:join:{eid}", disabled=done))
        v.add_item(discord.ui.Button(label="参加をやめる", style=discord.ButtonStyle.secondary,
                                     custom_id=f"xev:leave:{eid}", disabled=done))
        return v

    async def create_event(self, channel: discord.TextChannel, title: str, start: datetime, *, description: str = "",
                           location: str = "", remind: int = 30, discord_event: bool = False, by: int = 0) -> str:
        if start <= _now():
            raise ValueError("開始日時は、今より後にしてください")
        eid = secrets.token_hex(4)
        ev = {"guild": channel.guild.id, "channel": channel.id, "title": title[:100], "description": description[:1500],
              "location": location[:100], "start": start.timestamp(), "remind": max(0, min(remind, 1440)),
              "participants": [], "reminded": False, "started": False, "done": False, "by": by}
        msg = await channel.send(embed=self._event_embed(ev), view=self._event_view(eid, False))
        ev["message"] = msg.id
        if discord_event:
            try:
                se = await channel.guild.create_scheduled_event(
                    name=ev["title"], start_time=start, end_time=start + timedelta(hours=2),
                    entity_type=discord.EntityType.external, location=ev["location"] or channel.name,
                    privacy_level=discord.PrivacyLevel.guild_only, description=ev["description"][:1000] or None)
                ev["scheduled"] = se.id
            except discord.HTTPException as e:
                print(f"Discordイベントの登録に失敗: {type(e).__name__}")
        data = self.bot.store_load("extras_events")
        data[eid] = ev
        self.bot.store_save("extras_events", data)
        return eid

    event = app_commands.Group(name="event", description="イベントの作成・管理", guild_only=True,
                               default_permissions=discord.Permissions(manage_guild=True))

    @event.command(name="create", description="イベントを作成します（参加ボタン・開始前のお知らせ付き）")
    @app_commands.describe(title="イベント名", start="開始日時（例: 2026-12-24 20:00）", description="説明",
                           location="場所（VC名など）", remind="開始の何分前に知らせるか", discord_event="Discordのイベントにも登録する")
    async def event_create(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 100], start: str,
                           description: str = "", location: str = "", remind: app_commands.Range[int, 0, 1440] = 30,
                           discord_event: bool = False):
        try:
            await self.create_event(interaction.channel, title, _parse_dt(start), description=description,
                                    location=location, remind=remind, discord_event=discord_event, by=interaction.user.id)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message("✅ イベントを作成しました。", ephemeral=True)

    # ================= ボタン（投票・イベント） =================
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component or interaction.guild is None:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if cid.startswith("xpoll:"):
            await self._on_poll_button(interaction, cid)
        elif cid.startswith("xev:"):
            await self._on_event_button(interaction, cid)
        elif cid == "xfaq:open":
            await self._faq_open(interaction)
        elif cid == "xfaq:pick":
            await self._faq_pick(interaction)

    async def _on_poll_button(self, interaction, cid):
        try:
            _, pid, idx = cid.split(":")
            idx = int(idx)
        except ValueError:
            return
        data = self.bot.store_load("extras_polls")
        p = data.get(pid)
        if not p or p.get("closed"):
            await interaction.response.send_message("このアンケートは締め切られています。", ephemeral=True)
            return
        uid = str(interaction.user.id)
        sel = set(p["votes"].get(uid, []))
        if idx in sel:
            sel.discard(idx)
            note = "投票を取り消しました"
        else:
            sel = (sel | {idx}) if p.get("multi") else {idx}
            note = f"「{p['options'][idx]}」に投票しました"
        if sel:
            p["votes"][uid] = sorted(sel)
        else:
            p["votes"].pop(uid, None)
        self.bot.store_save("extras_polls", data)
        await interaction.response.edit_message(embed=self._poll_embed(p))
        try:
            await interaction.followup.send(f"✅ {note}", ephemeral=True)
        except discord.HTTPException:
            pass

    async def _on_event_button(self, interaction, cid):
        try:
            _, kind, eid = cid.split(":")
        except ValueError:
            return
        data = self.bot.store_load("extras_events")
        ev = data.get(eid)
        if not ev or ev.get("done"):
            await interaction.response.send_message("このイベントは終了しています。", ephemeral=True)
            return
        uid = interaction.user.id
        ps = ev.setdefault("participants", [])
        if kind == "join" and uid not in ps:
            ps.append(uid)
        elif kind == "leave" and uid in ps:
            ps.remove(uid)
        self.bot.store_save("extras_events", data)
        await interaction.response.edit_message(embed=self._event_embed(ev))

    # ================= FAQ =================
    async def _faq_open(self, interaction):
        items = self.cfg(interaction.guild.id)["faq"]["items"][:25]
        if not items:
            await interaction.response.send_message("よくある質問はまだ登録されていません。", ephemeral=True)
            return
        sel = discord.ui.Select(custom_id="xfaq:pick", placeholder="質問を選んでください",
                                options=[discord.SelectOption(label=it["q"][:100], value=str(i)) for i, it in enumerate(items)])
        v = discord.ui.View(timeout=300)
        v.add_item(sel)
        await interaction.response.send_message("❓ よくある質問", view=v, ephemeral=True)

    async def _faq_pick(self, interaction):
        items = self.cfg(interaction.guild.id)["faq"]["items"]
        try:
            it = items[int((interaction.data or {}).get("values", ["-1"])[0])]
        except (ValueError, IndexError):
            await interaction.response.send_message("その質問は見つかりません。", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(title=f"❓ {it['q']}", description=it["a"][:4000], color=0x3B82F6), ephemeral=True)

    @app_commands.command(name="faq", description="よくある質問を表示します")
    @app_commands.guild_only()
    async def faq_cmd(self, interaction: discord.Interaction):
        await self._faq_open(interaction)

    async def send_faq_panel(self, channel: discord.TextChannel):
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="よくある質問を見る", emoji="❓", style=discord.ButtonStyle.primary,
                                     custom_id="xfaq:open"))
        await channel.send(embed=discord.Embed(title="❓ よくある質問", color=0x3B82F6,
                                               description="下のボタンから、質問を選んで回答を確認できます（あなたにだけ表示されます）。"),
                           view=v)

    # ================= 代理送信 =================
    async def proxy_send(self, channel: discord.TextChannel, member: discord.Member, *, mode: str, content: str,
                         title: str = "", color: int | None = None, name: str = "", avatar: str = "",
                         mention: str = "") -> None:
        if not content and not title:
            raise ValueError("本文を入力してください")
        am = discord.AllowedMentions.none()
        prefix = ""
        if mention in ("everyone", "here"):
            if not member.guild_permissions.mention_everyone:
                raise ValueError("@everyone / @here のメンションには「全員宛メンション」権限が必要です")
            prefix, am = f"@{mention} ", discord.AllowedMentions(everyone=True)
        elif mention.isdigit():
            role = channel.guild.get_role(int(mention))
            if role is None:
                raise ValueError("メンションするロールが見つかりません")
            if not role.mentionable and not member.guild_permissions.mention_everyone:
                raise ValueError("そのロールをメンションする権限がありません")
            prefix, am = f"{role.mention} ", discord.AllowedMentions(roles=[role])
        embed = None
        if mode in ("embed", "webhook_embed"):
            embed = discord.Embed(title=title[:256] or None, description=content[:4000] or None, color=color or 0x3B82F6)
            text = prefix.strip() or None
        else:
            text = (prefix + content)[:2000]
        try:
            if mode.startswith("webhook"):
                hooks = await channel.webhooks()
                hook = next((h for h in hooks if h.name == "霊夢BOT 代理送信" and h.token), None) or \
                    await channel.create_webhook(name="霊夢BOT 代理送信")
                await hook.send(content=text, embed=embed or discord.utils.MISSING, username=(name or "お知らせ")[:80],
                                avatar_url=avatar if avatar.startswith("https://") else None, allowed_mentions=am)
            else:
                await channel.send(content=text, embed=embed, allowed_mentions=am)
        except discord.Forbidden:
            raise ValueError("Botの権限が足りません（Webhookの場合は「ウェブフックの管理」権限が必要です）")

    @app_commands.command(name="say", description="Botとしてメッセージを代理送信します")
    @app_commands.describe(message="本文", channel="送信先（省略で今のチャンネル）", embed="埋め込みで送る", title="埋め込みのタイトル")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def say(self, interaction: discord.Interaction, message: app_commands.Range[str, 1, 2000],
                  channel: discord.TextChannel | None = None, embed: bool = False, title: str = ""):
        try:
            await self.proxy_send(channel or interaction.channel, interaction.user, mode="embed" if embed else "text",
                                  content=message.replace("\\n", "\n"), title=title)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.send_message("✅ 送信しました。", ephemeral=True)

    # ================= チャンネルHTML保存 =================
    async def export_html(self, channel: discord.TextChannel, member, limit: int = 1000) -> tuple[str, bytes]:
        builder = getattr(self.bot, "build_transcript_html", None)
        if builder is None:
            raise ValueError("HTMLの作成機能（transcript.py）が読み込まれていません")
        try:
            msgs = [m async for m in channel.history(limit=max(1, min(limit, 5000)), oldest_first=False)]
        except discord.Forbidden:
            raise ValueError("Botにそのチャンネルの履歴を読む権限がありません")
        msgs.reverse()
        html_text = builder(channel, msgs, member)
        name = re.sub(r"[^\w.-]", "_", channel.name)[:40] or "channel"
        return f"{name}-{_now():%Y%m%d-%H%M}.html", html_text.encode("utf-8")

    @app_commands.command(name="export-channel", description="チャンネルの会話をHTMLファイルに書き出します")
    @app_commands.describe(channel="書き出すチャンネル（省略で今のチャンネル）", limit="最新から何件（最大5000）")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def export_channel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None,
                             limit: app_commands.Range[int, 1, 5000] = 1000):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            name, data = await self.export_html(channel or interaction.channel, interaction.user, limit)
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}")
            return
        await interaction.followup.send("📄 書き出しました。ブラウザで開くと会話を確認できます。",
                                        file=discord.File(io.BytesIO(data), filename=name))

    # ================= VC連動公開 =================
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        if member.bot or before.channel == after.channel:
            return
        if self._suspended(member.guild.id):
            return
        c = self.cfg(member.guild.id)["vclink"]
        if not c["enabled"] or not c["pairs"]:
            return
        g = member.guild
        if before.channel and str(before.channel.id) in c["pairs"]:
            ch = g.get_channel(int(c["pairs"][str(before.channel.id)]))
            if ch:
                try:
                    await ch.set_permissions(member, overwrite=None, reason="VC連動公開: 退出")
                except discord.HTTPException:
                    pass
        if after.channel and str(after.channel.id) in c["pairs"]:
            ch = g.get_channel(int(c["pairs"][str(after.channel.id)]))
            if ch:
                try:
                    await ch.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True,
                                             reason="VC連動公開: 参加")
                except discord.HTTPException:
                    pass

    # ================= 定期処理（自動メッセージ・週次レポート・イベント・締め切り） =================
    @tasks.loop(seconds=60)
    async def minute_loop(self):
        now = _now()
        hm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")
        data = self.bot.store_load("extras_config")
        changed = False
        for gid_s, raw in data.items():
            gid = int(gid_s)
            if self._suspended(gid) or self.bot.get_guild(gid) is None:
                continue
            c = self.cfg(gid)
            # 自動メッセージ
            if c["automsg"]["enabled"]:
                for it in c["automsg"]["items"]:
                    due = False
                    if it.get("mode") == "daily":
                        due = it.get("time") == hm and it.get("last") != today
                        stamp = today
                    else:
                        mins = max(10, int(it.get("minutes") or 60))
                        due = time.time() - float(it.get("last_ts") or 0) >= mins * 60
                        stamp = None
                    if not due:
                        continue
                    ch = self.bot.get_channel(int(it["channel"]))
                    if isinstance(ch, discord.TextChannel):
                        try:
                            guild = ch.guild
                            if it.get("webhook"):
                                mode = "webhook_embed" if it.get("title") else "webhook"
                            else:
                                mode = "embed" if it.get("title") else "text"
                            await self.proxy_send(ch, guild.me, mode=mode, content=it.get("content", ""),
                                                  title=it.get("title", ""), name=it.get("name", ""))
                        except Exception as e:
                            print(f"自動メッセージの送信に失敗 ({gid}): {type(e).__name__}")
                    if stamp:
                        it["last"] = stamp
                    it["last_ts"] = time.time()
                    raw.setdefault("automsg", {})["items"] = c["automsg"]["items"]
                    changed = True
            # 週次レポート
            rp = c["report"]
            if rp["enabled"] and rp["channel"] and now.weekday() == int(rp["weekday"]) and rp["time"] == hm \
                    and rp.get("last") != today:
                await self._send_report(gid, rp)
                rp["last"] = today
                raw["report"] = rp
                changed = True
        if changed:
            self.bot.store_save("extras_config", data)
        # イベントのお知らせ
        evs = self.bot.store_load("extras_events")
        ev_changed = False
        for eid, ev in evs.items():
            if ev.get("done") or self._suspended(ev["guild"]):
                continue
            ch = self.bot.get_channel(ev["channel"])
            left = ev["start"] - time.time()
            ps = " ".join(f"<@{u}>" for u in ev.get("participants", []))
            if not ev.get("reminded") and ev.get("remind") and left <= ev["remind"] * 60 and left > 0:
                ev["reminded"] = ev_changed = True
                if ch and ps:
                    await self._safe_send(ch, f"⏰ **{ev['title']}** はあと{max(1, round(left / 60))}分で始まります！ {ps}",
                                          users=True)
                for u in ev.get("participants", []):
                    user = self.bot.get_user(u)
                    if user:
                        try:
                            await user.send(f"⏰ 「{ev['title']}」があと{max(1, round(left / 60))}分で始まります。")
                        except discord.HTTPException:
                            pass
            if left <= 0:
                ev["started"] = ev["done"] = ev_changed = True
                if ch:
                    await self._safe_send(ch, f"🎉 **{ev['title']}** が始まりました！ {ps}", users=True)
                    try:
                        await ch.get_partial_message(ev["message"]).edit(embed=self._event_embed(ev),
                                                                         view=self._event_view(eid, True))
                    except discord.HTTPException:
                        pass
        if ev_changed:
            self.bot.store_save("extras_events", evs)
        # アンケートの締め切り
        for pid, p in list(self.bot.store_load("extras_polls").items()):
            if not p.get("closed") and p.get("deadline") and time.time() >= p["deadline"]:
                await self.close_poll(pid)
        if now.minute % 10 == 0:
            self._save_stats()

    @minute_loop.before_loop
    async def _before_minute(self):
        await self.bot.wait_until_ready()

    async def _safe_send(self, ch, text, *, users=False):
        try:
            await ch.send(text, allowed_mentions=discord.AllowedMentions(users=users))
        except discord.HTTPException:
            pass

    def build_report(self, guild: discord.Guild, rp: dict) -> discord.Embed:
        s = self._st(guild.id)
        members = guild.member_count or 0
        diff = members - rp["last_members"] if rp.get("last_members") is not None else None
        e = discord.Embed(title=f"📊 週次レポート（{_now():%Y/%m/%d}）", color=0x3B82F6,
                          description=f"**{guild.name}** のこの1週間の動きです。")
        e.add_field(name="👥 メンバー数", value=f"{members:,}人" + (f"（{diff:+,}）" if diff is not None else ""), inline=True)
        e.add_field(name="📥 参加 / 📤 退出", value=f"{s['joins']} / {s['leaves']}", inline=True)
        e.add_field(name="💬 メッセージ", value=f"{s['msgs']:,}件（{len(s['users'])}人が発言）", inline=True)
        top_ch = sorted(s["ch"].items(), key=lambda kv: -kv[1])[:5]
        if top_ch:
            e.add_field(name="🔥 よく使われたチャンネル", value="\n".join(f"<#{c}> {n:,}件" for c, n in top_ch), inline=False)
        top_u = sorted(s["users"].items(), key=lambda kv: -kv[1])[:5]
        if top_u:
            e.add_field(name="🏅 よく発言した人", value="\n".join(f"<@{u}> {n:,}件" for u, n in top_u), inline=False)
        e.set_footer(text="霊夢BOT 週次レポート")
        return e

    async def _send_report(self, gid: int, rp: dict):
        guild = self.bot.get_guild(gid)
        ch = guild.get_channel(int(rp["channel"])) if guild else None
        if ch is None:
            return
        try:
            await ch.send(embed=self.build_report(guild, rp), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return
        rp["last_members"] = guild.member_count
        self._stats[gid] = {}
        self._save_stats()

    # ================= YouTube Push =================
    def _yt_subs(self) -> dict[str, list[tuple[int, dict]]]:
        out: dict[str, list] = {}
        for gid_s in self.bot.store_load("extras_config"):
            c = self.cfg(int(gid_s))["youtube"]
            if not c["enabled"]:
                continue
            for s in c["subs"]:
                out.setdefault(s["yt"], []).append((int(gid_s), s))
        return out

    def _callback_url(self) -> str | None:
        fn = getattr(self.bot, "public_base_url", None)
        base = fn() if fn else ""
        return f"{base}/youtube/websub" if base.startswith("https://") else None

    async def websub_subscribe(self, yt_id: str, mode: str = "subscribe") -> bool:
        cb = self._callback_url()
        if not cb:
            return False
        try:
            r = await self.http.post(HUB, data={
                "hub.callback": cb, "hub.mode": mode, "hub.verify": "async", "hub.secret": self._yt_secret,
                "hub.topic": f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={yt_id}", "hub.lease_seconds": "864000"})
            return r.status_code in (202, 204)
        except httpx.HTTPError:
            return False

    async def websub_get(self, request):
        from fastapi.responses import PlainTextResponse
        q = request.query_params
        topic = q.get("hub.topic", "")
        m = re.search(r"channel_id=(UC[\w-]{20,30})", topic)
        if q.get("hub.mode") in ("subscribe", "unsubscribe") and m and (m.group(1) in self._yt_subs() or q.get("hub.mode") == "unsubscribe"):
            return PlainTextResponse(q.get("hub.challenge", "")[:200])
        return PlainTextResponse("not found", status_code=404)

    async def websub_post(self, request):
        from fastapi.responses import PlainTextResponse
        body = (await request.body())[:200_000]
        sig = request.headers.get("x-hub-signature", "")
        good = "sha1=" + hmac.new(self._yt_secret.encode(), body, hashlib.sha1).hexdigest()
        if not secrets.compare_digest(sig, good):
            return PlainTextResponse("bad signature", status_code=403)
        try:
            await self._yt_handle_feed(body, push=True)
        except Exception as e:
            print(f"YouTube通知の処理に失敗: {type(e).__name__}")
        return PlainTextResponse("ok")

    async def _yt_handle_feed(self, body: bytes, *, push: bool):
        root = ET.fromstring(body)
        subs = self._yt_subs()
        for ent in root.findall(f"{ATOM}entry"):
            vid = ent.findtext(f"{YT}videoId")
            cid = ent.findtext(f"{YT}channelId")
            if not vid or vid in self._yt_seen:
                continue
            self._yt_seen.add(vid)
            published = ent.findtext(f"{ATOM}published") or ""
            try:
                if datetime.fromisoformat(published.replace("Z", "+00:00")) < datetime.now(timezone.utc) - timedelta(days=2):
                    continue  # 古い動画の更新通知は送らない
            except ValueError:
                pass
            if not self._yt_boot and not push:
                continue
            title = ent.findtext(f"{ATOM}title") or "新しい動画"
            author = ent.findtext(f"{ATOM}author/{ATOM}name") or ""
            url = f"https://www.youtube.com/watch?v={vid}"
            for gid, s in subs.get(cid, []):
                if self._suspended(gid):
                    continue
                ch = self.bot.get_channel(int(s["channel"]))
                if ch is None:
                    continue
                mention = s.get("mention") or ""
                text = f"{('@' + mention + ' ') if mention in ('everyone', 'here') else (f'<@&{mention}> ' if mention.isdigit() else '')}" \
                       f"📺 **{author}** が新しい動画を公開しました！\n{url}"
                am = discord.AllowedMentions(everyone=mention in ("everyone", "here"), roles=mention.isdigit())
                try:
                    await ch.send(text, allowed_mentions=am)
                except discord.HTTPException:
                    pass
        if len(self._yt_seen) > 5000:
            self._yt_seen = set(list(self._yt_seen)[-2000:])

    @tasks.loop(minutes=10)
    async def yt_loop(self):
        """WebSubの登録更新と、届かなかった通知の補完（RSSの確認）。"""
        subs = self._yt_subs()
        for i, yt_id in enumerate(subs):
            try:
                r = await self.http.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={yt_id}")
                if r.status_code == 200:
                    await self._yt_handle_feed(r.content, push=False)
            except Exception:
                pass
            if self.yt_loop.current_loop % 144 == 0:  # 約1日ごとに購読を更新（期限は10日）
                await self.websub_subscribe(yt_id)
            await asyncio.sleep(1)
        self._yt_boot = True

    @yt_loop.before_loop
    async def _before_yt(self):
        await self.bot.wait_until_ready()

    # ================= ダッシュボード: 画面 =================
    async def render(self, tab: str, gid: int, guild: discord.Guild, member, H) -> str:
        """H: card, form, sel, lbl, chk, opt, e, channel_options, role_options, voice_options, text_area"""
        c = self.cfg(gid)
        out = []
        if tab == "events":
            evs = [(k, v) for k, v in self.bot.store_load("extras_events").items() if v.get("guild") == gid and not v.get("done")]
            pls = [(k, v) for k, v in self.bot.store_load("extras_polls").items() if v.get("guild") == gid and not v.get("closed")]
            out.append(H.card("🎉 イベントを作成", H.form("x_event",
                H.lbl("イベント名") + '<input type="text" name="title" maxlength="100">'
                + H.lbl("開始日時（例: 2026-12-24 20:00）") + '<input type="text" name="start" maxlength="16" placeholder="2026-12-24 20:00">'
                + H.lbl("説明（任意）") + H.text_area("description", "", 1500)
                + H.lbl("場所（任意・VC名など）") + '<input type="text" name="location" maxlength="100">'
                + H.lbl("告知するチャンネル") + H.sel("channel", H.channel_options(None))
                + H.lbl("開始の何分前に参加者へ知らせるか") + '<input type="text" name="remind" maxlength="4" value="30">'
                + H.chk("discord_event", False, "Discordの「イベント」にも登録する"), "イベントを作成"),
                note="参加ボタン付きで告知し、開始前に参加者へメンションとDMでお知らせします。"))
            out.append(H.card("📊 アンケート・日程調整を作成", H.form("x_poll",
                H.lbl("種類") + H.sel("kind", H.opt("poll", "アンケート", "poll") + H.opt("schedule", "日程調整（候補日時の投票・複数選択）", ""))
                + H.lbl("質問 / イベント名") + '<input type="text" name="question" maxlength="200">'
                + H.lbl("選択肢 / 候補日時（1行に1つ・最大20）") + H.text_area("options", "", 1500)
                + H.chk("multi", False, "複数選択を許可する（日程調整は常に複数）")
                + H.lbl("締め切りまでの時間（時間・0で手動）") + '<input type="text" name="hours" maxlength="4" value="0">'
                + H.lbl("投稿するチャンネル") + H.sel("channel", H.channel_options(None)), "作成する")))
            items = "".join(
                f'<div class="card" style="margin:8px 0;padding:12px"><b>{H.e(v["title"])}</b>'
                f'<div class="mut">{datetime.fromtimestamp(v["start"], JST):%m/%d %H:%M} ・ 参加 {len(v.get("participants", []))}人</div>'
                + H.form("x_event_delete", f'<input type="hidden" name="id" value="{H.e(k)}">', "中止して削除", cls="btn ghost") + "</div>"
                for k, v in evs)
            items += "".join(
                f'<div class="card" style="margin:8px 0;padding:12px"><b>{"📅" if v.get("kind") == "schedule" else "📊"} {H.e(v["question"])}</b>'
                f'<div class="mut">投票者 {len(v.get("votes", {}))}人</div>'
                + H.form("x_poll_close", f'<input type="hidden" name="id" value="{H.e(k)}">', "締め切る", cls="btn ghost") + "</div>"
                for k, v in pls)
            out.append(H.card("🗂️ 受付中のイベント・アンケート", items or '<div class="mut">ありません</div>'))
        elif tab == "tools":
            tr = c["translate"]
            tch = "".join(f'<div class="mut">・#{H.e(getattr(guild.get_channel(int(k)), "name", k))} → {H.e(LANG_NAME.get(v, v))}</div>'
                          for k, v in tr["channels"].items()) or '<div class="mut">自動翻訳するチャンネルはありません</div>'
            langs = "".join(H.opt(k, v, "JA") for k, v in LANG_NAME.items())
            out.append(H.card("🌐 翻訳（DeepL）", ("" if DEEPL_KEY else '<div class="flash ng">運営者が DEEPL_API_KEY を設定するまで使えません。</div>')
                + tch + H.form("x_translate",
                    H.chk("enabled", tr["enabled"], "翻訳機能を使う")
                    + H.chk("reaction", tr["reaction"], "国旗の絵文字でリアクションすると、その言語に翻訳する")
                    + H.lbl("自動翻訳するチャンネル（任意）") + H.sel("channel", H.channel_options(None, none_label="（変更しない）"))
                    + H.lbl("翻訳先の言語") + H.sel("lang", langs)
                    + H.lbl("操作") + H.sel("mode", H.opt("add", "このチャンネルを追加", "add") + H.opt("remove", "このチャンネルを外す", "")),
                    "保存する"), note="DeepLが対応する30以上の言語に翻訳できます。/translate でも使えます。"))
            am = c["automsg"]
            alist = "".join(
                f'<div class="card" style="margin:8px 0;padding:12px"><b>#{H.e(getattr(guild.get_channel(int(it["channel"])), "name", "?"))}</b> '
                f'<span class="mut">{"毎日 " + it.get("time", "") if it.get("mode") == "daily" else str(it.get("minutes")) + "分ごと"}'
                f'{" ・ Webhook" if it.get("webhook") else ""}</span><div class="mut">{H.e((it.get("title") or "") + " " + it.get("content", "")[:80])}</div>'
                + H.form("x_automsg_del", f'<input type="hidden" name="id" value="{H.e(it["id"])}">', "削除", cls="btn ghost") + "</div>"
                for it in am["items"]) or '<div class="mut">登録はありません</div>'
            out.append(H.card("⏰ 自動メッセージ", alist + H.form("x_automsg_add",
                H.lbl("送信先") + H.sel("channel", H.channel_options(None))
                + H.lbl("タイミング") + H.sel("mode", H.opt("daily", "毎日決まった時刻", "daily") + H.opt("interval", "一定の間隔", ""))
                + H.lbl("時刻（毎日のとき・例 09:00）") + '<input type="text" name="time" maxlength="5" value="09:00">'
                + H.lbl("間隔（分・10以上）") + '<input type="text" name="minutes" maxlength="5" value="60">'
                + H.lbl("タイトル（任意・入れると埋め込み）") + '<input type="text" name="title" maxlength="200">'
                + H.lbl("本文") + H.text_area("content", "", 1800)
                + H.chk("webhook", False, "Webhookで送る（名前を変えて送れます）")
                + H.lbl("Webhookの表示名（任意）") + '<input type="text" name="name" maxlength="80">', "追加する")))
            rp = c["report"]
            out.append(H.card("📊 週次レポート", H.form("x_report",
                H.chk("enabled", rp["enabled"], "週次レポートを送る")
                + H.lbl("送信先") + H.sel("channel", H.channel_options(rp["channel"]))
                + H.lbl("曜日") + H.sel("weekday", "".join(H.opt(i, f"{w}曜日", rp["weekday"]) for i, w in enumerate(WEEKDAYS)))
                + H.lbl("時刻（例 09:00）") + f'<input type="text" name="time" maxlength="5" value="{H.e(rp["time"])}">', "保存する"),
                note="メンバー数の増減・参加/退出・メッセージ数・よく使われたチャンネル・よく発言した人をまとめて送ります。"))
            fq = c["faq"]
            flist = "".join(
                f'<div class="card" style="margin:8px 0;padding:12px"><b>❓ {H.e(it["q"])}</b><div class="mut">{H.e(it["a"][:100])}</div>'
                + (f'<div class="mut">キーワード: {H.e("、".join(it.get("keywords", [])))}</div>' if it.get("keywords") else "")
                + H.form("x_faq_del", f'<input type="hidden" name="idx" value="{i}">', "削除", cls="btn ghost") + "</div>"
                for i, it in enumerate(fq["items"])) or '<div class="mut">登録はありません</div>'
            out.append(H.card("❓ FAQ（よくある質問）", flist
                + H.form("x_faq_add", H.lbl("質問") + '<input type="text" name="q" maxlength="100">'
                         + H.lbl("回答") + H.text_area("a", "", 3000)
                         + H.lbl("自動回答のキーワード（任意・カンマ区切り）") + '<input type="text" name="keywords" maxlength="200">', "追加する")
                + H.form("x_faq_set", H.chk("autoreply", fq["autoreply"], "キーワードを含むメッセージに自動で回答する"), "保存する")
                + H.form("x_faq_panel", H.lbl("FAQパネルを送るチャンネル") + H.sel("channel", H.channel_options(None)), "パネルを送信"),
                note="/faq でも表示できます。回答は押した本人にだけ表示されます。"))
            th = c["threads"]
            tlist = "".join(f'<div class="mut">・#{H.e(getattr(guild.get_channel(int(k)), "name", k))}（{int(v) // 60}時間で自動アーカイブ）</div>'
                            for k, v in th["channels"].items()) or '<div class="mut">設定はありません</div>'
            out.append(H.card("🧵 スレッド管理", tlist + H.form("x_threads",
                H.chk("enabled", th["enabled"], "スレッドの自動作成を使う")
                + H.lbl("投稿ごとにスレッドを作るチャンネル") + H.sel("channel", H.channel_options(None, none_label="（変更しない）"))
                + H.lbl("自動アーカイブまでの時間") + H.sel("archive", H.opt(60, "1時間", "") + H.opt(1440, "24時間", 1440)
                                                    + H.opt(4320, "3日", "") + H.opt(10080, "1週間", ""))
                + H.lbl("操作") + H.sel("mode", H.opt("add", "追加・変更", "add") + H.opt("remove", "外す", "")), "保存する")))
        elif tab == "media":
            yt = c["youtube"]
            ylist = "".join(
                f'<div class="card" style="margin:8px 0;padding:12px"><b>📺 {H.e(s.get("name") or s["yt"])}</b>'
                f'<div class="mut">→ #{H.e(getattr(guild.get_channel(int(s["channel"])), "name", "?"))}</div>'
                + H.form("x_youtube_del", f'<input type="hidden" name="yt" value="{H.e(s["yt"])}">', "解除", cls="btn ghost") + "</div>"
                for s in yt["subs"]) or '<div class="mut">登録はありません</div>'
            out.append(H.card("📺 YouTube Push", ylist + H.form("x_youtube_add",
                H.lbl("YouTubeのチャンネルID（UCから始まる）またはチャンネルURL") + '<input type="text" name="yt" maxlength="120" placeholder="https://www.youtube.com/channel/UC...">'
                + H.lbl("表示名（任意）") + '<input type="text" name="name" maxlength="80">'
                + H.lbl("通知するチャンネル") + H.sel("channel", H.channel_options(None))
                + H.lbl("メンション") + H.sel("mention", H.opt("", "なし", "") + H.opt("everyone", "@everyone", "") + H.opt("here", "@here", "")
                                           + "".join(H.opt(r.id, "@" + r.name, "") for r in sorted(guild.roles, key=lambda r: -r.position)
                                                     if not r.is_default() and not r.managed)),
                "登録する"), note="新しい動画が公開されると、YouTubeからの即時通知（WebSub）で知らせます。届かなかった場合も10分ごとの確認で補います。"))
            mention_opts = (H.opt("", "なし", "") + H.opt("everyone", "@everyone", "") + H.opt("here", "@here", "")
                            + "".join(H.opt(r.id, "@" + r.name, "") for r in sorted(guild.roles, key=lambda r: -r.position)
                                      if not r.is_default() and not r.managed))
            out.append(H.card("📣 代理送信", H.form("x_say",
                H.lbl("送信先") + H.sel("channel", H.channel_options(None))
                + H.lbl("送り方") + H.sel("mode", H.opt("text", "通常のテキスト", "text") + H.opt("embed", "埋め込み", "")
                                         + H.opt("webhook", "Webhook（テキスト）", "") + H.opt("webhook_embed", "Webhook（埋め込み）", ""))
                + H.lbl("メンション") + H.sel("mention", mention_opts)
                + H.lbl("タイトル（埋め込みのとき）") + '<input type="text" name="title" maxlength="256">'
                + H.lbl("本文") + H.text_area("content", "", 2000)
                + H.lbl("色（埋め込み・例 #3B82F6）") + '<input type="text" name="color" maxlength="7">'
                + H.lbl("Webhookの表示名・アイコンURL（Webhookのとき）") + '<input type="text" name="name" maxlength="80" placeholder="表示名">'
                + '<input type="text" name="avatar" maxlength="300" placeholder="https://...（任意）" style="margin-top:8px">', "送信する")))
            pairs = c["vclink"]["pairs"]
            plist = "".join(f'<div class="mut">・🔊 {H.e(getattr(guild.get_channel(int(k)), "name", k))} → #{H.e(getattr(guild.get_channel(int(v)), "name", v))}</div>'
                            for k, v in pairs.items()) or '<div class="mut">設定はありません</div>'
            out.append(H.card("🎧 VC連動公開", plist + H.form("x_vclink",
                H.lbl("ボイスチャンネル") + H.sel("vc", H.voice_options())
                + H.lbl("参加中だけ見られるテキストチャンネル（聞き専）") + H.sel("text", H.channel_options(None, none_label="（連動を外す）")),
                "保存する", need="admin"), need="admin",
                note="そのテキストチャンネルは、@everyone から「チャンネルを見る」を外しておいてください。VCに入った人だけに表示されます。"))
            out.append(H.card("📄 チャンネルHTML保存", H.form("x_html",
                H.lbl("書き出すチャンネル") + H.sel("channel", H.channel_options(None))
                + H.lbl("最新から何件（最大5000）") + '<input type="text" name="limit" maxlength="4" value="1000">',
                "HTMLをダウンロード", need="admin"), need="admin", note="会話をきれいなHTMLファイルにして、この画面からダウンロードします。"))
            rec = self.bot.get_cog("Recorder")
            if rec is not None:
                out.append(rec.render_card(gid, guild, H))
            else:
                out.append(H.card("🎙️ 録音", '<p class="mut">recorder.py が読み込まれていません。</p>'))
        return "".join(out)

    def ocr_card(self, gid: int, H) -> str:
        oc = self.cfg(gid)["ocr"]
        return H.card("🔍 OCR画像検閲（Google Vision）",
                      ("" if VISION_KEY else '<div class="flash ng">運営者が GOOGLE_VISION_API_KEY を設定するまで使えません。</div>')
                      + H.form("x_ocr", H.chk("enabled", oc["enabled"], "画像内の文字を読み取って検閲する")
                               + H.chk("invites", oc["invites"], "画像に写った招待リンクも検出する")
                               + H.lbl("禁止ワード（カンマ区切り）") + f'<input type="text" name="words" maxlength="1000" value="{H.e(", ".join(oc["words"]))}">'
                               + H.lbl("見つけたとき") + H.sel("action", H.opt("delete", "削除してログに記録", oc["action"]) + H.opt("log", "ログに記録するだけ", oc["action"]))
                               + H.lbl("タイムアウト（分・0でしない）") + f'<input type="text" name="timeout" maxlength="5" value="{H.e(oc["timeout"])}">',
                               "保存する", need="admin"), need="admin",
                      note="管理者・「メッセージの管理」権限のメンバーは対象外です。1サーバー1分20枚まで読み取ります。")

    # ================= ダッシュボード: 保存 =================
    async def dashboard_op(self, op: str, f: dict, guild: discord.Guild, member, P):
        """P: pick_channel, pick_role。戻り値は表示する文字列、または ("file", 名前, データ)。"""
        gid = guild.id
        c = self.cfg(gid)

        def num(name, lo, hi, default):
            v = (f.get(name) or "").strip()
            if not v:
                return default
            try:
                n = float(v)
            except ValueError:
                raise ValueError("数字で入力してください")
            if not lo <= n <= hi:
                raise ValueError(f"{lo}〜{hi} の範囲で入力してください")
            return n

        def hm(raw):
            m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", raw or "")
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                raise ValueError("時刻は「09:00」の形で入力してください")
            return f"{int(m.group(1)):02d}:{m.group(2)}"

        if op == "x_ocr":
            on = f.get("enabled") == "1"
            if on and not VISION_KEY:
                raise ValueError("GOOGLE_VISION_API_KEY が未設定のため、有効にできません")
            c["ocr"].update({"enabled": on, "invites": f.get("invites") == "1",
                             "words": [w.strip()[:50] for w in re.split(r"[,、\n]", f.get("words", "")) if w.strip()][:100],
                             "action": "log" if f.get("action") == "log" else "delete",
                             "timeout": int(num("timeout", 0, 40320, 0))})
            self.save(gid, c)
            return "OCR画像検閲の設定を保存しました"
        if op == "x_event":
            ch = P.pick_channel(f.get("channel"), required=True)
            title = (f.get("title") or "").strip()
            if not title:
                raise ValueError("イベント名を入力してください")
            await self.create_event(ch, title, _parse_dt(f.get("start")), description=f.get("description", ""),
                                    location=f.get("location", ""), remind=int(num("remind", 0, 1440, 30)),
                                    discord_event=f.get("discord_event") == "1", by=member.id)
            return f"#{ch.name} にイベント「{title}」を作成しました"
        if op == "x_event_delete":
            data = self.bot.store_load("extras_events")
            ev = data.get(f.get("id", ""))
            if not ev or ev.get("guild") != gid:
                raise ValueError("イベントが見つかりません")
            ev["done"] = True
            self.bot.store_save("extras_events", data)
            ch = self.bot.get_channel(ev["channel"])
            if ch:
                try:
                    await ch.get_partial_message(ev["message"]).edit(content="（このイベントは中止されました）",
                                                                     view=self._event_view(f["id"], True))
                except discord.HTTPException:
                    pass
            return f"イベント「{ev['title']}」を中止しました"
        if op == "x_poll":
            ch = P.pick_channel(f.get("channel"), required=True)
            q = (f.get("question") or "").strip()
            if not q:
                raise ValueError("質問を入力してください")
            kind = "schedule" if f.get("kind") == "schedule" else "poll"
            await self.create_poll(ch, q, re.split(r"[\n,、]", f.get("options", "")),
                                   multi=kind == "schedule" or f.get("multi") == "1", kind=kind,
                                   hours=num("hours", 0, 720, 0), by=member.id)
            return f"#{ch.name} に{'日程調整' if kind == 'schedule' else 'アンケート'}を作成しました"
        if op == "x_poll_close":
            data = self.bot.store_load("extras_polls")
            p = data.get(f.get("id", ""))
            if not p or p.get("guild") != gid:
                raise ValueError("アンケートが見つかりません")
            await self.close_poll(f["id"])
            return f"「{p['question']}」を締め切りました"
        if op == "x_translate":
            tr = c["translate"]
            on = f.get("enabled") == "1"
            if on and not DEEPL_KEY:
                raise ValueError("DEEPL_API_KEY が未設定のため、有効にできません")
            tr["enabled"], tr["reaction"] = on, f.get("reaction") == "1"
            ch = P.pick_channel(f.get("channel"))
            if ch is not None:
                if f.get("mode") == "remove":
                    tr["channels"].pop(str(ch.id), None)
                else:
                    lang = f.get("lang") if f.get("lang") in LANG_NAME else "JA"
                    tr["channels"][str(ch.id)] = lang
            self.save(gid, c)
            return "翻訳の設定を保存しました"
        if op == "x_automsg_add":
            ch = P.pick_channel(f.get("channel"), required=True)
            content = (f.get("content") or "").strip()
            if not content:
                raise ValueError("本文を入力してください")
            if len(c["automsg"]["items"]) >= 20:
                raise ValueError("自動メッセージは20件までです")
            mode = "interval" if f.get("mode") == "interval" else "daily"
            it = {"id": secrets.token_hex(3), "channel": ch.id, "mode": mode, "content": content[:1800],
                  "title": (f.get("title") or "").strip()[:200], "webhook": f.get("webhook") == "1",
                  "name": (f.get("name") or "").strip()[:80], "last": "", "last_ts": time.time()}
            if mode == "daily":
                it["time"] = hm(f.get("time"))
            else:
                it["minutes"] = int(num("minutes", 10, 10080, 60))
            c["automsg"]["items"].append(it)
            self.save(gid, c)
            return f"#{ch.name} への自動メッセージを追加しました"
        if op == "x_automsg_del":
            c["automsg"]["items"] = [it for it in c["automsg"]["items"] if it["id"] != f.get("id")]
            self.save(gid, c)
            return "自動メッセージを削除しました"
        if op == "x_report":
            rp = c["report"]
            ch = P.pick_channel(f.get("channel"))
            rp.update({"enabled": f.get("enabled") == "1" and ch is not None, "channel": ch.id if ch else None,
                       "weekday": int(num("weekday", 0, 6, 0)), "time": hm(f.get("time") or "09:00")})
            if rp["last_members"] is None:
                rp["last_members"] = guild.member_count
            self.save(gid, c)
            return "週次レポートの設定を保存しました"
        if op == "x_faq_add":
            q, a = (f.get("q") or "").strip(), (f.get("a") or "").strip()
            if not q or not a:
                raise ValueError("質問と回答を入力してください")
            if len(c["faq"]["items"]) >= 25:
                raise ValueError("FAQは25件までです")
            c["faq"]["items"].append({"q": q[:100], "a": a[:3000], "keywords": [k.strip()[:30] for k in
                                                                              re.split(r"[,、]", f.get("keywords", "")) if k.strip()][:10]})
            self.save(gid, c)
            return f"FAQ「{q[:30]}」を追加しました"
        if op == "x_faq_del":
            try:
                c["faq"]["items"].pop(int(f.get("idx", "-1")))
            except (ValueError, IndexError):
                raise ValueError("FAQが見つかりません")
            self.save(gid, c)
            return "FAQを削除しました"
        if op == "x_faq_set":
            c["faq"]["autoreply"] = f.get("autoreply") == "1"
            self.save(gid, c)
            return "FAQの自動回答の設定を保存しました"
        if op == "x_faq_panel":
            ch = P.pick_channel(f.get("channel"), required=True)
            await self.send_faq_panel(ch)
            return f"#{ch.name} にFAQパネルを送信しました"
        if op == "x_threads":
            th = c["threads"]
            th["enabled"] = f.get("enabled") == "1"
            ch = P.pick_channel(f.get("channel"))
            if ch is not None:
                if f.get("mode") == "remove":
                    th["channels"].pop(str(ch.id), None)
                else:
                    if not ch.permissions_for(guild.me).create_public_threads:
                        raise ValueError(f"#{ch.name} でBotに「公開スレッドの作成」権限がありません")
                    arc = f.get("archive")
                    th["channels"][str(ch.id)] = int(arc) if arc in ("60", "1440", "4320", "10080") else 1440
            self.save(gid, c)
            return "スレッド管理の設定を保存しました"
        if op == "x_youtube_add":
            raw = (f.get("yt") or "").strip()
            m = re.search(r"(UC[\w-]{20,30})", raw)
            if not m:
                raise ValueError("「UC」から始まるチャンネルID、または /channel/UC... の形のURLを入力してください")
            ch = P.pick_channel(f.get("channel"), required=True)
            mention = f.get("mention", "")
            if mention in ("everyone", "here") and not member.guild_permissions.mention_everyone:
                raise ValueError("@everyone / @here のメンションには「全員宛メンション」権限が必要です")
            subs = [s for s in c["youtube"]["subs"] if s["yt"] != m.group(1)]
            if len(subs) >= 20:
                raise ValueError("登録できるのは20チャンネルまでです")
            subs.append({"yt": m.group(1), "channel": ch.id, "mention": mention if (mention in ("everyone", "here") or mention.isdigit()) else "",
                         "name": (f.get("name") or "").strip()[:80]})
            c["youtube"]["subs"] = subs
            self.save(gid, c)
            ok = await self.websub_subscribe(m.group(1))
            return f"YouTubeチャンネルを登録しました（{'即時通知も登録済み' if ok else '即時通知の登録に失敗・10分ごとの確認で通知します'}）"
        if op == "x_youtube_del":
            c["youtube"]["subs"] = [s for s in c["youtube"]["subs"] if s["yt"] != f.get("yt")]
            self.save(gid, c)
            return "YouTubeチャンネルの登録を解除しました"
        if op == "x_say":
            ch = P.pick_channel(f.get("channel"), required=True)
            col = None
            if f.get("color"):
                try:
                    col = int(f["color"].strip().lstrip("#"), 16)
                except ValueError:
                    raise ValueError("色は #3B82F6 の形で入力してください")
            mode = f.get("mode") if f.get("mode") in ("text", "embed", "webhook", "webhook_embed") else "text"
            await self.proxy_send(ch, member, mode=mode, content=(f.get("content") or "").strip()[:4000],
                                  title=(f.get("title") or "").strip(), color=col, name=(f.get("name") or "").strip(),
                                  avatar=(f.get("avatar") or "").strip(), mention=f.get("mention", ""))
            return f"#{ch.name} に送信しました"
        if op == "x_vclink":
            vc_raw = f.get("vc", "")
            vc = guild.get_channel(int(vc_raw)) if vc_raw.isdigit() else None
            if not isinstance(vc, (discord.VoiceChannel, discord.StageChannel)):
                raise ValueError("ボイスチャンネルを選んでください")
            text = P.pick_channel(f.get("text"))
            if text is None:
                c["vclink"]["pairs"].pop(str(vc.id), None)
                res = f"🔊{vc.name} の連動を外しました"
            else:
                if not text.permissions_for(guild.me).manage_roles:
                    raise ValueError(f"#{text.name} でBotに「権限の管理」がありません")
                c["vclink"]["pairs"][str(vc.id)] = text.id
                res = f"🔊{vc.name} に参加中だけ #{text.name} を表示します"
            self.save(gid, c)
            return res
        if op == "x_html":
            ch = P.pick_channel(f.get("channel"), required=True)
            name, data = await self.export_html(ch, member, int(num("limit", 1, 5000, 1000)))
            return ("file", name, data)
        raise ValueError("不明な操作です")


async def setup(bot: commands.Bot):
    await bot.add_cog(Extras(bot))
