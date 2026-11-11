"""霊夢BOT ロックダウン・新規アカウントの隔離・一時VC

■ ロックダウン（/lockdown on|off）
  全テキストチャンネルで @everyone の書き込み（メッセージ・スレッド・リアクション）を一時的に禁止する。
  元の権限を覚えておき、解除するとそのまま戻す。Raid検知と連動して自動でかけることもできる。
■ 新規アカウントの隔離
  作成から N日 未満のアカウントが参加したら、「隔離ロール」を付けて隔離チャンネルだけ見えるようにする
  （またはタイムアウト / キック）。隔離中に付いたロールは預かり、解除のときに返す。
  アカウントが N日 を過ぎるか、指定の時間がたつと自動で解除する。
■ 一時VC
  「作成用VC」に入ると、その人専用のVCを作って移動させる。作った人はチャンネルを管理でき、
  誰もいなくなると自動で消える。作った人が抜けたら、残っている人に引き継ぐ。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

LOCK_PERMS = ("send_messages", "send_messages_in_threads", "create_public_threads",
              "create_private_threads", "add_reactions")
DEFAULTS = {
    "lockdown": {"auto_raid": False, "auto_minutes": 30, "exclude": [], "announce": "", "roles": ["everyone"]},
    "quarantine": {"enabled": False, "min_days": 7, "action": "role", "role": "", "channel": "",
                   "release_hours": 0, "timeout_hours": 24},
    "tempvc": {"enabled": False, "hub": "", "category": "", "name": "🔊 {user}のVC", "limit": 0},
}


def _fmt_ago(ts: float) -> str:
    return f"<t:{int(ts)}:R>"


class LockConfirm(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=60)
        self.uid, self.ok = uid, None

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.uid:
            await i.response.send_message("実行した本人だけが操作できます。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="ロックダウンする", emoji="🔒", style=discord.ButtonStyle.danger)
    async def yes(self, i: discord.Interaction, b):
        self.ok = True
        await i.response.edit_message(content="🔒 ロックダウンしています…", view=None)
        self.stop()

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def no(self, i: discord.Interaction, b):
        self.ok = False
        await i.response.edit_message(content="取り消しました。", view=None)
        self.stop()


class ServerTools(commands.Cog):
    """ロックダウン・新規アカウントの隔離・一時VC。"""

    lockdown = app_commands.Group(name="lockdown", description="サーバー全体の書き込みを一時的に止めます（荒らし対策）",
                                  guild_only=True, default_permissions=discord.Permissions(manage_channels=True))

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._busy: set[int] = set()
        self._vc_cool: dict[int, float] = {}

    # ---------------- 設定・状態 ----------------
    def _load(self, name: str) -> dict:
        try:
            return self.bot.store_load(name) or {}
        except Exception:
            return {}

    def cfg(self, gid: int, part: str) -> dict:
        c = dict(DEFAULTS[part])
        c.update(self._load("server_tools").get(str(gid), {}).get(part, {}))
        return c

    def save_cfg(self, gid: int, part: str, c: dict) -> None:
        data = self._load("server_tools")
        g = data.setdefault(str(gid), {})
        g[part] = {k: c.get(k, v) for k, v in DEFAULTS[part].items()}
        self.bot.store_save("server_tools", data)

    def state(self) -> dict:
        d = self._load("server_tools_state")
        for k in ("lock", "quar", "vc"):
            d.setdefault(k, {})
        return d

    def save_state(self, d: dict) -> None:
        self.bot.store_save("server_tools_state", d)

    async def log(self, guild: discord.Guild, title: str, text: str, color: int = 0x3B82F6) -> None:
        e = discord.Embed(title=title, description=text[:3900], color=color, timestamp=datetime.now(timezone.utc))
        try:
            await self.bot.guild_log(guild, e)
        except Exception:
            pass

    async def cog_load(self):
        self.ticker.start()

    async def cog_unload(self):
        self.ticker.cancel()

    # ================================================================== ロックダウン
    def is_locked(self, gid: int) -> dict | None:
        return self.state()["lock"].get(str(gid))

    async def lock(self, guild: discord.Guild, by: str, reason: str, minutes: int | None = None) -> str:
        if guild.id in self._busy:
            raise ValueError("ロックダウンの処理中です。少し待ってください。")
        if self.is_locked(guild.id):
            raise ValueError("すでにロックダウン中です。")
        me = guild.me
        if not me.guild_permissions.manage_channels and not me.guild_permissions.administrator:
            raise ValueError("Botに「チャンネルの管理」権限がありません。")
        c = self.cfg(guild.id, "lockdown")
        excl = {int(x) for x in c.get("exclude", []) if str(x).isdigit()}
        roles = self.lock_roles(guild)
        if not roles:
            raise ValueError("ロックダウンするロールが選ばれていません。ダッシュボードで選んでください。")
        self._busy.add(guild.id)
        saved, failed = {}, 0
        try:
            targets = [ch for ch in guild.channels
                       if isinstance(ch, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel))
                       and ch.id not in excl and (ch.category_id or 0) not in excl]
            for ch in targets:
                rec = {}
                for role in roles:  # 選んだロールごとに、元の権限を覚えてから止める
                    ow = ch.overwrites_for(role)
                    rec[str(role.id)] = {p: getattr(ow, p) for p in LOCK_PERMS}
                    for p in LOCK_PERMS:
                        setattr(ow, p, False)
                    try:
                        await ch.set_permissions(role, overwrite=ow, reason=f"ロックダウン: {reason}"[:500])
                    except discord.HTTPException:
                        rec.pop(str(role.id), None)
                        failed += 1
                    await asyncio.sleep(0.25)
                if rec:
                    saved[str(ch.id)] = rec
        finally:
            self._busy.discard(guild.id)
        st = self.state()
        st["lock"][str(guild.id)] = {"channels": saved, "at": time.time(), "by": by, "reason": reason,
                                     "until": time.time() + minutes * 60 if minutes else 0}
        self.save_state(st)
        names = "、".join("@everyone" if r.is_default() else f"@{r.name}" for r in roles)
        msg = (f"🔒 **サーバーをロックダウンしました**（{len(saved)}チャンネル・対象: {names}）\n理由: {reason}"
               + (f"\n⏱️ {minutes}分後に自動で解除します" if minutes else ""))
        await self._announce(guild, msg)
        await self.log(guild, "🔒 ロックダウン", f"{msg}\n実行: {by}" + (f"\n⚠️ 変更できなかったチャンネル: {failed}" if failed else ""),
                       0xEF4444)
        return f"{len(saved)}チャンネルをロックダウンしました" + (f"（{minutes}分後に自動解除）" if minutes else "") \
            + (f"・{failed}か所は権限不足で変更できませんでした" if failed else "")

    async def unlock(self, guild: discord.Guild, by: str) -> str:
        st = self.state()
        rec = st["lock"].pop(str(guild.id), None)
        if not rec:
            raise ValueError("ロックダウンしていません。")
        self.save_state(st)
        restored = 0
        for cid, per_role in rec.get("channels", {}).items():
            ch = guild.get_channel(int(cid))
            if ch is None:
                continue
            if per_role and all(k in LOCK_PERMS for k in per_role):  # 以前の形式（@everyone だけ）
                per_role = {str(guild.default_role.id): per_role}
            for rid, perms in per_role.items():
                role = guild.get_role(int(rid))
                if role is None:
                    continue
                ow = ch.overwrites_for(role)
                for p, v in perms.items():
                    setattr(ow, p, v)
                try:
                    await ch.set_permissions(role, overwrite=None if ow.is_empty() else ow, reason="ロックダウン解除")
                except discord.HTTPException:
                    pass
                await asyncio.sleep(0.25)
            restored += 1
        await self._announce(guild, "🔓 **ロックダウンを解除しました**。ご協力ありがとうございました。")
        await self.log(guild, "🔓 ロックダウン解除", f"{restored}チャンネルを元に戻しました\n実行: {by}", 0x22C55E)
        return f"ロックダウンを解除しました（{restored}チャンネルを元に戻しました）"

    def lock_roles(self, guild: discord.Guild) -> list[discord.Role]:
        """ロックダウンの対象にするロール（"everyone" は @everyone）。"""
        out = []
        for x in self.cfg(guild.id, "lockdown").get("roles", ["everyone"]):
            r = guild.default_role if str(x) in ("everyone", str(guild.id)) else (
                guild.get_role(int(x)) if str(x).isdigit() else None)
            if r is not None and r not in out and (r.is_default() or r < guild.me.top_role):
                out.append(r)
        return out

    async def _announce(self, guild: discord.Guild, text: str) -> None:
        c = self.cfg(guild.id, "lockdown")
        ch = guild.get_channel(int(c["announce"])) if str(c.get("announce") or "").isdigit() else None
        if ch is None:
            return
        try:
            await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_reimu_raid(self, guild: discord.Guild):
        """Raid（大量参加）を検知したら、設定に応じて自動でロックダウンする。"""
        c = self.cfg(guild.id, "lockdown")
        if not c.get("auto_raid") or self.is_locked(guild.id):
            return
        try:
            await self.lock(guild, "自動（Raid検知）", "Raid（大量参加）を検知したため", int(c.get("auto_minutes") or 30))
        except Exception as e:
            print(f"自動ロックダウンに失敗（{guild.id}）: {type(e).__name__}")

    @lockdown.command(name="on", description="サーバー全体の書き込みを止めます")
    @app_commands.describe(minutes="自動で解除するまでの分数（省略すると手動で解除するまで）", reason="理由")
    async def lock_cmd(self, interaction: discord.Interaction,
                       minutes: app_commands.Range[int, 1, 1440] | None = None, reason: str | None = None):
        view = LockConfirm(interaction.user.id)
        names = "、".join("@everyone" if r.is_default() else f"@{r.name}" for r in self.lock_roles(interaction.guild)) or "（未選択）"
        await interaction.response.send_message(f"🔒 **書き込みを止めます。よろしいですか？**\n対象のロール: {names}\n"
                                                "-# 対象のロールは、ダッシュボードの「セキュリティ」→「ロックダウン」で選べます。",
                                                view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        await view.wait()
        if not view.ok:
            return
        try:
            msg = await self.lock(interaction.guild, f"{interaction.user.mention}", (reason or "手動")[:200], minutes)
        except ValueError as e:
            msg = f"❌ {e}"
        await interaction.edit_original_response(content=msg)

    @lockdown.command(name="off", description="ロックダウンを解除します")
    async def unlock_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            msg = await self.unlock(interaction.guild, interaction.user.mention)
        except ValueError as e:
            msg = f"❌ {e}"
        await interaction.followup.send(msg, ephemeral=True)

    # ================================================================== 新規アカウントの隔離
    def is_quarantined(self, gid: int, uid: int) -> dict | None:
        return self.state()["quar"].get(str(gid), {}).get(str(uid))

    async def setup_quarantine(self, guild: discord.Guild, channel: discord.TextChannel | None) -> str:
        """隔離ロールを作り、隔離チャンネル以外を見えなくする。"""
        me = guild.me
        if not (me.guild_permissions.manage_roles and me.guild_permissions.manage_channels):
            raise ValueError("Botに「ロールの管理」「チャンネルの管理」権限が必要です。")
        c = self.cfg(guild.id, "quarantine")
        role = guild.get_role(int(c["role"])) if str(c.get("role") or "").isdigit() else None
        if role is None:
            role = await guild.create_role(name="🚧 隔離中", reason="新規アカウントの隔離", colour=discord.Colour(0x6B7280))
            try:  # Botのロールのすぐ下に置く（Botが付け外しできるように）
                await role.edit(position=max(1, me.top_role.position - 1))
            except discord.HTTPException:
                pass
        if channel is None:
            channel = await guild.create_text_channel("🚧隔離中の方へ", reason="新規アカウントの隔離",
                                                      position=0, overwrites={
                                                          guild.default_role: discord.PermissionOverwrite(view_channel=False),
                                                          role: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                                                            read_message_history=True),
                                                          me: discord.PermissionOverwrite(view_channel=True, send_messages=True)})
            try:
                await channel.send("🚧 **新しく作られたアカウントのため、一時的に隔離しています。**\n"
                                   "しばらくすると自動で解除されます。急ぎの場合は、ここで運営にお知らせください。")
            except discord.HTTPException:
                pass
        n = 0
        for ch in guild.channels:
            if ch.id == channel.id:
                continue
            try:  # カテゴリを変えても中のチャンネルには反映されないので、全部に設定する
                await ch.set_permissions(role, view_channel=False, send_messages=False, connect=False,
                                         reason="新規アカウントの隔離")
                n += 1
            except discord.HTTPException:
                pass
            await asyncio.sleep(0.25)
        try:
            await channel.set_permissions(role, view_channel=True, send_messages=True, read_message_history=True)
        except discord.HTTPException:
            pass
        c.update({"role": str(role.id), "channel": str(channel.id)})
        self.save_cfg(guild.id, "quarantine", c)
        return f"隔離ロール「{role.name}」と隔離チャンネル #{channel.name} を用意しました（{n}か所の見え方を変更）"

    async def quarantine(self, member: discord.Member, reason: str) -> str:
        guild = member.guild
        c = self.cfg(guild.id, "quarantine")
        action = c.get("action", "role")
        min_days = int(c.get("min_days") or 7)
        until = member.created_at.timestamp() + min_days * 86400
        rh = int(c.get("release_hours") or 0)
        if rh:
            until = min(until, time.time() + rh * 3600)
        if action == "kick":
            try:
                await member.send(f"🚧 **{guild.name}** は、作成から {min_days}日未満のアカウントの参加を制限しています。"
                                  f"<t:{int(member.created_at.timestamp() + min_days * 86400)}:f> 以降に、もう一度参加してください。")
            except discord.HTTPException:
                pass
            await member.kick(reason=f"新規アカウントの制限（作成から{min_days}日未満）")
            return "キック"
        if action == "timeout":
            hours = max(1, min(int(c.get("timeout_hours") or 24), 24 * 28))
            await member.timeout(discord.utils.utcnow() + timedelta(hours=hours),
                                 reason=f"新規アカウントの制限（作成から{min_days}日未満）")
            return f"{hours}時間のタイムアウト"
        role = guild.get_role(int(c["role"])) if str(c.get("role") or "").isdigit() else None
        if role is None:
            raise ValueError("隔離ロールが設定されていません")
        await member.add_roles(role, reason=reason)
        st = self.state()
        st["quar"].setdefault(str(guild.id), {})[str(member.id)] = {"at": time.time(), "until": until, "held": [],
                                                                    "reason": reason}
        self.save_state(st)
        return f"隔離（<t:{int(until)}:R>に自動解除）"

    async def release(self, guild: discord.Guild, uid: int, by: str) -> str:
        st = self.state()
        rec = st["quar"].get(str(guild.id), {}).pop(str(uid), None)
        self.save_state(st)
        if rec is None:
            raise ValueError("そのメンバーは隔離されていません")
        member = guild.get_member(uid)
        if member is None:
            return "隔離の記録を消しました（すでにサーバーにいません）"
        c = self.cfg(guild.id, "quarantine")
        role = guild.get_role(int(c["role"])) if str(c.get("role") or "").isdigit() else None
        try:
            if role and role in member.roles:
                await member.remove_roles(role, reason=f"隔離の解除（{by}）")
            held = [guild.get_role(int(r)) for r in rec.get("held", [])]
            held = [r for r in held if r and r < guild.me.top_role and not r.managed]
            if held:
                await member.add_roles(*held, reason="隔離中に預かったロールを返す")
        except discord.HTTPException:
            pass
        await self.log(guild, "✅ 隔離を解除", f"{member.mention}（`{member.id}`）\n実行: {by}", 0x22C55E)
        return f"{member.display_name} の隔離を解除しました"

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        c = self.cfg(member.guild.id, "quarantine")
        if not c.get("enabled"):
            return
        age_days = (discord.utils.utcnow() - member.created_at).total_seconds() / 86400
        if age_days >= int(c.get("min_days") or 7):
            return
        try:
            result = await self.quarantine(member, f"新規アカウント（作成から{age_days:.1f}日）")
        except Exception as e:
            result = f"失敗（{type(e).__name__}）"
        await self.log(member.guild, "🚧 新規アカウントを隔離",
                       f"{member.mention}（`{member.id}`）\nアカウント作成: <t:{int(member.created_at.timestamp())}:R>\n対処: {result}",
                       0xF59E0B)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """隔離中に付けられたロール（認証など）は預かって、解除のときに返す。"""
        rec = self.is_quarantined(after.guild.id, after.id)
        if not rec:
            return
        c = self.cfg(after.guild.id, "quarantine")
        qrole = int(c["role"]) if str(c.get("role") or "").isdigit() else 0
        added = [r for r in after.roles if r not in before.roles and r.id != qrole and not r.managed
                 and r < after.guild.me.top_role]
        if not added:
            return
        try:
            await after.remove_roles(*added, reason="隔離中のため、解除まで預かります")
        except discord.HTTPException:
            return
        st = self.state()
        r = st["quar"].get(str(after.guild.id), {}).get(str(after.id))
        if r is not None:
            r["held"] = sorted(set(r.get("held", [])) | {str(x.id) for x in added})
            self.save_state(st)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        st = self.state()
        if st["quar"].get(str(member.guild.id), {}).pop(str(member.id), None) is not None:
            self.save_state(st)

    # ================================================================== 一時VC
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        guild = member.guild
        c = self.cfg(guild.id, "tempvc")
        st = None
        # 作成用VCに入った → 専用VCを作る
        if c.get("enabled") and after.channel and str(after.channel.id) == str(c.get("hub")) and not member.bot:
            if time.time() - self._vc_cool.get(member.id, 0) < 10:
                return  # 連続で作れないように
            self._vc_cool[member.id] = time.time()
            hub = after.channel
            cat = guild.get_channel(int(c["category"])) if str(c.get("category") or "").isdigit() else hub.category
            name = (c.get("name") or "🔊 {user}のVC").replace("{user}", member.display_name)[:95]
            ow = dict(cat.overwrites) if isinstance(cat, discord.CategoryChannel) else dict(hub.overwrites)
            ow[member] = discord.PermissionOverwrite(manage_channels=True, move_members=True, mute_members=True,
                                                     connect=True, view_channel=True)
            ow[guild.me] = discord.PermissionOverwrite(manage_channels=True, move_members=True, connect=True, view_channel=True)
            try:
                ch = await guild.create_voice_channel(name, category=cat if isinstance(cat, discord.CategoryChannel) else None,
                                                      overwrites=ow, user_limit=int(c.get("limit") or 0),
                                                      bitrate=min(hub.bitrate, guild.bitrate_limit),
                                                      reason=f"一時VC（{member}）")
                await member.move_to(ch, reason="一時VCへ移動")
            except discord.HTTPException as e:
                print(f"一時VCを作れませんでした（{guild.id}）: {e}")
                return
            st = self.state()
            st["vc"].setdefault(str(guild.id), {})[str(ch.id)] = {"owner": member.id, "at": time.time()}
            self.save_state(st)
            try:
                await ch.send(f"🔊 {member.mention} 専用のVCです。名前・人数制限などはチャンネルの設定から変えられます。"
                              "誰もいなくなると自動で消えます。", allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        # 一時VCから抜けた → 空なら消す / 作った人が抜けたら引き継ぐ
        if before.channel and (before.channel != after.channel):
            st = st or self.state()
            rec = st["vc"].get(str(guild.id), {}).get(str(before.channel.id))
            if rec is None:
                return
            ch = before.channel
            humans = [m for m in ch.members if not m.bot]
            if not humans:
                await self._delete_vc(guild, ch.id)
            elif rec.get("owner") == member.id:
                new_owner = humans[0]
                try:
                    await ch.set_permissions(member, overwrite=None, reason="一時VCの持ち主が退出")
                    await ch.set_permissions(new_owner, manage_channels=True, move_members=True, mute_members=True,
                                             connect=True, view_channel=True, reason="一時VCの持ち主を引き継ぎ")
                except discord.HTTPException:
                    pass
                rec["owner"] = new_owner.id
                self.save_state(st)
                try:
                    await ch.send(f"👑 このVCの管理を {new_owner.mention} に引き継ぎました。",
                                  allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass

    async def _delete_vc(self, guild: discord.Guild, cid: int) -> None:
        st = self.state()
        st["vc"].get(str(guild.id), {}).pop(str(cid), None)
        self.save_state(st)
        ch = guild.get_channel(cid)
        if ch is not None:
            try:
                await ch.delete(reason="一時VC: 誰もいなくなったため")
            except discord.HTTPException:
                pass

    # ================================================================== 定期処理
    @tasks.loop(minutes=1)
    async def ticker(self):
        now = time.time()
        st = self.state()
        for gid, rec in list(st["lock"].items()):  # ロックダウンの自動解除
            if rec.get("until") and rec["until"] <= now:
                g = self.bot.get_guild(int(gid))
                if g:
                    try:
                        await self.unlock(g, "自動（時間切れ）")
                    except Exception:
                        pass
        st = self.state()
        for gid, users in list(st["quar"].items()):  # 隔離の自動解除
            g = self.bot.get_guild(int(gid))
            if g is None:
                continue
            for uid, rec in list(users.items()):
                if rec.get("until") and rec["until"] <= now:
                    try:
                        await self.release(g, int(uid), "自動（期間が過ぎたため）")
                    except Exception:
                        pass
        st = self.state()
        for gid, chans in list(st["vc"].items()):  # 取り残された一時VCの掃除（再起動の間に空になったもの）
            g = self.bot.get_guild(int(gid))
            if g is None:
                continue
            for cid in list(chans):
                ch = g.get_channel(int(cid))
                if ch is None:
                    chans.pop(cid, None)
                    self.save_state(st)
                elif not [m for m in ch.members if not m.bot] and now - chans[cid].get("at", 0) > 60:
                    await self._delete_vc(g, int(cid))

    @ticker.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ================================================================== ダッシュボード
    def module_states(self, gid: int) -> dict:
        q = self.cfg(gid, "quarantine")
        v = self.cfg(gid, "tempvc")
        lk = self.is_locked(gid)
        return {"lockdown": (bool(lk), "（ロックダウン中）" if lk else ("（Raid時に自動）" if self.cfg(gid, "lockdown")["auto_raid"] else "")),
                "quarantine": (bool(q["enabled"]), f"（作成から{q['min_days']}日未満）"),
                "tempvc": (bool(v["enabled"]), "")}

    def toggle(self, gid: int, key: str, on: bool) -> str:
        part = {"quarantine": "quarantine", "tempvc": "tempvc"}[key]
        c = self.cfg(gid, part)
        if on and part == "quarantine" and c["action"] == "role" and not c.get("role"):
            raise ValueError("先に「セキュリティ」タブで隔離ロールを用意してください")
        if on and part == "tempvc" and not c.get("hub"):
            raise ValueError("先に「配信・VC」タブで作成用VCを選んでください")
        c["enabled"] = on
        self.save_cfg(gid, part, c)
        return "有効" if on else "無効"

    def _roles(self, guild, H, cur):
        return '<option value="">（自動で作る）</option>' + "".join(
            H.opt(str(r.id), "@" + r.name, cur or "") for r in reversed(guild.roles)
            if not r.is_default() and not r.managed and r < guild.me.top_role)

    def render_security(self, gid: int, guild: discord.Guild, H) -> str:
        lc = self.cfg(gid, "lockdown")
        lk = self.is_locked(gid)
        excl = {str(x) for x in lc.get("exclude", [])}
        ex_box = "".join(H.chk(f"x_{c.id}", str(c.id) in excl, ("📁 " if isinstance(c, discord.CategoryChannel) else "#") + c.name)
                         for c in sorted(guild.channels, key=lambda c: (c.position, c.id))
                         if isinstance(c, (discord.CategoryChannel, discord.TextChannel, discord.ForumChannel)))
        status = (f'<div class="flash ng">🔒 ロックダウン中（{H.e(lk.get("reason", ""))}）</div>'
                  + H.form("st_unlock", "", "ロックダウンを解除する", cls="btn", need="manage_guild")) if lk else \
            H.form("st_lock", H.lbl("自動で解除するまでの分数（空欄なら手動で解除）")
                   + '<input type="number" name="minutes" min="1" max="1440">'
                   + H.chk("confirm", False, "サーバー全体の書き込みを止めます（確認）"),
                   "今すぐロックダウンする", cls="btn danger", need="manage_guild")
        sel_roles = {str(x) for x in lc.get("roles", ["everyone"])}
        role_box = H.chk("r_everyone", "everyone" in sel_roles or str(guild.id) in sel_roles, "@everyone（全員）") + "".join(
            H.chk(f"r_{r.id}", str(r.id) in sel_roles, "@" + r.name) for r in reversed(guild.roles)
            if not r.is_default() and not r.managed and r < guild.me.top_role)
        lock_card = H.card("🔒 ロックダウン", status + H.form(
            "st_lock_save",
            H.lbl("書き込みを止めるロール（複数選べます）")
            + '<div style="max-height:220px;overflow:auto;border:1px solid var(--line,#263243);border-radius:10px;padding:6px 10px">'
            + role_box + "</div>"
            + H.chk("auto_raid", lc["auto_raid"], "Raid（大量参加）を検知したら、自動でロックダウンする")
            + H.lbl("自動ロックダウンを解除するまでの分数") + f'<input type="number" name="auto_minutes" min="1" max="1440" value="{int(lc["auto_minutes"])}">'
            + H.lbl("お知らせを送るチャンネル（任意）") + H.sel("announce", H.channel_options(lc.get("announce") or None, none_label="（送らない）"))
            + H.lbl("ロックしないチャンネル・カテゴリ（運営用など）")
            + '<div style="max-height:200px;overflow:auto;border:1px solid var(--line,#263243);border-radius:10px;padding:6px 10px">'
            + ex_box + "</div>", "保存する", need="manage_guild"), need="manage_guild",
            note="選んだロールの、全チャンネルでの書き込み・スレッド作成・リアクションを止めます。解除すると元の権限にそのまま戻します。"
                 "ロールを持つ人は、ほかのロールで許可されていると書き込めることがあるので、@everyone も一緒に選ぶのがおすすめです。"
                 "ロックダウン中に選んだロールを変えても、解除は正しく元に戻ります。")
        qc = self.cfg(gid, "quarantine")
        qs = self.state()["quar"].get(str(gid), {})
        q_list = "".join(
            f'<div class="row" style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin:6px 0">'
            f'<span><code>{H.e(uid)}</code> {H.e(getattr(guild.get_member(int(uid)), "display_name", ""))}'
            f'<span class="mut"> ・ 自動解除 {datetime.fromtimestamp(r.get("until", 0), timezone.utc).strftime("%m/%d %H:%M")} UTC</span></span>'
            + H.form("st_release", f'<input type="hidden" name="uid" value="{H.e(uid)}">', "解除", cls="btn", need="moderate")
            + "</div>" for uid, r in list(qs.items())[:30]) or '<p class="mut">隔離中のメンバーはいません。</p>'
        acts = H.opt("role", "隔離ロールを付ける（おすすめ）", qc["action"]) + H.opt("timeout", "タイムアウト", qc["action"]) \
            + H.opt("kick", "キック（DMで理由を伝える）", qc["action"])
        q_card = H.card("🚧 新規アカウントの隔離", H.form(
            "st_q_save",
            H.chk("enabled", qc["enabled"], "作られて間もないアカウントを隔離する")
            + H.lbl("アカウント作成からの日数（これ未満を隔離）") + f'<input type="number" name="min_days" min="1" max="365" value="{int(qc["min_days"])}">'
            + H.lbl("対処") + H.sel("action", acts)
            + H.lbl("隔離を解除するまでの時間（時間・0ならアカウントが日数を超えたとき）")
            + f'<input type="number" name="release_hours" min="0" max="8760" value="{int(qc["release_hours"])}">'
            + H.lbl("タイムアウトの時間（時間・タイムアウトのとき）") + f'<input type="number" name="timeout_hours" min="1" max="672" value="{int(qc["timeout_hours"])}">'
            + H.lbl("隔離ロール") + f'<select name="role">{self._roles(guild, H, qc.get("role"))}</select>'
            + H.lbl("隔離チャンネル（隔離中の人だけが見られる）") + H.sel("channel", H.channel_options(qc.get("channel") or None, none_label="（自動で作る）")),
            "保存する", need="manage_guild")
            + H.form("st_q_setup", "", "隔離ロール・チャンネルを用意する（見え方を自動設定）", cls="btn", need="admin")
            + "<h3 style=\"margin:16px 0 6px;font-size:15px\">隔離中のメンバー</h3>" + q_list, need="manage_guild",
            note="隔離中は、隔離チャンネル以外が見えなくなります。認証などで付いたロールは預かり、解除のときに返します。")
        return lock_card + q_card

    def render_media(self, gid: int, guild: discord.Guild, H) -> str:
        vc = self.cfg(gid, "tempvc")
        cats = '<option value="">（作成用VCと同じカテゴリ）</option>' + "".join(
            H.opt(str(c.id), "📁 " + c.name, vc.get("category") or "") for c in guild.categories)
        active = len(self.state()["vc"].get(str(gid), {}))
        return H.card("➕ 一時VC", H.form(
            "st_vc_save",
            H.chk("enabled", vc["enabled"], "一時VCを使う")
            + H.lbl("作成用VC（ここに入ると専用VCができる）") + f'<select name="hub">{H.voice_options(int(vc["hub"]) if str(vc.get("hub") or "").isdigit() else None)}</select>'
            + H.lbl("作る場所のカテゴリ") + f'<select name="category">{cats}</select>'
            + H.lbl("チャンネル名（{user} が名前になります）") + f'<input type="text" name="name" maxlength="90" value="{H.e(vc["name"])}">'
            + H.lbl("人数制限（0で無制限）") + f'<input type="number" name="limit" min="0" max="99" value="{int(vc["limit"])}">',
            "保存する", need="manage_guild"), need="manage_guild",
            note=f"作った人は名前・人数制限・メンバーの移動などを自分で変えられます。誰もいなくなると自動で消えます。いま {active}個 あります。")

    async def dashboard_op(self, op: str, f: dict, guild: discord.Guild, member: discord.Member, P) -> str:
        gid = guild.id
        by = f"{member.mention}（ダッシュボード）"
        if op == "st_lock":
            if f.get("confirm") != "1":
                raise ValueError("確認にチェックしてください")
            m = (f.get("minutes") or "").strip()
            if m and (not m.isdigit() or not 1 <= int(m) <= 1440):
                raise ValueError("分数は 1〜1440 で指定してください")
            return await self.lock(guild, by, "ダッシュボードから", int(m) if m else None)
        if op == "st_unlock":
            return await self.unlock(guild, by)
        if op == "st_lock_save":
            c = self.cfg(gid, "lockdown")
            am = (f.get("auto_minutes") or "30").strip()
            if not am.isdigit() or not 1 <= int(am) <= 1440:
                raise ValueError("分数は 1〜1440 で指定してください")
            ann = P.pick_channel(f.get("announce"))
            valid = {c2.id for c2 in guild.channels}
            vroles = {r.id for r in guild.roles if not r.is_default() and not r.managed and r < guild.me.top_role}
            roles = (["everyone"] if f.get("r_everyone") == "1" else []) + [
                k[2:] for k in f if k.startswith("r_") and k[2:].isdigit() and int(k[2:]) in vroles]
            if not roles:
                raise ValueError("書き込みを止めるロールを1つ以上選んでください")
            c.update({"roles": roles[:50], "auto_raid": f.get("auto_raid") == "1", "auto_minutes": int(am),
                      "announce": str(ann.id) if ann else "",
                      "exclude": [k[2:] for k in f if k.startswith("x_") and k[2:].isdigit() and int(k[2:]) in valid][:200]})
            self.save_cfg(gid, "lockdown", c)
            return f"ロックダウンの設定を保存しました（対象ロール {len(roles)}個）"
        if op == "st_q_save":
            c = self.cfg(gid, "quarantine")
            md, rh, th = (f.get("min_days") or "").strip(), (f.get("release_hours") or "0").strip(), (f.get("timeout_hours") or "24").strip()
            if not md.isdigit() or not 1 <= int(md) <= 365:
                raise ValueError("日数は 1〜365 で指定してください")
            if not rh.isdigit() or int(rh) > 8760 or not th.isdigit() or not 1 <= int(th) <= 672:
                raise ValueError("時間の指定が正しくありません")
            action = f.get("action") if f.get("action") in ("role", "timeout", "kick") else "role"
            role = (f.get("role") or "").strip()
            if role and not (role.isdigit() and guild.get_role(int(role))):
                raise ValueError("ロールが見つかりません")
            ch = P.pick_channel(f.get("channel"))
            on = f.get("enabled") == "1"
            if on and action == "role" and not role:
                raise ValueError("隔離ロールを選ぶか、「隔離ロール・チャンネルを用意する」を押してください")
            c.update({"enabled": on, "min_days": int(md), "release_hours": int(rh), "timeout_hours": int(th),
                      "action": action, "role": role, "channel": str(ch.id) if ch else ""})
            self.save_cfg(gid, "quarantine", c)
            return f"新規アカウントの隔離を保存しました（{'有効' if on else '無効'}・{md}日未満）"
        if op == "st_q_setup":
            c = self.cfg(gid, "quarantine")
            ch = guild.get_channel(int(c["channel"])) if str(c.get("channel") or "").isdigit() else None
            return await self.setup_quarantine(guild, ch if isinstance(ch, discord.TextChannel) else None)
        if op == "st_release":
            uid = (f.get("uid") or "").strip()
            if not uid.isdigit():
                raise ValueError("ユーザーIDが正しくありません")
            return await self.release(guild, int(uid), by)
        if op == "st_vc_save":
            c = self.cfg(gid, "tempvc")
            hub = P.pick_channel(f.get("hub"), voice=True)
            cat = (f.get("category") or "").strip()
            if cat and not (cat.isdigit() and isinstance(guild.get_channel(int(cat)), discord.CategoryChannel)):
                raise ValueError("カテゴリが見つかりません")
            lim = (f.get("limit") or "0").strip()
            if not lim.isdigit() or int(lim) > 99:
                raise ValueError("人数制限は 0〜99 で指定してください")
            name = (f.get("name") or "").strip()[:90] or DEFAULTS["tempvc"]["name"]
            on = f.get("enabled") == "1"
            if on and hub is None:
                raise ValueError("作成用VCを選んでください")
            c.update({"enabled": on, "hub": str(hub.id) if hub else "", "category": cat, "name": name, "limit": int(lim)})
            self.save_cfg(gid, "tempvc", c)
            return f"一時VCの設定を保存しました（{'有効' if on else '無効'}）"
        raise ValueError("不明な操作です")


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerTools(bot))
