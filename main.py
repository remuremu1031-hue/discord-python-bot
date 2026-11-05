import asyncio
import atexit
import copy
import hashlib
import html
import io
import json
import logging
import math
import os
import platform
import re
import secrets
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
import httpx
import uvicorn
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

try:
    from redis import asyncio as aioredis  # pip install redis
except ImportError:  # 未インストールなら、GBAN機能だけが無効になる
    aioredis = None

load_dotenv()

# ---------- 設定 ----------
CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
REDIRECT_URI = os.getenv("REDIRECT_URI", "")  # クイックトンネル使用時は起動時に自動設定される
PORT = int(os.getenv("PORT", "8080"))
USE_QUICK_TUNNEL = os.getenv("USE_QUICK_TUNNEL", "1") != "0"  # 0で無効化
MIN_ACCOUNT_AGE_DAYS = int(os.getenv("MIN_ACCOUNT_AGE_DAYS", "3"))  # これ未満は認証しない
SYNC_COMMANDS = os.getenv("SYNC_COMMANDS", "1") != "0"  # 0にするとスラッシュコマンドの同期をしない（複数台で動かすときの副環境用）
PURGE_OLD_COMMANDS = os.getenv("PURGE_OLD_COMMANDS", "0") == "1"  # 1にすると起動時に不要なコマンドを削除
WATCHDOG_SECONDS = int(os.getenv("WATCHDOG_SECONDS", "300"))  # Botがこの秒数オフラインならプロセスを終了（ホスト側が再起動）。0で無効
READY_TIMEOUT = 8.0  # 認証処理でBotの準備完了を待つ最大秒数
SECURITY_WEBHOOK_URL = os.getenv("SECURITY_WEBHOOK_URL", "").strip()  # 不正アクセス通知のWebhook（未設定なら通知しない）
RATE_LIMIT_INDEX = os.getenv("RATE_LIMIT_INDEX", "30/minute")  # 認証ページ（/）
RATE_LIMIT_LOGIN = os.getenv("RATE_LIMIT_LOGIN", "10/minute")  # /login
RATE_LIMIT_CALLBACK = os.getenv("RATE_LIMIT_CALLBACK", "10/minute")  # /callback
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "1") != "0"  # プロキシ経由のIPヘッダーを使う（直接公開なら0）
SECURITY_SALT = os.getenv("SECURITY_SALT") or secrets.token_hex(16)  # 通知に出す識別子のハッシュ用
UPSTASH_REDIS_URL = os.getenv("UPSTASH_REDIS_URL", "").strip()  # GBAN用: rediss://default:パスワード@ホスト:6379
ENABLE_MESSAGE_CONTENT_INTENT = os.getenv("ENABLE_MESSAGE_CONTENT_INTENT", "0") == "1"  # メッセージ内容のログ・招待リンク検知に必要（PortalでMESSAGE CONTENT INTENTもON）
DISABLED_COGS = {x.strip() for x in os.getenv("DISABLED_COGS", "").split(",") if x.strip()}  # 使わない追加機能（例: tickets,automod）
ENABLE_MEMBERS_INTENT = os.getenv("ENABLE_MEMBERS_INTENT", "0") == "1"  # 参加時の自動BANに必要（Developer PortalでSERVER MEMBERS INTENTもON）
STATUS_CHANNEL_ID = int(os.getenv("STATUS_CHANNEL_ID") or 0)  # 稼働状況を表示するチャンネル（0で無効）
STATUS_INTERVAL_MINUTES = min(10, max(5, int(os.getenv("STATUS_INTERVAL_MINUTES") or 5)))  # 更新間隔（5〜10分）

BASE_URL = REDIRECT_URI.removesuffix("/callback").rstrip("/")
API = "https://discord.com/api/v10"
STATE_TTL = 600  # 秒
CONFIG_FILE = Path("guild_config.json")  # サーバーごとの設定
BROADCAST_FILE = Path("broadcast_channels.json")  # お知らせの送信先（サーバーごと）
BLACKLIST_FILE = Path("blacklist.json")  # ブラックリスト（ユーザー / サーバー）
SERVER_BLACKLIST_FILE = Path("server_blacklist.json")  # サーバーごとのブラックリスト
LICENSE_FILE = Path("license_suspended.json")  # ライセンス停止中のサーバー
STATUS_FILE = Path("status_message.json")  # 稼働状況メッセージのID
DEFENSE_CONFIG_FILE = Path("defense_config.json")  # Nuke/Raid防衛設定
BACKUP_DATA_FILE = Path("server_backups.json")  # サーバーバックアップ
INDEX_PATH = Path(__file__).parent / "index.html"  # main.pyと同じフォルダのindex.html
JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"

states: dict[str, tuple[float, int]] = {}  # state -> (発行時刻, guild_id)
dashboard_sessions: dict[str, tuple[float, int]] = {}  # ダッシュボードログインセッション


def safe_error(e: Exception) -> str:
    """ログ用：例外の種類（とHTTPステータス）だけを返す。
    例外メッセージにはURLなどが含まれることがあるため、そのままは出さない。"""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None) or getattr(e, "status", None)
    return f"{type(e).__name__}" + (f" (HTTP {code})" if code else "")


# ---------- サーバーごとの設定 ----------
class Storage:
    """設定の保存先。
    UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN があれば Upstash(Redis) に保存し、
    なければ、これまでどおりJSONファイルに保存する。
    読み込みは起動時にメモリへ載せ、以降はメモリから返す（高速）。
    書き込みはメモリを更新したうえで、バックグラウンドでUpstashへ送る。
    """

    def __init__(self):
        self.url = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
        self.token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
        self.prefix = os.getenv("STORAGE_PREFIX", "authbot:")
        self.remote = bool(self.url and self.token)
        self._cache: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._push_lock = threading.Lock()

    # --- Upstash REST API（POSTのJSON配列でコマンドを送る） ---
    def _cmd(self, *args):
        r = httpx.post(self.url, json=list(args), timeout=10,
                       headers={"Authorization": f"Bearer {self.token}"})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(data["error"])
        return data.get("result")

    @staticmethod
    def _read_file(path: Path):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"{path.name} の読み込みに失敗: {e}")
        return None

    def _initial(self, name: str, path: Path) -> dict:
        if not self.remote:
            data = self._read_file(path)
            return data if isinstance(data, dict) else {}
        # 失敗したら例外のまま（空データで上書きしてしまうのを防ぐ）
        raw = self._cmd("GET", self.prefix + name)
        if raw is not None:
            return json.loads(raw)
        # Upstashがまだ空で、手元にJSONファイルがあれば移行する
        data = self._read_file(path)
        if isinstance(data, dict) and data:
            self._cmd("SET", self.prefix + name, json.dumps(data, ensure_ascii=False))
            print(f"{path.name} をUpstashへ移行しました")
            return data
        return {}

    def load(self, name: str, path: Path) -> dict:
        with self._lock:
            if name not in self._cache:
                self._cache[name] = self._initial(name, path)
            return copy.deepcopy(self._cache[name])

    def save(self, name: str, path: Path, data: dict) -> None:
        with self._lock:
            self._cache[name] = copy.deepcopy(data)
        if self.remote:
            threading.Thread(target=self._push, args=(name,)).start()
        else:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")

    def _push(self, name: str) -> None:
        with self._push_lock:  # 送信の順番を守る
            with self._lock:
                payload = json.dumps(self._cache[name], ensure_ascii=False)
            for attempt in range(4):
                try:
                    self._cmd("SET", self.prefix + name, payload)
                    return
                except Exception as e:
                    print(f"設定の保存に失敗（{name}） {attempt + 1}/4: "
                          f"{safe_error(e)}")
                    time.sleep(2 ** attempt)
            print(f"⚠️ {name} をUpstashに保存できませんでした。"
                  "このまま再起動すると、最近の変更が失われます。")
            alert_threadsafe("設定の保存に失敗",
                             f"「{name}」をUpstashに4回保存できませんでした。"
                             "再起動すると、最近の変更が失われる可能性があります。",
                             dedupe_key="storage-fail")

    def preload(self, items: list[tuple[str, Path]]) -> None:
        for name, path in items:
            self.load(name, path)


storage = Storage()


def load_config() -> dict:
    return storage.load("guild_config", CONFIG_FILE)


def save_config(cfg: dict) -> None:
    storage.save("guild_config", CONFIG_FILE, cfg)


def load_broadcast() -> dict:
    return storage.load("broadcast", BROADCAST_FILE)


def save_broadcast(data: dict) -> None:
    storage.save("broadcast", BROADCAST_FILE, data)


# ---------- ブラックリスト ----------
def load_blacklist() -> dict[str, list[int]]:
    """-> {"users": [ID...], "guilds": [ID...]}"""
    data: dict[str, list[int]] = {"users": [], "guilds": []}
    raw = storage.load("blacklist", BLACKLIST_FILE)
    try:
        data["users"] = [int(x) for x in raw.get("users", [])]
        data["guilds"] = [int(x) for x in raw.get("guilds", [])]
    except Exception as e:
        print(f"ブラックリストの読み込みに失敗: {e}")
    return data


def save_blacklist(data: dict[str, list[int]]) -> None:
    out = {k: sorted(set(v)) for k, v in data.items()}
    storage.save("blacklist", BLACKLIST_FILE, out)


def is_user_blacklisted(user_id: int) -> bool:
    return user_id in load_blacklist()["users"]


def is_guild_blacklisted(guild_id: int) -> bool:
    return guild_id in load_blacklist()["guilds"]


# ---------- サーバーごとのブラックリスト（そのサーバーの認証だけをブロック） ----------
def load_server_blacklist() -> dict[str, list[int]]:
    """-> {"サーバーID": [ユーザーID...]}"""
    raw = storage.load("server_blacklist", SERVER_BLACKLIST_FILE)
    try:
        return {str(k): [int(x) for x in v] for k, v in raw.items()}
    except Exception as e:
        print(f"サーバー別ブラックリストの読み込みに失敗: {e}")
        return {}


def save_server_blacklist(data: dict[str, list[int]]) -> None:
    out = {k: sorted(set(v)) for k, v in data.items() if v}
    storage.save("server_blacklist", SERVER_BLACKLIST_FILE, out)


def is_server_user_blacklisted(guild_id: int, user_id: int) -> bool:
    return user_id in load_server_blacklist().get(str(guild_id), [])


# ---------- ライセンス（停止中のサーバー） ----------
def load_license() -> dict[str, dict]:
    """-> {"サーバーID": {"at": "停止した日時(JST)"}}  ※載っているサーバーが停止中"""
    raw = storage.load("license", LICENSE_FILE)
    return {str(k): (v if isinstance(v, dict) else {}) for k, v in raw.items()}


def save_license(data: dict[str, dict]) -> None:
    storage.save("license", LICENSE_FILE, data)


def is_license_suspended(guild_id: int) -> bool:
    return str(guild_id) in load_license()


# ---------- セキュリティ・防衛・バックアップ設定 ----------
def load_defense_config() -> dict:
    return storage.load("defense_config", DEFENSE_CONFIG_FILE)

def save_defense_config(data: dict) -> None:
    storage.save("defense_config", DEFENSE_CONFIG_FILE, data)

def load_backups() -> dict:
    return storage.load("server_backups", BACKUP_DATA_FILE)

def save_backups(data: dict) -> None:
    storage.save("server_backups", BACKUP_DATA_FILE, data)


BROADCAST_WARNING = (
    "⚠️ **お知らせの送信先が未設定です。**\n"
    "未設定のままだと、Botからのお知らせは"
    "**サーバーのチャンネル一覧の一番上**（Botが送信できるテキストチャンネル）に届きます。\n"
    "`/broadcast-channel` で送信先を決められます。"
)


def auth_url(guild_id: int) -> str:
    return f"{BASE_URL}/?guild={guild_id}"


# ---------- チケットコントロールビュー（ボタン式トランスクリプト＆削除） ----------
class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="トランスクリプト保存", style=discord.ButtonStyle.primary, custom_id="ticket_transcript", emoji="📝", row=0)
    async def transcript_button(self, interaction: discord.Interaction, button: discord.Button):
        await interaction.response.defer(ephemeral=True)
        
        messages = [msg async for msg in interaction.channel.history(limit=None, oldest_first=True)]
        
        log_content = f"=== チケットログ: {interaction.channel.name} ===\n"
        log_content += f"作成日時: {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        for msg in messages:
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            log_content += f"[{timestamp}] {msg.author} (ID: {msg.author.id}): {msg.content}\n"
            if msg.attachments:
                for att in msg.attachments:
                    log_content += f"  [添付ファイル: {att.url}]\n"
                    
        file = discord.File(
            io.BytesIO(log_content.encode('utf-8')), 
            filename=f"transcript-{interaction.channel.name}.txt"
        )
        
        await interaction.followup.send("📝 チケットのトランスクリプトを作成しました。", file=file, ephemeral=True)

    @discord.ui.button(label="チケット削除", style=discord.ButtonStyle.danger, custom_id="ticket_delete", emoji="🗑️", row=0)
    async def delete_button(self, interaction: discord.Interaction, button: discord.Button):
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ このボタンを押す権限がありません。", ephemeral=True)
            return
            
        await interaction.response.send_message("🗑️ 3秒後にこのチャンネルを削除します...", ephemeral=True)
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason=f"チケット削除: {interaction.user}")
        except Exception as e:
            print(f"チャンネル削除エラー: {e}")


# ---------- Discord Bot ----------
KEEP_COMMANDS = {"setup", "authlink", "panel", "broadcast", "broadcast-channel", "blacklist", "server-blacklist", "license", "gban", "set-log-channel",
                 "mass_role", "ticket", "warn", "warnings", "reactionrole", "timeout", "kick",
                 "ban", "automod", "eventlog", "clearwarnings", "untimeout", "unban", "nuke-defense", "backup"}  # 残すスラッシュコマンド


EXTRA_COGS = ["mass_role", "tickets", "warns", "reaction_roles",
              "event_logs", "moderation", "automod"]  # cogs/ フォルダの追加機能


class AuthBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        extra: dict = {}
        if ENABLE_MESSAGE_CONTENT_INTENT:
            intents.message_content = True
        if ENABLE_MEMBERS_INTENT:
            intents.members = True
            extra = dict(chunk_guilds_at_startup=False,
                         member_cache_flags=discord.MemberCacheFlags.none())
        super().__init__(command_prefix=commands.when_mentioned,
                         intents=intents, help_command=None, **extra)
        self._owner_ids: set[int] | None = None
        self._status_task: asyncio.Task | None = None

    async def on_message(self, message: discord.Message):
        if message.guild and not message.author.bot and isinstance(message.author, discord.Member):
            cfg = load_defense_config().get(str(message.guild.id), {})
            if cfg.get("invite_block", False):
                perms = message.author.guild_permissions
                if not (perms.administrator or perms.manage_messages):
                    if message.content and re.search(r"(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+", message.content, re.I):
                        try:
                            await message.delete()
                            await message.channel.send(f"{message.author.mention} 招待リンクの投稿は禁止されています。", delete_after=5)
                            return
                        except Exception:
                            pass
        return  

    def kv_ready(self) -> bool:
        return redis_client() is not None

    async def kv(self, op: str, *args):
        return await _redis_do(op, *args)

    async def guild_log(self, guild: discord.Guild, embed: discord.Embed) -> bool:
        return await send_guild_log(guild, embed)

    async def get_owner_ids(self) -> set[int]:
        if self._owner_ids is None:
            info = await self.application_info()
            if info.team:
                self._owner_ids = {info.team.owner_id}
            else:
                self._owner_ids = {info.owner.id}
        return self._owner_ids

    def build_status_text(self) -> str:
        guilds = len(self.guilds)
        members = sum((g.member_count or 0) for g in self.guilds)
        return f"remuremu | {guilds:,}サーバー | {members:,}人"

    async def update_status(self):
        if not self.is_ready() or self.is_closed():
            return
        try:
            await self.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(type=discord.ActivityType.watching,
                                          name=self.build_status_text()),
            )
        except Exception as e:
            print(f"ステータス更新エラー: {e}")

    def request_status_update(self, delay: float = 5.0):
        if self._status_task and not self._status_task.done():
            return
        self._status_task = asyncio.create_task(self._delayed_status_update(delay))

    async def _delayed_status_update(self, delay: float):
        await asyncio.sleep(delay)
        await self.update_status()

    async def on_guild_join(self, guild: discord.Guild):
        if is_guild_blacklisted(guild.id):
            try:
                await guild.leave()
                print(f"ブラックリストのサーバーから退出しました: {guild.name} ({guild.id})")
            except Exception as e:
                print(f"サーバーからの退出に失敗 ({guild.id}): {e}")
            return
        self.request_status_update()

    async def leave_blacklisted_guilds(self):
        banned = set(load_blacklist()["guilds"])
        if not banned:
            return
        async for g in self.fetch_guilds(limit=None):
            if g.id in banned:
                try:
                    await g.leave()
                    print(f"ブラックリストのサーバーから退出しました: {g.name} ({g.id})")
                except Exception as e:
                    print(f"サーバーからの退出に失敗 ({g.id}): {e}")

    async def on_guild_remove(self, guild: discord.Guild):
        self.request_status_update()

    async def on_member_join(self, member: discord.Member):
        try:
            await gban_auto_ban(member)
        except Exception as e:
            print(f"GBANの自動BAN処理エラー: {safe_error(e)}")

    @tasks.loop(minutes=10)
    async def refresh_status(self):
        await self.update_status()

    async def setup_hook(self):
        for name in EXTRA_COGS:
            if name in DISABLED_COGS:
                continue
            try:
                await self.load_extension(f"cogs.{name}")
            except Exception as e:
                print(f"追加機能 {name} の読み込みに失敗: {type(e).__name__}: {e}")
        if SYNC_COMMANDS:
            try:
                await self.tree.sync()
            except Exception as e:
                print(f"コマンドの同期に失敗: {type(e).__name__}: {e}")
        try:
            await self.leave_blacklisted_guilds()
        except Exception as e:
            print(f"ブラックリストのサーバーからの退出処理に失敗: {type(e).__name__}: {e}")
        if STATUS_CHANNEL_ID:
            try:
                await self.add_cog(StatusCog(self))
            except Exception as e:
                print(f"稼働状況表示の読み込みに失敗: {safe_error(e)}")

    async def on_ready(self):
        if not self.refresh_status.is_running():
            self.refresh_status.start()

        if not PURGE_OLD_COMMANDS or getattr(self, "_purged", False):
            return
        self._purged = True
        await self.purge_old_commands()

    async def purge_old_commands(self):
        try:
            for cmd in await self.tree.fetch_commands():
                if cmd.name not in KEEP_COMMANDS:
                    await cmd.delete()
                    print(f"古いコマンドを削除しました: /{cmd.name} (グローバル)")
        except Exception as e:
            print(f"グローバルコマンドの削除に失敗: {e}")
        for g in self.guilds:
            try:
                for cmd in await self.tree.fetch_commands(guild=g):
                    await cmd.delete()
                    print(f"古いコマンドを削除しました: /{cmd.name} ({g.name})")
            except Exception as e:
                print(f"サーバー限定コマンドの削除に失敗 ({g.name}): {e}")


bot = AuthBot()


# ---------- セキュリティ・Nuke/Raid防衛・バックアップ機能 ----------
raid_action_history: dict[int, list[float]] = {}

@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    guild = channel.guild
    cfg = load_defense_config().get(str(guild.id), {})
    if not cfg.get("nuke_defense", False):
        return
    now = time.time()
    history = raid_action_history.setdefault(guild.id, [])
    history.append(now)
    recent = [t for t in history if now - t < 10]
    raid_action_history[guild.id] = recent
    if len(recent) >= 3:
        embed = discord.Embed(title="🚨 Nuke（破壊的行為）を検知しました！", description="短時間に複数のチャンネルが削除されたため、自動防衛システムが作動しました。", color=0xEF4444)
        await send_guild_log(guild, embed)

@bot.event
async def on_guild_role_create(role: discord.Role):
    guild = role.guild
    if role.permissions.administrator or role.permissions.manage_guild:
        embed = discord.Embed(title="⚠️ 危険な高権限ロールの作成を検知", description=f"管理者権限を持つロール `{role.name}` が作成されました。", color=0xF59E0B)
        await send_guild_log(guild, embed)

@bot.tree.command(name="nuke-defense", description="Nuke（サーバー破壊）自動検知や招待リンクブロックを設定します")
@app_commands.describe(nuke_defense="Nuke自動検知を有効にするか", invite_block="招待リンクの自動ブロックを有効にするか")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def nuke_defense_cmd(interaction: discord.Interaction, nuke_defense: bool, invite_block: bool):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return
    guild = interaction.guild
    cfg = load_defense_config()
    cfg[str(guild.id)] = {"nuke_defense": nuke_defense, "invite_block": invite_block}
    save_defense_config(cfg)
    await interaction.response.send_message(
        f"✅ セキュリティ設定を更新しました\n・Nuke自動検知: `{'有効' if nuke_defense else '無効'}`\n・自動招待リンクブロック: `{'有効' if invite_block else '無効'}`",
        ephemeral=True
    )

@bot.tree.command(name="backup", description="現在のサーバーのロール構造やチャンネル構成をバックアップします")
@app_commands.describe(action="操作を選択")
@app_commands.choices(action=[
    app_commands.Choice(name="バックアップ作成 (create)", value="create"),
    app_commands.Choice(name="バックアップ一覧 (list)", value="list")
])
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def backup_cmd(interaction: discord.Interaction, action: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return
    guild = interaction.guild
    backups = load_backups()
    gid = str(guild.id)
    if action == "create":
        role_data = [{"name": r.name, "color": r.color.value, "permissions": r.permissions.value} for r in guild.roles if not r.is_default()]
        channel_data = [{"name": c.name, "type": str(c.type)} for c in guild.channels]
        server_backups = backups.setdefault(gid, [])
        server_backups.append({"time": datetime.now(JST).strftime("%Y-%m-%d %H:%M"), "roles": role_data, "channels": channel_data})
        save_backups(backups)
        await interaction.response.send_message("✅ 現在のロールとチャンネルの構成をバックアップしました。", ephemeral=True)
    else:
        server_backups = backups.get(gid, [])
        if not server_backups:
            await interaction.response.send_message("保存されたバックアップはありません。", ephemeral=True)
            return
        desc = "\n".join([f"・{b['time']} (ロール: {len(b['roles'])}件, チャンネル: {len(b['channels'])}件)" for b in server_backups])
        embed = discord.Embed(title="💾 サーバーバックアップ一覧", description=desc, color=0x3B82F6)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------- 稼働状況の表示（Cog） ----------
STATUS_TITLE = "📡 認証Bot 稼働状況"


class StatusCog(commands.Cog):
    def __init__(self, bot: "AuthBot"):
        self.bot = bot
        self._message: discord.Message | None = None
        self.update_status_message.change_interval(minutes=STATUS_INTERVAL_MINUTES)

    async def cog_load(self):
        self.update_status_message.start()

    async def cog_unload(self):
        self.update_status_message.cancel()

    async def check_auth_api(self) -> tuple[bool, int | None]:
        url = (BASE_URL or f"http://localhost:{PORT}") + "/health"
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(url)
            return r.status_code == 200, int((time.perf_counter() - started) * 1000)
        except Exception:
            return False, None

    def build_embed(self, api_ok: bool, api_ms: int | None) -> discord.Embed:
        bot_ok = self.bot.is_ready() and not self.bot.is_closed()
        lat = self.bot.latency
        ping = f"{round(lat * 1000)} ms" if math.isfinite(lat) else "計測中"
        now = int(time.time())

        if api_ok and bot_ok:
            color = 0x22C55E
        elif bot_ok:
            color = 0xF59E0B
        else:
            color = 0xEF4444

        api_text = "🔴 応答なし"
        if api_ok:
            api_text = "🟢 正常" + (f"（{api_ms} ms）" if api_ms is not None else "")

        embed = discord.Embed(title=STATUS_TITLE, color=color)
        embed.add_field(name="🔐 認証API", value=api_text, inline=True)
        embed.add_field(name="🤖 Bot", value="🟢 オンライン" if bot_ok else "🔴 オフライン",
                        inline=True)
        embed.add_field(name="🏓 Ping", value=ping, inline=True)
        embed.add_field(name="🏠 導入サーバー数", value=f"{len(self.bot.guilds):,} サーバー",
                        inline=True)
        embed.add_field(name="🕒 最終更新", value=f"<t:{now}:F>（<t:{now}:R>）", inline=False)
        embed.set_footer(text=f"{STATUS_INTERVAL_MINUTES}分ごとに自動更新")
        return embed

    async def _find_message(self, channel) -> discord.Message | None:
        saved = storage.load("status_message", STATUS_FILE)
        if saved.get("channel_id") == channel.id and saved.get("message_id"):
            try:
                return await channel.fetch_message(int(saved["message_id"]))
            except discord.NotFound:
                pass
        async for m in channel.history(limit=50):
            if (m.author.id == self.bot.user.id and m.embeds
                    and m.embeds[0].title == STATUS_TITLE):
                return m
        return None

    async def _send_new(self, channel, embed: discord.Embed) -> discord.Message:
        msg = await channel.send(embed=embed)
        storage.save("status_message", STATUS_FILE,
                     {"channel_id": channel.id, "message_id": msg.id})
        return msg

    @tasks.loop(minutes=5)
    async def update_status_message(self):
        try:
            channel = (self.bot.get_channel(STATUS_CHANNEL_ID)
                       or await self.bot.fetch_channel(STATUS_CHANNEL_ID))
            api_ok, api_ms = await self.check_auth_api()
            embed = self.build_embed(api_ok, api_ms)

            if self._message is None:
                self._message = await self._find_message(channel)
            if self._message is None:
                self._message = await self._send_new(channel, embed)
            else:
                try:
                    await self._message.edit(embed=embed)
                except discord.NotFound:
                    self._message = await self._send_new(channel, embed)
        except discord.Forbidden:
            print("稼働状況の更新: チャンネルの権限が足りません")
        except Exception as e:
            print(f"稼働状況の更新エラー: {safe_error(e)}")

    @update_status_message.before_loop
    async def _before_update(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)


# ---------- GBAN（グローバルBAN）とサーバーごとのログチャンネル ----------
_redis = None


def redis_client():
    global _redis
    if aioredis is None or not UPSTASH_REDIS_URL:
        return None
    if _redis is None:
        _redis = aioredis.from_url(UPSTASH_REDIS_URL, decode_responses=True,
                                   socket_timeout=5, socket_connect_timeout=5,
                                   health_check_interval=30)
    return _redis


def redis_problem() -> str | None:
    if aioredis is None:
        return "redis ライブラリが入っていません"
    if not UPSTASH_REDIS_URL:
        return "環境変数 UPSTASH_REDIS_URL が設定されていません"
    return None


async def _redis_do(op: str, *args):
    r = redis_client()
    if r is None:
        raise RuntimeError("redis-not-configured")
    last: Exception | None = None
    for _ in range(2):
        try:
            return await getattr(r, op)(*args)
        except Exception as e:
            last = e
            await asyncio.sleep(0.5)
    raise last


async def gban_startup_check():
    problem = redis_problem()
    if problem:
        print(f"GBAN: 無効（{problem}）")
        return
    try:
        await _redis_do("ping")
        print("GBAN: 有効（Redisに接続できました）")
    except Exception as e:
        print(f"GBAN: Redisに接続できません: {safe_error(e)}")


async def close_redis():
    global _redis
    if _redis is not None:
        try:
            await (_redis.aclose() if hasattr(_redis, "aclose") else _redis.close())
        except Exception:
            pass
        _redis = None


async def gban_get(uid: int, *, strict: bool = False) -> dict | None:
    if redis_client() is None:
        if strict:
            raise RuntimeError("redis-not-configured")
        return None
    try:
        raw = await _redis_do("get", f"gban:{uid}")
    except Exception as e:
        if strict:
            raise
        print(f"GBAN確認エラー: {safe_error(e)}")
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"reason": str(raw)}
    except Exception:
        return {"reason": str(raw)}


async def gban_set(uid: int, reason: str, by: int) -> None:
    value = json.dumps({"reason": reason, "by": by,
                        "at": datetime.now(JST).strftime("%Y-%m-%d %H:%M")},
                       ensure_ascii=False)
    await _redis_do("set", f"gban:{uid}", value)


async def gban_del(uid: int) -> bool:
    return bool(await _redis_do("delete", f"gban:{uid}"))


async def get_log_channel_id(guild_id: int) -> int | None:
    try:
        raw = await _redis_do("get", f"config:log_channel:{guild_id}")
    except Exception as e:
        if not isinstance(e, RuntimeError):
            print(f"ログチャンネルの取得エラー: {safe_error(e)}")
        return None
    return int(raw) if raw and str(raw).isdigit() else None


async def set_log_channel_id(guild_id: int, channel_id: int) -> None:
    await _redis_do("set", f"config:log_channel:{guild_id}", str(channel_id))


async def send_guild_log(guild: discord.Guild, embed: discord.Embed) -> bool:
    cid = await get_log_channel_id(guild.id)
    if cid is None:
        return False
    ch = guild.get_channel(cid)
    if not isinstance(ch, discord.TextChannel):
        return False
    perms = ch.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return False
    try:
        await ch.send(embed=embed)
        return True
    except Exception as e:
        print(f"ログの送信エラー: {safe_error(e)}")
        return False


def gban_log_embed(uid: int, reason: str, *, auto: bool, ok: bool = True) -> discord.Embed:
    if ok:
        title = "🚫 Global BAN対象者を自動BANしました" if auto else "🚫 Global BANによりBANしました"
        color = 0xEF4444
    else:
        title = "⚠️ Global BAN対象者をBANできませんでした"
        color = 0xF59E0B
    e = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    e.add_field(name="👤 対象ユーザー", value=f"<@{uid}>（`{uid}`）", inline=False)
    e.add_field(name="📄 理由", value=(reason or "理由なし")[:300], inline=False)
    e.add_field(name="🔧 実行",
                value="参加時の自動BAN" if auto else "Bot運営によるGBAN登録（一括BAN）",
                inline=False)
    if not ok:
        e.add_field(name="ℹ️ 補足",
                    value="Botのロールがそのユーザーのロールより下にあるか、「メンバーをBAN」の権限がありません。",
                    inline=False)
    return e


async def gban_auto_ban(member: discord.Member) -> None:
    rec = await gban_get(member.id)
    if rec is None:
        return
    reason = str(rec.get("reason") or "理由なし")
    try:
        await member.guild.ban(member, reason=f"GBAN: {reason}"[:500])
        await send_guild_log(member.guild, gban_log_embed(member.id, reason, auto=True))
    except discord.Forbidden:
        print(f"GBANの自動BAN: 権限が足りません（{member.guild.name}）")
        await send_guild_log(member.guild, gban_log_embed(member.id, reason, auto=False, ok=False))
    except Exception as e:
        print(f"GBANの自動BANエラー: {safe_error(e)}")


# ---------- FastAPI / Webサーバー ----------
limiter = Limiter(key_func=lambda req: req.client.host if req.client else "127.0.0.1")
app = FastAPI()
app.state.limiter = limiter

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/")
async def index_page(request: Request, guild: int = Query(...)):
    if is_guild_blacklisted(guild) or is_license_suspended(guild):
        return HTMLResponse("<h1>このサーバーの認証機能は停止されています。</h1>", status_code=403)
    
    return HTMLResponse(f"<h1>認証ページ (Server ID: {guild})</h1><a href='/login?guild={guild}'>Discordでログインして認証</a>")

@app.get("/login")
async def login_page(guild: int = Query(...)):
    state = secrets.token_hex(16)
    states[state] = (time.time(), guild)
    discord_auth_url = f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify+guilds.join&state={state}"
    return RedirectResponse(discord_auth_url)

@app.get("/callback")
async def callback(code: str = Query(...), state: str = Query(...)):
    if state not in states:
        return HTMLResponse("<h1>無効な状態（state）です。やり直してください。</h1>", status_code=400)
    
    issued_time, guild_id = states.pop(state)
    if time.time() - issued_time > STATE_TTL:
        return HTMLResponse("<h1>セッションが切れました。やり直してください。</h1>", status_code=400)

    if is_license_suspended(guild_id):
        return HTMLResponse("<h1>このサーバーはライセンスが停止されています。</h1>", status_code=403)

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            f"{API}/oauth2/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        if token_resp.status_code != 200:
            return HTMLResponse("<h1>Discord認証に失敗しました。</h1>", status_code=400)
        
        token_data = token_resp.json()
        access_token = token_data.get("access_token")

        user_resp = await client.get(
            f"{API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        if user_resp.status_code != 200:
            return HTMLResponse("<h1>ユーザー情報の取得に失敗しました。</h1>", status_code=400)
        
        user_data = user_resp.json()
        user_id = int(user_data["id"])

        if is_user_blacklisted(user_id) or is_server_user_blacklisted(guild_id, user_id):
            return HTMLResponse("<h1>あなたはブラックリストに登録されているため認証できません。</h1>", status_code=403)

        try:
            guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
            cfg = load_config().get(str(guild_id), {})
            role_id = cfg.get("verified_role_id")
            if role_id:
                role = guild.get_role(int(role_id))
                if role:
                    await member.add_roles(role, reason="Web認証完了")
        except Exception as e:
            print(f"ロール付与エラー: {e}")

    return HTMLResponse("<h1>認証が完了しました！Discordにお戻りください。</h1>")


# ---------- バックグラウンドワーカー・起動処理 ----------
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(bot.start(BOT_TOKEN))
    await gban_startup_check()

@app.on_event("shutdown")
async def shutdown_event():
    await close_redis()
    await bot.close()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)

