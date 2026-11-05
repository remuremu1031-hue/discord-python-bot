"""自動モデレーション。連続投稿スパムの検知・削除、招待リンクの自動削除、
Discord標準AutoModの発動ログ。どちらの自動削除も、初期状態は「オフ」（/automod で有効化）。
設定は Redis（automod:<guild_id> = {"spam": bool, "invite": bool}）。

・招待リンクの検知には MESSAGE CONTENT INTENT（ENABLE_MESSAGE_CONTENT_INTENT=1）が必要
・メッセージの削除には、Botに「メッセージの管理」権限が必要
・AutoModの発動ログを受け取るには、Botに「サーバーの管理」権限が必要
・管理者・「メッセージの管理」権限のメンバーは、自動削除の対象外"""
import re
import time
from collections import deque
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .common import FEATURE_UNAVAILABLE, get_json, is_admin, log_event, reply, safe_error, set_json, trim

SPAM_COUNT = 5             # この回数以上の投稿が
SPAM_WINDOW = 6.0          # この秒数以内に続いたらスパムとみなす
SPAM_TIMEOUT_MINUTES = 5   # スパムを検知したときのタイムアウト（分）
CFG_TTL_OK, CFG_TTL_FAIL = 300, 60  # 設定のキャッシュ時間（秒）

INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+", re.IGNORECASE)
DEFAULT_CFG = {"spam": False, "invite": False}


def key(guild_id: int) -> str:
    return f"automod:{guild_id}"


class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cfg: dict[int, tuple[float, dict]] = {}  # サーバー → (取得時刻, 設定)
        self._hist: dict[tuple[int, int], deque] = {}  # (サーバー, ユーザー) → (時刻, メッセージ)

    async def cog_load(self):
        self.prune.start()

    async def cog_unload(self):
        self.prune.cancel()

    @tasks.loop(minutes=5)
    async def prune(self):
        """古い投稿履歴を捨てて、メモリが増え続けないようにする。"""
        now = time.monotonic()
        for k in [k for k, dq in self._hist.items() if not dq or now - dq[-1][0] > SPAM_WINDOW]:
            self._hist.pop(k, None)

    async def _get_cfg(self, guild_id: int) -> dict:
        cached = self._cfg.get(guild_id)
        if cached and time.monotonic() - cached[0] < (CFG_TTL_OK if cached[1] is not None else CFG_TTL_FAIL):
            return cached[1] or DEFAULT_CFG
        try:
            data = await get_json(key(guild_id))
            cfg = {**DEFAULT_CFG, **(data or {})}
            self._cfg[guild_id] = (time.monotonic(), cfg)
            return cfg
        except Exception as e:
            if not isinstance(e, RuntimeError):
                print(f"自動モデレーション設定の取得エラー: {safe_error(e)}")
            self._cfg[guild_id] = (time.monotonic(), None)  # 失敗は短時間だけ覚えて、連続アクセスを避ける
            return DEFAULT_CFG

    group = app_commands.Group(name="automod", description="自動モデレーションを設定します",
                               guild_only=True,
                               default_permissions=discord.Permissions(administrator=True))

    async def _toggle(self, interaction: discord.Interaction, name: str, enabled: bool, label: str):
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        gid = interaction.guild.id
        try:
            cfg = {**DEFAULT_CFG, **((await get_json(key(gid))) or {})}
            cfg[name] = enabled
            await set_json(key(gid), cfg)
        except Exception as e:
            print(f"自動モデレーション設定の保存エラー: {safe_error(e)}")
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        self._cfg[gid] = (time.monotonic(), cfg)
        extra = ""
        if enabled and name == "invite" and not self.bot.intents.message_content:
            extra = "\n⚠️ 運営者が MESSAGE CONTENT INTENT を有効にしていないため、招待リンクは検知できません。"
        await reply(interaction, f"{'✅ 有効にしました' if enabled else '🔕 無効にしました'}: {label}{extra}")

    @group.command(name="spam", description="連続投稿スパムの自動削除をオン/オフします")
    @app_commands.describe(enabled="オンにするか")
    async def spam(self, interaction: discord.Interaction, enabled: bool):
        await self._toggle(interaction, "spam", enabled, "連続投稿スパムの自動削除")

    @group.command(name="invite", description="招待リンクの自動削除をオン/オフします")
    @app_commands.describe(enabled="オンにするか")
    async def invite(self, interaction: discord.Interaction, enabled: bool):
        await self._toggle(interaction, "invite", enabled, "招待リンクの自動削除")

    @group.command(name="status", description="自動モデレーションの設定を表示します")
    async def status(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        cfg = await self._get_cfg(interaction.guild.id)
        on = lambda b: "🟢 オン" if b else "⚪ オフ"  # noqa: E731
        embed = discord.Embed(title="🛡️ 自動モデレーション", color=0x5865F2)
        embed.add_field(name="連続投稿スパム", value=f"{on(cfg['spam'])}\n{SPAM_COUNT}回/{SPAM_WINDOW:.0f}秒で検知", inline=True)
        embed.add_field(name="招待リンク", value=on(cfg["invite"]), inline=True)
        embed.set_footer(text="管理者・「メッセージの管理」権限のメンバーは対象外")
        await reply(interaction, embed=embed)

    # --- メッセージの監視 ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or not isinstance(message.author, discord.Member):
            return
        cfg = await self._get_cfg(message.guild.id)
        if not (cfg["spam"] or cfg["invite"]):
            return
        perms = message.author.guild_permissions
        if perms.administrator or perms.manage_messages or perms.manage_guild:
            return  # 運営は対象外
        if cfg["invite"] and message.content and INVITE_RE.search(message.content):
            await self._remove(message, "招待リンク")
            return
        if cfg["spam"]:
            await self._check_spam(message)

    async def _remove(self, message: discord.Message, why: str) -> bool:
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False
        embed = discord.Embed(title=f"🛡️ 自動削除: {why}", color=0xF59E0B)
        embed.add_field(name="送信者", value=f"{message.author.mention}（`{message.author.id}`）", inline=True)
        embed.add_field(name="チャンネル", value=message.channel.mention, inline=True)
        if message.content:
            embed.add_field(name="内容", value=trim(message.content, 500), inline=False)
        await log_event(self.bot, message.guild, embed)
        try:
            await message.channel.send(f"{message.author.mention} {why}は、このサーバーでは送信できません。",
                                       delete_after=8,
                                       allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass
        return True

    async def _check_spam(self, message: discord.Message):
        k = (message.guild.id, message.author.id)
        now = time.monotonic()
        dq = self._hist.setdefault(k, deque())
        dq.append((now, message))
        while dq and now - dq[0][0] > SPAM_WINDOW:
            dq.popleft()
        if len(dq) < SPAM_COUNT:
            return
        msgs = [m for _, m in dq]
        dq.clear()
        deleted = 0
        for m in msgs:
            try:
                await m.delete()
                deleted += 1
            except discord.HTTPException:
                pass
        timed_out = False
        try:
            await message.author.timeout(timedelta(minutes=SPAM_TIMEOUT_MINUTES),
                                         reason="自動モデレーション: 連続投稿スパム")
            timed_out = True
        except discord.HTTPException:
            pass
        embed = discord.Embed(title="🛡️ 自動削除: 連続投稿スパム", color=0xEF4444)
        embed.add_field(name="送信者", value=f"{message.author.mention}（`{message.author.id}`）", inline=True)
        embed.add_field(name="チャンネル", value=message.channel.mention, inline=True)
        embed.add_field(name="対応", value=f"削除 {deleted} 件／タイムアウト "
                                          f"{SPAM_TIMEOUT_MINUTES}分{'' if timed_out else '（失敗）'}", inline=False)
        await log_event(self.bot, message.guild, embed)
        try:
            await message.channel.send(f"{message.author.mention} 連続投稿を検知しました。少し間をあけてください。",
                                       delete_after=8,
                                       allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass

    # --- Discord標準AutoModの発動ログ ---
    @commands.Cog.listener()
    async def on_automod_action(self, execution: discord.AutoModAction):
        guild = getattr(execution, "guild", None)
        if guild is None:
            return
        try:
            uid = getattr(execution, "user_id", None) or getattr(execution, "member_id", None)
            embed = discord.Embed(title="🛡️ Discord AutoMod が発動しました", color=0xF59E0B)
            rule = getattr(execution, "rule_trigger_type", None)
            action = getattr(getattr(execution, "action", None), "type", None)
            embed.add_field(name="ルール種別", value=getattr(rule, "name", str(rule)), inline=True)
            embed.add_field(name="対応", value=getattr(action, "name", str(action)), inline=True)
            if uid:
                embed.add_field(name="対象", value=f"<@{uid}>（`{uid}`）", inline=True)
            cid = getattr(execution, "channel_id", None)
            if cid:
                embed.add_field(name="チャンネル", value=f"<#{cid}>", inline=True)
            kw = getattr(execution, "matched_keyword", None)
            if kw:
                embed.add_field(name="一致したキーワード", value=trim(kw, 200), inline=True)
            content = getattr(execution, "content", None)
            if content:
                embed.add_field(name="内容", value=trim(content, 500), inline=False)
            await log_event(self.bot, guild, embed)
        except Exception as e:
            print(f"AutoModログの処理エラー: {safe_error(e)}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
