import asyncio
import io
import re

import discord
from discord import app_commands
from discord.ext import commands

from .common import FEATURE_UNAVAILABLE, get_json, is_admin, log_event, reply, safe_error, set_json


def cfg_key(guild_id: int) -> str:
    return f"ticket:config:{guild_id}"


class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # 永続View

    @discord.ui.button(label="お問い合わせ", emoji="🎫", style=discord.ButtonStyle.primary,
                       custom_id="ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("Tickets")
        if cog is None:
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        await cog.open_ticket(interaction)


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # 1. チケットを閉じるボタン
    @discord.ui.button(label="チケットを閉じる", emoji="🔒", style=discord.ButtonStyle.danger,
                       custom_id="ticket:close", row=0)
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("Tickets")
        if cog is None:
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        await cog.close_ticket(interaction)

    # 2. トランスクリプト（ログ）保存ボタン
    @discord.ui.button(label="トランスクリプト保存", emoji="📝", style=discord.ButtonStyle.secondary,
                       custom_id="ticket:transcript", row=0)
    async def transcript_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("Tickets")
        if cog is None:
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        await cog.transcript_ticket(interaction)


def ticket_owner_id(channel) -> int | None:
    """チャンネルのトピック（ticket:<ユーザーID>）から、チケットの持ち主を取り出す。"""
    topic = getattr(channel, "topic", None) or ""
    m = re.fullmatch(r"ticket:(\d+)", topic.strip())
    return int(m.group(1)) if m else None


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(TicketOpenView())
        self.bot.add_view(TicketCloseView())

    group = app_commands.Group(name="ticket", description="チケット機能を設定します",
                               guild_only=True,
                               default_permissions=discord.Permissions(administrator=True))

    @group.command(name="setup", description="チケットの対応ロールと、作成先カテゴリーを設定します")
    @app_commands.describe(support_role="チケットを見られる対応ロール", category="チケットを作るカテゴリー（省略可）")
    async def setup_cmd(self, interaction: discord.Interaction, support_role: discord.Role,
                        category: discord.CategoryChannel | None = None):
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        try:
            await set_json(cfg_key(interaction.guild.id),
                           {"role_id": support_role.id, "category_id": category.id if category else None})
        except Exception as e:
            print(f"チケット設定の保存エラー: {safe_error(e)}")
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        await reply(interaction, f"✅ 設定しました\n対応ロール: {support_role.mention}\n"
                                 f"カテゴリー: {category.mention if category else '（なし）'}\n"
                                 "次に `/ticket panel` でパネルを送信してください。",
                    allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="panel", description="チケット作成用のパネル（ボタン）を送信します")
    @app_commands.describe(channel="送信先（省略すると今のチャンネル）")
    async def panel_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        guild = interaction.guild
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        try:
            cfg = await get_json(cfg_key(guild.id))
        except Exception as e:
            print(f"チケット設定の取得エラー: {safe_error(e)}")
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        if not cfg:
            await reply(interaction, "先に `/ticket setup` で設定してください。")
            return
        target = channel or interaction.channel
        perms = target.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await reply(interaction, f"{target.mention} でBotに「チャンネルを見る・メッセージを送信・埋め込みリンク」の権限がありません。")
            return
        embed = discord.Embed(title="🎫 お問い合わせ",
                              description="下のボタンを押すと、あなた専用のチャンネルが作成されます。",
                              color=0x5865F2)
        await target.send(embed=embed, view=TicketOpenView())
        await reply(interaction, f"✅ {target.mention} にパネルを送信しました。")

    # --- ボタンの処理：チケット作成 ---
    async def open_ticket(self, interaction: discord.Interaction):
        guild, user = interaction.guild, interaction.user
        try:
            cfg = await get_json(cfg_key(guild.id))
        except Exception as e:
            print(f"チケット設定の取得エラー: {safe_error(e)}")
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        if not cfg:
            await reply(interaction, "チケットはまだ設定されていません。管理者にお知らせください。")
            return

        existing = next((c for c in guild.text_channels if ticket_owner_id(c) == user.id), None)
        if existing:
            await reply(interaction, f"すでにチケットがあります: {existing.mention}")
            return

        role = guild.get_role(cfg.get("role_id") or 0)
        category = guild.get_channel(cfg.get("category_id") or 0)
        if not isinstance(category, discord.CategoryChannel):
            category = None
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                              read_message_history=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                  manage_channels=True, read_message_history=True,
                                                  embed_links=True),
        }
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                           read_message_history=True, attach_files=True)
        slug = re.sub(r"[^a-z0-9_-]", "", user.name.lower())[:20] or "user"

        await interaction.response.defer(ephemeral=True)
        try:
            channel = await guild.create_text_channel(
                f"ticket-{slug}", category=category, overwrites=overwrites,
                topic=f"ticket:{user.id}", reason=f"チケット作成（{user}）"[:200])
        except discord.Forbidden:
            await reply(interaction, "チャンネルを作成する権限がBotにありません（「チャンネルの管理」が必要です）。")
            return
        except discord.HTTPException as e:
            print(f"チケット作成エラー: {safe_error(e)}")
            await reply(interaction, "チケットを作成できませんでした（カテゴリーのチャンネル数が上限の可能性があります）。")
            return

        embed = discord.Embed(title="🎫 チケット",
                              description="ご用件をこのチャンネルに書き込んでください。担当者が対応します。\n"
                                          "解決したら、下のボタンで閉じられます。",
                              color=0x22C55E)
        mention = f"{user.mention} {role.mention if role else ''}".strip()
        await channel.send(mention, embed=embed, view=TicketCloseView(),
                           allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await reply(interaction, f"✅ チケットを作成しました: {channel.mention}")

    # --- ボタンの処理：トランスクリプト（ログ）保存 ---
    async def transcript_ticket(self, interaction: discord.Interaction):
        channel = interaction.channel
        owner_id = ticket_owner_id(channel)
        if owner_id is None:
            await reply(interaction, "ここはチケットのチャンネルではありません。")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            messages = [msg async for msg in channel.history(limit=None, oldest_first=True)]
            
            log_content = f"=== チケットログ: {channel.name} ===\n"
            log_content += f"作成日時: {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            
            for msg in messages:
                timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                log_content += f"[{timestamp}] {msg.author} (ID: {msg.author.id}): {msg.content}\n"
                if msg.attachments:
                    for att in msg.attachments:
                        log_content += f"  [添付ファイル: {att.url}]\n"
                        
            file = discord.File(
                io.BytesIO(log_content.encode('utf-8')), 
                filename=f"transcript-{channel.name}.txt"
            )
            
            await interaction.followup.send("📝 チケットのトランスクリプトを作成しました。", file=file, ephemeral=True)
        except Exception as e:
            print(f"トランスクリプト作成エラー: {safe_error(e)}")
            await reply(interaction, "ログの生成に失敗しました。")

    # --- ボタンの処理：チケットを閉じる ---
    async def close_ticket(self, interaction: discord.Interaction):
        guild, user, channel = interaction.guild, interaction.user, interaction.channel
        owner_id = ticket_owner_id(channel)
        if owner_id is None:
            await reply(interaction, "ここはチケットのチャンネルではありません。")
            return
        role = None
        try:
            cfg = await get_json(cfg_key(guild.id))
            role = guild.get_role((cfg or {}).get("role_id") or 0)
        except Exception as e:
            print(f"チケット設定の取得エラー: {safe_error(e)}")
        allowed = (user.id == owner_id or (role is not None and role in user.roles)
                   or user.guild_permissions.manage_channels)
        if not allowed:
            await reply(interaction, "このチケットを閉じられるのは、作成者か対応担当者のみです。")
            return
        await interaction.response.send_message("🔒 5秒後に、このチケットを閉じます。")
        embed = discord.Embed(title="🎫 チケットを閉じました", color=0x6B7280,
                              description=f"作成者: <@{owner_id}>\n閉じた人: {user.mention}\nチャンネル: #{channel.name}")
        await log_event(self.bot, guild, embed)
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"チケットクローズ（{user}）"[:200])
        except discord.HTTPException as e:
            print(f"チケット削除エラー: {safe_error(e)}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
