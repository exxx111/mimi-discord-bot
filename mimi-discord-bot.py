"""
マッキー Discordボット - かわいい女の子キャラクター風 会話＆見守りボット

使い方:
  1. pip install discord.py
  2. Discord Developer Portal で Bot を作り、MESSAGE CONTENT INTENT を ON にする
  3. 環境変数にトークンを入れる
       export DISCORD_TOKEN="あなたのトークン"
     Windows (PowerShell):  $env:DISCORD_TOKEN="あなたのトークン"
  4. python mimi-discord-bot.py
  5. Bot の招待URLでサーバーに入れて、メンション or DM or 「マッキー」って呼んでね

Render等のWebサービスで動かす場合:
  環境変数 PORT が設定されていれば、ヘルスチェック用の小さな
  HTTPサーバーが別スレッドで自動起動します。ローカルでは不要です。

コマンドは無し（全部削除済み）。
会話はメンション、DM、文に「マッキー」を含めると返事するよ。
他のボットが話しかけてきたら、ボット同士でも会話するよ（無限ループ防止つき）。
返事はすぐ送らず、「入力中…」を出して少し考えてから送るよ。
サーバー内では全ての投稿を1通ごとにログに記録するよ。スパムっぽい投稿（連投・同じ文の
繰り返し・リンク連投・大量メンション・招待リンク・同じ文字や絵文字の連打・改行荒らし・
指定した監視ワード）は🚨検知つきで記録するよ（消したりBANはしないよ）。
ログは「mimi-log」「みみログ」「spam-log」「警告botログ」「マッキーログ」という名前のチャンネルがあればそこにも流すよ。
"""

import os
import re
import time
import random
import asyncio
import threading
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord.ext import commands

# ---------- キャラクター設定：マッキー ----------
MIMI_NAME = "マッキー"
MIMI_PROFILE = (
    "サーバーを見守る「マッキー」だよ。"
    "スパムや監視ワードを見張って、ログに残したり警告したりするよ。おしゃべりもできるよ。"
)

# ---------- keep-alive（Render等のWebサービス用） ----------
# 環境変数 PORT が設定されているときだけ、小さなHTTPサーバーを
# 別スレッドで起動してヘルスチェックに応答する。
# PORT が無いローカル実行では何もしない（従来通り）。
class _HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"mimi bot is running"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):  # ログを静かにする
        pass


def start_keep_alive() -> bool:
    """PORT が設定されていればHTTPサーバーを別スレッドで起動する。"""
    port_str = os.environ.get("PORT")
    if not port_str:
        return False
    try:
        port = int(port_str)
    except ValueError:
        print(f"PORT の値が不正です: {port_str!r}（keep-aliveは起動しません）")
        return False
    server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"keep-alive サーバー起動: port {port}")
    return True


# ---------- 常時稼働の維持（スリープ防止＆24時間ごとの自動再起動） ----------
# Render無料プランは、しばらく外部からのアクセスが無いとスリープしてしまう。
# そこで10分ごとに自分のURLへアクセスして眠らないようにし、
# 起動から24時間たったらプロセスを終了してRenderに立ち上げ直させる
# （終了するとRenderが自動で再起動する。再起動でメモリ内のログはリセットされる）。
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://mimi-discord-bot-6ol5.onrender.com")
PING_INTERVAL = 600          # 自己アクセスの間隔（秒）
RESTART_AFTER = 24 * 3600    # 自動再起動までの時間（秒）
_started_at = time.time()
_maintenance_started = False


def _self_ping() -> None:
    try:
        req = urllib.request.Request(SELF_URL, headers={"User-Agent": "mimi-keepalive"})
        with urllib.request.urlopen(req, timeout=30) as res:
            res.read()
    except Exception:
        pass


async def _maintenance_loop() -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL)
        await asyncio.to_thread(_self_ping)
        if time.time() - _started_at >= RESTART_AFTER:
            print("起動から24時間たったので、自動で再起動します", flush=True)
            await asyncio.sleep(1)
            os._exit(0)  # プロセス終了 → Renderが自動で再起動する


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
# スラッシュコマンドは全削除済み。起動時の sync() で Discord 側の登録も消す。
_tree_synced = False
# ボット同士の会話: チャンネルごとの連続応答カウント（無限ループ防止）
BOT_TALK_MAX = 5
_bot_talk_state: dict[int, dict] = {}

# ユーザーごとの状態
user_names: dict[int, str] = {}


async def send_thoughtful_reply(message: discord.Message, reply: str, source_text: str = "") -> None:
    """すぐ返さず、「入力中…」を出して少し考えてから返事する。

    考える時間は返信の長さで変わる（長い返事ほど少し長く考える）。
    質問っぽい文には、ちょっぴり長めに考える。
    """
    think = 1.2 + len(reply) * 0.05 + random.uniform(0, 0.8)
    if "？" in source_text or "?" in source_text or "なに" in source_text or "何" in source_text or "どう" in source_text:
        think += 0.8
    think = min(think, 5.0)
    try:
        async with message.channel.typing():
            await asyncio.sleep(think)
    except Exception:
        await asyncio.sleep(min(think, 2.0))
    await message.channel.send(reply)


# ---------- 会話 ----------
GREETINGS = ["こんにちは", "こんばんは", "おはよう", "やっほー", "hello", "hi"]
KEYWORD_REPLIES = [
    (["疲れ", "つかれ"], ["おつかれさま…！少し休んでね？マッキーがそばにいるよ。", "がんばったね。えらいえらい♪"]),
    (["ありがとう"], ["どういたしまして♪ うれしいな〜", "えへへ、いつでも呼んでね！"]),
    (["かわいい", "可愛い"], ["えっ…ほんと？ありがとう、照れちゃう…！", "えへへ、もっと言ってもいいんだよ？"]),
    (["好き"], ["マッキーもあなたのこと好きだよ！", "わ〜！うれしい言葉、ありがとう♪"]),
    (["おやすみ"], ["おやすみなさい。いい夢見てね…♪", "おやすみ〜、また明日話そうね。"]),
    (["さみしい", "寂しい", "つらい", "悲しい"], ["大丈夫、マッキーがいるよ。ゆっくり話そ？", "無理しないでね。ここにいるからね。"]),
    (["お腹", "おなか", "ごはん", "ご飯"], ["ごはんの話？マッキーは甘いもの大好きだよ〜", "美味しいもの食べると元気出るよね！何が好き？"]),
    (["天気"], ["天気の話だね。外に出るなら傘が必要かチェックしてね！", "いい天気だといいね〜お散歩したいな♪"]),
]

FALLBACKS = [
    "うんうん、それでそれで？もっと聞かせて♪",
    "なるほど〜！面白いね。あなたともっとお話ししたいな。",
    "えへへ、そうなんだ！もっと聞かせて♪",
    "わかる気がする…！マッキー、ちゃんと聞いてるよ。",
    "そうだね♪ 今日はどんな一日だった？",
    "お話ししてくれてありがとう！また声かけてね♪",
]


def chat_reply(user_id: int, text: str) -> str | None:
    t = text.strip()
    low = t.lower()
    # 名前を覚える: 「名前は〇〇」「〇〇って呼んで」
    m = re.search(r"(?:名前は|なまえは)\s*([^\s、。！!？?]{1,12})", t) or re.search(r"([^\s、。！!？?]{1,12})って呼んで", t)
    if m:
        user_names[user_id] = m.group(1)
        return f"わかった！これからは **{m.group(1)}** って呼ぶね♪ 覚えたよ！"
    if "名前" in t and ("覚えて" in t or "なんだっけ" in t or "何だっけ" in t):
        name = user_names.get(user_id)
        return f"覚えてるよ、**{name}** でしょ♪" if name else "まだ聞いてないかも…「名前は〇〇」って教えてね！"
    if any(g in low for g in GREETINGS):
        name = user_names.get(user_id)
        prefix = f"{name}、" if name else ""
        return random.choice([f"{prefix}やっほー！マッキーだよ♪ 今日も話せてうれしいな〜", f"{prefix}こんにちは！お話ししよ？"])
    if "自己紹介" in t or "誰" in t and "マッキー" in t:
        return f"わたしは{MIMI_PROFILE}"
    for keywords, replies in KEYWORD_REPLIES:
        if any(k in t for k in keywords):
            return random.choice(replies)
    if t.endswith("？") or t.endswith("?"):
        return random.choice(["うーん、マッキー的にはそう思うな〜。あなたはどう思う？", "いい質問だね！マッキーも気になる…一緒に考えよ♪", "それはね…えへへ、秘密にしたい気分♪ 冗談だよ、もうちょっと教えて？"])
    return random.choice(FALLBACKS)


# ---------- スパム監視＆ログ ----------
# サーバー内の投稿を見張って、スパムっぽい動きを見つけたらログに残す。
# メッセージの削除やBANはしない（検知と記録だけ）。
JST = timezone(timedelta(hours=9))

# 判定のしきい値（ガチガチ強化版）
BURST_COUNT = 4          # この件数を
BURST_WINDOW = 5         # この秒数以内に送ったら「連投」
REPEAT_COUNT = 3         # 同じ文をこの回数
REPEAT_WINDOW = 60       # この秒数以内に送ったら「繰り返し」
LINK_COUNT = 2           # リンク付き投稿をこの件数
LINK_WINDOW = 60         # この秒数以内に送ったら「リンク連投」
MANY_LINKS_IN_ONE = 3    # 1通にこれ以上のURLがあれば即検知
MANY_MENTIONS = 4        # 1通にこれ以上のメンションがあれば即検知
REPEAT_CHAR = 20         # 同じ文字がこれ以上連続したら即検知
MANY_NEWLINES = 15       # 1通の改行がこれ以上なら即検知
MANY_EMOJIS = 20         # 1通の絵文字がこれ以上なら即検知
FLAG_COOLDOWN = 30       # 同じ人を連続で記録しない間隔（秒）
LOG_KEEP = 300           # ログの最大保持件数
LOG_CHANNEL_NAMES = ["mimi-log", "みみログ", "spam-log", "警告botログ", "マッキーログ"]

# (guild_id, user_id) -> deque[(時刻, 正規化した文, リンク数)]
_recent_msgs: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=30))
# (guild_id, user_id) -> 最後に記録した時刻
_last_flagged: dict[tuple[int, int], float] = {}
# ログ（新しいものが末尾）。kind="msg" は通常の投稿、kind="spam" はスパム検知
log_entries: list[dict] = []

_URL_RE = re.compile(r"https?://|www\.|\.com|\.jp|\.net|\.io", re.IGNORECASE)
_INVITE_RE = re.compile(r"discord\.gg/|discord(app)?\.com/invite/", re.IGNORECASE)
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿]")

# 監視ワード：これを含む投稿は1通でも即ログ対象（ユーザーの指定リスト）
WATCH_WORDS = [
    "うお", "どわー", "きちー", "きちがい", "しね", "死ね",
    "あほ", "アホ", "障害", "障がい", "生姜",
]
WATCH_EMOJIS = ["🫚", "🤓"]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _detect_spam(message: discord.Message) -> str | None:
    """スパムっぽければ理由を返す。違えば None。"""
    now = time.time()
    key = (message.guild.id, message.author.id)
    history = _recent_msgs[key]
    content = message.content or ""
    url_count = len(_URL_RE.findall(content))
    history.append((now, _normalize(content), url_count))

    # 1通で即検知するタイプ
    norm_text = _normalize(content)
    for w in WATCH_WORDS:
        if w in norm_text:
            return f"監視ワード「{w}」を検知"
    for e in WATCH_EMOJIS:
        if e in content:
            return f"監視絵文字「{e}」を検知"
    if _INVITE_RE.search(content):
        return "Discord招待リンクの投稿"
    if url_count >= MANY_LINKS_IN_ONE:
        return f"1通にリンクが{url_count}個"
    if len(message.mentions) >= MANY_MENTIONS:
        return f"1通にメンションが{len(message.mentions)}人"
    if re.search(r"(.)\1{" + str(REPEAT_CHAR - 1) + r",}", content):
        return f"同じ文字の{REPEAT_CHAR}連続以上の連打"
    if content.count("\n") >= MANY_NEWLINES:
        return f"改行が{MANY_NEWLINES}個以上の荒らし"
    emoji_count = len(_EMOJI_RE.findall(content))
    if emoji_count >= MANY_EMOJIS:
        return f"絵文字が{emoji_count}個の連打"

    # 一定時間内の履歴で判定
    recent_all = [h for h in history if now - h[0] <= REPEAT_WINDOW]
    if len([h for h in history if now - h[0] <= BURST_WINDOW]) >= BURST_COUNT:
        return f"{BURST_WINDOW}秒以内に{BURST_COUNT}件以上の連投"
    norm = _normalize(content)
    if norm and len([h for h in recent_all if h[1] == norm]) >= REPEAT_COUNT:
        return f"同じ文を{REPEAT_WINDOW}秒以内に{REPEAT_COUNT}回"
    if len([h for h in recent_all if h[2] > 0]) >= LINK_COUNT:
        return f"リンク付き投稿を{LINK_WINDOW}秒以内に{LINK_COUNT}件"
    return None


def _find_log_channel(guild: discord.Guild):
    for name in LOG_CHANNEL_NAMES:
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch is not None:
            return ch
    return None


async def _post_log(guild: discord.Guild, text: str, exclude_channel_id: int | None = None) -> None:
    """ログ用チャンネルがあれば、そこにログを流す。"""
    log_ch = _find_log_channel(guild)
    if log_ch is None or log_ch.id == exclude_channel_id:
        return
    try:
        await log_ch.send(text)
    except discord.HTTPException:
        pass


def _base_entry(message: discord.Message, kind: str) -> dict:
    return {
        "kind": kind,
        "time": datetime.now(JST),
        "guild": message.guild.name,
        "channel": getattr(message.channel, "name", "DM"),
        "user": str(message.author),
        "user_id": message.author.id,
        "reason": "",
        "content": (message.content or "")[:100],
        "jump_url": message.jump_url,
    }


async def check_and_log_spam(message: discord.Message) -> bool:
    """スパムっぽければログに残す。検知したら True（管理者も含め全員対象）。
    ログ用チャンネルがあればそこにも流す。"""
    if message.guild is None:
        return False
    reason = _detect_spam(message)
    if not reason:
        return False
    key = (message.guild.id, message.author.id)
    now = time.time()
    if now - _last_flagged.get(key, 0) < FLAG_COOLDOWN:
        return True  # 検知はしたが、連続記録を間引いた
    _last_flagged[key] = now

    entry = _base_entry(message, "spam")
    entry["reason"] = reason
    log_entries.append(entry)
    del log_entries[:-LOG_KEEP]
    print(f"[検知] {entry['time']:%Y-%m-%d %H:%M:%S} {entry['guild']} #{entry['channel']} {entry['user']}: {reason}", flush=True)

    await _post_log(
        message.guild,
        "🚨 **ログ**\n"
        f"・時刻: {entry['time']:%Y-%m-%d %H:%M:%S}\n"
        f"・ユーザー: {entry['user']}\n"
        f"・場所: #{entry['channel']}\n"
        f"・理由: {reason}\n"
        f"・内容: {entry['content']}\n"
        f"・投稿: {message.jump_url}",
        exclude_channel_id=message.channel.id,
    )

    # 監視ワードのときは、そのチャンネルで本人に警告も出す
    m = re.search(r"監視ワード「(.+?)」を検知", reason)
    if m:
        try:
            await message.channel.send(
                f"⚠️ {message.author.mention} 警告だよ！「{m.group(1)}」は使っちゃダメな言葉だよ。この投稿はログに記録したからね。"
            )
        except discord.HTTPException:
            pass
    return True


async def log_plain_message(message: discord.Message) -> None:
    """通常の投稿も1通ごとにすぐログに残す（ログ用チャンネル内とボットは除く）。"""
    if message.guild is None:
        return
    if getattr(message.channel, "name", "") in LOG_CHANNEL_NAMES:
        return
    entry = _base_entry(message, "msg")
    log_entries.append(entry)
    del log_entries[:-LOG_KEEP]
    await _post_log(
        message.guild,
        "📝 **ログ**\n"
        f"・時刻: {entry['time']:%Y-%m-%d %H:%M:%S}\n"
        f"・ユーザー: {entry['user']}\n"
        f"・場所: #{entry['channel']}\n"
        f"・内容: {entry['content']}",
        exclude_channel_id=message.channel.id,
    )


# ---------- イベント＆コマンド ----------
@bot.event
async def on_ready():
    global _tree_synced, _maintenance_started
    print(f"ログインしました: {bot.user} / マッキーボット起動OK")
    guild_names = " / ".join(g.name for g in bot.guilds) or "なし"
    print(f"参加中のサーバー（{len(bot.guilds)}件）: {guild_names}", flush=True)
    if not _maintenance_started:
        _maintenance_started = True
        asyncio.create_task(_maintenance_loop())
    if not _tree_synced:
        try:
            synced = await bot.tree.sync()
            _tree_synced = True
            print(f"スラッシュコマンドを同期しました: {len(synced)}件")
        except Exception as e:
            print(f"スラッシュコマンドの同期に失敗: {e}")


@bot.event
async def on_guild_join(guild: discord.Guild):
    print(f"サーバーに参加しました: {guild.name}", flush=True)


@bot.event
async def on_message(message: discord.Message):
    # 自分自身の投稿は無視
    if bot.user and message.author.id == bot.user.id:
        return
    if message.author.bot:
        # 他のボットが自分宛てに話しかけたら、知識（会話ルール）で返事する。
        # ボット同士で無限に続かないよう、短時間の連続応答は回数で止める。
        mentioned = bot.user in message.mentions if bot.user else False
        called = "マッキー" in message.content or "警告bot" in message.content or "みみ" in message.content
        if mentioned or called:
            now = time.time()
            state = _bot_talk_state.setdefault(message.channel.id, {"count": 0, "last": 0.0})
            state["count"] = state["count"] + 1 if now - state["last"] < 60 else 1
            state["last"] = now
            if state["count"] <= BOT_TALK_MAX:
                clean = message.content
                if bot.user:
                    clean = clean.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
                clean = clean.replace("マッキー", "", 1).replace("警告bot", "", 1).replace("みみ", "", 1).strip(" 、,")
                reply = chat_reply(message.author.id, clean or "こんにちは")
                if reply:
                    await send_thoughtful_reply(message, reply, clean)
        return
    # サーバー内では全投稿をログに記録（スパムっぽいものは検知つき）
    if message.guild is not None:
        detected = await check_and_log_spam(message)
        if not detected:
            await log_plain_message(message)
    # 人が話したら、ボット同士の連続カウントはリセット
    _bot_talk_state.pop(message.channel.id, None)
    is_dm = isinstance(message.channel, discord.DMChannel)
    mentioned = bot.user in message.mentions if bot.user else False
    called = "マッキー" in message.content or "警告bot" in message.content or "みみ" in message.content
    if is_dm or mentioned or called:
        clean = message.content
        if bot.user:
            clean = clean.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
        clean = clean.replace("マッキー", "", 1).replace("警告bot", "", 1).replace("みみ", "", 1).strip(" 、,")
        reply = chat_reply(message.author.id, clean or "こんにちは")
        if reply:
            await send_thoughtful_reply(message, reply, clean)
        return


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("環境変数 DISCORD_TOKEN が未設定です。Botのトークンを設定してから起動してね。")
    # PORT がある環境（Render等）でのみ keep-alive を起動する
    start_keep_alive()
    bot.run(token)
