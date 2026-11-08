"""メッセージキープ（スティッキーメッセージ）。
指定チャンネルに新しいメッセージが来たら、古い案内を消して一番下に貼り直す。
設定: Redis ハッシュ sticky:<サーバーID>  フィールド <チャンネルID> → {"content","title","color","buttons","msg"}"""
from __future__ import annotations

import asyncio
import json
import time

import discord
from discord import app_commands
from discord.ext import commands


class kv:
    """Redisの小さなヘルパー（main.py の bot.kv を使う）。
    Redis が未設定・一時的に失敗したときは、メモリに保存して動き続ける（再起動で消える）。"""
    _mem: dict = {}
    _exp: dict = {}

    @classmethod
    def _alive(cls, key):
        if key in cls._exp and cls._exp[key] < time.time():
            cls._mem.pop(key, None)
            cls._exp.pop(key, None)
        return key in cls._mem

    @staticmethod
    async def _do(bot, op, *args):
        if getattr(bot, "kv_ready", lambda: False)():
            try:
                return True, await bot.kv(op, *args)
            except Exception as e:
                print(f"Redisの操作に失敗（{op}）: {type(e).__name__}")
        return False, None

    @classmethod
    async def get_json(cls, bot, key, default=None):
        ok, v = await cls._do(bot, "get", key)
        if ok:
            try:
                return json.loads(v) if v else default
            except Exception:
                return default
        return cls._mem.get(key, default) if cls._alive(key) else default

    @classmethod
    async def set_json(cls, bot, key, value, ttl=None):
        # redis-py の set(name, value, ex) は、3番目の引数が有効期限（秒）
        ok, _ = await cls._do(bot, "set", key, json.dumps(value, ensure_ascii=False), *([int(ttl)] if ttl else []))
        if not ok:
            cls._mem[key] = value
            if ttl:
                cls._exp[key] = time.time() + ttl

    @classmethod
    async def incr(cls, bot, key):
        ok, v = await cls._do(bot, "incr", key)
        if ok:
            return int(v)
        cls._mem[key] = int(cls._mem.get(key, 0) or 0) + 1
        return cls._mem[key]

    @classmethod
    async def get_int(cls, bot, key):
        ok, v = await cls._do(bot, "get", key)
        return int(v or 0) if ok else int(cls._mem.get(key, 0) or 0)

    @classmethod
    async def hgetall(cls, bot, key):
        ok, v = await cls._do(bot, "hgetall", key)
        out = {}
        for k, val in ((v if ok else cls._mem.get(key, {})) or {}).items():
            try:
                out[k] = json.loads(val) if isinstance(val, str) else val
            except Exception:
                pass
        return out

    @classmethod
    async def hset(cls, bot, key, field, value):
        raw = json.dumps(value, ensure_ascii=False)
        ok, _ = await cls._do(bot, "hset", key, field, raw)
        if not ok:
            cls._mem.setdefault(key, {})[field] = raw

    @classmethod
    async def hdel(cls, bot, key, field):
        ok, _ = await cls._do(bot, "hdel", key, field)
        if not ok:
            cls._mem.get(key, {}).pop(field, None)

DELAY = 4  # 最後の投稿からこの秒数たってから貼り直す（連投時の連続再投稿を防ぐ）


def key(gid: int) -> str:
    return f"sticky:{gid}"


class Sticky(commands.Cog):
    group = app_commands.Group(name="sticky", description="案内メッセージを常にチャンネルの一番下に保ちます",
                               guild_only=True, default_permissions=discord.Permissions(manage_messages=True))

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: dict[int, dict[str, dict]] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_post: dict[int, float] = {}

    async def _conf(self, gid: int) -> dict[str, dict]:
        if gid not in self._cache:
            self._cache[gid] = await kv.hgetall(self.bot, key(gid))
        return self._cache[gid]

    @staticmethod
    def _build(cfg: dict):
        embed = None
        if cfg.get("title") or cfg.get("embed"):
            embed = discord.Embed(title=cfg.get("title") or None, description=cfg.get("content") or None,
                                  color=cfg.get("color") or 0x5865F2)
            content = None
        else:
            content = cfg.get("content")
        view = None
        if cfg.get("buttons"):
            view = discord.ui.View(timeout=None)
            for b in cfg["buttons"][:5]:
                view.add_item(discord.ui.Button(label=b["label"][:80], url=b["url"]))
        return content, embed, view

    async def _repost(self, channel: discord.TextChannel, cfg: dict):
        lock = self._locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            if cfg.get("msg"):
                try:
                    await channel.get_partial_message(int(cfg["msg"])).delete()
                except discord.HTTPException:
                    pass
            content, embed, view = self._build(cfg)
            try:
                msg = await channel.send(content=content, embed=embed, view=view,
                                         allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                print(f"キープメッセージの再投稿に失敗 ({channel.id}): {type(e).__name__}")
                return
            cfg["msg"] = msg.id
            self._last_post[channel.id] = time.time()
            await kv.hset(self.bot, key(channel.guild.id), str(channel.id), cfg)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or self.bot.user is None:
            return
        conf = await self._conf(message.guild.id)
        cfg = conf.get(str(message.channel.id))
        if not cfg or message.id == cfg.get("msg"):
            return
        if message.author.id == self.bot.user.id:
            lock = self._locks.get(message.channel.id)
            if (lock and lock.locked()) or time.time() - self._last_post.get(message.channel.id, 0) < 3:
                return  # 自分が貼り直したメッセージには反応しない（無限ループ防止）
        old = self._tasks.get(message.channel.id)
        if old and not old.done():
            old.cancel()  # 連投中は待ち直す

        async def later():
            await asyncio.sleep(DELAY)
            cur = (await self._conf(message.guild.id)).get(str(message.channel.id))
            if cur:
                await self._repost(message.channel, cur)
        self._tasks[message.channel.id] = asyncio.create_task(later())

    @group.command(name="set", description="このチャンネル（または指定チャンネル）に、キープする案内メッセージを設定します")
    @app_commands.describe(text="本文（\\n で改行）", title="タイトル（入れると埋め込み表示）", color="色（例: #5865F2）",
                           button_label="リンクボタンの文字（任意）", button_url="リンクボタンのURL（任意）",
                           channel="対象チャンネル（省略で今のチャンネル）")
    async def set_(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 1800],
                   title: app_commands.Range[str, 1, 200] | None = None, color: str | None = None,
                   button_label: str | None = None, button_url: str | None = None,
                   channel: discord.TextChannel | None = None):
        ch = channel or interaction.channel
        p = ch.permissions_for(interaction.guild.me)
        if not (p.send_messages and p.manage_messages and p.embed_links):
            await interaction.response.send_message(
                f"{ch.mention} でBotに「メッセージの送信・メッセージの管理・埋め込みリンク」権限が必要です。", ephemeral=True)
            return
        try:
            col = int(color.lstrip("#"), 16) if color else None
        except ValueError:
            await interaction.response.send_message("色は #5865F2 の形式で入力してください。", ephemeral=True)
            return
        buttons = []
        if button_label and button_url:
            if not button_url.startswith(("https://", "http://")):
                await interaction.response.send_message("ボタンのURLは https:// から始めてください。", ephemeral=True)
                return
            buttons.append({"label": button_label, "url": button_url})
        conf = await self._conf(interaction.guild_id)
        old = conf.get(str(ch.id), {})
        cfg = {"content": text.replace("\\n", "\n"), "title": title, "color": col, "buttons": buttons,
               "msg": old.get("msg")}
        conf[str(ch.id)] = cfg
        await interaction.response.send_message(f"📌 {ch.mention} に案内メッセージをキープします。", ephemeral=True)
        await self._repost(ch, cfg)

    @group.command(name="copy", description="既存のメッセージ（Botのパネル等）をコピーして、キープ対象にします")
    @app_commands.describe(message_id="コピーするメッセージのID（このチャンネル内）")
    async def copy(self, interaction: discord.Interaction, message_id: str):
        try:
            src = await interaction.channel.fetch_message(int(message_id.strip()))
        except (ValueError, discord.HTTPException):
            await interaction.response.send_message("メッセージが見つかりません。", ephemeral=True)
            return
        e = src.embeds[0] if src.embeds else None
        buttons = [{"label": c.label or "リンク", "url": c.url} for row in src.components
                   for c in getattr(row, "children", []) if getattr(c, "url", None)]
        cfg = {"content": (e.description if e else src.content) or "", "title": e.title if e else None,
               "embed": bool(e), "color": e.colour.value if e and e.colour else None, "buttons": buttons[:5]}
        if not cfg["content"] and not cfg["title"]:
            await interaction.response.send_message("本文のないメッセージはコピーできません。", ephemeral=True)
            return
        conf = await self._conf(interaction.guild_id)
        cfg["msg"] = conf.get(str(interaction.channel_id), {}).get("msg")
        conf[str(interaction.channel_id)] = cfg
        await interaction.response.send_message(
            "📌 コピーしてキープ対象にしました。\n※ リンクボタン以外のボタン（認証ボタン等）はコピーできません。"
            "認証パネルは `/setup` のURLをリンクボタンにして `/sticky set` を使ってください。", ephemeral=True)
        await self._repost(interaction.channel, cfg)

    @group.command(name="remove", description="案内メッセージのキープを解除します")
    @app_commands.describe(channel="対象チャンネル（省略で今のチャンネル）", delete_message="最後の案内メッセージも削除する")
    async def remove(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None,
                     delete_message: bool = True):
        ch = channel or interaction.channel
        conf = await self._conf(interaction.guild_id)
        cfg = conf.pop(str(ch.id), None)
        await kv.hdel(self.bot, key(interaction.guild_id), str(ch.id))
        if cfg and delete_message and cfg.get("msg"):
            try:
                await ch.get_partial_message(int(cfg["msg"])).delete()
            except discord.HTTPException:
                pass
        await interaction.response.send_message("🗑️ 解除しました。" if cfg else "このチャンネルには設定がありません。",
                                                ephemeral=True)

    @group.command(name="list", description="キープ中の案内メッセージの一覧")
    async def list_(self, interaction: discord.Interaction):
        conf = await self._conf(interaction.guild_id)
        text = "\n".join(f"・<#{cid}>: {(c.get('title') or c.get('content') or '')[:40]}" for cid, c in conf.items())
        await interaction.response.send_message(text or "設定はありません。", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Sticky(bot))
