"""/help — 全コマンドの一覧と使い方ガイド（登録済みコマンドから自動生成）。"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

CATEGORIES = [
    ("🔐 認証", ["setup", "authlink", "panel", "subaccount", "server-blacklist"]),
    ("🛡️ セキュリティ", ["nuke-defense", "nuke-whitelist", "backup", "set-log-channel", "automod", "eventlog"]),
    ("🔨 モデレーション", ["warn", "warnings", "clearwarnings", "timeout", "untimeout", "kick", "ban", "unban", "mass_role"]),
    ("🎫 便利機能", ["ticket", "transcript-channel", "reactionrole", "sticky", "invite-log", "invite-stats", "weather",
                    "disaster", "broadcast-channel", "help"]),
    ("📈 レベル・ランク", ["rank", "leaderboard", "level"]),
    ("👑 オーナー専用", ["broadcast", "blacklist", "license", "gban"]),
]
OWNER_CAT = "👑 オーナー専用"
GUIDE = {
    "rank": "メッセージ送信（1分ごと）とVC参加（1分ごと）で経験値が貯まり、レベルが上がります。"
            "`/rank` でレベル・順位・次のレベルまでの進捗バー・次のランクまでのXP・活動量を表示します。",
    "leaderboard": "サーバー内のレベル上位15人と、自分の順位を表示します。",
    "level": "（管理者）`settings` でON/OFF・通知先・XP倍率、`role` でレベル到達時のランクロール、"
             "`no-xp` でXP対象外のチャンネル、`set` でレベルの変更・リセット。\n"
             "※ VCのXPは、2人以上いるVCでミュートしていない人だけが対象です",
    "sticky": "① `/sticky set text:本文` で登録（`title` を入れると埋め込み、`button_label`+`button_url` でリンクボタン）\n"
              "② 誰かが投稿するたびに、古い案内を消して一番下に貼り直します\n"
              "・`/sticky copy message_id:ID` で既存メッセージを流用 ・`/sticky remove` で解除 ・`/sticky list` で一覧",
    "invite-log": "招待リンクの作成と、メンバー参加時の招待元（作成者・作成日時）のログを送るチャンネルを設定します。"
                  "未設定なら `/set-log-channel` のチャンネルに送ります。",
    "invite-stats": "招待リンクの作成（URL・有効期限・作成者の累計作成数）と、メンバー参加時の招待元（作成者・作成日時）を"
                    "ログチャンネルに自動で記録します。認証ログにも招待元が表示されます。\n"
                    "・`/invite-stats user:@ユーザー` で累計作成数と有効なリンクを確認\n"
                    "※ Botに「サーバーの管理」権限が必要です",
    "setup": "認証で付与するロールと、認証ログを送るチャンネルを設定します。最初に実行してください。",
    "panel": "「認証する」ボタン付きのパネルを送ります。メンバーはボタンからDiscordでログインして認証します。",
    "subaccount": "同じIPから別アカウントが認証したとき「拒否」か「記録のみ」かを選べます。BAN・ブラックリスト対象と同じIPは常に拒否されます。",
    "nuke-defense": "大量削除・連続BANなどを検知し、実行者を自動でBAN/キック/権限剥奪し、消えたチャンネル・ロールを復元します。Botのロールは一番上に置いてください。",
    "nuke-whitelist": "Nuke検知の対象外にする信頼済みのユーザー・BOTを登録します。",
    "backup": "`create` で保存、`list` で一覧、`restore number:番号` で足りないロール・チャンネルを作り直します。",
    "transcript-channel": "チケットのトランスクリプト（HTML＋テキスト）を送るチャンネルを設定します。",
    "reactionrole": "`menu-create` → `menu-add`（一度に5個まで追加）→ `menu-send` で、何個でも同時に選べるロールメニューを作れます。従来の絵文字リアクション方式は `add` / `remove` / `list`。",
    "weather": "`set channel times:07:00,18:00` で全国の天気予報を毎日自動投稿します。`now` で今すぐ表示。",
    "disaster": "`set` で津波・火山・気象警報（と地震）を自動通知します。\n"
                "`quake` で地震情報だけを別チャンネルに分離（最小震度・メンションロールも個別に設定）、`quake-off` で分離解除。\n"
                "`test` で両方の通知先に動作確認、`off` で全停止。",
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
            if c is None or len(e.fields) >= 24:
                continue
            body = f"💡 {GUIDE[n]}\n" if n in GUIDE else ""
            if isinstance(c, app_commands.Group):
                body += "\n".join(f"`/{s.qualified_name}` {s.description}" for s in c.walk_commands()
                                   if isinstance(s, app_commands.Command))
            else:
                body += f"`{_usage(c)}`\n{c.description}"
            # 1項目1024文字の上限を超える場合は、行の区切りで分けて続きの項目にする
            chunk, part = "", 0
            for line in body.split("\n"):
                if len(chunk) + len(line) + 1 > 1000:
                    e.add_field(name=f"/{n}" + (" （続き）" if part else ""), value=chunk, inline=False)
                    chunk, part = "", part + 1
                chunk += line + "\n"
            if chunk.strip():
                e.add_field(name=f"/{n}" + (" （続き）" if part else ""), value=chunk, inline=False)
        e.set_footer(text="/help command:コマンド名 で、引数ごとの詳しい説明を表示します")
        return e

    def _top_embed(self, is_owner: bool) -> discord.Embed:
        cmds = self._commands()
        known = {n for _, ns in CATEGORIES for n in ns}
        e = discord.Embed(title="📖 コマンド一覧と使い方", color=0x5865F2,
                          description="下のメニューでカテゴリを選ぶと、使い方を詳しく表示します。\n\n"
                                      "**はじめに**\n"
                                      "・認証の設置: `/setup` → `/panel`\n"
                                      "・荒らし対策: `/nuke-defense` → `/set-log-channel`\n"
                                      "・案内を一番下に固定: `/sticky set`\n"
                                      "・レベル・ランク: `/rank` ／ 設定は `/level`")
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
