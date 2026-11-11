"""レベル・ランク機能。メッセージ送信とVC参加で経験値（XP）が貯まり、レベルアップする。
・メッセージ: 1分に1回まで 15〜25XP（短すぎる発言・連投は対象外）
・VC: 1分ごとに 10XP（2人以上いるVCで、ミュート/スピーカーミュート・AFKチャンネルでないとき）
・レベルに応じてロール（ランク）を自動付与。/rank で進捗、/leaderboard でランキング
必要XP: 次のレベルまで 5×L² + 50×L + 100（L=今のレベル）
Redis キー（bot.kv を使用。未設定ならメモリ＝再起動で消える）:
  lvl:<サーバー>        ソート済み集合  ユーザーID → 累計XP（ランキング用）
  lvlmsg:<サーバー>     ハッシュ        ユーザーID → メッセージ数
  lvlvc:<サーバー>      ハッシュ        ユーザーID → VC参加分数
設定（ON/OFF・通知先・ランクロール・倍率）は bot.store_load("level_config")"""
from __future__ import annotations

import asyncio
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

MSG_COOLDOWN = 60
MSG_XP = (15, 25)
VC_XP_PER_MIN = 10
MIN_MSG_LEN = 3


def xp_for_next(level: int) -> int:
    return 5 * level * level + 50 * level + 100


def level_from_xp(xp: int) -> tuple[int, int, int]:
    """累計XP → (レベル, 今のレベル内のXP, 次のレベルまでに必要なXP)"""
    level = 0
    while xp >= xp_for_next(level):
        xp -= xp_for_next(level)
        level += 1
    return level, xp, xp_for_next(level)


def total_xp_for(level: int) -> int:
    return sum(xp_for_next(i) for i in range(level))


def bar(cur: int, need: int, width: int = 14) -> str:
    filled = int(width * cur / need) if need else width
    return "█" * filled + "░" * (width - filled)


class Store:
    """Redis（bot.kv）を使い、失敗・未設定のときはメモリで動かす。"""

    def __init__(self, bot):
        self.bot = bot
        self.z: dict[str, dict[str, float]] = {}
        self.h: dict[str, dict[str, int]] = {}

    async def _do(self, op, *args):
        if getattr(self.bot, "kv_ready", lambda: False)():
            try:
                return True, await self.bot.kv(op, *args)
            except Exception as e:
                print(f"レベル機能: Redisの操作に失敗（{op}）: {type(e).__name__}")
        return False, None

    async def add_xp(self, gid: int, uid: int, amount: int) -> int:
        ok, v = await self._do("zincrby", f"lvl:{gid}", amount, str(uid))
        if ok:
            return int(float(v))
        d = self.z.setdefault(f"lvl:{gid}", {})
        d[str(uid)] = d.get(str(uid), 0) + amount
        return int(d[str(uid)])

    async def set_xp(self, gid: int, uid: int, xp: int) -> None:
        ok, _ = await self._do("zadd", f"lvl:{gid}", {str(uid): xp})
        if not ok:
            self.z.setdefault(f"lvl:{gid}", {})[str(uid)] = xp

    async def remove(self, gid: int, uid: int) -> None:
        for k, kind in ((f"lvl:{gid}", "z"), (f"lvlmsg:{gid}", "h"), (f"lvlvc:{gid}", "h")):
            ok, _ = await self._do("zrem" if kind == "z" else "hdel", k, str(uid))
            if not ok:
                (self.z if kind == "z" else self.h).get(k, {}).pop(str(uid), None)

    async def xp(self, gid: int, uid: int) -> int:
        ok, v = await self._do("zscore", f"lvl:{gid}", str(uid))
        if ok:
            return int(float(v or 0))
        return int(self.z.get(f"lvl:{gid}", {}).get(str(uid), 0))

    async def rank(self, gid: int, uid: int) -> tuple[int | None, int]:
        ok, r = await self._do("zrevrank", f"lvl:{gid}", str(uid))
        ok2, n = await self._do("zcard", f"lvl:{gid}")
        if ok and ok2:
            return (None if r is None else int(r) + 1), int(n or 0)
        d = self.z.get(f"lvl:{gid}", {})
        order = sorted(d, key=lambda k: -d[k])
        return (order.index(str(uid)) + 1 if str(uid) in d else None), len(d)

    async def top(self, gid: int, n: int) -> list[tuple[int, int]]:
        ok, v = await self._do("zrevrange", f"lvl:{gid}", 0, n - 1, "withscores")
        if ok:
            return [(int(u), int(float(s))) for u, s in v]
        d = self.z.get(f"lvl:{gid}", {})
        return [(int(u), int(s)) for u, s in sorted(d.items(), key=lambda kv: -kv[1])[:n]]

    async def hincr(self, key: str, uid: int, amount: int = 1) -> None:
        ok, _ = await self._do("hincrby", key, str(uid), amount)
        if not ok:
            d = self.h.setdefault(key, {})
            d[str(uid)] = d.get(str(uid), 0) + amount

    async def hget(self, key: str, uid: int) -> int:
        ok, v = await self._do("hget", key, str(uid))
        return int(v or 0) if ok else int(self.h.get(key, {}).get(str(uid), 0))


class Levels(commands.Cog):
    level = app_commands.Group(name="level", description="レベル機能の設定（管理者向け）", guild_only=True,
                               default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = Store(bot)
        self._last_msg: dict[tuple[int, int], float] = {}
        self._last_text: dict[tuple[int, int], str] = {}

    async def cog_load(self):
        self.vc_loop.start()

    async def cog_unload(self):
        self.vc_loop.cancel()

    # ---------- 設定 ----------
    def cfg(self, gid: int) -> dict:
        c = self.bot.store_load("level_config").get(str(gid), {})
        c.setdefault("enabled", True)
        c.setdefault("announce", "same")  # same（発言したチャンネル）/ チャンネルID / off
        c.setdefault("roles", {})          # "レベル" → ロールID
        c.setdefault("stack", True)        # True: 下位ランクも残す / False: 最上位だけ
        c.setdefault("multiplier", 1.0)
        c.setdefault("no_xp_channels", [])
        return c

    def save_cfg(self, gid: int, c: dict):
        data = self.bot.store_load("level_config")
        data[str(gid)] = c
        self.bot.store_save("level_config", data)

    def _suspended(self, gid: int) -> bool:
        return getattr(self.bot, "is_suspended", lambda _: False)(gid)

    # ---------- XP付与とレベルアップ ----------
    async def grant(self, guild: discord.Guild, uid: int, amount: int, *,
                    channel: discord.abc.Messageable | None = None, member: discord.Member | None = None):
        c = self.cfg(guild.id)
        amount = max(1, int(amount * float(c.get("multiplier", 1.0))))
        before = await self.store.xp(guild.id, uid)
        after = await self.store.add_xp(guild.id, uid, amount)
        lv_before, lv_after = level_from_xp(before)[0], level_from_xp(after)[0]
        if lv_after > lv_before:
            await self.on_level_up(guild, uid, lv_after, channel, member, c)

    async def on_level_up(self, guild, uid, new_level, channel, member, c):
        member = member or guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:
                return
        got_role = await self.apply_rank_roles(member, new_level, c)
        e = discord.Embed(title="🎉 レベルアップ！", color=0xF59E0B,
                          description=f"{member.mention} さんが **レベル {new_level}** になりました！")
        if got_role:
            e.add_field(name="🏅 新しいランク", value=got_role.mention)
        nxt = self.next_reward(c, new_level)
        if nxt:
            e.set_footer(text=f"次のランクはレベル {nxt[0]}")
        dest = None
        if c["announce"] == "same":
            dest = channel
        elif str(c["announce"]).isdigit():
            dest = guild.get_channel(int(c["announce"]))
        if dest is not None:
            try:
                await dest.send(embed=e, allowed_mentions=discord.AllowedMentions(users=[member]))
            except discord.HTTPException:
                pass

    @staticmethod
    def next_reward(c: dict, level: int) -> tuple[int, int] | None:
        ups = sorted((int(lv), rid) for lv, rid in c["roles"].items() if int(lv) > level)
        return ups[0] if ups else None

    async def apply_rank_roles(self, member: discord.Member, level: int, c: dict) -> discord.Role | None:
        guild = member.guild
        reached = sorted((int(lv), guild.get_role(int(rid))) for lv, rid in c["roles"].items() if int(lv) <= level)
        reached = [(lv, r) for lv, r in reached if r and r < guild.me.top_role and not r.managed]
        if not reached:
            return None
        top = reached[-1][1]
        want = {r for _, r in reached} if c.get("stack", True) else {top}
        all_rank = {guild.get_role(int(rid)) for rid in c["roles"].values()} - {None}
        add = [r for r in want if r not in member.roles]
        rem = [r for r in all_rank - want if r in member.roles and r < guild.me.top_role]
        try:
            if add:
                await member.add_roles(*add, reason=f"レベル {level} 到達")
            if rem:
                await member.remove_roles(*rem, reason="ランクの更新")
        except discord.HTTPException as e:
            print(f"ランクロールの付与に失敗: {type(e).__name__}")
        return top if top in add else None

    # ---------- メッセージ ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        c = self.cfg(message.guild.id)
        if not c["enabled"] or message.channel.id in c["no_xp_channels"]:
            return
        text = (message.content or "").strip()
        if len(text) < MIN_MSG_LEN and not message.attachments:
            return
        key = (message.guild.id, message.author.id)
        now = time.time()
        if now - self._last_msg.get(key, 0) < MSG_COOLDOWN or self._last_text.get(key) == text:
            return  # 連投・同じ文の繰り返しは数えない
        self._last_msg[key] = now
        self._last_text[key] = text
        await self.store.hincr(f"lvlmsg:{message.guild.id}", message.author.id)
        member = message.author if isinstance(message.author, discord.Member) else None
        await self.grant(message.guild, message.author.id, random.randint(*MSG_XP),
                         channel=message.channel, member=member)

    # ---------- VC ----------
    @tasks.loop(minutes=1)
    async def vc_loop(self):
        for guild in list(self.bot.guilds):
            if self._suspended(guild.id):
                continue
            c = self.cfg(guild.id)
            if not c["enabled"]:
                continue
            for ch in list(guild.voice_channels) + list(guild.stage_channels):
                if guild.afk_channel and ch.id == guild.afk_channel.id:
                    continue
                if ch.id in c["no_xp_channels"]:
                    continue
                states = getattr(ch, "voice_states", {}) or {}
                humans = {}
                for uid, vs in states.items():
                    m = guild.get_member(uid)
                    if m is not None and m.bot:
                        continue
                    humans[uid] = vs
                if len(humans) < 2:
                    continue  # 1人だけのVCは対象外（放置対策）
                for uid, vs in humans.items():
                    if vs.self_mute or vs.self_deaf or vs.mute or vs.deaf or vs.afk:
                        continue
                    await self.store.hincr(f"lvlvc:{guild.id}", uid)
                    await self.grant(guild, uid, VC_XP_PER_MIN)
            await asyncio.sleep(0)

    @vc_loop.before_loop
    async def _before_vc(self):
        await self.bot.wait_until_ready()

    # ---------- 進捗表示 ----------
    @app_commands.command(name="rank", description="レベル・ランクの進捗を表示します")
    @app_commands.describe(user="表示するユーザー（省略で自分）")
    @app_commands.guild_only()
    async def rank_cmd(self, interaction: discord.Interaction, user: discord.Member | None = None):
        user = user or interaction.user
        g = interaction.guild
        c = self.cfg(g.id)
        xp = await self.store.xp(g.id, user.id)
        lv, cur, need = level_from_xp(xp)
        pos, total = await self.store.rank(g.id, user.id)
        msgs = await self.store.hget(f"lvlmsg:{g.id}", user.id)
        vc = await self.store.hget(f"lvlvc:{g.id}", user.id)
        reached = [(int(l), r) for l, r in c["roles"].items() if int(l) <= lv]
        now_rank = g.get_role(max(reached)[1]) if reached else None
        nxt = self.next_reward(c, lv)
        e = discord.Embed(title=f"📊 {user.display_name} のレベル", color=0x5865F2)
        e.set_thumbnail(url=user.display_avatar.url)
        e.add_field(name="レベル", value=f"**{lv}**", inline=True)
        e.add_field(name="サーバー内順位", value=f"**{pos}** 位 / {total}人" if pos else "-", inline=True)
        e.add_field(name="累計XP", value=f"{xp:,}", inline=True)
        e.add_field(name=f"次のレベルまで（{cur:,} / {need:,} XP）",
                    value=f"`{bar(cur, need)}` {int(cur * 100 / need)}%\nあと **{need - cur:,} XP**", inline=False)
        e.add_field(name="🏅 現在のランク", value=now_rank.mention if now_rank else "なし", inline=True)
        if nxt:
            r = g.get_role(int(nxt[1]))
            remain = total_xp_for(nxt[0]) - xp
            e.add_field(name="🎯 次のランク", value=f"{r.mention if r else '（削除済み）'}（レベル {nxt[0]}・あと {remain:,} XP）",
                        inline=True)
        e.add_field(name="活動", value=f"💬 メッセージ {msgs:,} 回 ／ 🔊 VC {vc // 60}時間{vc % 60}分", inline=False)
        e.set_footer(text=f"メッセージ: 1分ごとに{MSG_XP[0]}〜{MSG_XP[1]}XP ／ VC: 1分ごとに{VC_XP_PER_MIN}XP"
                          + (f" ／ 倍率 ×{c['multiplier']}" if c["multiplier"] != 1 else ""))
        await interaction.response.send_message(embed=e, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="leaderboard", description="サーバー内のレベルランキングを表示します")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        top = await self.store.top(interaction.guild_id, 15)
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for i, (uid, xp) in enumerate(top, 1):
            lv = level_from_xp(xp)[0]
            lines.append(f"{medals.get(i, f'`{i}.`')} <@{uid}> — レベル **{lv}**（{xp:,} XP）")
        e = discord.Embed(title=f"🏆 {interaction.guild.name} のランキング", color=0xF59E0B,
                          description="\n".join(lines) or "まだ記録がありません。")
        pos, total = await self.store.rank(interaction.guild_id, interaction.user.id)
        if pos:
            e.set_footer(text=f"あなたの順位: {pos}位 / {total}人")
        await interaction.response.send_message(embed=e, allowed_mentions=discord.AllowedMentions.none())

    # ---------- 管理者向け設定 ----------
    @level.command(name="settings", description="レベル機能のON/OFF・通知先・XP倍率・ランクの付け方を設定します")
    @app_commands.describe(enabled="レベル機能を有効にする", announce="レベルアップの通知先",
                           announce_channel="通知先を「指定チャンネル」にした場合のチャンネル",
                           multiplier="XPの倍率（0.1〜5.0）", stack_roles="下位のランクロールも残す（オフで最上位だけ）")
    @app_commands.choices(announce=[app_commands.Choice(name="発言したチャンネル", value="same"),
                                    app_commands.Choice(name="指定チャンネル", value="channel"),
                                    app_commands.Choice(name="通知しない", value="off")])
    async def settings(self, interaction: discord.Interaction, enabled: bool | None = None, announce: str | None = None,
                       announce_channel: discord.TextChannel | None = None,
                       multiplier: app_commands.Range[float, 0.1, 5.0] | None = None, stack_roles: bool | None = None):
        c = self.cfg(interaction.guild_id)
        if enabled is not None:
            c["enabled"] = enabled
        if announce == "channel":
            if announce_channel is None:
                await interaction.response.send_message("`announce_channel` を指定してください。", ephemeral=True)
                return
            c["announce"] = str(announce_channel.id)
        elif announce:
            c["announce"] = announce
        if multiplier is not None:
            c["multiplier"] = round(float(multiplier), 2)
        if stack_roles is not None:
            c["stack"] = stack_roles
        self.save_cfg(interaction.guild_id, c)
        dest = {"same": "発言したチャンネル", "off": "通知しない"}.get(c["announce"], f"<#{c['announce']}>")
        await interaction.response.send_message(
            f"✅ レベル機能: {'有効' if c['enabled'] else '無効'} ／ 通知: {dest} ／ 倍率: ×{c['multiplier']} ／ "
            f"ランクロール: {'下位も残す' if c['stack'] else '最上位だけ'}", ephemeral=True)

    @level.command(name="role", description="レベル到達で付与するランクロールを追加・削除・一覧表示します")
    @app_commands.describe(action="操作", level="到達レベル", role="付与するロール")
    @app_commands.choices(action=[app_commands.Choice(name="追加", value="add"), app_commands.Choice(name="削除", value="remove"),
                                  app_commands.Choice(name="一覧", value="list")])
    async def role(self, interaction: discord.Interaction, action: str,
                   level: app_commands.Range[int, 1, 500] | None = None, role: discord.Role | None = None):
        c = self.cfg(interaction.guild_id)
        if action == "list":
            lines = [f"・レベル **{lv}** → <@&{rid}>" for lv, rid in sorted(c["roles"].items(), key=lambda kv: int(kv[0]))]
            await interaction.response.send_message("\n".join(lines) or "ランクロールは未設定です。", ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
            return
        if level is None:
            await interaction.response.send_message("`level` を指定してください。", ephemeral=True)
            return
        if action == "add":
            if role is None:
                await interaction.response.send_message("`role` を指定してください。", ephemeral=True)
                return
            if role.is_default() or role.managed or role >= interaction.guild.me.top_role or role.permissions.administrator:
                await interaction.response.send_message(
                    "そのロールは使えません（@everyone・連携ロール・管理者権限つき・Botより上のロール）。", ephemeral=True)
                return
            c["roles"][str(level)] = role.id
        else:
            c["roles"].pop(str(level), None)
        self.save_cfg(interaction.guild_id, c)
        await interaction.response.send_message(
            f"✅ レベル {level} のランクロールを{'設定' if action == 'add' else '削除'}しました。", ephemeral=True)

    @level.command(name="no-xp", description="XPが貯まらないチャンネル（VC含む）を追加・削除します")
    @app_commands.describe(action="追加 / 削除", channel="対象チャンネル")
    @app_commands.choices(action=[app_commands.Choice(name="追加", value="add"), app_commands.Choice(name="削除", value="remove")])
    async def no_xp(self, interaction: discord.Interaction, action: str,
                    channel: discord.TextChannel | discord.VoiceChannel | discord.StageChannel):
        c = self.cfg(interaction.guild_id)
        s = set(c["no_xp_channels"])
        (s.add if action == "add" else s.discard)(channel.id)
        c["no_xp_channels"] = list(s)
        self.save_cfg(interaction.guild_id, c)
        await interaction.response.send_message(
            f"✅ XP対象外: {' '.join(f'<#{x}>' for x in s) or 'なし'}", ephemeral=True)

    async def set_member_level(self, member: discord.Member, level: int) -> None:
        """ユーザーのレベルを変更する（ダッシュボードからも使う）。0でリセット。"""
        if level == 0:
            await self.store.remove(member.guild.id, member.id)
        else:
            await self.store.set_xp(member.guild.id, member.id, total_xp_for(level))
        await self.apply_rank_roles(member, level, self.cfg(member.guild.id))

    @level.command(name="set", description="ユーザーのレベルを変更します（XPはそのレベルの最初の値になります）")
    @app_commands.describe(user="対象ユーザー", level="設定するレベル（0でリセット）")
    async def set_level(self, interaction: discord.Interaction, user: discord.Member,
                        level: app_commands.Range[int, 0, 500]):
        if level == 0:
            await self.store.remove(interaction.guild_id, user.id)
        else:
            await self.store.set_xp(interaction.guild_id, user.id, total_xp_for(level))
        await self.apply_rank_roles(user, level, self.cfg(interaction.guild_id))
        await interaction.response.send_message(
            f"✅ {user.mention} を{'リセット' if level == 0 else f'レベル {level} に設定'}しました。",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(Levels(bot))
