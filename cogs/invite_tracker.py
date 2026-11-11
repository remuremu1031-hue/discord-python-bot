"""招待リンクの追跡。
・招待リンクの作成を検知し、URL・有効期限・作成者の累計作成回数をログチャンネルへ記録
・メンバー参加時に「どの招待リンク（誰がいつ作成）」から来たかを特定してログに出し、Redisに保存
  → main.py の認証ログが bot.get_join_invite() で読み出して表示する
Redis キー:
  invcount:<サーバーID>:<ユーザーID>   … そのユーザーが作成した招待リンクの累計数
  invjoin:<サーバーID>:<ユーザーID>    … 参加に使った招待リンクの情報（90日保持）
  invmeta:<サーバーID>:<コード>        … 招待リンクの作成者・作成日時（期限切れ後も分かるように）
※ Botに「サーバーの管理」権限が必要（招待一覧の取得のため）。メンバー参加の検知には
  SERVER MEMBERS INTENT（ENABLE_MEMBERS_INTENT=1）が必要。"""
from __future__ import annotations

import asyncio
import time
import json
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands


class kv:
    """Redisの小さなヘルパー（main.py の bot.kv を使う）。
    Redis が未設定・一時的に失敗したときは、メモリに保存して動き続ける（再起動で消える）。"""
    _mem: dict = {}
    _exp: dict = {}

    @classmethod
    def _alive(cls, key):
        if key in cls._exp and cls._exp[key] < time.time():
            cls._mem.pop(key, None)
            cls._exp.pop(key, None)
        return key in cls._mem

    @staticmethod
    async def _do(bot, op, *args):
        if getattr(bot, "kv_ready", lambda: False)():
            try:
                return True, await bot.kv(op, *args)
            except Exception as e:
                print(f"Redisの操作に失敗（{op}）: {type(e).__name__}")
        return False, None

    @classmethod
    async def get_json(cls, bot, key, default=None):
        ok, v = await cls._do(bot, "get", key)
        if ok:
            try:
                return json.loads(v) if v else default
            except Exception:
                return default
        return cls._mem.get(key, default) if cls._alive(key) else default

    @classmethod
    async def set_json(cls, bot, key, value, ttl=None):
        # redis-py の set(name, value, ex) は、3番目の引数が有効期限（秒）
        ok, _ = await cls._do(bot, "set", key, json.dumps(value, ensure_ascii=False), *([int(ttl)] if ttl else []))
        if not ok:
            cls._mem[key] = value
            if ttl:
                cls._exp[key] = time.time() + ttl

    @classmethod
    async def incr(cls, bot, key):
        ok, v = await cls._do(bot, "incr", key)
        if ok:
            return int(v)
        cls._mem[key] = int(cls._mem.get(key, 0) or 0) + 1
        return cls._mem[key]

    @classmethod
    async def get_int(cls, bot, key):
        ok, v = await cls._do(bot, "get", key)
        return int(v or 0) if ok else int(cls._mem.get(key, 0) or 0)

    @classmethod
    async def hgetall(cls, bot, key):
        ok, v = await cls._do(bot, "hgetall", key)
        out = {}
        for k, val in ((v if ok else cls._mem.get(key, {})) or {}).items():
            try:
                out[k] = json.loads(val) if isinstance(val, str) else val
            except Exception:
                pass
        return out

    @classmethod
    async def hset(cls, bot, key, field, value):
        raw = json.dumps(value, ensure_ascii=False)
        ok, _ = await cls._do(bot, "hset", key, field, raw)
        if not ok:
            cls._mem.setdefault(key, {})[field] = raw

    @classmethod
    async def hdel(cls, bot, key, field):
        ok, _ = await cls._do(bot, "hdel", key, field)
        if not ok:
            cls._mem.get(key, {}).pop(field, None)

JST = timezone(timedelta(hours=9))
JOIN_TTL = 90 * 86400


def _fmt(dt: datetime | None) -> str:
    return dt.astimezone(JST).strftime("%Y/%m/%d %H:%M") if dt else "不明"


class InviteTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: dict[int, dict[str, dict]] = {}  # サーバー → コード → {"uses","inviter","inviter_name","created","max_uses"}
        bot.get_join_invite = self.get_join_invite  # main.py から使う窓口
        bot.describe_invite = self.describe
        bot.invite_tracking_since = self.tracking_since

    # ---------- 招待一覧のキャッシュ ----------
    @staticmethod
    def _snap(inv: discord.Invite) -> dict:
        return {"uses": inv.uses or 0, "max_uses": inv.max_uses or 0,
                "inviter": inv.inviter.id if inv.inviter else None,
                "inviter_name": str(inv.inviter) if inv.inviter else None,
                "created": inv.created_at.timestamp() if inv.created_at else None,
                "expires": inv.expires_at.timestamp() if inv.expires_at else None}

    async def _refresh(self, guild: discord.Guild) -> dict[str, dict] | None:
        if not guild.me.guild_permissions.manage_guild:
            return None
        try:
            invites = await guild.invites()
        except discord.HTTPException:
            return None
        snap = {i.code: self._snap(i) for i in invites}
        if "VANITY_URL" in guild.features:
            try:
                v = await guild.vanity_invite()
                if v:
                    snap["__vanity__"] = {"uses": v.uses or 0, "code": v.code}
            except discord.HTTPException:
                pass
        return snap

    @commands.Cog.listener()
    async def on_ready(self):
        for g in list(self.bot.guilds):
            snap = await self._refresh(g)
            if snap is not None:
                self._cache[g.id] = snap
                await self._seed(g, snap)
            await asyncio.sleep(0.5)

    async def tracking_since(self, guild_id: int) -> float | None:
        """このサーバーで招待の記録を始めた時刻（これより前の参加者は、招待元が分からない）。"""
        v = await kv.get_json(self.bot, f"invstart:{guild_id}")
        return float(v) if v else None

    async def _seed(self, guild: discord.Guild, snap: dict[str, dict]):
        """記録前から存在する招待リンクも、作成者の累計数に1回ずつ数える（初回のみ）。"""
        if not await kv.get_json(self.bot, f"invstart:{guild.id}"):
            await kv.set_json(self.bot, f"invstart:{guild.id}", time.time())
        for code, d in snap.items():
            if code == "__vanity__" or not d.get("inviter"):
                continue
            mk = f"invmeta:{guild.id}:{code}"
            if await kv.get_json(self.bot, mk):
                continue
            await kv.set_json(self.bot, mk, {"inviter": d["inviter"], "inviter_name": d["inviter_name"],
                                             "created": d["created"]}, ttl=JOIN_TTL * 2)
            await kv.incr(self.bot, f"invcount:{guild.id}:{d['inviter']}")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        snap = await self._refresh(guild)
        if snap is not None:
            self._cache[guild.id] = snap

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild:
            self._cache.get(invite.guild.id, {}).pop(invite.code, None)

    # ---------- 作成の検知 ----------
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        guild = invite.guild
        if not isinstance(guild, discord.Guild):
            return
        s = self._snap(invite)
        self._cache.setdefault(guild.id, {})[invite.code] = s
        await kv.set_json(self.bot, f"invmeta:{guild.id}:{invite.code}",
                          {"inviter": s["inviter"], "inviter_name": s["inviter_name"], "created": s["created"]},
                          ttl=JOIN_TTL * 2)
        count = await kv.incr(self.bot, f"invcount:{guild.id}:{s['inviter']}") if s["inviter"] else 0
        if invite.max_age:
            exp = f"{_fmt(invite.expires_at)}（{self._age(invite.max_age)}）"
        else:
            exp = "無期限"
        e = discord.Embed(title="🔗 招待リンクが作成されました", color=0x3B82F6,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="URL", value=invite.url, inline=False)
        e.add_field(name="作成者", value=f"<@{s['inviter']}>（`{s['inviter_name']}` / `{s['inviter']}`）"
                    if s["inviter"] else "不明", inline=False)
        e.add_field(name="有効期限", value=exp, inline=True)
        e.add_field(name="使用回数の上限", value=f"{invite.max_uses}回" if invite.max_uses else "無制限", inline=True)
        e.add_field(name="チャンネル", value=getattr(invite.channel, "mention", "-"), inline=True)
        if invite.temporary:
            e.add_field(name="一時メンバー", value="はい（切断すると退出）", inline=True)
        e.add_field(name="この人の累計作成数", value=f"**{count}** 回", inline=True)
        await self._log(guild, e)

    @staticmethod
    def _age(sec: int) -> str:
        if sec % 86400 == 0:
            return f"{sec // 86400}日"
        if sec % 3600 == 0:
            return f"{sec // 3600}時間"
        return f"{sec // 60}分"

    async def _log(self, guild: discord.Guild, embed: discord.Embed):
        """送信先: /invite-log で指定したチャンネル → メインログ → /set-log-channel のチャンネル の順。"""
        cid = self.bot.store_load("invite_log_config").get(str(guild.id))
        if not cid and getattr(self.bot, "main_log_channel_id", None):
            cid = self.bot.main_log_channel_id(guild.id)  # 個別の設定が無ければメインログへ
        ch = guild.get_channel(int(cid)) if cid else None
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                return
            except discord.HTTPException as e:
                print(f"招待ログの送信に失敗 ({guild.id}): {type(e).__name__}")
        fn = getattr(self.bot, "guild_log", None)
        if fn:
            try:
                await fn(guild, embed)
            except Exception as e:
                print(f"招待ログの送信に失敗: {type(e).__name__}")

    @app_commands.command(name="invite-log", description="招待リンクの作成・メンバー参加（招待元）のログを送るチャンネルを設定します")
    @app_commands.describe(channel="送信先（省略すると解除して /set-log-channel のチャンネルに戻します）")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def invite_log(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        data = self.bot.store_load("invite_log_config")
        if channel is None:
            data.pop(str(interaction.guild_id), None)
            self.bot.store_save("invite_log_config", data)
            await interaction.response.send_message(
                "✅ 解除しました。招待ログは `/set-log-channel` のチャンネルに送られます（未設定なら送られません）。",
                ephemeral=True)
            return
        p = channel.permissions_for(interaction.guild.me)
        if not (p.view_channel and p.send_messages and p.embed_links):
            await interaction.response.send_message(
                f"{channel.mention} でBotに「チャンネルを見る・メッセージを送信・埋め込みリンク」権限が必要です。", ephemeral=True)
            return
        data[str(interaction.guild_id)] = channel.id
        self.bot.store_save("invite_log_config", data)
        warn = []
        if not interaction.guild.me.guild_permissions.manage_guild:
            warn.append("Botに「サーバーの管理」権限がないため、招待リンクの特定ができません")
        if not self.bot.intents.members:
            warn.append("メンバーIntentが無効のため、参加時のログは届きません（ENABLE_MEMBERS_INTENT=1）")
        await interaction.response.send_message(
            f"✅ 招待リンクの作成・メンバー参加のログを {channel.mention} に送ります。"
            + "".join(f"\n⚠️ {w}" for w in warn), ephemeral=True)

    # ---------- 参加時の特定 ----------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        before = self._cache.get(guild.id, {})
        after = await self._refresh(guild)
        if after is None:
            return
        self._cache[guild.id] = after
        used = None
        for code, a in after.items():
            b = before.get(code)
            if code != "__vanity__" and b and a["uses"] > b["uses"]:
                used = (code, a)
                break
        if used is None and "__vanity__" in after and after["__vanity__"]["uses"] > before.get("__vanity__", {}).get("uses", 0):
            used = (after["__vanity__"].get("code", "vanity"), {"inviter": None, "inviter_name": "カスタム招待URL", "created": None})
        if used is None:  # 使用回数1回のリンクは、使われると消える
            gone = [c for c in before if c not in after and c != "__vanity__"
                    and before[c].get("max_uses") and before[c]["uses"] + 1 >= before[c]["max_uses"]]
            if len(gone) == 1:
                used = (gone[0], before[gone[0]])
        info = {"code": None, "inviter": None, "inviter_name": None, "created": None}
        if used:
            code, d = used
            meta = await kv.get_json(self.bot, f"invmeta:{guild.id}:{code}", {}) or {}
            info = {"code": code, "inviter": d.get("inviter") or meta.get("inviter"),
                    "inviter_name": d.get("inviter_name") or meta.get("inviter_name"),
                    "created": d.get("created") or meta.get("created")}
        await kv.set_json(self.bot, f"invjoin:{guild.id}:{member.id}", info, ttl=JOIN_TTL)

        e = discord.Embed(title="📥 メンバーが参加しました", color=0x22C55E, timestamp=datetime.now(timezone.utc))
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name="メンバー", value=f"{member.mention}（`{member}` / `{member.id}`）", inline=False)
        e.add_field(name="アカウント作成日", value=_fmt(member.created_at), inline=True)
        for n, v in self.describe(info):
            e.add_field(name=n, value=v, inline=n != "招待リンク")
        await self._log(guild, e)

    @staticmethod
    def describe(info: dict | None) -> list[tuple[str, str]]:
        """認証ログ等に載せる (項目名, 内容) のリスト。"""
        if not info or not info.get("code"):
            why = (info or {}).get("missing")
            text = {
                "oauth": "なし（招待リンクを使わず、認証ページから直接サーバーに参加しました）",
                "before": "記録なし（招待の記録を始める前から、サーバーに参加していたメンバーです）",
            }.get(why, "特定できませんでした（同時に複数人が参加した・カスタム招待URL・Botの権限不足など）")
            return [("招待リンク", text)]
        created = (datetime.fromtimestamp(info["created"], JST).strftime("%Y/%m/%d %H:%M")
                   if info.get("created") else "不明")
        who = (f"<@{info['inviter']}>（`{info.get('inviter_name')}` / `{info['inviter']}`）"
               if info.get("inviter") else (info.get("inviter_name") or "不明"))
        return [("招待リンク", f"`discord.gg/{info['code']}`"), ("招待の作成者", who), ("招待の作成日時", created)]

    async def get_join_invite(self, guild_id: int, user_id: int) -> dict | None:
        return await kv.get_json(self.bot, f"invjoin:{guild_id}:{user_id}")

    # ---------- 確認用コマンド ----------
    @app_commands.command(name="invite-stats", description="ユーザーが作成した招待リンクの累計数と、今有効なリンクを表示します")
    @app_commands.describe(user="対象ユーザー（省略で自分）")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def invite_stats(self, interaction: discord.Interaction, user: discord.User | None = None):
        user = user or interaction.user
        total = await kv.get_int(self.bot, f"invcount:{interaction.guild_id}:{user.id}")
        active = [(c, d) for c, d in self._cache.get(interaction.guild_id, {}).items()
                  if c != "__vanity__" and d.get("inviter") == user.id]
        lines = [f"・`discord.gg/{c}` 使用 {d['uses']}回" + (f"/{d['max_uses']}" if d["max_uses"] else "")
                 for c, d in active[:15]]
        e = discord.Embed(title=f"🔗 {user} の招待リンク", color=0x3B82F6,
                          description=f"累計作成数: **{total}** 回（記録開始以降）\n有効なリンク: {len(active)} 件\n"
                                      + "\n".join(lines))
        await interaction.response.send_message(embed=e, ephemeral=True)


    @app_commands.command(name="invite-recount",
                          description="招待リンクの累計作成数を、今ある招待リンクから数え直します（過去分はリセット）")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def invite_recount(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("「サーバーの管理」権限が必要です。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            counts = await self.recount(interaction.guild)
        except ValueError as e:
            await interaction.followup.send(str(e))
            return
        top = "\n".join(f"・<@{u}>: {n}回" for u, n in sorted(counts.items(), key=lambda x: -x[1])[:15])
        await interaction.followup.send(
            f"✅ 数え直しました（今ある招待リンク {sum(counts.values())} 件）。\n{top or '招待リンクはありません。'}",
            allowed_mentions=discord.AllowedMentions.none())

    async def recount(self, guild: discord.Guild) -> dict[int, int]:
        """招待リンクの累計作成数を、今ある招待リンクから数え直す（ダッシュボードからも使う）。"""
        snap = await self._refresh(guild)
        if snap is None:
            raise ValueError("Botに「サーバーの管理」権限がないため、招待リンクを取得できません。")
        removed = 0
        if getattr(self.bot, "kv_ready", lambda: False)():
            try:
                for pattern in (f"invcount:{guild.id}:*", f"invmeta:{guild.id}:*"):
                    cursor = 0
                    while True:
                        cursor, keys = await self.bot.kv("scan", cursor, pattern, 500)
                        for k in keys:
                            await self.bot.kv("delete", k)
                            removed += 1
                        if not cursor:
                            break
            except Exception as e:
                raise ValueError(f"Redisの操作に失敗しました（{type(e).__name__}）。")
        for k in [k for k in kv._mem if k.startswith((f"invcount:{guild.id}:", f"invmeta:{guild.id}:"))]:
            kv._mem.pop(k, None)
        self._cache[guild.id] = snap
        await self._seed(guild, snap)
        counts: dict[int, int] = {}
        for c, d in snap.items():
            if c != "__vanity__" and d.get("inviter"):
                counts[d["inviter"]] = counts.get(d["inviter"], 0) + 1
        return counts


async def setup(bot: commands.Bot):
    await bot.add_cog(InviteTracker(bot))
