"""チケットのトランスクリプト（HTML / テキスト）を作るモジュール。
main.py から import して使う（このファイルは main.py を import しない）。"""
from __future__ import annotations

import html
import re
from datetime import timedelta, timezone

import discord

JST = timezone(timedelta(hours=9))

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#313338;color:#dbdee1;font-size:15px;line-height:1.45;
font-family:"gg sans","Noto Sans JP","Hiragino Sans","Yu Gothic",system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;background:#2b2d31;border-bottom:1px solid #1e1f22;
padding:14px 20px;display:flex;gap:14px;align-items:center}
header img{width:48px;height:48px;border-radius:50%}
header h1{margin:0;font-size:18px;color:#f2f3f5}
header .meta{color:#949ba4;font-size:13px}
.info{margin:16px 20px;padding:12px 16px;background:#2b2d31;border-radius:8px;
border-left:4px solid #5865f2;font-size:13px;color:#b5bac1}
.info b{color:#f2f3f5}
main{padding:8px 0 40px}
.group{display:flex;gap:16px;padding:6px 20px 2px;margin-top:12px}
.group:hover{background:#2e3035}
.avatar{width:40px;height:40px;border-radius:50%;flex:none;background:#5865f2}
.body{min-width:0;flex:1}
.head{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.name{font-weight:600;color:#f2f3f5}
.bot{background:#5865f2;color:#fff;font-size:10px;padding:1px 5px;border-radius:3px;font-weight:600}
.time{color:#949ba4;font-size:12px}
.msg{padding:1px 0;white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere}
.msg .edited{color:#949ba4;font-size:11px}
.reply{color:#949ba4;font-size:13px;margin-bottom:2px}
.reply:before{content:"┌ "}
code{background:#2b2d31;padding:1px 4px;border-radius:4px;font-family:Consolas,monospace;font-size:13px}
pre{background:#2b2d31;border:1px solid #1e1f22;padding:8px;border-radius:4px;overflow:auto;white-space:pre-wrap}
pre code{background:none;padding:0}
.mention{background:rgba(88,101,242,.3);color:#c9cdfb;padding:0 2px;border-radius:3px}
.spoiler{background:#1e1f22;color:transparent;border-radius:3px;cursor:pointer}
.spoiler:hover{color:inherit}
blockquote{margin:2px 0;padding-left:10px;border-left:4px solid #4e5058}
a{color:#00a8fc}
.emoji{width:22px;height:22px;vertical-align:bottom}
.att img{max-width:400px;max-height:300px;border-radius:6px;margin-top:4px;display:block}
.att .file{display:inline-block;margin-top:4px;padding:8px 12px;background:#2b2d31;border:1px solid #1e1f22;border-radius:6px}
.embed{margin-top:4px;max-width:520px;background:#2b2d31;border-left:4px solid #1e1f22;
border-radius:4px;padding:8px 12px}
.embed .t{font-weight:600;color:#f2f3f5}
.embed .f{margin-top:6px}
.embed .fn{font-weight:600;font-size:13px;color:#f2f3f5}
.embed .ft{margin-top:6px;font-size:12px;color:#949ba4}
.sticker{color:#949ba4;font-style:italic}
.sys{color:#949ba4;font-style:italic;padding:4px 20px 4px 76px}
footer{text-align:center;color:#6d6f78;font-size:12px;padding:20px}
"""


def _fmt_time(dt) -> str:
    return dt.astimezone(JST).strftime("%Y/%m/%d %H:%M:%S")


def _avatar(user) -> str:
    try:
        return str(user.display_avatar.replace(size=64, static_format="png").url)
    except Exception:
        return "https://cdn.discordapp.com/embed/avatars/0.png"


class _Resolver:
    def __init__(self, guild: discord.Guild | None, messages: list[discord.Message]):
        self.guild = guild
        self.users: dict[int, str] = {}
        for m in messages:
            self.users[m.author.id] = getattr(m.author, "display_name", str(m.author))
            for u in m.mentions:
                self.users[u.id] = getattr(u, "display_name", str(u))

    def user(self, uid: int) -> str:
        if uid in self.users:
            return self.users[uid]
        if self.guild:
            mem = self.guild.get_member(uid)
            if mem:
                return mem.display_name
        return str(uid)

    def channel(self, cid: int) -> str:
        ch = self.guild.get_channel(cid) if self.guild else None
        return ch.name if ch else str(cid)

    def role(self, rid: int) -> str:
        r = self.guild.get_role(rid) if self.guild else None
        return r.name if r else str(rid)


def render_markdown(text: str, rs: _Resolver) -> str:
    """Discordの書式をおおまかにHTMLへ変換する（入力は必ず先にエスケープする）。"""
    if not text:
        return ""
    stash: list[str] = []

    def keep(s: str) -> str:
        stash.append(s)
        return f"\x00{len(stash) - 1}\x00"

    # コードブロック・インラインコードは、ほかの書式より先に退避する
    text = re.sub(r"```(?:[a-zA-Z0-9_+-]*\n)?(.*?)```",
                  lambda m: keep(f"<pre><code>{html.escape(m.group(1))}</code></pre>"),
                  text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    # カスタム絵文字・メンション（エスケープ前に置き換え）
    text = re.sub(r"<(a?):(\w+):(\d+)>", lambda m: keep(
        f'<img class="emoji" alt=":{html.escape(m.group(2))}:" title=":{html.escape(m.group(2))}:" '
        f'src="https://cdn.discordapp.com/emojis/{m.group(3)}.{"gif" if m.group(1) else "png"}">'), text)
    text = re.sub(r"<@!?(\d+)>", lambda m: keep(
        f'<span class="mention">@{html.escape(rs.user(int(m.group(1))))}</span>'), text)
    text = re.sub(r"<@&(\d+)>", lambda m: keep(
        f'<span class="mention">@{html.escape(rs.role(int(m.group(1))))}</span>'), text)
    text = re.sub(r"<#(\d+)>", lambda m: keep(
        f'<span class="mention">#{html.escape(rs.channel(int(m.group(1))))}</span>'), text)
    text = re.sub(r"<t:(\d+)(?::[tTdDfFR])?>", lambda m: keep(
        f'<span class="mention">{_fmt_ts(int(m.group(1)))}</span>'), text)

    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text, flags=re.S)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.S)
    text = re.sub(r"\|\|(.+?)\|\|", r'<span class="spoiler">\1</span>', text, flags=re.S)
    text = re.sub(r"^&gt; (.*)$", r"<blockquote>\1</blockquote>", text, flags=re.M)
    text = re.sub(r"(https?://[^\s<>&\"']+)",
                  r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>', text)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def _fmt_ts(ts: int) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts, JST).strftime("%Y/%m/%d %H:%M")


def _embed_html(e: discord.Embed, rs: _Resolver) -> str:
    color = f"#{e.colour.value:06x}" if e.colour else "#1e1f22"
    parts = [f'<div class="embed" style="border-left-color:{color}">']
    if e.author and e.author.name:
        parts.append(f'<div class="fn">{html.escape(e.author.name)}</div>')
    if e.title:
        parts.append(f'<div class="t">{render_markdown(e.title, rs)}</div>')
    if e.description:
        parts.append(f'<div>{render_markdown(e.description, rs)}</div>')
    for f in e.fields:
        parts.append(f'<div class="f"><div class="fn">{render_markdown(f.name, rs)}</div>'
                     f'<div>{render_markdown(f.value, rs)}</div></div>')
    if e.image and e.image.url:
        parts.append(f'<div class="att"><img src="{html.escape(e.image.url)}" loading="lazy"></div>')
    if e.footer and e.footer.text:
        parts.append(f'<div class="ft">{html.escape(e.footer.text)}</div>')
    parts.append("</div>")
    return "".join(parts)


def _message_body(m: discord.Message, rs: _Resolver) -> str:
    out = []
    if m.reference and isinstance(m.reference.resolved, discord.Message):
        r = m.reference.resolved
        preview = (r.content or "（添付・埋め込み）")[:80]
        out.append(f'<div class="reply">@{html.escape(rs.user(r.author.id))}: '
                   f'{html.escape(preview)}</div>')
    if m.content:
        edited = ' <span class="edited">(編集済)</span>' if m.edited_at else ""
        out.append(f'<div class="msg">{render_markdown(m.content, rs)}{edited}</div>')
    for a in m.attachments:
        url = html.escape(a.url)
        if (a.content_type or "").startswith("image/"):
            out.append(f'<div class="att"><a href="{url}" target="_blank" rel="noopener">'
                       f'<img src="{url}" alt="{html.escape(a.filename)}" loading="lazy"></a></div>')
        else:
            out.append(f'<div class="att"><a class="file" href="{url}" target="_blank" rel="noopener">'
                       f'📎 {html.escape(a.filename)}（{a.size // 1024} KB）</a></div>')
    for e in m.embeds:
        out.append(_embed_html(e, rs))
    for s in m.stickers:
        out.append(f'<div class="sticker">［スタンプ: {html.escape(s.name)}］</div>')
    return "".join(out)


def build_html(channel, messages: list[discord.Message], *, saved_by=None,
               bot_name: str = "Bot") -> str:
    guild = getattr(channel, "guild", None)
    rs = _Resolver(guild, messages)
    participants = {}
    for m in messages:
        participants.setdefault(m.author.id, m.author)
    rows = []
    prev = None
    for m in messages:
        if m.type not in (discord.MessageType.default, discord.MessageType.reply,
                          discord.MessageType.chat_input_command,
                          discord.MessageType.context_menu_command):
            rows.append(f'<div class="sys">{html.escape(m.system_content or str(m.type))}'
                        f' — {_fmt_time(m.created_at)}</div>')
            prev = None
            continue
        same = (prev is not None and prev.author.id == m.author.id and m.reference is None
                and (m.created_at - prev.created_at).total_seconds() < 420)
        body = _message_body(m, rs)
        if same:
            rows[-1] = rows[-1].removesuffix("</div></div>") + body + "</div></div>"
        else:
            badge = '<span class="bot">BOT</span>' if m.author.bot else ""
            name = html.escape(getattr(m.author, "display_name", str(m.author)))
            rows.append(
                f'<div class="group"><img class="avatar" src="{html.escape(_avatar(m.author))}" '
                f'alt="" loading="lazy"><div class="body"><div class="head">'
                f'<span class="name" title="{html.escape(str(m.author))} ({m.author.id})">{name}</span>'
                f'{badge}<span class="time">{_fmt_time(m.created_at)}</span></div>{body}</div></div>')
        prev = m

    icon = ""
    if guild and guild.icon:
        icon = f'<img src="{html.escape(str(guild.icon.replace(size=64).url))}" alt="">'
    plist = "、".join(html.escape(f"{getattr(u, 'display_name', u)} ({u.id})")
                     for u in participants.values())
    from datetime import datetime
    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    saver = html.escape(f"{saved_by} ({saved_by.id})") if saved_by else "-"
    title = f"#{channel.name}"
    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{html.escape(title)} - トランスクリプト</title><style>{CSS}</style></head><body>
<header>{icon}<div><h1>{html.escape(title)}</h1>
<div class="meta">{html.escape(guild.name if guild else "")} ・ {len(messages)} 件のメッセージ</div></div></header>
<div class="info"><b>保存日時:</b> {now}　<b>保存者:</b> {saver}<br>
<b>参加者（{len(participants)}人）:</b> {plist or "-"}</div>
<main>{"".join(rows) or '<div class="sys">メッセージはありません</div>'}</main>
<footer>{html.escape(bot_name)} により生成 ・ 画像や添付ファイルのリンクは、Discordの仕様で一定期間後に表示できなくなることがあります</footer>
</body></html>"""


def build_text(channel, messages: list[discord.Message], *, saved_by=None) -> str:
    from datetime import datetime
    lines = [f"=== チケットログ: #{channel.name} ===",
             f"サーバー: {getattr(getattr(channel, 'guild', None), 'name', '')}",
             f"保存日時: {datetime.now(JST):%Y-%m-%d %H:%M:%S} (JST)",
             f"保存者: {saved_by} ({saved_by.id})" if saved_by else "保存者: -",
             f"メッセージ数: {len(messages)}", ""]
    for m in messages:
        lines.append(f"[{_fmt_time(m.created_at)}] {m.author} (ID: {m.author.id}): {m.content}")
        for a in m.attachments:
            lines.append(f"  [添付ファイル: {a.filename} {a.url}]")
        for e in m.embeds:
            lines.append(f"  [埋め込み: {e.title or ''} {(e.description or '')[:200]}]")
    return "\n".join(lines) + "\n"
