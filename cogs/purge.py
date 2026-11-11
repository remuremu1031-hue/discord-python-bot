"""霊夢BOT メッセージ削除（/purge）

- 件数を指定して削除（1〜1000件）。特定のユーザー・BOTだけに絞ったり、ピン留めを残したりできる。
- チャンネルのメッセージを全部削除。
    ・作り直さない（標準）: チャンネルはそのまま。14日より古いメッセージは1件ずつなので時間がかかる。
    ・作り直す        : チャンネルを複製して元を消す。一瞬で終わるが、チャンネルIDが変わる
                         （ログの送信先などに設定していた場合は、設定し直しが必要）。
- 全部削除は、確認ボタンを押してから実行する。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

ALL_MAX = 20000          # 「全部」で削除する上限（作り直さない場合）
_running: dict[int, dict] = {}  # チャンネルID → 進行状況（同じチャンネルで同時に動かさない）


class ConfirmView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.ok: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("実行した本人だけが操作できます。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="全部削除する", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.ok = True
        await interaction.response.edit_message(content="🗑️ 削除を始めます…", view=None)
        self.stop()

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.ok = False
        await interaction.response.edit_message(content="取り消しました。", view=None)
        self.stop()


class Purge(commands.Cog):
    """メッセージの一括削除。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------- 共通処理 ----------------
    @staticmethod
    def check_perms(channel, member: discord.Member | None, *, recreate: bool = False) -> str:
        me = channel.guild.me
        p = channel.permissions_for(me)
        if not (p.manage_messages and p.read_message_history and p.view_channel):
            return "Botに、このチャンネルの「メッセージの管理」「メッセージ履歴を読む」権限がありません。"
        if recreate and not (p.manage_channels and channel.guild.me.guild_permissions.manage_channels):
            return "チャンネルを作り直すには、Botに「チャンネルの管理」権限が必要です。"
        if member is not None:
            mp = channel.permissions_for(member)
            if not mp.manage_messages:
                return "このチャンネルの「メッセージの管理」権限が必要です。"
            if recreate and not mp.manage_channels:
                return "チャンネルを作り直すには、「チャンネルの管理」権限が必要です。"
        return ""

    async def delete_count(self, channel, count: int, *, user_id: int | None = None, bots_only: bool = False,
                           keep_pinned: bool = True, skip_id: int | None = None) -> int:
        """新しい順に count 件を見て、条件に合うものを削除する。削除した数を返す。"""
        def check(m: discord.Message) -> bool:
            if skip_id and m.id == skip_id:
                return False
            if keep_pinned and m.pinned:
                return False
            if user_id and m.author.id != user_id:
                return False
            if bots_only and not m.author.bot:
                return False
            return True
        deleted = await channel.purge(limit=count, check=check, bulk=True, oldest_first=False,
                                      reason="霊夢BOT: メッセージの一括削除")
        return len(deleted)

    async def delete_all(self, channel, *, keep_pinned: bool, progress=None) -> int:
        """チャンネルのメッセージを全部削除する（チャンネルは作り直さない）。"""
        total = 0
        state = _running.setdefault(channel.id, {"done": 0, "start": time.time()})
        while total < ALL_MAX:
            batch = await channel.purge(limit=min(500, ALL_MAX - total), check=(lambda m: not (keep_pinned and m.pinned)),
                                        bulk=True, reason="霊夢BOT: チャンネルのメッセージを全部削除")
            if not batch:
                break
            total += len(batch)
            state["done"] = total
            if progress:
                try:
                    await progress(total)
                except Exception:
                    pass
        return total

    async def recreate(self, channel: discord.TextChannel, member: discord.Member) -> discord.TextChannel:
        """チャンネルを複製して、元のチャンネルを消す（中身が全部消える）。"""
        new = await channel.clone(reason=f"霊夢BOT: {member} がチャンネルのメッセージを全部削除（作り直し）")
        try:
            await new.edit(position=channel.position, reason="元の位置に戻す")
        except discord.HTTPException:
            pass
        await channel.delete(reason=f"霊夢BOT: {member} がチャンネルのメッセージを全部削除（作り直し）")
        return new

    async def log(self, guild: discord.Guild, member, channel_name: str, text: str) -> None:
        e = discord.Embed(title="🗑️ メッセージを削除しました", color=0xF59E0B, description=text,
                          timestamp=datetime.now(timezone.utc))
        e.add_field(name="チャンネル", value=channel_name, inline=True)
        e.add_field(name="実行した人", value=f"{member.mention}（`{member.id}`）" if member else "不明", inline=True)
        try:
            await self.bot.guild_log(guild, e)
        except Exception:
            pass

    # ---------------- コマンド ----------------
    @app_commands.command(name="purge", description="メッセージを削除します（件数指定 / 全部）")
    @app_commands.describe(count="削除するメッセージの数（1〜1000）。「全部」のときは不要",
                           all="チャンネルのメッセージを全部削除する",
                           user="このユーザーのメッセージだけ削除（件数指定のとき）",
                           bots_only="BOTのメッセージだけ削除（件数指定のとき）",
                           keep_pinned="ピン留めしたメッセージは残す（標準: 残す）",
                           recreate="全部削除のとき、チャンネルを作り直して一瞬で消す（チャンネルIDが変わります）",
                           channel="削除するチャンネル（省略すると、このチャンネル）")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def purge_cmd(self, interaction: discord.Interaction,
                        count: app_commands.Range[int, 1, 1000] | None = None,
                        all: bool = False,
                        user: discord.User | None = None,
                        bots_only: bool = False,
                        keep_pinned: bool = True,
                        recreate: bool = False,
                        channel: discord.TextChannel | discord.Thread | discord.VoiceChannel | None = None):
        ch = channel or interaction.channel
        member = interaction.user
        if not isinstance(ch, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            await interaction.response.send_message("このチャンネルでは使えません。", ephemeral=True)
            return
        if not all and not count:
            await interaction.response.send_message("`count`（削除する数）を指定するか、`all` を「はい」にしてください。", ephemeral=True)
            return
        if recreate and not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("作り直しは、テキストチャンネルだけで使えます。", ephemeral=True)
            return
        why = self.check_perms(ch, member, recreate=all and recreate)
        if why:
            await interaction.response.send_message(f"❌ {why}", ephemeral=True)
            return
        if ch.id in _running:
            await interaction.response.send_message("このチャンネルでは、すでに削除を実行中です。", ephemeral=True)
            return

        if not all:  # 件数指定
            await interaction.response.defer(ephemeral=True, thinking=True)
            _running[ch.id] = {"done": 0, "start": time.time()}
            try:
                n = await self.delete_count(ch, count, user_id=user.id if user else None, bots_only=bots_only,
                                            keep_pinned=keep_pinned)
            except discord.HTTPException as e:
                await interaction.followup.send(f"❌ 削除できませんでした（{e.status}）。", ephemeral=True)
                return
            finally:
                _running.pop(ch.id, None)
            cond = "・".join(x for x in [f"{user.display_name} のみ" if user else "", "BOTのみ" if bots_only else "",
                                        "ピン留めは残す" if keep_pinned else ""] if x)
            await interaction.followup.send(f"🗑️ {ch.mention} のメッセージを **{n}件** 削除しました（直近 {count}件から"
                                            + (f"・{cond}" if cond else "") + "）。", ephemeral=True)
            await self.log(interaction.guild, member, ch.mention, f"{n}件を削除（直近 {count}件から" + (f"・{cond}" if cond else "") + "）")
            return

        # 全部削除：確認してから
        view = ConfirmView(member.id)
        note = ("チャンネルを**作り直して**、一瞬で全部消します。\n⚠️ チャンネルIDが変わるため、ログの送信先などにこのチャンネルを"
                "設定している場合は、ダッシュボードで設定し直してください。" if recreate else
                "チャンネルはそのままで、メッセージを全部消します。\n⚠️ 14日より古いメッセージは1件ずつ消すため、時間がかかります"
                f"（最大 {ALL_MAX:,}件）。")
        await interaction.response.send_message(
            f"🗑️ **{ch.mention} のメッセージを全部削除します。元に戻せません。**\n{note}"
            + ("\n📌 ピン留めしたメッセージは残します。" if keep_pinned and not recreate else ""),
            view=view, ephemeral=True)
        await view.wait()
        if not view.ok:
            if view.ok is None:
                await interaction.edit_original_response(content="時間切れのため取り消しました。", view=None)
            return
        name = ch.mention
        if recreate:
            try:
                new = await self.recreate(ch, member)
            except discord.HTTPException as e:
                await interaction.edit_original_response(content=f"❌ 作り直せませんでした（{e.status}）。")
                return
            try:
                await interaction.edit_original_response(content=f"✅ {new.mention} を作り直して、メッセージを全部削除しました。")
            except discord.HTTPException:
                pass
            try:
                await new.send(f"🗑️ このチャンネルのメッセージは、{member.mention} によって全部削除されました。",
                               allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
            await self.log(interaction.guild, member, new.mention, "チャンネルを作り直して全部削除（チャンネルIDが変わりました）")
            return

        last = {"t": 0.0}

        async def progress(n: int):
            if time.time() - last["t"] < 5:
                return
            last["t"] = time.time()
            await interaction.edit_original_response(content=f"🗑️ 削除中… {n:,}件")

        _running[ch.id] = {"done": 0, "start": time.time()}
        try:
            n = await self.delete_all(ch, keep_pinned=keep_pinned, progress=progress)
        except discord.HTTPException as e:
            n = _running.get(ch.id, {}).get("done", 0)
            await interaction.edit_original_response(content=f"⚠️ 途中で止まりました（{e.status}）。{n:,}件を削除しました。")
            return
        finally:
            _running.pop(ch.id, None)
        try:
            await interaction.edit_original_response(content=f"✅ {name} のメッセージを **{n:,}件** 削除しました。")
        except discord.HTTPException:
            pass
        await self.log(interaction.guild, member, name, f"全部削除（{n:,}件）")

    # ---------------- ダッシュボード ----------------
    def render_card(self, guild: discord.Guild, H) -> str:
        body = H.form("purge_run",
                      H.lbl("チャンネル") + H.sel("channel", H.channel_options(None, none_label="（選択してください）"))
                      + H.lbl("削除の方法") + H.sel("mode", H.opt("count", "件数を指定して削除", "count")
                                                   + H.opt("all", "全部削除（チャンネルはそのまま・時間がかかる）", "")
                                                   + H.opt("recreate", "全部削除（チャンネルを作り直す・一瞬・IDが変わる）", ""))
                      + H.lbl("削除する数（件数指定のとき・1〜1000）") + '<input type="number" name="count" min="1" max="1000" value="50">'
                      + H.lbl("このユーザーのメッセージだけ（ユーザーID・任意）") + '<input type="text" name="user" maxlength="21" inputmode="numeric">'
                      + H.chk("bots_only", False, "BOTのメッセージだけ")
                      + H.chk("keep_pinned", True, "ピン留めしたメッセージは残す")
                      + H.chk("confirm", False, "全部削除のときは、ここにチェック（元に戻せません）"),
                      "削除する", cls="btn danger", need="manage_messages")
        return H.card("🗑️ メッセージ削除", body, need="manage_messages",
                      note="件数指定は新しい順に数えます。全部削除（そのまま）は時間がかかるため、裏で進めて、終わったらログに記録します。")

    async def dashboard_op(self, f: dict, guild: discord.Guild, member: discord.Member, P) -> str:
        ch = P.pick_channel(f.get("channel"), required=True)
        mode = f.get("mode") or "count"
        if mode not in ("count", "all", "recreate"):
            raise ValueError("削除の方法を選んでください")
        if mode != "count" and f.get("confirm") != "1":
            raise ValueError("全部削除するときは「元に戻せません」にチェックしてください")
        if mode == "recreate" and not isinstance(ch, discord.TextChannel):
            raise ValueError("作り直しは、テキストチャンネルだけで使えます")
        why = self.check_perms(ch, member, recreate=mode == "recreate")
        if why:
            raise ValueError(why)
        if ch.id in _running:
            raise ValueError("このチャンネルでは、すでに削除を実行中です")
        keep = f.get("keep_pinned") == "1"
        if mode == "count":
            c = (f.get("count") or "").strip()
            if not c.isdigit() or not 1 <= int(c) <= 1000:
                raise ValueError("削除する数は 1〜1000 で指定してください")
            uid = (f.get("user") or "").strip()
            if uid and not uid.isdigit():
                raise ValueError("ユーザーIDは数字で入力してください")
            _running[ch.id] = {"done": 0, "start": time.time()}
            try:
                n = await self.delete_count(ch, int(c), user_id=int(uid) if uid else None,
                                            bots_only=f.get("bots_only") == "1", keep_pinned=keep)
            finally:
                _running.pop(ch.id, None)
            await self.log(guild, member, ch.mention, f"{n}件を削除（ダッシュボード・直近 {c}件から）")
            return f"#{ch.name} のメッセージを {n}件 削除しました"
        if mode == "recreate":
            new = await self.recreate(ch, member)
            await self.log(guild, member, new.mention, "チャンネルを作り直して全部削除（ダッシュボード・IDが変わりました）")
            return f"#{new.name} を作り直して、メッセージを全部削除しました（チャンネルIDが変わりました）"

        async def run():  # 時間がかかるので裏で進める
            _running[ch.id] = {"done": 0, "start": time.time()}
            try:
                n = await self.delete_all(ch, keep_pinned=keep)
                await self.log(guild, member, ch.mention, f"全部削除（ダッシュボード・{n:,}件）")
            except Exception as e:
                await self.log(guild, member, ch.mention, f"全部削除が途中で止まりました（{type(e).__name__}）")
            finally:
                _running.pop(ch.id, None)
        asyncio.create_task(run())
        return f"#{ch.name} のメッセージの全部削除を始めました（終わったらログに記録します）"


async def setup(bot: commands.Bot):
    await bot.add_cog(Purge(bot))
