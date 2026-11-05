"""警告 (Warn) システム。/warn /warnings /clearwarnings。
記録は Redis のリスト（warn:<guild_id>:<user_id>）に、最新100件まで保存する。"""
import json

import discord
from discord import app_commands
from discord.ext import commands

from .common import (FEATURE_UNAVAILABLE, dm_user, hierarchy_error, log_event, now_jst, rdo,
                     reply, safe_error, trim)

MAX_KEEP = 100  # 1人あたりの保存件数の上限


def key(guild_id: int, user_id: int) -> str:
    return f"warn:{guild_id}:{user_id}"


class Warns(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="warn", description="メンバーに警告を付与します（モデレーター用）")
    @app_commands.describe(member="警告するメンバー", reason="理由")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member,
                   reason: app_commands.Range[str, 1, 300]):
        guild = interaction.guild
        if not interaction.user.guild_permissions.moderate_members:
            await reply(interaction, "この操作には「メンバーをタイムアウト」権限が必要です。")
            return
        if member.bot:
            await reply(interaction, "Botには警告できません。")
            return
        err = hierarchy_error(interaction, member)
        if err:
            await reply(interaction, err)
            return

        entry = {"reason": reason, "by": interaction.user.id, "at": now_jst()}
        k = key(guild.id, member.id)
        try:
            await rdo("rpush", k, json.dumps(entry, ensure_ascii=False))
            await rdo("ltrim", k, -MAX_KEEP, -1)
            total = await rdo("llen", k)
        except Exception as e:
            print(f"警告の保存エラー: {safe_error(e)}")
            await reply(interaction, FEATURE_UNAVAILABLE)
            return

        dm = discord.Embed(title=f"⚠️ {guild.name} で警告を受けました", color=0xF59E0B,
                           description=f"理由: {reason}\n累計: {total} 回")
        dm_ok = await dm_user(member, dm)

        embed = discord.Embed(title="⚠️ 警告を付与しました", color=0xF59E0B)
        embed.add_field(name="対象", value=f"{member.mention}（`{member.id}`）", inline=False)
        embed.add_field(name="理由", value=reason, inline=False)
        embed.add_field(name="累計", value=f"{total} 回", inline=True)
        embed.add_field(name="DM通知", value="送信済み" if dm_ok else "送信できませんでした", inline=True)
        await reply(interaction, embed=embed, ephemeral=False,
                    allowed_mentions=discord.AllowedMentions.none())
        log = embed.copy()
        log.add_field(name="実行者", value=interaction.user.mention, inline=True)
        await log_event(self.bot, guild, log)

    @app_commands.command(name="warnings", description="メンバーの警告の履歴と累計回数を表示します")
    @app_commands.describe(member="確認するメンバー")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        if not interaction.user.guild_permissions.moderate_members:
            await reply(interaction, "この操作には「メンバーをタイムアウト」権限が必要です。")
            return
        try:
            rows = await rdo("lrange", key(interaction.guild.id, member.id), 0, -1)
        except Exception as e:
            print(f"警告の取得エラー: {safe_error(e)}")
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        embed = discord.Embed(title=f"⚠️ {member.display_name} の警告（累計 {len(rows)} 回）", color=0xF59E0B)
        if not rows:
            embed.description = "警告はありません。"
        for i, raw in enumerate(rows[-10:], start=max(1, len(rows) - 9)):  # 新しい10件
            try:
                e = json.loads(raw)
            except Exception:
                continue
            embed.add_field(name=f"#{i}  {e.get('at', '')}",
                            value=f"{trim(e.get('reason', ''), 200)}\n実行: <@{e.get('by', 0)}>", inline=False)
        await reply(interaction, embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="clearwarnings", description="メンバーの警告をすべて削除します（管理者のみ）")
    @app_commands.describe(member="警告を削除するメンバー")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        if not interaction.user.guild_permissions.administrator:
            await reply(interaction, "管理者のみ実行できます。")
            return
        try:
            n = await rdo("delete", key(interaction.guild.id, member.id))
        except Exception as e:
            print(f"警告の削除エラー: {safe_error(e)}")
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        await reply(interaction, f"✅ {member.mention} の警告を削除しました。" if n else
                    f"{member.mention} に警告はありません。", ephemeral=False,
                    allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(Warns(bot))
