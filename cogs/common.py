"""cogs 共通の部品（Redis・安全なエラー表示・権限チェックなど）。

※ Redisの接続URLは環境変数 UPSTASH_REDIS_URL（rediss://...）から読む。
   URLやパスワードは、ログにも画面にも出さない。
"""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import discord

try:
    from redis import asyncio as aioredis  # pip install redis
except ImportError:  # 未インストールなら、Redisを使う機能だけが無効になる
    aioredis = None

JST = timezone(timedelta(hours=9))
UPSTASH_REDIS_URL = os.getenv("UPSTASH_REDIS_URL", "").strip()
_redis = None

# サーバーの管理者などに見せる文面（内部の設定名は出さない）
FEATURE_UNAVAILABLE = "この機能は、現在利用できません。Botの運営者にお問い合わせください。"


def safe_error(e: Exception) -> str:
    """ログ用：例外の種類（とHTTPステータス）だけを返す。メッセージ本文は出さない。"""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None) or getattr(e, "status", None)
    return type(e).__name__ + (f" (HTTP {code})" if code else "")


def now_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


def trim(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------- Redis ----------
def redis_problem() -> str | None:
    """Redisが使えない原因（使える状態なら None）。"""
    if aioredis is None:
        return "redis ライブラリが入っていません（requirements.txt に redis を追加して再デプロイ）"
    if not UPSTASH_REDIS_URL:
        return "環境変数 UPSTASH_REDIS_URL が設定されていません"
    return None


def redis_client():
    global _redis
    if redis_problem():
        return None
    if _redis is None:
        _redis = aioredis.from_url(UPSTASH_REDIS_URL, decode_responses=True,
                                   socket_timeout=5, socket_connect_timeout=5,
                                   health_check_interval=30)
    return _redis


async def rdo(op: str, *args):
    """Redisの操作。アイドル切断に備えて1回だけ再試行する。未設定なら RuntimeError。"""
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


async def get_json(key: str):
    raw = await rdo("get", key)
    return json.loads(raw) if raw else None


async def set_json(key: str, obj) -> None:
    await rdo("set", key, json.dumps(obj, ensure_ascii=False))


# ---------- Discord 共通 ----------
async def reply(interaction: discord.Interaction, text: str | None = None, *,
                embed: discord.Embed | None = None, ephemeral: bool = True, **extra):
    """未応答なら response、応答済みなら followup で返す。"""
    kw = dict(extra)
    kw["ephemeral"] = ephemeral
    if text is not None:
        kw["content"] = text
    if embed is not None:
        kw["embed"] = embed
    if interaction.response.is_done():
        return await interaction.followup.send(**kw)
    return await interaction.response.send_message(**kw)


def is_admin(interaction: discord.Interaction) -> bool:
    u = interaction.user
    return isinstance(u, discord.Member) and u.guild_permissions.administrator


def hierarchy_error(interaction: discord.Interaction, target: discord.Member) -> str | None:
    """処罰・警告の対象にできない理由（問題なければ None）。"""
    guild, me, user = interaction.guild, interaction.guild.me, interaction.user
    if target.id == user.id:
        return "自分自身には実行できません。"
    if target.id == guild.owner_id:
        return "サーバーのオーナーには実行できません。"
    if target.id == me.id:
        return "Bot自身には実行できません。"
    if user.id != guild.owner_id and target.top_role >= user.top_role:
        return "あなたと同じか、より上位のロールのメンバーには実行できません。"
    if target.top_role >= me.top_role:
        return "Botのロールが対象のメンバーより下です。Botのロールを上に移動してください。"
    return None


async def dm_user(user: discord.abc.User, embed: discord.Embed) -> bool:
    """対象へのDM（拒否されていても失敗を無視する）。"""
    try:
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def log_event(bot, guild: discord.Guild, embed: discord.Embed) -> None:
    """そのサーバーのイベントログ用チャンネル（/eventlog set）へ送る。未設定なら何もしない。"""
    cog = bot.get_cog("EventLogs")
    if cog is not None:
        await cog.send(guild, embed)
