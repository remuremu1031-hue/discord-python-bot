"""モデレーションコマンド /timeout /untimeout /kick /ban /unban（管理者のみ）。
上位ロールの相手・オーナー・自分自身には実行できない。結果は全員に見える形で表示し、
対象へDMで通知（拒否されていれば無視）、イベントログにも記録する。"""
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from .common import dm_user, hierarchy_error, is_admin, log_event, reply, safe_error


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _guard(self, interaction: discord.Interaction, member: discord.Member | None = None) -> bool:
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return False
        if member is not None:
            err = hierarchy_error(interaction, member)
            if err:
                await reply(interaction, err)
                return False
        return True

    async def _done(self, interaction: discord.Interaction, title: str, target_text: str,
                    reason: str, color: int, extra: str = ""):
        embed = discord.Embed(title=title, color=color)
        embed.add_field(name="対象", value=target_text, inline=False)
        embed.add_field(name="理由", value=reason, inline=False)
        if extra:
            embed.add_field(name="詳細", value=extra, inline=False)
        await reply(interaction, embed=embed, ephemeral=False,
                    allowed_mentions=discord.AllowedMentions.none())
        log = embed.copy()
        log.add_field(name="実行者", value=interaction.user.mention, inline=True)
        await log_event(self.bot, interaction.guild, log)

    @app_commands.command(name="timeout", description="メンバーをタイムアウトします（管理者のみ）")
    @app_commands.describe(member="対象のメンバー", minutes="期間（分）。最大40320分＝28日", reason="理由")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def timeout(self, interaction: discord.Interaction, member: discord.Member,
                      minutes: app_commands.Range[int, 1, 40320],
                      reason: app_commands.Range[str, 1, 300] = "理由なし"):
        if not await self._guard(interaction, member):
            return
        await dm_user(member, discord.Embed(
            title=f"⏳ {interaction.guild.name} でタイムアウトされました", color=0xF59E0B,
            description=f"期間: {minutes}分\n理由: {reason}"))
        try:
            await member.timeout(timedelta(minutes=minutes), reason=f"{reason}（実行者: {interaction.user}）"[:500])
        except discord.Forbidden:
            await reply(interaction, "権限が足りません。Botに「メンバーをタイムアウト」権限が必要です。")
            return
        except discord.HTTPException as e:
            await reply(interaction, f"実行できませんでした（{safe_error(e)}）。")
            return
        await self._done(interaction, "⏳ タイムアウトしました", f"{member.mention}（`{member.id}`）",
                         reason, 0xF59E0B, f"期間: {minutes}分")

    @app_commands.command(name="untimeout", description="メンバーのタイムアウトを解除します（管理者のみ）")
    @app_commands.describe(member="対象のメンバー")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member):
        if not await self._guard(interaction, member):
            return
        try:
            await member.timeout(None, reason=f"タイムアウト解除（実行者: {interaction.user}）"[:500])
        except discord.HTTPException as e:
            await reply(interaction, f"実行できませんでした（{safe_error(e)}）。")
            return
        await self._done(interaction, "✅ タイムアウトを解除しました",
                         f"{member.mention}（`{member.id}`）", "—", 0x22C55E)

    @app_commands.command(name="kick", description="メンバーをキックします（管理者のみ）")
    @app_commands.describe(member="対象のメンバー", reason="理由")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member,
                   reason: app_commands.Range[str, 1, 300] = "理由なし"):
        if not await self._guard(interaction, member):
            return
        await dm_user(member, discord.Embed(
            title=f"👢 {interaction.guild.name} からキックされました", color=0xF59E0B,
            description=f"理由: {reason}"))
        try:
            await member.kick(reason=f"{reason}（実行者: {interaction.user}）"[:500])
        except discord.Forbidden:
            await reply(interaction, "権限が足りません。Botに「メンバーをキック」権限が必要です。")
            return
        except discord.HTTPException as e:
            await reply(interaction, f"実行できませんでした（{safe_error(e)}）。")
            return
        await self._done(interaction, "👢 キックしました", f"{member.mention}（`{member.id}`）",
                         reason, 0xF59E0B)

    @app_commands.command(name="ban", description="メンバーをBANします（管理者のみ）")
    @app_commands.describe(member="対象のメンバー", reason="理由", delete_days="さかのぼって削除するメッセージの日数（0〜7）")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member,
                  reason: app_commands.Range[str, 1, 300] = "理由なし",
                  delete_days: app_commands.Range[int, 0, 7] = 0):
        if not await self._guard(interaction, member):
            return
        await dm_user(member, discord.Embed(
            title=f"🔨 {interaction.guild.name} からBANされました", color=0xEF4444,
            description=f"理由: {reason}"))
        try:
            await interaction.guild.ban(member, reason=f"{reason}（実行者: {interaction.user}）"[:500],
                                        delete_message_seconds=delete_days * 86400)
        except discord.Forbidden:
            await reply(interaction, "権限が足りません。Botに「メンバーをBAN」権限が必要です。")
            return
        except discord.HTTPException as e:
            await reply(interaction, f"実行できませんでした（{safe_error(e)}）。")
            return
        await self._done(interaction, "🔨 BANしました", f"{member.mention}（`{member.id}`）",
                         reason, 0xEF4444, f"メッセージ削除: {delete_days}日分" if delete_days else "")

    @app_commands.command(name="unban", description="BANを解除します（管理者のみ）")
    @app_commands.describe(user_id="解除するユーザーID（数字）")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def unban(self, interaction: discord.Interaction, user_id: str):
        if not await self._guard(interaction):
            return
        raw = user_id.strip()
        if not raw.isdigit() or not (15 <= len(raw) <= 21):
            await reply(interaction, "ユーザーIDは数字（15〜21桁）で入力してください。")
            return
        try:
            await interaction.guild.unban(discord.Object(id=int(raw)),
                                          reason=f"BAN解除（実行者: {interaction.user}）"[:500])
        except discord.NotFound:
            await reply(interaction, "そのユーザーはBANされていません。")
            return
        except discord.HTTPException as e:
            await reply(interaction, f"実行できませんでした（{safe_error(e)}）。")
            return
        await self._done(interaction, "✅ BANを解除しました", f"<@{raw}>（`{raw}`）", "—", 0x22C55E)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
