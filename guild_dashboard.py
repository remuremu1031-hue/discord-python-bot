"""サーバー管理者向けWebダッシュボード（/guild-dashboard）。

・Discord OAuth2（identify guilds）でログインし、「管理者」または「サーバー管理」権限を持つサーバーを一覧表示
・サーバーを選ぶと、そのサーバーの設定（認証・ログ・トランスクリプト・Nuke対策・サブアカウント検知）を変更できる
・権限は、表示するとき・保存するときの両方で、Botが Discord から取得したメンバー情報で必ず再確認する
  （ブラウザから送られてくる値やログイン時の一覧は信用しない）。権限が無ければ 403。
・「認証ロール・Nuke対策・サブアカウント検知」は、スラッシュコマンドと同じく「管理者」権限が必要
main.py から register(app, ctx) で登録する。このファイルは main.py を import しない。
"""
from __future__ import annotations

import hashlib
import hmac
import html
import secrets
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
    async def gd_home(request: Request, guild: str = "", msg: str = "", ng: str = ""):
        sess = current(request)
        if sess is None:
            return page(status_card() + '<div class="card hero" style="margin-top:6vh"><h2>サーバー管理者向けダッシュボード</h2>'
                        '<p class="mut">Discordでログインすると、あなたが「管理者」または「サーバー管理」権限を持つ'
                        'サーバーの、霊夢BOTの設定を変更できます。</p>'
                        '<a class="btn" href="/guild-dashboard/login">Discordでログイン</a>'
                        '<p class="mut" style="margin-top:14px">取得する情報: ユーザーID・ユーザー名・参加サーバーの一覧'
                        '（<a href="/privacy">プライバシーポリシー</a>）</p></div>')
        if guild:
            return await settings_page(request, sess, guild, msg[:300], ng == "1")
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
            icon = (f'<img src="https://cdn.discordapp.com/icons/{_e(g["id"])}/{_e(g["icon"])}.png?size=96" alt="">'
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

    def role_options(guild: discord.Guild, member: discord.Member, current_id: int | None) -> str:
        opts = ['<option value="">（選択してください）</option>']
        for r in sorted(guild.roles, key=lambda r: -r.position):
            if r.is_default() or r.managed:
                continue
            ok = r < guild.me.top_role and (member.id == guild.owner_id or r < member.top_role) \
                and not any(getattr(r.permissions, p) for p in DANGEROUS)
            if not ok and r.id != current_id:
                continue
            opts.append(f'<option value="{r.id}"{" selected" if r.id == current_id else ""}>@{_e(r.name)}</option>')
        return "".join(opts)

    def channel_options(guild: discord.Guild, current_id: int | None, *, allow_none: bool,
                        none_label: str = "（設定しない）") -> str:
        opts = [f'<option value="">{_e(none_label)}</option>'] if allow_none else \
            ['<option value="">（選択してください）</option>']
        for ch in sorted(guild.text_channels, key=lambda c: (c.category.position if c.category else -1, c.position)):
            p = ch.permissions_for(guild.me)
            if not (p.view_channel and p.send_messages and p.embed_links) and ch.id != current_id:
                continue
            cat = f"{ch.category.name} / " if ch.category else ""
            opts.append(f'<option value="{ch.id}"{" selected" if ch.id == current_id else ""}>#{_e(cat + ch.name)}</option>')
        return "".join(opts)

    async def settings_page(request: Request, sess: dict, gid_raw: str, msg: str, is_ng: bool):
        try:
            guild, member, is_admin = await authorize(request, sess, gid_raw)
        except PermissionError as e:
            return forbidden(request, sess, str(e))
        except LookupError as e:
            if str(e) == "suspended":
                return error(403, "このサーバーでは利用できません", "このサーバーでは、霊夢BOTの機能が停止されています。", sess)
            return error(404, "Botが参加していません", "このサーバーには霊夢BOTが導入されていません。", sess)
        gid = guild.id
        cfg = ctx.load_config().get(str(gid)) or {}
        d = ctx.load_defense_config().get(str(gid), {})
        log_id = await ctx.get_log_channel_id(gid)
        tr_id = ctx.get_transcript_channel_id(gid)
        sub = ctx.get_subaccount_action(gid)
        lock = "" if is_admin else " disabled"
        lockcls = "" if is_admin else " lock"

        def chk(name, val, label):
            return (f'<label class="check{lockcls}"><input type="checkbox" name="{name}" value="1"'
                    f'{" checked" if val else ""}{lock}> {label}</label>')
        action = d.get("action", "ban")
        icon = (f'<img src="{_e(guild.icon.replace(size=96).url)}" alt="">' if guild.icon
                else f'<div class="ph">{_e(guild.name[:1])}</div>')
        flash = f'<div class="flash{" ng" if is_ng else ""}">{_e(msg)}</div>' if msg else ""
        admin_note = "" if is_admin else (
            '<div class="flash">🔒 あなたは「サーバー管理」権限のため、認証・Nuke対策・サブアカウント検知の設定は変更できません'
            '（「管理者」権限が必要です）。</div>')
        body = status_card() + f"""
<div class="g" style="margin-bottom:14px">{icon}<div><div class="n" style="font-size:19px">{_e(guild.name)}</div>
<div class="mut">メンバー {guild.member_count or 0:,} 人 ・ <a href="/guild-dashboard">サーバー一覧に戻る</a></div></div></div>
{flash}{admin_note}
<form method="post" action="/guild-dashboard/save">
<input type="hidden" name="csrf" value="{_e(sess['csrf'])}"><input type="hidden" name="guild" value="{gid}">
<div class="card{lockcls}"><h2 style="margin-top:0">🔐 認証</h2>
<label>認証後に付与するロール</label><select name="role_id"{lock}>{role_options(guild, member, cfg.get("role_id"))}</select>
<div class="mut">Botより下・あなたより下で、強い権限（管理者・BAN等）を持たないロールだけ選べます</div>
<label>認証ログを送るチャンネル</label><select name="auth_log"{lock}>{channel_options(guild, cfg.get("log_channel_id"), allow_none=False)}</select>
<label>同じIPからの別アカウント（サブアカウント）を検知したとき</label>
<select name="subaccount"{lock}><option value="block"{" selected" if sub == "block" else ""}>認証を拒否する</option>
<option value="log"{" selected" if sub == "log" else ""}>認証は通し、ログに記録するだけ</option></select>
{f'<p class="mut">認証ページ: <code>{_e(ctx.auth_url(gid))}</code></p>' if cfg else ""}
</div>
<div class="card" style="margin-top:12px"><h2 style="margin-top:0">📝 ログ・チケット</h2>
<label>セキュリティ・GBAN・招待などのログチャンネル（/set-log-channel）</label>
<select name="log_channel">{channel_options(guild, log_id, allow_none=True, none_label="（変更しない）")}</select>
<label>チケットのトランスクリプト送信先</label>
<select name="transcript">{channel_options(guild, tr_id, allow_none=True)}</select>
</div>
<div class="card{lockcls}" style="margin-top:12px"><h2 style="margin-top:0">🛡️ Nuke・Raid対策</h2>
{chk("nuke_defense", d.get("nuke_defense"), "Nuke（大量削除・大量BAN等）を自動検知する")}
<label>Nukeの実行者への自動対処</label><select name="action"{lock}>
<option value="ban"{" selected" if action == "ban" else ""}>BAN</option>
<option value="kick"{" selected" if action == "kick" else ""}>キック</option>
<option value="strip"{" selected" if action == "strip" else ""}>権限剥奪（ロールを全て外す）</option></select>
{chk("auto_restore", d.get("auto_restore", True), "消されたチャンネル・ロールを自動で復元する")}
{chk("raid_protection", d.get("raid_protection"), "大量参加（Raid）を自動検知する")}
{chk("invite_block", d.get("invite_block"), "招待リンクを自動でブロックする")}
</div>
<div style="margin:18px 0"><button class="btn">保存する</button></div>
</form>"""
        return page(body, sess)

    @app.post("/guild-dashboard/save")
    @ctx.limiter.limit("20/minute")
    async def gd_save(request: Request):
        sess = current(request)
        if sess is None:
            return RedirectResponse("/guild-dashboard", status_code=303)
        f = await read_form(request)
        if not secrets.compare_digest(f.get("csrf", ""), sess["csrf"]):
            ctx.guard_web_strike(request, "bad_dashboard_csrf")
            return error(400, "保存できません", "ページの有効期限が切れました。開き直してください。", sess)
        try:  # 保存の直前にも、権限を必ず確認し直す
            guild, member, is_admin = await authorize(request, sess, f.get("guild", ""))
        except PermissionError as e:
            return forbidden(request, sess, str(e))
        except LookupError:
            return error(404, "保存できません", "このサーバーでは設定を変更できません。", sess)
        gid = guild.id
        changes, errors = [], []

        def pick_channel(raw: str, *, attach: bool = False):
            if not raw:
                return None
            if not raw.isdigit():
                raise ValueError("チャンネルの指定が正しくありません")
            ch = guild.get_channel(int(raw))
            if not isinstance(ch, discord.TextChannel):
                raise ValueError("そのチャンネルはこのサーバーのテキストチャンネルではありません")
            p = ch.permissions_for(guild.me)
            if not (p.view_channel and p.send_messages and p.embed_links and (p.attach_files or not attach)):
                raise ValueError(f"#{ch.name} でBotにメッセージを送る権限がありません")
            return ch

        # --- 「サーバー管理」以上で変更できるもの ---
        try:
            ch = pick_channel(f.get("log_channel", ""))
            if ch is not None and ch.id != await ctx.get_log_channel_id(gid):
                try:
                    await ctx.set_log_channel_id(gid, ch.id)
                    changes.append(f"ログチャンネル → #{ch.name}")
                except Exception:
                    errors.append("ログチャンネルを保存できませんでした（データベースが未設定か一時的な障害）")
        except ValueError as e:
            errors.append(f"ログチャンネル: {e}")
        try:
            ch = pick_channel(f.get("transcript", ""), attach=True)
            new = ch.id if ch else None
            if new != ctx.get_transcript_channel_id(gid):
                ctx.set_transcript_channel_id(gid, new)
                changes.append(f"トランスクリプト送信先 → {('#' + ch.name) if ch else '解除'}")
        except ValueError as e:
            errors.append(f"トランスクリプト送信先: {e}")

        # --- 「管理者」だけが変更できるもの（画面で無効にしていても、送られてきたら必ず拒否する） ---
        if is_admin:
            role_raw, auth_log_raw = f.get("role_id", ""), f.get("auth_log", "")
            cfg_all = ctx.load_config()
            cur = cfg_all.get(str(gid)) or {}
            if role_raw or auth_log_raw:
                try:
                    if not (role_raw.isdigit() and auth_log_raw):
                        raise ValueError("認証ロールと認証ログのチャンネルは、両方選んでください")
                    role = guild.get_role(int(role_raw))
                    if role is None or role.is_default() or role.managed:
                        raise ValueError("そのロールは使えません")
                    if role >= guild.me.top_role:
                        raise ValueError("Botのロールより上のロールは使えません")
                    if member.id != guild.owner_id and role >= member.top_role:
                        raise ValueError("あなたのロールより上のロールは設定できません")
                    if any(getattr(role.permissions, p) for p in DANGEROUS):
                        raise ValueError("強い権限（管理者・BAN・ロール管理など）を持つロールは、認証ロールにできません")
                    ach = pick_channel(auth_log_raw)
                    if cur.get("role_id") != role.id or cur.get("log_channel_id") != ach.id:
                        cfg_all[str(gid)] = {**cur, "role_id": role.id, "log_channel_id": ach.id}
                        ctx.save_config(cfg_all)
                        changes.append(f"認証ロール → @{role.name} ／ 認証ログ → #{ach.name}")
                except ValueError as e:
                    errors.append(f"認証: {e}")
            sub = f.get("subaccount")
            if sub in ("block", "log") and sub != ctx.get_subaccount_action(gid):
                ctx.set_subaccount_action(gid, sub)
                changes.append(f"サブアカウント検知 → {'拒否' if sub == 'block' else '記録のみ'}")
            dcfg = ctx.load_defense_config()
            d = dict(dcfg.get(str(gid), {}))
            new = {"nuke_defense": f.get("nuke_defense") == "1", "invite_block": f.get("invite_block") == "1",
                   "raid_protection": f.get("raid_protection") == "1", "auto_restore": f.get("auto_restore") == "1",
                   "action": f.get("action") if f.get("action") in ("ban", "kick", "strip") else d.get("action", "ban")}
            diff = [k for k, v in new.items() if d.get(k, True if k == "auto_restore" else (
                "ban" if k == "action" else False)) != v]
            if diff:
                turned_on = new["nuke_defense"] and not d.get("nuke_defense")
                d.update(new)
                dcfg[str(gid)] = d
                ctx.save_defense_config(dcfg)
                changes.append("Nuke・Raid対策の設定を変更")
                if turned_on:
                    try:
                        ctx.take_backup(guild, auto=True)
                    except Exception:
                        pass
        else:
            sent_admin = any(f.get(k) for k in ("role_id", "auth_log", "nuke_defense", "invite_block",
                                                "raid_protection", "auto_restore"))
            if sent_admin:  # 画面では無効なのに送られてきた ＝ 改ざんされたリクエスト
                ctx.guard_web_strike(request, "guild_forbidden", "管理者専用の設定を、権限なしで変更しようとした")

        if changes:  # サーバーのログチャンネルに、誰が何を変えたかを残す
            e = discord.Embed(title="🛠️ ダッシュボードで設定が変更されました", color=0x5865F2,
                              description="\n".join(f"・{c}" for c in changes))
            e.add_field(name="変更した人", value=f"<@{member.id}>（`{member}` / `{member.id}`）")
            try:
                await ctx.send_guild_log(guild, e)
            except Exception:
                pass
        if errors:
            msg, ng = "⚠️ " + " ／ ".join(errors) + (f"（ほかの {len(changes)} 件は保存しました）" if changes else ""), "1"
        else:
            msg, ng = ("✅ 保存しました: " + "、".join(changes)) if changes else "変更はありませんでした。", ""
        q = httpx.QueryParams({"guild": str(gid), "msg": msg[:280], **({"ng": "1"} if ng else {})})
        return _headers(RedirectResponse(f"/guild-dashboard?{q}", status_code=303))

    return gd_callback
