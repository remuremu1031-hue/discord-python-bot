"""
Reimu Support Bot（統合版）
FastAPI (Uvicorn) + discord.py + Upstash Redis / Render (Web Service)

機能:
    - GET / : ヘルスチェック（Renderのスリープ防止）
    - /faq : よくある質問
    - /setup-role-panel : ロール付与パネル（再起動後もボタン有効）
    - /announce : サポートサーバー限定アナウンス
    - /ping, /count : 動作確認・Redisサンプル

必要な環境変数:
    DISCORD_TOKEN              Discord Botのトークン
    SUPPORT_GUILD_ID           サポートサーバーのID
    UPSTASH_REDIS_REST_URL     Upstash RedisのREST URL
    UPSTASH_REDIS_REST_TOKEN   Upstash RedisのREST Token
    PORT                       Renderが自動設定（ローカルでは省略可）
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import discord
import uvicorn
from discord import app_commands
from discord.ext import commands
from fastapi import FastAPI
from upstash_redis.asyncio import Redis

# ========== ログ ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("reimu-support")


# ========== 環境変数 ==========
def require_env(name: str) -> str:
    """必須の環境変数を取得。無ければ値を出さずに名前だけ示して終了。"""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"環境変数 {name} が設定されていません。")
    return value


DISCORD_TOKEN = require_env("DISCORD_TOKEN")
REDIS_URL = require_env("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = require_env("UPSTASH_REDIS_REST_TOKEN")
PORT = int(os.getenv("PORT", "8000"))

try:
    SUPPORT_GUILD_ID = int(require_env("SUPPORT_GUILD_ID"))
except ValueError:
    raise RuntimeError("環境変数 SUPPORT_GUILD_ID は数値で指定してください。")
SUPPORT_GUILD = discord.Object(id=SUPPORT_GUILD_ID)


# ========== Upstash Redis ==========
redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)


async def init_redis() -> None:
    """起動時に接続確認（失敗してもBot自体は起動させる）"""
    try:
        await redis.ping()
        log.info("Upstash Redis に接続しました。")
    except Exception as e:
        # URLやトークンが出ないよう、例外の型だけ記録
        log.error("Upstash Redis への接続に失敗しました: %s", type(e).__name__)


# ========== Discord Bot ==========
class ReimuBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self) -> None:
        # 再起動後もロールボタンを復元（discord.py 2.4+）
        self.add_dynamic_items(RoleToggleButton)
        await self.tree.sync()                      # グローバルコマンド
        await self.tree.sync(guild=SUPPORT_GUILD)   # サポートサーバー専用コマンド
        log.info("スラッシュコマンドを同期しました。")


bot = ReimuBot()


@bot.event
async def on_ready() -> None:
    log.info("Discordにログインしました: %s", bot.user)


# ========== 動作確認・Redisサンプル ==========
@bot.tree.command(name="ping", description="BOTの応答確認とRedis接続を確認します")
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(bot.latency * 1000)
    try:
        await redis.ping()
        redis_status = "✅ 正常"
    except Exception:
        redis_status = "❌ 接続エラー"
    await interaction.response.send_message(
        f"🏓 Pong! `{latency_ms}ms`\nRedis: {redis_status}", ephemeral=True
    )


@bot.tree.command(name="count", description="コマンド実行回数をカウントします（Redisサンプル）")
@app_commands.guild_only()
async def count(interaction: discord.Interaction) -> None:
    key = f"reimu:{interaction.guild_id}:count"
    try:
        n = await redis.incr(key)
    except Exception:
        await interaction.response.send_message("❌ データの保存に失敗しました。", ephemeral=True)
        return
    await interaction.response.send_message(f"🔢 このサーバーでの実行回数: **{n}** 回")


# ========== FAQ ==========
FAQ_DATA = {
    "setup": {
        "title": "❓ NEXUSの導入手順",
        "description": (
            "1. 招待リンクからBOTをサーバーに追加\n"
            "2. `/set-report-channel` で毎日のレポート送信先を指定\n"
            "3. `/set-alert-channel` で管理者専用の危険検知ログ送信先を指定"
        ),
    },
    "auth": {
        "title": "❓ 認証BOTのエラー・トラブル対処",
        "description": (
            "・Web認証がうまくいかない場合は、Cookieやキャッシュの影響が考えられるため、"
            "プライベートブラウザでお試しください。\n"
            "・改善しない場合はサポートチケットでお問い合わせください。"
        ),
    },
    "terms": {
        "title": "📜 NEXUS利用規約",
        "description": (
            "NEXUSの利用規約は `/terms` コマンド、またはWebページ"
            "（https://health-bot-5ki5.onrender.com/terms）から確認できます。"
        ),
    },
}


@bot.tree.command(name="faq", description="よくある質問に自動で回答します")
@app_commands.describe(topic="知りたい項目を選んでください")
@app_commands.choices(topic=[
    app_commands.Choice(name="NEXUSの導入手順", value="setup"),
    app_commands.Choice(name="認証BOTのエラー・トラブル対処", value="auth"),
    app_commands.Choice(name="利用規約の確認方法", value="terms"),
])
async def faq(interaction: discord.Interaction, topic: app_commands.Choice[str]) -> None:
    data = FAQ_DATA.get(topic.value)
    if data is None:
        await interaction.response.send_message("❌ 該当するFAQが見つかりません。", ephemeral=True)
        return
    embed = discord.Embed(
        title=data["title"],
        description=data["description"],
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="NEXUS サポート • FAQ")
    await interaction.response.send_message(embed=embed)


# ========== ロールパネル ==========
class RoleToggleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"nexus:role:(?P<role_id>[0-9]+)",
):
    """custom_idにrole_idを埋め込むため、再起動後もDB不要で復元できるボタン"""

    def __init__(self, role_id: int, label: str = "通知ロールを切り替える"):
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji="🔔",
                style=discord.ButtonStyle.primary,
                custom_id=f"nexus:role:{role_id}",
            )
        )
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["role_id"]), item.label or "通知ロールを切り替える")

    async def callback(self, interaction: discord.Interaction):
        guild, member = interaction.guild, interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("❌ サーバー内でのみ使用できます。", ephemeral=True)
            return
        role = guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message("❌ ロールが見つかりません。", ephemeral=True)
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="ロールパネル: 解除")
                msg = f"🔕 {role.mention} を解除しました。"
            else:
                await member.add_roles(role, reason="ロールパネル: 付与")
                msg = f"🔔 {role.mention} を付与しました。"
        except discord.Forbidden:
            msg = "❌ BOTの権限が不足しています。管理者にお問い合わせください。"
        except discord.HTTPException:
            msg = "❌ ロールの更新に失敗しました。しばらくしてからお試しください。"
        await interaction.response.send_message(
            msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class RolePanelView(discord.ui.View):
    def __init__(self, role_id: int, label: str = "通知ロールを切り替える"):
        super().__init__(timeout=None)
        self.add_item(RoleToggleButton(role_id, label))


@bot.tree.command(name="setup-role-panel", description="ロール付与パネルを設置します（管理者専用）")
@app_commands.describe(role="トグルするロール", title="パネルのタイトル",
                       description="パネルの説明文", button_label="ボタンの表示名")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def setup_role_panel(
    interaction: discord.Interaction,
    role: discord.Role,
    title: str = "🔔 アップデート通知ロール",
    description: str = "下のボタンを押すと通知ロールを付与／解除できます。\nもう一度押すと解除されます。",
    button_label: str = "通知ロールを切り替える",
) -> None:
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 管理者のみ実行できます。", ephemeral=True)
        return

    me = interaction.guild.me
    if role.is_default() or role.managed:
        await interaction.response.send_message("❌ このロールは使用できません。", ephemeral=True)
        return
    if role.permissions.administrator or role.permissions.manage_guild or role.permissions.manage_roles:
        await interaction.response.send_message(
            "❌ 管理系の権限を持つロールは設定できません（権限昇格防止）。", ephemeral=True)
        return
    if not me.guild_permissions.manage_roles or role >= me.top_role:
        await interaction.response.send_message(
            "❌ BOTに「ロールの管理」権限がないか、BOTのロールが対象ロールより下です。", ephemeral=True)
        return

    embed = discord.Embed(title=title[:256], description=description[:2000], color=discord.Color.green())
    embed.add_field(name="対象ロール", value=role.mention, inline=False)
    embed.set_footer(text="NEXUS • ロールパネル")
    try:
        await interaction.channel.send(
            embed=embed,
            view=RolePanelView(role.id, button_label[:80]),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        await interaction.response.send_message("❌ このチャンネルに送信する権限がありません。", ephemeral=True)
        return
    await interaction.response.send_message("✅ ロールパネルを設置しました。", ephemeral=True)


# ========== アナウンス（サポートサーバー限定） ==========
def _is_support_admin(interaction: discord.Interaction) -> bool:
    return (
        interaction.guild_id == SUPPORT_GUILD_ID
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


class AnnounceModal(discord.ui.Modal, title="📢 アナウンス作成"):
    heading = discord.ui.TextInput(
        label="タイトル", max_length=200, placeholder="例: アップデートのお知らせ")
    body = discord.ui.TextInput(
        label="本文", style=discord.TextStyle.paragraph, max_length=2000,
        placeholder="改行も使えます")

    def __init__(self, channel: discord.TextChannel, mention: str):
        super().__init__()
        self.channel = channel
        self.mention = mention

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_support_admin(interaction):
            await interaction.response.send_message("❌ 権限がありません。", ephemeral=True)
            return

        perms = self.channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                f"❌ {self.channel.mention} への送信権限がBOTにありません。", ephemeral=True)
            return
        if self.mention != "none" and not perms.mention_everyone:
            await interaction.response.send_message(
                "❌ BOTに「@everyone、@here、全てのロールにメンション」権限がありません。", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📢 {self.heading.value}",
            description=self.body.value,
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"NEXUS お知らせ • {interaction.user.display_name}")

        content = {"everyone": "@everyone", "here": "@here"}.get(self.mention)
        allowed = discord.AllowedMentions(everyone=content is not None)

        try:
            await self.channel.send(content=content, embed=embed, allowed_mentions=allowed)
        except discord.HTTPException:
            await interaction.response.send_message("❌ 送信に失敗しました。", ephemeral=True)
            return
        log.info("announce sent by %s in #%s", interaction.user.id, self.channel.id)
        await interaction.response.send_message(
            f"✅ {self.channel.mention} にアナウンスを送信しました。", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("AnnounceModal error")
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ エラーが発生しました。", ephemeral=True)


@bot.tree.command(
    name="announce",
    description="サポートサーバーにアナウンスを送信します（管理者専用）",
    guild=SUPPORT_GUILD,
)
@app_commands.describe(channel="送信先チャンネル", mention="メンションの種類")
@app_commands.choices(mention=[
    app_commands.Choice(name="メンションなし", value="none"),
    app_commands.Choice(name="@here", value="here"),
    app_commands.Choice(name="@everyone", value="everyone"),
])
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def announce(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    mention: app_commands.Choice[str] = None,
) -> None:
    if not _is_support_admin(interaction):
        await interaction.response.send_message(
            "❌ このコマンドはサポートサーバーの管理者のみ使用できます。", ephemeral=True)
        return
    await interaction.response.send_modal(
        AnnounceModal(channel, mention.value if mention else "none"))


# ========== FastAPI（lifespanでBot同居） ==========
async def run_bot() -> None:
    try:
        await bot.start(DISCORD_TOKEN)
    except discord.LoginFailure:
        log.error("Discordトークンが無効です。DISCORD_TOKEN を確認してください。")
    except Exception as e:
        log.error("Botが停止しました: %s", type(e).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis()
    bot_task = asyncio.create_task(run_bot())
    try:
        yield
    finally:
        await bot.close()
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Reimu Support", lifespan=lifespan)


# UptimeRobot等はHEADで監視することもあるため両方受ける
@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "alive", "bot_ready": bot.is_ready()}


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok", "bot_ready": bot.is_ready()}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
