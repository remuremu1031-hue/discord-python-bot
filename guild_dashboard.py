"""サーバー管理者向けWebダッシュボード（/guild-dashboard）。

・Discord OAuth2（identify guilds）でログインし、「管理者」または「サーバー管理」権限を持つサーバーを一覧表示
・サーバーを選ぶと、そのサーバーの設定（認証・ログ・トランスクリプト・Nuke対策・サブアカウント検知）を変更できる
・権限は、表示するとき・保存するときの両方で、Botが Discord から取得したメンバー情報で必ず再確認する
  （ブラウザから送られてくる値やログイン時の一覧は信用しない）。権限が無ければ 403。
・「認証ロール・Nuke対策・サブアカウント検知」は、スラッシュコマンドと同じく「管理者」権限が必要
main.py から register(app, ctx) で登録する。このファイルは main.py を import しない。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import secrets
import sys
import time
from urllib.parse import parse_qs

import discord
import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

API = "https://discord.com/api/v10"
COOKIE = "gdash_session"
STATE_COOKIE = "gdash_state"
SESSION_TTL = 6 * 3600
_sessions: dict[str, dict] = {}
_used_states: dict[str, float] = {}
_mass_running: set[int] = set()
TITLE = "霊夢BOT 管理ダッシュボード"
PERM_ADMIN = 0x8
PERM_MANAGE_GUILD = 0x20
# 認証ロールにすると、認証した人が強い権限を得てしまう権限（設定できないようにする）
DANGEROUS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "ban_members",
             "kick_members", "manage_webhooks", "moderate_members", "manage_messages", "mention_everyone")
# Botの招待に使う権限（認証・モデレーション・Nuke対策に必要なもの）
INVITE_PERMS = (0x2 | 0x4 | 0x10 | 0x20 | 0x80 | 0x400 | 0x800 | 0x2000 | 0x4000 | 0x8000 | 0x10000
                | 0x10000000 | 0x20000000 | 0x10000000000)

CSS = """
*{box-sizing:border-box}body{margin:0;background:#0b0f19;color:#e5e7eb;
font:15px/1.65 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
a{color:#93c5fd;text-decoration:none}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;
padding:12px 18px;background:#111827;border-bottom:1px solid #1f2937;position:sticky;top:0;z-index:5}
header h1{margin:0;font-size:17px}header .me{display:flex;gap:10px;align-items:center;color:#9ca3af}
header img{width:28px;height:28px;border-radius:50%}
main{padding:18px;max-width:980px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.card{background:#111827;border:1px solid #1f2937;border-radius:14px;padding:16px}
.g{display:flex;gap:12px;align-items:center}
.g img,.g .ph{width:48px;height:48px;border-radius:14px;background:#1f2937;display:flex;align-items:center;
justify-content:center;font-weight:700;flex:none}
.g .n{font-weight:700}.mut{color:#9ca3af;font-size:13px}
.btn{display:inline-block;background:#5865f2;color:#fff;border:0;border-radius:10px;padding:9px 16px;
font:inherit;cursor:pointer}.btn.ghost{background:#334155}.btn.danger{background:#dc2626}
h2{font-size:17px;margin:22px 0 10px}
label{display:block;margin:12px 0 4px;font-weight:600}
select,input[type=text]{width:100%;background:#0f172a;color:#e5e7eb;border:1px solid #334155;border-radius:10px;
padding:9px 11px;font:inherit}
.check{display:flex;gap:8px;align-items:center;margin:8px 0;font-weight:400}
.check input{width:18px;height:18px}
.flash{padding:10px 14px;border-radius:10px;background:#1e293b;border:1px solid #334155;margin-bottom:14px}
.flash.ng{border-color:#7f1d1d;background:#2a1215}
.lock{opacity:.55}.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;
background:#1f2937;color:#9ca3af}.tag.ok{background:#14532d;color:#86efac}
.hero{max-width:520px;margin:12vh auto;text-align:center}
code{color:#c4b5fd}
"""


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; img-src https://cdn.discordapp.com data:; "
        "form-action 'self' https://discord.com; base-uri 'none'; frame-ancestors 'none'")
    return resp


def register(app, ctx):
    """ctx（main.py が用意）: bot, client_id, client_secret, oauth_redirect_uri(), base_url(), limiter,
    guard_web_strike(), is_user_blacklisted(), is_license_suspended(), load_config(), save_config(),
    get_log_channel_id(), set_log_channel_id(), load_defense_config(), save_defense_config(),
    get_transcript_channel_id(), set_transcript_channel_id(), get_subaccount_action(),
    set_subaccount_action(), auth_url(), send_guild_log(), take_backup()"""

    def state_key() -> bytes:
        return hashlib.sha256(("guild-dashboard-state:" + ctx.client_secret).encode()).digest()

    def sign_state() -> str:
        ts, nonce = str(int(time.time())), secrets.token_urlsafe(12)
        sig = hmac.new(state_key(), f"{ts}.{nonce}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"gdash_{ts}.{nonce}.{sig}"

    def check_state(state: str) -> bool:
        try:
            ts, nonce, sig = state.removeprefix("gdash_").split(".", 2)
            if time.time() - int(ts) > 600:
                return False
            good = hmac.new(state_key(), f"{ts}.{nonce}".encode(), hashlib.sha256).hexdigest()[:32]
            return secrets.compare_digest(sig, good)
        except (ValueError, TypeError):
            return False

    def secure() -> bool:
        return ctx.base_url().startswith("https://")

    def page(body: str, sess: dict | None = None, status: int = 200) -> HTMLResponse:
        me = ""
        if sess:
            av = f'<img src="{_e(sess["avatar"])}" alt="">' if sess.get("avatar") else ""
            me = (f'<div class="me">{av}<span>{_e(sess["name"])}</span>'
                  f'<form method="post" action="/guild-dashboard/logout" style="display:inline">'
                  f'<input type="hidden" name="csrf" value="{_e(sess["csrf"])}"><button class="btn ghost">ログアウト</button>'
                  f'</form></div>')
        doc = (f'<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" '
               f'content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>{TITLE}</title>'
               f'<style>{CSS}</style></head><body><header><h1><a href="/guild-dashboard" style="color:inherit">'
               f'🛡️ {TITLE}</a></h1>{me}</header><main>{body}</main></body></html>')
        return _headers(HTMLResponse(doc, status_code=status))

    def status_card() -> str:
        """稼働状況（サポートサーバーの稼働状況Embedと同じ判定）。詳細なエラー内容は出さない。"""
        try:
            s = ctx.status_info()
        except Exception:
            return ""
        color = {"ok": "#22c55e", "recovered": "#22c55e", "minor": "#eab308", "healing": "#f97316"}.get(s["code"], "#ef4444")
        inc = "".join(f'<div class="mut">・{_e(i)}</div>' for i in s["incidents"][:5])
        return (f'<div class="card" style="margin-bottom:14px;border-left:4px solid {color}">'
                f'<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px">'
                f'<div><div class="mut">📡 霊夢BOT 稼働状況</div><div style="font-size:18px;font-weight:700">{_e(s["text"])}</div></div>'
                f'<div class="mut" style="text-align:right">🤖 Bot: {_e(s["bot"])}　🏓 Ping: {_e(s["ping"])}<br>'
                f'🏠 導入サーバー: {s["guilds"]:,}　🕒 稼働: {_e(s["uptime"])}</div></div>{inc}</div>')

    def error(status: int, title: str, text: str, sess=None) -> HTMLResponse:
        return page(f'<div class="card hero"><h2>{_e(title)}</h2><p class="mut">{_e(text)}</p>'
                    f'<a class="btn ghost" href="/guild-dashboard">サーバー一覧へ</a></div>', sess, status)

    def current(request: Request) -> dict | None:
        tok = request.cookies.get(COOKIE) or ""
        s = _sessions.get(tok)
        if not s or s["exp"] < time.time():
            _sessions.pop(tok, None)
            return None
        if ctx.is_user_blacklisted(s["uid"]):
            _sessions.pop(tok, None)
            return None
        return s

    async def user_guilds(sess: dict) -> list[dict]:
        """ログインした人が所属するサーバー（Discordの一覧。60秒キャッシュ）。一覧表示にだけ使う。"""
        cached = sess.get("guilds")
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{API}/users/@me/guilds", headers={"Authorization": f"Bearer {sess['token']}"})
        if r.status_code == 401:
            raise PermissionError("expired")
        r.raise_for_status()
        data = r.json()
        sess["guilds"] = (time.time(), data)
        return data

    async def authorize(request: Request, sess: dict, gid_raw: str):
        """そのサーバーを操作してよいかを、Botが取得した最新のメンバー情報で確認する。
        -> (guild, member, is_admin) / 権限が無ければ PermissionError"""
        if not (gid_raw or "").isdigit() or not 15 <= len(gid_raw) <= 21:
            raise PermissionError("invalid")
        gid = int(gid_raw)
        guild = ctx.bot.get_guild(gid)
        if guild is None:
            raise LookupError("not_joined")
        if ctx.is_license_suspended(gid):
            raise LookupError("suspended")
        member = guild.get_member(sess["uid"])
        if member is None:
            try:
                member = await guild.fetch_member(sess["uid"])
            except discord.NotFound:
                member = None
        if member is None:
            raise PermissionError("not_member")
        p = member.guild_permissions
        is_owner = member.id == guild.owner_id
        if not (is_owner or p.administrator or p.manage_guild):
            raise PermissionError("no_permission")
        return guild, member, bool(is_owner or p.administrator)

    def forbidden(request: Request, sess, reason: str) -> HTMLResponse:
        ctx.guard_web_strike(request, "guild_forbidden", f"権限のないサーバーの操作（{reason}）")
        return error(403, "このサーバーは操作できません",
                     "あなたは、このサーバーの「管理者」または「サーバー管理」権限を持っていません。", sess)

    # ---------- ログイン ----------
    @app.get("/guild-dashboard/login")
    @ctx.limiter.limit("15/minute")
    async def gd_login(request: Request):
        state = sign_state()
        params = httpx.QueryParams({"client_id": ctx.client_id, "redirect_uri": ctx.oauth_redirect_uri(),
                                    "response_type": "code", "scope": "identify guilds", "state": state,
                                    "prompt": "none"})
        resp = RedirectResponse(f"https://discord.com/oauth2/authorize?{params}", status_code=303)
        resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, secure=secure(), samesite="lax", path="/")
        return _headers(resp)

    async def gd_callback(request: Request, code: str | None, state: str | None):
        """main.py の /callback から呼ばれる（state が gdash_ で始まるとき）。"""
        cookie = request.cookies.get(STATE_COOKIE)
        if not code or not state or not check_state(state):
            return error(400, "ログインに失敗しました", "ログインの有効期限（10分）が切れたか、無効なリクエストです。")
        if not cookie or not secrets.compare_digest(cookie, state):
            return error(400, "ログインに失敗しました",
                         "ブラウザのCookieを確認できませんでした。ログインを始めたのと同じブラウザで開き直してください。")
        if state in _used_states:
            return error(400, "ログインに失敗しました", "このログインはすでに使われています。")
        _used_states[state] = time.time()
        for k in [k for k, v in _used_states.items() if time.time() - v > 900]:
            _used_states.pop(k, None)
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{API}/oauth2/token", data={
                "client_id": ctx.client_id, "client_secret": ctx.client_secret, "grant_type": "authorization_code",
                "code": code, "redirect_uri": ctx.oauth_redirect_uri()},
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            if r.status_code != 200:
                return error(400, "ログインに失敗しました", "Discordとの通信に失敗しました。もう一度お試しください。")
            tok = r.json()
            if "guilds" not in (tok.get("scope") or ""):
                return error(400, "ログインに失敗しました", "サーバー一覧の取得が許可されませんでした。")
            u = await c.get(f"{API}/users/@me", headers={"Authorization": f"Bearer {tok['access_token']}"})
        if u.status_code != 200:
            return error(400, "ログインに失敗しました", "ユーザー情報を取得できませんでした。")
        user = u.json()
        uid = int(user["id"])
        if ctx.is_user_blacklisted(uid):
            return error(403, "利用できません", "このアカウントは、このBotの利用が制限されています。")
        sid = secrets.token_urlsafe(32)
        for k in [k for k, v in _sessions.items() if v["exp"] < time.time()]:
            _sessions.pop(k, None)
        _sessions[sid] = {"uid": uid, "name": user.get("global_name") or user.get("username"),
                          "avatar": (f"https://cdn.discordapp.com/avatars/{uid}/{user['avatar']}.png?size=64"
                                     if user.get("avatar") else ""),
                          "token": tok["access_token"], "exp": time.time() + min(SESSION_TTL, int(tok.get("expires_in", SESSION_TTL))),
                          "csrf": secrets.token_urlsafe(24)}
        resp = RedirectResponse("/guild-dashboard", status_code=303)
        resp.set_cookie(COOKIE, sid, max_age=SESSION_TTL, httponly=True, secure=secure(), samesite="lax",
                        path="/guild-dashboard")
        resp.delete_cookie(STATE_COOKIE, path="/")
        return _headers(resp)

    async def read_form(request: Request) -> dict[str, str]:
        body = (await request.body())[:10000].decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(body).items()}

    @app.post("/guild-dashboard/logout")
    async def gd_logout(request: Request):
        sess = current(request)
        f = await read_form(request)
        if sess and secrets.compare_digest(f.get("csrf", ""), sess["csrf"]):
            _sessions.pop(request.cookies.get(COOKIE, ""), None)
        resp = RedirectResponse("/guild-dashboard", status_code=303)
        resp.delete_cookie(COOKIE, path="/guild-dashboard")
        return _headers(resp)

    # ---------- サーバー一覧・設定画面 ----------
    @app.get("/guild-dashboard")
    async def gd_home(request: Request, guild: str = "", msg: str = "", ng: str = "", tab: str = "auth", q: str = ""):
        sess = current(request)
        if sess is None:
            return page(status_card() + '<div class="card hero" style="margin-top:6vh"><h2>サーバー管理者向けダッシュボード</h2>'
                        '<p class="mut">Discordでログインすると、あなたが「管理者」または「サーバー管理」権限を持つ'
                        'サーバーの、霊夢BOTの設定を変更できます。</p>'
                        '<a class="btn" href="/guild-dashboard/login">Discordでログイン</a>'
                        '<p class="mut" style="margin-top:14px">取得する情報: ユーザーID・ユーザー名・参加サーバーの一覧'
                        '（<a href="/privacy">プライバシーポリシー</a>）</p></div>')
        if guild:
            return await settings_page(request, sess, guild, msg[:300], ng == "1", tab, q[:21])
        try:
            gs = await user_guilds(sess)
        except PermissionError:
            _sessions.pop(request.cookies.get(COOKIE, ""), None)
            return RedirectResponse("/guild-dashboard", status_code=303)
        except Exception:
            return error(502, "サーバー一覧を取得できません", "時間をおいて、もう一度お試しください。", sess)
        cards = []
        for g in gs:
            perms = int(g.get("permissions") or 0)
            if not (g.get("owner") or perms & PERM_ADMIN or perms & PERM_MANAGE_GUILD):
                continue
            icon = (f'<img src="https://cdn.discordapp.com/icons/{_e(g["id"])}/{_e(g["icon"])}.png?size=128" alt="">'
                    if g.get("icon") else f'<div class="ph">{_e((g.get("name") or "?")[:1])}</div>')
            joined = ctx.bot.get_guild(int(g["id"])) is not None
            if joined:
                act = f'<a class="btn" href="/guild-dashboard?guild={_e(g["id"])}">設定する</a>'
            else:
                q = httpx.QueryParams({"client_id": ctx.client_id, "scope": "bot applications.commands",
                                       "permissions": str(INVITE_PERMS), "guild_id": g["id"],
                                       "disable_guild_select": "true"})
                act = f'<a class="btn ghost" href="https://discord.com/oauth2/authorize?{q}" rel="noopener">Botを招待</a>'
            cards.append(f'<div class="card"><div class="g">{icon}<div><div class="n">{_e(g.get("name"))}</div>'
                         f'<div class="mut">{"導入済み" if joined else "未導入"}</div></div></div>'
                         f'<div style="margin-top:12px">{act}</div></div>')
        body = (status_card() + '<h2>サーバーを選択</h2><p class="mut">「管理者」または「サーバー管理」権限を持つサーバーだけが表示されます。</p>'
                + (f'<div class="grid">{"".join(cards)}</div>' if cards else
                   '<div class="card">管理できるサーバーがありません。</div>'))
        return page(body, sess)

    # ================= 設定画面（タブ） =================
    TABS = [("auth", "🔐 認証"), ("logs", "📝 ログ・通知"), ("security", "🛡️ セキュリティ"),
            ("moderation", "🔨 モデレーション"), ("automod", "🧹 自動モデ・ログ"), ("tickets", "🎫 チケット"),
            ("roles", "🎭 ロール"), ("sticky", "📌 案内の固定"), ("alerts", "🌦️ 天気・災害"), ("levels", "📈 レベル")]

    # ---- Redis（cogs が使うキーをそのまま読み書きする） ----
    async def kv_get(k):
        try:
            return await ctx.bot.kv("get", k)
        except Exception:
            return None

    async def kv_json(k):
        raw = await kv_get(k)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    async def kv_set_json(k, v):
        try:
            await ctx.bot.kv("set", k, json.dumps(v, ensure_ascii=False))
        except Exception:
            raise ValueError("保存できませんでした（データベースが未設定か一時的な障害）")

    async def kv_list(k):
        try:
            rows = await ctx.bot.kv("lrange", k, 0, -1)
        except Exception:
            return []
        out = []
        for r in rows or []:
            try:
                out.append(json.loads(r))
            except ValueError:
                pass
        return out

    def b_cog(name):
        return ctx.bot.get_cog(name)

    async def notify_dm(user, title, text):
        try:
            await user.send(embed=discord.Embed(title=title, description=text[:2000], color=0xF59E0B))
        except Exception:
            pass
    INT_ORDER = ["1", "2", "3", "4", "5-", "5+", "6-"]
    INT_NAME = {"5-": "5弱", "5+": "5強", "6-": "6弱"}

    def role_options(guild, member, current_id, *, assignable=True, none_label="（選択してください）"):
        opts = [f'<option value="">{_e(none_label)}</option>']
        for r in sorted(guild.roles, key=lambda r: -r.position):
            if r.is_default() or r.managed:
                continue
            if assignable:
                ok = r < guild.me.top_role and (member.id == guild.owner_id or r < member.top_role) \
                    and not any(getattr(r.permissions, p) for p in DANGEROUS)
                if not ok and r.id != current_id:
                    continue
            opts.append(f'<option value="{r.id}"{" selected" if r.id == current_id else ""}>@{_e(r.name)}</option>')
        return "".join(opts)

    def channel_options(guild, current_id, *, none_label="（選択してください）", voice=False):
        opts = [f'<option value="">{_e(none_label)}</option>']
        chans = list(guild.text_channels) + (list(guild.voice_channels) + list(guild.stage_channels) if voice else [])
        for ch in sorted(chans, key=lambda c: (c.category.position if c.category else -1, c.position)):
            if isinstance(ch, discord.TextChannel):
                p = ch.permissions_for(guild.me)
                if not (p.view_channel and p.send_messages and p.embed_links) and ch.id != current_id:
                    continue
            cat = f"{ch.category.name} / " if ch.category else ""
            mark = "🔊 " if not isinstance(ch, discord.TextChannel) else "#"
            opts.append(f'<option value="{ch.id}"{" selected" if ch.id == current_id else ""}>{mark}{_e(cat + ch.name)}</option>')
        return "".join(opts)

    def ctx_member_perms(member, guild):
        p = member.guild_permissions
        owner = member.id == guild.owner_id
        return {"admin": owner or p.administrator, "manage_guild": owner or p.administrator or p.manage_guild,
                "manage_messages": owner or p.administrator or p.manage_messages,
                "moderate": owner or p.administrator or p.moderate_members}

    async def settings_page(request, sess, gid_raw, msg, is_ng, tab="auth", f_q=""):
        try:
            guild, member, _ = await authorize(request, sess, gid_raw)
        except PermissionError as e:
            return forbidden(request, sess, str(e))
        except LookupError as e:
            if str(e) == "suspended":
                return error(403, "このサーバーでは利用できません", "このサーバーでは、霊夢BOTの機能が停止されています。", sess)
            return error(404, "Botが参加していません", "このサーバーには霊夢BOTが導入されていません。", sess)
        perms = ctx_member_perms(member, guild)
        tab = tab if tab in dict(TABS) else "auth"
        gid = guild.id

        def form(op, inner, button, cls="btn", need="manage_guild", confirm=""):
            dis = "" if perms[need] else " disabled"
            return (f'<form method="post" action="/guild-dashboard/save" style="margin:0">'
                    f'<input type="hidden" name="csrf" value="{_e(sess["csrf"])}">'
                    f'<input type="hidden" name="guild" value="{gid}"><input type="hidden" name="tab" value="{tab}">'
                    f'<input type="hidden" name="op" value="{op}">{inner}'
                    f'<div style="margin-top:12px"><button class="{cls}"{dis}>{_e(button)}</button></div></form>')

        def card(title, body, need="manage_guild", note=""):
            locked = not perms[need]
            lock_note = {"admin": "「管理者」権限", "manage_guild": "「サーバー管理」権限",
                         "manage_messages": "「メッセージの管理」権限", "moderate": "「メンバーをタイムアウト」権限"}[need]
            return (f'<div class="card{" lock" if locked else ""}" style="margin-bottom:12px"><h2 style="margin-top:0">{title}</h2>'
                    + (f'<div class="flash">🔒 この設定の変更には{lock_note}が必要です</div>' if locked else "")
                    + (f'<p class="mut">{note}</p>' if note else "") + body + "</div>")

        def sel(name, options):
            return f'<select name="{name}">{options}</select>'

        def lbl(text):
            return f"<label>{_e(text)}</label>"

        def chk(name, val, text):
            return f'<label class="check"><input type="checkbox" name="{name}" value="1"{" checked" if val else ""}> {_e(text)}</label>'

        def opt(value, label, cur):
            return f'<option value="{_e(value)}"{" selected" if str(cur) == str(value) else ""}>{_e(label)}</option>'

        html_parts = []
        if tab == "auth":
            cfg = ctx.load_config().get(str(gid)) or {}
            sub = ctx.get_subaccount_action(gid)
            html_parts.append(card("🔐 認証の設定（/setup・/subaccount）", form("auth_save",
                lbl("認証後に付与するロール") + sel("role_id", role_options(guild, member, cfg.get("role_id")))
                + '<div class="mut">Botより下・あなたより下で、強い権限を持たないロールだけ選べます</div>'
                + lbl("認証ログを送るチャンネル") + sel("auth_log", channel_options(guild, cfg.get("log_channel_id")))
                + lbl("同じIPからの別アカウント（サブアカウント）を検知したとき")
                + sel("subaccount", opt("block", "認証を拒否する", sub) + opt("log", "認証は通し、ログに記録するだけ", sub)),
                "保存する", need="admin"), need="admin"))
            if cfg:
                html_parts.append(card("📨 認証パネル（/panel・/authlink）", f'<p class="mut">認証ページ: <code>{_e(ctx.auth_url(gid))}</code></p>'
                    + form("panel_send", lbl("認証パネルを送信するチャンネル") + sel("channel", channel_options(guild, None)),
                           "認証パネルを送信", need="admin"), need="admin"))
        elif tab == "logs":
            log_id = await ctx.get_log_channel_id(gid)
            inv = ctx.bot.store_load("invite_log_config").get(str(gid))
            bc = ctx.load_broadcast().get(str(gid))
            html_parts.append(card("📝 ログ・通知の送信先", form("logs_save",
                lbl("セキュリティ・GBANなどのログ（/set-log-channel）") + sel("log_channel", channel_options(guild, log_id, none_label="（変更しない）"))
                + lbl("チケットのトランスクリプト（/transcript-channel）") + sel("transcript", channel_options(guild, ctx.get_transcript_channel_id(gid), none_label="（設定しない）"))
                + lbl("招待リンクの作成・参加の記録（/invite-log）") + sel("invite_log", channel_options(guild, int(inv) if inv else None, none_label="（ログチャンネルと同じ）"))
                + lbl("霊夢BOTからのお知らせ（/broadcast-channel・管理者のみ）") + sel("broadcast", channel_options(guild, bc, none_label="（自動で選ぶ）")),
                "保存する")))
        elif tab == "security":
            d = ctx.load_defense_config().get(str(gid), {})
            act = d.get("action", "ban")
            html_parts.append(card("🛡️ Nuke・Raid対策（/nuke-defense）", form("nuke_save",
                chk("nuke_defense", d.get("nuke_defense"), "Nuke（大量削除・大量BAN等）を自動検知する")
                + lbl("Nukeの実行者への自動対処") + sel("action", opt("ban", "BAN", act) + opt("kick", "キック", act) + opt("strip", "権限剥奪（ロールを全て外す）", act))
                + chk("auto_restore", d.get("auto_restore", True), "消されたチャンネル・ロールを自動で復元する")
                + chk("raid_protection", d.get("raid_protection"), "大量参加（Raid）を自動検知する")
                + chk("invite_block", d.get("invite_block"), "招待リンクを自動でブロックする"),
                "保存する", need="admin"), need="admin"))
            wl = [int(x) for x in d.get("whitelist", [])]
            wl_html = "".join(f'<div class="mut">・<code>{u}</code> {_e(getattr(guild.get_member(u), "display_name", ""))}</div>' for u in wl) or '<div class="mut">登録なし</div>'
            html_parts.append(card("🤝 信頼済みユーザー（/nuke-whitelist）", wl_html + form("wl_edit",
                lbl("ユーザーID") + '<input type="text" name="uid" inputmode="numeric" maxlength="21" placeholder="例: 123456789012345678">'
                + lbl("操作") + sel("mode", opt("add", "追加", "add") + opt("remove", "削除", "")), "実行", need="admin"), need="admin",
                note="Nuke検知の対象外にするユーザー・BOTです。"))
            sbl = ctx.load_server_blacklist().get(str(gid), [])
            sbl_html = "".join(f'<div class="mut">・<code>{u}</code></div>' for u in sbl[:50]) or '<div class="mut">登録なし</div>'
            html_parts.append(card("⛔ このサーバーのブラックリスト（/server-blacklist）", sbl_html + form("sbl_edit",
                lbl("ユーザーID") + '<input type="text" name="uid" inputmode="numeric" maxlength="21">'
                + lbl("操作") + sel("mode", opt("add", "追加（認証できなくする）", "add") + opt("remove", "削除", "")), "実行", need="admin"), need="admin"))
            backups = list(reversed(ctx.load_backups().get(str(gid), [])))
            blist = "".join(opt(i, f"{b.get('time')}{'（自動）' if b.get('auto') else ''} ロール{len(b.get('roles', []))}・チャンネル{len(b.get('channels', []))}", "") for i, b in enumerate(backups, 1))
            html_parts.append(card("💾 バックアップ（/backup）",
                form("backup_create", '<p class="mut">ロール・チャンネル構成を保存します（最新10件）。</p>', "バックアップを作成", need="admin")
                + (form("backup_restore", lbl("復元するバックアップ") + sel("number", blist)
                        + '<div class="mut">足りないロール・チャンネルだけを作り直します（既存のものは変更しません）。</div>',
                        "このバックアップから復元", cls="btn danger", need="admin") if backups else ""), need="admin"))
        elif tab == "sticky":
            cog = ctx.bot.get_cog("Sticky")
            if cog is None:
                html_parts.append('<div class="card">メッセージキープ機能（sticky.py）が読み込まれていません。</div>')
            else:
                conf = await cog._conf(gid)
                rows = "".join(
                    f'<div class="card" style="margin:8px 0;padding:10px"><b>#{_e(getattr(guild.get_channel(int(c)), "name", c))}</b>'
                    f'<div class="mut">{_e((v.get("title") or "") + " " + (v.get("content") or "")[:80])}</div>'
                    + form("sticky_remove", f'<input type="hidden" name="channel" value="{_e(c)}">', "解除", cls="btn ghost", need="manage_messages")
                    + "</div>" for c, v in conf.items())
                html_parts.append(card("📌 案内メッセージの固定（/sticky）", (rows or '<div class="mut">設定はありません</div>') + form("sticky_set",
                    lbl("チャンネル") + sel("channel", channel_options(guild, None))
                    + lbl("タイトル（任意・入れると埋め込み表示）") + '<input type="text" name="title" maxlength="200">'
                    + lbl("本文") + '<textarea name="text" maxlength="1800" rows="5" style="width:100%;background:#0f172a;color:#e5e7eb;border:1px solid #334155;border-radius:10px;padding:9px;font:inherit"></textarea>'
                    + lbl("リンクボタンの文字（任意）") + '<input type="text" name="button_label" maxlength="80">'
                    + lbl("リンクボタンのURL（任意・https://）") + '<input type="text" name="button_url" maxlength="300">',
                    "固定する（同じチャンネルなら上書き）", need="manage_messages"), need="manage_messages",
                    note="新しい投稿があるたびに、古い案内を消して一番下に貼り直します。"))
        elif tab == "alerts":
            w = ctx.bot.store_load("weather_config").get(str(gid), {})
            dz = ctx.bot.store_load("disaster_config").get(str(gid), {})
            html_parts.append(card("🗾 天気予報の自動投稿（/weather）", form("weather_save",
                lbl("投稿先") + sel("channel", channel_options(guild, w.get("channel"), none_label="（停止する）"))
                + lbl("投稿時刻（日本時間・カンマ区切り）") + f'<input type="text" name="times" maxlength="40" value="{_e(",".join(w.get("times", ["07:00"])))}">',
                "保存する")))
            wl = dz.get("weather", "special")
            mi = dz.get("min_int", "3")
            html_parts.append(card("🌊 津波・火山・気象警報（/disaster set）", form("disaster_save",
                lbl("通知先") + sel("channel", channel_options(guild, dz.get("channel"), none_label="（停止する）"))
                + lbl("地震の最小震度（地震を別チャンネルに分けていないとき）") + sel("min_int", "".join(opt(v, f"震度{INT_NAME.get(v, v)}以上", mi) for v in INT_ORDER))
                + lbl("気象警報の通知レベル") + sel("weather", opt("special", "特別警報・危険警報のみ", wl) + opt("warning", "警報以上", wl) + opt("advisory", "注意報以上（すべて）", wl) + opt("off", "通知しない", wl))
                + lbl("地域で絞り込む（任意・例: 東京）") + f'<input type="text" name="area" maxlength="20" value="{_e(dz.get("area", ""))}">'
                + lbl("重大な情報でメンションするロール") + sel("role", role_options(guild, member, dz.get("role"), assignable=False, none_label="（メンションしない）")),
                "保存する")))
            qm = dz.get("quake_min_int", "3")
            html_parts.append(card("🌏 地震情報を別チャンネルに分ける（/disaster quake）", form("quake_save",
                lbl("地震情報の通知先") + sel("channel", channel_options(guild, dz.get("quake_channel"), none_label="（分けない）"))
                + lbl("最小震度") + sel("min_int", "".join(opt(v, f"震度{INT_NAME.get(v, v)}以上", qm) for v in INT_ORDER))
                + lbl("震度5弱以上でメンションするロール") + sel("role", role_options(guild, member, dz.get("quake_role"), assignable=False, none_label="（メンションしない）")),
                "保存する")))
            html_parts.append(card("🚨 緊急地震速報（/disaster eew）", form("eew_save",
                lbl("通知先") + sel("channel", channel_options(guild, dz.get("eew_channel"), none_label="（通知しない）"))
                + lbl("発表時にメンションするロール") + sel("role", role_options(guild, member, dz.get("eew_role"), assignable=False, none_label="（メンションしない）")),
                "保存する"), note="予想最大震度5弱以上で発表される「警報」を通知します。"))
        elif tab == "tickets":
            tc = await kv_json(f"ticket:config:{gid}") or {}
            cats = '<option value="">（カテゴリーなし）</option>' + "".join(
                f'<option value="{c.id}"{" selected" if c.id == tc.get("category_id") else ""}>{_e(c.name)}</option>' for c in guild.categories)
            html_parts.append(card("🎫 チケット（/ticket setup）", form("ticket_setup",
                lbl("チケットを見られる対応ロール") + sel("role", role_options(guild, member, tc.get("role_id"), assignable=False))
                + lbl("チケットを作るカテゴリー") + sel("category", cats), "保存する", need="admin"), need="admin"))
            html_parts.append(card("📨 チケットのパネル（/ticket panel）", form("ticket_panel",
                lbl("送信先") + sel("channel", channel_options(guild, None)), "パネルを送信", need="admin"), need="admin",
                note="トランスクリプトの送信先は「ログ・通知」タブで設定できます。"))
        elif tab == "moderation":
            html_parts.append(card("🔨 メンバーへの処分（/timeout・/untimeout・/kick・/ban・/unban）", form("mod_action",
                lbl("対象のユーザーID") + '<input type="text" name="uid" inputmode="numeric" maxlength="21">'
                + lbl("操作") + sel("action", opt("timeout", "タイムアウト", "timeout") + opt("untimeout", "タイムアウト解除", "")
                                   + opt("kick", "キック", "") + opt("ban", "BAN", "") + opt("unban", "BAN解除", ""))
                + lbl("タイムアウトの分数（1〜40320）") + '<input type="text" name="minutes" maxlength="5" value="10">'
                + lbl("BANのとき、さかのぼって削除するメッセージの日数（0〜7）") + '<input type="text" name="days" maxlength="1" value="0">'
                + lbl("理由") + '<input type="text" name="reason" maxlength="300" placeholder="理由なし">',
                "実行する", cls="btn danger", need="admin"), need="admin",
                note="あなたより上のロールの人・サーバー所有者・自分自身は対象にできません。対象にはDMで通知されます。"))
            wq = (f_q or "").strip()
            warn_html = ""
            if wq.isdigit():
                rows = await kv_list(f"warn:{gid}:{wq}")
                warn_html = f'<p><b><code>{_e(wq)}</code> の警告: {len(rows)} 回</b></p>' + "".join(
                    f'<div class="mut">・{_e(r.get("at", ""))} {_e(r.get("reason", ""))}（実行: <code>{_e(r.get("by"))}</code>）</div>'
                    for r in reversed(rows[-15:]))
            html_parts.append(card("⚠️ 警告（/warn・/warnings・/clearwarnings）",
                f'<form method="get" action="/guild-dashboard"><input type="hidden" name="guild" value="{gid}">'
                f'<input type="hidden" name="tab" value="moderation"><label>警告を確認するユーザーID</label>'
                f'<input type="text" name="q" inputmode="numeric" maxlength="21" value="{_e(wq)}">'
                f'<div style="margin-top:10px"><button class="btn ghost">確認する</button></div></form>' + warn_html
                + form("warn_add", lbl("警告するユーザーID") + '<input type="text" name="uid" inputmode="numeric" maxlength="21">'
                       + lbl("理由") + '<input type="text" name="reason" maxlength="300">', "警告を付与", need="moderate")
                + form("warn_clear", lbl("警告をすべて削除するユーザーID（管理者のみ）") + '<input type="text" name="uid" inputmode="numeric" maxlength="21">',
                       "警告を削除", cls="btn ghost", need="admin"), need="moderate"))
        elif tab == "automod":
            am = {"spam": False, "invite": False, **(await kv_json(f"automod:{gid}") or {})}
            ev = await kv_get(f"eventlog:{gid}")
            html_parts.append(card("🧹 自動モデレーション（/automod）", form("automod_save",
                chk("spam", am["spam"], "連続投稿スパムを自動で削除し、5分タイムアウトする")
                + chk("invite", am["invite"], "招待リンクを自動で削除する"), "保存する", need="admin"), need="admin",
                note="管理者・「メッセージの管理」権限のメンバーは対象外です。"))
            html_parts.append(card("📋 イベントログ（/eventlog）", form("eventlog_save",
                lbl("メッセージの削除・編集、入退室、VCの移動を記録するチャンネル")
                + sel("channel", channel_options(guild, int(ev) if ev and str(ev).isdigit() else None, none_label="（記録しない）")),
                "保存する", need="admin"), need="admin"))
        elif tab == "roles":
            rr = b_cog("ReactionRoles")
            if rr is None:
                html_parts.append('<div class="card">リアクションロール機能（reaction_roles.py）が読み込まれていません。</div>')
            else:
                menus = await rr._get_menus(gid)
                mopts = "".join(f'<option value="{_e(pid)}">{_e(p.get("title"))}（{len(p.get("roles", []))}個）</option>' for pid, p in menus.items())
                mlist = "".join(
                    f'<div class="mut">・<b>{_e(p.get("title"))}</b>（<code>{_e(pid)}</code>）: '
                    + "、".join("@" + _e(getattr(guild.get_role(r["id"]), "name", "?")) for r in p.get("roles", []))
                    + (f' → 送信済み #{_e(getattr(guild.get_channel(p.get("c") or 0), "name", "?"))}' if p.get("m") else " （未送信）") + "</div>"
                    for pid, p in menus.items()) or '<div class="mut">ロールメニューはありません</div>'
                html_parts.append(card("🎭 ロールメニュー（複数選択・/reactionrole menu-*）", mlist
                    + form("rr_create", lbl("新しいメニューのタイトル") + '<input type="text" name="title" maxlength="100">'
                           + lbl("説明文（任意）") + '<input type="text" name="description" maxlength="1500">', "メニューを作成", need="admin")
                    + (form("rr_edit", lbl("メニュー") + sel("menu", mopts)
                            + lbl("ロール") + sel("role", role_options(guild, member, None))
                            + lbl("絵文字（任意）") + '<input type="text" name="emoji" maxlength="60">'
                            + lbl("説明（任意）") + '<input type="text" name="desc" maxlength="90">'
                            + lbl("操作") + sel("mode", opt("add", "ロールを追加", "add") + opt("remove", "ロールを外す", "")),
                            "実行", need="admin")
                       + form("rr_send", lbl("メニュー") + sel("menu", mopts) + lbl("送信先") + sel("channel", channel_options(guild, None)),
                              "メニューを送信", need="admin")
                       + form("rr_delete", lbl("削除するメニュー") + sel("menu", mopts), "メニューを削除", cls="btn danger", need="admin")
                       if menus else ""), need="admin"))
                emap = await rr._get_map(gid)
                elist = "".join(f'<div class="mut">・{_e(k.split(":", 1)[1] if not k.split(":", 1)[1].isdigit() else "カスタム絵文字")} → '
                                f'@{_e(getattr(guild.get_role(v.get("r", 0)), "name", "?"))}（メッセージ <code>{_e(k.split(":", 1)[0])}</code>）</div>'
                                for k, v in emap.items()) or '<div class="mut">設定はありません</div>'
                html_parts.append(card("😀 絵文字リアクション方式（/reactionrole add・remove）", elist
                    + '<p class="mut">追加・削除は、メッセージに絵文字を付ける操作が必要なため、スラッシュコマンドで行ってください。</p>', need="admin"))
            html_parts.append(card("🏷️ 一斉ロール付与（/mass_role）", form("mass_role",
                lbl("全員に付与するロール") + sel("role", role_options(guild, member, None))
                + chk("confirm", False, "メンバー全員（Bot・所持済みの人を除く）に付与することを確認しました"),
                "一斉付与を開始", cls="btn danger", need="admin"), need="admin",
                note="1人あたり約1.2秒かかります。完了するとログチャンネルに記録されます。"))
        elif tab == "levels":
            lc = ctx.bot.store_load("level_config").get(str(gid), {})
            ann = str(lc.get("announce", "same"))
            mode = ann if ann in ("same", "off") else "channel"
            html_parts.append(card("📈 レベル機能（/level settings）", form("level_save",
                chk("enabled", lc.get("enabled", True), "レベル機能を有効にする")
                + lbl("レベルアップの通知先") + sel("announce", opt("same", "発言したチャンネル", mode) + opt("channel", "指定チャンネル", mode) + opt("off", "通知しない", mode))
                + lbl("指定チャンネル（上で「指定チャンネル」を選んだとき）") + sel("announce_channel", channel_options(guild, int(ann) if ann.isdigit() else None))
                + lbl("XPの倍率（0.1〜5.0）") + f'<input type="text" name="multiplier" maxlength="4" value="{_e(lc.get("multiplier", 1.0))}">'
                + chk("stack", lc.get("stack", True), "下位のランクロールも残す（オフで最上位だけ）"),
                "保存する")))
            roles = lc.get("roles", {})
            rr = "".join(f'<div class="mut">・レベル {lv} → @{_e(getattr(guild.get_role(int(rid)), "name", "（削除済み）"))}</div>'
                         for lv, rid in sorted(roles.items(), key=lambda kv: int(kv[0]))) or '<div class="mut">未設定</div>'
            html_parts.append(card("🏅 ランクロール（/level role）", rr + form("level_role",
                lbl("到達レベル（1〜500）") + '<input type="text" name="level" maxlength="3" inputmode="numeric">'
                + lbl("付与するロール（削除のときは空欄）") + sel("role", role_options(guild, member, None))
                + lbl("操作") + sel("mode", opt("add", "追加・変更", "add") + opt("remove", "削除", "")), "実行")))
            nx = lc.get("no_xp_channels", [])
            nxh = "".join(f'<div class="mut">・{_e(getattr(guild.get_channel(c), "name", c))}</div>' for c in nx) or '<div class="mut">なし</div>'
            html_parts.append(card("🚫 XPが貯まらないチャンネル（/level no-xp）", nxh + form("level_noxp",
                lbl("チャンネル（VC含む）") + sel("channel", channel_options(guild, None, voice=True))
                + lbl("操作") + sel("mode", opt("add", "追加", "add") + opt("remove", "削除", "")), "実行")))

        nav = "".join(f'<a class="btn {"" if k == tab else "ghost"}" style="padding:6px 12px" '
                      f'href="/guild-dashboard?{httpx.QueryParams({"guild": str(gid), "tab": k})}">{_e(v)}</a>' for k, v in TABS)
        icon = (f'<img src="{_e(guild.icon.replace(size=128).url)}" alt="">' if guild.icon else f'<div class="ph">{_e(guild.name[:1])}</div>')
        flash = f'<div class="flash{" ng" if is_ng else ""}">{_e(msg)}</div>' if msg else ""
        role_text = "管理者" if perms["admin"] else "サーバー管理"
        body = (status_card() + f'<div class="g" style="margin-bottom:12px">{icon}<div><div class="n" style="font-size:19px">{_e(guild.name)}</div>'
                f'<div class="mut">あなたの権限: {role_text} ・ <a href="/guild-dashboard">サーバー一覧に戻る</a></div></div></div>'
                f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">{nav}</div>{flash}' + "".join(html_parts)
)
        return page(body, sess)

    # ================= 保存 =================
    OP_NEED = {"auth_save": "admin", "panel_send": "admin", "logs_save": "manage_guild", "nuke_save": "admin",
               "wl_edit": "admin", "sbl_edit": "admin", "backup_create": "admin", "backup_restore": "admin",
               "sticky_set": "manage_messages", "sticky_remove": "manage_messages", "weather_save": "manage_guild",
               "disaster_save": "manage_guild", "quake_save": "manage_guild", "eew_save": "manage_guild",
               "level_save": "manage_guild", "level_role": "manage_guild", "level_noxp": "manage_guild",
               "ticket_setup": "admin", "ticket_panel": "admin", "mod_action": "admin", "warn_add": "moderate",
               "warn_clear": "admin", "automod_save": "admin", "eventlog_save": "admin", "rr_create": "admin",
               "rr_edit": "admin", "rr_send": "admin", "rr_delete": "admin", "mass_role": "admin"}

    @app.post("/guild-dashboard/save")
    @ctx.limiter.limit("30/minute")
    async def gd_save(request: Request):
        sess = current(request)
        if sess is None:
            return RedirectResponse("/guild-dashboard", status_code=303)
        f = await read_form(request)
        if not secrets.compare_digest(f.get("csrf", ""), sess["csrf"]):
            ctx.guard_web_strike(request, "bad_dashboard_csrf")
            return error(400, "保存できません", "ページの有効期限が切れました。開き直してください。", sess)
        try:  # 保存の直前にも、権限を必ず確認し直す
            guild, member, _ = await authorize(request, sess, f.get("guild", ""))
        except PermissionError as e:
            return forbidden(request, sess, str(e))
        except LookupError:
            return error(404, "保存できません", "このサーバーでは設定を変更できません。", sess)
        op = f.get("op", "")
        tab = f.get("tab") if f.get("tab") in dict(TABS) else "auth"
        perms = ctx_member_perms(member, guild)
        need = OP_NEED.get(op)
        if need is None:
            ctx.guard_web_strike(request, "invalid_input", f"不明な操作 {op[:20]}")
            return error(400, "保存できません", "不明な操作です。", sess)
        if not perms[need]:  # 画面ではボタンを無効にしている ＝ 改ざんされたリクエスト（forbidden で加点）
            return forbidden(request, sess, f"{op} を権限なしで実行しようとした")
        gid = guild.id
        try:
            done = await apply_op(op, f, guild, member, perms)
            ok = True
        except ValueError as e:
            done, ok = str(e), False
        if ok and done:
            e = discord.Embed(title="🛠️ ダッシュボードで設定が変更されました", color=0x5865F2, description=done[:2000])
            e.add_field(name="変更した人", value=f"<@{member.id}>（`{member}` / `{member.id}`）")
            try:
                await ctx.send_guild_log(guild, e)
            except Exception:
                pass
        msg = ("✅ " + done) if ok else ("⚠️ " + done)
        q = httpx.QueryParams({"guild": str(gid), "tab": tab, "msg": msg[:280], **({} if ok else {"ng": "1"})})
        return _headers(RedirectResponse(f"/guild-dashboard?{q}", status_code=303))

    def pick_channel(guild, raw, *, attach=False, required=False, voice=False):
        if not raw:
            if required:
                raise ValueError("チャンネルを選んでください")
            return None
        if not raw.isdigit():
            raise ValueError("チャンネルの指定が正しくありません")
        ch = guild.get_channel(int(raw))
        if voice and isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            return ch
        if not isinstance(ch, discord.TextChannel):
            raise ValueError("そのチャンネルはこのサーバーのテキストチャンネルではありません")
        p = ch.permissions_for(guild.me)
        if not (p.view_channel and p.send_messages and p.embed_links and (p.attach_files or not attach)):
            raise ValueError(f"#{ch.name} でBotにメッセージを送る権限がありません")
        return ch

    def pick_role(guild, member, raw, *, assignable=True, required=False):
        if not raw:
            if required:
                raise ValueError("ロールを選んでください")
            return None
        if not raw.isdigit():
            raise ValueError("ロールの指定が正しくありません")
        r = guild.get_role(int(raw))
        if r is None or r.is_default() or r.managed:
            raise ValueError("そのロールは使えません")
        if assignable:
            if r >= guild.me.top_role:
                raise ValueError("Botのロールより上のロールは使えません")
            if member.id != guild.owner_id and r >= member.top_role:
                raise ValueError("あなたのロールより上のロールは設定できません")
            if any(getattr(r.permissions, p) for p in DANGEROUS):
                raise ValueError("強い権限（管理者・BAN・ロール管理など）を持つロールは使えません")
        return r

    def pick_uid(raw):
        raw = (raw or "").strip()
        if not (raw.isdigit() and 15 <= len(raw) <= 21):
            raise ValueError("ユーザーIDは15〜21桁の数字で入力してください")
        return int(raw)

    async def apply_op(op, f, guild, member, perms) -> str:
        gid, b = guild.id, ctx.bot
        if op == "auth_save":
            role = pick_role(guild, member, f.get("role_id"), required=True)
            ch = pick_channel(guild, f.get("auth_log"), required=True)
            cfg = ctx.load_config()
            cfg[str(gid)] = {**(cfg.get(str(gid)) or {}), "role_id": role.id, "log_channel_id": ch.id}
            ctx.save_config(cfg)
            if f.get("subaccount") in ("block", "log"):
                ctx.set_subaccount_action(gid, f["subaccount"])
            return f"認証ロール @{role.name} ／ 認証ログ #{ch.name} ／ サブアカウント検知: {'拒否' if f.get('subaccount') == 'block' else '記録のみ'}"
        if op == "panel_send":
            ch = pick_channel(guild, f.get("channel"), required=True)
            await ctx.send_auth_panel(guild, ch)
            return f"#{ch.name} に認証パネルを送信しました"
        if op == "logs_save":
            out = []
            ch = pick_channel(guild, f.get("log_channel"))
            if ch is not None and ch.id != await ctx.get_log_channel_id(gid):
                try:
                    await ctx.set_log_channel_id(gid, ch.id)
                except Exception:
                    raise ValueError("ログチャンネルを保存できませんでした（データベースが未設定か一時的な障害）")
                out.append(f"ログ #{ch.name}")
            tr = pick_channel(guild, f.get("transcript"), attach=True)
            if (tr.id if tr else None) != ctx.get_transcript_channel_id(gid):
                ctx.set_transcript_channel_id(gid, tr.id if tr else None)
                out.append(f"トランスクリプト {('#' + tr.name) if tr else '解除'}")
            inv = pick_channel(guild, f.get("invite_log"))
            data = b.store_load("invite_log_config")
            if (inv.id if inv else None) != data.get(str(gid)):
                if inv:
                    data[str(gid)] = inv.id
                else:
                    data.pop(str(gid), None)
                b.store_save("invite_log_config", data)
                out.append(f"招待ログ {('#' + inv.name) if inv else 'ログチャンネルと同じ'}")
            if perms["admin"]:
                bc = pick_channel(guild, f.get("broadcast"))
                data = ctx.load_broadcast()
                if (bc.id if bc else None) != data.get(str(gid)):
                    if bc:
                        data[str(gid)] = bc.id
                    else:
                        data.pop(str(gid), None)
                    ctx.save_broadcast(data)
                    out.append(f"お知らせ {('#' + bc.name) if bc else '自動'}")
            return "、".join(out) + " を保存しました" if out else ""
        if op == "nuke_save":
            dcfg = ctx.load_defense_config()
            d = dcfg.setdefault(str(gid), {})
            was = d.get("nuke_defense")
            d.update({"nuke_defense": f.get("nuke_defense") == "1", "invite_block": f.get("invite_block") == "1",
                      "raid_protection": f.get("raid_protection") == "1", "auto_restore": f.get("auto_restore") == "1",
                      "action": f.get("action") if f.get("action") in ("ban", "kick", "strip") else "ban"})
            ctx.save_defense_config(dcfg)
            if d["nuke_defense"] and not was:
                try:
                    ctx.take_backup(guild, auto=True)
                except Exception:
                    pass
            return "Nuke・Raid対策の設定を保存しました"
        if op in ("wl_edit", "sbl_edit"):
            uid = pick_uid(f.get("uid"))
            add = f.get("mode") == "add"
            if op == "wl_edit":
                dcfg = ctx.load_defense_config()
                d = dcfg.setdefault(str(gid), {})
                s = {int(x) for x in d.get("whitelist", [])}
                (s.add if add else s.discard)(uid)
                d["whitelist"] = sorted(s)
                ctx.save_defense_config(dcfg)
                return f"信頼済みユーザーに {uid} を{'追加' if add else '削除'}しました"
            data = ctx.load_server_blacklist()
            s = set(data.get(str(gid), []))
            if add and uid in (member.id, guild.owner_id):
                raise ValueError("自分自身やサーバー所有者は追加できません")
            (s.add if add else s.discard)(uid)
            data[str(gid)] = sorted(s)
            ctx.save_server_blacklist(data)
            return f"このサーバーのブラックリストに {uid} を{'追加' if add else '削除'}しました"
        if op == "backup_create":
            snap = ctx.take_backup(guild)
            return f"バックアップを作成しました（ロール {len(snap['roles'])}・チャンネル {len(snap['channels'])}）"
        if op == "backup_restore":
            backups = list(reversed(ctx.load_backups().get(str(gid), [])))
            n = f.get("number", "")
            if not n.isdigit() or not 1 <= int(n) <= len(backups):
                raise ValueError("バックアップを選んでください")
            rep = await ctx.restore_backup(guild, backups[int(n) - 1])
            return f"復元しました（ロール {rep['roles']}・チャンネル {rep['channels']} を作成、失敗 {rep['failed']}）"
        if op in ("sticky_set", "sticky_remove"):
            cog = b.get_cog("Sticky")
            if cog is None:
                raise ValueError("メッセージキープ機能が読み込まれていません")
            if op == "sticky_remove":
                raw = f.get("channel", "")
                if not raw.isdigit():
                    raise ValueError("チャンネルの指定が正しくありません")
                await cog.api_remove(guild, int(raw))
                return "案内メッセージの固定を解除しました"
            ch = pick_channel(guild, f.get("channel"), required=True)
            if not ch.permissions_for(guild.me).manage_messages:
                raise ValueError(f"#{ch.name} でBotに「メッセージの管理」権限がありません")
            text = (f.get("text") or "").strip()
            if not text:
                raise ValueError("本文を入力してください")
            url = (f.get("button_url") or "").strip()
            label = (f.get("button_label") or "").strip()[:80]
            if url and not url.startswith("https://"):
                raise ValueError("ボタンのURLは https:// から始めてください")
            await cog.api_set(ch, {"content": text[:1800], "title": (f.get("title") or "").strip()[:200] or None,
                                   "color": None, "buttons": [{"label": label or "リンク", "url": url}] if url else []})
            return f"#{ch.name} に案内メッセージを固定しました"
        if op == "weather_save":
            data = b.store_load("weather_config")
            ch = pick_channel(guild, f.get("channel"))
            if ch is None:
                data.pop(str(gid), None)
                b.store_save("weather_config", data)
                return "天気予報の自動投稿を停止しました"
            import re
            times = []
            for t in (f.get("times") or "07:00").replace("、", ",").split(","):
                m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", t)
                if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                    raise ValueError(f"時刻の形式が正しくありません: {t[:10]}（例: 07:00）")
                times.append(f"{int(m.group(1)):02d}:{m.group(2)}")
            data[str(gid)] = {"channel": ch.id, "times": sorted(set(times))[:6], "last": {}}
            b.store_save("weather_config", data)
            return f"#{ch.name} に {'、'.join(sorted(set(times)))} に天気予報を投稿します"
        if op in ("disaster_save", "quake_save", "eew_save"):
            data = b.store_load("disaster_config")
            cur = data.setdefault(str(gid), {})
            ch = pick_channel(guild, f.get("channel"))
            role = pick_role(guild, member, f.get("role"), assignable=False)
            mi = f.get("min_int") if f.get("min_int") in INT_ORDER else "3"
            if op == "disaster_save":
                if ch is None:
                    for k in ("channel", "min_int", "weather", "area", "role"):
                        cur.pop(k, None)
                    res = "津波・火山・気象警報の通知を停止しました"
                else:
                    cur.update({"channel": ch.id, "min_int": mi,
                                "weather": f.get("weather") if f.get("weather") in ("special", "warning", "advisory", "off") else "special",
                                "area": (f.get("area") or "").strip()[:20], "role": role.id if role else None})
                    res = f"#{ch.name} に災害情報を通知します"
            elif op == "quake_save":
                if ch is None:
                    for k in ("quake_channel", "quake_min_int", "quake_role"):
                        cur.pop(k, None)
                    res = "地震情報の分離をやめました"
                else:
                    cur.update({"quake_channel": ch.id, "quake_min_int": mi, "quake_role": role.id if role else None})
                    res = f"地震情報を #{ch.name} に通知します（震度{INT_NAME.get(mi, mi)}以上）"
            else:
                if ch is None:
                    cur.pop("eew_channel", None)
                    cur.pop("eew_role", None)
                    res = "緊急地震速報の通知を停止しました"
                else:
                    cur.update({"eew_channel": ch.id, "eew_role": role.id if role else None})
                    res = f"緊急地震速報を #{ch.name} に通知します"
            if not cur:
                data.pop(str(gid), None)
            b.store_save("disaster_config", data)
            return res
        if op in ("level_save", "level_role", "level_noxp"):
            data = b.store_load("level_config")
            lc = data.setdefault(str(gid), {})
            lc.setdefault("roles", {})
            lc.setdefault("no_xp_channels", [])
            if op == "level_save":
                ann = f.get("announce")
                if ann == "channel":
                    ch = pick_channel(guild, f.get("announce_channel"), required=True)
                    lc["announce"] = str(ch.id)
                elif ann in ("same", "off"):
                    lc["announce"] = ann
                try:
                    mult = float(f.get("multiplier") or 1)
                except ValueError:
                    raise ValueError("XPの倍率は数字で入力してください")
                if not 0.1 <= mult <= 5:
                    raise ValueError("XPの倍率は 0.1〜5.0 の範囲です")
                lc.update({"enabled": f.get("enabled") == "1", "multiplier": round(mult, 2), "stack": f.get("stack") == "1"})
                res = "レベル機能の設定を保存しました"
            elif op == "level_role":
                lv = (f.get("level") or "").strip()
                if not (lv.isdigit() and 1 <= int(lv) <= 500):
                    raise ValueError("到達レベルは 1〜500 の数字で入力してください")
                if f.get("mode") == "remove":
                    lc["roles"].pop(str(int(lv)), None)
                    res = f"レベル {int(lv)} のランクロールを削除しました"
                else:
                    r = pick_role(guild, member, f.get("role"), required=True)
                    lc["roles"][str(int(lv))] = r.id
                    res = f"レベル {int(lv)} で @{r.name} を付与します"
            else:
                ch = pick_channel(guild, f.get("channel"), required=True, voice=True)
                s = set(lc["no_xp_channels"])
                (s.add if f.get("mode") == "add" else s.discard)(ch.id)
                lc["no_xp_channels"] = sorted(s)
                res = f"#{ch.name} を XP対象外から{'追加' if f.get('mode') == 'add' else '削除'}しました"
            b.store_save("level_config", data)
            return res
        # ---------- チケット ----------
        if op == "ticket_setup":
            role = pick_role(guild, member, f.get("role"), assignable=False, required=True)
            cat_raw = f.get("category", "")
            cat = guild.get_channel(int(cat_raw)) if cat_raw.isdigit() else None
            if cat_raw and not isinstance(cat, discord.CategoryChannel):
                raise ValueError("カテゴリーの指定が正しくありません")
            await kv_set_json(f"ticket:config:{gid}", {"role_id": role.id, "category_id": cat.id if cat else None})
            return f"チケットの対応ロール @{role.name} ／ カテゴリー {cat.name if cat else 'なし'}"
        if op == "ticket_panel":
            if not await kv_json(f"ticket:config:{gid}"):
                raise ValueError("先にチケットの対応ロールを保存してください")
            cog = b_cog("Tickets")
            if cog is None:
                raise ValueError("チケット機能が読み込まれていません")
            ch = pick_channel(guild, f.get("channel"), required=True)
            view_cls = getattr(sys.modules[type(cog).__module__], "TicketOpenView")
            await ch.send(embed=discord.Embed(title="🎫 お問い合わせ", color=0x5865F2,
                                              description="下のボタンを押すと、あなた専用のチャンネルが作成されます。"),
                          view=view_cls())
            return f"#{ch.name} にチケットのパネルを送信しました"
        # ---------- モデレーション ----------
        if op == "mod_action":
            uid = pick_uid(f.get("uid"))
            action = f.get("action")
            reason = ((f.get("reason") or "").strip() or "理由なし")[:300]
            why = f"{reason}（ダッシュボード・実行者: {member}）"[:500]
            if action == "unban":
                try:
                    await guild.unban(discord.Object(id=uid), reason=why)
                except discord.NotFound:
                    raise ValueError("そのユーザーはBANされていません")
                return f"{uid} のBANを解除しました"
            if uid in (member.id, guild.owner_id) or (b.user and uid == b.user.id):
                raise ValueError("自分自身・サーバー所有者・Botは対象にできません")
            target = guild.get_member(uid)
            if target is None:
                try:
                    target = await guild.fetch_member(uid)
                except discord.NotFound:
                    target = None
            if target is not None:
                if member.id != guild.owner_id and target.top_role >= member.top_role:
                    raise ValueError("あなたと同じか上のロールを持つ人は対象にできません")
                if target.top_role >= guild.me.top_role:
                    raise ValueError("Botより上のロールを持つ人は対象にできません")
            elif action != "ban":
                raise ValueError("そのユーザーはサーバーにいません")
            try:
                if action == "timeout":
                    from datetime import timedelta
                    mins = f.get("minutes", "")
                    if not (mins.isdigit() and 1 <= int(mins) <= 40320):
                        raise ValueError("タイムアウトの分数は 1〜40320 で入力してください")
                    await target.timeout(timedelta(minutes=int(mins)), reason=why)
                    res = f"{target} を {mins}分タイムアウトしました"
                elif action == "untimeout":
                    await target.timeout(None, reason=why)
                    res = f"{target} のタイムアウトを解除しました"
                elif action == "kick":
                    await notify_dm(target, f"👢 {guild.name} からキックされました", reason)
                    await target.kick(reason=why)
                    res = f"{target} をキックしました"
                elif action == "ban":
                    days = f.get("days", "0")
                    days = int(days) if days.isdigit() and int(days) <= 7 else 0
                    if target is not None:
                        await notify_dm(target, f"🔨 {guild.name} からBANされました", reason)
                    await guild.ban(discord.Object(id=uid), reason=why, delete_message_seconds=days * 86400)
                    res = f"{target or uid} をBANしました"
                else:
                    raise ValueError("操作を選んでください")
            except discord.Forbidden:
                raise ValueError("Botの権限が足りません（BAN・キック・タイムアウトの権限を確認してください）")
            return f"{res}（理由: {reason}）"
        if op in ("warn_add", "warn_clear"):
            uid = pick_uid(f.get("uid"))
            k = f"warn:{gid}:{uid}"
            if op == "warn_clear":
                await b.kv("delete", k)
                return f"{uid} の警告をすべて削除しました"
            reason = (f.get("reason") or "").strip()[:300]
            if not reason:
                raise ValueError("理由を入力してください")
            target = guild.get_member(uid)
            if target is None:
                try:
                    target = await guild.fetch_member(uid)
                except discord.NotFound:
                    raise ValueError("そのユーザーはサーバーにいません")
            if target.bot:
                raise ValueError("Botには警告できません")
            if member.id != guild.owner_id and target.top_role >= member.top_role:
                raise ValueError("あなたと同じか上のロールを持つ人には警告できません")
            from datetime import datetime, timedelta, timezone
            entry = {"reason": reason, "by": member.id,
                     "at": datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")}
            await b.kv("rpush", k, json.dumps(entry, ensure_ascii=False))
            await b.kv("ltrim", k, -100, -1)
            total = await b.kv("llen", k)
            await notify_dm(target, f"⚠️ {guild.name} で警告を受けました", f"{reason}\n累計: {total} 回")
            return f"{target} に警告しました（累計 {total} 回・理由: {reason}）"
        # ---------- 自動モデレーション・イベントログ ----------
        if op == "automod_save":
            cfg = {"spam": f.get("spam") == "1", "invite": f.get("invite") == "1"}
            await kv_set_json(f"automod:{gid}", cfg)
            cog = b_cog("AutoMod")
            if cog is not None:
                import time as _t
                cog._cfg[gid] = (_t.monotonic(), cfg)  # すぐに反映する
            return f"自動モデレーション: スパム {'オン' if cfg['spam'] else 'オフ'} ／ 招待リンク {'オン' if cfg['invite'] else 'オフ'}"
        if op == "eventlog_save":
            ch = pick_channel(guild, f.get("channel"))
            if ch is None:
                await b.kv("delete", f"eventlog:{gid}")
            else:
                await b.kv("set", f"eventlog:{gid}", str(ch.id))
            cog = b_cog("EventLogs")
            if cog is not None:
                cog._cache[gid] = ch.id if ch else None
            return f"イベントログ: {('#' + ch.name) if ch else '記録しない'}"
        # ---------- リアクションロール ----------
        if op.startswith("rr_"):
            rr = b_cog("ReactionRoles")
            if rr is None:
                raise ValueError("リアクションロール機能が読み込まれていません")
            menus = await rr._get_menus(gid)
            if op == "rr_create":
                title = (f.get("title") or "").strip()[:100]
                if not title:
                    raise ValueError("タイトルを入力してください")
                pid = secrets.token_hex(3)
                if not await rr._save_menu(gid, pid, {"title": title, "description": (f.get("description") or "")[:1500],
                                                      "color": None, "roles": []}):
                    raise ValueError("保存できませんでした（データベースの障害）")
                return f"ロールメニュー「{title}」を作成しました"
            pid = f.get("menu", "")
            p = menus.get(pid)
            if p is None:
                raise ValueError("メニューを選んでください")
            if op == "rr_edit":
                role = pick_role(guild, member, f.get("role"), required=True)
                roles = [r for r in p["roles"] if r["id"] != role.id]
                if f.get("mode") == "remove":
                    p["roles"] = roles
                    res = f"「{p['title']}」から @{role.name} を外しました"
                else:
                    if len(roles) >= 25:
                        raise ValueError("1つのメニューに登録できるのは25個までです")
                    roles.append({"id": role.id, "emoji": (f.get("emoji") or "").strip()[:60] or None,
                                  "desc": (f.get("desc") or "").strip()[:90] or None})
                    p["roles"] = roles
                    res = f"「{p['title']}」に @{role.name} を追加しました（{len(roles)}/25）"
                if not await rr._save_menu(gid, pid, p):
                    raise ValueError("保存できませんでした（データベースの障害）")
                await rr._refresh(guild, pid, p)
                return res
            if op == "rr_send":
                if not p["roles"]:
                    raise ValueError("先にロールを追加してください")
                ch = pick_channel(guild, f.get("channel"), required=True)
                msg = await ch.send(embed=rr._menu_embed(guild, p), view=rr._menu_view(pid))
                p["c"], p["m"] = ch.id, msg.id
                await rr._save_menu(gid, pid, p)
                return f"#{ch.name} にロールメニュー「{p['title']}」を送信しました"
            if op == "rr_delete":
                await rr._save_menu(gid, pid, None)
                ch = guild.get_channel(p.get("c") or 0)
                if isinstance(ch, discord.TextChannel) and p.get("m"):
                    try:
                        await ch.get_partial_message(p["m"]).delete()
                    except discord.HTTPException:
                        pass
                return f"ロールメニュー「{p['title']}」を削除しました"
        # ---------- 一斉ロール付与 ----------
        if op == "mass_role":
            if f.get("confirm") != "1":
                raise ValueError("確認のチェックを入れてください")
            role = pick_role(guild, member, f.get("role"), required=True)
            if not b.intents.members:
                raise ValueError("メンバー一覧を取得できません（SERVER MEMBERS INTENT が無効です）")
            cog = b_cog("MassRole")
            running = getattr(cog, "_running", set()) if cog else _mass_running
            if gid in running:
                raise ValueError("このサーバーでは、すでに一斉付与を実行中です")
            running.add(gid)

            async def run():
                done = failed = skipped = 0
                try:
                    async for m in guild.fetch_members(limit=None):
                        if m.bot or role in m.roles:
                            skipped += 1
                            continue
                        try:
                            await m.add_roles(role, reason=f"一斉ロール付与（ダッシュボード・実行者: {member}）"[:200])
                            done += 1
                        except discord.HTTPException:
                            failed += 1
                        await asyncio.sleep(1.2)
                finally:
                    running.discard(gid)
                e = discord.Embed(title="🏷️ 一斉ロール付与が完了しました", color=0x5865F2,
                                  description=f"ロール: {role.mention}\n成功 {done} / 失敗 {failed} / スキップ {skipped}")
                e.add_field(name="実行者", value=f"<@{member.id}>（ダッシュボード）")
                try:
                    await ctx.send_guild_log(guild, e)
                except Exception:
                    pass
            asyncio.create_task(run())
            return f"@{role.name} の一斉付与を開始しました（完了するとログチャンネルに記録されます）"
        raise ValueError("不明な操作です")

    return gd_callback
