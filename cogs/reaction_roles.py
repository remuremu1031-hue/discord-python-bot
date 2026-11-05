"""リアクションロール。指定メッセージの絵文字リアクションで、ロールを付与・解除する。
設定は Redis のハッシュ（rr:<guild_id>）。フィールド "<メッセージID>:<絵文字>" → {"r":ロールID,"c":チャンネルID}"""
import json

import discord
from discord import app_commands
from discord.ext import commands

from .common import FEATURE_UNAVAILABLE, is_admin, rdo, reply, safe_error


def rr_key(guild_id: int) -> str:
    return f"rr:{guild_id}"


def emoji_key(emoji) -> str:
    """カスタム絵文字はID、通常の絵文字は文字そのものをキーにする。"""
    return str(emoji.id) if getattr(emoji, "id", None) else str(emoji.name)


class ReactionRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._maps: dict[int, dict[str, dict]] = {}  # サーバーごとの設定（メモリに保持してRedis呼び出しを減らす）

    async def _get_map(self, guild_id: int) -> dict[str, dict]:
        if guild_id in self._maps:
            return self._maps[guild_id]
        try:
            raw = await rdo("hgetall", rr_key(guild_id))
        except Exception as e:
            print(f"リアクションロールの取得エラー: {safe_error(e)}")
            return {}  # 失敗時は保持しない（次回また試す）
        data: dict[str, dict] = {}
        for field, val in raw.items():
            try:
                data[field] = json.loads(val)
            except Exception:
                pass
        self._maps[guild_id] = data
        return data

    group = app_commands.Group(name="reactionrole", description="リアクションロールを管理します",
                               guild_only=True,
                               default_permissions=discord.Permissions(administrator=True))

    @group.command(name="add", description="メッセージの絵文字リアクションにロールを割り当てます")
    @app_commands.describe(message_id="対象のメッセージID", emoji="絵文字（例: ✅ や カスタム絵文字）",
                           role="付与するロール", channel="メッセージのあるチャンネル（省略すると今のチャンネル）")
    async def add(self, interaction: discord.Interaction, message_id: str, emoji: str,
                  role: discord.Role, channel: discord.TextChannel | None = None):
        guild = interaction.guild
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        if role.is_default() or role.managed or role.permissions.administrator:
            await reply(interaction, "そのロールは割り当てできません（@everyone・連携ロール・管理者権限つき）。")
            return
        if role >= guild.me.top_role:
            await reply(interaction, "Botのロールが選んだロールより下です。Botのロールを上に移動してください。")
            return
        if not message_id.strip().isdigit():
            await reply(interaction, "メッセージIDは数字で入力してください。")
            return
        channel = channel or interaction.channel
        try:
            message = await channel.fetch_message(int(message_id.strip()))
            pe = discord.PartialEmoji.from_str(emoji.strip())
            await message.add_reaction(pe)  # 利用者が押しやすいように、Botが先にリアクションを付ける
        except discord.NotFound:
            await reply(interaction, "メッセージが見つかりません（チャンネルを確認してください）。")
            return
        except (discord.Forbidden, discord.HTTPException) as e:
            await reply(interaction, f"リアクションを付けられませんでした（{safe_error(e)}）。絵文字と、Botの権限を確認してください。")
            return
        field = f"{message.id}:{emoji_key(pe)}"
        try:
            await rdo("hset", rr_key(guild.id), field, json.dumps({"r": role.id, "c": channel.id}))
        except Exception as e:
            print(f"リアクションロールの保存エラー: {safe_error(e)}")
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        self._maps.pop(guild.id, None)  # 次の利用時に読み直す
        await reply(interaction, f"✅ {emoji} → {role.mention} を設定しました。\n{message.jump_url}",
                    allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="remove", description="リアクションロールの割り当てを削除します")
    @app_commands.describe(message_id="対象のメッセージID", emoji="絵文字")
    async def remove(self, interaction: discord.Interaction, message_id: str, emoji: str):
        guild = interaction.guild
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        pe = discord.PartialEmoji.from_str(emoji.strip())
        field = f"{message_id.strip()}:{emoji_key(pe)}"
        try:
            raw = await rdo("hget", rr_key(guild.id), field)
            n = await rdo("hdel", rr_key(guild.id), field)
        except Exception as e:
            print(f"リアクションロールの削除エラー: {safe_error(e)}")
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        self._maps.pop(guild.id, None)
        if not n:
            await reply(interaction, "その割り当ては見つかりません。")
            return
        try:  # Botが付けたリアクションを外す（失敗しても無視）
            ch = guild.get_channel(json.loads(raw).get("c", 0))
            if ch:
                msg = await ch.fetch_message(int(message_id.strip()))
                await msg.remove_reaction(pe, guild.me)
        except Exception:
            pass
        await reply(interaction, "✅ 割り当てを削除しました。")

    @group.command(name="list", description="このサーバーのリアクションロール一覧を表示します")
    async def list_(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        data = await self._get_map(guild.id)
        lines = []
        for field, v in sorted(data.items()):
            mid, ek = field.split(":", 1)
            url = f"https://discord.com/channels/{guild.id}/{v.get('c', 0)}/{mid}"
            e = f"<:e:{ek}>" if ek.isdigit() else ek
            lines.append(f"・{e} → <@&{v.get('r', 0)}> ／ [メッセージ]({url})")
        embed = discord.Embed(title=f"🎭 リアクションロール（{len(lines)}件）",
                              description="\n".join(lines)[:3800] if lines else "設定はありません。",
                              color=0x5865F2)
        await reply(interaction, embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # --- リアクションの付与・解除 ---
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle(payload, add=False)

    async def _handle(self, payload: discord.RawReactionActionEvent, *, add: bool):
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return
        mapping = await self._get_map(payload.guild_id)
        entry = mapping.get(f"{payload.message_id}:{emoji_key(payload.emoji)}") if mapping else None
        if not entry:
            return
        guild = self.bot.get_guild(payload.guild_id)
        role = guild.get_role(entry.get("r", 0)) if guild else None
        if guild is None or role is None:
            return
        try:
            member = payload.member if add and payload.member else await guild.fetch_member(payload.user_id)
            if member.bot:
                return
            if add:
                await member.add_roles(role, reason="リアクションロール")
            else:
                await member.remove_roles(role, reason="リアクションロール")
        except discord.NotFound:
            return
        except discord.HTTPException as e:
            print(f"リアクションロールの付与・解除エラー: {safe_error(e)}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))
