"""気象庁の公開データを使った、天気予報の定時投稿と災害情報のリアルタイム通知。

天気予報: https://www.jma.go.jp/bosai/forecast/data/forecast/<地域コード>.json
地震    : https://www.jma.go.jp/bosai/quake/data/list.json
津波・火山: https://www.data.jma.go.jp/developer/xml/feed/eqvol.xml（気象庁防災情報XML）
気象警報 : https://www.data.jma.go.jp/developer/xml/feed/extra.xml
"""
from __future__ import annotations

import asyncio
import os
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import discord
import httpx
from discord import app_commands
from discord.ext import commands, tasks

JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "DiscordBot-JMA-Alerts/1.0"}
ATOM = "{http://www.w3.org/2005/Atom}"
CITIES = [("札幌", "016000"), ("青森", "020000"), ("仙台", "040000"), ("新潟", "150000"),
          ("東京", "130000"), ("金沢", "170000"), ("長野", "200000"), ("名古屋", "230000"),
          ("大阪", "270000"), ("松江", "320000"), ("広島", "340000"), ("高松", "370000"),
          ("高知", "390000"), ("福岡", "400000"), ("鹿児島", "460100"), ("那覇", "471000")]
INT_ORDER = ["1", "2", "3", "4", "5-", "5+", "6-", "6+", "7"]
INT_LABEL = {"5-": "5弱", "5+": "5強", "6-": "6弱", "6+": "6強"}
INT_COLOR = {"1": 0x6B7280, "2": 0x3B82F6, "3": 0x22C55E, "4": 0xEAB308, "5-": 0xF97316,
             "5+": 0xEA580C, "6-": 0xDC2626, "6+": 0xB91C1C, "7": 0x7E22CE}


def _int_rank(v) -> int:
    return INT_ORDER.index(v) if v in INT_ORDER else -1


def _weather_emoji(code: str) -> str:
    c = str(code or "")[:1]
    return {"1": "☀️", "2": "☁️", "3": "☔", "4": "⛄"}.get(c, "🌡️")


class JMAAlerts(commands.Cog):
    weather = app_commands.Group(name="weather", description="全国の天気予報を指定時刻に自動投稿します",
                                 guild_only=True, default_permissions=discord.Permissions(manage_guild=True))
    disaster = app_commands.Group(name="disaster", description="地震・津波・火山・気象警報を自動通知します",
                                  guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.http = httpx.AsyncClient(timeout=15, headers=UA, follow_redirects=True)
        self._seen: set[str] = set()
        self._bootstrapped = False
        self._lastmod: dict[str, str] = {}
        self._fc_cache: tuple[float, list] = (0.0, [])
        self._sent_count: dict[int, list[float]] = {}

    async def cog_load(self):
        self.weather_loop.start()
        self.disaster_loop.start()
        self._eew_task = asyncio.create_task(self._eew_listener())

    async def cog_unload(self):
        self.weather_loop.cancel()
        self.disaster_loop.cancel()
        if getattr(self, "_eew_task", None):
            self._eew_task.cancel()
        await self.http.aclose()

    # ================= 緊急地震速報（P2P地震情報 API・WebSocket） =================
    # 気象庁は緊急地震速報を一般にネット配信していないため、P2P地震情報（https://www.p2pquake.net/）が
    # 中継する「緊急地震速報（警報）」を受信する。警報は予想最大震度5弱以上のときに発表される。
    EEW_WS = os.getenv("EEW_WS_URL", "wss://api.p2pquake.net/v2/ws")
    EEW_SCALE = {10: "1", 20: "2", 30: "3", 40: "4", 45: "5弱", 46: "5弱以上", 50: "5強", 55: "6弱",
                 60: "6強", 70: "7", 99: "以上"}

    def _scale(self, v) -> str:
        try:
            return self.EEW_SCALE.get(int(v), "不明")
        except (TypeError, ValueError):
            return "不明"

    async def _eew_listener(self):
        import aiohttp
        await self.bot.wait_until_ready()
        self._eew_seen: dict[str, int] = {}
        self._eew_msgs: dict[str, dict[int, int]] = {}  # 地震ID → {チャンネルID: メッセージID}（続報で更新）
        delay = 5
        while True:
            try:
                async with aiohttp.ClientSession(headers=UA) as sess:
                    async with sess.ws_connect(self.EEW_WS, heartbeat=30) as ws:
                        print("緊急地震速報の受信を開始しました（P2P地震情報）")
                        delay = 5
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                    break
                                continue
                            try:
                                data = json.loads(msg.data)
                            except ValueError:
                                continue
                            if isinstance(data, dict) and data.get("code") == 556:
                                asyncio.create_task(self._on_eew(data))
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"緊急地震速報の接続が切れました（{type(e).__name__}）。{delay}秒後に再接続します")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    def _eew_embed(self, d: dict) -> discord.Embed:
        eq = d.get("earthquake") or {}
        hy = eq.get("hypocenter") or {}
        areas = d.get("areas") or []
        serial = (d.get("issue") or {}).get("serial") or "-"
        if d.get("cancelled"):
            e = discord.Embed(title="⚪ 緊急地震速報は取り消されました", color=0x6B7280,
                              description="先ほどの緊急地震速報（警報）は取り消されました。")
            e.set_footer(text="出典：気象庁（P2P地震情報 経由）")
            return e
        top = max((int(a.get("scaleTo") if a.get("scaleTo") not in (None, 99, -1) else a.get("scaleFrom") or 0)
                   for a in areas), default=0)
        e = discord.Embed(title="🚨 緊急地震速報（警報）", color=0xDC2626,
                          description="**強い揺れに警戒してください。**\n"
                                      "身の安全を確保し、丈夫な机の下などで揺れがおさまるのを待ってください。")
        if d.get("test"):
            e.title = "🧪 【訓練・テスト】緊急地震速報（警報）"
        e.add_field(name="📍 震源", value=hy.get("name") or "調査中", inline=True)
        e.add_field(name="🕒 発生時刻", value=(eq.get("originTime") or "-")[-8:-3] or "-", inline=True)
        e.add_field(name="📈 予想最大震度", value=f"**{self._scale(top)}**" if top else "不明", inline=True)
        mag = hy.get("magnitude")
        e.add_field(name="📏 マグニチュード", value=f"M{mag}" if mag not in (None, -1) else "不明", inline=True)
        depth = hy.get("depth")
        e.add_field(name="⬇️ 深さ", value=("ごく浅い" if depth == 0 else f"約{depth}km") if depth not in (None, -1) else "不明",
                    inline=True)
        e.add_field(name="🔢 報数", value=f"第{serial}報", inline=True)
        # 強い揺れが予想される地域（都道府県ごと）
        by_pref: dict[str, list[str]] = {}
        for a in sorted(areas, key=lambda a: -(a.get("scaleFrom") or 0)):
            name = a.get("name") or ""
            if a.get("kindCode") == "11":
                name += "（到達済み）"
            by_pref.setdefault(a.get("pref") or "その他", []).append(name)
        for pref, names in list(by_pref.items())[:15]:
            e.add_field(name=pref, value="、".join(names)[:1024], inline=False)
        if not by_pref:
            e.add_field(name="対象地域", value="発表なし", inline=False)
        e.set_footer(text="出典：気象庁（P2P地震情報 経由）・予想は誤差を含みます")
        e.timestamp = datetime.now(timezone.utc)
        return e

    async def _on_eew(self, d: dict):
        eid = str((d.get("issue") or {}).get("eventId") or d.get("id") or "")
        try:
            serial = int((d.get("issue") or {}).get("serial") or 0)
        except ValueError:
            serial = 0
        if d.get("test") and os.getenv("EEW_ALLOW_TEST") != "1":
            return  # 試験配信は通知しない
        if not eid or (self._eew_seen.get(eid, -1) >= serial and not d.get("cancelled")):
            return  # 同じ報・古い報は送らない
        self._eew_seen[eid] = serial
        if len(self._eew_seen) > 200:
            for k in list(self._eew_seen)[:100]:
                self._eew_seen.pop(k, None)
                self._eew_msgs.pop(k, None)
        embed = self._eew_embed(d)
        sent = self._eew_msgs.setdefault(eid, {})
        for gid, cfg in self.bot.store_load("disaster_config").items():
            cid = cfg.get("eew_channel")
            if not cid or self._suspended(int(gid)):
                continue
            ch = self.bot.get_channel(int(cid))
            if ch is None:
                continue
            try:
                if cid in sent:  # 続報・取り消しは、最初のメッセージを書き換える
                    await ch.get_partial_message(sent[cid]).edit(embed=embed)
                else:
                    role = cfg.get("eew_role")
                    m = await ch.send(content=f"<@&{role}>" if role else None, embed=embed,
                                      allowed_mentions=discord.AllowedMentions(roles=True))
                    sent[cid] = m.id
            except discord.HTTPException as e:
                print(f"緊急地震速報の送信に失敗 ({gid}): {type(e).__name__}")

    def _suspended(self, gid: int) -> bool:
        return getattr(self.bot, "is_suspended", lambda _: False)(gid)

    # ================= 天気予報 =================
    async def _forecast_one(self, name: str, code: str) -> str:
        r = await self.http.get(f"https://www.jma.go.jp/bosai/forecast/data/forecast/{code}.json")
        r.raise_for_status()
        data = r.json()
        short = data[0]["timeSeries"]
        ts0 = short[0]
        area = ts0["areas"][0]
        date = ts0["timeDefines"][0][:10]
        weather = re.sub(r"\s+", " ", area.get("weathers", ["-"])[0])[:30]
        emoji = _weather_emoji(area.get("weatherCodes", [""])[0])
        pops = []
        if len(short) > 1:
            for t, p in zip(short[1]["timeDefines"], short[1]["areas"][0].get("pops", [])):
                if t[:10] == date and p != "":
                    pops.append(int(p))
        tmin = tmax = None
        if len(short) > 2:
            for t, v in zip(short[2]["timeDefines"], short[2]["areas"][0].get("temps", [])):
                if t[:10] != date or v in ("", None):
                    continue
                if t[11:13] == "00":
                    tmin = v
                elif t[11:13] == "09":
                    tmax = v
        if (tmin is None or tmax is None) and len(data) > 1:  # 週間予報で補う
            try:
                w = data[1]["timeSeries"][1]
                for t, lo, hi in zip(w["timeDefines"], w["areas"][0]["tempsMin"], w["areas"][0]["tempsMax"]):
                    if t[:10] == date:
                        tmin = tmin or lo or None
                        tmax = tmax or hi or None
            except Exception:
                pass
        temp = f"🌡️ {tmax or '-'}℃ / {tmin or '-'}℃"
        pop = f"☂️ {max(pops)}%" if pops else ""
        return f"**{name}** {emoji} {weather}　{temp}　{pop}"

    async def build_forecast_embed(self) -> discord.Embed:
        if time.time() - self._fc_cache[0] > 600:
            res = await asyncio.gather(*(self._forecast_one(n, c) for n, c in CITIES), return_exceptions=True)
            lines = [x if isinstance(x, str) else f"**{n}** 取得できませんでした" for x, (n, _) in zip(res, CITIES)]
            self._fc_cache = (time.time(), lines)
        now = datetime.now(JST)
        e = discord.Embed(title=f"🗾 全国の天気予報（{now.month}月{now.day}日）",
                          description="\n".join(self._fc_cache[1]), color=0x0EA5E9,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="見方", value="天気　最高 / 最低気温　最大降水確率", inline=False)
        e.set_footer(text="出典：気象庁ホームページ")
        return e

    @weather.command(name="set", description="天気予報を自動投稿するチャンネルと時刻を設定します")
    @app_commands.describe(channel="投稿先", times="投稿時刻（日本時間・カンマ区切り 例: 07:00,18:00）")
    async def weather_set(self, interaction: discord.Interaction, channel: discord.TextChannel, times: str = "07:00"):
        ts = []
        for t in times.replace("、", ",").split(","):
            m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", t)
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                await interaction.response.send_message(f"時刻の形式が正しくありません: `{t}`（例: 07:00）", ephemeral=True)
                return
            ts.append(f"{int(m.group(1)):02d}:{m.group(2)}")
        data = self.bot.store_load("weather_config")
        data[str(interaction.guild_id)] = {"channel": channel.id, "times": sorted(set(ts))[:6], "last": {}}
        self.bot.store_save("weather_config", data)
        await interaction.response.send_message(
            f"✅ 毎日 {'、'.join(sorted(set(ts)))}（日本時間）に {channel.mention} へ全国の天気予報を投稿します。",
            ephemeral=True)

    @weather.command(name="off", description="天気予報の自動投稿を止めます")
    async def weather_off(self, interaction: discord.Interaction):
        data = self.bot.store_load("weather_config")
        data.pop(str(interaction.guild_id), None)
        self.bot.store_save("weather_config", data)
        await interaction.response.send_message("🔕 天気予報の自動投稿を停止しました。", ephemeral=True)

    @weather.command(name="now", description="今すぐ全国の天気予報を表示します")
    async def weather_now(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            await interaction.followup.send(embed=await self.build_forecast_embed())
        except Exception as e:
            await interaction.followup.send(f"天気予報を取得できませんでした（{type(e).__name__}）。")

    @tasks.loop(seconds=30)
    async def weather_loop(self):
        now = datetime.now(JST)
        hm, today = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
        data = self.bot.store_load("weather_config")
        changed = False
        for gid, cfg in data.items():
            if hm not in cfg.get("times", []) or cfg.get("last", {}).get(hm) == today:
                continue
            cfg.setdefault("last", {})[hm] = today
            changed = True
            if self._suspended(int(gid)):
                continue
            ch = self.bot.get_channel(cfg["channel"])
            if ch is None:
                continue
            try:
                await ch.send(embed=await self.build_forecast_embed())
            except Exception as e:
                print(f"天気予報の投稿に失敗 ({gid}): {type(e).__name__}")
        if changed:
            self.bot.store_save("weather_config", data)

    @weather_loop.before_loop
    async def _wb(self):
        await self.bot.wait_until_ready()

    # ================= 災害情報 =================
    @disaster.command(name="set", description="災害情報を自動通知するチャンネルを設定します")
    @app_commands.describe(channel="通知先", min_intensity="通知する最小震度",
                           weather_level="気象警報の通知レベル", area="地域で絞り込む（例: 東京。気象・火山に適用）",
                           mention_role="重大な情報（震度5弱以上・津波警報・特別警報・噴火警報）でメンションするロール")
    @app_commands.choices(
        min_intensity=[app_commands.Choice(name=f"震度{INT_LABEL.get(v, v)}以上", value=v) for v in INT_ORDER[:7]],
        weather_level=[app_commands.Choice(name="特別警報・危険警報のみ", value="special"),
                       app_commands.Choice(name="警報以上（土砂災害・記録的大雨・竜巻も）", value="warning"),
                       app_commands.Choice(name="注意報以上（すべて）", value="advisory"),
                       app_commands.Choice(name="通知しない", value="off")])
    async def disaster_set(self, interaction: discord.Interaction, channel: discord.TextChannel,
                           min_intensity: str = "3", weather_level: str = "special",
                           area: str | None = None, mention_role: discord.Role | None = None):
        data = self.bot.store_load("disaster_config")
        cur = data.get(str(interaction.guild_id), {})
        cur.update({"channel": channel.id, "min_int": min_intensity,
                    "weather": weather_level, "area": (area or "").strip()[:20],
                    "role": mention_role.id if mention_role else None})
        data[str(interaction.guild_id)] = cur
        self.bot.store_save("disaster_config", data)
        await interaction.response.send_message(
            f"✅ {channel.mention} に災害情報を通知します\n"
            + (f"・地震: <#{cur['quake_channel']}> に分離中（`/disaster quake` の設定）\n" if cur.get("quake_channel")
               else f"・地震: 震度{INT_LABEL.get(min_intensity, min_intensity)}以上（`/disaster quake` で別チャンネルに分離できます）\n")
            + f"・津波・噴火: 常に通知\n・気象: {dict(special='特別警報・危険警報のみ', warning='警報以上', advisory='注意報以上', off='通知しない')[weather_level]}"
            + (f"（地域: {area}）" if area else "") + "\n※ 緊急地震速報（EEW）は気象庁が一般公開していないため対象外です。",
            ephemeral=True)

    @disaster.command(name="off", description="災害情報の自動通知を止めます")
    async def disaster_off(self, interaction: discord.Interaction):
        data = self.bot.store_load("disaster_config")
        data.pop(str(interaction.guild_id), None)
        self.bot.store_save("disaster_config", data)
        await interaction.response.send_message("🔕 災害情報（地震を含む）の自動通知をすべて停止しました。", ephemeral=True)

    @disaster.command(name="quake", description="地震情報だけを、別のチャンネルに通知するよう設定します")
    @app_commands.describe(channel="地震情報の通知先", min_intensity="通知する最小震度",
                           mention_role="震度5弱以上のときにメンションするロール（任意）")
    @app_commands.choices(
        min_intensity=[app_commands.Choice(name=f"震度{INT_LABEL.get(v, v)}以上", value=v) for v in INT_ORDER[:7]])
    async def disaster_quake(self, interaction: discord.Interaction, channel: discord.TextChannel,
                             min_intensity: str = "3", mention_role: discord.Role | None = None):
        data = self.bot.store_load("disaster_config")
        cur = data.setdefault(str(interaction.guild_id), {})
        cur.update({"quake_channel": channel.id, "quake_min_int": min_intensity,
                    "quake_role": mention_role.id if mention_role else None})
        self.bot.store_save("disaster_config", data)
        other = f"<#{cur['channel']}>" if cur.get("channel") else "未設定（`/disaster set` で設定）"
        await interaction.response.send_message(
            f"🌏 地震情報は {channel.mention} に通知します（震度{INT_LABEL.get(min_intensity, min_intensity)}以上）。\n"
            f"・津波・火山・気象警報の通知先: {other}", ephemeral=True)

    @disaster.command(name="eew", description="緊急地震速報（警報）を通知するチャンネルを設定します")
    @app_commands.describe(channel="緊急地震速報の通知先", mention_role="発表時にメンションするロール（任意）")
    async def disaster_eew(self, interaction: discord.Interaction, channel: discord.TextChannel,
                           mention_role: discord.Role | None = None):
        p = channel.permissions_for(interaction.guild.me)
        if not (p.send_messages and p.embed_links):
            await interaction.response.send_message(
                f"{channel.mention} でBotに「メッセージの送信・埋め込みリンク」権限が必要です。", ephemeral=True)
            return
        data = self.bot.store_load("disaster_config")
        cur = data.setdefault(str(interaction.guild_id), {})
        cur.update({"eew_channel": channel.id, "eew_role": mention_role.id if mention_role else None})
        self.bot.store_save("disaster_config", data)
        await interaction.response.send_message(
            f"🚨 緊急地震速報（警報）を {channel.mention} に通知します"
            + (f"（{mention_role.mention} をメンション）" if mention_role else "") + "。\n"
            "・警報は予想最大震度5弱以上のときに発表されます（予報は対象外）\n"
            "・続報や取り消しは、同じメッセージを書き換えて更新します\n"
            "※ ネット経由のため、テレビ・スマホの速報より数秒〜十数秒遅れることがあります。"
            "通知を待たずに、揺れを感じたらすぐ身を守ってください。",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @disaster.command(name="eew-off", description="緊急地震速報の通知を止めます")
    async def disaster_eew_off(self, interaction: discord.Interaction):
        data = self.bot.store_load("disaster_config")
        cur = data.get(str(interaction.guild_id), {})
        cur.pop("eew_channel", None)
        cur.pop("eew_role", None)
        if cur:
            data[str(interaction.guild_id)] = cur
        else:
            data.pop(str(interaction.guild_id), None)
        self.bot.store_save("disaster_config", data)
        await interaction.response.send_message("🔕 緊急地震速報の通知を止めました。", ephemeral=True)

    @disaster.command(name="quake-off", description="地震情報の分離をやめます（地震は通常の通知先に戻ります）")
    async def disaster_quake_off(self, interaction: discord.Interaction):
        data = self.bot.store_load("disaster_config")
        cur = data.get(str(interaction.guild_id), {})
        for k in ("quake_channel", "quake_min_int", "quake_role"):
            cur.pop(k, None)
        if cur:
            data[str(interaction.guild_id)] = cur
        else:
            data.pop(str(interaction.guild_id), None)
        self.bot.store_save("disaster_config", data)
        dest = f"<#{cur['channel']}>" if cur.get("channel") else "どこにも通知されません"
        await interaction.response.send_message(f"✅ 地震情報の分離をやめました。地震情報は {dest}。", ephemeral=True)

    @disaster.command(name="test", description="通知先へテスト通知を送ります")
    async def disaster_test(self, interaction: discord.Interaction):
        cfg = self.bot.store_load("disaster_config").get(str(interaction.guild_id)) or {}
        targets = []
        if cfg.get("channel"):
            targets.append((cfg["channel"], "津波・火山・気象警報" if cfg.get("quake_channel") else "地震・津波・火山・気象警報"))
        if cfg.get("quake_channel"):
            targets.append((cfg["quake_channel"], "地震情報"))
        if not targets:
            await interaction.response.send_message("先に `/disaster set` か `/disaster quake` で設定してください。", ephemeral=True)
            return
        results = []
        for cid, what in targets:
            ch = self.bot.get_channel(cid)
            try:
                await ch.send(embed=discord.Embed(title="🧪 災害情報通知のテスト", color=0x6B7280,
                                                  description=f"このチャンネルに、{what}が届きます。"))
                results.append(f"✅ <#{cid}>（{what}）")
            except (discord.HTTPException, AttributeError):
                results.append(f"❌ <#{cid}>（{what}）送信できません。Botの権限を確認してください")
        await interaction.response.send_message("\n".join(results), ephemeral=True)

    async def _get(self, url: str):
        h = {}
        if url in self._lastmod:
            h["If-Modified-Since"] = self._lastmod[url]
        r = await self.http.get(url, headers=h)
        if r.status_code == 304:
            return None
        r.raise_for_status()
        if r.headers.get("last-modified"):
            self._lastmod[url] = r.headers["last-modified"]
        return r

    async def _collect(self) -> list[dict]:
        """新しい情報を集める。戻り値: [{"kind", "key", "embed", "severe", "int", "text"}]"""
        out = []
        try:
            r = await self._get("https://www.jma.go.jp/bosai/quake/data/list.json")
            for q in (r.json()[:30] if r else []):
                if q.get("ttl") not in ("震度速報", "震源・震度情報") or not q.get("maxi"):
                    continue
                key = f"q:{q.get('eid')}:{q.get('ttl')}"
                mx = q["maxi"]
                item = {"kind": "quake", "key": key, "embed": None, "int": mx, "text": q.get("anm", ""),
                        "severe": _int_rank(mx) >= _int_rank("5-")}
                if self._bootstrapped and key not in self._seen:  # 新しい情報だけ、詳細を取りに行く
                    detail = None
                    if q.get("json"):
                        try:
                            dr = await self.http.get(f"https://www.jma.go.jp/bosai/quake/data/{q['json']}")
                            dr.raise_for_status()
                            detail = dr.json()
                        except Exception as ex:
                            print(f"地震の詳細の取得に失敗: {type(ex).__name__}")
                    item["embed"] = self._quake_embed(q, detail)
                out.append(item)
        except Exception as ex:
            print(f"地震情報の取得に失敗: {type(ex).__name__}")
        for url, kind in (("https://www.data.jma.go.jp/developer/xml/feed/eqvol.xml", "eqvol"),
                          ("https://www.data.jma.go.jp/developer/xml/feed/extra.xml", "weather")):
            try:
                r = await self._get(url)
                if r is None:
                    continue
                root = ET.fromstring(r.content)
                for ent in root.findall(f"{ATOM}entry")[:80]:
                    title = ent.findtext(f"{ATOM}title") or ""
                    eid = ent.findtext(f"{ATOM}id") or ""
                    content = (ent.findtext(f"{ATOM}content") or "").strip()
                    author = ent.findtext(f"{ATOM}author/{ATOM}name") or ""
                    text = f"{title} {author} {content}"
                    item = self._classify(kind, title, content, author)
                    if item is None:
                        continue
                    sub, color, severe, wlevel = item
                    e = discord.Embed(title=sub, description=content[:1500] or title, color=color)
                    e.set_author(name=author or "気象庁")
                    e.set_footer(text=f"出典：気象庁（{title}）")
                    out.append({"kind": sub.split()[0], "key": f"x:{eid}", "embed": e, "severe": severe,
                                "text": text, "cat": "weather" if kind == "weather" else "eqvol",
                                "wlevel": wlevel})
            except Exception as ex:
                print(f"気象庁XMLの取得に失敗 ({kind}): {type(ex).__name__}")
        return out

    @staticmethod
    def _as_list(v) -> list:
        return v if isinstance(v, list) else ([v] if v else [])

    @classmethod
    def _quake_embed(cls, q: dict, detail: dict | None) -> discord.Embed:
        """地震情報の埋め込み: 発生時刻・震源地・深さ・規模・最大震度・津波・震度3以上の地域。"""
        mx = q["maxi"]
        e = discord.Embed(title=f"🌏 {q['ttl']}・最大震度{INT_LABEL.get(mx, mx)}",
                          color=INT_COLOR.get(mx, 0x6B7280),
                          url="https://www.jma.go.jp/bosai/map.html#contents=earthquake_map")
        body = (detail or {}).get("Body", {}) or {}
        eq = body.get("Earthquake", {}) or {}
        # 深さは座標「+38.3+142.4-10000/」の最後（メートル・下向きがマイナス）
        cod = q.get("cod") or ""
        m = re.match(r"^([+-][\d.]+)([+-][\d.]+)([+-]\d+)?/?$", cod)
        if m and m.group(3) is not None:
            km = abs(int(m.group(3))) // 1000
            depth = "ごく浅い" if km == 0 else f"約{km}km"
        elif q["ttl"] == "震度速報":
            depth = "調査中"
        else:
            depth = "不明"
        e.add_field(name="🕒 発生時刻", value=(q.get("at") or "-")[:16].replace("T", " "), inline=True)
        e.add_field(name="📍 震源地", value=q.get("anm") or ((eq.get("Hypocenter") or {}).get("Area") or {}).get("Name") or "調査中", inline=True)
        e.add_field(name="⬇️ 深さ", value=depth, inline=True)
        e.add_field(name="📏 マグニチュード", value=f"M{q['mag']}" if q.get("mag") not in (None, "", "Ｍ不明") else "調査中", inline=True)
        e.add_field(name="📈 最大震度", value=f"**{INT_LABEL.get(mx, mx)}**", inline=True)
        # 津波
        comments = body.get("Comments", {}) or {}
        texts = [c.get("Text", "") for c in cls._as_list(comments.get("ForecastComment")) if isinstance(c, dict)]
        tsunami = " ".join(t for t in texts if t) or ("今後の情報に注意してください" if q["ttl"] == "震度速報" else "情報なし")
        e.add_field(name="🌊 津波", value=tsunami[:1024], inline=False)
        # 震度3以上の地域（震度ごとにまとめる。震源・震度情報は市町村名も付ける）
        groups: dict[str, list[str]] = {}
        obs = ((body.get("Intensity") or {}).get("Observation") or {})
        for pref in cls._as_list(obs.get("Pref")):
            for area in cls._as_list(pref.get("Area")):
                amax = area.get("MaxInt")
                if _int_rank(amax) < _int_rank("3"):
                    continue
                for city in cls._as_list(area.get("City")):
                    cmax = city.get("MaxInt")
                    if _int_rank(cmax) >= _int_rank("3"):
                        groups.setdefault(cmax, []).append(city.get("Name", ""))
                if not area.get("City"):  # 震度速報は地域単位
                    groups.setdefault(amax, []).append(area.get("Name", ""))
        for lv in sorted(groups, key=_int_rank, reverse=True):
            names = [n for n in dict.fromkeys(groups[lv]) if n]
            text, shown = "", 0
            for n in names:
                if len(text) + len(n) + 1 > 950:
                    break
                text += ("、" if text else "") + n
                shown += 1
            if shown < len(names):
                text += f" ほか{len(names) - shown}か所"
            e.add_field(name=f"震度{INT_LABEL.get(lv, lv)}", value=text or "-", inline=False)
            if len(e.fields) >= 24:
                break
        if not groups:
            e.add_field(name="震度3以上の地域", value="なし" if detail else "取得できませんでした（気象庁のページで確認してください）",
                        inline=False)
        e.set_footer(text="出典：気象庁")
        return e

    @staticmethod
    def _classify(kind, title, content, author):
        """通知する情報なら (表題, 色, 重大か, 気象の段階) を返す。表題は発表内容の段階に合わせる。
        気象の段階: 1=注意報 2=警報・警戒情報 3=危険警報 4=特別警報 0=解除"""
        text = title + " " + content
        body = re.sub(r"^【[^】]*】", "", content)
        if kind == "eqvol":
            if "津波" in title:
                text = content or title  # 表題は「津波警報・注意報・予報」と全部入りなので、本文で判断する
                if "解除" in text and "発表" not in text:
                    return ("✅ 津波警報・注意報の解除", 0x22C55E, False, None)
                if "大津波警報" in text:
                    return ("🚨 大津波警報", 0x7E22CE, True, None)
                if "津波警報" in text:
                    return ("🌊 津波警報", 0xDC2626, True, None)
                if "津波注意報" in text:
                    return ("🟡 津波注意報", 0xEAB308, False, None)
                return None  # 若干の海面変動などの予報のみ
            if "噴火" in title and "解説" not in title:
                if "噴火速報" in title:
                    return ("🌋 噴火速報", 0xB91C1C, True, None)
                if "噴火警報" in text and "解除" not in body[:40]:
                    return ("🌋 噴火警報", 0xDC2626, True, None)
                return ("🌋 噴火予報・火山情報", 0xB45309, False, None)
            return None
        # ---- 気象 ----
        named = {"土砂災害警戒情報": ("🟥 土砂災害警戒情報", 0xDC2626, 2),
                 "記録的短時間大雨情報": ("🟥 記録的短時間大雨情報", 0xDC2626, 2),
                 "竜巻注意情報": ("🌪️ 竜巻注意情報", 0xEA580C, 2),
                 "指定河川洪水予報": ("🌊 指定河川洪水予報", 0xEA580C, 2)}
        for key, (label, color, lv) in named.items():
            if key in title:
                if "解除" in body[:40]:
                    return (f"✅ {key}（解除）", 0x22C55E, False, 0)
                return (label, color, False, lv)
        if "警報" not in title and "注意報" not in title:
            return None  # 府県気象情報などの解説は対象外
        if "解除" in body and not any(w in body for w in ("発表", "警戒して", "注意して", "切り替え")):
            return ("✅ 警報・注意報の解除", 0x22C55E, False, 0)
        if "特別警報" in body and "解除" not in body[:40]:
            return ("🚨 特別警報", 0x7E22CE, True, 4)
        if "危険警報" in body:
            return ("🟪 危険警報", 0xB91C1C, True, 3)
        active = re.sub(r"[^、。]*警報を解除[^、。]*", "", body)  # 「○○警報を解除し…」の部分は数えない
        if "警報" in active.replace("注意報", "") or "警戒してください" in active:
            return ("⚠️ 警報", 0xDC2626, False, 2)
        if "注意報" in body or "注意してください" in body:
            return ("🟡 注意報", 0xEAB308, False, 1)
        if "解除" in body:
            return ("✅ 警報・注意報の解除", 0x22C55E, False, 0)
        return None

    @tasks.loop(seconds=45)
    async def disaster_loop(self):
        configs = self.bot.store_load("disaster_config")
        if not configs and self._bootstrapped:
            return
        items = await self._collect()
        new = [i for i in items if i["key"] not in self._seen]
        self._seen.update(i["key"] for i in items)
        if len(self._seen) > 5000:
            self._seen = {i["key"] for i in items}
        if not self._bootstrapped:  # 起動直後は、過去の情報を流さない
            self._bootstrapped = True
            return
        for item in reversed(new):
            if item.get("embed") is None:
                continue
            for gid, cfg in configs.items():
                if self._suspended(int(gid)) or not self._wants(cfg, item):
                    continue
                quake_sep = item["kind"] == "quake" and cfg.get("quake_channel")
                ch = self.bot.get_channel(cfg["quake_channel"] if quake_sep else cfg.get("channel") or 0)
                if ch is None:
                    continue
                sent = [t for t in self._sent_count.get(int(gid), []) if time.time() - t < 600]
                if not item["severe"] and len(sent) >= 15:
                    continue  # 大量発表時の連投を抑える（重大な情報は必ず送る）
                sent.append(time.time())
                self._sent_count[int(gid)] = sent
                role = (cfg.get("quake_role") if quake_sep else cfg.get("role")) if item["severe"] else None
                try:
                    await ch.send(content=f"<@&{role}>" if role else None, embed=item["embed"],
                                  allowed_mentions=discord.AllowedMentions(roles=True))
                except Exception as e:
                    print(f"災害情報の送信に失敗 ({gid}): {type(e).__name__}")
                await asyncio.sleep(0.3)

    @staticmethod
    def _wants(cfg: dict, item: dict) -> bool:
        if item["kind"] == "quake":
            if cfg.get("quake_channel"):
                return _int_rank(item["int"]) >= _int_rank(cfg.get("quake_min_int", "3"))
            return bool(cfg.get("channel")) and _int_rank(item["int"]) >= _int_rank(cfg.get("min_int", "3"))
        if not cfg.get("channel"):
            return False  # 地震だけを設定しているサーバー
        if item.get("cat") == "weather":
            # 設定した段階以上の情報だけを送る（解除は「警報」以下を選んでいるときに送る）
            need = {"special": 3, "warning": 2, "advisory": 1}.get(cfg.get("weather", "special"))
            wl = item.get("wlevel") or 0
            if need is None:
                return False
            if wl == 0:
                if need > 2:
                    return False
            elif wl < need:
                return False
        area = cfg.get("area")
        return not area or item["severe"] and item.get("cat") == "eqvol" or area in item["text"]

    @disaster_loop.before_loop
    async def _db(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(JMAAlerts(bot))
