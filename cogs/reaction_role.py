"""リアクションロール。
1) 絵文字リアクション方式（従来どおり）: 指定メッセージの絵文字で、ロールを付与・解除する。
   設定は Redis のハッシュ（rr:<guild_id>）。フィールド "<メッセージID>:<絵文字>" → {"r":ロールID,"c":チャンネルID}
2) セレクトメニュー方式（複数選択）: パネルの「ロールを選ぶ」ボタンで、本人にだけメニューを表示。
   今持っているロールにチェックが入った状態で開き、何個でも同時に付与・解除できる（1パネル最大25ロール）。
   設定は Redis のハッシュ（rrm:<guild_id>）。フィールド "<パネルID>" → パネルのJSON"""
import json
import re
import secrets

import discord
from discord import app_commands
from discord.ext import commands

from .common import FEATURE_UNAVAILABLE, is_admin, rdo, reply, safe_error


def rr_key(guild_id: int) -> str:
    return f"rr:{guild_id}"


def menu_key(guild_id: int) -> str:
    return f"rrm:{guild_id}"


MENU_MAX = 25  # セレクトメニューの選択肢の上限


def emoji_key(emoji) -> str:
    """カスタム絵文字はID、通常の絵文字は文字そのものをキーにする。"""
    return str(emoji.id) if getattr(emoji, "id", None) else str(emoji.name)


class ReactionRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._maps: dict[int, dict[str, dict]] = {}  # サーバーごとの設定（メモリに保持してRedis呼び出しを減らす）
        self._menus: dict[int, dict[str, dict]] = {}  # セレクトメニュー方式のパネル

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

    # ================= セレクトメニュー方式（複数選択） =================
    async def _get_menus(self, guild_id: int) -> dict[str, dict]:
        if guild_id in self._menus:
            return self._menus[guild_id]
        try:
            raw = await rdo("hgetall", menu_key(guild_id))
        except Exception as e:
            print(f"ロールメニューの取得エラー: {safe_error(e)}")
            return {}
        data = {}
        for k, v in raw.items():
            try:
                data[k] = json.loads(v)
            except Exception:
                pass
        self._menus[guild_id] = data
        return data

    async def _save_menu(self, guild_id: int, pid: str, panel: dict | None) -> bool:
        try:
            if panel is None:
                await rdo("hdel", menu_key(guild_id), pid)
            else:
                await rdo("hset", menu_key(guild_id), pid, json.dumps(panel, ensure_ascii=False))
        except Exception as e:
            print(f"ロールメニューの保存エラー: {safe_error(e)}")
            return False
        self._menus.pop(guild_id, None)
        return True

    @staticmethod
    def _parse_emoji(raw):
        if not raw:
            return None
        m = re.fullmatch(r"<(a?):(\w+):(\d+)>", raw.strip())
        if m:
            return discord.PartialEmoji(name=m.group(2), id=int(m.group(3)), animated=bool(m.group(1)))
        return raw.strip()[:8]

    def _menu_embed(self, guild: discord.Guild, p: dict) -> discord.Embed:
        lines = []
        for r in p.get("roles", []):
            role = guild.get_role(r["id"])
            if role:
                lines.append(f"{(r.get('emoji') or '') + ' ' if r.get('emoji') else ''}{role.mention}"
                             f"{' — ' + r['desc'] if r.get('desc') else ''}")
        e = discord.Embed(title=p.get("title") or "ロールを選択",
                          description=((p.get("description") or "") + "\n\n" + ("\n".join(lines) or "（ロール未登録）")).strip(),
                          color=p.get("color") or 0x5865F2)
        e.set_footer(text="「ロールを選ぶ」から、何個でも同時に付け外しできます")
        return e

    @staticmethod
    def _menu_view(pid: str) -> discord.ui.View:
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="ロールを選ぶ", emoji="🎭", style=discord.ButtonStyle.primary,
                                     custom_id=f"rrm:open:{pid}"))
        v.add_item(discord.ui.Button(label="すべて外す", emoji="🧹", style=discord.ButtonStyle.secondary,
                                     custom_id=f"rrm:clear:{pid}"))
        return v

    async def _refresh(self, guild: discord.Guild, pid: str, p: dict):
        ch = guild.get_channel(p.get("c") or 0)
        if isinstance(ch, discord.TextChannel) and p.get("m"):
            try:
                await ch.get_partial_message(p["m"]).edit(embed=self._menu_embed(guild, p), view=self._menu_view(pid))
            except discord.HTTPException:
                pass

    async def menu_autocomplete(self, interaction: discord.Interaction, current: str):
        menus = await self._get_menus(interaction.guild_id)
        return [app_commands.Choice(name=f"{p.get('title', '無題')}（{pid}）"[:100], value=pid)
                for pid, p in menus.items() if current.lower() in f"{p.get('title', '')}{pid}".lower()][:25]

    @group.command(name="menu-create", description="複数選択できるロールメニュー（パネル）を作成します")
    @app_commands.describe(title="タイトル", description="説明文（\\n で改行）", color="色（例: #5865F2）")
    async def menu_create(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 100],
                          description: app_commands.Range[str, 0, 1500] | None = None, color: str | None = None):
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        try:
            col = int(color.strip().lstrip("#"), 16) if color else None
        except ValueError:
            await reply(interaction, "色は #5865F2 のような形式で入力してください。")
            return
        pid = secrets.token_hex(3)
        panel = {"title": title, "description": (description or "").replace("\\n", "\n"), "color": col, "roles": []}
        if not await self._save_menu(interaction.guild_id, pid, panel):
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        await reply(interaction, f"✅ ロールメニュー `{pid}` を作成しました。\n"
                                 f"`/reactionrole menu-add menu:{pid} role:ロール` でロールを追加し、"
                                 "`/reactionrole menu-send` で送信してください。")

    @group.command(name="menu-add", description="ロールメニューにロールを追加します（最大25個）")
    @app_commands.describe(menu="ロールメニュー", role="追加するロール", emoji="絵文字（任意）", description="説明（任意）")
    @app_commands.autocomplete(menu=menu_autocomplete)
    async def menu_add(self, interaction: discord.Interaction, menu: str, role: discord.Role,
                       emoji: str | None = None, description: app_commands.Range[str, 0, 90] | None = None):
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
        p = (await self._get_menus(guild.id)).get(menu)
        if p is None:
            await reply(interaction, "ロールメニューが見つかりません。")
            return
        roles = [r for r in p["roles"] if r["id"] != role.id]
        if len(roles) >= MENU_MAX:
            await reply(interaction, f"1つのメニューに登録できるのは{MENU_MAX}個までです。別のメニューを作成してください。")
            return
        roles.append({"id": role.id, "emoji": emoji.strip() if emoji else None, "desc": description})
        p["roles"] = roles
        if not await self._save_menu(guild.id, menu, p):
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        await self._refresh(guild, menu, p)
        await reply(interaction, f"✅ {role.mention} を追加しました（{len(roles)}/{MENU_MAX}）。",
                    allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="menu-remove", description="ロールメニューからロールを外します")
    @app_commands.describe(menu="ロールメニュー", role="外すロール")
    @app_commands.autocomplete(menu=menu_autocomplete)
    async def menu_remove(self, interaction: discord.Interaction, menu: str, role: discord.Role):
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        p = (await self._get_menus(interaction.guild_id)).get(menu)
        if p is None:
            await reply(interaction, "ロールメニューが見つかりません。")
            return
        p["roles"] = [r for r in p["roles"] if r["id"] != role.id]
        if not await self._save_menu(interaction.guild_id, menu, p):
            await reply(interaction, FEATURE_UNAVAILABLE)
            return
        await self._refresh(interaction.guild, menu, p)
        await reply(interaction, f"✅ {role.mention} を外しました。", allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="menu-send", description="ロールメニューをチャンネルに送信します")
    @app_commands.describe(menu="ロールメニュー", channel="送信先（省略で今のチャンネル）")
    @app_commands.autocomplete(menu=menu_autocomplete)
    async def menu_send(self, interaction: discord.Interaction, menu: str, channel: discord.TextChannel | None = None):
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        p = (await self._get_menus(interaction.guild_id)).get(menu)
        if p is None or not p["roles"]:
            await reply(interaction, "メニューが見つからないか、ロールが未登録です。")
            return
        target = channel or interaction.channel
        try:
            msg = await target.send(embed=self._menu_embed(interaction.guild, p), view=self._menu_view(menu))
        except discord.HTTPException as e:
            await reply(interaction, f"送信できませんでした（{safe_error(e)}）。Botの権限を確認してください。")
            return
        p["c"], p["m"] = target.id, msg.id
        await self._save_menu(interaction.guild_id, menu, p)
        await reply(interaction, f"✅ {target.mention} に送信しました。\n{msg.jump_url}")

    @group.command(name="menu-delete", description="ロールメニューを削除します（送信済みメッセージも削除）")
    @app_commands.describe(menu="ロールメニュー")
    @app_commands.autocomplete(menu=menu_autocomplete)
    async def menu_delete(self, interaction: discord.Interaction, menu: str):
        if not is_admin(interaction):
            await reply(interaction, "管理者のみ実行できます。")
            return
        p = (await self._get_menus(interaction.guild_id)).get(menu)
        if p is None:
            await reply(interaction, "ロールメニューが見つかりません。")
            return
        await self._save_menu(interaction.guild_id, menu, None)
        ch = interaction.guild.get_channel(p.get("c") or 0)
        if isinstance(ch, discord.TextChannel) and p.get("m"):
            try:
                await ch.get_partial_message(p["m"]).delete()
            except discord.HTTPException:
                pass
        await reply(interaction, f"🗑️ ロールメニュー `{menu}` を削除しました。")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component or interaction.guild is None:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("rrm:"):
            return
        if getattr(self.bot, "is_suspended", lambda _: False)(interaction.guild.id):
            return  # ライセンス停止中（返信は main.py 側）
        try:
            _, kind, pid = cid.split(":", 2)
        except ValueError:
            return
        guild, member = interaction.guild, interaction.user
        p = (await self._get_menus(guild.id)).get(pid)
        if p is None:
            await interaction.response.send_message("このロールメニューは削除されています。", ephemeral=True)
            return
        meta = {r["id"]: r for r in p["roles"]}
        roles = [r for r in (guild.get_role(i) for i in meta) if r and not r.managed and r < guild.me.top_role]
        if not roles:
            await interaction.response.send_message("このメニューには使えるロールがありません。", ephemeral=True)
            return
        if kind == "open":
            select = discord.ui.Select(
                custom_id=f"rrm:pick:{pid}", placeholder="付けたいロールにチェック（複数可・外すとロール解除）",
                min_values=0, max_values=len(roles),
                options=[discord.SelectOption(label=r.name[:100], value=str(r.id), default=r in member.roles,
                                              description=meta[r.id].get("desc") or None,
                                              emoji=self._parse_emoji(meta[r.id].get("emoji"))) for r in roles])
            view = discord.ui.View(timeout=300)
            view.add_item(select)
            await interaction.response.send_message("チェックしたロールが付与され、外したロールは解除されます。",
                                                    view=view, ephemeral=True)
            return
        chosen = {int(v) for v in (interaction.data or {}).get("values", [])} if kind == "pick" else set()
        add = [r for r in roles if r.id in chosen and r not in member.roles]
        rem = [r for r in roles if r.id not in chosen and r in member.roles]
        try:
            if add:
                await member.add_roles(*add, reason="ロールメニュー")
            if rem:
                await member.remove_roles(*rem, reason="ロールメニュー")
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ ロールを変更できませんでした（{safe_error(e)}）。", ephemeral=True)
            return
        text = "\n".join(x for x in (("➕ 付与: " + " ".join(r.mention for r in add)) if add else "",
                                      ("➖ 解除: " + " ".join(r.mention for r in rem)) if rem else "") if x) or "変更はありません。"
        if kind == "pick":
            await interaction.response.edit_message(content=text, view=None)
        else:
            await interaction.response.send_message(text, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))
