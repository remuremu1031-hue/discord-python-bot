"""気象庁の公開データを使った、天気予報の定時投稿と災害情報のリアルタイム通知。

天気予報: https://www.jma.go.jp/bosai/forecast/data/forecast/<地域コード>.json
地震    : https://www.jma.go.jp/bosai/quake/data/list.json
津波・火山: https://www.data.jma.go.jp/developer/xml/feed/eqvol.xml（気象庁防災情報XML）
気象警報 : https://www.data.jma.go.jp/developer/xml/feed/extra.xml
"""
from __future__ import annotations

import asyncio
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

    async def cog_unload(self):
        self.weather_loop.cancel()
        self.disaster_loop.cancel()
        await self.http.aclose()

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
        weather_level=[app_commands.Choice(name="特別警報のみ", value="special"),
                       app_commands.Choice(name="警報・土砂災害・記録的大雨・竜巻も", value="warning"),
                       app_commands.Choice(name="通知しない", value="off")])
    async def disaster_set(self, interaction: discord.Interaction, channel: discord.TextChannel,
                           min_intensity: str = "3", weather_level: str = "special",
                           area: str | None = None, mention_role: discord.Role | None = None):
        data = self.bot.store_load("disaster_config")
        data[str(interaction.guild_id)] = {"channel": channel.id, "min_int": min_intensity,
                                           "weather": weather_level, "area": (area or "").strip()[:20],
                                           "role": mention_role.id if mention_role else None}
        self.bot.store_save("disaster_config", data)
        await interaction.response.send_message(
            f"✅ {channel.mention} に災害情報を通知します\n・地震: 震度{INT_LABEL.get(min_intensity, min_intensity)}以上\n"
            f"・津波・噴火: 常に通知\n・気象: {dict(special='特別警報のみ', warning='警報など', off='通知しない')[weather_level]}"
            + (f"（地域: {area}）" if area else "") + "\n※ 緊急地震速報（EEW）は気象庁が一般公開していないため対象外です。",
            ephemeral=True)

    @disaster.command(name="off", description="災害情報の自動通知を止めます")
    async def disaster_off(self, interaction: discord.Interaction):
        data = self.bot.store_load("disaster_config")
        data.pop(str(interaction.guild_id), None)
        self.bot.store_save("disaster_config", data)
        await interaction.response.send_message("🔕 災害情報の自動通知を停止しました。", ephemeral=True)

    @disaster.command(name="test", description="通知先へテスト通知を送ります")
    async def disaster_test(self, interaction: discord.Interaction):
        cfg = self.bot.store_load("disaster_config").get(str(interaction.guild_id))
        ch = self.bot.get_channel(cfg["channel"]) if cfg else None
        if ch is None:
            await interaction.response.send_message("先に `/disaster set` で設定してください。", ephemeral=True)
            return
        try:
            await ch.send(embed=discord.Embed(title="🧪 災害情報通知のテスト", color=0x6B7280,
                                              description="このチャンネルに、地震・津波・火山・気象警報が届きます。"))
            await interaction.response.send_message("✅ 送信しました。", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("❌ 送信できません。Botの権限を確認してください。", ephemeral=True)

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
                e = discord.Embed(title=f"🌏 地震情報（{q['ttl']}）・最大震度{INT_LABEL.get(mx, mx)}",
                                  color=INT_COLOR.get(mx, 0x6B7280), url="https://www.jma.go.jp/bosai/map.html#contents=earthquake_map")
                e.add_field(name="発生時刻", value=(q.get("at") or "-")[:16].replace("T", " "), inline=True)
                e.add_field(name="震源", value=q.get("anm") or "調査中", inline=True)
                e.add_field(name="規模", value=f"M{q['mag']}" if q.get("mag") else "-", inline=True)
                e.set_footer(text="出典：気象庁")
                out.append({"kind": "quake", "key": key, "embed": e, "int": mx, "text": q.get("anm", ""),
                            "severe": _int_rank(mx) >= _int_rank("5-")})
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
                    sub, color, severe = item
                    e = discord.Embed(title=sub, description=content[:1500] or title, color=color)
                    e.set_author(name=author or "気象庁")
                    e.set_footer(text=f"出典：気象庁（{title}）")
                    out.append({"kind": sub.split()[0], "key": f"x:{eid}", "embed": e, "severe": severe,
                                "text": text, "cat": "weather" if kind == "weather" else "eqvol"})
            except Exception as ex:
                print(f"気象庁XMLの取得に失敗 ({kind}): {type(ex).__name__}")
        return out

    @staticmethod
    def _classify(kind, title, content, author):
        """通知する情報なら (表題, 色, 重大か) を返す。"""
        body = re.sub(r"^【[^】]*】", "", content)
        if kind == "eqvol":
            if "津波" in title and any(w in title + content for w in ("大津波警報", "津波警報", "津波注意報")):
                severe = "警報" in content.replace("注意報", "")
                return ("🌊 津波情報", 0x1D4ED8 if not severe else 0xDC2626, severe)
            if "噴火" in title and "解説" not in title:
                severe = "噴火速報" in title or "噴火警報" in content
                return ("🌋 火山情報", 0xB45309, severe)
            return None
        tag = ("special" if ("特別警報" in title + content and "解除" not in body[:30]) else
               "warning" if (title in ("記録的短時間大雨情報", "土砂災害警戒情報", "竜巻注意情報")
                             or "危険警報" in body or "警報" in body.replace("注意報", "")) else None)
        if tag == "special":
            return ("🚨 特別警報", 0x7E22CE, True)
        if tag == "warning":
            return ("⚠️ 気象警報・情報", 0xEA580C, False)
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
            for gid, cfg in configs.items():
                if self._suspended(int(gid)) or not self._wants(cfg, item):
                    continue
                ch = self.bot.get_channel(cfg["channel"])
                if ch is None:
                    continue
                sent = [t for t in self._sent_count.get(int(gid), []) if time.time() - t < 600]
                if not item["severe"] and len(sent) >= 15:
                    continue  # 大量発表時の連投を抑える（重大な情報は必ず送る）
                sent.append(time.time())
                self._sent_count[int(gid)] = sent
                role = cfg.get("role") if item["severe"] else None
                try:
                    await ch.send(content=f"<@&{role}>" if role else None, embed=item["embed"],
                                  allowed_mentions=discord.AllowedMentions(roles=True))
                except Exception as e:
                    print(f"災害情報の送信に失敗 ({gid}): {type(e).__name__}")
                await asyncio.sleep(0.3)

    @staticmethod
    def _wants(cfg: dict, item: dict) -> bool:
        if item["kind"] == "quake":
            return _int_rank(item["int"]) >= _int_rank(cfg.get("min_int", "3"))
        if item.get("cat") == "weather":
            lv = cfg.get("weather", "special")
            if lv == "off" or (lv == "special" and not item["severe"]):
                return False
        area = cfg.get("area")
        return not area or item["severe"] and item.get("cat") == "eqvol" or area in item["text"]

    @disaster_loop.before_loop
    async def _db(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(JMAAlerts(bot))
