"""各種イベントログ。メッセージの削除・編集、メンバーの入退室、ボイスチャンネルの移動を
指定チャンネルへEmbedで通知する。設定は Redis（eventlog:<guild_id> = チャンネルID）。

・メッセージの内容を表示するには MESSAGE CONTENT INTENT（ENABLE_MESSAGE_CONTENT_INTENT=1）が必要
・入退室の通知には SERVER MEMBERS INTENT（ENABLE_MEMBERS_INTENT=1）が必要
・他のCogからは cogs.common.log_event() 経由で、同じチャンネルに通知できる"""
import discord
from discord import app_commands
from discord.ext import commands

from .common import FEATURE_UNAVAILABLE, is_admin, rdo, reply, safe_error, trim


def key(guild_id: int) -> str:
    return f"eventlog:{guild_id}"


class EventLogs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: dict[int, int | None] = {}  # サーバー → ログチャンネルID（なければNone）

    # --- 送信先 ---
    async def _channel_id(self, guild_id: int) -> int | None:
        if guild_id in self._cache:
            return self._cache[guild_id]
        try:
            raw = await rdo("get", key(guild_id))
        except Exception as e:
            if not isinstance(e, RuntimeError):
                print(f"イベントログ設定の取得エラー: {safe_error(e)}")
            return None  # 失敗時は保持しない
        self._cache[guild_id] = int(raw) if raw and str(raw).isdigit() else None
        return self._cache[guild_id]

    async def send(self, guild: discord.Guild, embed: discord.Embed) -> None:
        cid = await self._channel_id(guild.id)
        if not cid:
            return
        ch = guild.get_channel(cid)
        if not isinstance(ch, discord.TextChannel):
            return
        p = ch.permissions_for(guild.me)
        if not (p.view_channel and p.send_messages and p.embed_links):
            return
        try:
            await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            print(f"イベントログの送信エラー: {safe_error(e)}")

    group = app_commands.Group(name="eventlog", description="イベントログの送信先を設定します",
                               guild_only=True,
                               default_permissions=discord.Permissions(administrator=True))

    @group.command(name="set", description="イベントログを送信するチャンネルを設定します")
    @app_commands.describe(channel="ログを送信するチャンネル")
    async def set_(self, interaction: discord.Interaction, channel: discord.TextChannel):
        guild = interaction.guild
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        p = channel.permissions_for(guild.me)
        if not (p.view_channel and p.send_messages and p.embed_links):
            await reply(interaction, f"{channel.mention} でBotに「チャンネルを見る・メッセージを送信・埋め込みリンク」の権限がありません。")
            return
        try:
            await rdo("set", key(guild.id), str(channel.id))
        except Exception as e:
            print(f"イベントログ設定の保存エラー: {safe_error(e)}")
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        self._cache[guild.id] = channel.id
        await reply(interaction, f"✅ イベントログを {channel.mention} に送信します。")

    @group.command(name="off", description="イベントログの送信を止めます")
    async def off(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        try:
            await rdo("delete", key(interaction.guild.id))
        except Exception as e:
            print(f"イベントログ設定の削除エラー: {safe_error(e)}")
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        self._cache[interaction.guild.id] = None
        await reply(interaction, "🔕 イベントログの送信を止めました。")

    # --- メッセージ ---
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.guild_id is None:
            return
        cid = await self._channel_id(payload.guild_id)
        if not cid or payload.channel_id == cid:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        msg = payload.cached_message
        if msg is not None and msg.author.bot:
            return
        embed = discord.Embed(title="🗑️ メッセージが削除されました", color=0xEF4444)
        embed.add_field(name="チャンネル", value=f"<#{payload.channel_id}>", inline=True)
        if msg is not None:
            embed.add_field(name="送信者", value=f"{msg.author.mention}（`{msg.author.id}`）", inline=True)
            embed.add_field(name="内容", value=trim(msg.content, 1000) or "（本文なし／取得できません）", inline=False)
            if msg.attachments:
                embed.add_field(name="添付ファイル", value=f"{len(msg.attachments)} 件", inline=True)
        else:
            embed.description = "古いメッセージのため、送信者と内容は取得できません。"
            embed.add_field(name="メッセージID", value=f"`{payload.message_id}`", inline=True)
        await self.send(guild, embed)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        embed = discord.Embed(title="🗑️ メッセージが一括削除されました", color=0xEF4444,
                              description=f"<#{payload.channel_id}> で {len(payload.message_ids)} 件")
        await self.send(guild, embed)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.guild_id is None:
            return
        cid = await self._channel_id(payload.guild_id)
        if not cid or payload.channel_id == cid:
            return
        guild = self.bot.get_guild(payload.guild_id)
        data = payload.data or {}
        after = data.get("content")
        before_msg = payload.cached_message
        author = data.get("author") or {}
        if guild is None or after is None or author.get("bot"):
            return  # 埋め込みの更新など、本文の変更でないもの／Botの発言は記録しない
        if before_msg is not None and before_msg.content == after:
            return
        embed = discord.Embed(title="✏️ メッセージが編集されました", color=0xF59E0B)
        embed.add_field(name="チャンネル", value=f"<#{payload.channel_id}>", inline=True)
        uid = (before_msg.author.id if before_msg else author.get("id"))
        if uid:
            embed.add_field(name="送信者", value=f"<@{uid}>（`{uid}`）", inline=True)
        embed.add_field(name="編集前",
                        value=trim(before_msg.content, 900) if before_msg else "（取得できません）",
                        inline=False)
        embed.add_field(name="編集後", value=trim(after, 900) or "（本文なし）", inline=False)
        gid, mid = payload.guild_id, payload.message_id
        embed.add_field(name="リンク",
                        value=f"[メッセージへ](https://discord.com/channels/{gid}/{payload.channel_id}/{mid})",
                        inline=False)
        await self.send(guild, embed)

    # --- メンバーの入退室 ---
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        embed = discord.Embed(title="📥 メンバーが参加しました", color=0x22C55E,
                              description=f"{member.mention}（`{member.id}`）")
        embed.add_field(name="アカウント作成", value=discord.utils.format_dt(member.created_at, "R"))
        embed.set_thumbnail(url=member.display_avatar.url)
        await self.send(member.guild, embed)

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        user = payload.user
        embed = discord.Embed(title="📤 メンバーが退出しました", color=0x6B7280,
                              description=f"{user.mention}（`{user.id}`）")
        await self.send(guild, embed)

    # --- ボイスチャンネル ---
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        if before.channel == after.channel:
            return  # ミュート切り替えなど、チャンネル移動でないもの
        if before.channel is None:
            title, color, text = "🔊 ボイスに参加", 0x22C55E, f"{member.mention} → {after.channel.mention}"
        elif after.channel is None:
            title, color, text = "🔇 ボイスから退出", 0x6B7280, f"{member.mention} ← {before.channel.mention}"
        else:
            title, color = "🔀 ボイスを移動", 0x3B82F6
            text = f"{member.mention}\n{before.channel.mention} → {after.channel.mention}"
        await self.send(member.guild, discord.Embed(title=title, description=text, color=color))


async def setup(bot: commands.Bot):
    await bot.add_cog(EventLogs(bot))
