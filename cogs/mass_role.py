"""一斉ロール付与 (/mass_role)。管理者のみ。Bot除外・所持者スキップ・ウェイト付き。
※ メンバー一覧の取得に「SERVER MEMBERS INTENT」が必要（ENABLE_MEMBERS_INTENT=1）。"""
import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from .common import is_admin, log_event, reply, safe_error

WAIT_SECONDS = 1.2  # 1人ごとの待ち時間（レート制限対策）


class ConfirmView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("実行した本人だけが操作できます。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="実行する", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.edit_message(content="⏳ 付与を開始します…", view=None)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="キャンセルしました。", view=None)
        self.stop()


class MassRole(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._running: set[int] = set()  # 実行中のサーバー（同時に2回走らせない）

    @app_commands.command(name="mass_role",
                          description="指定したロールを、メンバー全員に一斉付与します（管理者のみ）")
    @app_commands.describe(role="付与するロール")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def mass_role(self, interaction: discord.Interaction, role: discord.Role):
        guild = interaction.guild
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        if guild.id in self._running:
            await reply(interaction, "このサーバーでは、すでに一斉付与を実行中です。")
            return
        if role.is_default() or role.managed:
            await reply(interaction, "そのロールは付与対象にできません（@everyone / 連携ロール）。")
            return
        if role.permissions.administrator:
            await reply(interaction, "管理者権限を含むロールは、安全のため一斉付与できません。")
            return
        if role >= guild.me.top_role:
            await reply(interaction, "Botのロールが選んだロールより下です。Botのロールを上に移動してください。")
            return
        if not self.bot.intents.members:
            await reply(interaction, "メンバー一覧を取得できません。運営者が SERVER MEMBERS INTENT を有効にする必要があります。")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        targets: list[discord.Member] = []
        skipped = bots = 0
        try:
            async for m in guild.fetch_members(limit=None):
                if m.bot:
                    bots += 1
                elif role in m.roles:
                    skipped += 1
                else:
                    targets.append(m)
        except Exception as e:
            print(f"一斉付与: メンバー取得エラー: {safe_error(e)}")
            await reply(interaction, "メンバー一覧を取得できませんでした。")
            return

        if not targets:
            await reply(interaction, f"付与対象がいません（所持済み {skipped} 人・Bot {bots} 体はスキップ）。")
            return

        minutes = len(targets) * WAIT_SECONDS / 60
        view = ConfirmView(interaction.user.id)
        msg = await interaction.followup.send(
            f"{role.mention} を **{len(targets)} 人** に付与します。\n"
            f"（Bot {bots} 体・所持済み {skipped} 人はスキップ／所要時間の目安: 約{minutes:.0f}分）\n"
            "実行しますか？", view=view, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        await view.wait()
        if not view.confirmed:
            return

        self._running.add(guild.id)
        done = failed = 0
        try:
            for i, m in enumerate(targets, 1):
                try:
                    await m.add_roles(role, reason=f"一斉ロール付与（実行者: {interaction.user}）"[:200])
                    done += 1
                except discord.HTTPException:  # 退出済み・権限不足など
                    failed += 1
                if i % 20 == 0:
                    try:
                        await msg.edit(content=f"⏳ {i}/{len(targets)} 人 処理済み（成功 {done} / 失敗 {failed}）")
                    except discord.HTTPException:
                        pass  # 15分を過ぎると編集できない（処理は続ける）
                await asyncio.sleep(WAIT_SECONDS)
        finally:
            self._running.discard(guild.id)

        summary = (f"✅ {role.mention} の一斉付与が完了しました\n"
                   f"成功 {done} 人 / 失敗 {failed} 人 / 所持済み {skipped} 人 / Bot {bots} 体")
        try:
            await msg.edit(content=summary)
        except discord.HTTPException:  # 長時間かかって編集できないときは、チャンネルに送る
            try:
                await interaction.channel.send(summary, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        embed = discord.Embed(title="🏷️ 一斉ロール付与", color=0x5865F2,
                              description=f"ロール: {role.mention}\n成功 {done} / 失敗 {failed} / スキップ {skipped}")
        embed.add_field(name="実行者", value=interaction.user.mention)
        await log_event(self.bot, guild, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MassRole(bot))
