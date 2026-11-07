import asyncio
import atexit
import copy
import hashlib
import hmac
import html
import importlib.util
import io
import ipaddress
import json
import logging
import math
import os
import platform
import re
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
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

def _import_optional(name: str):
    """main.py と同じ階層 → cogs/ フォルダの順に探す。見つからなければ None（その機能だけ無効）。"""
    import importlib
    for mod in (name, f"cogs.{name}"):
        try:
            return importlib.import_module(mod)
        except ModuleNotFoundError as e:
            if e.name not in (name, "cogs", f"cogs.{name}"):
                raise  # ファイルはあるが、中で別のモジュールが足りない
    print(f"⚠️ {name}.py が見つかりません（main.py と同じ場所か cogs/ に置いてください）。この機能は無効になります。")
    return None


dashboard = _import_optional("dashboard")  # 所有者専用Webダッシュボード（dashboard.py）
transcript = _import_optional("transcript")  # チケットのトランスクリプト生成（transcript.py）


# ---------- Botのログをメモリにも残す（ダッシュボードで閲覧するため） ----------
LOG_BUFFER: deque = deque(maxlen=int(os.getenv("LOG_BUFFER_LINES", "3000")))
BOOT_TIME = time.time()


class _LogTee(io.TextIOBase):
    """print() や discord.py のログを、元の出力に流しつつ、メモリにも保存する。"""

    def __init__(self, stream, level: str):
        self._stream = stream
        self._level = level
        self._partial = ""

    def write(self, s):
        try:
            self._stream.write(s)
        except Exception:
            pass
        self._partial += s
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            if line.strip():
                LOG_BUFFER.append((time.time(), self._level, line[:2000]))
        return len(s)

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")


sys.stdout = _LogTee(sys.stdout, "OUT")
sys.stderr = _LogTee(sys.stderr, "ERR")

load_dotenv()

# ---------- 設定 ----------
CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
REDIRECT_URI = os.getenv("REDIRECT_URI", "")  # クイックトンネル使用時は起動時に自動設定される
PORT = int(os.getenv("PORT", "8080"))
USE_QUICK_TUNNEL = os.getenv("USE_QUICK_TUNNEL", "1") != "0"  # 0で無効化
# --- IPアドレスによるサブアカウント検知（旧「作成3日以内＝サブ垢」の判定は廃止） ---
IP_RETENTION_DAYS = int(os.getenv("IP_RETENTION_DAYS", "90"))  # IPの記録を保持する日数
STORE_RAW_IP = os.getenv("STORE_RAW_IP", "0") == "1"  # 1で生のIPも保存（既定はハッシュ＋一部伏せ字のみ）
IP_HASH_KEY = (os.getenv("IP_HASH_KEY") or "").encode() or hashlib.sha256(
    ("ip-hash:" + os.environ["DISCORD_CLIENT_SECRET"]).encode()).digest()  # 再起動しても変わらない鍵
SUBACCOUNT_DEFAULT_ACTION = os.getenv("SUBACCOUNT_DEFAULT_ACTION", "block")  # block / log
# --- Nuke / Raid 対策 ---
NUKE_WINDOW = int(os.getenv("NUKE_WINDOW_SECONDS", "10"))  # この秒数の間に…
NUKE_LIMITS = {  # …この回数を超えたら、実行者をNukeとみなす
    "channel_delete": int(os.getenv("NUKE_CHANNEL_DELETE", "3")),
    "channel_create": int(os.getenv("NUKE_CHANNEL_CREATE", "6")),
    "role_delete": int(os.getenv("NUKE_ROLE_DELETE", "3")),
    "role_create": int(os.getenv("NUKE_ROLE_CREATE", "6")),
    "ban": int(os.getenv("NUKE_BAN", "3")),
    "kick": int(os.getenv("NUKE_KICK", "3")),
    "webhook_create": int(os.getenv("NUKE_WEBHOOK_CREATE", "4")),
    "prune": 1,
}
RAID_JOIN_THRESHOLD = int(os.getenv("RAID_JOIN_THRESHOLD", "10"))  # この人数が…
RAID_JOIN_WINDOW = int(os.getenv("RAID_JOIN_WINDOW", "15"))  # …この秒数以内に参加したらRaid
RAID_LOCK_MINUTES = int(os.getenv("RAID_LOCK_MINUTES", "10"))  # Raidモードを続ける分数
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

BASE_URL = REDIRECT_URI.strip().rstrip("/").removesuffix("/callback").rstrip("/")  # 末尾の / があっても正しく扱う
API = "https://discord.com/api/v10"
STATE_TTL = 600  # 秒
CONFIG_FILE = Path("guild_config.json")  # サーバーごとの設定
BROADCAST_FILE = Path("broadcast_channels.json")  # お知らせの送信先（サーバーごと）
BLACKLIST_FILE = Path("blacklist.json")  # ブラックリスト（ユーザー / サーバー）
SERVER_BLACKLIST_FILE = Path("server_blacklist.json")  # サーバーごとのブラックリスト
LICENSE_FILE = Path("license_suspended.json")  # ライセンス停止中のサーバー
STATUS_FILE = Path("status_message.json")  # 稼働状況メッセージのID
DEFENSE_CONFIG_FILE = Path("defense_config.json")  # Nuke防衛・招待リンクブロックの設定
BACKUP_DATA_FILE = Path("server_backups.json")  # サーバーバックアップ
IP_INDEX_FILE = Path("ip_index.json")  # 認証時のIP（ハッシュ）とアカウントの対応
AUTH_LOG_FILE = Path("auth_logs.json")  # 認証ログ（ダッシュボード用）
INCIDENT_FILE = Path("security_incidents.json")  # Nuke/Raid検知の履歴
TICKET_CONFIG_FILE = Path("ticket_config.json")  # トランスクリプトの送信先
SUBACCOUNT_CONFIG_FILE = Path("subaccount_config.json")  # サブアカウント検知時の動作
AUTH_LOG_MAX = 500
INCIDENT_MAX = 300
INDEX_PATH = Path(__file__).parent / "index.html"  # main.pyと同じフォルダのindex.html
JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"

states: dict[str, tuple[float, int]] = {}  # state -> (発行時刻, guild_id)


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
    global _suspended_cache
    storage.save("license", LICENSE_FILE, data)
    _suspended_cache = (0.0, frozenset())  # すぐに反映する


_suspended_cache: tuple[float, frozenset] = (0.0, frozenset())


def suspended_guild_ids() -> frozenset:
    """停止中のサーバーID（イベントごとに呼ばれるので、2秒だけキャッシュする）。"""
    global _suspended_cache
    now = time.monotonic()
    if now - _suspended_cache[0] > 2:
        _suspended_cache = (now, frozenset(int(k) for k in load_license()))
    return _suspended_cache[1]


def is_license_suspended(guild_id: int) -> bool:
    return int(guild_id or 0) in suspended_guild_ids()


# ---------- Nuke防衛・バックアップの設定 ----------
_defense_cache: tuple[float, dict] = (0.0, {})


def load_defense_config() -> dict:
    return storage.load("defense_config", DEFENSE_CONFIG_FILE)


def save_defense_config(data: dict) -> None:
    global _defense_cache
    storage.save("defense_config", DEFENSE_CONFIG_FILE, data)
    _defense_cache = (0.0, {})  # 次の参照で読み直す


def get_defense(guild_id: int) -> dict:
    """そのサーバーの防衛設定。メッセージごとに呼ばれるので、30秒キャッシュする。"""
    global _defense_cache
    now = time.monotonic()
    if now - _defense_cache[0] > 30:
        _defense_cache = (now, load_defense_config())
    return _defense_cache[1].get(str(guild_id), {})


def load_backups() -> dict:
    return storage.load("server_backups", BACKUP_DATA_FILE)


def save_backups(data: dict) -> None:
    storage.save("server_backups", BACKUP_DATA_FILE, data)


# ---------- IPアドレスの記録（サブアカウント検知・GBAN/ブラックリスト連携） ----------
def normalize_ip(raw: str) -> str | None:
    """照合に使う形へ。IPv6は同じ回線でも末尾が変わるので /64 単位にまとめる。
    プライベート・ループバックなど、本人を表さないアドレスは None（判定に使わない）。"""
    try:
        ip = ipaddress.ip_address((raw or "").strip())
    except ValueError:
        return None
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified \
            or ip.is_multicast or ip.is_reserved:
        return None
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def ip_hash(norm_ip: str) -> str:
    """IPの鍵付きハッシュ（同じIPかどうかは分かるが、元のIPには戻せない）。"""
    return hmac.new(IP_HASH_KEY, norm_ip.encode(), hashlib.sha256).hexdigest()[:32]


def mask_ip(norm_ip: str) -> str:
    """表示用に一部を伏せたIP（例: 203.0.113.xxx / 2001:db8:1234::/64 → 2001:db8:xxxx::/64）。"""
    if ":" in norm_ip:
        head = norm_ip.split(":")
        return ":".join(head[:2]) + ":xxxx::/64"
    p = norm_ip.split(".")
    return ".".join(p[:3]) + ".xxx" if len(p) == 4 else "?"


def load_ip_index() -> dict:
    """-> {"ハッシュ": {"m": 伏せ字IP, "raw": 生IP(任意), "e": [{"u": ユーザーID, "g": サーバーID, "t": 時刻}]}}"""
    return storage.load("ip_index", IP_INDEX_FILE)


def _prune_ip_index(data: dict) -> dict:
    cutoff = time.time() - IP_RETENTION_DAYS * 86400
    out = {}
    for h, rec in data.items():
        ents = [e for e in rec.get("e", []) if e.get("t", 0) >= cutoff][-40:]
        if ents:
            rec["e"] = ents
            out[h] = rec
    if len(out) > 5000:  # 保存サイズが増えすぎないよう、最近使われたものだけ残す
        keep = sorted(out.items(), key=lambda kv: kv[1]["e"][-1]["t"], reverse=True)[:5000]
        out = dict(keep)
    return out


def record_ip(h: str, norm_ip: str, uid: int, gid: int) -> None:
    data = load_ip_index()
    rec = data.setdefault(h, {"e": []})
    rec["m"] = mask_ip(norm_ip)
    if STORE_RAW_IP:
        rec["raw"] = norm_ip
    else:
        rec.pop("raw", None)
    # 同じユーザー・同じサーバーの古い記録は、最新のものに置き換える
    rec["e"] = [e for e in rec.get("e", []) if not (e.get("u") == uid and e.get("g") == gid)]
    rec["e"].append({"u": uid, "g": gid, "t": int(time.time())})
    storage.save("ip_index", IP_INDEX_FILE, _prune_ip_index(data))


def ip_linked_users(h: str, uid: int) -> dict[int, set[int]]:
    """同じIPから認証した、本人以外のユーザー -> {ユーザーID: {サーバーID...}}"""
    cutoff = time.time() - IP_RETENTION_DAYS * 86400
    out: dict[int, set[int]] = {}
    for e in load_ip_index().get(h, {}).get("e", []):
        if e.get("u") != uid and e.get("t", 0) >= cutoff:
            out.setdefault(int(e["u"]), set()).add(int(e["g"]))
    return out


def user_ip_hashes(uid: int) -> list[str]:
    return [h for h, rec in load_ip_index().items()
            if any(e.get("u") == uid for e in rec.get("e", []))]


def get_subaccount_action(guild_id: int) -> str:
    v = storage.load("subaccount_config", SUBACCOUNT_CONFIG_FILE).get(str(guild_id))
    return v if v in ("block", "log") else SUBACCOUNT_DEFAULT_ACTION


# ---------- 認証ログ・セキュリティ事案の履歴（ダッシュボード用） ----------
def append_auth_log(entry: dict) -> None:
    data = storage.load("auth_logs", AUTH_LOG_FILE)
    items = data.get("items", [])
    items.append(entry)
    storage.save("auth_logs", AUTH_LOG_FILE, {"items": items[-AUTH_LOG_MAX:]})


def load_auth_logs() -> list[dict]:
    return storage.load("auth_logs", AUTH_LOG_FILE).get("items", [])


def record_incident(entry: dict) -> None:
    data = storage.load("security_incidents", INCIDENT_FILE)
    items = data.get("items", [])
    entry.setdefault("t", int(time.time()))
    items.append(entry)
    storage.save("security_incidents", INCIDENT_FILE, {"items": items[-INCIDENT_MAX:]})


def load_incidents() -> list[dict]:
    return storage.load("security_incidents", INCIDENT_FILE).get("items", [])


BROADCAST_WARNING = (
    "⚠️ **お知らせの送信先が未設定です。**\n"
    "未設定のままだと、Botからのお知らせは"
    "**サーバーのチャンネル一覧の一番上**（Botが送信できるテキストチャンネル）に届きます。\n"
    "`/broadcast-channel` で送信先を決められます。"
)


def auth_url(guild_id: int) -> str:
    return f"{BASE_URL}/?guild={guild_id}"


# ---------- Discord Bot ----------
SUSPENDED_MESSAGE = ("このサーバーは、ライセンスが停止されているため、コマンドを使えません。"
                     "サーバーの管理者にお問い合わせください。")


class LicenseTree(app_commands.CommandTree):
    """ライセンス停止中のサーバーでは、すべてのスラッシュコマンドを使えなくする。
    （Botのオーナーは、ライセンスの管理のため対象外）"""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        deny = guard_check_interaction(interaction)  # 自動防御（ブロック中・ブラックリスト・連打）
        if deny:
            await _deny_interaction(interaction, deny)
            return False
        gid = interaction.guild_id
        try:
            if gid is None or not is_license_suspended(gid):
                return True
            if interaction.user.id in await interaction.client.get_owner_ids():
                return True
        except Exception:
            return True  # 状態を確認できないときは、止めない
        try:
            if interaction.response.is_done():
                await interaction.followup.send(SUSPENDED_MESSAGE, ephemeral=True)
            else:
                await interaction.response.send_message(SUSPENDED_MESSAGE, ephemeral=True)
        except discord.HTTPException:
            pass
        return False


def get_transcript_channel_id(guild_id: int) -> int | None:
    v = storage.load("ticket_config", TICKET_CONFIG_FILE).get(str(guild_id), {})
    cid = v.get("transcript_channel") if isinstance(v, dict) else None
    return int(cid) if cid else None


async def save_ticket_transcript(channel: discord.TextChannel, saved_by: discord.abc.User,
                                 *, reason: str = "手動保存") -> tuple[list[discord.File], str]:
    """チケットの会話をHTMLとテキストにまとめ、設定された送信先チャンネルへ送る。
    戻り値: (保存者へ渡すファイル, 結果メッセージ)"""
    if transcript is None:
        raise RuntimeError("transcript.py が見つかりません")
    messages = [m async for m in channel.history(limit=5000, oldest_first=True)]
    html_text = transcript.build_html(channel, messages, saved_by=saved_by,
                                      bot_name=bot.user.name if bot.user else "Bot")
    txt_text = transcript.build_text(channel, messages, saved_by=saved_by)
    base = re.sub(r"[^\w.-]", "_", channel.name)[:60] or "ticket"
    stamp = datetime.now(JST).strftime("%Y%m%d-%H%M")

    def files() -> list[discord.File]:
        return [discord.File(io.BytesIO(html_text.encode("utf-8")),
                             filename=f"transcript-{base}-{stamp}.html"),
                discord.File(io.BytesIO(txt_text.encode("utf-8")),
                             filename=f"transcript-{base}-{stamp}.txt")]

    participants: dict[int, discord.abc.User] = {}
    for m in messages:
        participants.setdefault(m.author.id, m.author)
    result = "送信先チャンネルが未設定のため、あなたにだけ送りました（`/transcript-channel` で設定できます）。"
    cid = get_transcript_channel_id(channel.guild.id)
    dest = channel.guild.get_channel(cid) if cid else None
    if isinstance(dest, discord.TextChannel) and dest.id != channel.id:
        embed = discord.Embed(title="📝 チケットのトランスクリプト", color=0x5865F2,
                              timestamp=datetime.now(timezone.utc))
        embed.add_field(name="チケット", value=f"#{channel.name}（`{channel.id}`）", inline=False)
        embed.add_field(name="保存者", value=f"{saved_by.mention}（`{saved_by.id}`）", inline=True)
        embed.add_field(name="理由", value=reason, inline=True)
        embed.add_field(name="メッセージ数", value=f"{len(messages)} 件", inline=True)
        plist = " ".join(u.mention for u in list(participants.values())[:30]) or "-"
        embed.add_field(name=f"参加者（{len(participants)}人）", value=plist[:1000], inline=False)
        embed.set_footer(text="HTMLファイルをダウンロードしてブラウザで開くと、会話をきれいに表示できます")
        try:
            await dest.send(embed=embed, files=files(),
                            allowed_mentions=discord.AllowedMentions.none())
            result = f"{dest.mention} に保存しました。"
        except discord.HTTPException as e:
            result = f"{dest.mention} への送信に失敗しました（{safe_error(e)}）。あなたにだけ送ります。"
    return files(), result


class TicketControlView(discord.ui.View):
    """チケットのトランスクリプト保存・削除ボタン（永続View）。"""

    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_license_suspended(interaction.guild_id or 0):
            await interaction.response.send_message(SUSPENDED_MESSAGE, ephemeral=True)
            return False
        return True

    @discord.ui.button(label="トランスクリプト保存", style=discord.ButtonStyle.primary,
                       custom_id="ticket_transcript", emoji="📝", row=0)
    async def transcript_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            files, result = await save_ticket_transcript(interaction.channel, interaction.user)
        except discord.Forbidden:
            await interaction.followup.send("❌ メッセージ履歴を読む権限がありません。", ephemeral=True)
            return
        except RuntimeError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        await interaction.followup.send(f"📝 トランスクリプトを作成しました。{result}",
                                        files=files, ephemeral=True)

    @discord.ui.button(label="チケット削除", style=discord.ButtonStyle.danger,
                       custom_id="ticket_delete", emoji="🗑️", row=0)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ このボタンを押す権限がありません。", ephemeral=True)
            return
        await interaction.response.send_message("🗑️ トランスクリプトを保存して、まもなくこのチャンネルを削除します...",
                                                ephemeral=True)
        if get_transcript_channel_id(interaction.guild_id or 0):  # 送信先があれば、削除前に自動保存
            try:
                await save_ticket_transcript(interaction.channel, interaction.user, reason="削除前の自動保存")
            except Exception as e:
                print(f"削除前のトランスクリプト保存に失敗: {safe_error(e)}")
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason=f"チケット削除: {interaction.user}")
        except Exception as e:
            print(f"チャンネル削除エラー: {safe_error(e)}")


KEEP_COMMANDS = {"setup", "authlink", "panel", "broadcast", "broadcast-channel", "blacklist", "server-blacklist", "license", "gban", "set-log-channel",
                 "mass_role", "ticket", "warn", "warnings", "reactionrole", "timeout", "kick",
                 "ban", "automod", "eventlog", "clearwarnings", "untimeout", "unban", "nuke-defense", "backup",
                 "nuke-whitelist", "subaccount", "transcript-channel", "help",
                 "weather", "disaster", "sticky", "invite-stats", "invite-log"}  # 残すスラッシュコマンド


EXTRA_COGS = ["mass_role", "tickets", "warns", "reaction_roles",
              "event_logs", "moderation", "automod",
              "jma_alerts", "help", "sticky", "invite_tracker"]  # cogs/ フォルダの追加機能

# ファイル名の別名（例: reaction_role.py でも reaction_roles として読み込む）
COG_ALIASES = {"reaction_roles": ["reaction_role"], "event_logs": ["event_log"],
               "tickets": ["ticket"], "warns": ["warn"]}

# ライセンス停止中でも止めないイベント（Bot自体の接続・サーバーの出入りなど）
LICENSE_EXEMPT_EVENTS = {
    "ready", "connect", "disconnect", "resumed", "shard_connect", "shard_disconnect",
    "shard_ready", "shard_resumed", "guild_join", "guild_remove", "guild_available",
    "guild_unavailable", "guild_update", "socket_event_type", "socket_raw_receive",
    "socket_raw_send", "error", "app_command_completion", "command_error",
}


def _event_guild_id(args) -> int | None:
    """イベントの引数から、どのサーバーのイベントかを調べる。"""
    for a in args:
        try:
            if isinstance(a, discord.Guild):
                return a.id
            gid = getattr(a, "guild_id", None)
            if isinstance(gid, int):
                return gid
            g = getattr(a, "guild", None)
            if isinstance(g, discord.Guild):
                return g.id
        except Exception:
            continue
    return None


def _event_actor_id(args) -> int | None:
    for a in args:
        for attr in ("user", "author"):
            try:
                u = getattr(a, attr, None)
                if u is not None and isinstance(getattr(u, "id", None), int):
                    return u.id
            except Exception:
                continue
    return None


class AuthBot(commands.Bot):
    def __init__(self):
        # commands.Bot は self.tree（CommandTree）を最初から持っている
        intents = discord.Intents.default()
        extra: dict = {}
        if ENABLE_MESSAGE_CONTENT_INTENT:  # 削除・編集ログの内容表示、招待リンクの検知に使う
            intents.message_content = True
        if ENABLE_MEMBERS_INTENT:  # 参加時の自動BAN用（メンバー情報は保持せず、メモリを節約する）
            intents.members = True
            extra = dict(chunk_guilds_at_startup=False,
                         member_cache_flags=discord.MemberCacheFlags.none())
        super().__init__(command_prefix=commands.when_mentioned,
                         intents=intents, help_command=None, tree_cls=LicenseTree, **extra)
        self._owner_ids: set[int] | None = None
        self._status_task: asyncio.Task | None = None

    async def on_message(self, message: discord.Message):
        # メッセージ起点のコマンドは使わない（スラッシュコマンドのみ）。Cogのリスナーは別に動く。
        # /nuke-defense で「招待リンクのブロック」を有効にしたサーバーでは、招待リンクを削除する
        if message.guild and not message.author.bot and isinstance(message.author, discord.Member):
            if get_defense(message.guild.id).get("invite_block", False):
                perms = message.author.guild_permissions
                if not (perms.administrator or perms.manage_messages):
                    if message.content and re.search(
                            r"(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+",
                            message.content, re.I):
                        try:
                            await message.delete()
                            await message.channel.send(
                                f"{message.author.mention} 招待リンクの投稿は禁止されています。",
                                delete_after=5)
                        except discord.HTTPException:
                            pass

    def is_suspended(self, guild_id: int) -> bool:
        """そのサーバーがライセンス停止中か（cogs から使う）。"""
        try:
            return is_license_suspended(guild_id)
        except Exception:
            return False

    # --- 追加機能（cogs/）が使う窓口 ---
    def kv_ready(self) -> bool:
        return redis_client() is not None

    async def kv(self, op: str, *args):
        """Redisの操作（例: await bot.kv("get", "key")）。未設定なら RuntimeError。"""
        return await _redis_do(op, *args)

    async def guild_log(self, guild: discord.Guild, embed: discord.Embed) -> bool:
        """/set-log-channel で設定された、そのサーバーのログチャンネルへ送る。"""
        return await send_guild_log(guild, embed)

    def store_load(self, name: str) -> dict:
        """設定の読み込み（Upstash があればそこ、なければ <name>.json）。"""
        return storage.load(name, Path(f"{name}.json"))

    def store_save(self, name: str, data: dict) -> None:
        storage.save(name, Path(f"{name}.json"), data)

    def is_owner_id(self, user_id: int) -> bool:
        return user_id in (self._owner_ids or set())

    # ---------- ライセンス停止中は、バックグラウンド処理もすべて止める ----------
    def dispatch(self, event_name: str, /, *args, **kwargs):
        """すべてのイベント（自動モデレーション・イベントログ・Nuke検知・cogsのリスナー等）の入口。
        停止中のサーバーのイベントは、Botのオーナーの操作を除いて、ここで捨てる。"""
        if event_name == "interaction" and args and isinstance(args[0], discord.Interaction) \
                and args[0].type in (discord.InteractionType.component, discord.InteractionType.modal_submit):
            inter = args[0]
            deny = guard_check_interaction(inter)
            if deny:
                asyncio.create_task(_deny_interaction(inter, deny))
                return
        try:
            suspended = suspended_guild_ids()
            if suspended and event_name not in LICENSE_EXEMPT_EVENTS:
                gid = _event_guild_id(args)
                if gid in suspended and _event_actor_id(args) not in (self._owner_ids or set()):
                    if event_name == "interaction" and args:
                        self._reply_suspended(args[0])
                    return
        except Exception:
            pass  # 判定できないときは、通常どおり処理する
        super().dispatch(event_name, *args, **kwargs)

    def _reply_suspended(self, interaction) -> None:
        """ボタン・セレクト・モーダル（cogs のもの含む）に、停止中であることを返す。
        スラッシュコマンドは LicenseTree が返すので、ここでは扱わない。"""
        if not isinstance(interaction, discord.Interaction):
            return
        if interaction.type not in (discord.InteractionType.component,
                                    discord.InteractionType.modal_submit):
            return

        async def _send():
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(SUSPENDED_MESSAGE, ephemeral=True)
            except discord.HTTPException:
                pass
        asyncio.create_task(_send())

    def _install_view_gate(self) -> None:
        """登録済みView（永続ボタン等）の処理も、停止中のサーバーでは実行しない。"""
        store = getattr(self._connection, "_view_store", None)
        if store is None:
            return
        for name in ("dispatch_view", "dispatch_modal"):
            orig = getattr(store, name, None)
            if orig is None:
                continue

            def gated(*a, __orig=orig, **kw):
                inter = next((x for x in list(a) + list(kw.values())
                              if isinstance(x, discord.Interaction)), None)
                if inter is not None and inter.user.id not in (self._owner_ids or set()) and (
                        guard_blocked("user", inter.user.id) or is_user_blacklisted(inter.user.id)
                        or (inter.guild_id and is_license_suspended(inter.guild_id))):
                    return None  # 返信は dispatch("interaction") 側で行う
                return __orig(*a, **kw)
            setattr(store, name, gated)

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

    async def on_member_join(self, member: discord.Member):
        # GBAN登録されたユーザーが参加したら、すぐにBANする
        try:
            await gban_auto_ban(member)
        except Exception as e:
            print(f"GBANの自動BAN処理エラー: {safe_error(e)}")
        try:  # 大量参加（Raid）の検知
            await raid_check_join(member)
        except Exception as e:
            print(f"Raid検知の処理エラー: {safe_error(e)}")

    @tasks.loop(hours=24)
    async def auto_backup(self):
        """Nuke防衛が有効なサーバーは、1日1回自動でバックアップする（復元用）。"""
        cfg = load_defense_config()
        for g in list(self.guilds):
            if not cfg.get(str(g.id), {}).get("nuke_defense") or is_license_suspended(g.id):
                continue
            try:
                take_backup(g, auto=True)
            except Exception as e:
                print(f"自動バックアップに失敗 ({g.id}): {safe_error(e)}")
            await asyncio.sleep(1)

    @auto_backup.before_loop
    async def _before_auto_backup(self):
        await self.wait_until_ready()
        await asyncio.sleep(60)

    @tasks.loop(minutes=10)
    async def refresh_status(self):
        # メンバー数は変動するので、10分ごとにも更新する
        await self.update_status()

    async def load_extra_cog(self, name: str) -> bool:
        """追加機能を読み込む。cogs/ フォルダの中でも、main.py と同じ階層（ルート直下）でも動く。
        例: "jma_alerts" → cogs/jma_alerts.py → jma_alerts.py の順に探す。"""
        candidates = [f"cogs.{name}", name] + [f"cogs.{a}" for a in COG_ALIASES.get(name, [])] \
            + COG_ALIASES.get(name, [])
        for ext in candidates:
            if importlib.util.find_spec(ext.split(".")[0]) is None:
                continue  # そのフォルダ（cogs）自体が無い
            try:
                if importlib.util.find_spec(ext) is None:
                    continue  # そのファイルが無い → 次の候補へ
            except ModuleNotFoundError:
                continue
            try:
                await self.load_extension(ext)
                print(f"追加機能を読み込みました: {ext}")
                return True
            except Exception as e:  # ファイルはあるが、中身でエラー（こちらは原因を表示する）
                print(f"追加機能 {ext} の読み込みに失敗: {type(e).__name__}: {e}")
                return False
        print(f"追加機能 {name} が見つかりません（cogs/{name}.py か {name}.py を置いてください）")
        return False

    async def setup_hook(self):
        self.add_view(TicketControlView())  # 再起動後も、ボタンが動くようにする
        self._install_view_gate()  # ライセンス停止中は、ボタン等の処理も止める
        try:  # 停止中サーバーの判定で、オーナーを素早く判別できるようにする
            await self.get_owner_ids()
        except Exception as e:
            print(f"オーナー情報の取得に失敗: {safe_error(e)}")
        for name in EXTRA_COGS:  # 追加機能（失敗しても、ほかの機能は動かす）
            if name in DISABLED_COGS:
                continue
            await self.load_extra_cog(name)
        if SYNC_COMMANDS:
            try:
                await self.tree.sync()  # グローバルコマンドを現在のコードの内容に同期
            except Exception as e:
                print(f"コマンドの同期に失敗: {type(e).__name__}: {e}")
        try:
            await self.leave_blacklisted_guilds()  # 起動時：ブラックリストのサーバーから退出
        except Exception as e:
            print(f"ブラックリストのサーバーからの退出処理に失敗: {type(e).__name__}: {e}")
        if STATUS_CHANNEL_ID:  # 稼働状況の表示（Cog）を読み込む
            try:
                await self.add_cog(StatusCog(self))
            except Exception as e:
                print(f"稼働状況表示の読み込みに失敗: {safe_error(e)}")

    async def on_ready(self):
        # ステータス更新ループを開始（再接続でon_readyが再度呼ばれても二重起動しない）
        if not self.refresh_status.is_running():
            self.refresh_status.start()
        if not self.auto_backup.is_running():
            self.auto_backup.start()

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


# ---------- セキュリティ・Nuke/Raid対策・バックアップ ----------
# 1) 監査ログを監視し、短時間の大量削除・大量BAN等を「実行者ごと」に数える
# 2) しきい値を超えたら、実行者（荒らし・乗っ取られたBOT等）を即座にBAN / キック / 権限剥奪
# 3) 消されたチャンネル・ロールを復元し、巻き込まれてBANされた人のBANを解除
# 4) サーバーの管理チャンネルとBotオーナーのDMへ緊急警告、ダッシュボードに履歴を残す
_nuke_hits: dict[tuple[int, int, str], deque] = {}
_nuke_active: dict[tuple[int, int], dict] = {}
_deleted_channels: dict[int, dict[int, tuple[float, dict]]] = {}
_deleted_roles: dict[int, dict[int, tuple[float, dict]]] = {}
_join_log: dict[int, deque] = {}
_raid_until: dict[int, float] = {}
_raid_prev_level: dict[int, discord.VerificationLevel] = {}
_dm_last: dict[str, float] = {}
SNAPSHOT_TTL = 900  # 削除されたチャンネル・ロールの情報を覚えておく秒数

NUKE_KIND_LABEL = {
    "channel_delete": "チャンネルの大量削除", "channel_create": "チャンネルの大量作成",
    "role_delete": "ロールの大量削除", "role_create": "ロールの大量作成",
    "ban": "連続BAN", "kick": "連続キック", "webhook_create": "Webhookの大量作成",
    "prune": "メンバーの一括削除（Prune）",
}
NUKE_ACTION_LABEL = {"ban": "BAN", "kick": "キック", "strip": "権限剥奪（ロールを全て外す）"}


def _audit_kinds() -> dict:
    A = discord.AuditLogAction
    pairs = {"channel_delete": "channel_delete", "channel_create": "channel_create",
             "role_delete": "role_delete", "role_create": "role_create", "ban": "ban",
             "kick": "kick", "webhook_create": "webhook_create", "member_prune": "prune"}
    return {getattr(A, k): v for k, v in pairs.items() if hasattr(A, k)}


AUDIT_KINDS = _audit_kinds()


def _ow_list(overwrites: dict) -> list[dict]:
    out = []
    for target, ow in overwrites.items():
        allow, deny = ow.pair()
        if isinstance(target, discord.Role):
            out.append({"kind": "role", "id": target.id, "name": target.name,
                        "everyone": target.is_default(), "allow": allow.value, "deny": deny.value})
        else:
            out.append({"kind": "member", "id": target.id, "allow": allow.value, "deny": deny.value})
    return out


def serialize_channel(ch: discord.abc.GuildChannel) -> dict:
    d = {"id": ch.id, "name": ch.name, "type": str(ch.type), "position": ch.position,
         "category_id": getattr(ch, "category_id", None),
         "category": ch.category.name if getattr(ch, "category", None) else None,
         "overwrites": _ow_list(ch.overwrites)}
    for attr in ("topic", "nsfw", "slowmode_delay", "bitrate", "user_limit"):
        v = getattr(ch, attr, None)
        if v is not None:
            d[attr] = v
    return d


def serialize_role(r: discord.Role) -> dict:
    return {"id": r.id, "name": r.name, "color": r.color.value, "permissions": r.permissions.value,
            "hoist": r.hoist, "mentionable": r.mentionable, "position": r.position}


def take_backup(guild: discord.Guild, *, auto: bool = False) -> dict:
    roles = [serialize_role(r) for r in sorted(guild.roles, key=lambda r: r.position)
             if not r.is_default() and not r.managed]
    channels = [serialize_channel(c) for c in sorted(guild.channels, key=lambda c: c.position)]
    snap = {"v": 2, "time": datetime.now(JST).strftime("%Y-%m-%d %H:%M"), "auto": auto,
            "roles": roles, "channels": channels}
    backups = load_backups()
    lst = backups.setdefault(str(guild.id), [])
    lst.append(snap)
    manual = [b for b in lst if not b.get("auto")][-10:]  # 手動は10件
    autos = [b for b in lst if b.get("auto")][-3:]        # 自動は3件まで残す
    backups[str(guild.id)] = sorted(manual + autos, key=lambda b: b.get("time", ""))
    save_backups(backups)
    return snap


def _build_overwrites(guild: discord.Guild, items: list[dict], role_map: dict[int, discord.Role]):
    out = {}
    for o in items or []:
        try:
            pair = discord.PermissionOverwrite.from_pair(discord.Permissions(int(o["allow"])),
                                                         discord.Permissions(int(o["deny"])))
        except Exception:
            continue
        target = None
        if o.get("kind") == "role":
            if o.get("everyone"):
                target = guild.default_role
            else:
                target = (guild.get_role(int(o["id"])) or role_map.get(int(o["id"]))
                          or discord.utils.get(guild.roles, name=o.get("name")))
        else:
            target = guild.get_member(int(o["id"]))
        if target is not None:
            out[target] = pair
    return out


async def _create_channel(guild: discord.Guild, d: dict, category, role_map) -> discord.abc.GuildChannel:
    ow = _build_overwrites(guild, d.get("overwrites"), role_map)
    kind = d.get("type", "text")
    name = d.get("name", "restored")
    reason = "Nuke対策: バックアップから復元"
    if kind == "category":
        return await guild.create_category(name, overwrites=ow, reason=reason)
    if kind in ("voice", "stage_voice"):
        bitrate = min(int(d.get("bitrate") or 64000), int(guild.bitrate_limit))
        if kind == "stage_voice" and hasattr(guild, "create_stage_channel"):
            return await guild.create_stage_channel(name, category=category, overwrites=ow, reason=reason)
        return await guild.create_voice_channel(name, category=category, overwrites=ow, bitrate=bitrate,
                                                user_limit=int(d.get("user_limit") or 0), reason=reason)
    kw = dict(category=category, overwrites=ow, reason=reason)
    if d.get("topic"):
        kw["topic"] = d["topic"][:1024]
    if kind == "forum" and hasattr(guild, "create_forum"):
        return await guild.create_forum(name, **kw)
    if d.get("nsfw"):
        kw["nsfw"] = True
    if d.get("slowmode_delay"):
        kw["slowmode_delay"] = int(d["slowmode_delay"])
    if kind == "news" and "COMMUNITY" in guild.features:
        kw["news"] = True
    return await guild.create_text_channel(name, **kw)


async def _create_role(guild: discord.Guild, d: dict) -> discord.Role:
    perms = discord.Permissions(int(d.get("permissions", 0)))
    perms.value &= guild.me.guild_permissions.value  # Bot自身が持たない権限は付けられない
    return await guild.create_role(name=d.get("name", "restored"), permissions=perms,
                                   colour=discord.Colour(int(d.get("color", 0))),
                                   hoist=bool(d.get("hoist")), mentionable=bool(d.get("mentionable")),
                                   reason="Nuke対策: バックアップから復元")


async def restore_items(guild: discord.Guild, roles: list[dict], channels: list[dict],
                        *, limit: int = 250) -> dict:
    """ロール → カテゴリ → チャンネルの順に作成する。既存のものは変更・削除しない。"""
    rep = {"roles": 0, "channels": 0, "failed": 0}
    role_map: dict[int, discord.Role] = {}
    made = 0
    for d in sorted(roles, key=lambda x: x.get("position", 0)):
        if made >= limit:
            break
        try:
            r = await _create_role(guild, d)
            if d.get("id"):
                role_map[int(d["id"])] = r
            rep["roles"] += 1
            made += 1
        except discord.HTTPException:
            rep["failed"] += 1
        await asyncio.sleep(0.4)
    cat_map: dict[int, discord.CategoryChannel] = {}
    cats = [c for c in channels if c.get("type") == "category"]
    others = [c for c in channels if c.get("type") != "category"]
    for d in cats + others:
        if made >= limit:
            break
        category = None
        if d.get("type") != "category":
            cid = d.get("category_id")
            category = (guild.get_channel(cid) if cid else None) or cat_map.get(cid or 0)
            if category is None and d.get("category"):
                category = discord.utils.get(guild.categories, name=d["category"])
        try:
            ch = await _create_channel(guild, d, category, role_map)
            if d.get("type") == "category" and d.get("id"):
                cat_map[int(d["id"])] = ch
            rep["channels"] += 1
            made += 1
            if d.get("position") is not None:
                try:
                    await ch.edit(position=int(d["position"]))
                except discord.HTTPException:
                    pass
        except discord.HTTPException:
            rep["failed"] += 1
        await asyncio.sleep(0.4)
    return rep


async def restore_backup(guild: discord.Guild, b: dict) -> dict:
    """バックアップにあって、今のサーバーに無いものだけを作り直す（旧形式のバックアップにも対応）。"""
    role_names = {r.name for r in guild.roles}
    roles = [r for r in b.get("roles", []) if r.get("name") not in role_names]
    existing = {(c.name, str(c.type), c.category.name if c.category else None) for c in guild.channels}
    chans = []
    for c in b.get("channels", []):
        key = (c.get("name"), c.get("type", "text"),
               c.get("category") if c.get("type") != "category" else None)
        if key not in existing:
            c = dict(c)
            c.pop("category_id", None)  # 旧IDは使わず、カテゴリ名で探す
            chans.append(c)
    return await restore_items(guild, roles, chans)


async def send_admin_alert(guild: discord.Guild, embed: discord.Embed) -> bool:
    """サーバーの管理チャンネルへ警告する（/set-log-channel → /setup のログ → システムチャンネルの順）。"""
    if await send_guild_log(guild, embed):
        return True
    gcfg = load_config().get(str(guild.id)) or {}
    for ch in (guild.get_channel(int(gcfg.get("log_channel_id") or 0)), guild.system_channel,
               guild.public_updates_channel):
        if _can_send(ch, guild):
            try:
                await ch.send(embed=embed)
                return True
            except discord.HTTPException:
                continue
    return False


async def dm_owners(embed: discord.Embed, *, dedupe: str | None = None, cooldown: float = 30) -> None:
    """Botのオーナーへ、DMで緊急警告を送る。"""
    now = time.time()
    if dedupe:
        if now - _dm_last.get(dedupe, 0) < cooldown:
            return
        _dm_last[dedupe] = now
    try:
        owner_ids = await bot.get_owner_ids()
    except Exception:
        return
    for oid in owner_ids:
        try:
            user = bot.get_user(oid) or await bot.fetch_user(oid)
            await user.send(embed=embed)
        except Exception as e:
            print(f"オーナーへのDM送信に失敗: {safe_error(e)}")


async def _punish(guild: discord.Guild, uid: int, action: str, reason: str) -> str:
    """実行者を止める。戻り値は結果の説明。"""
    if uid == guild.owner_id:
        return "⚠️ サーバー所有者のため、Botからは処罰できません（所有者アカウントの乗っ取りの可能性）"
    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except discord.HTTPException:
            member = None
    if member is not None and member.top_role >= guild.me.top_role:
        return "⚠️ 実行者のロールがBotより上のため、処罰できません（Botのロールを一番上に移動してください）"
    if action == "strip" and member is not None and member.bot:
        action = "kick"  # BOTの連携ロールは外せないので、キックにする
    try:
        if action == "ban":
            await guild.ban(discord.Object(id=uid), reason=reason[:500], delete_message_seconds=0)
            return "✅ BANしました"
        if action == "kick" and member is not None:
            await member.kick(reason=reason[:500])
            return "✅ キックしました"
        if member is not None:
            keep = [r for r in member.roles if r.managed or r.is_default()]
            await member.edit(roles=keep, reason=reason[:500])
            return "✅ ロールをすべて外しました（権限剥奪）"
        return "⚠️ 実行者がサーバーに見つかりません"
    except discord.HTTPException as e:
        if member is not None and action != "strip":  # BAN・キックに失敗したら、せめて権限を外す
            try:
                await member.edit(roles=[r for r in member.roles if r.managed or r.is_default()],
                                  reason=reason[:500])
                return f"⚠️ {NUKE_ACTION_LABEL.get(action, action)}に失敗したため、ロールを外しました"
            except discord.HTTPException:
                pass
        return f"❌ 処罰に失敗しました（{safe_error(e)}・権限不足の可能性）"


def _incident_embed(guild: discord.Guild, title: str, color: int, fields: list[tuple[str, str]]) -> discord.Embed:
    e = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    e.add_field(name="🏠 サーバー", value=f"{guild.name}（`{guild.id}`）", inline=False)
    for n, v in fields:
        e.add_field(name=n, value=(v or "-")[:1024], inline=False)
    return e


async def handle_nuke(guild: discord.Guild, uid: int, kind: str, hits: list[tuple[float, int]]):
    key = (guild.id, uid)
    active = _nuke_active.get(key)
    if active and time.time() - active["t"] < 120:
        active["targets"].setdefault(kind, set()).update(t for _, t in hits)
        return  # 対応中（重ねて処罰・通知しない）
    active = {"t": time.time(), "targets": {kind: {t for _, t in hits}}}
    _nuke_active[key] = active

    d = get_defense(guild.id)
    action = d.get("action", "ban")
    actor = guild.get_member(uid) or bot.get_user(uid)
    actor_text = f"<@{uid}>（`{uid}`）" + (f" {actor}" if actor else "")
    is_bot = bool(getattr(actor, "bot", False))
    result = await _punish(guild, uid, action, f"Nuke対策: {NUKE_KIND_LABEL.get(kind, kind)}を検知")

    fields = [("🧨 検知内容", f"{NUKE_KIND_LABEL.get(kind, kind)}（{len(hits)}回 / {NUKE_WINDOW}秒）"),
              ("👤 実行者", actor_text + ("（BOT・乗っ取りの可能性）" if is_bot else "")),
              ("🔨 自動対処", f"{NUKE_ACTION_LABEL.get(action, action)} → {result}"),
              ("♻️ 自動復元", "数秒後に実行します" if d.get("auto_restore", True) else "無効（/nuke-defense で有効化）")]
    embed = _incident_embed(guild, "🚨 Nuke（サーバー破壊）を検知しました", 0xEF4444, fields)
    await send_admin_alert(guild, embed)
    await dm_owners(embed, dedupe=f"nuke:{guild.id}")
    fire_and_forget(send_security_alert("Nuke検知", f"{guild.name}: {NUKE_KIND_LABEL.get(kind, kind)} / {result}",
                                        None, level="critical", dedupe_key=f"nuke:{guild.id}"))
    record_incident({"type": "nuke", "guild": guild.id, "guild_name": guild.name, "actor": uid,
                     "actor_name": str(actor or uid), "actor_bot": is_bot, "kind": kind,
                     "count": len(hits), "action": action, "result": result})
    if d.get("auto_restore", True):
        await asyncio.sleep(5)  # 削除イベントが出揃うのを待つ
        await auto_restore(guild, uid, active["targets"])


async def auto_restore(guild: discord.Guild, uid: int, targets: dict[str, set[int]]):
    now = time.time()
    chans = [snap for cid, (t, snap) in _deleted_channels.get(guild.id, {}).items()
             if cid in targets.get("channel_delete", set()) and now - t < SNAPSHOT_TTL]
    roles = [snap for rid, (t, snap) in _deleted_roles.get(guild.id, {}).items()
             if rid in targets.get("role_delete", set()) and now - t < SNAPSHOT_TTL]
    rep = await restore_items(guild, roles, chans) if (chans or roles) else {"roles": 0, "channels": 0, "failed": 0}
    for cid in targets.get("channel_delete", set()):
        _deleted_channels.get(guild.id, {}).pop(cid, None)
    for rid in targets.get("role_delete", set()):
        _deleted_roles.get(guild.id, {}).pop(rid, None)
    unbanned = 0
    for vid in targets.get("ban", set()):  # 荒らしにBANされた人のBANを解除
        if vid == uid:
            continue
        try:
            await guild.unban(discord.Object(id=vid), reason="Nuke対策: 荒らしによるBANを取り消し")
            unbanned += 1
        except discord.HTTPException:
            pass
        await asyncio.sleep(0.4)
    if not (rep["roles"] or rep["channels"] or rep["failed"] or unbanned):
        return
    text = (f"ロール {rep['roles']} 件・チャンネル {rep['channels']} 件を復元、BAN解除 {unbanned} 件"
            + (f"（失敗 {rep['failed']} 件）" if rep["failed"] else ""))
    embed = _incident_embed(guild, "♻️ Nukeの被害を自動復元しました", 0x22C55E, [("結果", text)])
    embed.set_footer(text="チャンネル内のメッセージは復元できません。/backup restore で全体の復元もできます")
    await send_admin_alert(guild, embed)
    record_incident({"type": "restore", "guild": guild.id, "guild_name": guild.name,
                     "actor": uid, "result": text})


@bot.event
async def on_audit_log_entry_create(entry: discord.AuditLogEntry):
    """監査ログから実行者を特定する（Botに「監査ログを表示」権限が必要）。"""
    guild = entry.guild
    kind = AUDIT_KINDS.get(entry.action)
    if kind is None or not get_defense(guild.id).get("nuke_defense", False):
        return
    uid = getattr(entry, "user_id", None) or (entry.user.id if entry.user else None)
    if uid is None or (bot.user and uid == bot.user.id):
        return  # Bot自身（復元作業など）は数えない
    d = get_defense(guild.id)
    if uid in {int(x) for x in d.get("whitelist", [])} or bot.is_owner_id(uid):
        return
    if kind == "prune":
        removed = int(getattr(entry.extra, "members_removed", 0) or 0)
        if removed < 10:
            return
    target_id = int(getattr(entry.target, "id", 0) or 0)
    now = time.time()
    dq = _nuke_hits.setdefault((guild.id, uid, kind), deque(maxlen=100))
    dq.append((now, target_id))
    while dq and now - dq[0][0] > NUKE_WINDOW:
        dq.popleft()
    active = _nuke_active.get((guild.id, uid))
    if active and now - active["t"] < 120:  # 対応中の追加被害も、復元の対象にする
        active["targets"].setdefault(kind, set()).add(target_id)
        return
    if len(dq) >= NUKE_LIMITS.get(kind, 3):
        hits = list(dq)
        dq.clear()
        await handle_nuke(guild, uid, kind, hits)


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    """削除されたチャンネルの情報を覚えておく（自動復元用）。"""
    guild = channel.guild
    if not get_defense(guild.id).get("nuke_defense", False):
        return
    store = _deleted_channels.setdefault(guild.id, {})
    store[channel.id] = (time.time(), serialize_channel(channel))
    if len(store) > 300:
        for k in sorted(store, key=lambda k: store[k][0])[:len(store) - 300]:
            store.pop(k, None)


@bot.event
async def on_guild_role_delete(role: discord.Role):
    guild = role.guild
    if role.managed or not get_defense(guild.id).get("nuke_defense", False):
        return
    store = _deleted_roles.setdefault(guild.id, {})
    store[role.id] = (time.time(), serialize_role(role))
    if len(store) > 300:
        for k in sorted(store, key=lambda k: store[k][0])[:len(store) - 300]:
            store.pop(k, None)


@bot.event
async def on_guild_role_create(role: discord.Role):
    guild = role.guild
    if not get_defense(guild.id).get("nuke_defense", False):
        return
    if role.permissions.administrator or role.permissions.manage_guild:
        embed = discord.Embed(
            title="⚠️ 危険な高権限ロールの作成を検知",
            description=f"管理者権限を持つロール `{role.name}` が作成されました。", color=0xF59E0B)
        await send_admin_alert(guild, embed)
        record_incident({"type": "warning", "guild": guild.id, "guild_name": guild.name,
                         "kind": "danger_role", "result": f"高権限ロール「{role.name}」が作成された"})


async def raid_check_join(member: discord.Member):
    """短時間の大量参加（Raid）を検知し、Raidモード中の参加者を自動でキックする。
    ※ SERVER MEMBERS INTENT（ENABLE_MEMBERS_INTENT=1）が必要。"""
    guild = member.guild
    if member.bot or not get_defense(guild.id).get("raid_protection", False):
        return
    now = time.time()
    if _raid_until.get(guild.id, 0) > now:
        try:
            await member.kick(reason="Raid対策: Raidモード中の参加")
        except discord.HTTPException:
            pass
        return
    dq = _join_log.setdefault(guild.id, deque(maxlen=200))
    dq.append((now, member.id))
    while dq and now - dq[0][0] > RAID_JOIN_WINDOW:
        dq.popleft()
    if len(dq) < RAID_JOIN_THRESHOLD:
        return
    _raid_until[guild.id] = now + RAID_LOCK_MINUTES * 60
    burst = [uid for _, uid in dq]
    dq.clear()
    level_note = "変更なし（「サーバー管理」権限がありません）"
    try:
        if guild.verification_level < discord.VerificationLevel.high:
            _raid_prev_level[guild.id] = guild.verification_level
            await guild.edit(verification_level=discord.VerificationLevel.high, reason="Raid対策")
            level_note = "認証レベルを「高」に引き上げました"
        else:
            level_note = "すでに高い認証レベルです"
    except discord.HTTPException:
        pass
    kicked = 0
    for uid in burst:
        try:
            await guild.kick(discord.Object(id=uid), reason="Raid対策: 大量参加の一員")
            kicked += 1
        except discord.HTTPException:
            pass
        await asyncio.sleep(0.3)
    embed = _incident_embed(guild, "🚨 Raid（大量参加）を検知しました", 0xEF4444, [
        ("📈 検知内容", f"{RAID_JOIN_WINDOW}秒間に {len(burst)} 人が参加"),
        ("🔨 自動対処", f"参加者 {kicked} 人をキック / {level_note}"),
        ("⏱️ Raidモード", f"{RAID_LOCK_MINUTES}分間、新しい参加者を自動でキックします")])
    await send_admin_alert(guild, embed)
    await dm_owners(embed, dedupe=f"raid:{guild.id}")
    record_incident({"type": "raid", "guild": guild.id, "guild_name": guild.name,
                     "count": len(burst), "result": f"{kicked}人キック / {level_note}"})
    fire_and_forget(_end_raid_mode(guild))


async def _end_raid_mode(guild: discord.Guild):
    await asyncio.sleep(RAID_LOCK_MINUTES * 60)
    if _raid_until.get(guild.id, 0) > time.time():
        return
    prev = _raid_prev_level.pop(guild.id, None)
    if prev is not None:
        try:
            await guild.edit(verification_level=prev, reason="Raid対策の終了")
        except discord.HTTPException:
            pass
    await send_admin_alert(guild, discord.Embed(
        title="✅ Raidモードを解除しました", color=0x22C55E,
        description="新しい参加者の自動キックを終了し、認証レベルを元に戻しました。"))


@bot.tree.command(name="nuke-defense", description="Nuke/Raid対策（自動検知・自動処罰・自動復元）と招待リンクブロックを設定します")
@app_commands.describe(nuke_defense="Nuke（大量削除・大量BAN等）の自動検知を有効にするか",
                       invite_block="招待リンクの自動ブロックを有効にするか",
                       action="Nukeの実行者への自動対処（省略時は今の設定 / 初期値はBAN）",
                       raid_protection="大量参加（Raid）の自動検知を有効にするか（メンバーIntentが必要）",
                       auto_restore="消されたチャンネル・ロールを自動で復元するか")
@app_commands.choices(action=[app_commands.Choice(name="BAN", value="ban"),
                              app_commands.Choice(name="キック", value="kick"),
                              app_commands.Choice(name="権限剥奪（ロールを全て外す）", value="strip")])
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def nuke_defense_cmd(interaction: discord.Interaction, nuke_defense: bool, invite_block: bool,
                           action: str | None = None, raid_protection: bool | None = None,
                           auto_restore: bool | None = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return
    guild = interaction.guild
    cfg = load_defense_config()
    cur = cfg.get(str(guild.id), {})
    cur.update({"nuke_defense": nuke_defense, "invite_block": invite_block})
    if action is not None:
        cur["action"] = action
    if raid_protection is not None:
        cur["raid_protection"] = raid_protection
    if auto_restore is not None:
        cur["auto_restore"] = auto_restore
    cfg[str(guild.id)] = cur
    save_defense_config(cfg)
    if nuke_defense:
        try:
            take_backup(guild, auto=True)  # 有効化した時点の状態を、復元用に保存しておく
        except Exception as e:
            print(f"バックアップに失敗: {safe_error(e)}")
    p = guild.me.guild_permissions
    missing = [n for n, ok in (("監査ログを表示", p.view_audit_log), ("メンバーをBAN", p.ban_members),
                               ("メンバーをキック", p.kick_members), ("ロールの管理", p.manage_roles),
                               ("チャンネルの管理", p.manage_channels)) if not ok]
    on = lambda v: "有効" if v else "無効"  # noqa: E731
    msg = (f"✅ セキュリティ設定を更新しました\n"
           f"・Nuke自動検知: `{on(nuke_defense)}`（対処: `{NUKE_ACTION_LABEL.get(cur.get('action', 'ban'))}`）\n"
           f"・自動復元: `{on(cur.get('auto_restore', True))}`\n"
           f"・Raid自動検知: `{on(cur.get('raid_protection', False))}`"
           + ("" if ENABLE_MEMBERS_INTENT else "（※Bot側でメンバーIntentが無効のため動作しません）") + "\n"
           f"・自動招待リンクブロック: `{on(invite_block)}`\n"
           f"・信頼済み（除外）ユーザー: {len(cur.get('whitelist', []))} 人（`/nuke-whitelist`）")
    if missing:
        msg += "\n\n⚠️ Botに次の権限がありません: " + "、".join(missing)
    msg += "\n💡 Botのロールは、できるだけ一番上に置いてください（上位のロールを持つ荒らしは処罰できません）。"
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="nuke-whitelist", description="Nuke検知の対象外にする信頼済みユーザー・BOTを管理します")
@app_commands.describe(action="追加 / 削除 / 一覧", user="対象のユーザー・BOT")
@app_commands.choices(action=[app_commands.Choice(name="追加 (add)", value="add"),
                              app_commands.Choice(name="削除 (remove)", value="remove"),
                              app_commands.Choice(name="一覧 (list)", value="list")])
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def nuke_whitelist_cmd(interaction: discord.Interaction, action: str, user: discord.User | None = None):
    if interaction.user.id != interaction.guild.owner_id and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return
    cfg = load_defense_config()
    cur = cfg.setdefault(str(interaction.guild.id), {})
    wl = [int(x) for x in cur.get("whitelist", [])]
    if action == "list":
        text = "\n".join(f"・<@{u}>（`{u}`）" for u in wl) or "登録なし"
        await interaction.response.send_message(f"🛡️ 信頼済みユーザー（{len(wl)}人）\n{text}"[:1900],
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return
    if user is None:
        await interaction.response.send_message("`user` を指定してください。", ephemeral=True)
        return
    if action == "add" and user.id not in wl:
        wl.append(user.id)
    elif action == "remove" and user.id in wl:
        wl.remove(user.id)
    cur["whitelist"] = wl
    save_defense_config(cfg)
    await interaction.response.send_message(
        f"✅ {user.mention} を信頼済みユーザーから{'追加' if action == 'add' else '削除'}しました。",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="backup", description="サーバーのロール・チャンネル構成のバックアップを作成・一覧・復元します")
@app_commands.describe(action="操作を選択", number="復元するバックアップの番号（一覧の番号。省略で最新）")
@app_commands.choices(action=[
    app_commands.Choice(name="バックアップ作成 (create)", value="create"),
    app_commands.Choice(name="バックアップ一覧 (list)", value="list"),
    app_commands.Choice(name="バックアップから復元 (restore)", value="restore")])
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def backup_cmd(interaction: discord.Interaction, action: str,
                     number: app_commands.Range[int, 1, 20] | None = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return
    guild = interaction.guild
    server_backups = load_backups().get(str(guild.id), [])
    if action == "create":
        b = take_backup(guild)
        await interaction.response.send_message(
            f"✅ バックアップしました（ロール {len(b['roles'])} 件・チャンネル {len(b['channels'])} 件）。",
            ephemeral=True)
        return
    if not server_backups:
        await interaction.response.send_message("保存されたバックアップはありません。", ephemeral=True)
        return
    newest_first = list(reversed(server_backups))
    if action == "list":
        desc = "\n".join(
            f"`{i}`. {b['time']}{'（自動）' if b.get('auto') else ''} — ロール {len(b.get('roles', []))} 件 / "
            f"チャンネル {len(b.get('channels', []))} 件" for i, b in enumerate(newest_first, 1))
        await interaction.response.send_message(
            embed=discord.Embed(title="💾 サーバーバックアップ一覧", description=desc, color=0x3B82F6)
            .set_footer(text="復元: /backup action:restore number:番号（足りないものだけを作り直します）"),
            ephemeral=True)
        return
    idx = (number or 1) - 1
    if idx >= len(newest_first):
        await interaction.response.send_message("その番号のバックアップはありません。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    rep = await restore_backup(guild, newest_first[idx])
    await interaction.followup.send(
        f"♻️ {newest_first[idx]['time']} のバックアップから復元しました\n"
        f"・作成したロール: {rep['roles']} 件\n・作成したチャンネル: {rep['channels']} 件"
        + (f"\n・失敗: {rep['failed']} 件（権限不足など）" if rep["failed"] else "")
        + "\n※ 既存のロール・チャンネルは変更しません。メッセージは復元できません。", ephemeral=True)



# ---------- 稼働状況の表示（Cog） ----------
STATUS_TITLE = "📡 霊夢BOT 稼働状況"


class StatusCog(commands.Cog):
    """サポートサーバー用：稼働状況のEmbedを、定期的に編集して表示する。
    表示するのは固定の項目だけ（認証API・Bot・Ping・導入サーバー数・最終更新）。
    URL・ID・トークン・エラー文は、画面にもログにも出さない。"""

    def __init__(self, bot: "AuthBot"):
        self.bot = bot
        self._message: discord.Message | None = None
        self.update_status_message.change_interval(minutes=STATUS_INTERVAL_MINUTES)

    async def cog_load(self):
        self.update_status_message.start()

    async def cog_unload(self):
        self.update_status_message.cancel()

    # --- 状態の確認 ---
    async def check_auth_api(self) -> tuple[bool, int | None]:
        """認証API（このアプリの /health）が応答するか確認する。URLは表示しない。"""
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
            color = 0x22C55E   # 緑：すべて正常
        elif bot_ok:
            color = 0xF59E0B   # 橙：認証APIだけ異常
        else:
            color = 0xEF4444   # 赤：Bot異常

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
        # 相対表示（◯分前）はDiscord側で計算されるので、更新が止まると古さが一目でわかる
        embed.add_field(name="🕒 最終更新", value=f"<t:{now}:F>（<t:{now}:R>）", inline=False)
        embed.set_footer(text=f"{STATUS_INTERVAL_MINUTES}分ごとに自動更新")
        return embed

    # --- メッセージの取得・作成 ---
    async def _find_message(self, channel) -> discord.Message | None:
        # 1) 保存してあるIDから（再起動しても同じメッセージを更新できる）
        saved = storage.load("status_message", STATUS_FILE)
        if saved.get("channel_id") == channel.id and saved.get("message_id"):
            try:
                return await channel.fetch_message(int(saved["message_id"]))
            except discord.NotFound:
                pass
        # 2) 保存がなければ、直近の履歴からBot自身の状況メッセージを探す
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

    # --- 定期更新 ---
    @tasks.loop(minutes=5)
    async def update_status_message(self):
        try:
            channel = (self.bot.get_channel(STATUS_CHANNEL_ID)
                       or await self.bot.fetch_channel(STATUS_CHANNEL_ID))
            api_ok, api_ms = await self.check_auth_api()
            embed = self.build_embed(api_ok, api_ms)

            if self._message is None:
                self._message = await self._find_message(channel)
            if self._message is None:  # 起動時にメッセージがなければ新規送信
                self._message = await self._send_new(channel, embed)
            else:  # あれば上書き更新
                try:
                    await self._message.edit(embed=embed)
                except discord.NotFound:  # 手動で消された場合は作り直す
                    self._message = await self._send_new(channel, embed)
        except discord.Forbidden:
            print("稼働状況の更新: チャンネルの権限が足りません"
                  "（チャンネルを見る・メッセージを送信・埋め込みリンク・メッセージ履歴を読む）")
        except Exception as e:
            print(f"稼働状況の更新エラー: {safe_error(e)}")  # 詳細な文面は出さない

    @update_status_message.before_loop
    async def _before_update(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)  # Webサーバーの起動を待ってから、最初の確認をする


# ---------- GBAN（グローバルBAN）とサーバーごとのログチャンネル ----------
# Redisのキー: gban:<user_id>（JSON） / config:log_channel:<guild_id>（チャンネルID）
_redis = None


def redis_client():
    """Upstash Redis（redis.asyncio）のクライアント。
    接続URLやライブラリが無ければ None（GBAN機能だけが無効になり、認証は通常どおり動く）。"""
    global _redis
    if aioredis is None or not UPSTASH_REDIS_URL:
        return None
    if _redis is None:
        _redis = aioredis.from_url(UPSTASH_REDIS_URL, decode_responses=True,
                                   socket_timeout=5, socket_connect_timeout=5,
                                   health_check_interval=30)
    return _redis


def redis_problem() -> str | None:
    """Redisが使えない原因（使える状態なら None）。接続URLそのものは含めない。"""
    if aioredis is None:
        return "redis ライブラリが入っていません（requirements.txt に redis を追加して再デプロイ）"
    if not UPSTASH_REDIS_URL:
        return "環境変数 UPSTASH_REDIS_URL が設定されていません"
    return None


async def _redis_do(op: str, *args):
    """Redisの操作。アイドル切断に備えて、1回だけ再試行する。"""
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
    raise last  # type: ignore[misc]


async def gban_startup_check():
    """起動時にRedisへ接続できるか確認し、結果だけをログに出す（接続URLは出さない）。"""
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
    """GBAN登録を返す（なければ None）。strict=False のときは、Redis障害でも None を返して認証を止めない。"""
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


async def gban_list(limit: int = 2000) -> list[tuple[int, dict]]:
    """GBAN登録の一覧（ダッシュボード用）。"""
    r = redis_client()
    if r is None:
        raise RuntimeError("redis-not-configured")
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = await r.scan(cursor, match="gban:*", count=500)
        keys.extend(batch)
        if not cursor or len(keys) >= limit:
            break
    out = []
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        vals = await r.mget(chunk)
        for k, v in zip(chunk, vals):
            try:
                d = json.loads(v) if v else {}
            except Exception:
                d = {"reason": str(v)}
            try:
                out.append((int(k.split(":", 1)[1]), d if isinstance(d, dict) else {"reason": str(d)}))
            except ValueError:
                continue
    return sorted(out, key=lambda x: str(x[1].get("at", "")), reverse=True)


async def gban_add_all(uid: int, reason: str, by: int) -> None:
    """GBAN登録して、全サーバーでBANする（ダッシュボードから使う。BANはバックグラウンド）。"""
    await gban_set(uid, reason, by)

    async def _run():
        ok = 0
        for guild in list(bot.guilds):
            if is_license_suspended(guild.id):
                continue
            try:
                await guild.ban(discord.Object(id=uid), reason=f"Global BAN: {reason}"[:500])
                ok += 1
                await send_guild_log(guild, gban_log_embed(uid, reason, auto=False))
            except discord.Forbidden:
                await send_guild_log(guild, gban_log_embed(uid, reason, auto=False, ok=False))
            except Exception:
                pass
            await asyncio.sleep(0.5)
        print(f"GBAN（ダッシュボード）: {uid} を {ok} サーバーでBANしました")
    fire_and_forget(_run())


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
    """そのサーバーの、/set-log-channel で設定されたチャンネルへEmbedを送る。
    未設定・権限なし・送信失敗のときは False（例外は外に出さない）。"""
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
                    value="Botのロールがそのユーザーのロールより下にあるか、「メンバーをBAN」の権限が"
                          "ありません。Botのロールを上に移動し、権限を付与してください。",
                    inline=False)
    return e


async def gban_auto_ban(member: discord.Member) -> None:
    """参加したユーザーがGBAN対象なら、すぐにBANしてログを送る。"""
    rec = await gban_get(member.id)
    if rec is None:
        return
    reason = str(rec.get("reason") or "理由なし")
    try:
        await member.guild.ban(member, reason=f"GBAN: {reason}"[:500])
        await send_guild_log(member.guild, gban_log_embed(member.id, reason, auto=True))
    except discord.Forbidden:
        print(f"GBANの自動BAN: 権限が足りません（{member.guild.name}）")
        await send_guild_log(member.guild,
                             gban_log_embed(member.id, reason, auto=True, ok=False))
    except discord.HTTPException as e:
        print(f"GBANの自動BANに失敗: {safe_error(e)}")


def _parse_snowflake(raw: str) -> int | None:
    raw = raw.strip()
    return int(raw) if raw.isdigit() and 15 <= len(raw) <= 21 else None


async def _owner_only(interaction: discord.Interaction) -> bool:
    if interaction.user.id in await bot.get_owner_ids():
        return True
    await interaction.response.send_message("このコマンドはBotのオーナー専用です。", ephemeral=True)
    return False


gban_group = app_commands.Group(
    name="gban", description="（オーナー専用）Global BANを管理します",
    default_permissions=discord.Permissions(administrator=True))
bot.tree.add_command(gban_group)


@gban_group.command(name="add", description="ユーザーをGlobal BAN登録し、Botが入っている全サーバーでBANします")
@app_commands.describe(user_id="対象のユーザーID（数字）", reason="理由（ログに表示されます）")
async def gban_add(interaction: discord.Interaction, user_id: str,
                   reason: app_commands.Range[str, 1, 200]):
    if not await _owner_only(interaction):
        return
    uid = _parse_snowflake(user_id)
    if uid is None:
        await interaction.response.send_message("ユーザーIDは数字（15〜21桁）で入力してください。",
                                                ephemeral=True)
        return
    if uid in await bot.get_owner_ids() or uid == bot.user.id:
        await interaction.response.send_message("オーナー自身やBotはGlobal BANできません。", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)  # 結果は、そのチャンネルの全員に見える
    try:  # 先に登録する（登録できなければ、BANもしない）
        await gban_set(uid, reason, interaction.user.id)
    except Exception as e:
        msg = (f"Global BAN機能が使えません: {redis_problem()}。"
               if isinstance(e, RuntimeError) else
               f"Redisに保存できませんでした（{safe_error(e)}）。BANは実行していません。")
        await interaction.delete_original_response()  # 「考え中」の公開表示を消して、エラーは本人にだけ見せる
        await interaction.followup.send(msg, ephemeral=True)
        return

    ok = logged = 0
    failed: list[str] = []
    for guild in list(bot.guilds):
        try:
            await guild.ban(discord.Object(id=uid), reason=f"Global BAN: {reason}"[:500])
            ok += 1
            if await send_guild_log(guild, gban_log_embed(uid, reason, auto=False)):
                logged += 1
        except discord.Forbidden:
            failed.append(f"{guild.name}（権限不足）")
            await send_guild_log(guild, gban_log_embed(uid, reason, auto=False, ok=False))
        except Exception as e:
            failed.append(f"{guild.name}（{type(e).__name__}）")
        await asyncio.sleep(0.5)  # レート制限対策

    # 全員に見える結果（他のサーバー名は載せない。メンションは通知されないようにする）
    result = (f"🚫 <@{uid}>（`{uid}`）をGlobal BAN登録しました\n"
              f"📄 理由: {reason}\n"
              f"✅ BAN成功: {ok} サーバー（うちログ送信 {logged}）\n"
              f"❌ 失敗: {len(failed)} サーバー")
    await interaction.followup.send(result[:1900],
                                    allowed_mentions=discord.AllowedMentions.none())
    if failed:  # 失敗したサーバー名は、実行した本人にだけ見せる
        detail = "失敗したサーバー（あなたにだけ表示）:\n" + "\n".join(f"・{n}" for n in failed[:10])
        if len(failed) > 10:
            detail += f"\n…ほか {len(failed) - 10} 件"
        await interaction.followup.send(detail[:1900], ephemeral=True)


@gban_group.command(name="remove", description="Global BAN登録を削除します（各サーバーのBAN自体は残ります）")
@app_commands.describe(user_id="対象のユーザーID（数字）")
async def gban_remove(interaction: discord.Interaction, user_id: str):
    if not await _owner_only(interaction):
        return
    uid = _parse_snowflake(user_id)
    if uid is None:
        await interaction.response.send_message("ユーザーIDは数字（15〜21桁）で入力してください。",
                                                ephemeral=True)
        return
    try:
        existed = await gban_del(uid)
    except Exception as e:
        msg = (f"Global BAN機能が使えません: {redis_problem()}。"
               if isinstance(e, RuntimeError) else f"Redisの操作に失敗しました（{safe_error(e)}）。")
        await interaction.response.send_message(msg, ephemeral=True)
        return
    if existed:
        msg = (f"✅ `{uid}` のGlobal BAN登録を削除しました。\n"
               "今後は参加しても自動BANされません。すでにBANされている各サーバーでは、"
               "サーバー側でBAN解除が必要です。")
    else:
        msg = f"`{uid}` はGlobal BAN登録されていません。"
    await interaction.response.send_message(
        msg, allowed_mentions=discord.AllowedMentions.none())  # 全員に見える


@bot.tree.command(name="set-log-channel",
                  description="Global BANなどの通知を受け取るログチャンネルを設定します")
@app_commands.describe(channel="通知を送信するチャンネル")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def set_log_channel_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    guild = interaction.guild
    if not interaction.user.guild_permissions.administrator:  # サーバーのオーナーも管理者権限を持つ
        await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return
    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        await interaction.response.send_message(
            f"{channel.mention} でBotに「チャンネルを見る・メッセージを送信・埋め込みリンク」の"
            "権限がありません。", ephemeral=True)
        return
    try:
        await set_log_channel_id(guild.id, channel.id)
    except Exception as e:
        msg = ("この機能は、現在利用できません。Botの運営者にお問い合わせください。"
               if isinstance(e, RuntimeError) else f"保存に失敗しました（{safe_error(e)}）。")
        await interaction.response.send_message(msg, ephemeral=True)
        return
    await interaction.response.send_message(
        f"✅ Global BANなどの通知は、{channel.mention} に届きます。\n"
        "※ Global BANを実行・自動BANするには、Botに「メンバーをBAN」の権限が必要です。",
        ephemeral=True)


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


@bot.tree.command(name="subaccount",
                  description="同じIPからの別アカウント（サブアカウント）を検知したときの動作を設定します")
@app_commands.describe(action="検知したときの動作")
@app_commands.choices(action=[
    app_commands.Choice(name="認証を拒否する (block)", value="block"),
    app_commands.Choice(name="認証は通し、ログに「該当」と記録するだけ (log)", value="log"),
    app_commands.Choice(name="現在の設定を確認 (status)", value="status")])
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def subaccount_cmd(interaction: discord.Interaction, action: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
        return
    gid = str(interaction.guild.id)
    if action != "status":
        data = storage.load("subaccount_config", SUBACCOUNT_CONFIG_FILE)
        data[gid] = action
        storage.save("subaccount_config", SUBACCOUNT_CONFIG_FILE, data)
    cur = get_subaccount_action(interaction.guild.id)
    await interaction.response.send_message(
        f"🛡️ サブアカウント検知時の動作: **{'認証を拒否' if cur == 'block' else 'ログに記録のみ'}**\n"
        "・判定方法: このサーバーで認証済みの別アカウントと、同じIPアドレス（IPv6は回線単位）からの認証\n"
        "・Global BAN・ブラックリスト・このサーバーでBAN済みのアカウントと同じIPからの認証は、"
        "設定にかかわらず常に拒否します（BAN回避対策）\n"
        f"・IPの記録は{IP_RETENTION_DAYS}日で自動削除されます\n"
        "※ 家族・学校・携帯回線などでIPを共有している場合も「該当」になることがあります。",
        ephemeral=True)


@bot.tree.command(name="transcript-channel",
                  description="チケットのトランスクリプト（会話ログ）を送るチャンネルを設定します（省略で解除）")
@app_commands.describe(channel="トランスクリプトを送信するチャンネル（省略すると解除）")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def transcript_channel_cmd(interaction: discord.Interaction,
                                 channel: discord.TextChannel | None = None):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("「サーバーの管理」権限が必要です。", ephemeral=True)
        return
    data = storage.load("ticket_config", TICKET_CONFIG_FILE)
    gid = str(interaction.guild.id)
    if channel is None:
        data.get(gid, {}).pop("transcript_channel", None)
        storage.save("ticket_config", TICKET_CONFIG_FILE, data)
        await interaction.response.send_message(
            "🔕 送信先を解除しました。トランスクリプトは、ボタンを押した人にだけ送られます。", ephemeral=True)
        return
    p = channel.permissions_for(interaction.guild.me)
    if not (p.view_channel and p.send_messages and p.embed_links and p.attach_files):
        await interaction.response.send_message(
            f"{channel.mention} でBotに「チャンネルを見る・メッセージを送信・埋め込みリンク・ファイルを添付」の"
            "権限がありません。", ephemeral=True)
        return
    data.setdefault(gid, {})["transcript_channel"] = channel.id
    storage.save("ticket_config", TICKET_CONFIG_FILE, data)
    await interaction.response.send_message(
        f"✅ チケットのトランスクリプト（HTML＋テキスト）は {channel.mention} に送られます。\n"
        "・チケット内の「📝 トランスクリプト保存」ボタンで送信\n"
        "・「🗑️ チケット削除」時にも、削除前に自動で保存します\n"
        "※ 閲覧できる人は、このチャンネルの権限で管理してください。", ephemeral=True)


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
               "認証ページ（`/?guild=…`・`/login`・`/callback`）、このサーバーのすべてのコマンド・ボタン・"
               "セレクトメニューに加え、自動モデレーション・イベントログ・Nuke検知・天気/災害の自動送信などの"
               "バックグラウンド処理も、すぐにすべて停止します。Botはサーバーに残ります。\n"
               "（Botオーナーの操作と `/license active` での再開は可能です）")
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
_main_loop: asyncio.AbstractEventLoop | None = None  # スレッドからセキュリティ通知を送るため


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
    except discord.PrivilegedIntentsRequired:
        _bot_fatal = True  # 設定を直さない限り直らないので、ウォッチドッグの再起動は止める
        print("特権インテントが有効になっていません。Developer Portalの Bot ページで "
              "SERVER MEMBERS INTENT / MESSAGE CONTENT INTENT をオンにするか、"
              "環境変数 ENABLE_MEMBERS_INTENT / ENABLE_MESSAGE_CONTENT_INTENT を 0 にしてください。")
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
            try:  # 再起動する前に、管理者へ知らせる
                await asyncio.wait_for(send_security_alert(
                    "Botが長時間オフライン",
                    f"{WATCHDOG_SECONDS}秒以上オフラインのため、プロセスを再起動します",
                    None, level="critical", dedupe_key="watchdog"), 5)
            except Exception:
                pass
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
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    bg_tasks = [asyncio.create_task(run_bot()), asyncio.create_task(watchdog()),
                asyncio.create_task(gban_startup_check())]
    for t in bg_tasks:
        t.add_done_callback(_log_task_result)
    try:
        yield
    finally:
        await bot.close()
        await close_redis()
        for t in bg_tasks:
            t.cancel()


app = FastAPI(lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)  # /docs 等でAPI構成を公開しない


# ---------- セキュリティ（レート制限・不正アクセスの通知） ----------
def client_ip(request: Request) -> str:
    """接続元のIP。プロキシ（Render・Cloudflare等）の後ろでは、ヘッダーから本当のIPを取る。
    レート制限のほか、認証時はサブアカウント検知のため鍵付きハッシュ（と伏せ字）にして記録する。
    セキュリティ通知には生のIPは載せない。"""
    if TRUST_PROXY_HEADERS:
        cf = request.headers.get("cf-connecting-ip")  # Cloudflareが付ける（偽装されにくい）
        if cf:
            return cf.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
    return request.client.host if request.client else "unknown"


def client_tag(request: Request) -> str:
    """通知用の識別子。IPのハッシュの先頭だけ（同じ相手かどうかは分かるが、IPは分からない）。"""
    return hashlib.sha256(f"{SECURITY_SALT}{client_ip(request)}".encode()).hexdigest()[:10]


limiter = Limiter(key_func=client_ip)  # カウントはメモリ上（再起動で消える）
app.state.limiter = limiter

_bg_tasks: set[asyncio.Task] = set()
_alert_last: dict[str, float] = {}   # 通知の重複防止（キー → 最後に送った時刻）
_alert_times: list[float] = []       # 全体の送信回数の制限用
ALERT_COOLDOWN = 300                 # 同じ種類・同じ相手の通知は、5分に1回まで
ALERT_MAX_PER_MINUTE = 10            # 全体でも1分に10件まで（Webhookの上限・自己DoS対策）
_SUSPICIOUS_PATH = re.compile(
    r"(\.env|\.git|wp-|phpmyadmin|\.php|\.aspx?|/etc/passwd|\.\./|cgi-bin|/admin|"
    r"\.sql|\.bak|actuator|/vendor/|/shell|/config)", re.I)


def fire_and_forget(coro) -> None:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def _clean(text: str, limit: int) -> str:
    """通知に載せる文字列を安全にする（改行・記号・メンションを無害化し、長さを制限）。"""
    text = re.sub(r"[\x00-\x1f\x7f`]", " ", str(text))
    return text.replace("@", "@\u200b")[:limit]


async def send_security_alert(kind: str, detail: str, request: Request | None = None, *,
                              level: str = "warning", dedupe_key: str | None = None) -> None:
    """DiscordのWebhookへ、警告をEmbedで送る。失敗してもアプリは止めない。
    載せるのは、種類・説明・パス（クエリ文字列なし）・メソッド・IPのハッシュ・UAの一部だけ。"""
    url = SECURITY_WEBHOOK_URL
    if not url:
        return
    if not url.startswith(("https://discord.com/api/webhooks/",
                           "https://discordapp.com/api/webhooks/")):
        return  # 想定外のURLには送らない（URL自体はログに出さない）

    now = time.time()
    key = dedupe_key or f"{kind}:{client_tag(request) if request else '-'}"
    last = _alert_last.get(key)
    if last is not None and now - last < ALERT_COOLDOWN:
        return  # 同じ内容の通知を連続で送らない
    _alert_times[:] = [t for t in _alert_times if now - t < 60]
    if len(_alert_times) >= ALERT_MAX_PER_MINUTE:
        return  # 攻撃中に通知が溢れないようにする
    _alert_last[key] = now
    _alert_times.append(now)
    if len(_alert_last) > 500:  # 古い記録を捨てて、メモリが増え続けないようにする
        for k in [k for k, v in _alert_last.items() if now - v > ALERT_COOLDOWN]:
            _alert_last.pop(k, None)

    colors = {"info": 0x3B82F6, "warning": 0xF59E0B, "critical": 0xEF4444}
    title = "🚨 重大なアラート" if level == "critical" else "⚠️ セキュリティ警告"
    fields = [{"name": "種類", "value": _clean(kind, 100), "inline": True},
              {"name": "時刻", "value": f"<t:{int(now)}:F>", "inline": True}]
    if request is not None:
        fields += [
            {"name": "リクエスト", "value": f"`{_clean(request.method, 10)} "
                                          f"{_clean(request.url.path, 120)}`", "inline": False},
            {"name": "相手の識別子", "value": f"`{client_tag(request)}`", "inline": True},
            {"name": "User-Agent",
             "value": _clean(request.headers.get("user-agent", "不明"), 80), "inline": True},
        ]
    payload = {
        "username": "セキュリティ通知",
        "allowed_mentions": {"parse": []},
        "embeds": [{"title": title, "description": _clean(detail, 500),
                    "color": colors.get(level, colors["warning"]), "fields": fields}],
    }
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(url, json=payload)
        if r.status_code >= 300:
            print(f"セキュリティ通知の送信に失敗 (HTTP {r.status_code})")
    except Exception as e:
        print(f"セキュリティ通知の送信に失敗: {safe_error(e)}")  # URLは出さない


def alert_threadsafe(kind: str, detail: str, *, level: str = "critical",
                     dedupe_key: str | None = None) -> None:
    """別スレッド（Upstashへの保存など）から、セキュリティ通知を送る。"""
    loop = _main_loop
    if loop is None or not SECURITY_WEBHOOK_URL:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            send_security_alert(kind, detail, None, level=level,
                                dedupe_key=dedupe_key or kind), loop)
    except Exception:
        pass


# ---------- 自動防御（Bot本体・Webへの不正利用・不正アクセス） ----------
# 不審な行動に「点数」を付け、一定時間内に合計がしきい値を超えた相手を自動でブロックする。
# ブロック時間は繰り返すほど長くなる（10分 → 1時間 → 24時間 → 7日）。
#   Web : 接続元IP（の鍵付きハッシュ）単位。ブロック中は全ページ 403
#   Bot : DiscordユーザーID単位。ブロック中はコマンド・ボタン・メニューをすべて拒否
# ブロックの記録は Upstash / JSON に保存されるので、再起動しても続く。Botオーナーは対象外。
SEC_BLOCK_FILE = Path("security_blocks.json")
GUARD_THRESHOLD = int(os.getenv("GUARD_THRESHOLD", "6"))      # この点数で自動ブロック
GUARD_WINDOW = int(os.getenv("GUARD_WINDOW_SECONDS", "3600"))  # 点数を数える期間
GUARD_DURATIONS = [600, 3600, 86400, 7 * 86400]                # 1回目, 2回目, 3回目, 4回目以降
GUARD_POINTS = {
    # Web
    "suspicious_path": 2, "rate_limit": 2, "bad_state": 1, "dashboard_denied": 6, "bad_dashboard_csrf": 3,
    # Bot
    "owner_command": 2, "interaction_flood": 6, "blacklisted_use": 0,
}
GUARD_LABEL = {
    "suspicious_path": "脆弱性を探す不審なパスへのアクセス", "rate_limit": "レート制限の超過",
    "bad_state": "不正・期限切れの認証セッション", "dashboard_denied": "ダッシュボードへの不正ログイン",
    "bad_dashboard_csrf": "ダッシュボードへの偽装リクエスト",
    "owner_command": "オーナー専用コマンドの不正使用", "interaction_flood": "コマンド・ボタンの連打（スパム）",
}
OWNER_ONLY_COMMANDS = {"gban", "broadcast", "blacklist", "license"}
INTERACTION_FLOOD = (int(os.getenv("GUARD_FLOOD_COUNT", "12")), 10)  # 10秒に12回を超えたら連打
_guard_points: dict[str, deque] = {}
_guard_inter: dict[int, deque] = {}


def load_blocks() -> dict:
    """-> {"ip": {識別子: {...}}, "user": {ユーザーID: {...}}}"""
    d = storage.load("security_blocks", SEC_BLOCK_FILE)
    d.setdefault("ip", {})
    d.setdefault("user", {})
    return d


def guard_web_id(request: Request) -> str | None:
    """Webの相手の識別子。他人のIPを名乗ってその人をブロックさせる手口を防ぐため、
    X-Forwarded-For は経路全体で識別する。プライベートIPなど本人を表さないものは None。"""
    cf = request.headers.get("cf-connecting-ip") if TRUST_PROXY_HEADERS else None
    xff = request.headers.get("x-forwarded-for") if TRUST_PROXY_HEADERS else None
    raw = cf or (xff or "").replace(" ", "") or (request.client.host if request.client else "")
    first = raw.split(",")[0]
    if not first or normalize_ip(first) is None:
        return None
    return hmac.new(IP_HASH_KEY, ("web:" + raw).encode(), hashlib.sha256).hexdigest()[:24]


def guard_blocked(kind: str, ident) -> dict | None:
    rec = load_blocks()[kind].get(str(ident))
    if rec and rec.get("until", 0) > time.time():
        return rec
    return None


def guard_unblock(kind: str, ident) -> bool:
    d = load_blocks()
    rec = d[kind].get(str(ident))
    if not rec:
        return False
    rec["until"] = 0  # 回数（次回の長さ）の記録は残す
    storage.save("security_blocks", SEC_BLOCK_FILE, d)
    return True


def guard_strike(kind: str, ident, reason: str, *, request: Request | None = None,
                 detail: str = "") -> None:
    """不審な行動を1件記録し、しきい値を超えたら自動ブロックする。"""
    if ident is None:
        return
    pts = GUARD_POINTS.get(reason, 1)
    if pts <= 0:
        return
    key = f"{kind}:{ident}"
    now = time.time()
    dq = _guard_points.setdefault(key, deque(maxlen=200))
    dq.append((now, pts, reason))
    while dq and now - dq[0][0] > GUARD_WINDOW:
        dq.popleft()
    if len(_guard_points) > 5000:
        for k in [k for k, v in _guard_points.items() if not v or now - v[-1][0] > GUARD_WINDOW][:2000]:
            _guard_points.pop(k, None)
    if guard_blocked(kind, ident) or sum(p for _, p, _ in dq) < GUARD_THRESHOLD:
        return
    reasons = sorted({GUARD_LABEL.get(r, r) for _, _, r in dq})
    dq.clear()
    fire_and_forget(_guard_block(kind, str(ident), reasons, detail, request))


async def _guard_block(kind: str, ident: str, reasons: list[str], detail: str, request: Request | None):
    d = load_blocks()
    prev = d[kind].get(ident, {})
    count = int(prev.get("count", 0)) + 1
    dur = GUARD_DURATIONS[min(count, len(GUARD_DURATIONS)) - 1]
    d[kind][ident] = {"until": int(time.time() + dur), "count": count, "reason": "、".join(reasons),
                      "at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"), "detail": detail[:200]}
    storage.save("security_blocks", SEC_BLOCK_FILE, d)
    span = f"{dur // 86400}日" if dur >= 86400 else (f"{dur // 3600}時間" if dur >= 3600 else f"{dur // 60}分")
    who = f"<@{ident}>（`{ident}`）" if kind == "user" else f"接続元 `{ident[:10]}`"
    print(f"🧱 自動ブロック: {kind} {ident[:10]} / {span} / {'、'.join(reasons)}")
    record_incident({"type": "abuse", "kind": kind, "actor": ident if kind == "user" else None,
                     "actor_name": ident[:10] if kind == "ip" else ident, "count": count,
                     "result": f"{span}ブロック（{count}回目）: {'、'.join(reasons)}"})
    fire_and_forget(send_security_alert(
        "自動ブロック", f"{'Discordユーザー' if kind == 'user' else 'Webの接続元'}を{span}ブロックしました"
        f"（{count}回目）: {'、'.join(reasons)}", request, level="critical", dedupe_key=f"block:{kind}:{ident}"))
    embed = discord.Embed(title="🧱 不正利用を検知して自動ブロックしました", color=0xEF4444,
                          timestamp=datetime.now(timezone.utc))
    embed.add_field(name="対象", value=who, inline=False)
    embed.add_field(name="理由", value="、".join(reasons)[:1000], inline=False)
    if detail:
        embed.add_field(name="詳細", value=detail[:1000], inline=False)
    embed.add_field(name="ブロック期間", value=f"{span}（{count}回目）", inline=True)
    embed.set_footer(text="解除はダッシュボードの「自動ブロック」タブから")
    await dm_owners(embed, dedupe=f"block:{kind}:{ident}", cooldown=60)


def guard_check_interaction(interaction: discord.Interaction) -> str | None:
    """Botへの操作を許可してよいか。拒否するときは、表示する理由を返す。（オーナーは常に許可）"""
    uid = interaction.user.id
    if uid in (bot._owner_ids or set()):
        return None
    if guard_blocked("user", uid):
        return "🧱 不正な利用が検知されたため、一時的にこのBotを利用できません。"
    if is_user_blacklisted(uid):
        return "🚫 このアカウントは、このBotの利用が制限されています。"
    if interaction.type == discord.InteractionType.autocomplete:
        return None  # 入力中の候補表示は連打に数えない
    now = time.time()
    dq = _guard_inter.setdefault(uid, deque(maxlen=50))
    dq.append(now)
    while dq and now - dq[0] > INTERACTION_FLOOD[1]:
        dq.popleft()
    if len(dq) > INTERACTION_FLOOD[0]:
        dq.clear()
        guard_strike("user", uid, "interaction_flood",
                     detail=f"{INTERACTION_FLOOD[1]}秒間に{INTERACTION_FLOOD[0]}回を超える操作"
                            f"（サーバー: {interaction.guild_id or 'DM'}）")
        return "🧱 操作が多すぎるため、一時的にこのBotを利用できません。"
    if interaction.type == discord.InteractionType.application_command:
        name = ((interaction.data or {}).get("name") or "")
        if name in OWNER_ONLY_COMMANDS:
            guard_strike("user", uid, "owner_command",
                         detail=f"/{name} を実行（サーバー: {interaction.guild_id or 'DM'}）")
            fire_and_forget(send_security_alert(
                "オーナー専用コマンドの不正使用",
                f"オーナー以外（ユーザーID: {uid}）が /{name} を実行しようとしました", None,
                dedupe_key=f"ownercmd:{uid}"))
    return None


async def _deny_interaction(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.type == discord.InteractionType.autocomplete:
            await interaction.response.autocomplete([])
        elif interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


@app.middleware("http")
async def guard_middleware(request: Request, call_next):
    """自動ブロック中の接続元は、すべてのページで 403 を返す。"""
    ident = guard_web_id(request)
    request.state.guard_id = ident
    if ident and guard_blocked("ip", ident):
        return HTMLResponse("<!DOCTYPE html><meta charset='utf-8'><title>403</title>"
                            "<p style='font-family:sans-serif;padding:2em'>不正なアクセスが検知されたため、"
                            "一時的にアクセスを制限しています。</p>", status_code=403,
                            headers={"Cache-Control": "no-store"})
    return await call_next(request)


def guard_web_strike(request: Request, reason: str, detail: str = "") -> None:
    guard_strike("ip", getattr(request.state, "guard_id", None) or guard_web_id(request), reason,
                 request=request, detail=detail)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    fire_and_forget(send_security_alert(
        "レート制限超過", f"短時間にアクセスが集中しました（制限: {exc.detail}）", request))
    guard_web_strike(request, "rate_limit", f"{request.url.path}（制限: {exc.detail}）")
    resp = result_page(False, "アクセスが多すぎます",
                       "短時間にアクセスが集中しました。1分ほど待ってから、もう一度お試しください。",
                       status=429)
    resp.headers["Retry-After"] = "60"
    return resp


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    # 脆弱性スキャンのような不審なパスへのアクセスを通知する（応答は今まで通り）
    if exc.status_code == 404 and _SUSPICIOUS_PATH.search(request.url.path):
        fire_and_forget(send_security_alert(
            "不審なパスへのアクセス", "存在しない管理系・設定系のパスにアクセスがありました",
            request))
        guard_web_strike(request, "suspicious_path", request.url.path[:100])
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    fire_and_forget(send_security_alert(
        "重大なエラー（500）",
        f"処理中に {type(exc).__name__} が発生しました（詳細はサーバーのログを確認）",
        request, level="critical", dedupe_key="error-500"))
    return result_page(False, "エラーが発生しました",
                       "時間をおいて、もう一度お試しください。", status=500)


# ---------- 認証ログ ----------
async def check_ip_links(h: str | None, uid: int, guild_id: int) -> dict:
    """同じIPのアカウントを調べ、サブアカウント判定とGBAN/ブラックリスト連携の結果を返す。"""
    res = {"status": "unknown", "linked": [], "flags": [], "block": False}
    if h is None:
        return res  # IPを判定に使えない（プロキシ設定・社内ネットワーク等）
    linked = ip_linked_users(h, uid)
    same_guild = sorted(u for u, gs in linked.items() if guild_id in gs)
    res["linked"] = same_guild
    res["status"] = "hit" if same_guild else "clear"
    bl = load_blacklist()["users"]
    sbl = load_server_blacklist().get(str(guild_id), [])
    guild = bot.get_guild(guild_id)
    for other in list(linked)[:15]:
        if await gban_get(other) is not None:
            res["flags"].append(("gban", other))
        elif other in bl:
            res["flags"].append(("blacklist", other))
        elif other in sbl:
            res["flags"].append(("server_blacklist", other))
        elif guild is not None and other in same_guild:
            try:
                await guild.fetch_ban(discord.Object(id=other))
                res["flags"].append(("banned", other))
            except discord.HTTPException:
                pass
    if res["flags"]:
        res["block"] = True  # BAN・ブラックリスト対象と同一IP → BAN回避とみなす
    elif res["status"] == "hit" and get_subaccount_action(guild_id) == "block":
        res["block"] = True
    return res


FLAG_LABEL = {"gban": "Global BAN対象", "blacklist": "ブラックリスト登録者",
              "server_blacklist": "このサーバーのブラックリスト登録者", "banned": "このサーバーでBAN済み"}


async def send_auth_log(channel_id: int, user: dict, role: discord.Role | None,
                        age: timedelta, passed: bool, blacklisted: bool = False,
                        *, guild_id: int = 0, ipres: dict | None = None,
                        ip_tag: str | None = None, ip_masked: str | None = None,
                        reason: str = ""):
    uid = int(user["id"])
    ipres = ipres or {"status": "unknown", "linked": [], "flags": []}
    sub_label = {"hit": "⚠️ 該当", "clear": "✅ 非該当"}.get(ipres["status"], "❔ 判定不可（IPを取得できません）")
    # 参加に使った招待リンク（invite_tracker が記録したもの）
    inv = None
    if getattr(bot, "get_join_invite", None):
        try:
            inv = await bot.get_join_invite(guild_id, uid)
        except Exception as e:
            print(f"招待情報の取得に失敗: {safe_error(e)}")
    # ダッシュボード用に記録（チャンネルへ送れなくても残す）
    append_auth_log({"invite": inv,"t": int(time.time()), "guild": guild_id, "user": uid,
                     "name": user.get("username"), "passed": passed, "blacklisted": blacklisted,
                     "sub": ipres["status"], "linked": ipres["linked"][:20],
                     "flags": [[k, v] for k, v in ipres["flags"]][:10],
                     "ip": ip_tag, "ip_masked": ip_masked, "reason": reason,
                     "age_days": age.days})

    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if user.get("avatar"):
        avatar = f"https://cdn.discordapp.com/avatars/{uid}/{user['avatar']}.png?size=256"
    else:
        avatar = f"https://cdn.discordapp.com/embed/avatars/{(uid >> 22) % 6}.png"

    now = datetime.now(JST)
    date_str = (f"{now.year}年{now.month}月{now.day}日 "
                f"{WEEKDAYS[now.weekday()]}曜日 {now:%H:%M}")
    age_str = f"アカウント作成から {age.days}日{age.seconds // 3600}時間"

    if passed:
        title, color = "✨ ユーザー認証ログ", 0x00FF00 if ipres["status"] != "hit" else 0xF59E0B
        role_text = f"✅ 付与完了 ({role.mention})"
    elif blacklisted:
        title, color = "🚫 ブラックリスト拒否ログ", 0xEF4444
        role_text = "❌ 付与なし (ブロック)"
    else:
        title, color = "⛔ 認証拒否ログ", 0xEF4444
        role_text = "❌ 付与なし (認証拒否)"

    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=avatar)
    embed.add_field(name="👤 ユーザー", value=f"<@{uid}> (`{user['username']}`)", inline=False)
    embed.add_field(name="🆔 ユーザーID", value=f"`{uid}`", inline=False)
    embed.add_field(name="📅 認証日時", value=date_str, inline=False)
    embed.add_field(name="🛡️ サブアカウント検知", value=sub_label, inline=True)
    embed.add_field(name="🌐 IP識別子", value=f"`{ip_tag[:10]}`" if ip_tag else "-", inline=True)
    embed.add_field(name="🕒 アカウント年齢", value=age_str, inline=True)
    if ipres["linked"]:
        lst = " ".join(f"<@{u}>" for u in ipres["linked"][:10])
        more = f" ほか{len(ipres['linked']) - 10}件" if len(ipres["linked"]) > 10 else ""
        embed.add_field(name=f"👥 同一IPで認証済みのアカウント（{len(ipres['linked'])}件）",
                        value=lst + more, inline=False)
    if ipres["flags"]:
        embed.add_field(name="🚨 IP連携の警告",
                        value="\n".join(f"・{FLAG_LABEL.get(k, k)}（<@{v}>）と同一IP"
                                        for k, v in ipres["flags"][:5]), inline=False)
    if getattr(bot, "describe_invite", None):
        for n, v in bot.describe_invite(inv):
            embed.add_field(name=f"📨 {n}", value=v[:1024], inline=n != "招待リンク")
    if reason:
        embed.add_field(name="📄 判定理由", value=reason[:500], inline=False)
    embed.add_field(name="🏷️ ロール付与状況", value=role_text, inline=False)
    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


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
section a{color:#93c5fd;word-break:break-all}
nav a.current{color:#e5e7eb;font-weight:600;pointer-events:none}
@media (min-width:640px){.wrap{padding-top:48px}.card{padding:42px 46px}h1{font-size:1.75rem}}
"""

TERMS_TITLE = "【霊夢BOT】利用規約"
TERMS_INTRO = ("この利用規約（以下、「本規約」）は、霊夢(remuremu17)（以下、「当方」）が提供する"
               "Discordボット「霊夢BOT」（以下、「本ボット」）の利用条件を定めるものです。"
               "本ボットを利用するすべてのユーザー（以下、「ユーザー」）は、本規約に同意したものとみなされます。")
TERMS_SECTIONS = [
    ("第1条（適用）", [
        ("p", "本規約は、ユーザーと当方との間の本ボットの利用に関わる一切の関係に適用されます。"),
    ]),
    ("第2条（機能と利用）", [
        ("p", "本ボットは、Discordサーバーの運営・セキュリティ強化を目的として、主に以下の機能を提供します。"
              "各機能の利用の有無や設定は、導入先サーバーの管理者が決定します。"),
        ("ul", ["認証機能：Discordアカウントによる認証（OAuth2）と、認証後のロールの自動付与、"
                "および同一IPアドレスからの複数アカウント（サブアカウント）・BAN回避の検知",
                "セキュリティ機能：チャンネル・ロールの大量削除や連続BANなどのサーバー破壊行為（Nuke）および"
                "大量参加（Raid）の自動検知と、実行者の自動処罰（BAN・キック・権限の剥奪）、"
                "サーバー構成のバックアップ・復元、招待リンクの自動ブロック",
                "利用制限機能：本ボット導入サーバー全体で共通の利用禁止（Global BAN）、"
                "ブラックリスト、サーバーごとのブラックリスト",
                "モデレーション機能：警告・タイムアウト・キック・BAN、自動モデレーション、イベントログ",
                "招待の追跡機能：招待リンクの作成（作成者・有効期限・作成回数）と、"
                "メンバーがどの招待リンクから参加したかの記録",
                "便利機能：チケットとその会話記録（トランスクリプト）の保存、ロールの選択メニュー・"
                "リアクションによる付与、案内メッセージの固定表示、お知らせの配信",
                "情報配信機能：気象庁の公開データに基づく天気予報の定時投稿、および地震・津波・火山・"
                "気象警報などの災害情報の自動通知"]),
        ("p", "本ボットの自動処理（自動処罰・自動復元・自動通知等）は、設定された条件に基づいて機械的に行われます。"
              "誤検知や処理の失敗が起こる場合があり、当方はその結果について責任を負いません。"),
        ("p", "天気予報および災害情報は参考情報であり、通知の遅延・欠落・誤りが生じる場合があります。"
              "避難や安全に関わる判断は、必ず気象庁・自治体などの公式な情報に基づいて行ってください。"),
        ("p", "チケットのトランスクリプトや各種ログには、ユーザーの発言内容・ユーザー名・ID等が含まれ、"
              "導入先サーバーの管理者が閲覧できる場合があります。"),
        ("p", "当方は、サーバーごとに本ボットの機能の全部または一部を停止（ライセンス停止）することがあります。"),
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
    ("第4条（不正防止のための情報の取得）", [
        ("p", "本ボットは、サブアカウントによる認証やBAN回避などの不正を防ぐため、認証時に接続元の"
              "IPアドレスを取得し、記録します。ユーザーは、認証を行うことで、これに同意したものとみなされます。"),
        ("p", "同じIPアドレスから複数のアカウントが認証された場合や、利用制限を受けたアカウントと同じ"
              "IPアドレスからの認証は、サブアカウントと判定され、認証が制限されることがあります。"),
    ]),
    ("第5条（免責事項）", [
        ("p", "当方は、本ボットの動作にバグや中断がないことを保証するものではありません。"),
        ("p", "本ボットの利用または利用不能によってユーザーに生じた損害について、"
              "当方に故意または重大な過失がある場合を除き、一切の責任を負いません。"),
    ]),
]

PRIVACY_TITLE = "【霊夢BOT】プライバシーポリシー"
PRIVACY_INTRO = ("霊夢(remuremu17)（以下、「当方」）は、提供するDiscordボット「霊夢BOT」（以下、「本ボット」）における"
                 "ユーザーの個人情報の取扱いについて、以下の通りプライバシーポリシー（以下、「本ポリシー」）を定めます。")
SUPPORT_SERVER_URL = os.getenv("SUPPORT_SERVER_URL", "https://discord.gg/eWpUkX9bPM")  # サポートサーバー

PRIVACY_SECTIONS = [
    ("1. 取得する情報", [
        ("p", "本ボットのOAuth2認証機能を利用する際、以下の情報を取得する場合があります。"),
        ("ul", ["DiscordのユーザーID、ユーザー名（識別・認証のため）、アバター",
                "認証時に許可された最小限のスコープ（例：identify 等の基本情報）",
                "認証と同時に、対象のサーバーへ自動で参加させるための許可（guilds.join）",
                "認証ページへアクセスした際の接続元IPアドレス（IPv6の場合は回線単位にまとめたもの）、"
                "および認証した日時・サーバー",
                "招待リンクに関する情報（作成者のユーザーID・ユーザー名、リンクのコード、作成日時・有効期限、"
                "作成回数）、およびメンバーが参加時に使用した招待リンク",
                "チケット機能で会話記録（トランスクリプト）を保存した場合の、チケット内のメッセージ本文・"
                "添付ファイルのリンク・発言者のユーザー名とID・送信日時",
                "警告・処罰の履歴、サーバー破壊行為（Nuke）・大量参加（Raid）を検知した際の実行者のIDと内容"]),
    ]),
    ("2. 情報の利用目的", [
        ("p", "取得した情報は、以下の目的のためにのみ利用します。"),
        ("ul", ["Discordサーバーにおけるユーザーの本人確認およびロールの自動付与",
                "認証に関するログの記録、および不正アクセスの防止・トラブルシューティング",
                "同一IPアドレスからの複数アカウントの認証（サブアカウント）の検知",
                "Global BAN（本ボット導入サーバー全体での利用禁止）、ブラックリスト、"
                "サーバーごとのBANの対象者によるBAN回避の検知・防止",
                "短時間の大量アクセスなど、攻撃的な利用の検知（レート制限）",
                "招待リンクの作成状況の把握、およびメンバーの参加経路（どの招待リンクから参加したか）の記録",
                "チケット（問い合わせ）の対応内容の記録と、関係者による確認",
                "サーバー破壊行為などの調査、および導入先サーバーの管理者・当方への通知"]),
    ]),
    ("3. IPアドレスの取扱い", [
        ("ul", ["IPアドレスは、元に戻せない鍵付きハッシュ値と、末尾を伏せた形式（例：203.0.113.xxx）に"
                "変換して保存します。サーバーの運営上必要な場合に限り、元のIPアドレスを保存することがあります。",
                "保存したIP情報は、本ボットを導入している複数のサーバー間で、上記のセキュリティ目的に限り"
                "照合・共有します（他のサーバーの管理者へIPアドレスそのものを開示することはありません）。",
                "各サーバーの管理者には、認証ログとして「サブアカウント検知：該当 / 非該当」の判定結果、"
                "同一IPで認証したアカウント、およびIPの識別子（ハッシュの一部）を表示します。",
                f"IP情報は、最後の認証から{IP_RETENTION_DAYS}日を過ぎたものから順に自動で削除します。"]),
    ]),
    ("4. 招待リンク・チケットの記録の取扱い", [
        ("ul", ["招待リンクの作成や、メンバーの参加経路の記録は、導入先サーバーの管理者が指定したログチャンネルに"
                "送信され、そのチャンネルを閲覧できる人が確認できます。",
                "招待の作成回数と参加経路の情報は、データの保存先（Upstash等）に保存します。"
                "参加経路は90日、招待リンクの情報（作成者・作成日時）は180日を過ぎると自動で削除します。"
                "招待リンクの作成回数は累計として保存し、削除の希望があった場合は「7. データの開示・削除」に従い対応します。",
                "チケットの会話記録（HTML・テキストファイル）は、サーバーの管理者が指定したチャンネルと、"
                "保存操作をした人に送信されます。その後の保存期間や閲覧できる人は、導入先サーバーの管理者が"
                "Discord上で管理します（当方のサーバーには保存しません）。",
                "会話記録に含まれる画像・添付ファイルは、Discordの仕様により一定期間後に表示できなくなることがあります。"]),
    ]),
    ("5. 第三者提供の制限", [
        ("p", "当方は、法令に基づく場合を除き、ユーザーの同意を得ることなく、"
              "取得した個人情報を第三者に提供することはありません。"),
        ("p", "なお、本ボットの運用のため、通信経路（Cloudflare等）やデータの保存先（Upstash等）の"
              "外部サービスを利用しており、これらのサービス上で情報が処理・保存されます。"),
    ]),
    ("6. データの管理", [
        ("p", "取得した情報は、不正アクセスや漏洩を防ぐために適切な管理を行います。"
              "なお、各サーバーのログチャンネル等へ送信された情報は、そのサーバーの管理者の管理下となります。"
              "管理画面はボットの運営者のみが、Discordアカウントでのログインにより閲覧できます。"
              "また、不要となったデータは定期的に削除または匿名化に努めます。"),
    ]),
    ("7. データの開示・削除", [
        ("p", "ユーザーから取得した情報の開示、修正、または削除の希望があった場合、"
              "本人確認を行った上で、法令および合理的な範囲で速やかに対応いたします。"
              "ただし、不正防止のために必要な記録は、保存期間内は削除をお断りする場合があります。"),
    ]),
    ("8. お問い合わせ", [
        ("p", "本ポリシーおよび本ボットに関するお問い合わせ・データの開示や削除のご依頼は、"
              "以下のサポートサーバーまでご連絡ください。"),
        ("link", ("サポートサーバー：", SUPPORT_SERVER_URL)),
    ]),
]


AUTH_NOTICE_HTML = """
<div style="max-width:440px;margin:18px auto 24px;padding:12px 16px;border-radius:12px;
background:rgba(255,255,255,.05);border:1px solid rgba(148,163,184,.25);color:#9ca3af;
font:13px/1.7 system-ui,-apple-system,'Hiragino Sans','Noto Sans JP',sans-serif;text-align:left">
🔒 認証すると、DiscordのユーザーID・アカウント名に加え、<b style="color:#e5e7eb">IPアドレス</b>を
サブアカウント・BAN回避の検知などセキュリティ目的で取得・保存します。認証を続けると、
<a href="/terms" style="color:#93c5fd">利用規約</a>と
<a href="/privacy" style="color:#93c5fd">プライバシーポリシー</a>に同意したものとみなされます。
</div>"""


def legal_page(title: str, intro: str, sections: list, current: str) -> HTMLResponse:
    """利用規約・プライバシーポリシー共通のページ（スマホ対応）。"""
    parts = []
    for heading, blocks in sections:
        parts.append(f"<section><h2>{html.escape(heading)}</h2>")
        for kind, val in blocks:
            if kind == "ul":
                items = "".join(f"<li>{html.escape(x)}</li>" for x in val)
                parts.append(f"<ul>{items}</ul>")
            elif kind == "link":
                label, url = val
                parts.append(f'<p>{html.escape(label)}<a href="{html.escape(url)}" target="_blank" '
                             f'rel="noopener noreferrer">{html.escape(url)}</a></p>')
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
@limiter.limit(RATE_LIMIT_INDEX)
async def index(request: Request, guild: int | None = None):
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
    # index.html にある「取得する情報は、ユーザーIDとアカウント名のみです。」の一文を、要素ごと取り除く
    page = re.sub(r"<(p|div|small|span)\b[^>]*>[^<]*取得する情報は[^<]*</\1>", "", page)
    page = re.sub(r"[^<>]*取得する情報は[^<>]*のみです。?", "", page)  # 要素で囲まれていない場合
    # IPアドレスの取得について、認証ボタンのページで必ず案内する
    if "</body>" in page:
        page = page.replace("</body>", AUTH_NOTICE_HTML + "</body>", 1)
    else:
        page += AUTH_NOTICE_HTML
    return HTMLResponse(page)


@app.get("/login")
@limiter.limit(RATE_LIMIT_LOGIN)
async def login(request: Request, guild: int | None = None):
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
@limiter.limit(RATE_LIMIT_CALLBACK)
async def callback(request: Request, code: str | None = None, state: str | None = None,
                   error: str | None = None,
                   invited_guild_id: str | None = Query(default=None, alias="guild_id")):
    # ダッシュボードのログイン（state が dash_ で始まる）は、ダッシュボード側で処理する
    if state and state.startswith("dash_"):
        if dashboard_callback is None:
            return result_page(False, "ダッシュボードは無効です", "dashboard.py が見つかりません。", status=404)
        return await dashboard_callback(request, None if error else code, state)
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
        guard_web_strike(request, "bad_state")
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
            fire_and_forget(send_security_alert(
                "OAuth2のトークン交換に失敗", f"Discordから HTTP {r.status_code} が返りました",
                request, dedupe_key="oauth-token"))
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

        age = datetime.now(timezone.utc) - discord.utils.snowflake_time(user_id)  # ログ表示用

        # 2.3) 接続元IP（サブアカウント検知・GBAN/ブラックリスト連携に使う）
        norm_ip = normalize_ip(client_ip(request))
        h = ip_hash(norm_ip) if norm_ip else None
        masked = mask_ip(norm_ip) if norm_ip else None

        async def deny_log(reason: str, *, blacklisted: bool, ipres: dict | None = None):
            try:
                await asyncio.wait_for(bot.wait_until_ready(), READY_TIMEOUT)
                await send_auth_log(log_channel_id, user, None, age, False, blacklisted=blacklisted,
                                    guild_id=guild_id, ipres=ipres, ip_tag=h, ip_masked=masked,
                                    reason=reason)
            except Exception as e:
                print(f"認証ログ送信エラー: {safe_error(e)}")

        # 拒否された場合も、IPは記録する（同じIPからの別アカウントを検知するため）
        def remember_ip():
            if h:
                try:
                    record_ip(h, norm_ip, user_id, guild_id)
                except Exception as e:
                    print(f"IPの記録に失敗: {safe_error(e)}")

        # 2.4) ブラックリストのユーザーはブロック
        if is_user_blacklisted(user_id):
            remember_ip()
            await deny_log("Botのブラックリストに登録されたユーザー", blacklisted=True)
            return blocked_page("認証できません",
                                "このアカウントは、認証の利用が制限されています。"
                                "心当たりがない場合は、サーバーの管理者にお問い合わせください。")
        # 2.45) このサーバーのブラックリスト
        if is_server_user_blacklisted(guild_id, user_id):
            remember_ip()
            await deny_log("このサーバーのブラックリストに登録されたユーザー", blacklisted=True)
            return blocked_page("認証できません",
                                "このサーバーでは、あなたのアカウントの認証が制限されています。"
                                "心当たりがない場合は、サーバーの管理者にお問い合わせください。")

        # 2.46) GBAN（グローバルBAN）されたユーザーは、サーバーに参加させる前にブロック
        if await gban_get(user_id) is not None:
            remember_ip()
            await deny_log("Global BAN登録ユーザー", blacklisted=True)
            return blocked_page("認証できません",
                                "このアカウントは、認証の利用が制限されています。")

        # 2.5) 同一IPによるサブアカウント検知（BAN・ブラックリスト対象と同一IPならBAN回避としてブロック）
        ipres = await check_ip_links(h, user_id, guild_id)
        remember_ip()
        if ipres["block"]:
            if ipres["flags"]:
                why = "、".join(sorted({FLAG_LABEL.get(k, k) for k, _ in ipres["flags"]}))
                reason = f"制限対象のアカウント（{why}）と同じIPからの認証"
            else:
                reason = "このサーバーで認証済みの別アカウントと同じIPからの認証（サブアカウント）"
            await deny_log(reason, blacklisted=bool(ipres["flags"]), ipres=ipres)
            return result_page(
                False, "認証できません",
                "同じネットワークから、すでに別のアカウントが認証されているため、"
                "サブアカウントと判定されました。心当たりがない場合は、サーバーの管理者にお問い合わせください。")

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
                await send_auth_log(log_channel_id, user, role, age, True, guild_id=guild_id,
                                    ipres=ipres, ip_tag=h, ip_masked=masked,
                                    reason="同一IPのアカウントあり（記録のみ）" if ipres["status"] == "hit" else "")
            except Exception as e:
                print(f"認証ログ送信エラー: {safe_error(e)}")

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


# ---------- 所有者専用ダッシュボード（/dashboard） ----------
from types import SimpleNamespace  # noqa: E402

dashboard_callback = None if dashboard is None else dashboard.register(app, SimpleNamespace(
    bot=bot, client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
    base_url=lambda: BASE_URL or f"http://localhost:{PORT}",  # クイックトンネルで後から変わるので関数で渡す
    oauth_redirect_uri=lambda: REDIRECT_URI,  # 認証と同じ /callback を使う（追加のRedirect登録は不要）
    get_owner_ids=bot.get_owner_ids, limiter=limiter, client_ip=client_ip,
    send_security_alert=send_security_alert, log_buffer=LOG_BUFFER, boot_time=BOOT_TIME,
    storage_remote=storage.remote, redis_client=redis_client,
    load_auth_logs=load_auth_logs, load_incidents=load_incidents,
    load_license=load_license, save_license=save_license,
    load_blacklist=load_blacklist, save_blacklist=save_blacklist,
    load_ip_index=load_ip_index, load_defense_config=load_defense_config, load_config=load_config,
    gban_list=gban_list, gban_add_all=gban_add_all, gban_del=gban_del,
    load_blocks=load_blocks, guard_unblock=guard_unblock, guard_web_strike=guard_web_strike,
    ip_retention_days=IP_RETENTION_DAYS,
))


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
                         ("defense_config", DEFENSE_CONFIG_FILE), ("server_backups", BACKUP_DATA_FILE),
                         ("license", LICENSE_FILE), ("status_message", STATUS_FILE),
                         ("ip_index", IP_INDEX_FILE), ("auth_logs", AUTH_LOG_FILE),
                         ("security_incidents", INCIDENT_FILE), ("ticket_config", TICKET_CONFIG_FILE),
                         ("subaccount_config", SUBACCOUNT_CONFIG_FILE),
                         ("security_blocks", SEC_BLOCK_FILE),
                         ("weather_config", Path("weather_config.json")),
                         ("invite_log_config", Path("invite_log_config.json")),
                         ("disaster_config", Path("disaster_config.json"))])
    except Exception as e:
        raise SystemExit("設定の読み込みに失敗しました（Upstashに接続できません）: "
                         f"{safe_error(e)}")
    print("設定の保存先:", "Upstash（外部Redis）" if storage.remote
          else "ローカルのJSONファイル（再起動で消える環境では設定が失われます）")

    print("メッセージ内容の取得:", "有効" if ENABLE_MESSAGE_CONTENT_INTENT
          else "無効（ENABLE_MESSAGE_CONTENT_INTENT=1 で有効）")
    print("参加時の自動BAN:", "有効（SERVER MEMBERS INTENT を使用）" if ENABLE_MEMBERS_INTENT
          else "無効（ENABLE_MEMBERS_INTENT=1 にすると有効）")

    if USE_QUICK_TUNNEL:
        tunnel_url = start_quick_tunnel(PORT)
        if tunnel_url:
            BASE_URL = tunnel_url
            REDIRECT_URI = f"{tunnel_url}/callback"
            print("=" * 60)
            print("Cloudflare Tunnel 起動成功")
            print(f"公開URL     : {BASE_URL}")
            print(f"Redirect URI: {REDIRECT_URI}")
            print(f"ダッシュボード: {BASE_URL}/dashboard")
            print("→ Developer Portal の OAuth2 > Redirects に、上の Redirect URI を登録してください")
            print("=" * 60)

    if not REDIRECT_URI:
        raise SystemExit("REDIRECT_URI が決まりません。トンネルの起動に失敗したか、"
                         ".env に REDIRECT_URI を設定してください。")
    print(f"所有者専用ダッシュボード: {BASE_URL}/dashboard（ログインは認証と同じ {REDIRECT_URI} を使用）")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
