"""/help — 全コマンドの一覧と使い方ガイド（登録済みコマンドから自動生成）。"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

CATEGORIES = [
    ("🔐 認証", ["setup", "authlink", "panel", "subaccount", "server-blacklist"]),
    ("🛡️ セキュリティ", ["nuke-defense", "nuke-whitelist", "backup", "set-log-channel", "automod", "eventlog"]),
    ("🔨 モデレーション", ["warn", "warnings", "clearwarnings", "timeout", "untimeout", "kick", "ban", "unban", "mass_role"]),
    ("🎫 便利機能", ["ticket", "transcript-channel", "reactionrole", "sticky", "invite-stats", "weather",
                    "disaster", "broadcast-channel", "help"]),
    ("🎵 VC（読み上げ・音楽）", ["tts", "music"]),
    ("👑 オーナー専用", ["broadcast", "blacklist", "license", "gban"]),
]
OWNER_CAT = "👑 オーナー専用"
GUIDE = {
    "sticky": "`set` で案内メッセージを登録すると、新しい投稿があるたびに古い案内を消して一番下に貼り直します。`copy` で既存メッセージを流用できます。",
    "invite-stats": "ユーザーが作成した招待リンクの累計数と、今有効なリンクを表示します。作成・参加の記録はログチャンネルに届きます。",
    "tts": "VCに入って `/tts join` を実行すると、そのチャンネルの投稿を読み上げます。`;` で始めると読み上げません。",
    "music": "`/music play 曲名またはURL` で再生。`skip` `pause` `resume` `queue` `loop` `volume` `stop` で操作します。",
    "setup": "認証で付与するロールと、認証ログを送るチャンネルを設定します。最初に実行してください。",
    "panel": "「認証する」ボタン付きのパネルを送ります。メンバーはボタンからDiscordでログインして認証します。",
    "subaccount": "同じIPから別アカウントが認証したとき「拒否」か「記録のみ」かを選べます。BAN・ブラックリスト対象と同じIPは常に拒否されます。",
    "nuke-defense": "大量削除・連続BANなどを検知し、実行者を自動でBAN/キック/権限剥奪し、消えたチャンネル・ロールを復元します。Botのロールは一番上に置いてください。",
    "nuke-whitelist": "Nuke検知の対象外にする信頼済みのユーザー・BOTを登録します。",
    "backup": "`create` で保存、`list` で一覧、`restore number:番号` で足りないロール・チャンネルを作り直します。",
    "transcript-channel": "チケットのトランスクリプト（HTML＋テキスト）を送るチャンネルを設定します。",
    "reactionrole": "`menu-create` → `menu-add`（ロールを追加）→ `menu-send` で、何個でも同時に選べるロールメニューを作れます。従来の絵文字リアクション方式は `add` / `remove` / `list`。",
    "weather": "`set channel times:07:00,18:00` で全国の天気予報を毎日自動投稿します。`now` で今すぐ表示。",
    "disaster": "`set` で地震（最小震度を指定）・津波・火山・気象警報を自動通知します。`test` で動作確認。",
    "license": "（オーナー）`suspend` でサーバーの全機能（自動処理を含む）を停止、`active` で再開します。",
    "gban": "（オーナー）全サーバー共通のBANです。同じIPからの認証もBAN回避としてブロックされます。",
}


def _usage(cmd) -> str:
    parts = []
    for p in getattr(cmd, "parameters", []):
        parts.append(f"{p.display_name}:" if p.required else f"[{p.display_name}:]")
    return f"/{cmd.qualified_name} " + " ".join(parts)


def _detail(cmd) -> str:
    lines = [f"**`{_usage(cmd)}`**", cmd.description or ""]
    for p in getattr(cmd, "parameters", []):
        lines.append(f"　・`{p.display_name}`{'（必須）' if p.required else ''}: {p.description or '-'}")
    return "\n".join(x for x in lines if x)


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _commands(self) -> dict[str, app_commands.Command | app_commands.Group]:
        return {c.name: c for c in self.bot.tree.get_commands()
                if isinstance(c, (app_commands.Command, app_commands.Group))}

    def _cat_embed(self, cat: str, names: list[str]) -> discord.Embed:
        cmds = self._commands()
        e = discord.Embed(title=f"{cat} のコマンド", color=0x5865F2)
        for n in names:
            c = cmds.get(n)
            if c is None:
                continue
            if isinstance(c, app_commands.Group):
                body = "\n".join(f"`/{s.qualified_name}` {s.description}" for s in c.walk_commands()
                                 if isinstance(s, app_commands.Command))
            else:
                body = f"`{_usage(c)}`\n{c.description}"
            if n in GUIDE:
                body += f"\n💡 {GUIDE[n]}"
            e.add_field(name=f"/{n}", value=body[:1024] or "-", inline=False)
        e.set_footer(text="/help command:コマンド名 で、引数ごとの詳しい説明を表示します")
        return e

    def _top_embed(self, is_owner: bool) -> discord.Embed:
        cmds = self._commands()
        known = {n for _, ns in CATEGORIES for n in ns}
        e = discord.Embed(title="📖 コマンド一覧と使い方", color=0x5865F2,
                          description="下のメニューでカテゴリを選ぶと、使い方を詳しく表示します。\n"
                                      "**はじめに:** `/setup` → `/panel` で認証を設置しましょう。")
        for cat, names in CATEGORIES:
            if cat == OWNER_CAT and not is_owner:
                continue
            have = [f"`/{n}`" for n in names if n in cmds]
            if have:
                e.add_field(name=cat, value=" ".join(have), inline=False)
        other = [f"`/{n}`" for n in cmds if n not in known]
        if other:
            e.add_field(name="📦 その他", value=" ".join(other)[:1024], inline=False)
        return e

    @app_commands.command(name="help", description="利用できる全コマンドの一覧と使い方を表示します")
    @app_commands.describe(command="詳しく知りたいコマンド名（省略で一覧）")
    async def help(self, interaction: discord.Interaction, command: str | None = None):
        is_owner = self.bot.is_owner_id(interaction.user.id) if hasattr(self.bot, "is_owner_id") else False
        cmds = self._commands()
        if command:
            c = cmds.get(command.strip().lstrip("/").split()[0])
            if c is None:
                await interaction.response.send_message("そのコマンドは見つかりません。", ephemeral=True)
                return
            subs = list(c.walk_commands()) if isinstance(c, app_commands.Group) else [c]
            text = "\n\n".join(_detail(s) for s in subs if isinstance(s, app_commands.Command))
            e = discord.Embed(title=f"/{c.name} の使い方", description=text[:3800], color=0x5865F2)
            if c.name in GUIDE:
                e.add_field(name="💡 ガイド", value=GUIDE[c.name], inline=False)
            await interaction.response.send_message(embed=e, ephemeral=True)
            return

        cats = [(c, n) for c, n in CATEGORIES if is_owner or c != OWNER_CAT]
        select = discord.ui.Select(placeholder="カテゴリを選択", options=[
            discord.SelectOption(label=c, value=str(i)) for i, (c, _) in enumerate(cats)])
        cog = self

        async def on_select(inter: discord.Interaction):
            c, names = cats[int(select.values[0])]
            await inter.response.edit_message(embed=cog._cat_embed(c, names))
        select.callback = on_select
        view = discord.ui.View(timeout=300)
        view.add_item(select)
        await interaction.response.send_message(embed=self._top_embed(is_owner), view=view, ephemeral=True)

    @help.autocomplete("command")
    async def _ac(self, interaction: discord.Interaction, current: str):
        return [app_commands.Choice(name=f"/{n}", value=n) for n in self._commands()
                if current.lower() in n][:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
