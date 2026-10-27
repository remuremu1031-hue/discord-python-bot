import asyncio
import atexit
import copy
import html
import json
import logging
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
from discord.ext import tasks
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, RedirectResponse

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

# ステータス表示チャンネルの自動更新用設定（環境変数から取得、未設定ならNone）
STATUS_CHANNEL_ID = int(os.getenv("STATUS_CHANNEL_ID", "0") or "0")
STATUS_MESSAGE_ID = int(os.getenv("STATUS_MESSAGE_ID", "0") or "0")

BASE_URL = REDIRECT_URI.removesuffix("/callback").rstrip("/")
API = "https://discord.com/api/v10"
STATE_TTL = 600  # 秒
CONFIG_FILE = Path("guild_config.json")  # サーバーごとの設定
BROADCAST_FILE = Path("broadcast_channels.json")  # お知らせの送信先（サーバーごと）
BLACKLIST_FILE = Path("blacklist.json")  # ブラックリスト（ユーザー / サーバー）
SERVER_BLACKLIST_FILE = Path("server_blacklist.json")  # サーバーごとのブラックリスト
LICENSE_FILE = Path("license_suspended.json")  # ライセンス停止中のサーバー
INDEX_PATH = Path(__file__).parent / "index.html"  # main.pyと同じフォルダのindex.html
JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"

states: dict[str, tuple[float, int]] = {}  # state -> (発行時刻, guild_id)


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
                          f"{type(e).__name__}: {e}")
                    time.sleep(2 ** attempt)
            print(f"⚠️ {name} をUpstashに保存できませんでした。"
                  "このまま再起動すると、最近の変更が失われます。")

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


BROADCAST_WARNING = (
    "⚠️ **お知らせの送信先が未設定です。**\n"
    "未設定のままだと、Botからのお知らせは"
    "**サーバーのチャンネル一覧の一番上**（Botが送信できるテキストチャンネル）に届きます。\n"
    "`/broadcast-channel` で送信先を決められます。"
)


def auth_url(guild_id: int) -> str:
    return f"{BASE_URL}/?guild={guild_id}"


# ---------- Discord Bot ----------
KEEP_COMMANDS = {"setup", "authlink", "panel", "broadcast", "broadcast-channel", "blacklist", "server-blacklist", "license"}  # 残すスラッシュコマンド


class AuthBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self._owner_ids: set[int] | None = None
        self._status_task: asyncio.Task | None = None
        self.status_message_id = STATUS_MESSAGE_ID  # リアルタイムステータス用

    async def get_owner_ids(self) -> set[int]:
        """Botのオーナー（チーム所有の場合はチームのオーナー）のIDを返す。"""
        if self._owner_ids is None:
            info = await self.application_info()
            if info.team:
                self._owner_ids = {info.team.owner_id}
            else:
                self._owner_ids = {info.owner.id}
        return self._owner_ids

    # ---------- 動的ステータス（視聴中） ----------
    def build_status_text(self) -> str:
        guilds = len(self.guilds)
        members = sum((g.member_count or 0) for g in self.guilds)
        return f"remuremu | {guilds:,}サーバー | {members:,}人"

    async def update_status(self):
        if not self.is_ready() or self.is_closed():
            return  # 切断・再接続中は更新しない（"Cannot write to closing transport" 対策）
        try:
            await self.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(type=discord.ActivityType.watching,
                                          name=self.build_status_text()),
            )
        except Exception as e:  # 切断中などで失敗しても、Bot全体は止めない
            print(f"ステータス更新エラー: {e}")

    def request_status_update(self, delay: float = 5.0):
        """参加・退出が連続しても、まとめて1回だけ更新する（レート制限対策）。"""
        if self._status_task and not self._status_task.done():
            return  # すでに予約済み
        self._status_task = asyncio.create_task(self._delayed_status_update(delay))

    async def _delayed_status_update(self, delay: float):
        await asyncio.sleep(delay)
        await self.update_status()

    async def on_guild_join(self, guild: discord.Guild):
        # ブラックリストのサーバーに招待されたら、すぐに退出する
        if is_guild_blacklisted(guild.id):
            try:
                await guild.leave()
                print(f"ブラックリストのサーバーから退出しました: {guild.name} ({guild.id})")
            except Exception as e:
                print(f"サーバーからの退出に失敗 ({guild.id}): {e}")
            return
        self.request_status_update()

    async def leave_blacklisted_guilds(self):
        """すでに参加しているブラックリストのサーバーから退出する。"""
        banned = set(load_blacklist()["guilds"])
        if not banned:
            return
        # setup_hook の時点では self.guilds が空なので、REST（fetch_guilds）で取得する
        async for g in self.fetch_guilds(limit=None):
            if g.id in banned:
                try:
                    await g.leave()
                    print(f"ブラックリストのサーバーから退出しました: {g.name} ({g.id})")
                except Exception as e:
                    print(f"サーバーからの退出に失敗 ({g.id}): {e}")

    async def on_guild_remove(self, guild: discord.Guild):
        self.request_status_update()

    @tasks.loop(minutes=10)
    async def refresh_status(self):
        # メンバー数は変動するので、10分ごとにも更新する
        await self.update_status()

    # ---------- リアルタイムステータス表示の定期更新タスク ----------
    @tasks.loop(minutes=5)
    async def update_live_status_channel(self):
        if not STATUS_CHANNEL_ID:
            return
        channel = self.get_channel(STATUS_CHANNEL_ID)
        if not channel:
            try:
                channel = await self.fetch_channel(STATUS_CHANNEL_ID)
            except Exception:
                return

        latency = round(self.latency * 1000)
        guilds = len(self.guilds)
        members = sum((g.member_count or 0) for g in self.guilds)
        now_str = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")

        embed = discord.Embed(
            title="🟢【公式】認証Bot 稼働ステータス",
            description="現在のシステム稼働状況です。（5分ごとに自動更新）",
            color=0x2ECC71,
        )
        embed.add_field(name="● システムステータス", value="🟢 **認証API:** 正常稼働中\n🟢 **Botプロセス:** 正常稼働中", inline=False)
        embed.add_field(name="📡 応答速度 (Ping)", value=f"`{latency}ms`", inline=True)
        embed.add_field(name="🏠 導入サーバー数", value=f"`{guilds:,}` サーバー", inline=True)
        embed.add_field(name="👤 総メンバー数", value=f"`{members:,}` 人", inline=True)
        embed.set_footer(text=f"最終更新日時: {now_str}")

        try:
            if self.status_message_id:
                try:
                    msg = await channel.fetch_message(self.status_message_id)
                    await msg.edit(embed=embed)
                    return
                except discord.NotFound:
                    pass  # メッセージが消されていた場合は新しく投稿し直す
            
            # 新規送信
            msg = await channel.send(embed=embed)
            self.status_message_id = msg.id
            print(f"[StatusUpdater] リアルタイムステータスメッセージを新規送信しました。MESSAGE_ID: {msg.id}")
            print("※次回以降の起動用に .env に STATUS_MESSAGE_ID=<このID> を設定することをおすすめします。")
        except Exception as e:
            print(f"[StatusUpdater] ステータスチャンネルの更新に失敗: {e}")

    @update_live_status_channel.before_loop
    async def before_live_status(self):
        await self.wait_until_ready()

    async def setup_hook(self):
        if SYNC_COMMANDS:
            try:
                await self.tree.sync()  # グローバルコマンドを現在のコードの内容に同期
            except Exception as e:
                print(f"コマンドの同期に失敗: {type(e).__name__}: {e}")
        try:
            await self.leave_blacklisted_guilds()  # 起動時：ブラックリストのサーバーから退出
        except Exception as e:
            print(f"ブラックリストのサーバーからの退出処理に失敗: {type(e).__name__}: {e}")

    async def on_ready(self):
        # ステータス更新ループを開始（再接続でon_readyが再度呼ばれても二重起動しない）
        if not self.refresh_status.is_running():
            self.refresh_status.start()
        if STATUS_CHANNEL_ID and not self.update_live_status_channel.is_running():
            self.update_live_status_channel.start()

        # 起動時に1回だけ、不要な古いスラッシュコマンド（例: /veryfi-panel）を削除する
        if not PURGE_OLD_COMMANDS or getattr(self, "_purged", False):
            return
        self._purged = True
        await self.purge_old_commands()

    async def purge_old_commands(self):
        """このBot(アプリ)に登録されているコマンドのうち、KEEP_COMMANDS以外を全て削除。"""
        # グローバルコマンド
        try:
            for cmd in await self.tree.fetch_commands():
                if cmd.name not in KEEP_COMMANDS:
                    await cmd.delete()
                    print(f"古いコマンドを削除しました: /{cmd.name} (グローバル)")
        except Exception as e:
            print(f"グローバルコマンドの削除に失敗: {e}")
        # サーバー限定コマンド（このBotでは使わないので全て削除）
        for g in self.guilds:
            try:
                for cmd in await self.tree.fetch_commands(guild=g):
                    await cmd.delete()
                    print(f"古いコマンドを削除しました: /{cmd.name} ({g.name})")
            except Exception as e:
                print(f"サーバー限定コマンドの削除に失敗 ({g.name}): {e}")


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
    text = (f"✅ 設定しました\n"
            f"認証ロール: {role.mention}\n"
            f"ログチャンネル: {log_channel.mention}\n\n"
            f"認証ページURL（このサーバー専用）:\n{auth_url(guild.id)}")
    if str(guild.id) not in load_broadcast():
        text += "\n\n" + BROADCAST_WARNING
    await interaction.response.send_message(text, ephemeral=True)


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
        title="🔐 サーバー認証",
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


def _can_send(ch: discord.abc.GuildChannel | None, guild: discord.Guild) -> bool:
    if not isinstance(ch, discord.TextChannel):
        return False
    p = ch.permissions_for(guild.me)
    return p.view_channel and p.send_messages and p.embed_links


def _first_sendable(guild: discord.Guild) -> discord.TextChannel | None:
    return next((c for c in guild.text_channels if _can_send(c, guild)), None)


def pick_broadcast_channel(guild: discord.Guild) -> tuple[discord.TextChannel | None, str]:
    """送信先を決める。戻り値は (チャンネル, 状態)。状態は set / unset / fallback。"""
    cid = load_broadcast().get(str(guild.id))
    if cid is None:
        return _first_sendable(guild), "unset"      # 未設定 → 一番上
    ch = guild.get_channel(cid)
    if _can_send(ch, guild):
        return ch, "set"                            # 管理者が設定したチャンネル
    return _first_sendable(guild), "fallback"       # 設定先が使えない → 一番上


@bot.tree.command(name="broadcast-channel",
                  description="Botからのお知らせを受け取るチャンネルを設定します（省略で解除）")
@app_commands.describe(channel="お知らせを受け取るチャンネル（省略すると設定を解除）")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def broadcast_channel_cmd(interaction: discord.Interaction,
                                channel: discord.TextChannel | None = None):
    guild = interaction.guild
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return

    data = load_broadcast()
    if channel is None:
        data.pop(str(guild.id), None)
        save_broadcast(data)
        await interaction.response.send_message(
            "🔕 お知らせの送信先の設定を解除しました。\n\n" + BROADCAST_WARNING,
            ephemeral=True)
        return

    if not _can_send(channel, guild):
        await interaction.response.send_message(
            f"{channel.mention} でBotに「チャンネルを見る・メッセージを送信・"
            "埋め込みリンク」の権限がありません。", ephemeral=True)
        return

    data[str(guild.id)] = channel.id
    save_broadcast(data)
    await interaction.response.send_message(
        f"✅ 今後のBotからのお知らせは {channel.mention} に届きます。", ephemeral=True)


@bot.tree.command(name="broadcast",
                  description="（オーナー専用）Botが参加している全サーバーにお知らせを送信します")
@app_commands.describe(message="送信するお知らせの本文（改行したい所は \\n と入力）")
@app_commands.default_permissions(administrator=True)
async def broadcast_cmd(interaction: discord.Interaction, message: str):
    # --- Botのオーナーだけが実行できる ---
    if interaction.user.id not in await bot.get_owner_ids():
        await interaction.response.send_message(
            "このコマンドはBotのオーナー専用です。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    embed = discord.Embed(
        title="📢 運営からのお知らせ",
        description=message.replace("\\n", "\n"),
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"{bot.user.name} 開発者より")

    counts = {"set": 0, "unset": 0, "fallback": 0}
    failed: list[str] = []
    for guild in list(bot.guilds):
        target, state = pick_broadcast_channel(guild)
        if target is None:
            failed.append(f"{guild.name}（送信できるチャンネルなし）")
            continue
        try:
            await target.send(embed=embed)
            counts[state] += 1
        except Exception as e:
            failed.append(f"{guild.name}（{type(e).__name__}）")
        await asyncio.sleep(0.5)  # レート制限対策

    success = sum(counts.values())
    result = (f"📢 送信が完了しました\n"
              f"✅ 成功: {success} サーバー\n"
              f"❌ 失敗: {len(failed)} サーバー")
    if counts["unset"]:
        result += f"\nℹ️ 送信先が未設定で、一番上のチャンネルへ送信: {counts['unset']} サーバー"
    if counts["fallback"]:
        result += f"\nℹ️ 設定先が使えず、一番上のチャンネルへ送信: {counts['fallback']} サーバー"
    if failed:
        result += "\n\n失敗したサーバー:\n" + "\n".join(f"・{n}" for n in failed[:10])
        if len(failed) > 10:
            result += f"\n…ほか {len(failed) - 10} 件"
    await interaction.followup.send(result[:1900], ephemeral=True)


@bot.tree.command(name="blacklist",
                  description="（オーナー専用）ユーザー / サーバーのブラックリストを管理します")
@app_commands.describe(action="追加 / 削除 / 一覧表示",
                       target_type="対象の種類（一覧では省略すると両方表示）",
                       target_id="対象のID（数字）。追加・削除のときに指定")
@app_commands.choices(
    target_type=[app_commands.Choice(name="ユーザー", value="user"),
                 app_commands.Choice(name="サーバー", value="guild")],
    action=[app_commands.Choice(name="追加 (add)", value="add"),
            app_commands.Choice(name="削除 (remove)", value="remove"),
            app_commands.Choice(name="一覧表示 (list)", value="list")])
@app_commands.default_permissions(administrator=True)
async def blacklist_cmd(interaction: discord.Interaction, action: str,
                        target_type: str | None = None,
                        target_id: str | None = None):
    # --- Botのオーナーだけが実行できる ---
    owner_ids = await bot.get_owner_ids()
    if interaction.user.id not in owner_ids:
        await interaction.response.send_message(
            "このコマンドはBotのオーナー専用です。", ephemeral=True)
        return

    # --- 一覧表示 ---
    if action == "list":
        data = load_blacklist()
        embed = discord.Embed(title="🚫 ブラックリスト一覧", color=0xEF4444)

        def block(lines: list[str]) -> str:
            if not lines:
                return "登録なし"
            text, shown = "", 0
            for ln in lines:
                if len(text) + len(ln) + 1 > 950:  # フィールド上限(1024字)に収める
                    break
                text += ln + "\n"
                shown += 1
            if shown < len(lines):
                text += f"…ほか {len(lines) - shown} 件"
            return text

        if target_type in (None, "user"):
            lines = [f"・`{uid}` <@{uid}>" for uid in sorted(data["users"])]
            embed.add_field(name=f"👤 ユーザー（{len(lines)}件）",
                            value=block(lines), inline=False)
        if target_type in (None, "guild"):
            lines = []
            for gid in sorted(data["guilds"]):
                g = bot.get_guild(gid)
                lines.append(f"・`{gid}` " + (g.name if g else "（Bot未参加）"))
            embed.add_field(name=f"🏠 サーバー（{len(lines)}件）",
                            value=block(lines), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # --- 追加 / 削除 ---
    if target_type is None or target_id is None:
        await interaction.response.send_message(
            "追加・削除のときは、`target_type` と `target_id` の両方を指定してください。",
            ephemeral=True)
        return

    # IDは19桁前後でスラッシュコマンドの整数型に収まらないため、文字列で受け取って変換する
    raw = target_id.strip()
    if not raw.isdigit() or not (15 <= len(raw) <= 21):
        await interaction.response.send_message(
            "IDは数字（15〜21桁）で入力してください。", ephemeral=True)
        return
    tid = int(raw)

    key = "users" if target_type == "user" else "guilds"
    label = "ユーザー" if target_type == "user" else "サーバー"
    data = load_blacklist()

    if action == "add":
        if target_type == "user" and tid in owner_ids:
            await interaction.response.send_message(
                "オーナー自身はブラックリストに追加できません。", ephemeral=True)
            return
        if tid in data[key]:
            await interaction.response.send_message(
                f"{label} `{tid}` はすでに登録されています。", ephemeral=True)
            return
        data[key].append(tid)
        save_blacklist(data)
        msg = f"🚫 {label} `{tid}` をブラックリストに追加しました。"
        # すでに参加しているサーバーなら、その場で退出する
        if target_type == "guild":
            g = bot.get_guild(tid)
            if g is not None:
                try:
                    await g.leave()
                    msg += f"\nBotは「{g.name}」から退出しました。"
                except Exception as e:
                    msg += f"\n⚠️ 退出に失敗しました: {type(e).__name__}"
    else:
        if tid not in data[key]:
            await interaction.response.send_message(
                f"{label} `{tid}` は登録されていません。", ephemeral=True)
            return
        data[key].remove(tid)
        save_blacklist(data)
        msg = f"✅ {label} `{tid}` をブラックリストから削除しました。"

    msg += (f"\n\n現在の登録数: ユーザー {len(data['users'])} 件 / "
            f"サーバー {len(data['guilds'])} 件")
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="server-blacklist",
                  description="このサーバーの認証をブロックするユーザーを管理します")
@app_commands.describe(action="追加 / 削除 / 一覧表示",
                       user="対象のユーザー（メンバーから選択）",
                       user_id="対象のユーザーID（サーバーにいない人はこちら）")
@app_commands.choices(action=[
    app_commands.Choice(name="追加 (add)", value="add"),
    app_commands.Choice(name="削除 (remove)", value="remove"),
    app_commands.Choice(name="一覧表示 (list)", value="list")])
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def server_blacklist_cmd(interaction: discord.Interaction, action: str,
                               user: discord.User | None = None,
                               user_id: str | None = None):
    guild = interaction.guild
    # サーバーのオーナー・管理者（オーナーは常に管理者権限を持つ）だけが実行できる
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return

    key = str(guild.id)
    data = load_server_blacklist()
    ids = data.get(key, [])

    # --- 一覧表示 ---
    if action == "list":
        lines, shown_len = [], 0
        for uid in sorted(ids):
            ln = f"・`{uid}` <@{uid}>"
            if shown_len + len(ln) + 1 > 3800:
                lines.append(f"…ほか {len(ids) - len(lines)} 件")
                break
            lines.append(ln)
            shown_len += len(ln) + 1
        embed = discord.Embed(
            title=f"🚫 このサーバーのブラックリスト（{len(ids)}件）",
            description="\n".join(lines) if lines else "登録なし",
            color=0xEF4444)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # --- 追加 / 削除：対象を決める ---
    if user is not None:
        tid = user.id
    elif user_id:
        raw = user_id.strip()
        if not raw.isdigit() or not (15 <= len(raw) <= 21):
            await interaction.response.send_message(
                "ユーザーIDは数字（15〜21桁）で入力してください。", ephemeral=True)
            return
        tid = int(raw)
    else:
        await interaction.response.send_message(
            "`user` か `user_id` のどちらかを指定してください。", ephemeral=True)
        return

    if action == "add":
        if tid == interaction.user.id:
            await interaction.response.send_message(
                "自分自身は追加できません。", ephemeral=True)
            return
        if tid == guild.owner_id or tid == bot.user.id:
            await interaction.response.send_message(
                "サーバーのオーナーやBotは追加できません。", ephemeral=True)
            return
        if tid in ids:
            await interaction.response.send_message(
                f"<@{tid}>（`{tid}`）はすでに登録されています。", ephemeral=True)
            return
        ids.append(tid)
        data[key] = ids
        save_server_blacklist(data)
        msg = (f"🚫 <@{tid}>（`{tid}`）をこのサーバーのブラックリストに追加しました。\n"
               "このサーバーでは認証できなくなります。"
               "（すでに付与されているロールは外れません）")
    else:
        if tid not in ids:
            await interaction.response.send_message(
                f"<@{tid}>（`{tid}`）は登録されていません。", ephemeral=True)
            return
        ids.remove(tid)
        data[key] = ids
        save_server_blacklist(data)
        msg = f"✅ <@{tid}>（`{tid}`）をこのサーバーのブラックリストから削除しました。"

    msg += f"\n\n現在の登録数: {len(ids)} 件"
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="license",
                  description="（オーナー専用）サーバーの利用ライセンスを管理します")
@app_commands.describe(action="操作を選択",
                       guild_id="対象のサーバーID（停止・有効化のときに指定）")
@app_commands.choices(action=[
    app_commands.Choice(name="ライセンス停止 (suspend)", value="suspend"),
    app_commands.Choice(name="ライセンス有効化 (active)", value="active"),
    app_commands.Choice(name="停止中のサーバー一覧 (list)", value="list")])
@app_commands.default_permissions(administrator=True)
async def license_cmd(interaction: discord.Interaction, action: str,
                      guild_id: str | None = None):
    # --- Botのオーナーだけが実行できる ---
    if interaction.user.id not in await bot.get_owner_ids():
        await interaction.response.send_message(
            "このコマンドはBotのオーナー専用です。", ephemeral=True)
        return

    data = load_license()

    # --- 停止中のサーバー一覧 ---
    if action == "list":
        lines, used = [], 0
        for gid, info in sorted(data.items()):
            g = bot.get_guild(int(gid))
            name = g.name if g else "（Bot未参加）"
            at = info.get("at")
            ln = f"・`{gid}` {name}" + (f" ／ 停止: {at}" if at else "")
            if used + len(ln) + 1 > 3800:
                lines.append(f"…ほか {len(data) - len(lines)} 件")
                break
            lines.append(ln)
            used += len(ln) + 1
        embed = discord.Embed(
            title=f"⏸️ ライセンス停止中のサーバー（{len(data)}件）",
            description="\n".join(lines) if lines else "停止中のサーバーはありません",
            color=0xF59E0B)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # --- 停止 / 有効化：サーバーIDを確認 ---
    raw = (guild_id or "").strip()
    if not raw.isdigit() or not (15 <= len(raw) <= 21):
        await interaction.response.send_message(
            "停止・有効化のときは、`guild_id` にサーバーID（数字15〜21桁）を指定してください。",
            ephemeral=True)
        return
    gid = str(int(raw))
    g = bot.get_guild(int(gid))
    label = f"「{g.name}」（`{gid}`）" if g else f"サーバー `{gid}`"

    if action == "suspend":
        if gid in data:
            await interaction.response.send_message(
                f"{label} はすでにライセンス停止中です。", ephemeral=True)
            return
        data[gid] = {"at": datetime.now(JST).strftime("%Y-%m-%d %H:%M")}
        save_license(data)
        msg = (f"⏸️ {label} のライセンスを停止しました。\n"
               "認証ページ（`/?guild=…`・`/login`・`/callback`）は、すぐにブロックされます。"
               "Botはサーバーに残ります。")
    else:  # active
        if gid not in data:
            await interaction.response.send_message(
                f"{label} はライセンス停止中ではありません。", ephemeral=True)
            return
        data.pop(gid)
        save_license(data)
        msg = f"✅ {label} のライセンスを有効化しました。認証ページが再び使えます。"

    msg += f"\n\n現在の停止中: {len(data)} サーバー"
    await interaction.response.send_message(msg, ephemeral=True)


_bot_fatal = False  # トークン無効など、再試行しても直らない失敗


async def run_bot():
    """ログインに失敗しても諦めず、間隔をあけて再試行する。接続後の再接続はdiscord.pyに任せる。"""
    global _bot_fatal
    delay = 15
    while True:
        try:
            await bot.login(BOT_TOKEN)
            break
        except discord.LoginFailure:
            _bot_fatal = True
            print("Botトークンが無効です。DISCORD_BOT_TOKEN を確認してください。")
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Discordへのログインに失敗: {type(e).__name__}: {e}"
                  f"（{delay}秒後に再試行）")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)
    try:
        await bot.connect(reconnect=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"Discordとの接続が終了しました: {type(e).__name__}: {e}")


async def watchdog():
    """Botが長時間オフラインなら、プロセスごと終了してホスト側の自動再起動に任せる。"""
    if WATCHDOG_SECONDS <= 0:
        return
    offline_since: float | None = None
    while True:
        await asyncio.sleep(30)
        if _bot_fatal:
            return  # 再起動しても直らないので何もしない
        if bot.is_ready():
            offline_since = None
            continue
        now = time.time()
        if offline_since is None:
            offline_since = now
        elif now - offline_since > WATCHDOG_SECONDS:
            print(f"Botが{WATCHDOG_SECONDS}秒以上オフラインのため、プロセスを再起動します")
            os._exit(1)


async def bot_ready(timeout: float = READY_TIMEOUT) -> bool:
    """Botの準備完了を、タイムアウト付きで待つ（無限に待たない）。"""
    if bot.is_ready():
        return True
    try:
        await asyncio.wait_for(bot.wait_until_ready(), timeout)
        return True
    except (asyncio.TimeoutError, RuntimeError):
        return False


def _log_task_result(task: asyncio.Task):
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:  # バックグラウンドの失敗を握りつぶさずログに出す
        print(f"バックグラウンドタスクが異常終了: {type(e).__name__}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    bg_tasks = [asyncio.create_task(run_bot()), asyncio.create_task(watchdog())]
    for t in bg_tasks:
        t.add_done_callback(_log_task_result)
    try:
        yield
    finally:
        await bot.close()
        for t in bg_tasks:
            t.cancel()


app = FastAPI(lifespan=lifespan)


# ---------- 認証ログ ----------
async def send_auth_log(channel_id: int, user: dict, role: discord.Role | None,
                        age: timedelta, passed: bool, blacklisted: bool = False):
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
    elif blacklisted:
        title, color = "🚫 ブラックリスト拒否ログ", 0xEF4444
        sub = "🚫 ブラックリストに登録されたユーザー"
        role_text = "❌ 付与なし (ブロック)"
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
def result_page(ok: bool, title: str, message: str,
                status: int | None = None) -> HTMLResponse:
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
    return HTMLResponse(body, status_code=status or (200 if ok else 400))


def blocked_page(title: str, message: str) -> HTMLResponse:
    """ブラックリストによるブロック専用のエラーページ。"""
    body = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;
background:#140b0f;color:#e5e7eb;font-family:system-ui,-apple-system,sans-serif}}
.card{{width:100%;max-width:440px;margin:16px;padding:36px 28px;border-radius:16px;
background:#1f1217;border:1px solid #7f1d1d;text-align:center;
box-shadow:0 0 32px rgba(239,68,68,.25)}}
.icon{{font-size:2.6rem;margin-bottom:8px}}
h1{{margin:0 0 14px;font-size:1.35rem;color:#f87171}}
p{{margin:0;line-height:1.8;color:#9ca3af}}
</style></head><body><div class="card">
<div class="icon">🚫</div>
<h1>{html.escape(title)}</h1>
<p>{html.escape(message)}</p>
</div></body></html>"""
    return HTMLResponse(body, status_code=403)


def license_suspended_page() -> HTMLResponse:
    """ライセンス停止中のサーバー向けの専用エラーページ（403）。"""
    body = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ライセンス停止中</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;
background:#16110a;color:#e5e7eb;font-family:system-ui,-apple-system,sans-serif}
.card{width:100%;max-width:440px;margin:16px;padding:36px 28px;border-radius:16px;
background:#201808;border:1px solid #92400e;text-align:center;
box-shadow:0 0 32px rgba(245,158,11,.22)}
.icon{font-size:2.6rem;margin-bottom:8px}
h1{margin:0 0 14px;font-size:1.35rem;color:#fbbf24}
p{margin:0;line-height:1.8;color:#9ca3af}
</style></head><body><div class="card">
<div class="icon">⏸️</div>
<h1>ライセンス停止中</h1>
<p>このサーバーの認証機能は、ライセンスが停止されているため利用できません。<br>
サーバーの管理者にお問い合わせください。</p>
</div></body></html>"""
    return HTMLResponse(body, status_code=403)


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


# ---------- 利用規約 / プライバシーポリシー ----------
LEGAL_CSS = """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#0b0f19;color:#e5e7eb;line-height:1.9;font-size:16px;
font-family:system-ui,-apple-system,"Hiragino Sans","Noto Sans JP","Yu Gothic",sans-serif;
word-break:break-word;overflow-wrap:anywhere}
.wrap{max-width:760px;margin:0 auto;padding:28px 16px 56px}
.card{background:rgba(255,255,255,.05);border:1px solid rgba(88,101,242,.45);
border-radius:16px;padding:28px 22px;box-shadow:0 0 28px rgba(88,101,242,.18)}
h1{font-size:1.45rem;line-height:1.5;margin:0 0 18px;color:#fff}
.lead{color:#9ca3af;margin:0 0 6px}
h2{font-size:1.1rem;line-height:1.6;margin:32px 0 10px;padding-left:12px;
border-left:4px solid #5865f2;color:#c7d2fe}
p{margin:0 0 12px}
ul{margin:0 0 12px;padding-left:1.4em}
li{margin:6px 0}
li::marker{color:#8b5cf6}
nav{margin-top:26px;text-align:center;font-size:.92rem;color:#6b7280}
nav a{color:#93c5fd;text-decoration:none;margin:0 10px}
nav a.current{color:#e5e7eb;font-weight:600;pointer-events:none}
@media (min-width:640px){.wrap{padding-top:48px}.card{padding:42px 46px}h1{font-size:1.75rem}}
"""

TERMS_TITLE = "【認証ボット】利用規約"
TERMS_INTRO = ("この利用規約（以下、「本規約」）は、霊夢(remuremu17)（以下、「当方」）が提供する"
               "Discord認証ボット（以下、「本ボット」）の利用条件を定めるものです。"
               "本ボットを利用するすべてのユーザー（以下、「ユーザー」）は、本規約に同意したものとみなされます。")
TERMS_SECTIONS = [
    ("第1条（適用）", [
        ("p", "本規約は、ユーザーと当方との間の本ボットの利用に関わる一切の関係に適用されます。"),
    ]),
    ("第2条（機能と利用）", [
        ("p", "本ボットは、Discordサーバーのセキュリティ強化およびメンバーのロール自動付与を"
              "目的としたOAuth2認証機能を提供します。"),
        ("p", "ユーザーは、Discordの利用規約およびコミュニティガイドラインを遵守し、"
              "自己の責任において本ボットを利用するものとします。"),
    ]),
    ("第3条（禁止事項）", [
        ("p", "ユーザーは、本ボットの利用にあたり、以下の行為を行ってはならないものとします。"),
        ("ul", ["本ボットのシステムに過度な負荷をかける行為、またはハッキングや不正アクセスを試みる行為",
                "不正な目的や他のユーザーになりすまして認証を行う行為",
                "本ボットの運営を妨害する行為、または他のユーザーに迷惑をかける行為",
                "その他、当方が不適切と判断する行為"]),
    ]),
    ("第4条（免責事項）", [
        ("p", "当方は、本ボットの動作にバグや中断がないことを保証するものではありません。"),
        ("p", "本ボットの利用または利用不能によってユーザーに生じた損害について、"
              "当方に故意または重大な過失がある場合を除き、一切の責任を負いません。"),
    ]),
]

PRIVACY_TITLE = "【認証ボット】プライバシーポリシー"
PRIVACY_INTRO = ("霊夢(remuremu17)（以下、「当方」）は、提供するDiscord認証ボット（以下、「本ボット」）における"
                 "ユーザーの個人情報の取扱いについて、以下の通りプライバシーポリシー（以下、「本ポリシー」）を定めます。")
PRIVACY_SECTIONS = [
    ("1. 取得する情報", [
        ("p", "本ボットのOAuth2認証機能を利用する際、以下の情報を取得する場合があります。"),
        ("ul", ["DiscordのユーザーID、ユーザー名（識別・認証のため）、アバター",
                "認証時に許可された最小限のスコープ（例：identify 等の基本情報）",
                "認証と同時に、対象のサーバーへ自動で参加させるための許可（guilds.join）"]),
    ]),
    ("2. 情報の利用目的", [
        ("p", "取得した情報は、以下の目的のためにのみ利用します。"),
        ("ul", ["Discordサーバーにおけるユーザーの本人確認およびロールの自動付与",
                "認証に関するログの記録、および不正アクセスの防止・トラブルシューティング"]),
    ]),
    ("3. 第三者提供の制限", [
        ("p", "当方は、法令に基づく場合を除き、ユーザーの同意を得ることなく、"
              "取得した個人情報を第三者に提供することはありません。"),
    ]),
    ("4. データの管理", [
        ("p", "取得した情報は、不正アクセスや漏洩を防ぐために適切な管理を行います。"
              "また、不要となったデータは定期的に削除または匿名化に努めます。"),
    ]),
    ("5. データの開示・削除", [
        ("p", "ユーザーから取得した情報の開示、修正、または削除の希望があった場合、"
              "本人確認を行った上で、法令および合理的な範囲で速やかに対応いたします。"),
    ]),
    ("6. お問い合わせ", [
        ("p", "本ポリシーに関するお問い合わせは、当サーバーの管理メンバー（または管理者）までご連絡ください。"),
    ]),
]


def legal_page(title: str, intro: str, sections: list, current: str) -> HTMLResponse:
    """利用規約・プライバシーポリシー共通のページ（スマホ対応）。"""
    parts = []
    for heading, blocks in sections:
        parts.append(f"<section><h2>{html.escape(heading)}</h2>")
        for kind, val in blocks:
            if kind == "ul":
                items = "".join(f"<li>{html.escape(x)}</li>" for x in val)
                parts.append(f"<ul>{items}</ul>")
            else:
                parts.append(f"<p>{html.escape(val)}</p>")
        parts.append("</section>")
    terms_cls = ' class="current"' if current == "terms" else ""
    privacy_cls = ' class="current"' if current == "privacy" else ""
    body = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{LEGAL_CSS}</style></head><body>
<div class="wrap"><div class="card">
<h1>{html.escape(title)}</h1>
<p class="lead">{html.escape(intro)}</p>
{"".join(parts)}
</div>
<nav><a href="/terms"{terms_cls}>利用規約</a>|<a href="/privacy"{privacy_cls}>プライバシーポリシー</a></nav>
</div></body></html>"""
    return HTMLResponse(body)


@app.api_route("/terms", methods=["GET", "HEAD"])
async def terms_page():
    return legal_page(TERMS_TITLE, TERMS_INTRO, TERMS_SECTIONS, "terms")


@app.api_route("/privacy", methods=["GET", "HEAD"])
async def privacy_page():
    return legal_page(PRIVACY_TITLE, PRIVACY_INTRO, PRIVACY_SECTIONS, "privacy")


# ---------- ルーティング ----------
@app.api_route("/health", methods=["GET", "HEAD"])  # UptimeRobotのHEADにも対応
async def health():
    """定期アクセス(cron-job等)用の軽量なエンドポイント。"""
    return {"ok": True, "bot_ready": bot.is_ready(), "guilds": len(bot.guilds),
            "storage": "upstash" if storage.remote else "local-file"}


@app.api_route("/", methods=["GET", "HEAD"])  # UptimeRobotのHEADにも対応
async def index(guild: int | None = None):
    if guild is None:
        # サーバー指定なしのアクセス（死活監視のping等）は、200で応答する
        return result_page(True, "認証Bot 稼働中",
                           "認証ページは、サーバー管理者が案内するURLからアクセスしてください。")
    if is_license_suspended(guild):
        return license_suspended_page()
    if str(guild) not in load_config():
        return result_page(False, "認証URLが無効です",
                           "サーバー管理者に正しい認証URLを確認してください。")
    if is_guild_blacklisted(guild):
        return blocked_page("このサーバーでは利用できません",
                            "このサーバーでは、認証機能の利用が制限されています。")
    page = INDEX_PATH.read_text(encoding="utf-8")
    # ログインボタンのリンクに guild を引き継ぐ
    page = page.replace("/login", f"/login?guild={guild}")
    return HTMLResponse(page)


@app.get("/login")
async def login(guild: int | None = None):
    if guild is not None and is_license_suspended(guild):
        return license_suspended_page()
    if guild is None or str(guild) not in load_config():
        return result_page(False, "認証URLが無効です",
                           "このサーバーは設定されていません。")
    if is_guild_blacklisted(guild):
        return blocked_page("このサーバーでは利用できません",
                            "このサーバーでは、認証機能の利用が制限されています。")
    if not await bot_ready():
        return result_page(False, "Botが起動中です",
                           "少し待ってから、もう一度お試しください。", status=503)
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

    if not await bot_ready():
        return result_page(False, "Botが起動中です",
                           "少し待ってから、このページを更新してください。", status=503)

    issued = states.pop(state or "", None)
    if issued is None or time.time() - issued[0] > STATE_TTL:
        return result_page(False, "セッションが無効です",
                           "最初からやり直してください。")
    guild_id = issued[1]

    # ライセンス停止中のサーバーからの認証はブロック
    if is_license_suspended(guild_id):
        return license_suspended_page()

    # ブラックリストのサーバーからの認証はブロック
    if is_guild_blacklisted(guild_id):
        return blocked_page("このサーバーでは利用できません",
                            "このサーバーでは、認証機能の利用が制限されています。")

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

        # 2.4) ブラックリストのユーザーはブロック
        if is_user_blacklisted(user_id):
            try:
                await asyncio.wait_for(bot.wait_until_ready(), READY_TIMEOUT)
                await send_auth_log(log_channel_id, user, None, age, False,
                                    blacklisted=True)
            except Exception as e:
                print(f"認証ログ送信エラー: {e}")
            return blocked_page("認証できません",
                                "このアカウントは、認証の利用が制限されています。"
                                "心当たりがない場合は、サーバーの管理者にお問い合わせください。")
        # 2.45) このサーバーのブラックリスト
        if is_server_user_blacklisted(guild_id, user_id):
            try:
                await asyncio.wait_for(bot.wait_until_ready(), READY_TIMEOUT)
                await send_auth_log(log_channel_id, user, None, age, False,
                                    blacklisted=True)
            except Exception as e:
                print(f"認証ログ送信エラー: {e}")
            return blocked_page("認証できません",
                                "このサーバーでは、あなたのアカウントの認証が制限されています。"
                                "心当たりがない場合は、サーバーの管理者にお問い合わせください。")

        if age < timedelta(days=MIN_ACCOUNT_AGE_DAYS):
            try:
                await asyncio.wait_for(bot.wait_until_ready(), READY_TIMEOUT)
                await send_auth_log(log_channel_id, user, None, age, False)
            except Exception as e:
                print(f"認証ログ送信エラー: {e}")
            return result_page(
                False, "認証できません",
                f"アカウント作成から{MIN_ACCOUNT_AGE_DAYS}日以上経過している必要があります。"
                "時間をおいてから再度お試しください。")

        # 3) ロール付与 + 認証ログ
        try:
            await asyncio.wait_for(bot.wait_until_ready(), READY_TIMEOUT)
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

        except asyncio.TimeoutError:
            return result_page(False, "Botが起動中です",
                               "少し待ってから、もう一度お試しください。", status=503)
        except discord.Forbidden:
            return result_page(False, "権限エラー",
                               "Botのロールが付与対象ロールより下か、権限が不足しています。")
        except discord.HTTPException as e:
            return result_page(False, "Discord APIエラー", str(e))

    return result_page(True, "認証完了",
                       f"{username} さん、認証が完了しました。Discordに戻ってください。")


# ---------- Cloudflare クイックトンネル ----------
def _download_cloudflared(path: Path) -> bool:
    """cloudflared が無ければ GitHub から取得する（Linuxのみ）。"""
    arch = {"x86_64": "amd64", "amd64": "amd64",
            "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if platform.system() != "Linux" or arch is None:
        return False
    url = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
           f"cloudflared-linux-{arch}")
    try:
        print("cloudflared をダウンロードしています...")
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        return True
    except Exception as e:
        print(f"cloudflared のダウンロードに失敗: {e}")
        path.unlink(missing_ok=True)
        return False


def start_quick_tunnel(port: int, timeout: float = 45.0) -> str | None:
    """cloudflared を起動し、発行された https://xxxx.trycloudflare.com を返す。"""
    binary = Path(__file__).parent / "cloudflared"
    if not binary.exists() and not _download_cloudflared(binary):
        print("Cloudflare Tunnel 起動エラー: cloudflared を用意できませんでした")
        return None
    try:
        binary.chmod(0o755)
    except OSError:
        pass

    try:
        proc = subprocess.Popen(
            [str(binary), "tunnel", "--no-autoupdate",
             "--url", f"http://localhost:{port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except Exception as e:
        print(f"Cloudflare Tunnel 起動エラー: {e}")
        return None
    atexit.register(proc.terminate)

    found = threading.Event()
    holder: dict[str, str] = {}

    def _watch():  # 出力は最後まで読み続ける（詰まり防止）
        for line in proc.stderr:
            if "url" in holder:
                continue
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and m.group(0) != "https://api.trycloudflare.com":
                holder["url"] = m.group(0)
                found.set()

    threading.Thread(target=_watch, daemon=True).start()

    if not found.wait(timeout):
        print("Cloudflare Tunnel 起動エラー: URLを取得できませんでした（タイムアウト）")
        return None
    return holder["url"]


if __name__ == "__main__":
    # run() を使わないと discord.py のログが出ないため、接続状況や429を見えるようにする
    discord.utils.setup_logging(level=logging.INFO, root=False)

    # 設定を起動時に読み込む（Upstashに繋がらないときは、空データで上書きしないよう停止する）
    try:
        storage.preload([("guild_config", CONFIG_FILE), ("broadcast", BROADCAST_FILE),
                         ("blacklist", BLACKLIST_FILE),
                         ("server_blacklist", SERVER_BLACKLIST_FILE),
                         ("license", LICENSE_FILE)])
    except Exception as e:
        raise SystemExit("設定の読み込みに失敗しました（Upstashに接続できません）: "
                         f"{type(e).__name__}: {e}")
    print("設定の保存先:", "Upstash（外部Redis）" if storage.remote
          else "ローカルのJSONファイル（再起動で消える環境では設定が失われます）")

    if USE_QUICK_TUNNEL:
        tunnel_url = start_quick_tunnel(PORT)
        if tunnel_url:
            BASE_URL = tunnel_url
            REDIRECT_URI = f"{tunnel_url}/callback"
            print("=" * 60)
            print("Cloudflare Tunnel 起動成功")
            print(f"公開URL     : {BASE_URL}")
            print(f"Redirect URI: {REDIRECT_URI}")
            print("→ Developer Portal の OAuth2 > Redirects に上のURLを登録してください")
            print("=" * 60)

    if not REDIRECT_URI:
        raise SystemExit("REDIRECT_URI が決まりません。トンネルの起動に失敗したか、"
                         .env に REDIRECT_URI を設定してください。")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
