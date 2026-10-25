import asyncio
import html
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
import httpx
import uvicorn
from discord import app_commands
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, RedirectResponse

load_dotenv()

# ---------- 設定 ----------
CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
REDIRECT_URI = os.environ["REDIRECT_URI"]
PORT = int(os.getenv("PORT", "8000"))
MIN_ACCOUNT_AGE_DAYS = int(os.getenv("MIN_ACCOUNT_AGE_DAYS", "3"))  # これ未満は認証しない

BASE_URL = REDIRECT_URI.removesuffix("/callback").rstrip("/")
API = "https://discord.com/api/v10"
STATE_TTL = 600  # 秒
CONFIG_FILE = Path("guild_config.json")  # サーバーごとの設定
INDEX_PATH = Path(__file__).parent / "index.html"  # main.pyと同じフォルダのindex.html
JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"

states: dict[str, tuple[float, int]] = {}  # state -> (発行時刻, guild_id)


# ---------- サーバーごとの設定 ----------
def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def auth_url(guild_id: int) -> str:
    return f"{BASE_URL}/?guild={guild_id}"


# ---------- Discord Bot ----------
class AuthBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


bot = AuthBot()


@bot.tree.command(name="setup", description="認証ロールとログチャンネルを設定します")
@app_commands.describe(role="認証完了後に付与するロール",
                       log_channel="認証ログを送信するチャンネル")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def setup_cmd(interaction: discord.Interaction, role: discord.Role,
                    log_channel: discord.TextChannel):
    guild = interaction.guild
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return
    if role.is_default() or role.managed:
        await interaction.response.send_message(
            "そのロールは付与対象にできません（@everyone / 連携ロール）。", ephemeral=True)
        return
    if role >= guild.me.top_role:
        await interaction.response.send_message(
            "Botのロールが選んだロールより下です。サーバー設定でBotのロールを"
            "認証ロールより**上**に移動してください。", ephemeral=True)
        return
    perms = log_channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        await interaction.response.send_message(
            f"{log_channel.mention} でBotに「チャンネルを見る・メッセージを送信・"
            "埋め込みリンク」の権限がありません。", ephemeral=True)
        return

    cfg = load_config()
    cfg[str(guild.id)] = {"role_id": role.id, "log_channel_id": log_channel.id}
    save_config(cfg)
    await interaction.response.send_message(
        f"✅ 設定しました\n"
        f"認証ロール: {role.mention}\n"
        f"ログチャンネル: {log_channel.mention}\n\n"
        f"認証ページURL（このサーバー専用）:\n{auth_url(guild.id)}",
        ephemeral=True,
    )


@bot.tree.command(name="authlink", description="このサーバーの認証ページURLを表示します")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def authlink_cmd(interaction: discord.Interaction):
    if str(interaction.guild.id) not in load_config():
        await interaction.response.send_message(
            "先に `/setup` で設定してください。", ephemeral=True)
        return
    await interaction.response.send_message(auth_url(interaction.guild.id),
                                            ephemeral=True)


@bot.tree.command(name="panel", description="認証パネル（ボタン付き）をチャンネルに送信します")
@app_commands.describe(channel="パネルを送信するチャンネル（省略すると今のチャンネル）")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def panel_cmd(interaction: discord.Interaction,
                    channel: discord.TextChannel | None = None):
    guild = interaction.guild
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return
    gcfg = load_config().get(str(guild.id))
    if gcfg is None:
        await interaction.response.send_message(
            "先に `/setup` で設定してください。", ephemeral=True)
        return

    target = channel or interaction.channel
    perms = target.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        await interaction.response.send_message(
            f"{target.mention} でBotに「チャンネルを見る・メッセージを送信・"
            "埋め込みリンク」の権限がありません。", ephemeral=True)
        return

    role = guild.get_role(gcfg["role_id"])
    role_text = role.mention if role else "認証ロール"
    embed = discord.Embed(
        title="🔐 認証パネル",
        description=(f"下のボタンを押してDiscordでログインすると、認証が完了して"
                     f"{role_text} が自動で付与されます。"),
        color=0x5865F2,
    )
    embed.set_footer(text="取得する情報はユーザーIDとアカウント名のみです")

    view = discord.ui.View()
    view.add_item(discord.ui.Button(
        label="認証する", emoji="✅",
        style=discord.ButtonStyle.link, url=auth_url(guild.id)))

    try:
        await target.send(embed=embed, view=view)
    except discord.HTTPException as e:
        await interaction.response.send_message(
            f"送信に失敗しました: {e}\n"
            "（`REDIRECT_URI` が http://localhost だとボタンのURLとして"
            "拒否されることがあります。公開URLに変更してください）", ephemeral=True)
        return
    await interaction.response.send_message(
        f"✅ {target.mention} に認証パネルを送信しました。", ephemeral=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(bot.start(BOT_TOKEN))
    try:
        yield
    finally:
        await bot.close()
        task.cancel()


app = FastAPI(lifespan=lifespan)


# ---------- 認証ログ ----------
async def send_auth_log(channel_id: int, user: dict, role: discord.Role | None,
                        age: timedelta, passed: bool):
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)

    uid = int(user["id"])
    if user.get("avatar"):
        avatar = f"https://cdn.discordapp.com/avatars/{uid}/{user['avatar']}.png?size=256"
    else:
        avatar = f"https://cdn.discordapp.com/embed/avatars/{(uid >> 22) % 6}.png"

    now = datetime.now(JST)
    date_str = (f"{now.year}年{now.month}月{now.day}日 "
                f"{WEEKDAYS[now.weekday()]}曜日 {now:%H:%M}")
    age_str = f"アカウント作成から {age.days}日{age.seconds // 3600}時間"

    if passed:
        title, color = "✨ ユーザー認証ログ", 0x00FF00
        sub = f"なし ({age_str})"
        role_text = f"✅ 付与完了 ({role.mention})"
    else:
        title, color = "⛔ 認証拒否ログ", 0xEF4444
        sub = f"⚠️ あり ({age_str} / {MIN_ACCOUNT_AGE_DAYS}日未満)"
        role_text = "❌ 付与なし (認証拒否)"

    embed = discord.Embed(title=title, color=color,
                          timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=avatar)
    embed.add_field(name="👤 ユーザー",
                    value=f"<@{uid}> (`{user['username']}`)", inline=False)
    embed.add_field(name="🆔 ユーザーID", value=f"`{uid}`", inline=False)
    embed.add_field(name="📅 認証日時", value=date_str, inline=False)
    embed.add_field(name="🛡️ サブアカウント判定", value=sub, inline=False)
    embed.add_field(name="🏷️ ロール付与状況", value=role_text, inline=False)
    await channel.send(embed=embed)


# ---------- 結果ページ ----------
def result_page(ok: bool, title: str, message: str) -> HTMLResponse:
    color = "#22c55e" if ok else "#ef4444"
    body = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;
background:#0b0f19;color:#e5e7eb;font-family:system-ui,sans-serif}}
.card{{max-width:420px;margin:16px;padding:32px;border-radius:16px;
background:rgba(255,255,255,.06);backdrop-filter:blur(12px);
border:1px solid {color};box-shadow:0 0 24px {color}55;text-align:center}}
h1{{color:{color};font-size:1.4rem}}
</style></head><body><div class="card">
<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>
</div></body></html>"""
    return HTMLResponse(body, status_code=200 if ok else 400)


def invite_done_page() -> HTMLResponse:
    """ボット招待後（guild_idあり・stateなし）に表示するページ。"""
    body = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ボットの招待が完了しました</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;
background:#0f1117;color:#e5e7eb;font-family:system-ui,-apple-system,sans-serif}
.card{width:100%;max-width:440px;margin:16px;padding:36px 28px;border-radius:16px;
background:#181b23;border:1px solid #2a2f3a;text-align:center;
box-shadow:0 8px 32px rgba(0,0,0,.4)}
h1{margin:0 0 16px;font-size:1.4rem}
p{margin:0;line-height:1.8;color:#9ca3af}
</style></head><body><div class="card">
<h1>🤖 ボットの招待が完了しました！</h1>
<p>Discordサーバーへの追加ありがとうございます。このタブを閉じてDiscordアプリに戻り、認証を進めてください。</p>
</div></body></html>"""
    return HTMLResponse(body)


# ---------- ルーティング ----------
@app.get("/")
async def index(guild: int | None = None):
    if guild is None or str(guild) not in load_config():
        return result_page(False, "認証URLが無効です",
                           "サーバー管理者に正しい認証URLを確認してください。")
    page = INDEX_PATH.read_text(encoding="utf-8")
    # ログインボタンのリンクに guild を引き継ぐ
    page = page.replace("/login", f"/login?guild={guild}")
    return HTMLResponse(page)


@app.get("/login")
async def login(guild: int | None = None):
    if guild is None or str(guild) not in load_config():
        return result_page(False, "認証URLが無効です",
                           "このサーバーは設定されていません。")
    now = time.time()
    for k in [k for k, v in states.items() if now - v[0] > STATE_TTL]:
        states.pop(k, None)

    state = secrets.token_urlsafe(24)
    states[state] = (now, guild)
    params = httpx.QueryParams({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds.join",
        "state": state,
        "prompt": "none",
    })
    return RedirectResponse(f"{API}/oauth2/authorize?{params}")


@app.get("/callback")
async def callback(code: str | None = None, state: str | None = None,
                   error: str | None = None,
                   invited_guild_id: str | None = Query(default=None, alias="guild_id")):
    # ボット招待後のリダイレクト（guild_idあり・stateなし）はエラーにせず案内ページを返す
    if invited_guild_id and not state:
        return invite_done_page()

    if error or not code:
        return result_page(False, "認証がキャンセルされました",
                           "もう一度お試しください。")

    issued = states.pop(state or "", None)
    if issued is None or time.time() - issued[0] > STATE_TTL:
        return result_page(False, "セッションが無効です",
                           "最初からやり直してください。")
    guild_id = issued[1]

    gcfg = load_config().get(str(guild_id))
    if gcfg is None:
        return result_page(False, "設定エラー", "このサーバーは未設定です。")
    role_id = gcfg["role_id"]
    log_channel_id = gcfg["log_channel_id"]

    async with httpx.AsyncClient(timeout=15) as http:
        # 1) 認可コード → アクセストークン
        r = await http.post(
            f"{API}/oauth2/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            return result_page(False, "トークン取得に失敗",
                               "認証サーバーとの通信に失敗しました。")
        access_token = r.json()["access_token"]

        # 2) ユーザー情報取得
        r = await http.get(f"{API}/users/@me",
                           headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code != 200:
            return result_page(False, "ユーザー情報の取得に失敗", "")
        user = r.json()
        user_id = int(user["id"])
        username = user.get("global_name") or user["username"]

        # 2.5) アカウント作成日数チェック（サブアカウント対策）
        age = datetime.now(timezone.utc) - discord.utils.snowflake_time(user_id)
        if age < timedelta(days=MIN_ACCOUNT_AGE_DAYS):
            try:
                await bot.wait_until_ready()
                await send_auth_log(log_channel_id, user, None, age, False)
            except Exception as e:
                print(f"認証ログ送信エラー: {e}")
            return result_page(
                False, "認証できません",
                f"アカウント作成から{MIN_ACCOUNT_AGE_DAYS}日以上経過している必要があります。"
                "時間をおいてから再度お試しください。")

        # 3) ロール付与 + 認証ログ
        try:
            await bot.wait_until_ready()
            guild = bot.get_guild(guild_id)
            if guild is None:
                return result_page(False, "設定エラー",
                                   "Botがこのサーバーに参加していません。")
            role = guild.get_role(role_id)
            if role is None:
                return result_page(False, "設定エラー",
                                   "認証ロールが見つかりません。/setup をやり直してください。")

            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                member = None

            if member is None:
                # 未参加ならOAuth2経由でサーバーに参加させ、同時にロール付与
                jr = await http.put(
                    f"{API}/guilds/{guild_id}/members/{user_id}",
                    headers={"Authorization": f"Bot {BOT_TOKEN}"},
                    json={"access_token": access_token, "roles": [role_id]},
                )
                if jr.status_code not in (200, 201, 204):
                    return result_page(False, "サーバー参加に失敗",
                                       "先にサーバーへ参加してからお試しください。")
                if jr.status_code == 204:  # 既に参加済みだった場合
                    member = await guild.fetch_member(user_id)

            if member is not None:
                await member.add_roles(role, reason="Web認証完了")

            # --- 認証ログ送信（失敗しても認証自体は成功扱い） ---
            try:
                await send_auth_log(log_channel_id, user, role, age, True)
            except Exception as e:
                print(f"認証ログ送信エラー: {e}")

        except discord.Forbidden:
            return result_page(False, "権限エラー",
                               "Botのロールが付与対象ロールより下か、権限が不足しています。")
        except discord.HTTPException as e:
            return result_page(False, "Discord APIエラー", str(e))

    return result_page(True, "認証完了",
                       f"{username} さん、認証が完了しました。Discordに戻ってください。")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
