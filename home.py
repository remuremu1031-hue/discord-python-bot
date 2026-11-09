"""霊夢BOT 公式サイト（ランディングページ）

main.py から register(app, ctx) を呼ぶと、次のページが使えるようになる。
  /          … トップページ（?guild= 付きのときは、これまで通り認証ページ）
  /guide     … 導入ガイド・各モジュールの使い方
  /demo      … ログインせずに試せる、ダッシュボードの操作デモ（保存はされない）
  /status    … 公開用の稼働状況

スクリプトは一切使わず、HTMLとCSSだけで動く（CSPで script を禁止できる）。
"""
from __future__ import annotations

import html
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
from types import SimpleNamespace

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

SUPPORT_URL = "https://discord.gg/eWpUkX9bPM"

_e = html.escape

# ---------------- 掲載内容（ここを書き換えればLP・ガイド・デモに反映） ----------------
# (キー, アイコン, 名前, 概要, 使い方[...], ダッシュボードのタブ, 状態)
MODULES = [
    ("ocr", "🔍", "OCR画像検閲", "画像に写った禁止ワードや招待リンクを読み取り、自動で削除・記録します。文字入り画像による荒らしも見逃しません。",
     ["ダッシュボード「🛡️ セキュリティ」→「OCR画像検閲」をオンにします。",
      "禁止ワード（カンマ区切り）と、見つけたときの対処（削除／記録のみ・タイムアウト）を選びます。",
      "管理者・メッセージ管理権限を持つメンバーは対象外です。"], "security", ""),
    ("events", "🎉", "イベント", "候補日時の投票から予定の作成、参加者へのリマインドまで。Discordのイベント機能とも連携します。",
     ["/event create で、タイトル・日時・告知チャンネルを指定して作成します。",
      "参加ボタンを押したメンバーに、開始前にリマインドが届きます。",
      "日程が決まっていないときは、アンケートの「日程調整」で候補日時を投票できます。"], "events", ""),
    ("polls", "📊", "アンケート", "ボタンで投票、締め切りで自動集計。複数選択や日程調整にも対応します。",
     ["/poll create で質問と選択肢（最大20）を指定します。",
      "締め切りを決めると、その時刻に自動で集計結果を投稿します。",
      "ダッシュボード「🎉 イベント・投票」からも作成・締め切りができます。"], "events", ""),
    ("translate", "🌐", "翻訳", "国旗の絵文字でリアクションするだけで、その国の言語に翻訳。指定チャンネルの自動翻訳もできます。",
     ["メッセージに 🇺🇸 🇰🇷 🇨🇳 などの国旗でリアクションすると翻訳を返信します。",
      "/translate で好きな文章を翻訳できます。",
      "ダッシュボード「🧰 便利ツール」で、自動翻訳するチャンネルを登録できます。"], "tools", ""),
    ("automsg", "⏰", "自動メッセージ", "毎日決まった時刻や、一定の間隔でメッセージを自動投稿。Webhookで名前を変えて送ることもできます。",
     ["ダッシュボード「🧰 便利ツール」→「自動メッセージ」で送信先とタイミングを選びます。",
      "タイトルを入れると埋め込みで送信されます。"], "tools", ""),
    ("youtube", "📺", "YouTube Push", "新しい動画や配信を、YouTubeからのPush通知で即座にお知らせ。見逃しを防ぎます。",
     ["ダッシュボード「📺 配信・VC」で、チャンネルのURLまたはIDと通知先を登録します。",
      "通知のたびにメンションするロールも選べます。"], "media", ""),
    ("say", "📣", "代理送信", "Botから、テキスト・埋め込み・Webhookでお知らせを送信。メンションの有無も選べます。",
     ["/say で送信先と本文を指定します。",
      "ダッシュボード「🧰 便利ツール」→「代理送信」では、埋め込みの色やWebhookの名前も指定できます。"], "tools", ""),
    ("report", "📈", "定期レポート", "メンバーの増減・参加と退出・メッセージ数・よく使われたチャンネルを、毎週まとめてお届けします。",
     ["ダッシュボード「🧰 便利ツール」→「週次レポート」で、送信先・曜日・時刻を選びます。"], "tools", ""),
    ("faq", "❓", "FAQ", "よくある質問をパネルにまとめて、ボタンで回答。キーワードへの自動回答もできます。",
     ["ダッシュボード「🧰 便利ツール」→「FAQ」で質問と回答を登録します。",
      "/faq または「パネルを送信」で、質問を選べるパネルを設置できます。"], "tools", ""),
    ("threads", "🧵", "スレッド管理", "指定したチャンネルの投稿に、自動でスレッドを作成。話題ごとに会話が整理されます。",
     ["ダッシュボード「🧰 便利ツール」→「スレッド管理」でチャンネルとアーカイブまでの時間を選びます。"], "tools", ""),
    ("vclink", "🎧", "VC連動公開", "ボイスチャンネルに参加している人だけに、聞き専チャンネルを表示します。",
     ["ダッシュボード「📺 配信・VC」で、ボイスチャンネルとテキストチャンネルを組み合わせます。",
      "テキストチャンネルは @everyone から「チャンネルを見る」を外しておいてください。"], "media", ""),
    ("recording", "🎙️", "録音", "ボイスチャンネルの録音。Discordの音声暗号化への対応を待って提供予定です。",
     ["現在は準備中です。提供開始はお知らせでご案内します。"], "media", "準備中"),
    ("html", "📄", "チャンネルHTML保存", "チャンネルの会話を、見やすいHTMLファイルとして保存。記録の保管や引き継ぎに。",
     ["/export-channel で、最新から指定した件数を書き出します。",
      "ダッシュボード「📺 配信・VC」からは、その場でダウンロードできます。"], "media", ""),
]

# 3つの柱（ヒーロー下）
PILLARS = [
    ("守り", "🛡️", "Nuke・Raid対策、招待リンクやスパムの自動削除、サブアカウント検知、OCR画像検閲。荒らしや不正からサーバーを守ります。"),
    ("整え", "🧩", "Web認証、ロールメニュー、チケット、ログ、バックアップ。ややこしい設定は、すべてダッシュボードから。"),
    ("語り合う", "💬", "レベル・ランク、イベント、アンケート、翻訳、FAQ。会話が生まれ、コミュニティが育つ仕組みを。"),
]

# お知らせの初期値（新しい順）。所有者がトップページから投稿すると、そちらが使われる
UPDATES = [
    ("2026.11", "NEW", "13の追加モジュールを公開", "OCR画像検閲・イベント・アンケート・翻訳・YouTube Push など、運営に役立つモジュールを追加しました。"),
    ("2026.11", "UPDATE", "サーバー管理ダッシュボードを刷新", "すべてのコマンドの設定がブラウザから行えるようになりました。モジュールの一覧からワンタップで切り替えできます。"),
    ("2026.11", "UPDATE", "メインログに対応", "個別に設定していないログを、1つのチャンネルにまとめて送れるようになりました。"),
    ("2026.11", "NEW", "緊急地震速報・災害情報の通知", "地震・津波・気象警報などを、警報の種類に合わせて自動でお知らせします。"),
    ("2026.11", "NEW", "レベル・ランク機能", "メッセージやVCで経験値が貯まり、ランクロールを自動で付与します。"),
]

STEPS = [
    ("01", "サーバーに追加", "「Discordに追加する」から、霊夢BOTを導入したいサーバーを選んで追加します。"),
    ("02", "使う機能を選ぶ", "ダッシュボードにログインし、モジュールの一覧から使いたい機能をオンにして、通知先などを設定します。"),
    ("03", "動作を確認して運用スタート", "テストメッセージや /help で動きを確かめたら、準備完了。あとは霊夢BOTにおまかせください。"),
]

# ---------------- スタイル ----------------
CSS = """
:root{--bg:#07090d;--bg2:#0c1016;--card:#10151d;--card2:#141b25;--line:#1d2633;--line2:#263243;
--text:#e8edf5;--mut:#8b97a8;--blue:#3b82f6;--blue2:#60a5fa;--blue3:#1d4ed8;--glow:rgba(59,130,246,.35)}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);line-height:1.7;-webkit-font-smoothing:antialiased;
font-family:"Inter","Hiragino Sans","Hiragino Kaku Gothic ProN","Noto Sans JP","Yu Gothic UI","Meiryo",system-ui,sans-serif}
a{color:var(--blue2);text-decoration:none}a:hover{color:#93c5fd}
.wrap{max-width:1160px;margin:0 auto;padding:0 22px}
/* ヘッダー */
header{position:sticky;top:0;z-index:20;background:rgba(7,9,13,.78);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
header .wrap{display:flex;align-items:center;gap:18px;height:64px}
.logo{display:flex;align-items:center;gap:10px;font-weight:800;color:var(--text);letter-spacing:.02em;font-size:17px}
.logo img,.logo .ph{width:32px;height:32px;border-radius:10px;background:linear-gradient(135deg,var(--blue),var(--blue3));display:grid;place-items:center;font-size:16px}
header nav{display:flex;align-items:center;gap:22px;margin-left:auto;font-size:14px}header nav a{color:var(--mut)}header nav a:hover{color:var(--text)}
.hbtn{padding:8px 14px;border-radius:10px;background:var(--blue);color:#fff!important;font-weight:700;font-size:13px}
@media(max-width:820px){header nav a:not(.hbtn){display:none}}
/* ボタン */
.btn{display:inline-flex;align-items:center;gap:8px;padding:13px 22px;border-radius:12px;font-weight:700;font-size:15px;
border:1px solid transparent;transition:.2s;white-space:nowrap}
.btn.pri{background:linear-gradient(180deg,#4f8ff7,var(--blue));color:#fff;box-shadow:0 8px 30px var(--glow),inset 0 1px 0 rgba(255,255,255,.18)}
.btn.pri:hover{transform:translateY(-1px);box-shadow:0 12px 36px var(--glow);color:#fff}
.btn.sec{background:rgba(59,130,246,.08);border-color:rgba(59,130,246,.35);color:var(--blue2)}
.btn.sec:hover{background:rgba(59,130,246,.15);color:#bfdbfe}
.btn.ghost{border-color:var(--line2);color:var(--text)}.btn.ghost:hover{border-color:var(--blue);color:var(--text)}
/* ヒーロー */
.hero{position:relative;padding:92px 0 40px;overflow:hidden}
.hero:before{content:"";position:absolute;inset:-200px -10% auto;height:700px;pointer-events:none;
background:radial-gradient(ellipse 50% 50% at 50% 30%,rgba(59,130,246,.22),transparent 70%)}
.hero:after{content:"";position:absolute;inset:0;pointer-events:none;opacity:.5;
background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);
background-size:56px 56px;mask-image:radial-gradient(ellipse 70% 55% at 50% 25%,#000,transparent 75%);-webkit-mask-image:radial-gradient(ellipse 70% 55% at 50% 25%,#000,transparent 75%)}
.hero .wrap{position:relative;z-index:1;text-align:center}
.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:999px;border:1px solid rgba(59,130,246,.35);
background:rgba(59,130,246,.08);color:var(--blue2);font-size:13px;font-weight:600}
.pill i{width:7px;height:7px;border-radius:50%;background:var(--blue2);box-shadow:0 0 10px var(--blue2);font-style:normal}
h1{font-size:clamp(40px,7.4vw,84px);line-height:1.08;letter-spacing:-.03em;margin:22px 0 18px;font-weight:900}
h1 .b{background:linear-gradient(120deg,#93c5fd,var(--blue) 55%,#2563eb);-webkit-background-clip:text;background-clip:text;color:transparent}
.lead{color:var(--mut);font-size:clamp(15px,2vw,18px);max-width:640px;margin:0 auto}
.cta{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin:34px 0 0}
.stats{display:flex;gap:28px;justify-content:center;margin-top:26px;color:var(--mut);font-size:13px;flex-wrap:wrap}
.stats b{color:var(--text);font-size:15px;margin-right:4px}
@media(max-width:600px){.lead br{display:none}.stats{gap:10px 18px}.mm .t{font-size:12px}}
/* モックアップ */
.mock{margin:56px auto 0;max-width:980px;border-radius:18px;border:1px solid var(--line2);background:var(--bg2);text-align:left;
box-shadow:0 40px 120px -20px rgba(59,130,246,.35),0 0 0 1px rgba(255,255,255,.02);overflow:hidden}
.mock .bar{display:flex;align-items:center;gap:7px;padding:12px 16px;border-bottom:1px solid var(--line);background:#0a0e14}
.mock .bar i{width:11px;height:11px;border-radius:50%;background:#263243;display:block}
.mock .bar span{margin-left:12px;padding:4px 12px;border-radius:8px;background:var(--card);color:var(--mut);font-size:12px;flex:1;max-width:340px}
.mock .in{display:grid;grid-template-columns:190px 1fr}
.mock .side{border-right:1px solid var(--line);padding:16px 10px;display:flex;flex-direction:column;gap:3px;font-size:13px;color:var(--mut)}
.mock .side div{padding:8px 10px;border-radius:9px}.mock .side .on{background:rgba(59,130,246,.14);color:var(--blue2);font-weight:700}
.mock .main{padding:18px}
.mock .sh{display:flex;align-items:center;gap:10px;margin-bottom:14px;font-weight:700}
.mock .sh small{color:var(--mut);font-weight:400}
.mg{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.mm{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:13px}
.mm.w{grid-column:span 2}
.mm .t{gap:8px;display:flex;align-items:center;justify-content:space-between;font-weight:700;font-size:13px}
.mm p{margin:6px 0 0;color:var(--mut);font-size:11.5px;line-height:1.55}
.sw{width:34px;height:20px;border-radius:999px;background:#263243;position:relative;flex:none}
.sw:after{content:"";position:absolute;top:3px;left:3px;width:14px;height:14px;border-radius:50%;background:#cbd5e1;transition:.2s}
.sw.on{background:var(--blue)}.sw.on:after{left:17px;background:#fff}
@media(max-width:820px){.mock .in{grid-template-columns:1fr}.mock .side{display:none}.mg{grid-template-columns:1fr 1fr}.mm.w{grid-column:span 2}}
/* セクション */
section{padding:96px 0}
.eyebrow{color:var(--blue2);font-size:12px;font-weight:800;letter-spacing:.22em}
h2{font-size:clamp(28px,4.4vw,46px);line-height:1.2;letter-spacing:-.02em;margin:12px 0 14px;font-weight:900}
.sub{color:var(--mut);max-width:620px;margin:0}
.head{margin-bottom:40px}
/* 3本柱 */
.pillars{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:8px}
.pl{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:20px;padding:26px}
.pl .k{font-size:28px;font-weight:900;letter-spacing:-.02em;display:flex;align-items:center;gap:10px}
.pl .k span{font-size:20px;width:42px;height:42px;border-radius:12px;display:grid;place-items:center;background:rgba(59,130,246,.12);border:1px solid rgba(59,130,246,.25)}
.pl p{color:var(--mut);margin:12px 0 0;font-size:14.5px}
@media(max-width:820px){.pillars{grid-template-columns:1fr}}
/* Bento（モジュール） */
.bento{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.mc{position:relative;background:var(--card);border:1px solid var(--line);border-radius:20px;padding:24px;display:flex;flex-direction:column;
transition:.25s;overflow:hidden}
.mc:hover{border-color:rgba(59,130,246,.5);transform:translateY(-2px);box-shadow:0 18px 50px -20px var(--glow)}
.mc.w{grid-column:span 2}
.mc.w:before{content:"";position:absolute;right:-80px;top:-80px;width:260px;height:260px;border-radius:50%;
background:radial-gradient(circle,rgba(59,130,246,.18),transparent 70%);pointer-events:none}
.mc .no{font-size:12px;font-weight:800;color:var(--blue2);letter-spacing:.12em;font-variant-numeric:tabular-nums}
.mc .ic{font-size:26px;margin:14px 0 10px;width:50px;height:50px;border-radius:14px;display:grid;place-items:center;background:var(--card2);border:1px solid var(--line2)}
.mc h3{margin:0 0 6px;font-size:18px;letter-spacing:-.01em}
.mc p{margin:0;color:var(--mut);font-size:14px;flex:1}
.mc a{margin-top:16px;font-size:13.5px;font-weight:700}
.tag{display:inline-block;margin-left:8px;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;vertical-align:middle;
background:rgba(148,163,184,.12);color:#94a3b8;border:1px solid var(--line2)}
@media(max-width:1000px){.bento{grid-template-columns:repeat(2,1fr)}}
@media(max-width:600px){.bento{grid-template-columns:1fr}.mc.w{grid-column:auto}}
/* ステップ */
.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;counter-reset:s}
.st{position:relative;background:var(--card);border:1px solid var(--line);border-radius:20px;padding:28px}
.st .n{font-size:44px;font-weight:900;line-height:1;letter-spacing:-.03em;
background:linear-gradient(180deg,var(--blue2),var(--blue3));-webkit-background-clip:text;background-clip:text;color:transparent}
.st h3{margin:16px 0 8px;font-size:19px}.st p{margin:0;color:var(--mut);font-size:14.5px}
.st:not(:last-child):after{content:"➔";position:absolute;right:-13px;top:50%;transform:translateY(-50%);z-index:2;color:var(--blue2);
width:26px;height:26px;border-radius:50%;background:var(--bg);border:1px solid var(--line2);display:grid;place-items:center;font-size:12px}
@media(max-width:820px){.steps{grid-template-columns:1fr}.st:not(:last-child):after{content:"↓";right:auto;left:50%;top:auto;bottom:-13px;transform:translateX(-50%)}}
.ctabox{margin-top:28px;display:flex;gap:12px;flex-wrap:wrap}
/* お知らせ */
.upd{border:1px solid var(--line);border-radius:20px;overflow:hidden;background:var(--card)}
.ui{display:grid;grid-template-columns:110px 90px 1fr;gap:16px;padding:20px 24px;border-top:1px solid var(--line);align-items:baseline}
.ui:first-child{border-top:0}
.ui time{color:var(--mut);font-size:13px;font-variant-numeric:tabular-nums}
.ui .lb{font-size:11px;font-weight:800;letter-spacing:.08em;padding:3px 9px;border-radius:999px;text-align:center;width:max-content;
color:var(--blue2);background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.3)}
.ui .lb.up{color:#a5b4fc;background:rgba(129,140,248,.08);border-color:rgba(129,140,248,.25)}
.ui b{display:block;font-size:15.5px}.ui span.d{color:var(--mut);font-size:14px}
@media(max-width:700px){.ui{grid-template-columns:auto 1fr;gap:6px 12px}.ui>div{grid-column:1/-1}}
/* 最後のCTA */
.final{text-align:center;border:1px solid rgba(59,130,246,.3);border-radius:28px;padding:64px 22px;position:relative;overflow:hidden;
background:radial-gradient(ellipse 60% 80% at 50% 0%,rgba(59,130,246,.22),transparent 70%),var(--card)}
.final h2{margin-top:0}.final .cta{margin-top:26px}
/* フッター */
footer{border-top:1px solid var(--line);padding:56px 0 40px;margin-top:40px;color:var(--mut);font-size:14px}
.fg{display:grid;grid-template-columns:1.4fr 1fr 1fr 1fr;gap:28px}
footer h4{color:var(--text);font-size:13px;margin:0 0 12px;letter-spacing:.06em}
footer ul{list-style:none;margin:0;padding:0;display:grid;gap:8px}footer ul a{color:var(--mut)}footer ul a:hover{color:var(--blue2)}
.copy{margin-top:40px;padding-top:22px;border-top:1px solid var(--line);font-size:12.5px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}
@media(max-width:820px){.fg{grid-template-columns:1fr 1fr}.fg>div:first-child{grid-column:1/-1}}
/* ガイド・ステータス共通 */
.page{padding:64px 0 20px}
.toc{display:flex;flex-wrap:wrap;gap:8px;margin:22px 0 34px}
.toc a{padding:7px 13px;border-radius:999px;border:1px solid var(--line2);color:var(--mut);font-size:13px;background:var(--card)}
.toc a:hover{border-color:var(--blue);color:var(--text)}
.gd{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;margin-bottom:14px;scroll-margin-top:84px}
.gd:target{border-color:var(--blue);box-shadow:0 0 0 3px rgba(59,130,246,.2)}
.gd h3{margin:0 0 6px;display:flex;align-items:center;gap:10px;font-size:19px}
.gd .no{color:var(--blue2);font-size:12px;font-weight:800;letter-spacing:.12em}
.gd p{color:var(--mut);margin:0 0 12px}
.gd ol{margin:0;padding-left:20px}.gd li{margin:4px 0}
code{background:var(--card2);border:1px solid var(--line2);border-radius:6px;padding:1px 6px;font-size:.92em;color:#bfdbfe}
.big{font-size:28px;font-weight:900;display:flex;align-items:center;gap:12px}
.dot{width:14px;height:14px;border-radius:50%;background:#22c55e;box-shadow:0 0 16px #22c55e}
.dot.warn{background:#f59e0b;box-shadow:0 0 16px #f59e0b}.dot.bad{background:#ef4444;box-shadow:0 0 16px #ef4444}
.kv{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:20px}
.kv div{background:var(--card2);border:1px solid var(--line);border-radius:14px;padding:16px}
.kv small{color:var(--mut);display:block;font-size:12px}.kv b{font-size:20px}
@media(max-width:700px){.kv{grid-template-columns:1fr 1fr}}
/* デモ（CSSだけで切り替え） */
.demo{border:1px solid var(--line2);border-radius:18px;overflow:hidden;background:var(--bg2)}
.demo input[type=radio],.demo input.t{position:absolute;opacity:0;pointer-events:none}
.dtabs{display:flex;gap:6px;padding:12px;border-bottom:1px solid var(--line);overflow-x:auto;background:#0a0e14}
.dtabs label{padding:8px 14px;border-radius:10px;color:var(--mut);font-size:13.5px;cursor:pointer;white-space:nowrap;border:1px solid transparent}
.dtabs label:hover{color:var(--text)}
.dp{display:none;padding:18px}
#d1:checked~.dtabs label[for=d1],#d2:checked~.dtabs label[for=d2],#d3:checked~.dtabs label[for=d3]{background:rgba(59,130,246,.14);color:var(--blue2);border-color:rgba(59,130,246,.3);font-weight:700}
#d1:checked~.p1,#d2:checked~.p2,#d3:checked~.p3{display:block}
.dg{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
@media(max-width:820px){.dg{grid-template-columns:1fr}}
.dm{position:relative;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px}
.dm .t{display:flex;justify-content:space-between;align-items:center;font-weight:700}
.dm p{color:var(--mut);font-size:13px;margin:6px 0 12px}
.dm label.sl{cursor:pointer;display:block;width:42px;height:24px}
.dm label.sl .sw{display:block;width:42px;height:24px}.dm label.sl .sw:after{width:18px;height:18px}
.dm input.t:checked+.t label.sl .sw,.dm input.t:checked~.t label.sl .sw{background:var(--blue)}
.dm input.t:checked~.t label.sl .sw:after{left:21px;background:#fff}
.bd{font-size:11.5px;font-weight:700;padding:3px 10px;border-radius:999px;background:rgba(148,163,184,.12);color:#94a3b8}
.bd:after{content:"無効"}
.dm input.t:checked~.ft .bd{background:rgba(59,130,246,.15);color:var(--blue2)}
.dm input.t:checked~.ft .bd:after{content:"有効"}
.ft{display:flex;justify-content:space-between;align-items:center;font-size:12.5px}
.ff label{display:block;color:var(--mut);font-size:12.5px;margin:12px 0 5px}
.ff select,.ff input[type=text],.ff textarea{width:100%;background:var(--bg2);color:var(--text);border:1px solid var(--line2);border-radius:10px;padding:10px;font:inherit;font-size:14px}
.ff .b{margin-top:14px;display:inline-block;padding:10px 18px;border-radius:10px;background:var(--blue);color:#fff;font-weight:700;font-size:14px;border:0;opacity:.6}
/* 所有者用のお知らせ投稿 */
.own{margin-bottom:16px;border:1px dashed rgba(59,130,246,.5);border-radius:20px;padding:20px;background:rgba(59,130,246,.05)}
.own h3{margin:0 0 4px;font-size:16px}.own .mut{color:var(--mut);font-size:13px;margin:0 0 12px}
.own form{display:grid;grid-template-columns:150px 150px 1fr;gap:10px}
.own input,.own select,.own textarea{width:100%;background:var(--bg2);color:var(--text);border:1px solid var(--line2);border-radius:10px;padding:10px;font:inherit;font-size:14px}
.own textarea,.own .full{grid-column:1/-1}
.own button,.del button{cursor:pointer;font:inherit;font-weight:700;border-radius:10px;border:0}
.own button{padding:11px 18px;background:var(--blue);color:#fff;width:max-content}
.del{margin:0}.del button{padding:5px 10px;font-size:12px;background:rgba(239,68,68,.12);color:#fca5a5;border:1px solid rgba(239,68,68,.3)}
.ui.adm{grid-template-columns:110px 90px 1fr auto}
@media(max-width:700px){.own form{grid-template-columns:1fr 1fr}.ui.adm{grid-template-columns:auto 1fr auto}}
.note{margin:14px 0 0;padding:12px 14px;border-radius:12px;background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.25);color:#bfdbfe;font-size:13.5px}
"""


def register(app, ctx) -> SimpleNamespace:
    """ctx: bot, client_id, status_info(), limiter, invite_url(省略可)"""

    def invite_url() -> str:
        custom = getattr(ctx, "invite_url", None)
        if custom:
            return custom() if callable(custom) else custom
        return (f"https://discord.com/oauth2/authorize?client_id={ctx.client_id}"
                "&permissions=8&scope=bot+applications.commands")

    def bot_avatar() -> str:
        try:
            u = ctx.bot.user
            return str(u.display_avatar.replace(size=64).url) if u else ""
        except Exception:
            return ""

    JST = timezone(timedelta(hours=9))
    LABELS = ["NEW", "UPDATE", "INFO", "FIX", "MAINTENANCE"]

    def load_updates() -> list[dict]:
        try:
            data = ctx.load_updates() or {}
        except Exception:
            data = {}
        if "items" not in data:  # まだ一度も投稿していない → 初期値を表示
            return [{"id": f"d{i}", "date": dt, "label": lb, "title": t, "body": d} for i, (dt, lb, t, d) in enumerate(UPDATES)]
        return list(data["items"])

    async def owner_session(request: Request) -> dict | None:
        """所有者専用ダッシュボード（/dashboard）にログイン中の所有者なら、そのセッションを返す。"""
        fn = getattr(ctx, "owner_session", None)
        if fn is None:
            return None
        try:
            return await fn(request)
        except Exception:
            return None

    def respond(doc: str, status: int = 200) -> HTMLResponse:
        r = HTMLResponse(doc, status_code=status)
        r.headers["Content-Security-Policy"] = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' https://cdn.discordapp.com data:; "
                                                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        r.headers["Cache-Control"] = "public, max-age=300"
        return r

    def shell(title: str, body: str, desc: str = "") -> str:
        av = bot_avatar()
        logo = f'<img src="{_e(av)}" alt="">' if av else '<span class="ph">⛩️</span>'
        inv = _e(invite_url())
        d = _e(desc or "荒らしや不正からサーバーを守り、複雑な設定を整え、活発な会話とコミュニティを広げる。すべてを一つのBotで。")
        return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#07090d">
<title>{_e(title)}</title><meta name="description" content="{d}">
<meta property="og:title" content="{_e(title)}"><meta property="og:description" content="{d}"><meta property="og:type" content="website">
<style>{CSS}</style></head><body>
<header><div class="wrap"><a class="logo" href="/">{logo}霊夢BOT</a>
<nav><a href="/#features">機能</a><a href="/#howto">導入ガイド</a><a href="/demo">デモ</a><a href="/#updates">お知らせ</a>
<a href="/guild-dashboard">ダッシュボード</a><a class="hbtn" href="{inv}" target="_blank" rel="noopener">追加する ↗</a></nav></div></header>
{body}
<footer><div class="wrap"><div class="fg">
<div><div class="logo" style="margin-bottom:12px">{logo}霊夢BOT</div>
<div>守り、整え、語り合う。<br>Discordサーバーの運営を、ひとつのBotで。</div></div>
<div><h4>プロダクト</h4><ul><li><a href="/#features">機能一覧</a></li><li><a href="/guide">導入ガイド</a></li><li><a href="/demo">操作デモ</a></li><li><a href="/#updates">お知らせ</a></li></ul></div>
<div><h4>リンク</h4><ul><li><a href="/guild-dashboard">ダッシュボード ↗</a></li><li><a href="/status">稼働状況 ↗</a></li>
<li><a href="{SUPPORT_URL}" target="_blank" rel="noopener">サポート ↗</a></li></ul></div>
<div><h4>規約</h4><ul><li><a href="/terms">利用規約</a></li><li><a href="/privacy">プライバシーポリシー</a></li></ul></div>
</div><div class="copy"><span>© 霊夢BOT ・ <a href="/dashboard/login" style="color:var(--mut)">所有者ログイン</a></span><span>Discord は Discord Inc. の商標です。本サービスは Discord Inc. とは関係ありません。</span></div></div></footer>
</body></html>"""

    # ---------------- トップページ ----------------
    def mock_html() -> str:
        cards = [("🛡️ Nuke対策", "大量削除・連続BANを自動処罰", True, True), ("🔍 OCR画像検閲", "画像内の禁止ワードを検出", True, False),
                 ("📈 レベル", "会話で経験値とランク", True, False), ("📺 YouTube Push", "新着動画を即時通知", True, True),
                 ("🌐 翻訳", "国旗リアクションで翻訳", False, False), ("🎉 イベント", "日程投票とリマインド", True, False)]
        mm = "".join(f'<div class="mm{" w" if w else ""}"><div class="t">{_e(n)}<span class="sw{" on" if on else ""}"></span></div><p>{_e(p)}</p></div>'
                     for n, p, on, w in cards)
        side = "".join(('<div class="on">' if i == 0 else "<div>") + f"{t}</div>" for i, t in enumerate(
            ["🧩 モジュール", "🔐 認証", "📝 ログ・通知", "🛡️ セキュリティ", "🎉 イベント・投票", "🧰 便利ツール", "📺 配信・VC"]))
        return (f'<div class="mock"><div class="bar"><i></i><i></i><i></i><span>🔒 霊夢BOT / ダッシュボード</span></div>'
                f'<div class="in"><div class="side">{side}</div><div class="main">'
                f'<div class="sh">🧩 モジュール <small>・ ワンタップで切り替え</small></div><div class="mg">{mm}</div></div></div></div>')

    async def landing(request: Request) -> HTMLResponse:
        sess = await owner_session(request)
        inv = _e(invite_url())
        try:
            info = ctx.status_info()
        except Exception:
            info = {}
        guilds = info.get("guilds")
        stats = (f'<div class="stats"><span><b>{len(MODULES)}+</b>モジュール</span>'
                 + (f'<span><b>{_e(str(guilds))}</b>サーバーで稼働中</span>' if guilds else "")
                 + '<span><b>Web</b>ダッシュボードで設定</span></div>')
        pillars = "".join(f'<div class="pl"><div class="k"><span>{ic}</span>{_e(k)}</div><p>{_e(t)}</p></div>' for k, ic, t in PILLARS)
        wide = {"ocr", "youtube", "html"}
        mods = "".join(
            f'<div class="mc{" w" if key in wide else ""}" id="m-{key}"><div class="no">{i:02d}</div><div class="ic">{ic}</div>'
            f'<h3>{_e(name)}{f"<span class=tag>{_e(state)}</span>" if state else ""}</h3><p>{_e(desc)}</p>'
            f'<a href="/guide#{key}">使い方を見る →</a></div>'
            for i, (key, ic, name, desc, _u, _t, state) in enumerate(MODULES, 1))
        steps = "".join(f'<div class="st"><div class="n">{n}</div><h3>{_e(t)}</h3><p>{_e(d)}</p></div>' for n, t, d in STEPS)
        items = load_updates()
        csrf = _e(sess["csrf"]) if sess else ""

        def del_btn(it):
            if not sess:
                return ""
            return (f'<form class="del" method="post" action="/home/updates"><input type="hidden" name="csrf" value="{csrf}">'
                    f'<input type="hidden" name="op" value="delete"><input type="hidden" name="id" value="{_e(it.get("id", ""))}">'
                    f'<button type="submit">削除</button></form>')
        upd = "".join(f'<div class="ui{" adm" if sess else ""}"><time>{_e(it.get("date", ""))}</time>'
                      f'<span class="lb{" up" if it.get("label") != "NEW" else ""}">{_e(it.get("label", ""))}</span>'
                      f'<div><b>{_e(it.get("title", ""))}</b><span class="d">{_e(it.get("body", ""))}</span></div>{del_btn(it)}</div>'
                      for it in items[:10]) or '<div class="ui"><div class="d" style="color:var(--mut)">お知らせはまだありません。</div></div>'
        own = ""
        if sess:
            today = datetime.now(JST).strftime("%Y.%m.%d")
            own = (f'<div class="own"><h3>✍️ お知らせを投稿（所有者のみ表示）</h3>'
                   f'<p class="mut">{_e(sess.get("name", ""))} としてログイン中です。投稿すると、すぐにこのページへ反映されます。</p>'
                   f'<form method="post" action="/home/updates"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="op" value="add">'
                   f'<input type="text" name="date" value="{today}" maxlength="20" aria-label="日付">'
                   f'<select name="label" aria-label="ラベル">{"".join(f"<option>{x}</option>" for x in LABELS)}</select>'
                   f'<input type="text" name="title" maxlength="80" required placeholder="タイトル" aria-label="タイトル">'
                   f'<textarea name="body" rows="3" maxlength="400" placeholder="本文（400文字まで）" aria-label="本文"></textarea>'
                   f'<button type="submit">投稿する</button></form></div>')
        body = f"""
<div class="hero"><div class="wrap">
<span class="pill"><i></i>13の新モジュールを追加しました</span>
<h1>守り、整え、<br><span class="b">語り合う。</span></h1>
<p class="lead">荒らしや不正からサーバーを守り、複雑な設定を整え、<br>活発な会話とコミュニティを広げる。すべてを一つのBotで。</p>
<div class="cta"><a class="btn pri" href="{inv}" target="_blank" rel="noopener">Discordに追加する ↗</a>
<a class="btn sec" href="/demo">ログインせずに試す ↗</a><a class="btn ghost" href="/guild-dashboard">ダッシュボード ↗</a></div>
{stats}{mock_html()}</div></div>

<section style="padding-top:40px"><div class="wrap"><div class="pillars">{pillars}</div></div></section>

<section id="features"><div class="wrap"><div class="head"><div class="eyebrow">FEATURES</div>
<h2>運営に必要なことを、<br>ひとつずつ、確実に。</h2>
<p class="sub">必要なモジュールだけをオンにして使えます。設定はすべてダッシュボードから。</p></div>
<div class="bento">{mods}</div></div></section>

<section id="howto"><div class="wrap"><div class="head"><div class="eyebrow">HOW TO USE</div>
<h2>はじめの設定から、<br>日々の運営まで。</h2><p class="sub">3つのステップで、すぐに使い始められます。</p></div>
<div class="steps">{steps}</div>
<div class="ctabox"><a class="btn pri" href="{inv}" target="_blank" rel="noopener">Discordに追加する ↗</a><a class="btn sec" href="/guide">導入ガイドを読む</a></div></div></section>

<section id="updates"><div class="wrap"><div class="head"><div class="eyebrow">LATEST UPDATES</div>
<h2>お知らせ</h2><p class="sub">最新の機能追加やアップデートの情報です。</p></div>
{own}<div class="upd">{upd}</div></div></section>

<section style="padding-top:20px"><div class="wrap"><div class="final">
<div class="eyebrow">GET STARTED</div><h2>あなたのサーバーにも、霊夢BOTを。</h2>
<p class="sub" style="margin:0 auto">導入は無料。困ったときは、サポートサーバーでお気軽にご相談ください。</p>
<div class="cta"><a class="btn pri" href="{inv}" target="_blank" rel="noopener">Discordに追加する ↗</a>
<a class="btn ghost" href="{SUPPORT_URL}" target="_blank" rel="noopener">サポートサーバー ↗</a></div></div></div></section>"""
        r = respond(shell("霊夢BOT ｜ 守り、整え、語り合う。", body))
        r.headers["Cache-Control"] = "no-store" if sess else "private, no-cache"  # 古い画面が出ないように毎回確認
        return r

    # ---------------- お知らせの投稿・削除（所有者のみ） ----------------
    @app.post("/home/updates")
    @ctx.limiter.limit("20/minute")
    async def post_updates(request: Request):
        sess = await owner_session(request)
        body = (await request.body())[:6000].decode("utf-8", "replace")
        f = {k: v[0] for k, v in parse_qs(body).items()}
        if not sess or not secrets.compare_digest(f.get("csrf", ""), sess.get("csrf", "")):
            strike = getattr(ctx, "guard_web_strike", None)
            if strike:
                try:
                    strike(request, "home_forbidden", "/home/updates")
                except Exception:
                    pass
            return respond(shell("権限がありません ｜ 霊夢BOT", '<div class="page"><div class="wrap"><h2>権限がありません</h2>'
                                 '<p class="sub">お知らせを投稿できるのは、BOTの所有者だけです。</p></div></div>'), 403)
        items = load_updates()
        if f.get("op") == "add":
            title = (f.get("title") or "").strip()[:80]
            if title:
                label = f.get("label") if f.get("label") in LABELS else "INFO"
                date = (f.get("date") or "").strip()[:20] or datetime.now(JST).strftime("%Y.%m.%d")
                items.insert(0, {"id": uuid.uuid4().hex[:12], "date": date, "label": label, "title": title,
                                 "body": (f.get("body") or "").strip()[:400], "by": sess.get("uid"), "t": int(time.time())})
        elif f.get("op") == "delete":
            items = [it for it in items if it.get("id") != f.get("id")]
        ctx.save_updates({"items": items[:30]})
        return RedirectResponse("/#updates", status_code=303)

    # ---------------- 導入ガイド ----------------
    @app.api_route("/guide", methods=["GET", "HEAD"])
    @ctx.limiter.limit("60/minute")
    async def guide(request: Request):
        inv = _e(invite_url())
        toc = "".join(f'<a href="#{k}">{ic} {_e(n)}</a>' for k, ic, n, *_ in MODULES)
        steps = "".join(f'<div class="gd"><div class="no">STEP {n}</div><h3>{_e(t)}</h3><p style="margin:0">{_e(d)}</p></div>' for n, t, d in STEPS)
        items = "".join(
            f'<div class="gd" id="{k}"><div class="no">{i:02d}</div><h3>{ic} {_e(n)}{f"<span class=tag>{_e(st)}</span>" if st else ""}</h3>'
            f'<p>{_e(d)}</p><ol>{"".join(f"<li>{_e(u)}</li>" for u in us)}</ol></div>'
            for i, (k, ic, n, d, us, _t, st) in enumerate(MODULES, 1))
        body = f"""<div class="page"><div class="wrap">
<div class="eyebrow">GUIDE</div><h2>導入ガイド</h2>
<p class="sub">サーバーへの追加から、各モジュールの使い方までをまとめています。コマンドの一覧は、Discordで <code>/help</code> を送ると確認できます。</p>
<div class="ctabox" style="margin-bottom:40px"><a class="btn pri" href="{inv}" target="_blank" rel="noopener">Discordに追加する ↗</a><a class="btn sec" href="/demo">操作デモを見る</a></div>
<h3 style="font-size:22px">はじめの設定</h3>{steps}
<h3 style="font-size:22px;margin-top:44px">モジュールの使い方</h3><div class="toc">{toc}</div>{items}
<div class="note">ダッシュボードの設定は、「サーバー管理」または「管理者」権限を持つメンバーだけが変更できます。</div>
</div></div>"""
        return respond(shell("導入ガイド ｜ 霊夢BOT", body))

    # ---------------- 操作デモ ----------------
    @app.api_route("/demo", methods=["GET", "HEAD"])
    @ctx.limiter.limit("60/minute")
    async def demo(request: Request):
        def dm(i, ic, name, desc, on=False):
            return (f'<div class="dm"><input class="t" type="checkbox" id="s{i}"{" checked" if on else ""}>'
                    f'<div class="t">{ic} {_e(name)}<label class="sl" for="s{i}" aria-label="{_e(name)}を切り替え"><span class="sw"></span></label></div>'
                    f'<p>{_e(desc)}</p><div class="ft"><span class="bd"></span><span style="color:var(--blue2)">設定を開く ›</span></div></div>')
        mods = [("🛡️", "Nuke対策", "大量削除・連続BANを検知して自動処罰", True), ("🚧", "Raid対策", "大量参加を検知して自動で守る", True),
                ("🔍", "OCR画像検閲", "画像内の禁止ワードや招待リンクを検出", False), ("📈", "レベル・ランク", "会話とVCで経験値が貯まる", True),
                ("🌐", "翻訳", "国旗リアクションで翻訳", False), ("❓", "FAQ", "よくある質問をパネルで回答", False),
                ("📺", "YouTube Push", "新着動画・配信を即時通知", True), ("⏰", "自動メッセージ", "決まった時刻に自動投稿", False),
                ("🎧", "VC連動公開", "VC参加中だけ聞き専を表示", False)]
        p1 = "".join(dm(i, ic, n, d, on) for i, (ic, n, d, on) in enumerate(mods))
        sel = lambda *o: "<select>" + "".join(f"<option>{_e(x)}</option>" for x in o) + "</select>"
        p2 = (f'<div class="dg" style="grid-template-columns:1fr 1fr"><div class="dm ff"><div class="t">🎉 イベントを作成</div>'
              f'<label>タイトル</label><input type="text" value="週末のゲーム大会">'
              f'<label>開始日時</label><input type="text" value="2026-11-14 21:00">'
              f'<label>告知するチャンネル</label>{sel("#お知らせ", "#雑談")}'
              f'<label>リマインド</label>{sel("30分前", "1時間前", "しない")}<span class="b">作成する</span></div>'
              f'<div class="dm ff"><div class="t">📊 アンケート</div><label>質問</label><input type="text" value="次のイベント、何がいい？">'
              f'<label>選択肢（1行に1つ）</label><textarea rows="4">マイクラ\nスプラ\nAmong Us</textarea>'
              f'<label>締め切り</label><input type="text" value="2026-11-12 23:00"><span class="b">作成する</span></div></div>')
        p3 = (f'<div class="dg" style="grid-template-columns:1fr 1fr"><div class="dm ff"><div class="t">📣 代理送信</div>'
              f'<label>送信方法</label>{sel("埋め込み", "テキスト", "Webhook", "Webhook（埋め込み）")}'
              f'<label>本文</label><textarea rows="4">メンテナンスのお知らせ：今夜22時から30分ほど停止します。</textarea>'
              f'<label>メンション</label>{sel("しない", "@everyone", "@お知らせ")}<span class="b">送信する</span></div>'
              f'<div class="dm ff"><div class="t">📈 定期レポート</div><label>送信先</label>{sel("#運営", "#ログ")}'
              f'<label>曜日</label>{sel("月曜日", "金曜日", "日曜日")}<label>時刻</label><input type="text" value="09:00">'
              f'<span class="b">保存する</span></div></div>')
        body = f"""<div class="page"><div class="wrap">
<div class="eyebrow">DEMO</div><h2>ログインせずに、試してみる。</h2>
<p class="sub">実際のダッシュボードと同じ画面で、操作の雰囲気を確かめられます。スイッチは押すと切り替わります（デモのため保存はされません）。</p>
<div class="demo" style="margin-top:28px">
<input type="radio" name="dt" id="d1" checked><input type="radio" name="dt" id="d2"><input type="radio" name="dt" id="d3">
<div class="dtabs"><label for="d1">🧩 モジュール</label><label for="d2">🎉 イベント・投票</label><label for="d3">🧰 便利ツール</label></div>
<div class="dp p1"><div class="dg">{p1}</div></div><div class="dp p2">{p2}</div><div class="dp p3">{p3}</div></div>
<div class="note">本物のダッシュボードでは、Discordでログインすると、あなたが管理しているサーバーの設定をこの画面から変更できます。</div>
<div class="ctabox"><a class="btn pri" href="/guild-dashboard">ダッシュボードにログイン ↗</a><a class="btn sec" href="{_e(invite_url())}" target="_blank" rel="noopener">Discordに追加する ↗</a></div>
</div></div>"""
        return respond(shell("操作デモ ｜ 霊夢BOT", body))

    # ---------------- 稼働状況（公開用・内部情報は出さない） ----------------
    @app.api_route("/status", methods=["GET", "HEAD"])
    @ctx.limiter.limit("60/minute")
    async def status(request: Request):
        try:
            info = ctx.status_info()
        except Exception:
            info = {"text": "⚪ 確認できません", "code": "unknown"}
        code = info.get("code", "ok")
        cls = "" if code in ("ok", "recovered") else ("bad" if code in ("manual", "critical", "unknown") else "warn")
        label = {"ok": "すべて正常に稼働しています", "recovered": "すべて正常に稼働しています（復旧済み）",
                 "minor": "一部の機能に影響が出ています（自動で対処済み）", "healing": "復旧作業中です",
                 "critical": "障害が発生しています", "manual": "障害が発生しています（復旧作業中）"}.get(code, "状況を確認できません")
        bot_on = "オンライン" in str(info.get("bot", "")) and "オフライン" not in str(info.get("bot", ""))
        body = f"""<div class="page"><div class="wrap">
<div class="eyebrow">STATUS</div><h2>稼働状況</h2>
<div class="gd" style="margin-top:22px"><div class="big"><span class="dot {cls}"></span>{_e(label)}</div>
<div class="kv"><div><small>Bot</small><b>{"オンライン" if bot_on else "確認中"}</b></div>
<div><small>応答速度</small><b>{_e(str(info.get("ping", "-")))}</b></div>
<div><small>稼働時間</small><b>{_e(str(info.get("uptime", "-")))}</b></div>
<div><small>導入サーバー</small><b>{_e(str(info.get("guilds", "-")))}</b></div></div></div>
<div class="note">障害やメンテナンスの詳しい情報は、<a href="{SUPPORT_URL}" target="_blank" rel="noopener">サポートサーバー</a>でお知らせしています。</div>
</div></div>"""
        r = respond(shell("稼働状況 ｜ 霊夢BOT", body))
        r.headers["Cache-Control"] = "no-store"
        return r

    return SimpleNamespace(landing=landing)
