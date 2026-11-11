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
import re
import sys
import time
from urllib.parse import parse_qs

import discord
import httpx
from fastapi import Request
from types import SimpleNamespace
from urllib.parse import quote

from fastapi.responses import HTMLResponse, RedirectResponse, Response

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
:root{--bg:#0a0b0d;--bg2:#101114;--card:#16171b;--card2:#1c1d22;--line:#26282e;--line2:#30333b;
--text:#e8eaf0;--mut:#8b90a0;--blue:#3b82f6;--blue2:#2563eb;--blue-soft:rgba(59,130,246,.14);
--ok:#22c55e;--ng:#ef4444;--warn:#f59e0b;--r:18px}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:radial-gradient(1200px 500px at 50% -200px,rgba(37,99,235,.18),transparent 70%),var(--bg);
color:var(--text);font:15px/1.6 "Inter","Noto Sans JP","Hiragino Sans",system-ui,-apple-system,sans-serif;min-height:100vh}
a{color:#7fb0ff;text-decoration:none}
header{position:sticky;top:0;z-index:20;display:flex;justify-content:space-between;align-items:center;gap:10px;
padding:12px 16px;background:rgba(10,11,13,.82);backdrop-filter:saturate(160%) blur(14px);
-webkit-backdrop-filter:saturate(160%) blur(14px);border-bottom:1px solid var(--line)}
header h1{margin:0;font-size:16px;font-weight:700;letter-spacing:.01em;display:flex;align-items:center;gap:8px;
white-space:nowrap;min-width:0;overflow:hidden}
header h1 a{min-width:0;overflow:hidden;text-overflow:ellipsis}
header .btn{white-space:nowrap;min-height:36px;padding:7px 12px;font-size:13px;flex:none}
header form{flex:none}
@media (max-width:520px){header h1{font-size:14px}header .me span{display:none}header{padding:10px 12px}}
header h1 .logo{width:28px;height:28px;border-radius:9px;background:linear-gradient(135deg,var(--blue),#1d4ed8);
display:inline-flex;align-items:center;justify-content:center;font-size:15px;box-shadow:0 4px 14px rgba(37,99,235,.45)}
header .me{display:flex;gap:8px;align-items:center;color:var(--mut);font-size:13px;min-width:0}
header .me span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:120px}
header img{width:30px;height:30px;border-radius:50%;border:1px solid var(--line2)}
main{padding:18px 14px 60px;max-width:1120px;margin:0 auto}
h2{font-size:16px;margin:0 0 10px;font-weight:700}
.sec{display:flex;align-items:center;gap:10px;margin:26px 2px 12px}
.sec h3{margin:0;font-size:13px;letter-spacing:.08em;color:var(--mut);font-weight:700}
.sec .line{flex:1;height:1px;background:var(--line)}
.card{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:var(--r);
padding:18px;box-shadow:0 1px 0 rgba(255,255,255,.03) inset,0 8px 24px rgba(0,0,0,.25)}
.grid,.bento{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(270px,1fr))}
.bento .wide{grid-column:span 2}
@media (max-width:640px){.bento .wide{grid-column:span 1}main{padding:14px 10px 60px}.card{padding:15px}}
.mod{display:flex;flex-direction:column;gap:10px;min-height:150px;transition:border-color .15s,transform .15s}
.mod:hover{border-color:var(--line2);transform:translateY(-1px)}
.mod .top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.mod .ic{width:42px;height:42px;border-radius:13px;background:var(--blue-soft);color:#93c5fd;display:flex;
align-items:center;justify-content:center;font-size:21px;flex:none}
.mod .name{font-weight:700;font-size:15px;margin-top:2px}
.mod .desc{color:var(--mut);font-size:13px;line-height:1.55;flex:1}
.mod .foot{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:2px}
.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;
background:#22252c;color:var(--mut);border:1px solid var(--line2)}
.badge:before{content:"";width:7px;height:7px;border-radius:50%;background:#5b616e}
.badge.on{background:rgba(59,130,246,.12);color:#93c5fd;border-color:rgba(59,130,246,.35)}
.badge.on:before{background:var(--blue);box-shadow:0 0 8px var(--blue)}
.open{font-size:13px;font-weight:600;color:#93c5fd;padding:6px 10px;border-radius:10px}
.open:hover{background:var(--blue-soft)}
.switch{position:relative;width:48px;height:28px;border-radius:999px;border:0;background:#2c2f37;cursor:pointer;
flex:none;padding:0;transition:background .2s;box-shadow:inset 0 0 0 1px var(--line2)}
.switch:after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#cfd3dc;
transition:transform .2s,background .2s;box-shadow:0 2px 6px rgba(0,0,0,.4)}
.switch.on{background:var(--blue);box-shadow:0 0 0 3px rgba(59,130,246,.18)}
.switch.on:after{transform:translateX(20px);background:#fff}
.switch:disabled{opacity:.4;cursor:not-allowed}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;background:var(--blue);color:#fff;border:0;
border-radius:12px;padding:10px 16px;font:inherit;font-weight:600;cursor:pointer;min-height:42px;
box-shadow:0 6px 16px rgba(37,99,235,.28);transition:background .15s}
.btn:hover{background:var(--blue2)}.btn:disabled{opacity:.45;cursor:not-allowed;box-shadow:none}
.btn.ghost{background:#22252c;box-shadow:none;border:1px solid var(--line2);color:var(--text)}
.btn.ghost:hover{background:#2a2d35}
.btn.danger{background:#dc2626;box-shadow:0 6px 16px rgba(220,38,38,.25)}
.tabs{display:flex;gap:8px;overflow-x:auto;padding:4px 2px 10px;margin:0 -2px 6px;scrollbar-width:none;
-webkit-overflow-scrolling:touch;position:sticky;top:55px;z-index:10;background:linear-gradient(var(--bg) 70%,transparent)}
.tabs::-webkit-scrollbar{display:none}
.tabs a{flex:none;padding:8px 14px;border-radius:999px;background:var(--card);border:1px solid var(--line);
color:var(--text);font-size:13px;font-weight:600;white-space:nowrap}
.tabs a.on{background:var(--blue);border-color:var(--blue);color:#fff;box-shadow:0 4px 14px rgba(37,99,235,.35)}
label{display:block;margin:14px 0 6px;font-weight:600;font-size:13px;color:#c9cdd6}
select,input[type=text],textarea{width:100%;background:var(--bg2);color:var(--text);border:1px solid var(--line2);
border-radius:12px;padding:11px 12px;font:inherit;font-size:15px;min-height:44px;outline:none}
select:focus,input[type=text]:focus,textarea:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(59,130,246,.2)}
.check{display:flex;gap:12px;align-items:center;justify-content:space-between;margin:10px 0;padding:12px 14px;
border-radius:12px;background:var(--bg2);border:1px solid var(--line);font-weight:500;color:var(--text)}
.check input{appearance:none;-webkit-appearance:none;width:48px;height:28px;border-radius:999px;background:#2c2f37;
position:relative;cursor:pointer;flex:none;order:2;margin:0;transition:background .2s;box-shadow:inset 0 0 0 1px var(--line2)}
.check input:after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#cfd3dc;
transition:transform .2s}
.check input:checked{background:var(--blue)}.check input:checked:after{transform:translateX(20px);background:#fff}
.flash{padding:12px 14px;border-radius:14px;background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.3);
margin-bottom:14px;font-size:14px}
.flash.ng{background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.35)}
.lock{opacity:.6}
.tag{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;background:#22252c;color:var(--mut)}
.tag.ok{background:rgba(59,130,246,.15);color:#93c5fd}
.g{display:flex;gap:12px;align-items:center;min-width:0}
.g img,.g .ph{width:52px;height:52px;border-radius:16px;background:#22252c;display:flex;align-items:center;
justify-content:center;font-weight:700;flex:none;border:1px solid var(--line2)}
.g .n{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mut{color:var(--mut);font-size:13px}
.hero{max-width:520px;margin:10vh auto;text-align:center;padding:30px 22px}
.hero h2{font-size:22px}
code{color:#a5c8ff;background:rgba(59,130,246,.1);padding:1px 6px;border-radius:6px;font-size:12px;word-break:break-all}
.ghead{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:4px 0 14px}
form{margin:0}
.toast{position:fixed;left:50%;bottom:22px;z-index:50;display:flex;align-items:center;gap:10px;width:max-content;max-width:min(92vw,520px);
padding:12px 18px;border-radius:14px;background:rgba(22,23,27,.96);border:1px solid rgba(59,130,246,.45);color:var(--text);
font-size:14px;font-weight:600;box-shadow:0 12px 32px rgba(0,0,0,.5),0 0 0 4px rgba(59,130,246,.12);
transform:translate(-50%,0);opacity:0;pointer-events:none;animation:toast 3.6s ease forwards}
.toast .tick{width:24px;height:24px;border-radius:50%;background:var(--blue);color:#fff;display:inline-flex;
align-items:center;justify-content:center;font-size:14px;flex:none}
@keyframes toast{0%{opacity:0;transform:translate(-50%,16px)}10%{opacity:1;transform:translate(-50%,0)}
80%{opacity:1;transform:translate(-50%,0)}100%{opacity:0;transform:translate(-50%,8px);visibility:hidden}}
@media (prefers-reduced-motion:reduce){.toast{animation:toast-fade 3.6s steps(1) forwards}}
@keyframes toast-fade{0%{opacity:1}100%{opacity:0;visibility:hidden}}
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
               f'<style>{CSS}</style></head><body><header><h1 style="display:flex;align-items:center;gap:8px">'
               f'<a href="/" title="公式サイト（ホーム）へ" aria-label="公式サイト（ホーム）へ"><span class="logo">⛩️</span></a>'
               f'<a href="/guild-dashboard" style="color:inherit">{TITLE}</a></h1>{me}</header>'
               f'<main>{body}</main></body></html>')
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
    async def gd_home(request: Request, guild: str = "", msg: str = "", ng: str = "", tab: str = "modules", q: str = ""):
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
        body = (status_card() + '<div class="sec"><h3>サーバーを選択</h3><div class="line"></div></div>'
                '<p class="mut" style="margin-top:-4px">「管理者」または「サーバー管理」権限を持つサーバーだけが表示されます。</p>'
                + (f'<div class="grid">{"".join(cards)}</div>' if cards else
                   '<div class="card">管理できるサーバーがありません。</div>'))
        return page(body, sess)

    # ================= 設定画面（タブ） =================
    TABS = [("modules", "🧩 モジュール"), ("auth", "🔐 認証"), ("logs", "📝 ログ・通知"), ("security", "🛡️ セキュリティ"),
            ("moderation", "🔨 モデレーション"), ("automod", "🧹 自動モデ・ログ"), ("tickets", "🎫 チケット"),
            ("roles", "🎭 ロール"), ("sticky", "📌 案内の固定"), ("alerts", "🌦️ 天気・災害"), ("levels", "📈 レベル"),
            ("events", "🎉 イベント・投票"), ("tools", "🧰 便利ツール"), ("media", "📺 配信・VC")]
    EXT_TABS = ("events", "tools", "media")
    KIND_LABEL = {"channel_delete": "チャンネルの大量削除", "channel_create": "チャンネルの大量作成",
                  "role_delete": "ロールの大量削除", "role_create": "ロールの大量作成", "ban": "連続BAN",
                  "kick": "連続キック", "webhook_create": "Webhookの大量作成", "prune": "メンバーの一括削除（Prune）"}
    # extras.py（追加モジュール）の操作と必要な権限
    EXT_NEED = {"x_ocr": "admin", "x_event": "manage_guild", "x_poll": "manage_guild", "x_poll_close": "manage_guild",
                "x_event_delete": "manage_guild", "x_translate": "manage_guild", "x_automsg_add": "manage_guild",
                "x_automsg_del": "manage_guild", "x_report": "manage_guild", "x_faq_add": "manage_guild",
                "x_faq_del": "manage_guild", "x_faq_panel": "manage_guild", "x_faq_set": "manage_guild",
                "x_threads": "manage_guild", "x_youtube_add": "manage_guild", "x_youtube_del": "manage_guild",
                "x_say": "manage_guild", "x_vclink": "admin", "x_html": "admin"}
    EXT_TOGGLE = {"ocr": "admin", "translate": "manage_guild", "automsg": "manage_guild", "report": "manage_guild",
                  "faq": "manage_guild", "threads": "manage_guild", "youtube": "manage_guild", "vclink": "admin"}

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
        tab = tab if tab in dict(TABS) else "modules"
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

        def voice_options(current=None):
            out = ['<option value="">（選択してください）</option>']
            for ch in sorted(list(guild.voice_channels) + list(guild.stage_channels), key=lambda c: c.position):
                cat = f"{ch.category.name} / " if ch.category else ""
                out.append(f'<option value="{ch.id}"{" selected" if ch.id == current else ""}>🔊 {_e(cat + ch.name)}</option>')
            return "".join(out)

        def text_area(name, value, maxlen):
            return f'<textarea name="{_e(name)}" maxlength="{int(maxlen)}" rows="4">{_e(value or "")}</textarea>'

        H = SimpleNamespace(card=card, form=form, sel=sel, lbl=lbl, chk=chk, opt=opt, e=_e, voice_options=voice_options,
                            text_area=text_area,
                            channel_options=lambda cur, none_label="（選択してください）", voice=False: channel_options(
                                guild, int(cur) if str(cur or "").isdigit() else None, none_label=none_label, voice=voice))
        ext = b_cog("Extras")
        xs = ext.module_states(gid) if ext is not None else {}

        rec_cog = b_cog("Recorder")
        rec_state = rec_cog.module_state(gid) if rec_cog is not None else (False, "（未導入）")

        def xm(key):
            return xs.get(key, (False, "（未導入）"))

        html_parts = []
        if tab == "modules":
            d = ctx.load_defense_config().get(str(gid), {})
            am = {"spam": False, "invite": False, **(await kv_json(f"automod:{gid}") or {})}
            lc = ctx.bot.store_load("level_config").get(str(gid), {})
            w = ctx.bot.store_load("weather_config").get(str(gid))
            dz = ctx.bot.store_load("disaster_config").get(str(gid), {})
            cfg = ctx.load_config().get(str(gid))
            ev = await kv_get(f"eventlog:{gid}")
            tc = await kv_json(f"ticket:config:{gid}")
            sticky = b_cog("Sticky")
            st_n = len(await sticky._conf(gid)) if sticky else 0
            rr = b_cog("ReactionRoles")
            rr_n = len(await rr._get_menus(gid)) if rr else 0
            bk_n = len(ctx.load_backups().get(str(gid), []))
            inv_log = ctx.bot.store_load("invite_log_config").get(str(gid))
            main_id = ctx.get_main_log_channel_id(gid)
            main_name = getattr(guild.get_channel(main_id or 0), "name", None)
            ev_on = bool(ev) and str(ev) != "0" or (not ev and bool(main_id))
            # (キー, アイコン, 名前, 説明, 有効か, トグル可否["both"/"off"/None], 詳細タブ, 必要な権限, 幅広)
            groups = [
                ("保護", [
                    ("nuke", "🛡️", "Nuke対策", "大量削除・連続BANを検知し、実行者を自動処罰・被害を自動復元", d.get("nuke_defense"), "both", "security", "admin", True),
                    ("raid", "🚧", "Raid対策", "短時間の大量参加を検知し、参加者のキックと認証レベルの引き上げ", d.get("raid_protection"), "both", "security", "admin", False),
                    ("inviteblock", "🔗", "招待リンクブロック", "他サーバーの招待リンクを自動でブロック", d.get("invite_block"), "both", "security", "admin", False),
                    ("spam", "🧹", "スパム対策", "連続投稿を自動削除して5分タイムアウト", am["spam"], "both", "automod", "admin", False),
                    ("amInvite", "✂️", "招待リンク削除", "招待リンクを含むメッセージを自動削除", am["invite"], "both", "automod", "admin", False),
                    ("ignore", "🙈", "監視しないチャンネル", f"指定したチャンネルでは全機能を停止（{len(ctx.get_ignored_channels(gid))}件）", bool(ctx.get_ignored_channels(gid)), None, "security", "admin", False),
                    ("ocr", "🔍", "OCR画像検閲", "画像内の文字を読み取り、禁止ワードや招待リンクを検出" + xm("ocr")[1], xm("ocr")[0], "both", "security", "admin", False),
                ]),
                ("参加・認証", [
                    ("auth", "🔐", "Web認証", "Discordログインで認証し、ロールを自動付与", bool(cfg), None, "auth", "admin", True),
                    ("subaccount", "🕵️", "サブアカウント検知", "同じIPからの別アカウントの認証を拒否（オフで記録のみ）", ctx.get_subaccount_action(gid) == "block", "both", "auth", "admin", False),
                    ("invites", "📨", "招待トラッキング", "誰の招待で参加したかを記録", bool(b_cog("InviteTracker")), None, "logs", "manage_guild", False),
                ]),
                ("エンゲージメント", [
                    ("levels", "📈", "レベル・ランク", "メッセージとVCで経験値が貯まり、ランクロールを付与", lc.get("enabled", True) and bool(b_cog("Levels")), "both", "levels", "manage_guild", True),
                    ("roles", "🎭", "ロールメニュー", f"複数選択できるロールパネル（{rr_n}個）", rr_n > 0, None, "roles", "admin", False),
                    ("sticky", "📌", "案内の固定", f"案内を常にチャンネルの一番下に表示（{st_n}件）", st_n > 0, None, "sticky", "manage_messages", False),
                    ("events", "🎉", "イベント", "候補日時の投票・予定の作成・参加者へのリマインド通知" + xm("events")[1], xm("events")[0], None, "events", "manage_guild", True),
                    ("polls", "📊", "アンケート", "ボタンで投票、締め切りで自動集計" + xm("polls")[1], xm("polls")[0], None, "events", "manage_guild", False),
                    ("faq", "❓", "FAQ", "よくある質問パネルと、キーワードへの自動回答" + xm("faq")[1], xm("faq")[0], "both", "tools", "manage_guild", False),
                ]),
                ("通知・配信", [
                    ("weather", "🗾", "天気予報", "全国の天気予報を指定時刻に自動投稿", bool(w), "off", "alerts", "manage_guild", False),
                    ("disaster", "🌊", "災害情報", "地震・津波・火山・気象警報を自動通知", bool(dz.get("channel") or dz.get("quake_channel")), "off", "alerts", "manage_guild", False),
                    ("eew", "🚨", "緊急地震速報", "予想最大震度5弱以上の警報を即時通知", bool(dz.get("eew_channel")), "off", "alerts", "manage_guild", False),
                    ("eventlog", "📋", "イベントログ", "削除・編集・入退室・VC移動を記録", ev_on, "off", "automod", "admin", False),
                    ("youtube", "📺", "YouTube通知", "新着動画・配信をPush（WebSub）で即時通知" + xm("youtube")[1], xm("youtube")[0], "both", "media", "manage_guild", True),
                    ("automsg", "⏰", "自動メッセージ", "指定した曜日・時刻にメッセージを自動投稿" + xm("automsg")[1], xm("automsg")[0], "both", "tools", "manage_guild", False),
                    ("report", "📊", "週次レポート", "メッセージ数・参加/退出・よく話す人を毎週まとめて送信", xm("report")[0], "both", "tools", "manage_guild", False),
                ]),
                ("ログ", [
                    ("mainlog", "🗂️", "メインログ", (f"#{main_name} に、個別に設定していないすべてのログを送信" if main_name
                     else "個別に設定していないすべてのログを、1つのチャンネルにまとめて送信"), bool(main_id), "off", "logs",
                     "manage_guild", True),
                ]),
                ("運営・管理", [
                    ("tickets", "🎫", "チケット", "ボタンで問い合わせ用チャンネルを作成（トランスクリプト保存）", bool(tc), None, "tickets", "admin", True),
                    ("moderation", "🔨", "モデレーション・警告", "タイムアウト・キック・BAN・警告の管理", True, None, "moderation", "moderate", False),
                    ("backup", "💾", "バックアップ", f"ロール・チャンネル構成の保存と復元（{bk_n}件）", bk_n > 0, None, "security", "admin", False),
                    ("logs", "📝", "個別のログ", "セキュリティ・トランスクリプト・招待ログを、メインログとは別のチャンネルへ", bool(inv_log) or bool(ctx.get_transcript_channel_id(gid)), None, "logs", "manage_guild", False),
                    ("translate", "🌐", "翻訳", "国旗リアクションや指定チャンネルで自動翻訳（DeepL）" + xm("translate")[1], xm("translate")[0], "both", "tools", "manage_guild", False),
                    ("threads", "🧵", "スレッド管理", "指定チャンネルの投稿に自動でスレッドを作成", xm("threads")[0], "both", "tools", "manage_guild", False),
                    ("say", "📣", "代理送信", "テキスト・埋め込み・Webhookで、Botから送信（メンションは任意）", ext is not None, None, "tools", "manage_guild", False),
                    ("vclink", "🎧", "VC連動公開", "VCに参加中の人だけに聞き専チャンネルを表示" + xm("vclink")[1], xm("vclink")[0], "both", "media", "admin", False),
                    ("html", "📄", "チャンネルHTML保存", "チャンネルの会話をHTMLファイルとしてダウンロード", ext is not None, None, "media", "admin", False),
                    ("recording", "🎙️", "録音", "VCを話した人ごとに録音し、ZIPで送信" + rec_state[1], rec_state[0], "both", "media", "admin", False),
                ]),
            ]
            out = []
            for gname, mods in groups:
                cards = []
                for key, ic, name, desc, on, tog, link, need, wide in mods:
                    sw = ""
                    if tog == "both" or (tog == "off" and on):
                        dis = "" if perms[need] else " disabled"
                        sw = (f'<form method="post" action="/guild-dashboard/save"><input type="hidden" name="csrf" value="{_e(sess["csrf"])}">'
                              f'<input type="hidden" name="guild" value="{gid}"><input type="hidden" name="tab" value="modules">'
                              f'<input type="hidden" name="op" value="module_toggle"><input type="hidden" name="key" value="{key}">'
                              f'<input type="hidden" name="to" value="{"0" if on else "1"}">'
                              f'<button class="switch{" on" if on else ""}" title="{"無効にする" if on else "有効にする"}" '
                              f'aria-label="{_e(name)}を{"無効" if on else "有効"}にする"{dis}></button></form>')
                    href = f'/guild-dashboard?{httpx.QueryParams({"guild": str(gid), "tab": link})}'
                    cards.append(
                        f'<div class="card mod{" wide" if wide else ""}"><div class="top"><div class="g" style="gap:12px">'
                        f'<div class="ic">{ic}</div><div class="name">{_e(name)}</div></div>{sw}</div>'
                        f'<div class="desc">{_e(desc)}</div><div class="foot">'
                        f'<span class="badge{" on" if on else ""}">{"有効" if on else "無効"}</span>'
                        f'<a class="open" href="{href}">設定を開く ›</a></div></div>')
                out.append(f'<div class="sec"><h3>{_e(gname)}</h3><div class="line"></div></div><div class="bento">{"".join(cards)}</div>')
            html_parts.append("".join(out))
        elif tab == "auth":

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
            main_id = ctx.get_main_log_channel_id(gid)
            log_id = await ctx.get_log_channel_id(gid)
            inv = ctx.bot.store_load("invite_log_config").get(str(gid))
            bc = ctx.load_broadcast().get(str(gid))
            to_main = "（メインログに送る）"
            html_parts.append(card("🗂️ メインログ（すべてのログ）", form("mainlog_save",
                lbl("メインログのチャンネル") + sel("channel", channel_options(guild, main_id, none_label="（設定しない）")),
                "保存する"), note="下で個別のチャンネルを決めていないログは、すべてここに届きます。"
                "個別に決めたログは、そのチャンネルだけに届きます（メインログには重ねて送りません）。"))
            html_parts.append(card("📝 個別のログチャンネル", form("logs_save",
                lbl("セキュリティ・GBAN・設定変更のログ（/set-log-channel）") + sel("log_channel", channel_options(guild, log_id, none_label=to_main))
                + lbl("チケットのトランスクリプト（/transcript-channel）") + sel("transcript", channel_options(guild, ctx.get_transcript_channel_id(gid), none_label=to_main))
                + lbl("招待リンクの作成・参加の記録（/invite-log）") + sel("invite_log", channel_options(guild, int(inv) if inv else None, none_label=to_main))
                + lbl("霊夢BOTからのお知らせ（/broadcast-channel・管理者のみ）") + sel("broadcast", channel_options(guild, bc, none_label="（自動で選ぶ）")),
                "保存する"), note="認証ログは「認証」タブ、イベントログは「自動モデ・ログ」タブで決められます。"))
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
            # ---- 監視しないチャンネル（/ignore-channel） ----
            ign = set(ctx.get_ignored_channels(gid))
            ign_box = "".join(chk(f"c_{c.id}", c.id in ign, ("📁 " if str(getattr(c, "type", "")) == "category" else "#") + c.name)
                              for c in sorted(guild.channels, key=lambda c: (c.position, c.id)))
            ign_box += "".join(chk(f"c_{t}", True, f"🧵 スレッド {t}") for t in sorted(ign) if guild.get_channel(t) is None)
            html_parts.append(card("🙈 監視しないチャンネル（/ignore-channel）", form("ignore_save",
                '<div style="max-height:260px;overflow:auto;border:1px solid var(--line,#263243);border-radius:10px;padding:6px 10px">'
                + ign_box + '</div>', "保存する", need="admin"), need="admin",
                note="チェックしたチャンネル（カテゴリなら中のチャンネルすべて）では、霊夢BOTのすべての機能が止まります。"
                     "メッセージ・リアクション・コマンド・ボタンに一切反応しません（/ignore-channel だけは、戻せるように使えます）。"))
            # ---- 除外設定（/defense-exempt） ----
            ex_roles = {int(x) for x in d.get("nuke_exempt_roles", [])}
            ex_chans = {int(x) for x in d.get("nuke_exempt_channels", [])}
            ex_kinds = set(d.get("nuke_exempt_kinds", []))
            role_box = "".join(chk(f"r_{r.id}", r.id in ex_roles, "@" + r.name)
                               for r in reversed(guild.roles) if not r.is_default() and not r.managed)
            chan_box = "".join(chk(f"c_{c.id}", c.id in ex_chans, ("📁 " if str(getattr(c, "type", "")) == "category" else "#") + c.name)
                               for c in sorted(guild.channels, key=lambda c: (c.position, c.id)))
            kind_box = "".join(chk(f"k_{k}", k in ex_kinds, v) for k, v in KIND_LABEL.items())
            scroll = '<div style="max-height:220px;overflow:auto;border:1px solid var(--line,#263243);border-radius:10px;padding:6px 10px">{}</div>'
            html_parts.append(card("🚫 Nukeの除外（/defense-exempt）", form("nuke_exempt",
                lbl("このロールを持つ人の操作は検知しない") + scroll.format(role_box or '<span class="mut">ロールがありません</span>')
                + lbl("このチャンネル・カテゴリの作成・削除は数えない") + scroll.format(chan_box)
                + lbl("検知しない操作") + kind_box
                + lbl("その他") + chk("bots", d.get("nuke_exempt_bots"), "BOTによる操作をすべて検知しない"),
                "保存する", need="admin"), need="admin",
                note="ユーザー単位の除外は、上の「信頼済みユーザー」で設定します。自動で一時チャンネルを作るBOTなどがいる場合に使ってください。"))
            r_users = "\n".join(str(u) for u in d.get("raid_exempt_users", []))
            r_inv = "\n".join(d.get("raid_exempt_invites", []))
            html_parts.append(card("🚫 Raidの除外（/defense-exempt）", form("raid_exempt",
                lbl("除外するユーザーID（1行に1人）") + f'<textarea name="users" rows="3" maxlength="4000">{_e(r_users)}</textarea>'
                + lbl("この招待リンクからの参加は数えない（コードまたはURL・1行に1つ）") + f'<textarea name="invites" rows="3" maxlength="2000" placeholder="例: abcDEF12">{_e(r_inv)}</textarea>'
                + lbl("アカウント作成からこの日数以上の人は数えない（0で制限なし）")
                + f'<input type="number" name="min_age" min="0" max="3650" value="{int(d.get("raid_min_age_days", 0) or 0)}">',
                "保存する", need="admin"), need="admin",
                note="除外された人は、Raidモード中でもキックされません。イベント用の招待リンクなど、一度に人が集まるときに使ってください。"))
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
            if ext is not None:
                html_parts.append(ext.ocr_card(gid, H))
        elif tab in EXT_TABS:
            if ext is None:
                html_parts.append(card("🧩 追加モジュール", '<p class="mut">extras.py が読み込まれていません。ファイルを配置して再起動してください。</p>'))
            else:
                html_parts.append(await ext.render(tab, gid, guild, member, H))
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
            ev_mode = "off" if str(ev) == "0" else ("main" if not ev else "ch")
            html_parts.append(card("🧹 自動モデレーション（/automod）", form("automod_save",
                chk("spam", am["spam"], "連続投稿スパムを自動で削除し、5分タイムアウトする")
                + chk("invite", am["invite"], "招待リンクを自動で削除する"), "保存する", need="admin"), need="admin",
                note="管理者・「メッセージの管理」権限のメンバーは対象外です。"))
            html_parts.append(card("📋 イベントログ（/eventlog）", form("eventlog_save",
                lbl("記録のしかた") + sel("mode", opt("main", "メインログに送る", ev_mode) + opt("ch", "下のチャンネルに送る", ev_mode)
                                       + opt("off", "記録しない", ev_mode))
                + lbl("送信先チャンネル（「下のチャンネルに送る」のとき）")
                + sel("channel", channel_options(guild, int(ev) if ev and str(ev).isdigit() and str(ev) != "0" else None)),
                "保存する", need="admin"), need="admin", note="メッセージの削除・編集、入退室、VCの移動、チケットのクローズなどを記録します。"))
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

        nav = "".join(f'<a class="{"on" if k == tab else ""}" '
                      f'href="/guild-dashboard?{httpx.QueryParams({"guild": str(gid), "tab": k})}">{_e(v)}</a>' for k, v in TABS)
        icon = (f'<img src="{_e(guild.icon.replace(size=128).url)}" alt="">' if guild.icon else f'<div class="ph">{_e(guild.name[:1])}</div>')
        if not msg:
            flash = ""
        elif is_ng:
            flash = f'<div class="flash ng">{_e(msg)}</div>'
        else:  # 保存できたときは、画面下に数秒だけ表示して消える（スクリプトなし・CSSのアニメーションだけ）
            flash = (f'<div class="toast" role="status"><span class="tick">✓</span>'
                     f'<span>{_e(msg.removeprefix("✅").strip())}</span></div>')
        role_text = "管理者" if perms["admin"] else "サーバー管理"
        body = (status_card() + f'<div class="g" style="margin-bottom:12px">{icon}<div><div class="n" style="font-size:19px">{_e(guild.name)}</div>'
                f'<div class="mut">あなたの権限: {role_text} ・ <a href="/guild-dashboard">サーバー一覧に戻る</a></div></div></div>'
                f'<nav class="tabs">{nav}</nav>{flash}' + "".join(html_parts)
)
        return page(body, sess)

    # ================= 保存 =================
    OP_NEED = {"auth_save": "admin", "panel_send": "admin", "logs_save": "manage_guild", "nuke_save": "admin",
               "wl_edit": "admin", "ignore_save": "admin", "rec_save": "admin", "rec_stop": "admin", "nuke_exempt": "admin", "raid_exempt": "admin", "sbl_edit": "admin", "backup_create": "admin", "backup_restore": "admin",
               "sticky_set": "manage_messages", "sticky_remove": "manage_messages", "weather_save": "manage_guild",
               "disaster_save": "manage_guild", "quake_save": "manage_guild", "eew_save": "manage_guild",
               "level_save": "manage_guild", "level_role": "manage_guild", "level_noxp": "manage_guild",
               "ticket_setup": "admin", "ticket_panel": "admin", "mod_action": "admin", "warn_add": "moderate",
               "warn_clear": "admin", "automod_save": "admin", "eventlog_save": "admin", "rr_create": "admin",
               "rr_edit": "admin", "rr_send": "admin", "rr_delete": "admin", "mass_role": "admin",
               "module_toggle": "manage_guild", "mainlog_save": "manage_guild"}

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
        tab = f.get("tab") if f.get("tab") in dict(TABS) else "modules"
        perms = ctx_member_perms(member, guild)
        need = OP_NEED.get(op) or EXT_NEED.get(op)
        if need is None:
            ctx.guard_web_strike(request, "invalid_input", f"不明な操作 {op[:20]}")
            return error(400, "保存できません", "不明な操作です。", sess)
        if not perms[need]:  # 画面ではボタンを無効にしている ＝ 改ざんされたリクエスト（forbidden で加点）
            return forbidden(request, sess, f"{op} を権限なしで実行しようとした")
        gid = guild.id
        try:
            if op in ("rec_save", "rec_stop"):
                rec = b_cog("Recorder")
                if rec is None:
                    raise ValueError("録音機能（recorder.py）が読み込まれていません")
                P = SimpleNamespace(pick_channel=lambda raw, required=False: pick_channel(guild, raw, required=required))
                done = await rec.dashboard_op(op, f, guild, P)
            elif op.startswith("x_"):
                ext = b_cog("Extras")
                if ext is None:
                    raise ValueError("追加モジュール（extras.py）が読み込まれていません")
                P = SimpleNamespace(pick_channel=lambda raw, required=False, voice=False: pick_channel(
                    guild, raw, required=required, voice=voice))
                done = await ext.dashboard_op(op, f, guild, member, P)
                if isinstance(done, tuple) and done and done[0] == "file":  # HTML保存はその場でダウンロード
                    _, fname, data = done
                    return _headers(Response(data, media_type="text/html; charset=utf-8", headers={
                        "Content-Disposition": f"attachment; filename=\"channel.html\"; filename*=UTF-8''{quote(fname)}",
                        "Cache-Control": "no-store"}))
            else:
                done = await apply_op(op, f, guild, member, perms)
            ok = True
        except ValueError as e:
            done, ok = str(e), False
        msg = ("✅ " + (done or "変更はありませんでした")) if ok else ("⚠️ " + done)
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
        if op == "mainlog_save":
            ch = pick_channel(guild, f.get("channel"))
            ctx.set_main_log_channel_id(gid, ch.id if ch else None)
            ev = b_cog("EventLogs")
            if ev is not None and hasattr(ev, "_stamp"):
                ev._stamp.clear()  # イベントログの送信先を読み直させる
            return f"メインログを {('#' + ch.name) if ch else '解除'} にしました"
        if op == "logs_save":
            out = []
            ch = pick_channel(guild, f.get("log_channel"))
            cur_log = await ctx.get_log_channel_id(gid)
            if (ch.id if ch else None) != cur_log:
                try:
                    if ch:
                        await ctx.set_log_channel_id(gid, ch.id)
                    else:
                        await ctx.del_log_channel_id(gid)
                except Exception:
                    raise ValueError("ログチャンネルを保存できませんでした（データベースが未設定か一時的な障害）")
                out.append(f"セキュリティログ {('#' + ch.name) if ch else '→ メインログ'}")
            tr = pick_channel(guild, f.get("transcript"), attach=True)
            if (tr.id if tr else None) != ctx.get_transcript_channel_id(gid):
                ctx.set_transcript_channel_id(gid, tr.id if tr else None)
                out.append(f"トランスクリプト {('#' + tr.name) if tr else '→ メインログ'}")
            inv = pick_channel(guild, f.get("invite_log"))
            data = b.store_load("invite_log_config")
            if (inv.id if inv else None) != data.get(str(gid)):
                if inv:
                    data[str(gid)] = inv.id
                else:
                    data.pop(str(gid), None)
                b.store_save("invite_log_config", data)
                out.append(f"招待ログ {('#' + inv.name) if inv else '→ メインログ'}")
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
        if op == "ignore_save":
            valid = {c.id for c in guild.channels}
            keep_threads = [t for t in ctx.get_ignored_channels(gid) if t not in valid and f.get(f"c_{t}") == "1"]
            ids = [int(k[2:]) for k in f if k.startswith("c_") and k[2:].isdigit() and int(k[2:]) in valid] + keep_threads
            ctx.set_ignored_channels(gid, ids)
            return f"監視しないチャンネルを保存しました（{len(ids)}件）" if ids else "すべてのチャンネルを監視する設定にしました"
        if op == "nuke_exempt":
            dcfg = ctx.load_defense_config()
            d = dcfg.setdefault(str(gid), {})
            valid_r = {r.id for r in guild.roles}
            valid_c = {c.id for c in guild.channels}
            d["nuke_exempt_roles"] = [int(k[2:]) for k in f if k.startswith("r_") and k[2:].isdigit() and int(k[2:]) in valid_r][:200]
            d["nuke_exempt_channels"] = [int(k[2:]) for k in f if k.startswith("c_") and k[2:].isdigit() and int(k[2:]) in valid_c][:200]
            d["nuke_exempt_kinds"] = [k[2:] for k in f if k.startswith("k_") and k[2:] in KIND_LABEL]
            d["nuke_exempt_bots"] = f.get("bots") == "1"
            ctx.save_defense_config(dcfg)
            return (f"Nukeの除外を保存しました（ロール {len(d['nuke_exempt_roles'])}・チャンネル {len(d['nuke_exempt_channels'])}"
                    f"・操作 {len(d['nuke_exempt_kinds'])}）")
        if op == "raid_exempt":
            dcfg = ctx.load_defense_config()
            d = dcfg.setdefault(str(gid), {})
            users = []
            for ln in (f.get("users") or "").split():
                if not re.fullmatch(r"\d{15,21}", ln.strip()):
                    raise ValueError(f"ユーザーIDの形式が正しくありません: {ln[:25]}")
                if int(ln) not in users:
                    users.append(int(ln))
            codes = []
            for ln in (f.get("invites") or "").split():
                c = re.sub(r"^(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/", "", ln.strip()).strip("/")
                if not re.fullmatch(r"[A-Za-z0-9-]{2,32}", c):
                    raise ValueError(f"招待リンクの形式が正しくありません: {ln[:40]}")
                if c not in codes:
                    codes.append(c)
            age = (f.get("min_age") or "0").strip()
            if not age.isdigit() or int(age) > 3650:
                raise ValueError("日数は 0〜3650 で指定してください")
            d["raid_exempt_users"], d["raid_exempt_invites"], d["raid_min_age_days"] = users[:200], codes[:100], int(age)
            ctx.save_defense_config(dcfg)
            return f"Raidの除外を保存しました（ユーザー {len(users)}・招待リンク {len(codes)}・日数 {int(age)}日）"
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
        # ---------- モジュール一覧のトグル ----------
        if op == "module_toggle":
            key, on = f.get("key", ""), f.get("to") == "1"
            if key == "recording":
                rec = b_cog("Recorder")
                if rec is None:
                    raise ValueError("録音機能（recorder.py）が読み込まれていません")
                return f"録音 を{rec.toggle(gid, on)}にしました"
            if key in EXT_TOGGLE:
                if not perms[EXT_TOGGLE[key]]:
                    raise ValueError("このモジュールを切り替える権限がありません")
                ext = b_cog("Extras")
                if ext is None:
                    raise ValueError("追加モジュール（extras.py）が読み込まれていません")
                names = {"ocr": "OCR画像検閲", "translate": "翻訳", "automsg": "自動メッセージ", "report": "週次レポート",
                         "faq": "FAQ", "threads": "スレッド管理", "youtube": "YouTube通知", "vclink": "VC連動公開"}
                return f"{names[key]} を{ext.toggle(gid, key, on)}にしました"
            need = {"nuke": "admin", "raid": "admin", "inviteblock": "admin", "spam": "admin", "amInvite": "admin",
                    "subaccount": "admin", "levels": "manage_guild", "weather": "manage_guild", "disaster": "manage_guild",
                    "eew": "manage_guild", "eventlog": "admin", "mainlog": "manage_guild"}.get(key)
            if need is None:
                raise ValueError("このモジュールは切り替えられません")
            if not perms[need]:
                raise ValueError("このモジュールを切り替える権限がありません")
            label = "有効" if on else "無効"
            if key in ("nuke", "raid", "inviteblock"):
                field = {"nuke": "nuke_defense", "raid": "raid_protection", "inviteblock": "invite_block"}[key]
                dcfg = ctx.load_defense_config()
                dd = dcfg.setdefault(str(gid), {})
                dd[field] = on
                ctx.save_defense_config(dcfg)
                if key == "nuke" and on:
                    try:
                        ctx.take_backup(guild, auto=True)
                    except Exception:
                        pass
                return f"{ {'nuke': 'Nuke対策', 'raid': 'Raid対策', 'inviteblock': '招待リンクブロック'}[key]} を{label}にしました"
            if key in ("spam", "amInvite"):
                cfg = {"spam": False, "invite": False, **(await kv_json(f"automod:{gid}") or {})}
                cfg["spam" if key == "spam" else "invite"] = on
                await kv_set_json(f"automod:{gid}", cfg)
                cog = b_cog("AutoMod")
                if cog is not None:
                    import time as _t
                    cog._cfg[gid] = (_t.monotonic(), cfg)
                return f"{'スパム対策' if key == 'spam' else '招待リンク削除'} を{label}にしました"
            if key == "subaccount":
                ctx.set_subaccount_action(gid, "block" if on else "log")
                return f"サブアカウント検知を{'拒否' if on else '記録のみ'}にしました"
            if key == "levels":
                data = b.store_load("level_config")
                data.setdefault(str(gid), {})["enabled"] = on
                b.store_save("level_config", data)
                return f"レベル機能を{label}にしました"
            if on:
                raise ValueError("通知先のチャンネルを決める必要があります。「設定を開く」から設定してください")
            if key == "weather":
                data = b.store_load("weather_config")
                data.pop(str(gid), None)
                b.store_save("weather_config", data)
                return "天気予報の自動投稿を停止しました"
            if key in ("disaster", "eew"):
                data = b.store_load("disaster_config")
                cur = data.get(str(gid), {})
                drop = ("channel", "min_int", "weather", "area", "role", "quake_channel", "quake_min_int", "quake_role") \
                    if key == "disaster" else ("eew_channel", "eew_role")
                for k in drop:
                    cur.pop(k, None)
                if cur:
                    data[str(gid)] = cur
                else:
                    data.pop(str(gid), None)
                b.store_save("disaster_config", data)
                return f"{'災害情報' if key == 'disaster' else '緊急地震速報'}の通知を停止しました"
            if key == "eventlog":
                await b.kv("set", f"eventlog:{gid}", "0")
                cog = b_cog("EventLogs")
                if cog is not None:
                    cog._cache.pop(gid, None)
                return "イベントログを停止しました"
            if key == "mainlog":
                ctx.set_main_log_channel_id(gid, None)
                return "メインログを解除しました"
            raise ValueError("不明なモジュールです")
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
            mode = f.get("mode")
            if mode == "ch":
                ch = pick_channel(guild, f.get("channel"), required=True)
                await b.kv("set", f"eventlog:{gid}", str(ch.id))
                res = f"イベントログ: #{ch.name}"
            elif mode == "off":
                await b.kv("set", f"eventlog:{gid}", "0")
                res = "イベントログ: 記録しない"
            else:
                await b.kv("delete", f"eventlog:{gid}")
                res = "イベントログ: メインログに送る"
            cog = b_cog("EventLogs")
            if cog is not None:
                cog._cache.pop(gid, None)
            return res
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
