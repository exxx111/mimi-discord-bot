"""
みみ Discordボット - かわいい女の子キャラクター風 会話＆じゃんけんボット

使い方:
  1. pip install discord.py
  2. Discord Developer Portal で Bot を作り、MESSAGE CONTENT INTENT を ON にする
  3. 環境変数にトークンを入れる
       export DISCORD_TOKEN="あなたのトークン"
     Windows (PowerShell):  $env:DISCORD_TOKEN="あなたのトークン"
  4. python mimi-discord-bot.py
  5. Bot の招待URLでサーバーに入れて、メンション or DM or 「みみ」って呼んでね

Render等のWebサービスで動かす場合:
  環境変数 PORT が設定されていれば、ヘルスチェック用の小さな
  HTTPサーバーが別スレッドで自動起動します。ローカルでは不要です。

コマンド（!版と /スラッシュ版の両方があるよ）:
  !hello / /hello       あいさつ
  !janken / /janken     ボタンでじゃんけん対決（スコア付き）
  !janken グー          文字でも出せるよ（グー / チョキ / パー）。/janken は手の選択肢つき
  !omikuji / /omikuji   おみくじ
  !game / /game         ゲームのリンクを送るよ（!リンク でもOK、「ゲーム」って言っても送るよ）
  !spamlog / /spamlog   スパム検知ログの最新10件を見る（管理者向け）
  !help_mimi / /help_mimi ヘルプ
会話はメンション、DM、文に「みみ」を含めると返事するよ。
サーバー内ではスパムっぽい投稿（連投・同じ文の繰り返し・リンク連投・大量メンション・
招待リンク・同じ文字や絵文字の連打・改行荒らし・指定した監視ワード）を自動で見張って、
見つけたらログに残すよ（消したりBANはしないよ）。
ログは「mimi-log」「みみログ」「spam-log」という名前のチャンネルがあればそこにも流すよ。
"""

import os
import re
import time
import random
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord import app_commands
from discord.ext import commands

# ---------- キャラクター設定：みみ ----------
MIMI_NAME = "みみ"
MIMI_PROFILE = (
    "茶髪お団子・青い瞳の女の子キャラクター「みみ」だよ。"
    "明るくてちょっと甘えんぼう、語尾はやわらかく話すよ。"
)

# ゲーム（コインタップゲーム）のリンク
GAME_URL = "https://muse.ai/s/gemuapuri-3-uxe6wxnxtar4xzq/i5K212IGw5Ftctt8OsuXYCA4MOFnxbk831koPjh_F18"


def game_link_text() -> str:
    return (
        "えへへ、ゲームのリンクだよ♪ みんなで遊んでね！\n"
        f"🎮 ゲームはこちら → {GAME_URL}"
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

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ユーザーごとの状態
janken_score = defaultdict(lambda: {"win": 0, "lose": 0, "draw": 0})
user_names: dict[int, str] = {}

HANDS = ["グー", "チョキ", "パー"]
# 何に勝つか: グー>チョキ, チョキ>パー, パー>グー
BEATS = {"グー": "チョキ", "チョキ": "パー", "パー": "グー"}
HAND_EMOJI = {"グー": "✊", "チョキ": "✌️", "パー": "🖐️"}


def judge(player: str, cpu: str) -> str:
    if player == cpu:
        return "draw"
    return "win" if BEATS[player] == cpu else "lose"


def janken_result_text(user: discord.abc.User, player: str, cpu: str) -> str:
    result = judge(player, cpu)
    s = janken_score[user.id]
    if result == "win":
        s["win"] += 1
        comment = random.choice([
            "すごーい！あなたの勝ちだよ…！みみ、負けちゃった〜",
            "やるね！みみの負けだよ。もう一回しよ？",
        ])
    elif result == "lose":
        s["lose"] += 1
        comment = random.choice([
            "やった！みみの勝ちだよ〜♪ 次は負けないからね？",
            "えへへ、みみ強いでしょ？リベンジ待ってるよ！",
        ])
    else:
        s["draw"] += 1
        comment = "あいこだね！気が合うのかも…？もう一回！"
    return (
        f"{HAND_EMOJI[player]} あなた: **{player}**　vs　{HAND_EMOJI[cpu]} みみ: **{cpu}**\n"
        f"{comment}\n"
        f"📊 {user.display_name} の成績: {s['win']}勝 {s['lose']}敗 {s['draw']}分"
    )


class JankenView(discord.ui.View):
    def __init__(self, author: discord.abc.User):
        super().__init__(timeout=60)
        self.author = author

    async def _play(self, interaction: discord.Interaction, hand: str):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("これは他の人の対決だよ。`!janken` で自分のを始めてね！", ephemeral=True)
            return
        cpu = random.choice(HANDS)
        text = janken_result_text(interaction.user, hand, cpu)
        # ボタンを残して何度でも対決できるようにする
        await interaction.response.edit_message(content=text, view=self)

    @discord.ui.button(label="グー", emoji="✊", style=discord.ButtonStyle.primary)
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._play(interaction, "グー")

    @discord.ui.button(label="チョキ", emoji="✌️", style=discord.ButtonStyle.success)
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._play(interaction, "チョキ")

    @discord.ui.button(label="パー", emoji="🖐️", style=discord.ButtonStyle.danger)
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._play(interaction, "パー")


# ---------- 会話 ----------
GREETINGS = ["こんにちは", "こんばんは", "おはよう", "やっほー", "hello", "hi"]
KEYWORD_REPLIES = [
    (["疲れ", "つかれ"], ["おつかれさま…！少し休んでね？みみがそばにいるよ。", "がんばったね。えらいえらい♪"]),
    (["ありがとう"], ["どういたしまして♪ うれしいな〜", "えへへ、いつでも呼んでね！"]),
    (["かわいい", "可愛い"], ["えっ…ほんと？ありがとう、照れちゃう…！", "えへへ、もっと言ってもいいんだよ？"]),
    (["好き"], ["みみもあなたのこと好きだよ！", "わ〜！うれしい言葉、ありがとう♪"]),
    (["おやすみ"], ["おやすみなさい。いい夢見てね…♪", "おやすみ〜、また明日話そうね。"]),
    (["さみしい", "寂しい", "つらい", "悲しい"], ["大丈夫、みみがいるよ。ゆっくり話そ？", "無理しないでね。ここにいるからね。"]),
    (["お腹", "おなか", "ごはん", "ご飯"], ["ごはんの話？みみは甘いもの大好きだよ〜", "美味しいもの食べると元気出るよね！何が好き？"]),
    (["天気"], ["天気の話だね。外に出るなら傘が必要かチェックしてね！", "いい天気だといいね〜お散歩したいな♪"]),
]

FALLBACKS = [
    "うんうん、それでそれで？もっと聞かせて♪",
    "なるほど〜！面白いね。あなたともっとお話ししたいな。",
    "えへへ、そうなんだ！…じゃんけんもしたくなってきたな〜 `!janken` する？",
    "わかる気がする…！みみ、ちゃんと聞いてるよ。",
    "そうだね♪ 今日はどんな一日だった？",
    "お話ししてくれてありがとう！「じゃんけん」って言うと対決もできるよ✊",
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
        return random.choice([f"{prefix}やっほー！みみだよ♪ 今日も話せてうれしいな〜", f"{prefix}こんにちは！何して遊ぶ？じゃんけん？お話し？"])
    if "ゲーム" in t or "リンク" in t:
        return game_link_text()
    if "自己紹介" in t or "誰" in t and "みみ" in t:
        return f"わたしは{MIMI_PROFILE}\n`!janken` でじゃんけん対決、`!omikuji` でおみくじ、`!game` でゲームのリンクが送れるよ！"
    for keywords, replies in KEYWORD_REPLIES:
        if any(k in t for k in keywords):
            return random.choice(replies)
    if t.endswith("？") or t.endswith("?"):
        return random.choice(["うーん、みみ的にはそう思うな〜。あなたはどう思う？", "いい質問だね！みみも気になる…一緒に考えよ♪", "それはね…えへへ、秘密にしたい気分♪ 冗談だよ、もうちょっと教えて？"])
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
LOG_KEEP = 100           # ログの最大保持件数
LOG_CHANNEL_NAMES = ["mimi-log", "みみログ", "spam-log"]

# (guild_id, user_id) -> deque[(時刻, 正規化した文, リンク数)]
_recent_msgs: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=30))
# (guild_id, user_id) -> 最後に記録した時刻
_last_flagged: dict[tuple[int, int], float] = {}
# 検知ログ（新しいものが末尾）
spam_log: list[dict] = []

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


async def check_and_log_spam(message: discord.Message) -> None:
    """検知したらログに残し、ログ用チャンネルがあればそこにも流す。
    管理者も含めて全員が対象（ボット自身は除く）。"""
    if message.guild is None:
        return
    reason = _detect_spam(message)
    if not reason:
        return
    key = (message.guild.id, message.author.id)
    now = time.time()
    if now - _last_flagged.get(key, 0) < FLAG_COOLDOWN:
        return
    _last_flagged[key] = now

    entry = {
        "time": datetime.now(JST),
        "guild": message.guild.name,
        "channel": getattr(message.channel, "name", "DM"),
        "user": str(message.author),
        "user_id": message.author.id,
        "reason": reason,
        "content": (message.content or "")[:100],
        "jump_url": message.jump_url,
    }
    spam_log.append(entry)
    del spam_log[:-LOG_KEEP]
    print(f"[SPAM検知] {entry['time']:%Y-%m-%d %H:%M:%S} {entry['guild']} #{entry['channel']} {entry['user']}: {reason}")

    for name in LOG_CHANNEL_NAMES:
        log_ch = discord.utils.get(message.guild.text_channels, name=name)
        if log_ch is not None and log_ch.id != message.channel.id:
            try:
                await log_ch.send(
                    "🚨 **スパム検知ログ**\n"
                    f"・時刻: {entry['time']:%Y-%m-%d %H:%M:%S}\n"
                    f"・ユーザー: {entry['user']}\n"
                    f"・場所: #{entry['channel']}\n"
                    f"・理由: {reason}\n"
                    f"・内容: {entry['content']}\n"
                    f"・投稿: {message.jump_url}"
                )
            except discord.HTTPException:
                pass
            break


# ---------- イベント＆コマンド ----------
@bot.event
async def on_ready():
    global _tree_synced
    print(f"ログインしました: {bot.user} / みみボット起動OK")
    if not _tree_synced:
        try:
            synced = await bot.tree.sync()
            _tree_synced = True
            print(f"スラッシュコマンドを同期しました: {len(synced)}件")
        except Exception as e:
            print(f"スラッシュコマンドの同期に失敗: {e}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # サーバー内ではスパム監視（記録のみ）
    if message.guild is not None:
        await check_and_log_spam(message)
    # コマンドは先に処理
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return
    is_dm = isinstance(message.channel, discord.DMChannel)
    mentioned = bot.user in message.mentions if bot.user else False
    called = "みみ" in message.content
    # 自然文で「ゲーム」と言われたらリンクを送る
    if "ゲーム" in message.content and (is_dm or mentioned or called):
        await message.channel.send(game_link_text())
        return
    # 自然文でじゃんけん開始
    if ("じゃんけん" in message.content or "ジャンケン" in message.content) and (is_dm or mentioned or called):
        await message.channel.send(
            f"じゃんけん対決だね！{message.author.display_name}、手を選んで♪\n最初はグー…じゃんけん・ぽん！",
            view=JankenView(message.author),
        )
        return
    if is_dm or mentioned or called:
        clean = message.content
        if bot.user:
            clean = clean.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
        clean = clean.replace("みみ", "", 1).strip(" 、,")
        reply = chat_reply(message.author.id, clean or "こんにちは")
        if reply:
            await message.channel.send(reply)
        return
    await bot.process_commands(message)


@bot.command(name="hello")
async def hello(ctx: commands.Context):
    name = user_names.get(ctx.author.id, ctx.author.display_name)
    await ctx.send(f"やっほー、{name}！みみだよ♪ 会話はメンションかDMで、じゃんけんは `!janken` だよ！")


@bot.command(name="janken")
async def janken(ctx: commands.Context, hand: str = None):
    """!janken → ボタン対決 / !janken グー → 即対決"""
    if hand is None:
        await ctx.send(f"{ctx.author.display_name} とじゃんけん対決！手を選んでね♪\n最初はグー…じゃんけん・ぽん！", view=JankenView(ctx.author))
        return
    hand = hand.strip()
    aliases = {"ぐー": "グー", "ちょき": "チョキ", "ぱー": "パー", "rock": "グー", "scissors": "チョキ", "paper": "パー"}
    hand = aliases.get(hand.lower(), hand)
    if hand not in HANDS:
        await ctx.send("手は **グー / チョキ / パー** のどれかで出してね！例: `!janken グー`")
        return
    await ctx.send(janken_result_text(ctx.author, hand, random.choice(HANDS)))


@bot.command(name="game", aliases=["リンク", "link", "げーむ"])
async def game(ctx: commands.Context):
    """!game / !リンク → ゲームのリンクを送る"""
    await ctx.send(game_link_text())


@bot.command(name="omikuji")
async def omikuji(ctx: commands.Context):
    result = random.choice(["大吉", "中吉", "小吉", "吉", "末吉", "凶"])
    comments = {"大吉": "すごーい大吉！今日は最高の一日だよ♪", "凶": "凶…でも大丈夫、みみが応援してるからね！"}
    await ctx.send(f"🎋 {ctx.author.display_name} のおみくじは **{result}**！{comments.get(result, 'いい日になる予感だよ♪')}")


def spamlog_text(guild_name: str) -> str:
    """スパム検知ログの表示テキスト（!版と /版で共用）。"""
    entries = [e for e in spam_log if e["guild"] == guild_name][-10:]
    if not entries:
        return "📋 スパム検知ログはまだありません。この調子で平和だね♪"
    lines = ["📋 **スパム検知ログ（最新10件）**"]
    for e in reversed(entries):
        lines.append(
            f"・{e['time']:%m/%d %H:%M} {e['user']}（#{e['channel']}）: {e['reason']}\n"
            f"　内容: {e['content']}"
        )
    return "\n".join(lines)


@bot.command(name="spamlog")
async def spamlog(ctx: commands.Context):
    """!spamlog : スパム検知ログの最新10件（メッセージ管理権限のある人だけ）"""
    if ctx.guild is None:
        await ctx.send("スパムログはサーバーの中で見てね。")
        return
    if not (isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.manage_messages):
        await ctx.send("スパムログはサーバーの管理者だけが見られるよ。")
        return
    await ctx.send(spamlog_text(ctx.guild.name))


@bot.command(name="help_mimi")
async def help_mimi(ctx: commands.Context):
    await ctx.send(HELP_TEXT)


# ---------- スラッシュコマンド（/ で出てくる版） ----------
# 起動時に bot.tree.sync() で Discord に登録される。
_tree_synced = False


@bot.tree.command(name="hello", description="みみがあいさつするよ")
async def slash_hello(interaction: discord.Interaction):
    name = user_names.get(interaction.user.id, interaction.user.display_name)
    await interaction.response.send_message(f"やっほー、{name}！みみだよ♪ 会話はメンションかDMで、じゃんけんは `/janken` だよ！")


@bot.tree.command(name="janken", description="みみとじゃんけん対決！")
@app_commands.describe(hand="手を選ぶと即対決。選ばないとボタン対決になるよ")
@app_commands.choices(hand=[
    app_commands.Choice(name="グー", value="グー"),
    app_commands.Choice(name="チョキ", value="チョキ"),
    app_commands.Choice(name="パー", value="パー"),
])
async def slash_janken(interaction: discord.Interaction, hand: str = None):
    if hand is None:
        await interaction.response.send_message(
            f"{interaction.user.display_name} とじゃんけん対決！手を選んでね♪\n最初はグー…じゃんけん・ぽん！",
            view=JankenView(interaction.user),
        )
        return
    await interaction.response.send_message(janken_result_text(interaction.user, hand, random.choice(HANDS)))


@bot.tree.command(name="omikuji", description="今日の運勢をおみくじで占うよ")
async def slash_omikuji(interaction: discord.Interaction):
    result = random.choice(["大吉", "中吉", "小吉", "吉", "末吉", "凶"])
    comments = {"大吉": "すごーい大吉！今日は最高の一日だよ♪", "凶": "凶…でも大丈夫、みみが応援してるからね！"}
    await interaction.response.send_message(f"🎋 {interaction.user.display_name} のおみくじは **{result}**！{comments.get(result, 'いい日になる予感だよ♪')}")


@bot.tree.command(name="game", description="コインタップゲームのリンクを送るよ")
async def slash_game(interaction: discord.Interaction):
    await interaction.response.send_message(game_link_text())


@bot.tree.command(name="spamlog", description="スパム検知ログの最新10件を見る（管理者向け）")
async def slash_spamlog(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("スパムログはサーバーの中で見てね。", ephemeral=True)
        return
    if not (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_messages):
        await interaction.response.send_message("スパムログはサーバーの管理者だけが見られるよ。", ephemeral=True)
        return
    await interaction.response.send_message(spamlog_text(interaction.guild.name), ephemeral=True)


HELP_TEXT = (
    "**みみの使い方**\n"
    "💬 会話: DM、メンション、文に「みみ」を入れて話しかけてね。「名前は〇〇」で名前を覚えるよ\n"
    "✊ `!janken` / `/janken` : じゃんけん対決（ボタン or 手の選択肢、スコア記録あり）\n"
    "🎋 `!omikuji` / `/omikuji` : おみくじ\n"
    "🎮 `!game` / `/game` : ゲームのリンクを送るよ（`!リンク` でもOK、「ゲーム」って言っても送るよ）\n"
    "🚨 スパム監視中: 連投やリンク連投を見つけたらログに残すよ（管理者は `!spamlog` / `/spamlog` で確認）\n"
    "👋 `!hello` / `/hello` : あいさつ"
)


@bot.tree.command(name="help_mimi", description="みみの使い方を表示するよ")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(HELP_TEXT)


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("環境変数 DISCORD_TOKEN が未設定です。Botのトークンを設定してから起動してね。")
    # PORT がある環境（Render等）でのみ keep-alive を起動する
    start_keep_alive()
    bot.run(token)
