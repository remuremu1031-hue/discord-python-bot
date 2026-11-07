"""Botオーナー専用のWebダッシュボード（Discord OAuth2ログイン付き）。

main.py から register(app, ctx) で登録する。このファイルは main.py を import しない。
アクセス制御:
  - Discordでログインし、Botのオーナー（チーム所有ならチームのオーナー）であることを毎リクエスト確認
  - セッションはサーバー側のメモリに保存（Cookieにはランダムなトークンだけ。再起動でログアウト）
  - ログインのstateはCookieと突き合わせる（ログインCSRF対策）、操作はCSRFトークン必須
  - オーナー以外がログインしようとしたら、セキュリティ通知を送る
"""
from __future__ import annotations

import asyncio
import html
import json
import platform
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import discord
import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

JST = timezone(timedelta(hours=9))
SESSION_COOKIE = "dash_session"
STATE_COOKIE = "dash_state"
SESSION_TTL = 12 * 3600
_sessions: dict[str, dict] = {}
_states: dict[str, float] = {}

TABS = [("overview", "📊 概要"), ("logs", "📜 Botログ"), ("auth", "🔐 認証ログ"),
        ("security", "🚨 Nuke/Raid"), ("gban", "🚫 GBAN"), ("license", "⏸️ ライセンス"),
        ("blacklist", "⛔ ブラックリスト"), ("guilds", "🏠 サーバー"), ("ips", "🌐 IP連携")]

CSS = """
*{box-sizing:border-box}body{margin:0;background:#0b0f19;color:#e5e7eb;
font:14px/1.6 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
a{color:#93c5fd;text-decoration:none}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;
padding:12px 18px;background:#111827;border-bottom:1px solid #1f2937;position:sticky;top:0;z-index:5}
header h1{margin:0;font-size:17px}header .me{display:flex;gap:10px;align-items:center;color:#9ca3af}
header img{width:28px;height:28px;border-radius:50%}
nav{display:flex;gap:4px;overflow-x:auto;padding:8px 12px;background:#0f172a;border-bottom:1px solid #1f2937}
nav a{padding:6px 12px;border-radius:8px;color:#cbd5e1;white-space:nowrap}
nav a.on{background:#5865f2;color:#fff}
main{padding:16px;max-width:1400px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px;margin-bottom:16px}
.card{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:14px}
.card .k{color:#9ca3af;font-size:12px}.card .v{font-size:20px;font-weight:700;margin-top:4px}
.ok{color:#22c55e}.ng{color:#ef4444}.warn{color:#f59e0b}.mut{color:#9ca3af}
table{width:100%;border-collapse:collapse;background:#111827;border:1px solid #1f2937;border-radius:12px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid #1f2937;text-align:left;vertical-align:top;font-size:13px}
th{background:#0f172a;color:#9ca3af;font-weight:600;position:sticky;top:0}
tr:hover td{background:#151d2e}
.wrap{overflow:auto;max-height:75vh;border-radius:12px}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600}
.t-ok{background:#14532d;color:#86efac}.t-ng{background:#7f1d1d;color:#fecaca}
.t-warn{background:#78350f;color:#fde68a}.t-mut{background:#1f2937;color:#9ca3af}
form.inline{display:inline}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
input,select{background:#0f172a;color:#e5e7eb;border:1px solid #334155;border-radius:8px;padding:7px 10px;font:inherit}
button{background:#5865f2;color:#fff;border:0;border-radius:8px;padding:7px 12px;font:inherit;cursor:pointer}
button.danger{background:#dc2626}button.ghost{background:#334155}
pre.log{margin:0;white-space:pre-wrap;word-break:break-all;font:12px/1.5 Consolas,monospace}
h2{font-size:16px;margin:18px 0 10px}
.flash{padding:10px 14px;border-radius:10px;background:#1e293b;border:1px solid #334155;margin-bottom:12px}
code{font-family:Consolas,monospace;font-size:12px;color:#c4b5fd}
"""


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _t(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), JST).strftime("%m/%d %H:%M:%S")
    except Exception:
        return "-"


def _secure_headers(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; img-src https://cdn.discordapp.com data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
    return resp


def register(app, ctx) -> None:
    """ctx に必要なもの（main.py が用意）:
    bot, client_id, client_secret, base_url()（関数）, get_owner_ids(), limiter, client_ip(),
    send_security_alert(), log_buffer, boot_time, storage_remote, redis_client(), load_auth_logs(),
    load_incidents(), load_license(), save_license(), load_blacklist(), save_blacklist(), load_ip_index(),
    load_defense_config(), load_config(), gban_list(), gban_add_all(), gban_del(), ip_retention_days"""

    def redirect_uri() -> str:
        return ctx.base_url() + "/dashboard/callback"

    def cookie_secure() -> bool:
        return ctx.base_url().startswith("https://")

    async def current(request: Request) -> dict | None:
        tok = request.cookies.get(SESSION_COOKIE)
        sess = _sessions.get(tok or "")
        if not sess or sess["exp"] < time.time():
            _sessions.pop(tok or "", None)
            return None
        try:  # オーナーでなくなった場合（チーム移譲など）に備え、毎回確認する
            if sess["uid"] not in await ctx.get_owner_ids():
                _sessions.pop(tok, None)
                return None
        except Exception:
            return None
        return sess

    def page(sess: dict, tab: str, body: str, flash: str = "") -> HTMLResponse:
        nav = "".join(f'<a class="{"on" if k == tab else ""}" href="/dashboard?tab={k}">{_e(v)}</a>'
                      for k, v in TABS)
        av = f'<img src="{_e(sess["avatar"])}" alt="">' if sess.get("avatar") else ""
        doc = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>管理ダッシュボード</title><style>{CSS}</style></head><body>
<header><h1>🛡️ 認証Bot 管理ダッシュボード</h1><div class="me">{av}<span>{_e(sess["name"])}</span>
<form class="inline" method="post" action="/dashboard/logout"><input type="hidden" name="csrf" value="{_e(sess['csrf'])}">
<button class="ghost">ログアウト</button></form></div></header>
<nav>{nav}</nav><main>{f'<div class="flash">{_e(flash)}</div>' if flash else ""}{body}</main></body></html>"""
        return _secure_headers(HTMLResponse(doc))

    def form(sess, action: str, fields: str, label: str, cls: str = "", confirm: str = "") -> str:
        return (f'<form class="inline" method="post" action="/dashboard/action">'
                f'<input type="hidden" name="csrf" value="{_e(sess["csrf"])}">'
                f'<input type="hidden" name="action" value="{_e(action)}">{fields}'
                f'<button class="{cls}">{_e(label)}</button></form>')

    def hidden(name, value) -> str:
        return f'<input type="hidden" name="{_e(name)}" value="{_e(value)}">'

    def guild_name(gid) -> str:
        g = ctx.bot.get_guild(int(gid or 0))
        return g.name if g else str(gid)

    # ---------- 各タブ ----------
    async def tab_overview(sess, q) -> str:
        bot = ctx.bot
        ready = bot.is_ready()
        lat = bot.latency
        ping = f"{round(lat * 1000)} ms" if lat == lat and lat != float("inf") else "-"
        redis_ok = "未設定"
        r = ctx.redis_client()
        if r is not None:
            try:
                await asyncio.wait_for(r.ping(), 3)
                redis_ok = "正常"
            except Exception:
                redis_ok = "接続エラー"
        now = time.time()
        logs = ctx.load_auth_logs()
        day = [x for x in logs if now - x.get("t", 0) < 86400]
        inc = [x for x in ctx.load_incidents() if now - x.get("t", 0) < 86400]
        try:
            gbans = len(await ctx.gban_list())
        except Exception:
            gbans = "-"
        bl = ctx.load_blacklist()
        up = int(now - ctx.boot_time)
        mem = "-"
        try:
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            mem = f"{rss / 1024:.0f} MB" if sys.platform != "darwin" else f"{rss / 1048576:.0f} MB"
        except Exception:
            pass

        def card(k, v, cls=""):
            return f'<div class="card"><div class="k">{_e(k)}</div><div class="v {cls}">{_e(v)}</div></div>'
        cards = "".join([
            card("Bot", "オンライン" if ready else "オフライン", "ok" if ready else "ng"),
            card("Ping", ping),
            card("稼働時間", f"{up // 86400}日 {up % 86400 // 3600}時間 {up % 3600 // 60}分"),
            card("導入サーバー", f"{len(bot.guilds):,}"),
            card("合計メンバー", f"{sum((g.member_count or 0) for g in bot.guilds):,}"),
            card("設定の保存先", "Upstash" if ctx.storage_remote else "ローカルJSON"),
            card("Redis（GBAN）", redis_ok, "ok" if redis_ok == "正常" else "warn"),
            card("メモリ（最大）", mem),
            card("認証（24時間）", f"成功 {sum(1 for x in day if x.get('passed'))} / 拒否 {sum(1 for x in day if not x.get('passed'))}"),
            card("サブ垢検知（24時間）", sum(1 for x in day if x.get("sub") == "hit"),
                 "warn" if any(x.get("sub") == "hit" for x in day) else ""),
            card("Nuke/Raid（24時間）", len(inc), "ng" if inc else "ok"),
            card("GBAN登録", gbans),
            card("ライセンス停止中", len(ctx.load_license())),
            card("ブラックリスト", f"ユーザー {len(bl['users'])} / サーバー {len(bl['guilds'])}"),
            card("IP記録", f"{len(ctx.load_ip_index()):,} 件（{ctx.ip_retention_days}日保持）"),
        ])
        cogs = ", ".join(sorted(x.removeprefix("cogs.") for x in bot.extensions)) or "なし"
        sysinfo = (f"<table><tr><th>項目</th><th>内容</th></tr>"
                   f"<tr><td>Python</td><td>{_e(platform.python_version())}</td></tr>"
                   f"<tr><td>discord.py</td><td>{_e(discord.__version__)}</td></tr>"
                   f"<tr><td>読み込み済みの追加機能</td><td>{_e(cogs)}</td></tr>"
                   f"<tr><td>公開URL</td><td>{_e(ctx.base_url())}</td></tr>"
                   f"<tr><td>エクスポート</td><td><a href='/dashboard/export?kind=auth'>認証ログ</a> ・ "
                   f"<a href='/dashboard/export?kind=incidents'>Nuke/Raid履歴</a> ・ "
                   f"<a href='/dashboard/export?kind=logs'>Botログ</a>（JSON）</td></tr></table>")
        recent = "".join(
            f"<tr><td>{_t(x.get('t'))}</td><td>{_e(x.get('type'))}</td><td>{_e(x.get('guild_name') or x.get('guild'))}</td>"
            f"<td>{_e(x.get('result'))}</td></tr>" for x in reversed(ctx.load_incidents()[-8:]))
        return (f'<div class="cards">{cards}</div><h2>システム情報</h2>{sysinfo}'
                f'<h2>最近のセキュリティ事案</h2><div class="wrap"><table><tr><th>時刻</th><th>種類</th>'
                f'<th>サーバー</th><th>結果</th></tr>{recent or "<tr><td colspan=4 class=mut>なし</td></tr>"}</table></div>')

    async def tab_logs(sess, q) -> str:
        lines = list(ctx.log_buffer)
        if q:
            lines = [x for x in lines if q.lower() in x[2].lower()]
        rows = "".join(
            f'<tr><td class="mut">{_t(t)}</td><td><span class="tag {"t-ng" if lv == "ERR" else "t-mut"}">{_e(lv)}</span></td>'
            f'<td><pre class="log">{_e(line)}</pre></td></tr>' for t, lv, line in reversed(lines[-800:]))
        return (search_bar("logs", q, "ログを検索") +
                f'<div class="wrap"><table><tr><th>時刻</th><th>種類</th><th>内容（新しい順・最大800行）</th></tr>'
                f'{rows or "<tr><td colspan=3 class=mut>ログはありません</td></tr>"}</table></div>')

    def search_bar(tab, q, ph, extra="") -> str:
        return (f'<form class="bar" method="get" action="/dashboard">{hidden("tab", tab)}'
                f'<input name="q" value="{_e(q)}" placeholder="{_e(ph)}" size="30">{extra}<button>検索</button></form>')

    async def tab_auth(sess, q, sub="") -> str:
        logs = ctx.load_auth_logs()
        if sub:
            logs = [x for x in logs if x.get("sub") == sub]
        if q:
            ql = q.lower()
            logs = [x for x in logs if ql in json.dumps(x, ensure_ascii=False).lower()
                    or ql in guild_name(x.get("guild")).lower()]
        sub_tag = {"hit": '<span class="tag t-warn">該当</span>', "clear": '<span class="tag t-ok">非該当</span>'}
        unknown_tag = '<span class="tag t-mut">判定不可</span>'
        rows = []
        for x in reversed(logs[-500:]):
            res = ('<span class="tag t-ok">成功</span>' if x.get("passed") else
                   '<span class="tag t-ng">拒否</span>')
            linked = " ".join(f"<code>{_e(u)}</code>" for u in x.get("linked", [])[:8])
            flags = "<br>".join(f"{_e(k)}: <code>{_e(v)}</code>" for k, v in x.get("flags", []))
            rows.append(
                f"<tr><td>{_t(x.get('t'))}</td><td>{_e(guild_name(x.get('guild')))}</td>"
                f"<td>{_e(x.get('name'))}<br><code>{_e(x.get('user'))}</code></td><td>{res}</td>"
                f"<td>{sub_tag.get(x.get('sub'), unknown_tag)}</td>"
                f"<td>{linked or '-'}</td><td>{flags or '-'}</td>"
                f"<td><code>{_e((x.get('ip') or '')[:10]) or '-'}</code><br><span class='mut'>{_e(x.get('ip_masked') or '')}</span></td>"
                f"<td>{_e(x.get('reason') or '')}<br><span class='mut'>作成{_e(x.get('age_days'))}日</span></td></tr>")
        sel = (f'<select name="sub"><option value="">すべて</option>'
               f'<option value="hit"{" selected" if sub == "hit" else ""}>サブ垢：該当のみ</option>'
               f'<option value="clear"{" selected" if sub == "clear" else ""}>サブ垢：非該当のみ</option></select>')
        return (search_bar("auth", q, "ユーザー名・ID・サーバー名", sel) +
                '<div class="wrap"><table><tr><th>時刻</th><th>サーバー</th><th>ユーザー</th><th>結果</th>'
                '<th>サブアカウント検知</th><th>同一IPのアカウント</th><th>IP連携の警告</th><th>IP識別子</th><th>理由</th></tr>'
                + ("".join(rows) or "<tr><td colspan=9 class=mut>記録はありません</td></tr>") + "</table></div>")

    async def tab_security(sess, q) -> str:
        items = ctx.load_incidents()
        if q:
            items = [x for x in items if q.lower() in json.dumps(x, ensure_ascii=False).lower()]
        cls = {"nuke": "t-ng", "raid": "t-ng", "restore": "t-ok", "warning": "t-warn"}
        rows = "".join(
            f"<tr><td>{_t(x.get('t'))}</td><td><span class='tag {cls.get(x.get('type'), 't-mut')}'>{_e(x.get('type'))}</span></td>"
            f"<td>{_e(x.get('guild_name'))}<br><code>{_e(x.get('guild'))}</code></td>"
            f"<td>{_e(x.get('actor_name') or '')}<br><code>{_e(x.get('actor') or '')}</code>{' 🤖' if x.get('actor_bot') else ''}</td>"
            f"<td>{_e(x.get('kind') or '')} {(_e(x.get('count')) + '回') if x.get('count') else ''}</td>"
            f"<td>{_e(x.get('result'))}</td></tr>" for x in reversed(items))
        return (search_bar("security", q, "サーバー名・ID・実行者") +
                '<div class="wrap"><table><tr><th>時刻</th><th>種類</th><th>サーバー</th><th>実行者</th>'
                '<th>内容</th><th>自動対処の結果</th></tr>'
                + (rows or "<tr><td colspan=6 class=mut>記録はありません</td></tr>") + "</table></div>")

    async def tab_gban(sess, q) -> str:
        try:
            items = await ctx.gban_list()
        except Exception as e:
            return f'<div class="flash">GBANの一覧を取得できません（{_e(type(e).__name__)}）。Redisの設定を確認してください。</div>'
        if q:
            items = [(u, d) for u, d in items if q in str(u) or q.lower() in json.dumps(d, ensure_ascii=False).lower()]
        add = form(sess, "gban_add", '<input name="uid" placeholder="ユーザーID" required size="22">'
                   '<input name="reason" placeholder="理由" required size="30" maxlength="200">',
                   "GBAN登録（全サーバーでBAN）", "danger")
        rows = "".join(
            f"<tr><td><code>{_e(u)}</code></td><td>{_e(d.get('reason'))}</td><td>{_e(d.get('at'))}</td>"
            f"<td><code>{_e(d.get('by'))}</code></td><td>{form(sess, 'gban_remove', hidden('uid', u), '登録解除', 'ghost')}</td></tr>"
            for u, d in items)
        return (f'<div class="bar">{add}</div>' + search_bar("gban", q, "ユーザーID・理由") +
                f'<div class="wrap"><table><tr><th>ユーザーID</th><th>理由</th><th>登録日時</th><th>登録者</th><th></th></tr>'
                f'{rows or "<tr><td colspan=5 class=mut>登録はありません</td></tr>"}</table></div>')

    async def tab_license(sess, q) -> str:
        data = ctx.load_license()
        add = form(sess, "license_suspend", '<input name="gid" placeholder="サーバーID" required size="22">',
                   "ライセンス停止", "danger")
        rows = "".join(
            f"<tr><td><code>{_e(g)}</code></td><td>{_e(guild_name(g))}</td><td>{_e(v.get('at'))}</td>"
            f"<td>{form(sess, 'license_active', hidden('gid', g), '有効化（再開）')}</td></tr>"
            for g, v in sorted(data.items()))
        return (f'<div class="bar">{add}</div><p class="mut">停止中のサーバーでは、コマンド・ボタン・自動モデレーション・'
                f'イベントログ・Nuke検知・自動送信など、すべての処理が止まります。</p>'
                f'<div class="wrap"><table><tr><th>サーバーID</th><th>名前</th><th>停止日時</th><th></th></tr>'
                f'{rows or "<tr><td colspan=4 class=mut>停止中のサーバーはありません</td></tr>"}</table></div>')

    async def tab_blacklist(sess, q) -> str:
        bl = ctx.load_blacklist()
        add = form(sess, "bl_add", '<select name="kind"><option value="users">ユーザー</option>'
                   '<option value="guilds">サーバー</option></select><input name="id" placeholder="ID" required size="22">',
                   "追加", "danger")
        rows = ""
        for kind, label in (("users", "ユーザー"), ("guilds", "サーバー")):
            for i in sorted(bl[kind]):
                name = guild_name(i) if kind == "guilds" else ""
                rows += (f"<tr><td>{label}</td><td><code>{_e(i)}</code> {_e(name if name != str(i) else '')}</td>"
                         f"<td>{form(sess, 'bl_remove', hidden('kind', kind) + hidden('id', i), '削除', 'ghost')}</td></tr>")
        return (f'<div class="bar">{add}</div><div class="wrap"><table><tr><th>種類</th><th>ID</th><th></th></tr>'
                f'{rows or "<tr><td colspan=3 class=mut>登録はありません</td></tr>"}</table></div>')

    async def tab_guilds(sess, q) -> str:
        lic = ctx.load_license()
        dcfg = ctx.load_defense_config()
        cfg = ctx.load_config()
        gs = sorted(ctx.bot.guilds, key=lambda g: -(g.member_count or 0))
        if q:
            gs = [g for g in gs if q.lower() in g.name.lower() or q in str(g.id)]
        rows = []
        for g in gs:
            d = dcfg.get(str(g.id), {})
            st = ('<span class="tag t-ng">停止中</span>' if str(g.id) in lic else '<span class="tag t-ok">有効</span>')
            sec = " ".join(lbl for k, lbl in (("nuke_defense", "Nuke"), ("raid_protection", "Raid"),
                                             ("invite_block", "招待ブロック")) if d.get(k)) or "-"
            rows.append(f"<tr><td>{_e(g.name)}<br><code>{g.id}</code></td><td>{g.member_count or 0:,}</td>"
                        f"<td><code>{_e(g.owner_id)}</code></td><td>{st}</td>"
                        f"<td>{'設定済み' if str(g.id) in cfg else '<span class=mut>未設定</span>'}</td><td>{_e(sec)}</td></tr>")
        return (search_bar("guilds", q, "サーバー名・ID") +
                '<div class="wrap"><table><tr><th>サーバー</th><th>メンバー</th><th>所有者ID</th><th>ライセンス</th>'
                '<th>認証設定</th><th>セキュリティ</th></tr>' + "".join(rows) + "</table></div>")

    async def tab_ips(sess, q) -> str:
        idx = ctx.load_ip_index()
        rows = []
        items = []
        for h, rec in idx.items():
            users = {e["u"] for e in rec.get("e", [])}
            if q and not (q in h or any(q == str(u) for u in users)):
                continue
            if not q and len(users) < 2:
                continue  # 既定では、複数アカウントが紐づくIPだけを表示
            items.append((len(users), h, rec, users))
        items.sort(key=lambda x: (-x[0], -max(e["t"] for e in x[2]["e"])))
        for n, h, rec, users in items[:300]:
            gl = sorted({guild_name(e["g"]) for e in rec["e"]})
            raw = f"<br><span class='warn'>{_e(rec['raw'])}</span>" if rec.get("raw") else ""
            rows.append(f"<tr><td><code>{_e(h[:10])}</code><br><span class='mut'>{_e(rec.get('m'))}</span>{raw}</td>"
                        f"<td>{n}</td><td>{' '.join(f'<code>{_e(u)}</code>' for u in sorted(users))}</td>"
                        f"<td>{_e('、'.join(gl)[:300])}</td><td>{_t(max(e['t'] for e in rec['e']))}</td></tr>")
        return (search_bar("ips", q, "ユーザーID・IP識別子で検索") +
                '<p class="mut">既定では、2つ以上のアカウントが同じIPから認証した記録だけを表示します。</p>'
                '<div class="wrap"><table><tr><th>IP識別子</th><th>アカウント数</th><th>ユーザーID</th><th>サーバー</th><th>最終認証</th></tr>'
                + ("".join(rows) or "<tr><td colspan=5 class=mut>該当なし</td></tr>") + "</table></div>")

    TAB_FUNCS = {"overview": tab_overview, "logs": tab_logs, "security": tab_security,
                 "gban": tab_gban, "license": tab_license, "blacklist": tab_blacklist,
                 "guilds": tab_guilds, "ips": tab_ips}

    # ---------- ルーティング ----------
    @app.get("/dashboard")
    async def dashboard_home(request: Request, tab: str = "overview", q: str = "", sub: str = "",
                             msg: str = ""):
        sess = await current(request)
        if sess is None:
            return RedirectResponse("/dashboard/login", status_code=303)
        q = q.strip()[:100]
        if tab == "auth":
            body = await tab_auth(sess, q, sub if sub in ("hit", "clear") else "")
        else:
            tab = tab if tab in TAB_FUNCS else "overview"
            body = await TAB_FUNCS[tab](sess, q)
        return page(sess, tab, body, msg[:300])

    @app.get("/dashboard/login")
    @ctx.limiter.limit("15/minute")
    async def dashboard_login(request: Request):
        now = time.time()
        for k in [k for k, v in _states.items() if now - v > 600]:
            _states.pop(k, None)
        state = secrets.token_urlsafe(24)
        _states[state] = now
        params = httpx.QueryParams({"client_id": ctx.client_id, "redirect_uri": redirect_uri(),
                                    "response_type": "code", "scope": "identify", "state": state,
                                    "prompt": "none"})
        resp = RedirectResponse(f"https://discord.com/oauth2/authorize?{params}", status_code=303)
        resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, secure=cookie_secure(),
                        samesite="lax", path="/dashboard")
        return _secure_headers(resp)

    def denied(title: str, text: str, status: int = 403) -> HTMLResponse:
        doc = (f'<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" '
               f'content="width=device-width,initial-scale=1"><title>{_e(title)}</title><style>{CSS}'
               f'.c{{max-width:420px;margin:15vh auto;padding:28px;text-align:center}}</style></head>'
               f'<body><div class="card c"><h2>{_e(title)}</h2><p class="mut">{_e(text)}</p>'
               f'<a href="/dashboard/login">もう一度ログイン</a></div></body></html>')
        return _secure_headers(HTMLResponse(doc, status_code=status))

    @app.get("/dashboard/callback")
    @ctx.limiter.limit("15/minute")
    async def dashboard_callback(request: Request, code: str | None = None, state: str | None = None):
        cookie_state = request.cookies.get(STATE_COOKIE)
        issued = _states.pop(state or "", None)
        if (not code or not state or issued is None or time.time() - issued > 600
                or not cookie_state or not secrets.compare_digest(cookie_state, state)):
            return denied("ログインに失敗しました", "セッションが無効です。最初からやり直してください。", 400)
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.post("https://discord.com/api/v10/oauth2/token", data={
                "client_id": ctx.client_id, "client_secret": ctx.client_secret,
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()},
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            if r.status_code != 200:
                return denied("ログインに失敗しました",
                              "Discordとの通信に失敗しました（Developer PortalのRedirectsに "
                              "/dashboard/callback が登録されているか確認してください）。", 400)
            token = r.json().get("access_token")
            r = await http.get("https://discord.com/api/v10/users/@me",
                               headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            return denied("ログインに失敗しました", "ユーザー情報を取得できませんでした。", 400)
        user = r.json()
        uid = int(user["id"])
        if uid not in await ctx.get_owner_ids():
            asyncio.create_task(ctx.send_security_alert(
                "ダッシュボードへの不正ログイン試行",
                f"Botのオーナー以外のアカウント（ID: {uid}）がダッシュボードにログインしようとしました",
                request, level="critical", dedupe_key=f"dash-deny:{uid}"))
            return denied("アクセスできません", "このダッシュボードは、Botの所有者専用です。")
        av = (f"https://cdn.discordapp.com/avatars/{uid}/{user['avatar']}.png?size=64"
              if user.get("avatar") else "")
        tok = secrets.token_urlsafe(32)
        for k in [k for k, v in _sessions.items() if v["exp"] < time.time()]:
            _sessions.pop(k, None)
        _sessions[tok] = {"uid": uid, "name": user.get("global_name") or user.get("username"),
                          "avatar": av, "exp": time.time() + SESSION_TTL, "csrf": secrets.token_urlsafe(24)}
        print(f"ダッシュボードにログインしました（オーナー ID: {uid}）")
        resp = RedirectResponse("/dashboard", status_code=303)
        resp.set_cookie(SESSION_COOKIE, tok, max_age=SESSION_TTL, httponly=True, secure=cookie_secure(),
                        samesite="lax", path="/dashboard")
        resp.delete_cookie(STATE_COOKIE, path="/dashboard")
        return _secure_headers(resp)

    async def read_form(request: Request) -> dict[str, str]:
        body = (await request.body())[:20000].decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(body).items()}

    @app.post("/dashboard/logout")
    async def dashboard_logout(request: Request):
        sess = await current(request)
        f = await read_form(request)
        if sess and secrets.compare_digest(f.get("csrf", ""), sess["csrf"]):
            _sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        resp = RedirectResponse("/dashboard/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/dashboard")
        return _secure_headers(resp)

    def _id(v: str) -> int | None:
        v = (v or "").strip()
        return int(v) if v.isdigit() and 15 <= len(v) <= 21 else None

    @app.post("/dashboard/action")
    async def dashboard_action(request: Request):
        sess = await current(request)
        if sess is None:
            return RedirectResponse("/dashboard/login", status_code=303)
        f = await read_form(request)
        if not secrets.compare_digest(f.get("csrf", ""), sess["csrf"]):
            return denied("操作できません", "CSRFトークンが一致しません。ページを開き直してください。", 400)
        act = f.get("action", "")
        tab, msg = "overview", ""
        try:
            if act == "gban_add":
                tab = "gban"
                uid = _id(f.get("uid", ""))
                reason = (f.get("reason") or "").strip()[:200]
                if uid is None or not reason:
                    msg = "ユーザーIDと理由を正しく入力してください。"
                elif uid in await ctx.get_owner_ids() or (ctx.bot.user and uid == ctx.bot.user.id):
                    msg = "オーナー自身やBotはGlobal BANできません。"
                else:
                    await ctx.gban_add_all(uid, reason, sess["uid"])
                    msg = f"{uid} をGBAN登録しました。全サーバーでのBANをバックグラウンドで実行中です。"
            elif act == "gban_remove":
                tab = "gban"
                uid = _id(f.get("uid", ""))
                msg = (f"{uid} のGBAN登録を削除しました。" if uid and await ctx.gban_del(uid)
                       else "登録が見つかりません。")
            elif act in ("license_suspend", "license_active"):
                tab = "license"
                gid = _id(f.get("gid", ""))
                data = ctx.load_license()
                if gid is None:
                    msg = "サーバーIDを正しく入力してください。"
                elif act == "license_suspend":
                    data[str(gid)] = {"at": datetime.now(JST).strftime("%Y-%m-%d %H:%M")}
                    ctx.save_license(data)
                    msg = f"{gid} のライセンスを停止しました。"
                else:
                    data.pop(str(gid), None)
                    ctx.save_license(data)
                    msg = f"{gid} のライセンスを有効化しました。"
            elif act in ("bl_add", "bl_remove"):
                tab = "blacklist"
                kind = f.get("kind") if f.get("kind") in ("users", "guilds") else None
                tid = _id(f.get("id", ""))
                if kind is None or tid is None:
                    msg = "IDを正しく入力してください。"
                elif act == "bl_add" and kind == "users" and tid in await ctx.get_owner_ids():
                    msg = "オーナー自身は追加できません。"
                else:
                    bl = ctx.load_blacklist()
                    if act == "bl_add":
                        bl[kind] = sorted(set(bl[kind]) | {tid})
                        if kind == "guilds" and (g := ctx.bot.get_guild(tid)):
                            asyncio.create_task(g.leave())
                    else:
                        bl[kind] = [x for x in bl[kind] if x != tid]
                    ctx.save_blacklist(bl)
                    msg = f"{tid} を{'追加' if act == 'bl_add' else '削除'}しました。"
            else:
                msg = "不明な操作です。"
        except Exception as e:
            msg = f"操作に失敗しました（{type(e).__name__}）"
        if msg:
            print(f"ダッシュボード操作（{sess['uid']}）: {act} → {msg}")
        resp = RedirectResponse("/dashboard?" + str(httpx.QueryParams({"tab": tab, "msg": msg})), status_code=303)
        return _secure_headers(resp)

    @app.get("/dashboard/export")
    async def dashboard_export(request: Request, kind: str = "auth"):
        sess = await current(request)
        if sess is None:
            return RedirectResponse("/dashboard/login", status_code=303)
        if kind == "incidents":
            data = ctx.load_incidents()
        elif kind == "logs":
            data = [{"t": t, "level": lv, "line": line} for t, lv, line in ctx.log_buffer]
        else:
            kind, data = "auth", ctx.load_auth_logs()
        body = json.dumps(data, ensure_ascii=False, indent=1)
        resp = Response(body, media_type="application/json")
        resp.headers["Content-Disposition"] = f'attachment; filename="{kind}-{int(time.time())}.json"'
        return _secure_headers(resp)
