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

コマンド:
  !hello            あいさつ
  !janken           ボタンでじゃんけん対決（スコア付き）
  !janken グー      文字でも出せるよ（グー / チョキ / パー）
  !omikuji          おみくじ
  !game             ゲームのリンクを送るよ（!リンク でもOK、「ゲーム」って言っても送るよ）
  !help_mimi        ヘルプ
会話はメンション、DM、文に「みみ」を含めると返事するよ。
"""

import os
import re
import random
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
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


# ---------- イベント＆コマンド ----------
@bot.event
async def on_ready():
    print(f"ログインしました: {bot.user} / みみボット起動OK")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
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


@bot.command(name="help_mimi")
async def help_mimi(ctx: commands.Context):
    await ctx.send(
        "**みみの使い方**\n"
        "💬 会話: DM、メンション、文に「みみ」を入れて話しかけてね。「名前は〇〇」で名前を覚えるよ\n"
        "✊ `!janken` : ボタンでじゃんけん対決（スコア記録あり）\n"
        "✌️ `!janken グー` : 文字で即対決\n"
        "🎋 `!omikuji` : おみくじ\n"
        "🎮 `!game` : ゲームのリンクを送るよ（`!リンク` でもOK、「ゲーム」って言っても送るよ）\n"
        "👋 `!hello` : あいさつ"
    )


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("環境変数 DISCORD_TOKEN が未設定です。Botのトークンを設定してから起動してね。")
    # PORT がある環境（Render等）でのみ keep-alive を起動する
    start_keep_alive()
    bot.run(token)
