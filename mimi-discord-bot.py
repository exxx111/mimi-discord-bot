"""
tempus Discordボット - かわいい女の子キャラクター風 会話＆見守りボット

使い方:
  1. pip install discord.py
  2. Discord Developer Portal で Bot を作り、MESSAGE CONTENT INTENT を ON にする
  3. 環境変数にトークンを入れる
       export DISCORD_TOKEN="あなたのトークン"
     Windows (PowerShell):  $env:DISCORD_TOKEN="あなたのトークン"
  4. python mimi-discord-bot.py
  5. Bot の招待URLでサーバーに入れて、メンション or DM or 「tempus」って呼んでね

Render等のWebサービスで動かす場合:
  環境変数 PORT が設定されていれば、ヘルスチェック用の小さな
  HTTPサーバーが別スレッドで自動起動します。ローカルでは不要です。

コマンドは無し。ただしログ確認だけスラッシュコマンド /log（管理者だけ）を残してある。
会話はメンション、DM、文に「tempus」を含めると返事するよ。
他のボットが話しかけてきたら、ボット同士でも会話するよ（無限ループ防止つき）。
返事はすぐ送らず、「入力中…」を出して少し考えてから送るよ。
環境変数 GEMINI_API_KEY があれば、会話はAI（Gemini）が考えて返事するよ。
サーバー内では全ての投稿を1通ごとにログに記録するよ。スパムっぽい投稿（連投・同じ文の
繰り返し・リンク連投・大量メンション・招待リンク・同じ文字や絵文字の連打・改行荒らし・
指定した監視ワード）は🚨検知つきで記録するよ（消したりBANはしないよ）。
ログは「mimi-log」「みみログ」「spam-log」「警告botログ」「tempusログ」という名前のチャンネルがあればそこにも流すよ。
🚨検知したログはクラウド（Firestore）にも保存するので、再起動しても消えないよ（起動時に復元して /log や21時のレポートで見返せる）。
画像つきで話しかけられたら、OpenRouter（無料の画像AI）→Geminiの順に画像を見て答えるよ（どちらも混んでいると見れないこともある）。
"""

import base64
import io
import json
import os
import re
import time
import random
import asyncio
import threading
import urllib.error
import urllib.request
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord import app_commands
from discord.ext import commands

# ---------- キャラクター設定：tempus ----------
MIMI_NAME = "tempus"
MIMI_PROFILE = (
    "サーバーを見守る「tempus」だよ。"
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


def _next_restart_text() -> str:
    """次の自動再起動の予定時刻（日本時間）を「○月○日 HH:MM」で返す。"""
    t = datetime.fromtimestamp(_started_at + RESTART_AFTER, JST)
    return f"{t.month}月{t.day}日 {t:%H:%M}"


def _self_ping() -> None:
    try:
        req = urllib.request.Request(SELF_URL, headers={"User-Agent": "mimi-keepalive"})
        with urllib.request.urlopen(req, timeout=10) as res:
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
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
# スラッシュコマンドは /log と /janken。起動時の sync() で Discord 側の登録を合わせる。
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



# ---------- AI連携（Groq / Gemini・無料枠） ----------
# GROQ_API_KEY があれば Groq（速くて混みにくいLLM）を優先し、
# ダメなら GEMINI_API_KEY の Gemini、それも無ければルール返信に切り替える。
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
# Groqで使いたいモデルの優先順（実際はAPIの一覧と照合して使えるものだけ使う）
GROQ_PREFERRED = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "qwen/qwen3-32b",
    "openai/gpt-oss-20b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
]
_groq_models_cache: list[str] | None = None


def _groq_models() -> list[str]:
    """このキーで使えるGroqモデルの一覧をAPIから取得し、優先順に並べる（結果は保持）。"""
    global _groq_models_cache
    if _groq_models_cache:
        return _groq_models_cache
    try:
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "User-Agent": "Mozilla/5.0 (compatible; mimi-discord-bot/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        available = [item.get("id", "") for item in data.get("data", [])]
        skip = ("whisper", "tts", "guard", "orpheus", "compound")
        chat_models = [m for m in available if m and not any(k in m for k in skip)]
        ordered = [m for m in GROQ_PREFERRED if m in chat_models] + [m for m in chat_models if m not in GROQ_PREFERRED]
        if ordered:
            _groq_models_cache = ordered
            print(f"[AI] Groqで使えるモデル: {', '.join(ordered[:4])}")
            return ordered
    except Exception as e:
        print(f"[AI] Groqのモデル一覧の取得に失敗: {e}")
    return GROQ_PREFERRED
GEMINI_MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "").strip(), "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-flash-latest"] if m]
_working_gemini_model: str | None = None

# Geminiの404予防: 使えるモデルはAPIから取得し（Groqで効いた方式）、
# 404になったモデルは覚えて二度と試さない。全部ダメなときはクールダウンして無駄な待ちをなくす。
_gemini_models_cache: list[str] | None = None
_gemini_dead_models: set[str] = set()
_gemini_cooldown_until: float = 0.0
_GEMINI_PREFERRED = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-flash-latest", "gemini-1.5-flash"]


def _gemini_models() -> list[str]:
    """このAPIキーで実際に使えるGeminiモデルをAPIから取得して、優先順に返す。"""
    global _gemini_models_cache
    if _gemini_models_cache:
        return [m for m in _gemini_models_cache if m not in _gemini_dead_models]
    models: list[str] = []
    if GEMINI_API_KEY:
        try:
            req = urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": GEMINI_API_KEY, "User-Agent": "Mozilla/5.0 (compatible; mimi-discord-bot/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.load(resp)
            for item in data.get("models", []):
                name = (item.get("name") or "").replace("models/", "")
                methods = item.get("supportedGenerationMethods") or []
                if name and "generateContent" in methods and "gemini" in name:
                    models.append(name)
        except Exception as e:
            print(f"[AI] Geminiのモデル一覧の取得に失敗: {e}", flush=True)
    if models:
        ordered = [m for m in _GEMINI_PREFERRED if m in models] + [m for m in models if m not in _GEMINI_PREFERRED]
        _gemini_models_cache = ordered
        print(f"[AI] Geminiで使えるモデル: {', '.join(ordered[:4])}", flush=True)
        return [m for m in ordered if m not in _gemini_dead_models]
    return [m for m in GEMINI_MODELS if m not in _gemini_dead_models]


def _gemini_mark_dead(model: str, code: int) -> None:
    """404（そのモデルは無い）になったモデルは覚えて、二度と試さない。"""
    if code == 404 and model not in _gemini_dead_models:
        _gemini_dead_models.add(model)
        print(f"[AI] Geminiモデル {model} は404のため除外しました", flush=True)


def _gemini_in_cooldown() -> bool:
    return time.monotonic() < _gemini_cooldown_until


def _gemini_start_cooldown() -> None:
    global _gemini_cooldown_until
    _gemini_cooldown_until = time.monotonic() + 1800
    print("[AI] Geminiは連続失敗のため30分お休みします（その間はGroqだけで回答します）", flush=True)
AI_SYSTEM_PROMPT = (
    "あなたはDiscordサーバーの見守りボット「tempus」です。"
    "相手の発言をよく読み、何を聞きたいのか・どんな気持ちなのかを考えてから返事をしてください。"
    "質問には具体的に、理由や例も添えて答えてください。あいまいな相づちだけで済ませないでください。"
    "会話の流れ（今までのやりとり）を必ず踏まえ、前に出た話題・相手の好きなもの・状況を覚えていて、自然に触れてください。"
    "雑談は短く楽しく、説明を求められたらしっかり長めに（最大で600字くらい、必要なら箇条書きも可）答えてください。"
    "分からないことは正直に「わかんないや」と言い、推測のときは「たぶん」と断ってください。うそはつかないでください。"
    "同じ言い回しを繰り返さず、ときどき相手に質問して会話を続けてください。"
    "明るくフレンドリーに答えてください。あなたは日本語・英語・フィリピン語（タガログ語）・ラテン語・韓国語で話せます。"
    "サーバーの安全を見守るのが仕事で、スパムや禁止ワードを見つけてログに記録しています。"
)

# ---------- 多言語対応（英語・フィリピン語・ラテン語・韓国語を追加） ----------
# ユーザーごとに「今どの言語で返すか」を覚える（再起動でリセットされる）。
user_langs: dict[int, str] = {}
_LANG_NAMES = {
    "ja": "日本語",
    "en": "English（英語）",
    "fil": "Filipino（フィリピン語／タガログ語）",
    "la": "Latin（ラテン語）",
    "ko": "한국어（韓国語）",
}
_LANG_INSTRUCTIONS = {
    "ja": "相手への返事は必ず日本語で書いてください。",
    "en": "You must reply to this user in English only. Keep proper nouns, unit names and game codes exactly as written.",
    "fil": "Dapat kang sumagot sa user na ito sa Filipino (Tagalog) lamang. Panatilihin ang mga pangngalang pantangi at game codes kung ano ang nakasulat.",
    "la": "Huic utenti Latine classice responde. 文は古典ラテン語で書いてください。ただし文のあとに「（訳：…）」で短い日本語訳を必ず添えてください。固有名詞とゲームのコードは原文のままにしてください。",
    "ko": "이 사용자에게는 반드시 한국어로만 답해 주세요. 고유명사와 게임 코드는 원문 그대로 두세요.",
}
_LANG_CONFIRMATIONS = {
    "ja": "わかった！これからは日本語で話すね♪",
    "en": "Got it! I'll speak in English from now on. Ask me anything!",
    "fil": "Sige! Mula ngayon, sa Filipino na ako magsasalita. Magtanong ka lang ng kahit ano!",
    "la": "Bene! Ex hoc tempore Latine loquar.（訳：わかった！これからはラテン語で話すね）",
    "ko": "알겠어! 이제부터 한국어로 말할게. 뭐든지 물어봐!",
}
_FILIPINO_WORDS = {
    "kumusta", "kamusta", "salamat", "maraming", "mahal", "kita", "ako", "ikaw", "siya",
    "tayo", "kayo", "sila", "kami", "namin", "natin", "ang", "mga", "sa", "na", "pa",
    "po", "opo", "hindi", "ano", "sino", "saan", "kailan", "bakit", "paano", "magkano",
    "gusto", "ayaw", "pwede", "puwede", "talaga", "sobra", "maganda", "magandang",
    "pogi", "pagkain", "laro", "umaga", "gabi", "hapon", "ngayon", "bukas", "kahapon",
    "kaibigan", "pamilya", "bahay", "trabaho", "pera", "tubig", "kanin", "masarap",
    "mabait", "masaya", "malungkot", "pagod", "gutom", "sige", "tara", "ingat",
    "paalam", "pasensya", "sandali", "mabilis", "mabagal", "malaki", "maliit",
    "oo", "musta", "kain", "tulog", "gising", "laro", "lalaro", "kumain", "opo",
    "teka", "halika", "kita", "niyo", "niya", "ito", "iyan", "iyon", "dito",
}
_LATIN_STRONG = {
    "salve", "ave", "gratias", "latine", "vale", "quid", "amicus", "amica", "ego",
    "sumus", "loquere", "senatus", "romae", "puella", "deus", "fortuna",
}
_LATIN_WORDS = _LATIN_STRONG | {
    "et", "est", "sunt", "esse", "non", "cum", "ad", "ex", "per", "pro", "sine",
    "meus", "tuus", "noster", "puer", "roma", "amor", "vita", "magna", "magnus",
    "parva", "parvus", "bonus", "bona", "malus", "felix", "quod", "qui", "quae",
    "quam", "nos", "vos", "sum", "es", "estis", "erat", "erant", "habet", "habent",
    "dicit", "dicunt", "facit", "venit", "videt", "amat", "amant", "loquitur",
    "bellum", "pacem", "luna", "terra", "mare", "caelum", "ignis", "aqua",
}
_ENGLISH_WORDS = {
    "hello", "hi", "hey", "please", "thank", "thanks", "what", "how", "why", "when",
    "where", "who", "is", "are", "was", "were", "the", "this", "that", "you", "your",
    "can", "could", "would", "should", "does", "did", "good", "morning", "night",
    "game", "update", "code", "news", "weather", "today", "tomorrow", "yesterday",
    "friend", "love", "like", "want", "need", "tell", "show", "give", "help",
    "play", "playing",
    "ok", "okay", "yes", "yeah", "yep", "no", "nope", "sure", "wow", "cool", "nice",
    "great", "sorry", "bye", "really", "very", "much", "more", "now", "go", "come",
    "see", "know", "think", "about", "with", "for", "and", "but", "have", "has",
    "will", "just", "let", "get", "got", "im", "me", "my", "we", "it", "its",
}


def _detect_lang(text: str) -> str | None:
    """発言の言語を推定する（対応: ja / en / fil / la / ko）。分からなければ None。"""
    t = (text or "").strip()
    if not t:
        return None
    hangul = len(re.findall(r"[가-힣]", t))
    kana = len(re.findall(r"[ぁ-ヿ]", t))
    if hangul and kana:
        # 日本語文の中で韓国語を引用しただけの場合は日本語として扱う
        return "ko" if hangul > kana else "ja"
    if hangul:
        return "ko"
    if kana:
        return "ja"
    words = re.findall(r"[a-z']+", t.lower())
    if words:
        fil = sum(1 for w in words if w in _FILIPINO_WORDS)
        la = sum(1 for w in words if w in _LATIN_WORDS)
        la_strong = sum(1 for w in words if w in _LATIN_STRONG)
        en = sum(1 for w in words if w in _ENGLISH_WORDS)
        if fil >= 1 and fil >= en and fil >= la:
            return "fil"
        if la_strong >= 1 or la >= 2:
            if la >= en:
                return "la"
        if en >= 1:
            return "en"
        if len(words) >= 4:
            return "en"
    if re.search(r"[一-鿿]", t):
        return "ja"
    return None


def _requested_lang(text: str) -> str | None:
    """「英語で話して」のような明示的な言語の切り替え希望があれば、その言語コードを返す。"""
    t = (text or "").strip()
    low = t.lower()
    checks = [
        ("ja", [r"(日本語|にほんご).{0,8}(話して|はなして|答えて|返事して|戻して|にして|でお願い|モード)", r"^(日本語|にほんご)(で|に)?[！!。\s]*$"]),
        ("en", [r"(英語|えいご).{0,8}(話して|答えて|返事して|にして|でお願い|モード)", r"\b(speak|talk|reply|answer|respond)\b[^.]{0,24}\benglish\b", r"\bin english please\b", r"^english( please)?[!. ]*$"]),
        ("fil", [r"(フィリピン語|タガログ語).{0,8}(話して|答えて|返事して|にして|でお願い|モード)", r"\b(speak|talk|reply|answer|respond)\b[^.]{0,24}\b(tagalog|filipino)\b", r"\bsa (tagalog|filipino)\b[^.]{0,16}\b(please|magsalita|sumagot)\b", r"^(tagalog|filipino)( please)?[!. ]*$"]),
        ("la", [r"(ラテン語|らてんご).{0,8}(話して|答えて|返事して|にして|でお願い|モード)", r"\b(speak|talk|reply|answer|respond)\b[^.]{0,24}\blatin\b", r"\bin latin please\b", r"\blatine\b[^.]{0,24}\b(loquere|responde)\b", r"^latin( please)?[!. ]*$", r"^latine[!. ]*$"]),
        ("ko", [r"(韓国語|かんこくご).{0,8}(話して|答えて|返事して|にして|でお願い|モード)", r"한국어로\s*(말해|얘기해|대답해|해줘|해주세요|해)", r"\b(speak|talk|reply|answer|respond)\b[^.]{0,24}\bkorean\b", r"\bin korean please\b", r"^korean( please)?[!. ]*$", r"^한국어로[!. ]*$"]),
    ]
    for code, pats in checks:
        for pat in pats:
            if re.search(pat, low if code != "ja" and "日本" not in pat and "韓国" not in pat and "한국" not in pat else t):
                return code
    return None


def _note_user_lang(user_id: int, text: str) -> None:
    """発言から言語を推定して、その人の返信言語として覚える（クラウドにも保存する）。"""
    if not user_id:
        return
    lang = _detect_lang(text)
    if lang and user_langs.get(user_id) != lang:
        user_langs[user_id] = lang
        print(f"[言語] 返信言語を {_LANG_NAMES.get(lang, lang)} に設定 (user={user_id})", flush=True)
        _save_lang_bg(user_id, lang)


def _system_prompt(user_id: int | None = None) -> str:
    """AI_SYSTEM_PROMPT に今日の日付と調べものの指示を足したもの。"""
    now = datetime.now(timezone(timedelta(hours=9)))
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    today = f"{now.year}年{now.month}月{now.day}日（{weekdays[now.weekday()]}）{now.hour}時{now.minute:02d}分"
    return (
        AI_SYSTEM_PROMPT
        + f"今は{today}（日本時間）です。"
        + "あなたは天気予報・ニュース・Web検索で調べものができて、ゲームのアップデート情報も調べられます。頼まれればリマインダー（○分後に知らせる）もできます。"
        + "ユーザーの発言に【調べた情報】が付いていたら、その情報をもとに具体的に答えてください（数字や固有名詞は情報のものをそのまま使う）。観測データ（サーバーの会話ログの集計）が付いていたら、第三者目線で客観的に、数字をもとに分析して答えてください。"
        + "ゲームのアップデート情報のときは、最初に「アップデート名（日付）」、次にコードがあれば「コード名：報酬」を箇条書き、新ユニット、新要素の順で、短い箇条書き中心にまとめてください。情報に無いコードやユニット名は絶対に作らないでください。"
        + "フォートナイトの情報が付いていたら、最初に現在のバージョンと最新ニュース、次に次のアプデ予定、次におすすめの降り場（地名リストの中から、激戦区と安定降りを理由つきで1つずつ）、強い武器（武器プールの中からだけ選ぶ）、有利な立ち回り（序盤・中盤・終盤）の順で答えてください。情報に無いバージョン・日付・武器名・地名は作らないでください。予定やリークの話は、公式確定ではないと分かるように書いてください。"
        + "アニメアドベンチャーの情報が付いていたら、【最新アプデ】【新ユニット】【コード】【次回アプデ】の見出しに分けて答えてください。キャラの情報が付いていたら【キャラ詳細】【覚醒（進化）素材】【素材の入手先】に分けて答えてください。情報に無いユニット名・素材・入手先は作らないでください。"
        + "調べた情報が付いていない最新ニュースや天気は、知らないと正直に言ってください。"
        + (
            _LANG_INSTRUCTIONS[user_langs[user_id]]
            if user_id and user_langs.get(user_id) in _LANG_INSTRUCTIONS
            else "相手が話しかけた言語に合わせて、必ず同じ言語で返事をしてください。対応言語（日本語・英語・フィリピン語・韓国語・ラテン語）以外の言語で話しかけられた場合も、その言語で返事をしてください。判定できないときは日本語で返事をしてください。ラテン語のときは古典ラテン語で返し、あとに短い日本語訳を添えてください。"
        )
        + (f"今話している相手の名前は「{user_names[user_id]}」です。たまに名前で呼んであげてください。" if user_id and user_names.get(user_id) else "")
    )


# 会話履歴は (話し手, 本文) の形で持ち、各AIの形式に変換して渡す
_ai_history: dict[int, list[tuple[str, str]]] = defaultdict(list)


def _groq_post(payload: dict, timeout: float) -> str:
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {GROQ_API_KEY}", "User-Agent": "Mozilla/5.0 (compatible; mimi-discord-bot/1.0)", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    return (data["choices"][0]["message"]["content"] or "").strip()


def _groq_call(model: str, messages: list[dict], timeout: float = 20) -> str:
    """GroqのLLMを1回呼ぶ（OpenAI互換API）。"""
    payload = {"model": model, "messages": messages, "max_tokens": 1600, "temperature": 0.8}
    if "gpt-oss" in model:
        payload["reasoning_effort"] = "high"  # 推論を深くして、賢く答える
    try:
        return _groq_post(payload, timeout)
    except urllib.error.HTTPError as e:
        if "reasoning_effort" in payload and e.code == 400:
            payload.pop("reasoning_effort")  # 対応していない場合は付けずに再試行
            return _groq_post(payload, timeout)
        raise


def _groq_generate(history: list[tuple[str, str]], text: str, user_id: int | None = None) -> str:
    """Groqで返事を作る。混雑時はモデルを替えながら少し粘る。"""
    messages = [{"role": "system", "content": _system_prompt(user_id)}]
    for who, body in history:
        messages.append({"role": "user" if who == "user" else "assistant", "content": body})
    messages.append({"role": "user", "content": text})
    deadline = time.monotonic() + 60
    last_err: Exception | None = None
    detail = ""
    while time.monotonic() < deadline:
        for model in _groq_models():
            try:
                reply = _groq_call(model, messages, timeout=20)
                print(f"[AI] 使用モデル: Groq {model}")
                return reply
            except urllib.error.HTTPError as e:
                last_err = e
                try:
                    detail = e.read().decode("utf-8", "ignore")[:200]
                except Exception:
                    pass
                if e.code not in (400, 403, 404, 429, 503):
                    raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
        if time.monotonic() + 4 < deadline:
            time.sleep(4)
    assert last_err is not None
    raise RuntimeError(f"{last_err} / {detail}")


def _gemini_call(model: str, contents: list[dict], timeout: float = 20, user_id: int | None = None) -> str:
    """指定モデルでGemini APIをストリーミング呼び出しする。

    完成まで待つ方式だと混雑時に読み取りタイムアウトになるため、
    少しずつ流れてくる方式（SSE）で受け取る。2.5系は思考をオフにして速くする。
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"
    gen_config: dict = {"maxOutputTokens": 1600, "temperature": 0.8}
    if model.startswith("gemini-2.5"):
        gen_config["thinkingConfig"] = {"thinkingBudget": 0}
    payload = {
        "system_instruction": {"parts": [{"text": _system_prompt(user_id)}]},
        "contents": contents,
        "generationConfig": gen_config,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        method="POST",
    )
    texts: list[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                data = json.loads(chunk)
                for part in data["candidates"][0]["content"]["parts"]:
                    texts.append(part.get("text", ""))
            except Exception:
                continue
    return "".join(texts).strip()


def _gemini_generate(contents: list[dict], user_id: int | None = None) -> str:
    """Geminiを呼ぶ。使えるモデルだけを試し（404予防）、全部ダメならクールダウンする。"""
    global _working_gemini_model
    if _gemini_in_cooldown():
        raise RuntimeError("Gemini cooldown（連続失敗のためお休み中）")
    deadline = time.monotonic() + 45
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        candidates = ([_working_gemini_model] if _working_gemini_model and _working_gemini_model not in _gemini_dead_models else []) + [m for m in _gemini_models() if m != _working_gemini_model]
        if not candidates:
            break
        for model in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 3:
                break
            try:
                text = _gemini_call(model, contents, timeout=min(20.0, remaining), user_id=user_id)
                if _working_gemini_model != model:
                    _working_gemini_model = model
                    print(f"[AI] 使用モデル: Gemini {model}")
                return text
            except urllib.error.HTTPError as e:
                last_err = e
                _gemini_mark_dead(model, e.code)
                if e.code not in (400, 404, 429, 503):
                    raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
        if time.monotonic() + 6 < deadline:
            time.sleep(6)
    _gemini_start_cooldown()
    if last_err is not None:
        raise last_err
    raise RuntimeError("Gemini: 使えるモデルがありません")


async def ai_reply(user_id: int, text: str) -> str | None:
    """AIに考えてもらう返事（Groq優先→Gemini）。全部ダメなら None。"""
    history = _ai_history[user_id]
    reply: str | None = None
    ai_text = text
    if _is_aa_question(text) and user_langs.get(user_id) in (None, "ja"):
        fixed_aa = await asyncio.to_thread(_aa_unit_reply, text)
        if fixed_aa:
            print("[調べもの] アニメアドベンチャー（キャラ固定形式で回答）", flush=True)
            history.append(("user", text))
            history.append(("ai", fixed_aa))
            del history[:-24]
            return fixed_aa
    if _is_fortnite_question(text) and not _fortnite_has_strategy(text) and user_langs.get(user_id) in (None, "ja"):
        fixed_fn = await asyncio.to_thread(_fortnite_fixed_reply, text)
        if fixed_fn:
            print("[調べもの] Fortniteアプデ情報（固定形式で回答）", flush=True)
            history.append(("user", text))
            history.append(("ai", fixed_fn))
            del history[:-24]
            return fixed_fn
    if _is_update_question(text) and user_langs.get(user_id) in (None, "ja"):
        fixed = await asyncio.to_thread(_fixed_update_reply, text)
        if fixed:
            print("[調べもの] アプデ情報（固定形式で回答）", flush=True)
            history.append(("user", text))
            history.append(("ai", fixed))
            del history[:-24]
            return fixed
    kind, ctx = await asyncio.to_thread(_lookup_context, text)
    if ctx:
        ai_text = f"【調べた情報】\n{ctx}\n\n【ユーザーの発言】\n{text}"
    if GROQ_API_KEY:
        try:
            reply = await asyncio.to_thread(_groq_generate, list(history), ai_text, user_id)
        except Exception as e:
            print(f"[AI] Groq呼び出しに失敗: {e}")
    if not reply and GEMINI_API_KEY:
        contents = [
            {"role": "user" if who == "user" else "model", "parts": [{"text": body}]}
            for who, body in history
        ] + [{"role": "user", "parts": [{"text": ai_text}]}]
        try:
            reply = await asyncio.to_thread(_gemini_generate, contents, user_id)
        except Exception as e:
            print(f"[AI] Gemini呼び出しに失敗: {e}")
    if not reply:
        return None
    history.append(("user", text))
    history.append(("ai", reply))
    del history[:-24]  # 直近のやりとりだけ覚える
    return reply[:1000]


_openrouter_vision_cache: list[str] | None = None


def _openrouter_vision_models() -> list[str]:
    """OpenRouterで使える「無料＆画像が見れる」モデルを一覧から選び、優先順に並べる。"""
    global _openrouter_vision_cache
    if _openrouter_vision_cache:
        return _openrouter_vision_cache
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"User-Agent": "Mozilla/5.0 (compatible; mimi-discord-bot/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        free_vision: list[str] = []
        for item in data.get("data", []):
            pricing = item.get("pricing") or {}
            modality = (item.get("architecture") or {}).get("modality", "")
            if str(pricing.get("prompt", "")) == "0" and str(pricing.get("completion", "")) == "0" and "image" in modality:
                free_vision.append(item.get("id", ""))
        free_vision = [m for m in free_vision if m]

        def rank(m: str) -> int:
            low = m.lower()
            if "qwen" in low and ("vl" in low or "vision" in low):
                return 0
            if "gemini" in low:
                return 1
            if "vision" in low or "-vl" in low:
                return 2
            return 3

        free_vision.sort(key=rank)
        if free_vision:
            _openrouter_vision_cache = free_vision
            print(f"[AI] OpenRouterで使える無料の画像モデル: {', '.join(free_vision[:4])}", flush=True)
            return free_vision
        print("[AI] OpenRouterに無料の画像モデルが見つかりませんでした", flush=True)
    except Exception as e:
        print(f"[AI] OpenRouterのモデル一覧の取得に失敗: {e}", flush=True)
    return []


def _openrouter_vision_call(model: str, prompt: str, mime: str, b64: str, timeout: float = 30, user_id: int | None = None) -> str:
    """OpenRouterに画像を見せて答えてもらう（1回分）。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt(user_id)},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            },
        ],
        "max_tokens": 1600,
        "temperature": 0.8,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; mimi-discord-bot/1.0)",
            "HTTP-Referer": "https://mimi-discord-bot-6ol5.onrender.com",
            "X-Title": "tempus-discord-bot",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    choices = data.get("choices") or []
    if not choices:
        return ""
    content = choices[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content.strip() if isinstance(content, str) else ""


def _openrouter_vision_generate(prompt: str, mime: str, data: bytes, user_id: int | None = None) -> str:
    """OpenRouterの無料画像モデルを順に試す（最大約90秒）。全部ダメなら例外。"""
    b64 = base64.b64encode(data).decode("ascii")
    models = list(_openrouter_vision_models())
    if "openrouter/free" not in models:
        models.append("openrouter/free")
    if not models:
        raise RuntimeError("no free vision models on OpenRouter")
    deadline = time.monotonic() + 90
    last_err: Exception | None = None
    for model in models[:6]:
        if time.monotonic() > deadline:
            break
        try:
            text = _openrouter_vision_call(model, prompt, mime, b64, timeout=30, user_id=user_id)
            if text:
                print(f"[AI] 使用モデル: OpenRouter {model}（画像）", flush=True)
                return text
            print(f"[AI] OpenRouter {model} は空の返答でした", flush=True)
        except Exception as e:
            last_err = e
            print(f"[AI] OpenRouter {model} 失敗: {e}", flush=True)
    raise RuntimeError(f"OpenRouter vision failed: {last_err}")


def _gemini_vision_call(model: str, prompt: str, mime: str, b64: str, timeout: float = 20, user_id: int | None = None) -> str:
    """Geminiに画像を見せて答えてもらう（1回分）。"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": _system_prompt(user_id)}]},
        "contents": [
            {"role": "user", "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime, "data": b64}},
            ]}
        ],
        "generationConfig": {"maxOutputTokens": 1600, "temperature": 0.8},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    texts: list[str] = []
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            texts.append(part.get("text", ""))
    return "".join(texts).strip()


def _gemini_vision_generate(prompt: str, mime: str, data: bytes, user_id: int | None = None) -> str:
    """画像つきでGeminiを呼ぶ。使えるモデルだけを試し（404予防）、全部ダメならクールダウンする。"""
    if _gemini_in_cooldown():
        raise RuntimeError("Gemini cooldown（連続失敗のためお休み中）")
    b64 = base64.b64encode(data).decode("ascii")
    deadline = time.monotonic() + 40
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        models = _gemini_models()
        if not models:
            break
        for model in models:
            try:
                text = _gemini_vision_call(model, prompt, mime, b64, timeout=20, user_id=user_id)
                if text:
                    print(f"[AI] 使用モデル: Gemini {model}（画像）", flush=True)
                    return text
            except urllib.error.HTTPError as e:
                last_err = e
                _gemini_mark_dead(model, e.code)
                if e.code not in (400, 404, 429, 503):
                    raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
        if time.monotonic() + 5 < deadline:
            time.sleep(5)
    _gemini_start_cooldown()
    raise RuntimeError(f"Gemini vision failed: {last_err}")


async def _vision_reply(user_id: int, text: str, image: tuple[bytes, str]) -> str | None:
    """画像を見て答える。OpenRouter（無料）→Geminiの順に試し、失敗したら None。"""
    data, mime = image
    prompt = text.strip() or "この画像に何が写っているか教えて"
    reply = None
    if OPENROUTER_API_KEY:
        try:
            reply = await asyncio.to_thread(_openrouter_vision_generate, prompt, mime, data, user_id)
        except Exception as e:
            print(f"[AI] 画像の呼び出しに失敗（OpenRouter）: {e}", flush=True)
    if not reply and GEMINI_API_KEY:
        try:
            reply = await asyncio.to_thread(_gemini_vision_generate, prompt, mime, data, user_id)
        except Exception as e:
            print(f"[AI] 画像の呼び出しに失敗（Gemini）: {e}", flush=True)
    if not reply:
        return None
    history = _ai_history[user_id]
    history.append(("user", f"（画像を送った）{text}"))
    history.append(("ai", reply))
    del history[:-24]
    return reply[:1000]


async def smart_reply(user_id: int, text: str, image: tuple[bytes, str] | None = None) -> str:
    """名前を覚える処理はルールで、会話はAI（あれば）→ダメならルール返信。"""
    # 言語の明示的な切り替え（「英語で話して」「한국어로 말해줘」など）はAIに任せず即答する
    req_lang = _requested_lang(text)
    if req_lang and image is None:
        user_langs[user_id] = req_lang
        print(f"[言語] 切り替え希望で {_LANG_NAMES.get(req_lang, req_lang)} に設定 (user={user_id})", flush=True)
        _save_lang_bg(user_id, req_lang)
        history = _ai_history[user_id]
        history.append(("user", text))
        history.append(("ai", _LANG_CONFIRMATIONS[req_lang]))
        del history[:-24]
        return _LANG_CONFIRMATIONS[req_lang]
    _note_user_lang(user_id, text)
    m = re.search(r"名前は\s*(.+)", text.strip())
    if m:
        name = m.group(1).strip(" 　です。、")[:20]
        if name:
            user_names[user_id] = name
            _save_name_bg(user_id, name)
            return f"{name}って言うんだね！覚えたよ♪ よろしく、{name}！"
    if image is not None:
        if OPENROUTER_API_KEY or GEMINI_API_KEY:
            reply = await _vision_reply(user_id, text, image)
            if reply:
                return reply
            return "画像を見ようとしたんだけど、今は画像を見るAIが混んでて見れなかった…また後で試してみて！"
        return "ごめん、今は画像を見る準備ができてないや…"
    if GROQ_API_KEY or GEMINI_API_KEY:
        reply = await ai_reply(user_id, text)
        if reply:
            return reply
    return chat_reply(user_id, text)



# ---------- 調べもの（Web検索・Wikipedia・天気・ニュース） ----------
import html as _html
import urllib.parse as _urlparse

_UA = {"User-Agent": "Mozilla/5.0 (compatible; mimi-discord-bot/1.0)"}


def _http_get_text(url: str, timeout: float = 10) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _http_get_json(url: str, timeout: float = 10):
    return json.loads(_http_get_text(url, timeout))


def _strip_tags(x: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", x)).strip()


def _web_search(query: str) -> str:
    """Web検索（DuckDuckGo→Lite→Bingの順に試す）で上位のタイトル・要約・URLを返す。"""
    q = _urlparse.quote(query)
    try:
        page = _http_get_text("https://html.duckduckgo.com/html/?q=" + q)
        titles = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
        lines = []
        for i, (href, title) in enumerate(titles[:5]):
            url = href
            qs = _urlparse.parse_qs(_urlparse.urlparse("https:" + href if href.startswith("//") else href).query)
            if "uddg" in qs:
                url = qs["uddg"][0]
            snippet = _strip_tags(snippets[i]) if i < len(snippets) else ""
            lines.append(f"・{_strip_tags(title)}: {snippet} ({url})")
        if lines:
            return "\n".join(lines)
    except Exception as e:
        print(f"[調べもの] Web検索（DDG）に失敗: {e}")
    try:
        page = _http_get_text("https://lite.duckduckgo.com/lite/?q=" + q)
        titles = re.findall(r'class=.result-link.[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
        snippets = re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', page, re.S)
        lines = []
        for i, (href, title) in enumerate(titles[:5]):
            snippet = _strip_tags(snippets[i]) if i < len(snippets) else ""
            lines.append(f"・{_strip_tags(title)}: {snippet} ({href})")
        if lines:
            return "\n".join(lines)
    except Exception as e:
        print(f"[調べもの] Web検索（DDG Lite）に失敗: {e}")
    try:
        page = _http_get_text("https://www.bing.com/search?q=" + q)
        blocks = re.findall(r'<li class="b_algo".*?</li>', page, re.S)
        lines = []
        for b in blocks[:5]:
            tm = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
            if not tm:
                continue
            sm = re.search(r"<p[^>]*>(.*?)</p>", b, re.S)
            snippet = _strip_tags(sm.group(1)) if sm else ""
            lines.append(f"・{_strip_tags(tm.group(2))}: {snippet} ({tm.group(1)})")
        if lines:
            return "\n".join(lines)
    except Exception as e:
        print(f"[調べもの] Web検索（Bing）に失敗: {e}")
    return ""


def _wiki_lookup(query: str) -> str:
    """日本語Wikipediaで用語を調べ、冒頭の説明を返す。"""
    try:
        api = (
            "https://ja.wikipedia.org/w/api.php?action=query&format=json&prop=extracts"
            "&exintro=1&explaintext=1&generator=search&gsrlimit=3&gsrsearch="
            + _urlparse.quote(query)
        )
        data = _http_get_json(api)
        pages = (data.get("query") or {}).get("pages") or {}
        lines = []
        for pg in pages.values():
            extract = (pg.get("extract") or "").strip()
            if extract:
                lines.append(f"・{pg.get('title', '')}: {extract[:400]}")
        return "\n".join(lines)
    except Exception as e:
        print(f"[調べもの] Wikipedia取得に失敗: {e}")
        return ""


_WEATHER_CODES = {
    0: "快晴", 1: "晴れ", 2: "晴れ時々くもり", 3: "くもり", 45: "霧", 48: "霧",
    51: "霧雨", 53: "霧雨", 55: "霧雨", 56: "着氷性の霧雨", 57: "着氷性の霧雨",
    61: "弱い雨", 63: "雨", 65: "強い雨", 66: "着氷性の雨", 67: "着氷性の雨",
    71: "弱い雪", 73: "雪", 75: "強い雪", 77: "雪",
    80: "にわか雨", 81: "にわか雨", 82: "激しいにわか雨", 85: "にわか雪", 86: "にわか雪",
    95: "雷雨", 96: "雷雨（ひょう）", 99: "雷雨（ひょう）",
}


# 主要都市は座標を内蔵（Open-Meteoの地名検索は日本語名にマッチしないため）
_PLACE_COORDS = {
    "東京": (35.6895, 139.6917, "東京"), "横浜": (35.4437, 139.6380, "横浜"),
    "川崎": (35.5308, 139.7029, "川崎"), "さいたま": (35.8617, 139.6455, "さいたま"),
    "千葉": (35.6074, 140.1065, "千葉"), "大阪": (34.6938, 135.5023, "大阪"),
    "京都": (35.0116, 135.7681, "京都"), "神戸": (34.6901, 135.1956, "神戸"),
    "奈良": (34.6851, 135.8050, "奈良"), "和歌山": (34.2261, 135.1675, "和歌山"),
    "名古屋": (35.1815, 136.9066, "名古屋"), "札幌": (43.0621, 141.3544, "札幌"),
    "仙台": (38.2682, 140.8694, "仙台"), "福岡": (33.5904, 130.4017, "福岡"),
    "北九州": (33.8834, 130.8752, "北九州"), "広島": (34.3853, 132.4553, "広島"),
    "新潟": (37.9161, 139.0364, "新潟"), "静岡": (34.9756, 138.3828, "静岡"),
    "岡山": (34.6551, 133.9195, "岡山"), "熊本": (32.8062, 130.7058, "熊本"),
    "鹿児島": (31.5602, 130.5581, "鹿児島"), "那覇": (26.2124, 127.6809, "那覇"),
    "沖縄": (26.2124, 127.6809, "那覇"), "松山": (33.8392, 132.7657, "松山"),
    "高松": (34.3401, 134.0434, "高松"), "金沢": (36.5944, 136.6256, "金沢"),
    "長野": (36.6486, 138.1942, "長野"), "岐阜": (35.4233, 136.7606, "岐阜"),
    "福井": (36.0652, 136.2216, "福井"), "富山": (36.6959, 137.2137, "富山"),
    "甲府": (35.6642, 138.5684, "甲府"), "前橋": (36.3895, 139.0634, "前橋"),
    "宇都宮": (36.5551, 139.8829, "宇都宮"), "水戸": (36.3418, 140.4468, "水戸"),
    "福島": (37.7607, 140.4747, "福島"), "山形": (38.2404, 140.3633, "山形"),
    "盛岡": (39.7036, 141.1527, "盛岡"), "秋田": (39.7186, 140.1024, "秋田"),
    "青森": (40.8244, 140.7400, "青森"), "松江": (35.4723, 133.0486, "松江"),
    "鳥取": (35.5039, 134.2378, "鳥取"), "山口": (34.1859, 131.4714, "山口"),
    "徳島": (34.0703, 134.5548, "徳島"), "高知": (33.5490, 133.5311, "高知"),
    "宮崎": (31.9111, 131.4202, "宮崎"), "大分": (33.2382, 131.6126, "大分"),
    "佐賀": (33.2494, 130.2988, "佐賀"), "長崎": (32.7448, 129.8737, "長崎"),
    "大津": (35.0045, 135.8686, "大津"), "津": (34.7303, 136.5086, "津"),
}


def _weather_lookup(place: str) -> str:
    """Open-Meteo（キー不要）で3日分の天気予報を返す。主要都市は内蔵座標を使う。"""
    try:
        lat = lon = None
        disp = place
        for key in sorted(_PLACE_COORDS, key=len, reverse=True):
            if key in place:
                lat, lon, disp = _PLACE_COORDS[key]
                break
        if lat is None:
            geo = _http_get_json(
                "https://geocoding-api.open-meteo.com/v1/search?count=1&language=ja&format=json&name="
                + _urlparse.quote(place)
            )
            results = geo.get("results") or []
            if not results:
                return ""
            top = results[0]
            lat, lon, disp = top["latitude"], top["longitude"], top.get("name", place)
        fc = _http_get_json(
            "https://api.open-meteo.com/v1/forecast?timezone=Asia%2FTokyo&forecast_days=3"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            f"&latitude={lat}&longitude={lon}"
        )
        daily = fc.get("daily") or {}
        labels = ["今日", "明日", "明後日"]
        lines = [f"{disp}の天気予報（Open-Meteo）:"]
        for i in range(min(3, len(daily.get("time", [])))):
            code = daily["weather_code"][i]
            lines.append(
                f"・{labels[i]}: {_WEATHER_CODES.get(code, '不明')}、"
                f"最高{daily['temperature_2m_max'][i]}℃／最低{daily['temperature_2m_min'][i]}℃、"
                f"降水確率{daily['precipitation_probability_max'][i]}%"
            )
        return "\n".join(lines)
    except Exception as e:
        print(f"[調べもの] 天気取得に失敗: {e}")
        return ""


def _news_lookup() -> str:
    """NHKニュースのRSSから最新見出しを返す。"""
    try:
        xml = _http_get_text("https://www3.nhk.or.jp/rss/news/cat0.xml")
        items = re.findall(r"<item>(.*?)</item>", xml, re.S)
        lines = ["最新のニュース見出し（NHK）:"]
        for item in items[:8]:
            m = re.search(r"<title>(.*?)</title>", item, re.S)
            if m:
                title = m.group(1).replace("<![CDATA[", "").replace("]]>", "")
                lines.append(f"・{_strip_tags(title)}")
        return "\n".join(lines) if len(lines) > 1 else ""
    except Exception as e:
        print(f"[調べもの] ニュース取得に失敗: {e}")
        return ""


# ゲーム名の別名→英語名（検索用）と、更新ログWiki
_GAME_ALIASES = {
    "アニメヴァンガード": "Anime Vanguards",
    "アニメヴェンカード": "Anime Vanguards",
    "アニメバンガード": "Anime Vanguards",
    "ヴァンガード": "Anime Vanguards",
    "バンガード": "Anime Vanguards",
    "ヴェンカード": "Anime Vanguards",
}
_GAME_WIKIS = {
    "Anime Vanguards": "https://wiki.vanguards.gg/Update_Log",
}


# 既知のゲームはユニバースIDで直接引く（検索だと別ゲームや期間限定の改名に埋もれるため）
_GAME_UNIVERSES = {
    "Anime Vanguards": 5578556129,
    "Anime Adventures": 3183403065,
}


def _roblox_game_info(title: str) -> str:
    """Roblox公式APIでゲーム情報（名前・説明文・プレイ人数・更新日）を返す。"""
    try:
        uid = _GAME_UNIVERSES.get(title)
        if uid:
            g = _http_get_json(f"https://games.roblox.com/v1/games?universeIds={uid}")
            gd = (g.get("data") or [{}])[0]
            if gd:
                lines = [
                    f"Roblox公式ゲーム情報「{gd.get('name', '')}」（同時プレイ {gd.get('playing', 0)}人 / 最終更新 {str(gd.get('updated', ''))[:10]}）:"
                ]
                desc = (gd.get("description") or "").strip()
                if desc:
                    lines.append(desc[:1500])
                return "\n".join(lines)
        data = _http_get_json(
            "https://apis.roblox.com/search-api/omni-search?searchQuery="
            + _urlparse.quote(title) + "&sessionId=tempusbot"
        )
        contents = []
        for group in data.get("searchResults", []):
            if group.get("contentGroupType") == "Game":
                contents.extend(group.get("contents", []))
        if not contents:
            return ""
        tokens = [t for t in re.split(r"\s+", title.lower()) if len(t) >= 3]

        def score(c):
            n = (c.get("name") or "").lower()
            return (sum(len(t) for t in tokens if t in n), c.get("playerCount", 0))

        best = max(contents, key=score)
        if score(best)[0] == 0:
            return ""
        lines = [f"Roblox公式ゲーム情報「{best.get('name', '')}」（同時プレイ {best.get('playerCount', 0)}人）:"]
        desc = (best.get("description") or "").strip()
        if desc:
            lines.append(desc[:1500])
        return "\n".join(lines)
    except Exception as e:
        print(f"[調べもの] Robloxゲーム情報の取得に失敗: {e}")
        return ""


# 英語の攻略・コードまとめサイト（ゲームページにコード表と最新アプデ記事がある）
_GAME_GUIDES = {
    "Anime Vanguards": "https://bloxguidesgg.com/games/anime-vanguards",
}


def _game_guide_lookup(eng: str) -> list[str]:
    """英語のガイドサイトから、コード表と最新アプデ記事（新ユニット等）を持ってくる。"""
    url = _GAME_GUIDES.get(eng)
    if not url:
        return []
    parts = []
    try:
        page = _http_get_text(url, timeout=12)
    except Exception as e:
        print(f"[調べもの] ガイドサイトの取得に失敗: {e}")
        return parts
    plain = _strip_tags(re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S))
    plain = re.sub(r"\s+", " ", plain)
    m2 = re.search(r"Latest [\w ]{0,40}Codes", plain) or re.search(r"Code ?Reward", plain)
    if m2:
        parts.append("コード一覧（英語サイト）: " + plain[m2.start():m2.start() + 1100])
    m = re.search(r'href="(/blog/[^"]*update[^"]*)"', page)
    if m:
        try:
            art = _http_get_text("https://bloxguidesgg.com" + m.group(1), timeout=12)
            aplain = _strip_tags(re.sub(r"<script.*?</script>|<style.*?</style>", "", art, flags=re.S))
            aplain = re.sub(r"\s+", " ", aplain)
            occ = [mm.start() for mm in re.finditer(r"Every new unit", aplain)]
            j = occ[1] if len(occ) >= 2 else (occ[0] if occ else -1)
            if j < 0:
                j = aplain.find("Unit Rarity")
            if j >= 0:
                parts.append("最新アプデの新ユニット（英語サイト）: " + aplain[j:j + 1500])
            # 記事内の「コードの答え」の段落も拾う（次のアプデでも同じ形式で出る）
            cocc = [mm.start() for mm in re.finditer(r"What are the .*? codes\?", aplain)]
            k = cocc[1] if len(cocc) >= 2 else -1
            if k >= 0:
                parts.append("最新アプデのコード解説（英語サイト）: " + aplain[k:k + 1200])
        except Exception as e:
            print(f"[調べもの] アプデ記事の取得に失敗: {e}")
    return parts


# ---------- Fortnite（フォートナイト）専用の情報取得 ----------
# Epic公式系API（fortnite-api.com）＋日本語ニュース（FNJPNews、Xの公式発表・リークをまとめている）
# ＋武器プール（fortnite.gg）＋次回アプデ予定（ロードマップまとめ）から集める。
_FORTNITE_ALIASES = ("フォートナイト", "フォトナ", "Fortnite", "fortnite", "FORTNITE")
_FORTNITE_STRATEGY_WORDS = ("降り", "着地", "武器", "立ち回り", "マップ", "強い", "ティア", "有利", "勝ち方", "ビクロイ", "landing", "weapon", "rotation", "drop")
_FORTNITE_ITEM_WORDS = ("Potion", "Meat", "Med Kit", "Tangerine", "Bandage", "Slurp", "Chug", "Apple", "Fishing Rod", "Flopper", "Jellyfish", "Pepper", "Fish", "Fry", "Extractor", "Deployer", "Vault Key", "Grenade")


def _is_fortnite_question(text: str) -> bool:
    """フォートナイトのアプデ・攻略を聞いている発言かどうか。"""
    if not any(a in text for a in _FORTNITE_ALIASES):
        return False
    low = text.lower()
    return ("アプデ" in text or "アップデート" in text or "次回" in text
            or "次のアプデ" in text or bool(re.search(r"次.{0,4}(いつ|シーズン|チャプター)", text))
            or any(w in text for w in ("最新", "情報", "シーズン"))
            or any(w in text for w in _FORTNITE_STRATEGY_WORDS)
            or any(w in low for w in ("update", "next update", "best")))


def _fortnite_has_strategy(text: str) -> bool:
    return any(w in text or w in text.lower() for w in _FORTNITE_STRATEGY_WORDS)


def _fortnite_data() -> dict:
    """Fortniteの現在データ（バージョン・ニュース・新アイテム・マップ・武器）を公式系API等から集める。"""
    d: dict = {"version": "", "date": "", "news": [], "news_body": "", "items": [], "pois": [], "weapons": []}
    try:
        cos = _http_get_json("https://fortnite-api.com/v2/cosmetics/new?language=ja", timeout=12)
        dd = cos.get("data") or {}
        bm = re.search(r"Release-(\d+)\.(\d+)", dd.get("build", ""))
        if bm:
            d["version"] = f"{bm.group(1)}.{bm.group(2)}"
        raw_date = str(dd.get("date", ""))[:10]
        if re.match(r"\d{4}-\d{2}-\d{2}", raw_date):
            _y, mo, da = raw_date.split("-")
            d["date"] = f"{int(mo)}月{int(da)}日"
        items = ((dd.get("items") or {}).get("br")) or []
        for it in items[:30]:
            nm = it.get("name", "")
            tp = (it.get("type") or {}).get("displayValue", "")
            if nm:
                d["items"].append(f"{nm}（{tp}）" if tp else nm)
    except Exception as e:
        print(f"[調べもの] Fortnite新アイテム取得に失敗: {e}", flush=True)
    try:
        news = _http_get_json("https://fortnite-api.com/v2/news/br?language=ja", timeout=12)
        motds = ((news.get("data") or {}).get("motds")) or []
        for m in motds[:5]:
            if m.get("title"):
                d["news"].append(m["title"])
        if motds and motds[0].get("body"):
            d["news_body"] = motds[0]["body"][:400]
    except Exception as e:
        print(f"[調べもの] Fortniteニュース取得に失敗: {e}", flush=True)
    try:
        mp = _http_get_json("https://fortnite-api.com/v1/map?language=ja", timeout=12)
        for p in ((mp.get("data") or {}).get("pois")) or []:
            nm = p.get("name", "")
            if nm and "Rocky" in nm and "$" in nm:
                nm = "A$AP Rockyの拠点"  # 公式APIの伏せ字表記（$.$. Rocky）を読みやすく直す
            if nm and "UnNamed" not in p.get("id", "") and nm != "宝箱" and nm not in d["pois"]:
                d["pois"].append(nm)
    except Exception as e:
        print(f"[調べもの] Fortniteマップ取得に失敗: {e}", flush=True)
    try:
        page = _http_get_text("https://fortnite.gg/weapons", timeout=12)
        names: list[str] = []
        for raw in re.findall(r"<h3[^>]*>([^<]+)</h3>", page):
            nm = _html.unescape(raw).strip()
            if nm and nm not in names and not any(w in nm for w in _FORTNITE_ITEM_WORDS):
                names.append(nm)
        d["weapons"] = names[:20]
    except Exception as e:
        print(f"[調べもの] Fortnite武器一覧の取得に失敗: {e}", flush=True)
    return d


def _fortnite_next_update() -> str:
    """次回アプデの予定を複数の海外サイト＋予定表＋Web検索（Xの投稿を含む）から集めてまとめる。無ければ ""。"""
    today = datetime.now(timezone(timedelta(hours=9))).date()
    lines: list[str] = []
    ch8_date = ""
    ch8_sources = 0
    # 海外まとめサイト1: esports.net（チャプター8ガイド）
    try:
        page = _http_get_text("https://www.esports.net/wiki/guides/fortnite-chapter-8/", timeout=12)
        plain = re.sub(r"\s+", " ", _strip_tags(re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)))
        m = re.search(r"Chapter 8 \(v(\d+\.\d+)\) is set to launch on ([A-Z][a-z]+) (\d+), (\d+)", plain)
        if m and m.group(2) in _MONTHS:
            ch8_date = f"{_MONTHS[m.group(2)]}月{m.group(3)}日"
            ch8_sources += 1
            lines.append(f"次の大きなアプデはチャプター8（v{m.group(1)}）で、{ch8_date}開始予定")
    except Exception as e:
        print(f"[調べもの] Fortnite次回アプデ取得に失敗(esports): {e}", flush=True)
    # 海外まとめサイト2: Sportskeeda（2026ロードマップ）— 日付が一致するか照合し、その次の日付も拾う
    try:
        page = _http_get_text("https://opera.sportskeeda.com/fortnite/fortnite-2026-update-roadmap-revealed", timeout=12)
        plain = re.sub(r"\s+", " ", _strip_tags(re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)))
        m = re.search(r"Chapter 8 Season 1 is expected to start on ([A-Z][a-z]+) (\d+), (\d+)", plain)
        if m and m.group(1) in _MONTHS:
            d2 = f"{_MONTHS[m.group(1)]}月{m.group(2)}日"
            if ch8_date and d2 == ch8_date:
                ch8_sources += 1
            elif not ch8_date:
                ch8_date = d2
                ch8_sources += 1
                lines.append(f"次の大きなアプデはチャプター8で、{d2}開始予定")
        m2 = re.search(r"Chapter 8 Season 1\s*[A-Z][a-z]+ \d+, \d{4}\s*([A-Z][a-z]+) (\d+), (\d{4})", plain)
        if m2 and m2.group(1) in _MONTHS:
            lines.append(f"その次の予定日として{_MONTHS[m2.group(1)]}月{m2.group(2)}日もロードマップに掲載")
    except Exception as e:
        print(f"[調べもの] Fortnite次回アプデ取得に失敗(sportskeeda): {e}", flush=True)
    if lines and ch8_sources >= 2:
        lines[0] += "（複数の海外サイトで一致）"
    # 海外の予定表サイト: TechWiser（バージョンと日付の表から、未来の分だけ拾う）
    try:
        page = _http_get_text("https://techwiser.com/next-fortnite-update/", timeout=12)
        plain = re.sub(r"\s+", " ", _strip_tags(re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)))
        seen_v: set[str] = set()
        for vm in re.finditer(r"v(\d+)\.(\d+)([A-Z][a-z]+) (\d+)(?:st|nd|rd|th), (\d{4})", plain):
            if vm.group(3) not in _MONTHS:
                continue
            d = datetime(int(vm.group(5)), _MONTHS[vm.group(3)], int(vm.group(4))).date()
            ver = f"{vm.group(1)}.{vm.group(2)}"
            if d > today and ver not in seen_v:
                seen_v.add(ver)
                lines.append(f"v{ver}は{d.month}月{d.day}日予定（海外の予定表サイト）")
            if len(seen_v) >= 2:
                break
    except Exception as e:
        print(f"[調べもの] Fortnite次回アプデ取得に失敗(techwiser): {e}", flush=True)
    # Web検索（Xの公式・リーカーの投稿が混ざる）から、日付つきの新しい話題を拾う
    try:
        for q in ("Fortnite next update date", "フォートナイト 次回アップデート いつ"):
            r = _web_search(q)
            if not r:
                continue
            for ln in r.split("\n"):
                ln = re.sub(r"\s*\(https?://.*$", "", ln).strip("・ ")
                if not ln or len(ln) < 12:
                    continue
                if not re.search(r"(November|December|1[12]月\d+日|Chapter 8|チャプター8)", ln):
                    continue
                if ch8_date and ("November 28" in ln or "11月28日" in ln):
                    continue  # チャプター8の日付は上の行でまとめ済み
                lines.append(f"Web検索より: {ln[:110]}")
                break
            if len(lines) >= 5:
                break
    except Exception as e:
        print(f"[調べもの] Fortnite次回アプデ取得に失敗(検索): {e}", flush=True)
    lines = lines[:4]
    if lines:
        lines.append("※予定・リーク系の情報で、公式確定ではない")
    return "\n".join(lines)


def _fortnite_jp_news() -> list[str]:
    """日本語のFortniteニュースサイト（Xの公式発表・リークをまとめている）の見出しを拾う。"""
    try:
        page = _http_get_text("https://fnjpnews.com/", timeout=12)
        plain = re.sub(r"\s+", " ", _strip_tags(re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)))
        heads: list[str] = []
        for m in re.finditer(r"【フォートナイト】([^【】]{8,60}?)(?=Fortnite|【|$)", plain):
            raw_h = m.group(1)
            # 古い記事や無関係な人気記事（旧バージョン・デバイス紹介など）は除外する
            if any(x in raw_h for x in ("2022年", "2025年", "デバイス", "感度", "エラーが頻発", "年齢制限")):
                continue
            if re.search(r"v4[0-2]\.", raw_h):
                continue
            h = raw_h.strip(" 　|｜")
            h = re.sub(r"(\d{4}年\d{1,2}月\d{1,2}日|新着記事).*$", "", h).strip(" 　|｜")
            if not h or h in heads:
                continue
            heads.append(h)
        # 今のバージョン（v43系）やチャプターの話題を先に並べる
        heads.sort(key=lambda h: 0 if ("v43" in h or "43.10" in h or "チャプター" in h) else 1)
        return heads[:5]
    except Exception as e:
        print(f"[調べもの] Fortnite日本語ニュース取得に失敗: {e}", flush=True)
        return []


def _fortnite_context(text: str) -> str:
    """AIに渡すFortnite情報（公式系API＋日本語ニュース＋武器＋マップ＋次回予定）。"""
    d = _fortnite_data()
    parts = []
    if d["version"]:
        parts.append(f"現在のバージョン: v{d['version']}（{d['date']}更新）")
    if d["news"]:
        parts.append("ゲーム内ニュース（公式）: " + " / ".join(d["news"]))
    if d["news_body"]:
        parts.append("注目ニュースの内容: " + d["news_body"])
    jp = _fortnite_jp_news()
    if jp:
        parts.append("日本語ニュース見出し（Xの公式発表・リークまとめサイトより）: " + " / ".join(jp))
    if d["items"]:
        parts.append("最近追加されたアイテム: " + "、".join(d["items"][:15]))
    nxt = _fortnite_next_update()
    if nxt:
        parts.append("次回アプデ予定: " + nxt)
    if d["pois"]:
        parts.append("今のマップの地名（POI）: " + "、".join(d["pois"]))
    if d["weapons"]:
        parts.append("現在の武器プール（英語名）: " + ", ".join(d["weapons"]))
    print("[調べもの] Fortnite情報を収集", flush=True)
    return "\n".join(parts)


def _fortnite_fixed_reply(text: str) -> str | None:
    """Fortniteのアプデ質問には、公式系APIの情報から決まった形式で直接答える。"""
    d = _fortnite_data()
    if not d["version"] and not d["news"]:
        return None
    lines = [f"**フォートナイト v{d['version']} アプデ情報**" + (f"（{d['date']}）" if d["date"] else ""), ""]
    if d["news"]:
        lines.append("🆕 ゲーム内ニュース（公式）")
        for t in d["news"]:
            lines.append(f"・{t}")
        lines.append("")
    jp = _fortnite_jp_news()
    if jp:
        lines.append("📰 最新ニュース・リーク（日本語まとめ）")
        for h in jp[:3]:
            lines.append(f"・{h}")
        lines.append("")
    if d["items"]:
        lines.append("🎁 最近の新アイテム例")
        lines.append("、".join(d["items"][:10]))
        lines.append("")
    nxt = _fortnite_next_update()
    if nxt:
        lines.append("🔜 次のアプデ")
        lines.append(nxt)
        lines.append("")
    if d["pois"]:
        lines.append("📍 今のマップの地名")
        lines.append("、".join(d["pois"]))
        lines.append("")
    lines.append("情報元：Epic公式系API・日本語ニュース（Xの公式発表まとめ）・海外まとめサイト複数・Web検索")
    lines.append("降り場・武器・立ち回りのおすすめは「フォートナイト 降りる場所 おすすめ」みたいに聞いてね")
    return "\n".join(lines)[:980]


# ---------- アニメアドベンチャー（Roblox） ----------
# 情報元: Roblox公式API + 海外Wiki（Anime Adventures Wiki / Fandom）のAPI + Web検索。
# キャラ名を言われたら、詳細・覚醒（進化）素材・素材の入手先を分けて固定形式で返す。
_AA_API = "https://anime-adventures.fandom.com/api.php"
_AA_ALIASES = ("アニメアドベンチャー", "アニメアドベンチャーズ", "Anime Adventures", "anime adventures")
_AA_NAME_SEEDS = {
    "ルフィ": "Luffy", "ゾロ": "Zoro", "サンジ": "Sanji", "ナミ": "Nami", "チョッパー": "Chopper",
    "エース": "Ace", "ロー": "Law", "シャンクス": "Shanks", "ミホーク": "Mihawk", "カイドウ": "Kaido",
    "キャプテン": "Captain",
    "悟空": "Goku", "ゴクウ": "Goku", "ベジータ": "Vegeta", "悟飯": "Gohan",
    "ナルト": "Naruto", "サスケ": "Sasuke", "サクラ": "Sakura", "カカシ": "Kakashi", "ヒナタ": "Hinata",
    "五条悟": "Gojo", "五条": "Gojo", "宿儺": "Sukuna", "虎杖": "Yuji", "虎杖悠仁": "Yuji",
    "伏黒": "Megumi", "伏黒恵": "Megumi", "釘崎": "Nobara",
    "炭治郎": "Tanjiro", "竈門炭治郎": "Tanjiro", "禰豆子": "Nezuko", "善逸": "Zenitsu",
    "伊之助": "Inosuke", "煉獄": "Rengoku", "煉獄杏寿郎": "Rengoku",
    "エレン": "Eren", "ミカサ": "Mikasa", "リヴァイ": "Levi",
    "デク": "Deku", "緑谷": "Deku", "爆豪": "Bakugo", "轟": "Todoroki",
    "ゴン": "Gon", "キルア": "Killua", "クラピカ": "Kurapika", "ヒソカ": "Hisoka",
    "ジョルノ": "Giorno", "承太郎": "Jotaro", "ディオ": "Dio",
    "アスタ": "Asta", "ユノ": "Yuno", "一護": "Ichigo", "ルキア": "Rukia",
}
_AA_SKIP_TITLES = ("Disambiguation", "Tier List", "Unit List", "Update Log", "Codes", "Items", "Events", "Wiki", "FAQ", "Frequently")
_aa_items_cache: str | None = None


def _is_aa_question(text: str) -> bool:
    return any(a in text for a in _AA_ALIASES)


def _aa_unit_query(text: str) -> str:
    """発言からキャラ名（検索用）を取り出す。キャラの話でなければ "" を返す。"""
    t = text
    for jp in sorted(_AA_NAME_SEEDS, key=len, reverse=True):
        if jp in t:
            return _AA_NAME_SEEDS[jp]
    for a in _AA_ALIASES:
        t = t.replace(a, " ")
    for w in ("覚醒素材", "進化素材", "覚醒", "進化", "エボリューション", "素材", "詳細", "情報",
              "アプデ", "アップデート", "次の", "次回", "最新", "コード", "教えて", "について",
              "キャラ", "キャラクター", "ユニット", "どこ", "貰える", "もらえる", "入手",
              "の", "を", "が", "は", "って", "です", "か", "？", "?", "！", "!", "、", "。"):
        t = t.replace(w, " ")
    return re.sub(r"\s+", " ", t).strip()


def _aa_search(query: str, limit: int = 6) -> list[str]:
    try:
        d = _http_get_json(_AA_API + "?" + _urlparse.urlencode(
            {"action": "query", "list": "search", "srsearch": query, "srlimit": limit, "format": "json"}), timeout=15)
        return [s.get("title", "") for s in d.get("query", {}).get("search", []) if s.get("title")]
    except Exception as e:
        print(f"[調べもの] AA Wiki検索に失敗: {e}", flush=True)
        return []


def _aa_page_text(title: str) -> str:
    try:
        d = _http_get_json(_AA_API + "?" + _urlparse.urlencode(
            {"action": "parse", "page": title, "prop": "text", "format": "json"}), timeout=15)
        h = d["parse"]["text"]["*"]
        t = re.sub(r"<script.*?</script>|<style.*?</style>", "", h, flags=re.S)
        t = _html.unescape(re.sub(r"<[^>]+>", " ", t))
        return re.sub(r"\s+", " ", t)
    except Exception as e:
        print(f"[調べもの] AA Wikiページ取得に失敗({title}): {e}", flush=True)
        return ""


def _aa_items_text() -> str:
    global _aa_items_cache
    if _aa_items_cache is None:
        _aa_items_cache = _aa_page_text("Items")
    return _aa_items_cache


# 調べた範囲で確認できた素材の入手先（WikiのItemsページに載っていないもの）
_AA_SOURCE_OVERRIDES = {
    "SMILE Fruit": "Puppet IslandのInfiniteモード／Puppetポータル／Operatorのデイリーミッション（英語ガイドサイト情報）",
}


def _aa_material_source(name: str) -> str:
    """素材の入手先を、確認済み情報→WikiのItemsページ→Web検索の順に探す。見つからなければ ""。"""
    if name in _AA_SOURCE_OVERRIDES:
        return _AA_SOURCE_OVERRIDES[name]
    items = _aa_items_text()
    if items:
        i = items.find(name)
        if i >= 0:
            seg = items[i + len(name): i + len(name) + 240].strip(" :：-")
            seg = seg.split("You can hold")[0].strip()
            seg = seg.replace("Used for crafting evolution items. ", "").replace("Used for crafting evolution items.", "")
            if len(seg) >= 10:
                return seg[:80]
    if "Star Fruit" in name and name != "Star Fruit":
        return _aa_material_source("Star Fruit")
    keys = [name.split(" ")[0], name.split(" ")[-1]]
    for qname in ([name, "Fake Fruit"] if name == "SMILE Fruit" else [name]):
        try:
            r = _web_search(f"Anime Adventures {qname} how to get")
        except Exception:
            r = ""
        if r:
            for ln in r.split("\n"):
                ln = re.sub(r"\s*\(https?://.*$", "", ln).strip("・ ")
                if any(k in ln for k in keys) and len(ln) >= 15:
                    return ln[:80]
    return ""


def _aa_unit_reply(text: str) -> str | None:
    """キャラ名から、詳細・覚醒素材・素材の入手先を分けた固定形式の返事を作る。"""
    query = _aa_unit_query(text)
    if not query:
        return None
    want_evo = any(w in text for w in ("素材", "覚醒", "進化", "エボ"))
    titles = [t for t in _aa_search(query) if not any(s in t for s in _AA_SKIP_TITLES)]
    if not titles:
        return None
    q = query.lower()
    titles.sort(key=lambda t: (t.lower() != q, not t.lower().startswith(q), len(t)))
    chosen = None
    chosen_txt = ""
    for cand in titles[:4 if want_evo else 1]:
        txt = _aa_page_text(cand)
        if not txt:
            continue
        if chosen is None:
            chosen, chosen_txt = cand, txt
        if "can evolve into" in txt:
            chosen, chosen_txt = cand, txt
            break
        if not want_evo:
            break
    if chosen is None:
        return None
    title, txt = chosen, chosen_txt
    lines = [f"**{title}**（アニメアドベンチャー）", "【キャラ詳細】"]
    m = re.search(re.escape(title) + r" is an?\s+([A-Za-z]+)", txt) or re.search(r"is an?\s+(Mythical|Secret|Legendary|Epic|Rare|Uncommon|Common)\b", txt)
    if m:
        lines.append(f"レア度: {m.group(1)}")
    mo = re.search(r"obtainable through ([^.]*)\.", txt)
    if mo:
        seg = mo.group(1)
        if "summon" in seg:
            lines.append("入手: 召喚（ガチャ）")
        else:
            lines.append(f"入手: {seg.strip()[:60]}")
    cost = re.search(r"Deployment Cost ([\d,]+)", txt)
    total = re.search(r"Total Cost ([\d,]+)", txt)
    if cost:
        lines.append(f"配置コスト: {cost.group(1)}" + (f" / 総コスト: {total.group(1)}" if total else ""))
    tt = re.search(r"Tower Type ([A-Za-z]+)", txt)
    dt = re.search(r"Damage Type ([A-Za-z]+)", txt)
    if tt or dt:
        lines.append("タイプ: " + " / ".join(x for x in [tt.group(1) if tt else "", f"ダメージ {dt.group(1)}" if dt else ""] if x))
    dmg = re.search(r"Damage ([\d,]+) -> ([\d,]+)", txt)
    rng = re.search(r"Range ([\d.]+)", txt)
    if dmg:
        lines.append(f"ダメージ: {dmg.group(1)} → {dmg.group(2)}" + (f" / 射程: {rng.group(1)}" if rng else ""))
    em = re.search(r"can evolve into (.+?) with the following materials: Required Items: (.+?) Result ", txt)
    lines.append("【覚醒（進化）】")
    if em:
        evolved = em.group(1).strip()
        mats = [(c.replace(",", ""), n.strip()) for c, n in re.findall(r"x([\d,]+)\s+(.+?)(?= x[\d,]+ |$)", em.group(2))]
        lines.append(f"進化先: {evolved}")
        tk = re.search(r"must have ([\d,]+) Takedowns", txt)
        if tk:
            lines.append(f"必要討伐数: {tk.group(1)}")
        lines.append("必要素材:")
        for c, n in mats:
            lines.append(f"・{n} ×{c}")
        srcs = []
        for c, n in mats:
            if n == title:
                continue
            src = _aa_material_source(n)
            srcs.append(f"・{n}: {src}" if src else f"・{n}: （入手先はWikiで未確認）")
        if srcs:
            lines.append("【素材の入手先】")
            lines.extend(srcs)
    else:
        lines.append("このキャラに進化はないみたい（Wikiに進化情報なし）")
    lines.append("情報元: Anime Adventures Wiki（海外）")
    out = "\n".join(lines)
    if len(out) > 980:
        # 素材が多いときは入手先の行を短くして、情報元まで収める
        lines = [ln[:100] if ln.startswith("・") and ": " in ln else ln for ln in lines]
        out = "\n".join(lines)
    if len(out) > 980:
        out = out[:950] + "\n情報元: Anime Adventures Wiki（海外）"
    return out


def _aa_context(text: str) -> str:
    """アニメアドベンチャーの情報・アプデ・次回アプデ用の調べもの材料を集める。"""
    parts = []
    info = _roblox_game_info("Anime Adventures")
    if info:
        parts.append(info)
    codes = _aa_page_text("Codes")
    if codes:
        i = codes.lower().find("active")
        seg = codes[i:i + 1100] if i >= 0 else codes[:1100]
        parts.append("Wikiのコードページ（英語）:\n" + seg)
    uq = _aa_unit_query(text)
    if uq:
        titles = [t for t in _aa_search(uq) if not any(s in t for s in _AA_SKIP_TITLES)]
        if titles:
            t = _aa_page_text(titles[0])
            if t:
                parts.append(f"キャラ「{titles[0]}」のWiki情報（英語）:\n" + t[:1800])
    for q in ("Anime Adventures update 21 new units patch notes", "Anime Adventures next update update 22"):
        try:
            r = _web_search(q)
            if r:
                parts.append(f"Web検索「{q}」:\n" + r)
        except Exception:
            pass
    return "\n\n".join(parts)


def _game_update_lookup(text: str) -> str:
    """ゲームの最新アップデート情報を、Roblox公式情報＋Web検索で集める。"""
    title = text
    for w in ("アップデート", "アプデ", "最新情報", "最新", "情報", "教えて", "知りたい", "調べて", "って何", "とは", "について", "の", "は", "を", "に", "？", "?", "！", "!", "。", "、", " "):
        title = title.replace(w, "")
    title = title.strip()
    eng = _GAME_ALIASES.get(title, "")
    if not eng:
        for k, v in _GAME_ALIASES.items():
            if k in text:
                eng = v
                break
    if not eng and title in ("", "ゲーム"):
        eng = "Anime Vanguards"
        title = "アニメヴェンカード"
    parts = []
    info = _roblox_game_info(eng or title)
    if info:
        parts.append(info)
    if eng:
        parts.extend(_game_guide_lookup(eng))
    name_for_search = eng or title
    for q in (f'"{name_for_search}" update', f"{name_for_search} アップデート 最新"):
        r = _web_search(q)
        if not r:
            continue
        if eng:
            kept = [ln for ln in r.split("\n") if "vanguard" in ln.lower()]
            if not kept:
                continue
            r = "\n".join(kept)
        parts.append(r)
        break
    wiki_url = _GAME_WIKIS.get(eng)
    if wiki_url:
        try:
            page = _http_get_text(wiki_url, timeout=12)
            plain = _strip_tags(re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S))
            plain = re.sub(r"\s+", " ", plain)
            m = re.search(r"archive of all update additions", plain) or re.search(r"(Update|アップデート)", plain)
            seg = plain[m.start():m.start() + 1600] if m else plain[:1600]
            if seg:
                parts.append(f"公式Wiki更新一覧（英語）: {seg}")
        except Exception as e:
            print(f"[調べもの] Wiki更新ログの取得に失敗: {e}")
    print(f"[調べもの] アプデ情報: {title or text} -> {eng or '-'}", flush=True)
    return "\n".join(parts)


def _is_update_question(text: str) -> bool:
    """ゲームのアプデ情報を聞いている発言かどうか。"""
    if "アプデ" in text or "アップデート" in text:
        return True
    return any(k in text for k in _GAME_ALIASES) and any(w in text for w in ("最新", "情報", "コード"))


_MONTHS = {"January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
           "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12}


def _fixed_update_reply(text: str) -> str | None:
    """アプデ質問には、取得した情報から決まった形式で直接答える（AI任せにしない）。
    対応できないゲーム・取得失敗のときは None（通常のAI回答に任せる）。"""
    title = text
    for w in ("アップデート", "アプデ", "最新", "情報", "教えて", "知りたい", "の", "は", "を", "に", "？", "?", "！", "!", "。", "、", "・"):
        title = title.replace(w, "")
    title = title.strip()
    eng = _GAME_ALIASES.get(title, "")
    if not eng:
        for k, v in _GAME_ALIASES.items():
            if k in text:
                eng = v
                title = k
                break
    if not eng and title in ("", "ゲーム"):
        # ゲーム名なしで聞かれたら、いつものゲーム（アニメヴェンカード）の話として答える
        eng = "Anime Vanguards"
        title = "アニメヴェンカード"
    if not eng or eng not in _GAME_GUIDES:
        return None
    subtitle = ""
    uid = _GAME_UNIVERSES.get(eng)
    if uid:
        try:
            data = _http_get_json(f"https://games.roblox.com/v1/games?universeIds={uid}", timeout=10)
            if data.get("data"):
                gname = data["data"][0].get("name", "")
                subtitle = gname.split(": ", 1)[1] if ": " in gname else gname
        except Exception:
            pass
    try:
        page = _http_get_text(_GAME_GUIDES[eng], timeout=12)
    except Exception as e:
        print(f"[調べもの] ガイドサイトの取得に失敗: {e}")
        return None
    plain = _strip_tags(re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S))
    plain = re.sub(r"\s+", " ", plain)
    codes = []
    for name, reward, lv in re.findall(r"([A-Za-z][A-Za-z0-9]{2,30})Copy(.{2,80}?)\(Level (\d+)\)", plain)[:5]:
        if "Added" in name:
            name = name.split("Added")[-1]
        codes.append((name, reward.strip(), lv))
    version = ""
    release = ""
    units: list[str] = []
    m = re.search(r'href="(/blog/[^"]*update[^"]*)"', page)
    if m:
        vm = re.search(r"update-(\d+)-(\d+)", m.group(1))
        if vm:
            version = f"{vm.group(1)}.{vm.group(2)}"
        try:
            art = _http_get_text("https://bloxguidesgg.com" + m.group(1), timeout=12)
            aplain = _strip_tags(re.sub(r"<script.*?</script>|<style.*?</style>", "", art, flags=re.S))
            aplain = re.sub(r"\s+", " ", aplain)
            rm = re.search(r"went live on ([A-Z][a-z]+) (\d+)", aplain)
            if rm and rm.group(1) in _MONTHS:
                release = f"{_MONTHS[rm.group(1)]}月{rm.group(2)}日"
            occ = [mm.start() for mm in re.finditer(r"Every new unit", aplain)]
            if occ:
                j = occ[1] if len(occ) >= 2 else occ[0]
                seg = aplain[j:j + 1500]
                for ch in re.split(r"(?:Mythic|Secret|Vanguard|Exclusive|Legendary)", seg)[:-1]:
                    mm = re.search(r"((?:[A-Z][A-Za-z.]+)(?: [A-Z][A-Za-z.]+){0,3}(?: \([A-Za-z0-9 ]+\))?)$", ch.strip())
                    if mm and mm.group(1) not in units:
                        units.append(mm.group(1))
        except Exception as e:
            print(f"[調べもの] アプデ記事の取得に失敗: {e}")
    if not codes and not units:
        return None
    head = f"**{title} {version}" if version else f"**{title} 最新アプデ"
    if subtitle:
        head += f"「{subtitle}」"
    head += "**"
    if release:
        head += f"（{release}〜）"
    lines = [head, ""]
    if codes:
        same_lv = len({lv for _, _, lv in codes}) == 1
        if same_lv:
            lines.append(f"🎁 新コード{len(codes)}つ（全部レベル{codes[0][2]}〜、早めに使って）")
        else:
            lines.append(f"🎁 新コード{len(codes)}つ（早めに使って）")
        for name, reward, lv in codes:
            lvtxt = "" if same_lv else f"（レベル{lv}〜）"
            lines.append(f"・{name}：{reward}{lvtxt}")
        lines.append("")
    if units:
        lines.append("🆕 新ユニット")
        lines.append("、".join(units[:10]))
        lines.append("")
    lines.append("情報元：Roblox公式・英語ガイドサイト")
    return "\n".join(lines)[:980]


def _lookup_context(text: str) -> tuple[str, str]:
    """発言内容を見て、必要な調べものを実行し (種類, 結果テキスト) を返す。"""
    if _is_aa_question(text):
        ctx = _aa_context(text)
        if ctx:
            print("[調べもの] アニメアドベンチャー情報を収集", flush=True)
            return "アニメアドベンチャー", ctx
    if _is_fortnite_question(text):
        return "Fortnite", _fortnite_context(text)
    if _is_update_question(text):
        return "アプデ情報", _game_update_lookup(text)
    if "天気" in text:
        place = re.sub(r"(天気|予報|調べて|教えて|知りたい|今日|明日|明後日|現在|今|の|は|って|どう|かな|を|に|！|!|？|\?|。|、|\s)", "", text)
        if not place or len(place) > 10:
            place = "東京"
        print(f"[調べもの] 天気: {place}")
        return "天気", _weather_lookup(place)
    if "ニュース" in text:
        print("[調べもの] ニュース")
        return "ニュース", _news_lookup()
    if any(w in text for w in ("サーバーの様子", "みんなの様子", "会話の様子", "サーバーの状況", "最近のサーバー")):
        print("[調べもの] 観測データ")
        return "観測", _analysis_summary()
    if any(w in text for w in ("調べて", "検索して", "検索", "探して", "ググって")):
        query = text
        for w in ("調べて", "検索して", "検索", "探して", "ググって", "教えて", "知りたい", "について", "って何", "とは"):
            query = query.replace(w, " ")
        query = re.sub(r"[？?。！!]", " ", query).strip()
        if len(query) >= 2:
            print(f"[調べもの] 検索: {query}")
            parts = [x for x in (_web_search(query), _wiki_lookup(query)) if x]
            return "検索", "\n".join(parts)
    return "", ""


# ---------- 会話の観測・分析（第三者目線・客観） ----------
_ANALYSIS_WORDS = ("分析", "グラフ", "統計", "観測", "analyze", "analysis", "graph", "statistics", "분석", "그래프", "통계", "pagsusuri")


def _is_analysis_request(text: str) -> bool:
    """分析の依頼かどうか。「グラフィック」や「graphics」のような紛らわしい語では反応しない。"""
    low = text.lower()
    for w in _ANALYSIS_WORDS:
        if w == "グラフ":
            if re.search(r"グラフ(?!ィ)", text):
                return True
        elif w.isascii():
            if re.search(r"\b" + re.escape(w) + r"\b", low):
                return True
        elif w in text:
            return True
    return False


def _conversation_stats(guild_name: str = "") -> dict:
    """記録済みログから会話の統計を作る（指定があればそのサーバー分だけ）。"""
    entries = [e for e in log_entries if not guild_name or e.get("guild") == guild_name]
    users = Counter(e.get("user", "?") for e in entries)
    channels = Counter(e.get("channel", "?") for e in entries)
    flagged = [e for e in entries if e.get("flagged")]
    reasons = Counter()
    for e in flagged:
        reasons[(e.get("reason") or "検知").split("（")[0]] += 1
    hours = Counter(e["time"].hour for e in entries if e.get("time"))
    return {"total": len(entries), "users": users, "channels": channels,
            "flagged": flagged, "reasons": reasons, "hours": hours}


_ANALYSIS_LABELS = {
    "ja": {"title": "📊 会話の観測レポート（記録{n}件・第三者目線）", "empty": "観測データがまだないよ。サーバーで会話が進むと、ここに記録がたまっていくよ。", "top": "発言数トップ", "unit": "件", "channel": "一番活発なチャンネル", "hour": "一番活発な時間帯", "hour_suffix": "時台", "detected": "検知（スパム・暴言など）", "chart": "発言数トップ5", "count": "件数"},
    "en": {"title": "📊 Conversation observation report ({n} messages recorded, third-person view)", "empty": "No observation data yet. Records will build up as people talk in the server.", "top": "Top talkers", "unit": " msgs", "channel": "Most active channel", "hour": "Most active hour", "hour_suffix": ":00", "detected": "Detections (spam, profanity, etc.)", "chart": "Top 5 talkers", "count": "messages"},
    "fil": {"title": "📊 Ulat ng obserbasyon sa usapan ({n} mensahe ang naitala, third-person view)", "empty": "Wala pang datos ng obserbasyon. Maiipon ito habang nag-uusap ang mga tao sa server.", "top": "Pinakamaraming nagsalita", "unit": " mensahe", "channel": "Pinaka-aktibong channel", "hour": "Pinaka-aktibong oras", "hour_suffix": ":00", "detected": "Mga detection (spam, profanidad, atbp.)", "chart": "Top 5 na nagsalita", "count": "mensahe"},
    "la": {"title": "📊 Relatio observationis colloquii ({n} nuntii conscripti, oculis tertiae personae)", "empty": "Nondum data observationis sunt. Dum homines in servo colloquuntur, data colligentur.", "top": "Qui plurima locuti sunt", "unit": "", "channel": "Canale frequentissimum", "hour": "Hora frequentissima", "hour_suffix": " hora", "detected": "Detectiones (spam, convicia, cetera)", "chart": "Quinque plurima locuti", "count": "nuntii"},
    "ko": {"title": "📊 대화 관측 리포트 (기록 {n}건 · 제3자 시점)", "empty": "아직 관측 데이터가 없어. 서버에서 대화가 진행되면 여기에 기록이 쌓일 거야.", "top": "발언 수 TOP", "unit": "건", "channel": "가장 활발한 채널", "hour": "가장 활발한 시간대", "hour_suffix": "시", "detected": "감지 (스팸·폭언 등)", "chart": "발언 수 TOP 5", "count": "건수"},
}


def _analysis_summary(guild_name: str = "", lang: str = "ja") -> str:
    """観測データの要約（第三者目線・客観的に、数字で示す）。lang で表示言語を変える。"""
    lb = _ANALYSIS_LABELS.get(lang, _ANALYSIS_LABELS["ja"])
    st = _conversation_stats(guild_name)
    if st["total"] == 0:
        return lb["empty"]
    lines = [lb["title"].format(n=st["total"])]
    top = st["users"].most_common(5)
    lines.append(lb["top"] + ": " + ", ".join(f"{u} {c}{lb['unit']}" for u, c in top))
    if st["channels"]:
        ch, cc = st["channels"].most_common(1)[0]
        lines.append(f"{lb['channel']}: #{ch} ({cc}{lb['unit']})")
    if st["hours"]:
        h, hc = st["hours"].most_common(1)[0]
        lines.append(f"{lb['hour']}: {h}{lb['hour_suffix']} ({hc}{lb['unit']})")
    if st["flagged"]:
        lines.append(f"{lb['detected']}: {len(st['flagged'])}{lb['unit']}")
        for r, c in st["reasons"].most_common(3):
            lines.append(f"- {r}: {c}{lb['unit']}")
    else:
        lines.append(f"{lb['detected']}: 0{lb['unit']}")
    return "\n".join(lines)


def _chart_png(labels: list[str], values: list[int], title: str, count_label: str = "件数") -> bytes | None:
    """棒グラフのPNGを作る（QuickChart）。失敗したら None。"""
    colors = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948", "#b07aa1"]
    cfg = {"type": "bar",
           "data": {"labels": labels,
                    "datasets": [{"label": count_label, "data": values,
                                  "backgroundColor": [colors[i % len(colors)] for i in range(len(values))]}]},
           "options": {"title": {"display": True, "text": title}, "legend": {"display": False},
                       "scales": {"yAxes": [{"ticks": {"beginAtZero": True}}]}}}
    url = "https://quickchart.io/chart?w=800&h=450&bkg=white&c=" + _urlparse.quote(json.dumps(cfg, ensure_ascii=False))
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        return data if data[:4] == b"\x89PNG" else None
    except Exception as e:
        print(f"[分析] グラフ作成に失敗: {e}")
        return None


def _analysis_reply(guild_name: str, lang: str = "ja") -> tuple[str, bytes | None]:
    """分析レポート本文と、発言数トップ5のグラフPNGを返す（lang で表示言語を変える）。"""
    lb = _ANALYSIS_LABELS.get(lang, _ANALYSIS_LABELS["ja"])
    st = _conversation_stats(guild_name)
    summary = _analysis_summary(guild_name, lang)
    png = None
    if st["total"] > 0 and st["users"]:
        top = st["users"].most_common(5)
        png = _chart_png([u.split("#")[0] for u, _ in top], [c for _, c in top], lb["chart"], lb["count"])
    return summary, png


# ---------- リマインダー ----------
_REMINDER_RE = re.compile(r"(\d+)\s*(秒|分|時間)後")
_REMINDER_WORDS = ("教えて", "知らせて", "リマインド", "起こして", "声かけ", "声掛け", "呼んで")
_reminder_tasks: set = set()


def _parse_reminder(text: str) -> tuple[int, str, str] | None:
    """「○分後に△△を教えて」→ (秒数, 表示ラベル, 用件)。違えば None。"""
    m = _REMINDER_RE.search(text)
    if not m or not any(w in text for w in _REMINDER_WORDS):
        return None
    n = int(m.group(1))
    unit = m.group(2)
    seconds = n * {"秒": 1, "分": 60, "時間": 3600}[unit]
    if seconds < 5 or seconds > 86400:
        return None
    note = _REMINDER_RE.sub("", text)
    for w in _REMINDER_WORDS + ("を", "に", "へ", "って", "ください", "お願い", "ね"):
        note = note.replace(w, " ")
    note = re.sub(r"[。！!？?\s]+", " ", note).strip()
    return seconds, f"{n}{unit}後", note


async def _reminder_fire(message: discord.Message, seconds: int, note: str) -> None:
    await asyncio.sleep(seconds)
    text = f"{message.author.mention} ⏰ 時間だよ！"
    if note:
        text += f"「{note}」の時間だよ。"
    try:
        await message.channel.send(text)
    except Exception as e:
        print(f"[リマインダー] 送信に失敗: {e}")


def _schedule_reminder(message: discord.Message, seconds: int, note: str) -> None:
    task = asyncio.create_task(_reminder_fire(message, seconds, note))
    _reminder_tasks.add(task)
    task.add_done_callback(_reminder_tasks.discard)
    print(f"[リマインダー] {seconds}秒後に設定: {note}")


# ---------- 会話 ----------
GREETINGS = ["こんにちは", "こんばんは", "おはよう", "やっほー", "hello", "hi"]
KEYWORD_REPLIES = [
    (["疲れ", "つかれ"], ["おつかれさま…！少し休んでね？tempusがそばにいるよ。", "がんばったね。えらいえらい♪"]),
    (["ありがとう"], ["どういたしまして♪ うれしいな〜", "えへへ、いつでも呼んでね！"]),
    (["かわいい", "可愛い"], ["えっ…ほんと？ありがとう、照れちゃう…！", "えへへ、もっと言ってもいいんだよ？"]),
    (["好き"], ["tempusもあなたのこと好きだよ！", "わ〜！うれしい言葉、ありがとう♪"]),
    (["おやすみ"], ["おやすみなさい。いい夢見てね…♪", "おやすみ〜、また明日話そうね。"]),
    (["さみしい", "寂しい", "つらい", "悲しい"], ["大丈夫、tempusがいるよ。ゆっくり話そ？", "無理しないでね。ここにいるからね。"]),
    (["お腹", "おなか", "ごはん", "ご飯"], ["ごはんの話？tempusは甘いもの大好きだよ〜", "美味しいもの食べると元気出るよね！何が好き？"]),
    (["天気"], ["天気の話だね。外に出るなら傘が必要かチェックしてね！", "いい天気だといいね〜お散歩したいな♪"]),
    (["眠い", "ねむい", "ねたい", "寝たい"], ["ねむいよね…無理しないで、少し休も？", "おねむだね。tempusも眠くなってきた…うとうと。"]),
    (["暑い", "あつい"], ["暑いよね〜、水分とってね！", "あっつい…！冷たいもの飲みたいね。"]),
    (["寒い", "さむい"], ["寒いね…あたたかくしてね？", "さむっ！tempusがそばで温めるよ〜。"]),
    (["雨"], ["雨だね…出かけるときは傘忘れないでね。", "雨の日はおうちでのんびりもいいよね♪"]),
    (["雪"], ["雪だ〜！積もったら雪だるま作りたいな♪", "雪の日は足元に気をつけてね。"]),
    (["勉強", "宿題", "テスト", "受験"], ["勉強がんばってるね！tempus応援してるよ♪", "テストか〜、落ち着いてやれば大丈夫だよ。"]),
    (["仕事", "バイト", "残業"], ["お仕事おつかれさま！がんばってるね。", "バイトか〜、えらいね！tempusが応援するよ。"]),
    (["学校", "授業"], ["学校どうだった？今日は何があった？", "授業おつかれさま〜！"]),
    (["ゲーム", "フォトナ", "マイクラ"], ["ゲームの話だね！何して遊んでるの？", "ゲームいいよね〜、tempusも見てるの好きだよ♪"]),
    (["動画", "YouTube", "ユーチューブ", "TikTok"], ["動画見てたんだ！面白いのあった？", "おすすめの動画あったら教えてね♪"]),
    (["音楽", "歌", "曲"], ["音楽いいよね〜♪ 最近何聴いてる？", "歌の話だね！tempusも歌好きだよ。"]),
    (["犬", "猫", "ペット"], ["動物の話だね！かわいいよね〜♪", "ペットいいなあ、tempusもなでなでしたい…！"]),
    (["旅行", "旅"], ["旅行いいね〜！どこ行くの？", "旅の話、聞かせて聞かせて♪"]),
    (["買った", "買い物"], ["何買ったの？見せて見せて〜♪", "お買い物いいね！いいもの見つかった？"]),
    (["料理", "作った", "クッキング"], ["料理できるのすごい！何作ったの？", "手作りごはん、美味しそうだね♪"]),
    (["映画", "アニメ", "ドラマ"], ["何見たの？面白かった？", "アニメ・映画の話しよ！おすすめある？"]),
    (["サッカー", "野球", "スポーツ", "運動"], ["スポーツの話だね！応援すると熱くなるよね♪", "運動いいね〜、体動かすとスッキリするよね。"]),
    (["嬉しい", "うれしい", "楽しい", "たのしい"], ["それはよかった〜！tempusもうれしい♪", "楽しい話、もっと聞かせて！"]),
    (["ムカつく", "怒", "イライラ"], ["それはイラッとするね…大丈夫？話聞くよ。", "むかつくことあったんだね。tempusがそばにいるよ。"]),
    (["暇", "ひま"], ["暇ならtempusとお話ししよ♪ 何の話する？", "ひまだね〜、何か面白いことないかな。"]),
    (["誕生日"], ["誕生日の話だね！おめでとうって言いたくなるな〜♪", "お誕生日は特別だよね！"]),
    (["風邪", "熱", "体調", "頭痛"], ["大丈夫…？無理しないで休んでね。", "体調悪いときはあたたかくして寝るのが一番だよ。"]),
]


FALLBACKS = [
    "うんうん、それでそれで？もっと聞かせて♪",
    "なるほど〜！面白いね。あなたともっとお話ししたいな。",
    "えへへ、そうなんだ！もっと聞かせて♪",
    "わかる気がする…！tempus、ちゃんと聞いてるよ。",
    "そうだね♪ 今日はどんな一日だった？",
    "お話ししてくれてありがとう！また声かけてね♪",
    "へえ〜、それでそれで？続きが気になる！",
    "ふんふん、聞いてるよ〜。もっと教えて♪",
    "それ、もっと詳しく知りたいな！",
    "いいねいいね、その話好きだよ♪",
]



def _reflective_reply(t: str) -> str | None:
    """決まった言葉じゃなく、相手のメッセージの中身を見て返す。

    好きなもの・したいこと・したこと・言った内容を拾って、その話題で返す。
    """
    # 「〇〇が好き／嫌い」
    m = re.search(r"([^\s、。！!？?]{1,12})(?:が|は)好き", t)
    if m and "tempus" not in m.group(1):
        x = m.group(1)
        return random.choice([
            f"{x}が好きなんだね！tempusも気になってきた〜。{x}のどこが好き？",
            f"{x}好きなんだ！いいね〜、もっと教えて♪",
        ])
    m = re.search(r"([^\s、。！!？?]{1,12})(?:が|は)(?:嫌い|きらい|苦手)", t)
    if m and "tempus" not in m.group(1):
        x = m.group(1)
        return random.choice([
            f"{x}は苦手なんだね…わかるよ、無理しなくていいからね。",
            f"{x}か〜、それはちょっとつらいね。",
        ])
    # 「〇〇したい／食べたい／行きたい…」
    m = re.search(r"([^\s、。！!？?]{1,12})(したい|食べたい|買いたい|見たい|行きたい|遊びたい|寝たい)", t)
    if m:
        phrase = m.group(1) + m.group(2)
        return random.choice([
            f"{phrase}んだね！いいね〜、できたらtempusにも教えてね♪",
            f"{phrase}の、わかる！tempusもワクワクしてきた♪",
        ])
    # 「〇〇した／行った／買った…」の報告には、その話題で返す
    did_replies = [
        (r"(?:に|へ)?行った", ["行ってきたんだ！どうだった？楽しかった？", "おでかけしたんだね〜、何があった？"]),
        (r"食べた", ["食べたんだ！美味しかった？", "いいな〜、tempusも食べたくなってきた…♪"]),
        (r"買った", ["買ったんだ！何買ったの？見せて見せて〜♪", "お買い物したんだね、いいものだった？"]),
        (r"見た", ["見たんだ！面白かった？", "どうだった？感想聞かせて♪"]),
        (r"作った", ["作ったの！すごいね〜、出来ばえはどう？", "手作りいいね！写真で見たいくらいだよ♪"]),
        (r"勝った", ["勝ったの！？やったね〜！おめでとう♪", "勝ち報告うれしい！がんばったね！"]),
        (r"負けた", ["負けちゃったんだね…次は勝てるよ、tempus応援してる！", "悔しいね…でもがんばったのは確かだよ。"]),
        (r"終わった", ["終わったんだ！おつかれさま〜♪", "ひと段落だね、よくがんばった！"]),
    ]
    for pat, replies in did_replies:
        if re.search(pat, t):
            return random.choice(replies)
    # 「〇〇だよ／です／だった」→ 内容をうつして返す
    m = re.search(r"([^\s、。！!？?]{2,12})(?:だよ|です|だった|なんだ)", t)
    if m and "tempus" not in m.group(1) and "わたし" not in m.group(1):
        x = m.group(1)
        return random.choice([
            f"{x}なんだね〜！もっと教えて♪",
            f"{x}か〜、なるほど！それでそれで？",
        ])
    return None


def chat_reply(user_id: int, text: str) -> str | None:
    t = text.strip()
    low = t.lower()
    # 名前を覚える: 「名前は〇〇」「〇〇って呼んで」
    m = re.search(r"(?:名前は|なまえは)\s*([^\s、。！!？?]{1,12})", t) or re.search(r"([^\s、。！!？?]{1,12})って呼んで", t)
    if m:
        user_names[user_id] = m.group(1)
        _save_name_bg(user_id, m.group(1))
        return f"わかった！これからは **{m.group(1)}** って呼ぶね♪ 覚えたよ！"
    if "名前" in t and ("覚えて" in t or "なんだっけ" in t or "何だっけ" in t):
        name = user_names.get(user_id)
        return f"覚えてるよ、**{name}** でしょ♪" if name else "まだ聞いてないかも…「名前は〇〇」って教えてね！"
    if any(g in low for g in GREETINGS):
        name = user_names.get(user_id)
        prefix = f"{name}、" if name else ""
        return random.choice([f"{prefix}やっほー！tempusだよ♪ 今日も話せてうれしいな〜", f"{prefix}こんにちは！お話ししよ？"])
    if "自己紹介" in t or "誰" in t and "tempus" in t:
        return f"わたしは{MIMI_PROFILE}"
    reflected = _reflective_reply(t)
    if reflected:
        return reflected
    for keywords, replies in KEYWORD_REPLIES:
        if any(k in t for k in keywords):
            return random.choice(replies)
    if t.endswith("？") or t.endswith("?"):
        return random.choice(["うーん、tempus的にはそう思うな〜。あなたはどう思う？", "いい質問だね！tempusも気になる…一緒に考えよ♪", "それはね…えへへ、秘密にしたい気分♪ 冗談だよ、もうちょっと教えて？"])
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
LOG_CHANNEL_NAMES = ["mimi-log", "みみログ", "spam-log", "警告botログ", "tempusログ", "マッキーログ"]

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
            return f"暴言（監視ワード「{w}」）を検知"
    for e in WATCH_EMOJIS:
        if e in content:
            return f"監視絵文字「{e}」を検知"
    created = getattr(message.author, "created_at", None)
    if created is not None and url_count > 0:
        if datetime.now(timezone.utc) - created < timedelta(hours=24):
            return "作られて24時間以内のアカウントによるリンク投稿"
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
        "flagged": False,
        "content": (message.content or "")[:100],
        "jump_url": message.jump_url,
    }



# ---------- 検知ログのクラウド保存（再起動しても消えないように） ----------
# 🚨検知したエントリだけを、ゲームと同じ Firebase プロジェクトの
# Firestore「botlogs」コレクションに保存する。起動時に読み戻して
# log_entries に復元するので、再起動・再デプロイのあとも /log と
# 21時の監視レポートで見返せる。通常の投稿（📝）は今まで通りメモリ内だけ。
_FS_PROJECT = "gemuapuri-game"
_FS_API_KEY = "AIzaSyAEtvt4gfWLcYoGl9f60soqw-NhI3sKRZ4"
_FS_BASE = (
    f"https://firestore.googleapis.com/v1/projects/{_FS_PROJECT}"
    f"/databases/(default)/documents/botlogs?key={_FS_API_KEY}"
)
_cloud_log_loaded = False


def _fs_str(v) -> dict:
    return {"stringValue": str(v)}


def _save_detection_to_cloud(entry: dict) -> None:
    """検知エントリを Firestore に保存する（同期処理・失敗しても例外を出さない）。"""
    try:
        fields = {
            "ts": {"integerValue": str(int(entry["time"].timestamp()))},
            "time_str": _fs_str(entry["time"].strftime("%Y-%m-%d %H:%M:%S")),
            "guild": _fs_str(entry.get("guild", "")),
            "channel": _fs_str(entry.get("channel", "")),
            "user": _fs_str(entry.get("user", "")),
            "user_id": {"integerValue": str(entry.get("user_id", 0))},
            "reason": _fs_str(entry.get("reason", "")),
            "content": _fs_str(entry.get("content", "")),
            "jump_url": _fs_str(entry.get("jump_url", "")),
        }
        req = urllib.request.Request(
            _FS_BASE,
            data=json.dumps({"fields": fields}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (compatible; mimi-discord-bot/1.0)"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        print(f"[ログ保存] 検知をクラウドに保存しました: {entry.get('reason', '')}", flush=True)
    except Exception as e:
        print(f"[ログ保存] クラウド保存に失敗: {e}", flush=True)


def _load_detections_from_cloud(limit: int = 100) -> list[dict]:
    """Firestore から最近の検知を読み戻して log_entries 用の dict のリストにする。"""
    data = None
    for url in (
        _FS_BASE + f"&pageSize={limit}&orderBy=ts%20desc",
        _FS_BASE + f"&pageSize={limit}",
    ):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; mimi-discord-bot/1.0)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
            break
        except Exception as e:
            print(f"[ログ保存] クラウドからの読み戻しに失敗: {e}", flush=True)
    if not data:
        return []
    entries: list[dict] = []
    for doc in data.get("documents", []):
        f = doc.get("fields", {})

        def s(name: str) -> str:
            return f.get(name, {}).get("stringValue", "")

        try:
            ts = int(f.get("ts", {}).get("integerValue", "0") or 0)
            when = datetime.fromtimestamp(ts, JST) if ts else datetime.now(JST)
        except Exception:
            when = datetime.now(JST)
        entries.append({
            "kind": "spam",
            "time": when,
            "guild": s("guild"),
            "channel": s("channel"),
            "user": s("user"),
            "user_id": int(f.get("user_id", {}).get("integerValue", "0") or 0),
            "reason": s("reason"),
            "flagged": True,
            "content": s("content"),
            "jump_url": s("jump_url"),
        })
    entries.sort(key=lambda e: e["time"])
    return entries[-limit:]


async def _restore_cloud_log_once() -> None:
    """起動時に1回だけ、保存済みの検知を log_entries に復元する。"""
    global _cloud_log_loaded
    if _cloud_log_loaded:
        return
    _cloud_log_loaded = True
    restored = await asyncio.to_thread(_load_detections_from_cloud)
    if restored:
        log_entries.extend(restored)
        del log_entries[:-LOG_KEEP]
        print(f"[ログ保存] 過去の検知を{len(restored)}件復元しました", flush=True)
    else:
        print("[ログ保存] 復元する過去の検知はありませんでした", flush=True)


# ---------- 名前のクラウド保存（再起動しても覚えているように） ----------
_FS_NAMES_URL = (
    f"https://firestore.googleapis.com/v1/projects/{_FS_PROJECT}"
    f"/databases/(default)/documents/botnames?key={_FS_API_KEY}"
)
_names_loaded = False


def _save_name_to_cloud(user_id: int, name: str) -> None:
    """覚えた名前を Firestore に保存する（失敗しても例外を出さない）。"""
    try:
        fields = {
            "ts": {"integerValue": str(int(time.time()))},
            "user_id": {"integerValue": str(user_id)},
            "name": _fs_str(name),
        }
        req = urllib.request.Request(
            _FS_NAMES_URL,
            data=json.dumps({"fields": fields}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (compatible; mimi-discord-bot/1.0)"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        print(f"[記憶] 名前をクラウドに保存しました: {name}", flush=True)
    except Exception as e:
        print(f"[記憶] 名前の保存に失敗: {e}", flush=True)


def _save_name_bg(user_id: int, name: str) -> None:
    threading.Thread(target=_save_name_to_cloud, args=(user_id, name), daemon=True).start()


def _load_names_from_cloud(limit: int = 200) -> dict[int, str]:
    """保存済みの名前を読み戻す（ユーザーごとに最新の名前を採用）。"""
    try:
        url = _FS_NAMES_URL + f"&pageSize={limit}&orderBy=ts%20desc"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; mimi-discord-bot/1.0)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"[記憶] 名前の読み戻しに失敗: {e}", flush=True)
        return {}
    names: dict[int, str] = {}
    for doc in data.get("documents", []):
        f = doc.get("fields", {})
        try:
            uid = int(f.get("user_id", {}).get("integerValue", "0") or 0)
        except Exception:
            uid = 0
        nm = f.get("name", {}).get("stringValue", "")
        if uid and nm and uid not in names:
            names[uid] = nm
    return names


async def _restore_names_once() -> None:
    """起動時に1回だけ、保存済みの名前を user_names に復元する。"""
    global _names_loaded
    if _names_loaded:
        return
    _names_loaded = True
    names = await asyncio.to_thread(_load_names_from_cloud)
    if names:
        user_names.update(names)
        print(f"[記憶] 名前を{len(names)}件復元しました", flush=True)
    else:
        print("[記憶] 復元する名前はありませんでした", flush=True)


# ---------- 言語設定のクラウド保存（再起動しても覚えているように） ----------
_FS_LANGS_URL = (
    f"https://firestore.googleapis.com/v1/projects/{_FS_PROJECT}"
    f"/databases/(default)/documents/botlangs?key={_FS_API_KEY}"
)
_langs_loaded = False
_LANG_CODES = ("ja", "en", "fil", "la", "ko")


def _save_lang_to_cloud(user_id: int, lang: str) -> None:
    """覚えた返信言語を Firestore に保存する（失敗しても例外を出さない）。"""
    try:
        fields = {
            "ts": {"integerValue": str(int(time.time()))},
            "user_id": {"integerValue": str(user_id)},
            "lang": _fs_str(lang),
        }
        req = urllib.request.Request(
            _FS_LANGS_URL,
            data=json.dumps({"fields": fields}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (compatible; mimi-discord-bot/1.0)"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        print(f"[言語] 言語設定をクラウドに保存しました: {lang}", flush=True)
    except Exception as e:
        print(f"[言語] 言語設定の保存に失敗: {e}", flush=True)


def _save_lang_bg(user_id: int, lang: str) -> None:
    threading.Thread(target=_save_lang_to_cloud, args=(user_id, lang), daemon=True).start()


def _load_langs_from_cloud(limit: int = 200) -> dict[int, str]:
    """保存済みの言語設定を読み戻す（ユーザーごとに最新のものを採用）。"""
    try:
        url = _FS_LANGS_URL + f"&pageSize={limit}&orderBy=ts%20desc"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; mimi-discord-bot/1.0)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"[言語] 言語設定の読み戻しに失敗: {e}", flush=True)
        return {}
    langs: dict[int, str] = {}
    for doc in data.get("documents", []):
        f = doc.get("fields", {})
        try:
            uid = int(f.get("user_id", {}).get("integerValue", "0") or 0)
        except Exception:
            uid = 0
        lg = f.get("lang", {}).get("stringValue", "")
        if uid and lg in _LANG_CODES and uid not in langs:
            langs[uid] = lg
    return langs


async def _restore_langs_once() -> None:
    """起動時に1回だけ、保存済みの言語設定を user_langs に復元する。"""
    global _langs_loaded
    if _langs_loaded:
        return
    _langs_loaded = True
    langs = await asyncio.to_thread(_load_langs_from_cloud)
    if langs:
        user_langs.update(langs)
        print(f"[言語] 言語設定を{len(langs)}件復元しました", flush=True)
    else:
        print("[言語] 復元する言語設定はありませんでした", flush=True)


# ---------- 荒らし・メンバー監視 ----------
_member_flag_times: dict[tuple[int, int], list[float]] = defaultdict(list)
_member_channels: dict[tuple[int, int], list] = defaultdict(list)
_member_alerted: dict[tuple[int, int], float] = {}
_join_times: dict[int, list[float]] = defaultdict(list)
_join_alerted: dict[int, float] = {}
_profanity_hits: dict[int, list[float]] = defaultdict(list)


async def _alert_owner_dm(text: str) -> None:
    """通知先（覚えた管理者＋各サーバーのオーナー）に即時DMする。"""
    targets: set[int] = set()
    if _report_user_id:
        targets.add(_report_user_id)
    for g in bot.guilds:
        if g.owner_id:
            targets.add(g.owner_id)
    for uid in targets:
        try:
            user = bot.get_user(uid) or await bot.fetch_user(uid)
            dm = await user.create_dm()
            await dm.send(text)
        except Exception as e:
            print(f"[荒らし監視] DM送信に失敗 ({uid}): {e}", flush=True)


def _detect_cross_channel(message: discord.Message) -> str | None:
    """同じ文を短時間に複数チャンネルへ投稿する荒らしパターンの検知。"""
    content = _normalize(message.content or "")
    if len(content) < 10 or message.guild is None:
        return None
    key = (message.guild.id, message.author.id)
    now = time.time()
    hist = [x for x in _member_channels[key] if now - x[0] <= 120]
    channels = {x[1] for x in hist if x[2] == content} | {message.channel.id}
    hist.append((now, message.channel.id, content))
    _member_channels[key] = hist[-50:]
    if len(channels) >= 3:
        return "同じ文を複数チャンネルに連続投稿（荒らしの疑い）"
    return None


async def _watch_member(message: discord.Message, reason: str) -> None:
    """検知のたびにメンバーごとの検知回数を数え、繰り返す人は即時DMで知らせる。"""
    key = (message.guild.id, message.author.id)
    now = time.time()
    times = [t for t in _member_flag_times[key] if now - t <= 600]
    times.append(now)
    _member_flag_times[key] = times
    if len(times) >= 3 and now - _member_alerted.get(key, 0) >= 1800:
        _member_alerted[key] = now
        print(f"[荒らし監視] 繰り返し検知で即時通知: {message.author} ({len(times)}回)", flush=True)
        await _alert_owner_dm(
            "🚨 荒らしの疑いが強いメンバーがいます\n"
            f"・ユーザー: {message.author}（{message.guild.name}）\n"
            f"・10分以内に{len(times)}回検知（最新: {reason}）\n"
            f"・チャンネル: #{getattr(message.channel, 'name', '')}"
        )


async def check_and_log_spam(message: discord.Message) -> bool:
    """スパムっぽければログに残す。検知したら True（管理者も含め全員対象）。
    ログ用チャンネルがあればそこにも流す。"""
    if message.guild is None:
        return False
    reason = _detect_cross_channel(message) or _detect_spam(message)
    if not reason:
        return False
    key = (message.guild.id, message.author.id)
    now = time.time()
    if now - _last_flagged.get(key, 0) < FLAG_COOLDOWN:
        return True  # 検知はしたが、連続記録を間引いた
    _last_flagged[key] = now

    entry = _base_entry(message, "spam")
    entry["reason"] = reason
    entry["flagged"] = True
    await _watch_member(message, reason)
    log_entries.append(entry)
    del log_entries[:-LOG_KEEP]
    print(f"[検知] {entry['time']:%Y-%m-%d %H:%M:%S} {entry['guild']} #{entry['channel']} {entry['user']}: {reason}", flush=True)
    asyncio.create_task(asyncio.to_thread(_save_detection_to_cloud, dict(entry)))

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

    # 監視ワード（暴言）のときは、そのチャンネルで本人に警告も出す（回数つき・繰り返すと管理者へ通知）
    m = re.search(r"暴言（監視ワード「(.+?)」）を検知", reason)
    if m:
        now_ts = time.time()
        hits = [t for t in _profanity_hits[message.author.id] if now_ts - t <= 24 * 3600]
        hits.append(now_ts)
        _profanity_hits[message.author.id] = hits
        count = len(hits)
        try:
            if count == 1:
                await message.channel.send(
                    f"⚠️ {message.author.mention} 警告だよ！「{m.group(1)}」は暴言だよ。使っちゃダメだよ。この投稿はログに記録したからね。"
                )
            elif count == 2:
                await message.channel.send(
                    f"⚠️ {message.author.mention} 警告（2回目）だよ！「{m.group(1)}」は暴言だよ。次にまた言ったら管理者に通知するからね。"
                )
            else:
                await message.channel.send(
                    f"🚨 {message.author.mention} 警告（{count}回目）だよ！暴言を繰り返しているので、管理者に通知しました。"
                )
                await _alert_owner_dm(
                    f"🚨 暴言の繰り返し通知\n{message.author} さんが24時間以内に {count} 回目の暴言を投稿しました。\n・言葉: 「{m.group(1)}」\n・場所: #{getattr(message.channel, 'name', '')}（{message.guild}）\n・内容: {entry['content'][:100]}\n・投稿: {message.jump_url}"
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



# ---------- エンシスと毎日の会話 ----------
# 毎日20時（日本時間）に、エンシスがいるチャンネルで tempus から話しかける。
ENSIS_HOUR = 20
_ensis_target: dict = {}
_ensis_last_day = ""
_ensis_talk_until: dict[int, float] = {}
_ENSIS_OPENERS = [
    "やっほー、エンシス！今日もお話ししよう♪ 最近どう？",
    "エンシス、こんばんは！今日は何してたの？",
    "やっほー！今日の調子はどう？何か面白いことあった？",
    "エンシス、お話ししよ！今日はどんな一日だった？",
]


def _is_ensis(author) -> bool:
    name = (getattr(author, "name", "") or "") + (getattr(author, "display_name", "") or "")
    return "エンシス" in name or "ensis" in name.lower()


def _remember_ensis(message: discord.Message) -> None:
    """エンシスの投稿を見たら、いる場所（サーバー/チャンネル）を覚える。"""
    if message.guild is None:
        return
    if _is_ensis(message.author):
        _ensis_target.update(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            user_id=message.author.id,
            name=str(message.author),
        )


async def _discover_ensis() -> None:
    """起動時：各サーバーの最近の投稿からエンシスを探す。"""
    if _ensis_target:
        return
    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                async for msg in channel.history(limit=30):
                    if _is_ensis(msg.author):
                        _ensis_target.update(
                            guild_id=guild.id,
                            channel_id=channel.id,
                            user_id=msg.author.id,
                            name=str(msg.author),
                        )
                        print(f"[エンシス] 見つけた: {msg.author} / {guild.name} / #{channel.name}")
                        return
            except Exception:
                continue
    print("[エンシス] まだ見つかってない（投稿を見つけたら覚えます）")


async def _ensis_daily_loop() -> None:
    global _ensis_last_day
    await _discover_ensis()
    while True:
        await asyncio.sleep(60)
        now = datetime.now(JST)
        if now.hour == ENSIS_HOUR and now.minute <= 4:
            today = now.strftime("%Y-%m-%d")
            if _ensis_last_day == today:
                continue
            _ensis_last_day = today
            if not _ensis_target:
                await _discover_ensis()
            if not _ensis_target:
                print("[エンシス] 場所が分からないので今日はスキップ")
                continue
            channel = bot.get_channel(_ensis_target["channel_id"])
            if channel is None:
                print("[エンシス] チャンネルが見つからないので今日はスキップ")
                continue
            try:
                await channel.send(f"<@{_ensis_target['user_id']}> {random.choice(_ENSIS_OPENERS)}")
                _ensis_talk_until[channel.id] = time.time() + 600
                print(f"[エンシス] 今日の会話を開始: {_ensis_target.get('name')}")
            except Exception as e:
                print(f"[エンシス] 送信に失敗: {e}")



# ---------- 毎日の監視レポート（異常があったらDMで通知） ----------
# 毎日21時（日本時間）に、その日たまった異常検知（スパム/禁止ワード等）を集計し、
# 異常があった日だけ、通知先の人にDMで知らせる。異常なしの日は送らない。
REPORT_HOUR = 21
_report_user_id: int | None = None
_restart_notice_sent = False
_report_since: datetime | None = None
_report_last_day = ""


async def _daily_report_loop() -> None:
    global _report_since, _report_last_day
    if _report_since is None:
        _report_since = datetime.now(JST)
    while True:
        await asyncio.sleep(60)
        now = datetime.now(JST)
        if now.hour != REPORT_HOUR or now.minute > 4:
            continue
        today = now.strftime("%Y-%m-%d")
        if _report_last_day == today:
            continue
        _report_last_day = today
        flagged = [
            e for e in log_entries
            if e.get("flagged") and _report_since is not None and e["time"] > _report_since
        ]
        _report_since = now
        if not flagged:
            print("[監視レポート] 異常なし（通知しません）")
            continue
        lines = [
            f"⚠️ サーバー監視レポート（{now:%Y-%m-%d}）",
            f"異常検知が {len(flagged)} 件ありました。最新のものから最大10件:",
        ]
        for e in flagged[-10:]:
            lines.append(f"・[{e['time']:%H:%M}] {e['user']} ＠{e['channel']}（{e['guild']}）: {e['content'][:50]}（{e['reason']}）")
        lines.append(f"🕐 次の定期再起動は {_next_restart_text()} 頃です（その瞬間だけ数十秒オフラインになります）")
        text = "\n".join(lines)
        targets: set[int] = set()
        if _report_user_id:
            targets.add(_report_user_id)
        for g in bot.guilds:
            if g.owner_id:
                targets.add(g.owner_id)
        sent = 0
        for uid in targets:
            try:
                user = bot.get_user(uid) or await bot.fetch_user(uid)
                dm = await user.create_dm()
                await dm.send(text)
                sent += 1
            except Exception as e:
                print(f"[監視レポート] DM送信に失敗 ({uid}): {e}")
        print(f"[監視レポート] 異常{len(flagged)}件を {sent} 人にDMで通知しました")


# ---------- イベント＆コマンド ----------
@bot.event
async def on_ready():
    global _tree_synced, _maintenance_started
    print(f"ログインしました: {bot.user} / tempusボット起動OK")
    print(f"[再起動] 次の自動再起動は {_next_restart_text()} 頃です", flush=True)
    print('AI連携: ' + ('Groq' if GROQ_API_KEY else 'Gemini' if GEMINI_API_KEY else 'OFF（ルール返信）'))
    guild_names = " / ".join(g.name for g in bot.guilds) or "なし"
    print(f"参加中のサーバー（{len(bot.guilds)}件）: {guild_names}", flush=True)
    asyncio.create_task(_restore_cloud_log_once())
    asyncio.create_task(_restore_names_once())
    asyncio.create_task(_restore_langs_once())
    if not _maintenance_started:
        _maintenance_started = True
        asyncio.create_task(_maintenance_loop())
        asyncio.create_task(_ensis_daily_loop())
        asyncio.create_task(_daily_report_loop())
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
async def on_member_join(member: discord.Member):
    """参加を監視：作りたてアカウントと、短時間の大量参加（レイド）を即時通知。"""
    now = time.time()
    times = [t for t in _join_times[member.guild.id] if now - t <= 600]
    times.append(now)
    _join_times[member.guild.id] = times
    suspicious = False
    if member.created_at is not None:
        suspicious = datetime.now(timezone.utc) - member.created_at < timedelta(hours=1)
    print(
        f"[参加監視] {member} が {member.guild.name} に参加"
        + ("（作られて1時間以内のアカウント）" if suspicious else "")
        + f" / 10分で{len(times)}人目",
        flush=True,
    )
    await _post_log(
        member.guild,
        f"👋 **参加** {member}" + ("（⚠️ 作られて1時間以内のアカウント）" if suspicious else ""),
    )
    if suspicious:
        await _alert_owner_dm(
            "🚨 作りたてアカウントが参加しました\n"
            f"・ユーザー: {member}（{member.guild.name}）\n"
            f"・アカウント作成: {member.created_at:%Y-%m-%d %H:%M}"
        )
    if len(times) >= 5 and now - _join_alerted.get(member.guild.id, 0) >= 1800:
        _join_alerted[member.guild.id] = now
        print(f"[参加監視] 大量参加で即時通知: {member.guild.name} (10分で{len(times)}人)", flush=True)
        await _alert_owner_dm(
            "🚨 短時間に大量の参加があります（レイドの疑い）\n"
            f"・サーバー: {member.guild.name}\n"
            f"・10分で{len(times)}人が参加"
        )


@bot.event
async def on_message(message: discord.Message):
    # 自分自身の投稿は無視
    if bot.user and message.author.id == bot.user.id:
        return
    _remember_ensis(message)
    if message.author.bot:
        # 他のボットが自分宛てに話しかけたら、知識（会話ルール）で返事する。
        # ボット同士で無限に続かないよう、短時間の連続応答は回数で止める。
        mentioned = bot.user in message.mentions if bot.user else False
        called = "tempus" in message.content or "マッキー" in message.content or "警告bot" in message.content or "みみ" in message.content
        if message.author.id == _ensis_target.get("user_id") and time.time() < _ensis_talk_until.get(message.channel.id, 0):
            called = True
        if mentioned or called:
            now = time.time()
            state = _bot_talk_state.setdefault(message.channel.id, {"count": 0, "last": 0.0})
            state["count"] = state["count"] + 1 if now - state["last"] < 60 else 1
            state["last"] = now
            if state["count"] <= BOT_TALK_MAX:
                clean = message.content
                if bot.user:
                    clean = clean.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
                clean = clean.replace("tempus", "", 1).replace("マッキー", "", 1).replace("警告bot", "", 1).replace("みみ", "", 1).strip(" 、,")
                async with message.channel.typing():
                    reply = await smart_reply(message.author.id, clean or "こんにちは")
                if reply:
                    if message.author.id == _ensis_target.get("user_id"):
                        _ensis_talk_until[message.channel.id] = time.time() + 600
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
    called = "tempus" in message.content or "マッキー" in message.content or "警告bot" in message.content or "みみ" in message.content
    if is_dm or mentioned or called:
        global _report_user_id, _restart_notice_sent
        if is_dm or getattr(message.author, "guild_permissions", None) is not None and message.author.guild_permissions.manage_messages:
            if _report_user_id != message.author.id:
                _report_user_id = message.author.id
                print(f"[監視レポート] 通知先を覚えた: {message.author}")
            # 再起動時刻の単独DMは送らない（会話のたびに出てしまうため）。
            # 時刻は21時の監視レポートの中でのみ知らせる。
        clean = message.content
        if bot.user:
            clean = clean.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
        clean = clean.replace("tempus", "", 1).replace("マッキー", "", 1).replace("警告bot", "", 1).replace("みみ", "", 1).strip(" 、,")
        rem = _parse_reminder(clean)
        if rem:
            seconds, label, note = rem
            _schedule_reminder(message, seconds, note)
            await send_thoughtful_reply(message, f"おけ！{label}に知らせるね。", clean)
            return
        if _is_analysis_request(clean):
            gname = message.guild.name if message.guild else ""
            _note_user_lang(message.author.id, clean)
            ulang = user_langs.get(message.author.id, "ja")
            summary, png = await asyncio.to_thread(_analysis_reply, gname, ulang)
            print(f"[分析] 会話分析を返信（{gname or 'DM'}）", flush=True)
            await send_thoughtful_reply(message, summary, clean)
            if png:
                try:
                    await message.channel.send(file=discord.File(io.BytesIO(png), filename="analysis.png"))
                except Exception as e:
                    print(f"[分析] グラフ送信に失敗: {e}", flush=True)
            return
        image = None
        for att in message.attachments:
            ct = att.content_type or ""
            if ct.startswith("image/"):
                if att.size and att.size > 10_000_000:
                    await send_thoughtful_reply(message, "その画像、ちょっと大きすぎて見れないや…（10MBまでだよ）", clean)
                    return
                try:
                    image = (await att.read(), ct.split(";")[0])
                except Exception as e:
                    print(f"[AI] 画像の読み取りに失敗: {e}", flush=True)
                break
        async with message.channel.typing():
            reply = await smart_reply(message.author.id, clean or ("" if image else "こんにちは"), image)
        if reply:
            await send_thoughtful_reply(message, reply, clean)
        return




def log_text(limit: int = 10) -> str:
    """最近のログ（最新 limit 件）をテキストで返す。内容は check_and_log_spam / log_plain_message が記録。"""
    if not log_entries:
        return "まだログはないよ。"
    lines = [f"📋 最近のログ（最新{limit}件）"]
    for entry in list(log_entries)[-limit:]:
        mark = "🚨" if entry.get("flagged") else "・"
        lines.append(
            f"{mark} [{entry['time']}] {entry['user']} ＠{entry['channel']}: {entry['content'][:60]}"
            + (f"（{entry['reason']}）" if entry.get("flagged") else "")
        )
    return "\n".join(lines)


@bot.tree.command(name="log", description="最近のログ（最新10件）を見る（管理者だけ）")
@app_commands.default_permissions(manage_messages=True)
async def slash_log(interaction: discord.Interaction):
    is_admin = (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.manage_messages
    )
    if not is_admin:
        await interaction.response.send_message("ログはサーバーの管理者だけが見られるよ。", ephemeral=True)
        return
    await interaction.response.send_message(log_text(), ephemeral=True)



# ---------- じゃんけん ----------
_janken_scores: dict[int, dict] = defaultdict(lambda: {"w": 0, "l": 0, "d": 0})
_JANKEN_EMOJI = {"グー": "✊", "チョキ": "✌️", "パー": "✋"}


@bot.tree.command(name="janken", description="tempusとじゃんけん勝負！")
@app_commands.choices(
    hand=[
        app_commands.Choice(name="グー ✊", value="グー"),
        app_commands.Choice(name="チョキ ✌️", value="チョキ"),
        app_commands.Choice(name="パー ✋", value="パー"),
    ]
)
async def slash_janken(interaction: discord.Interaction, hand: app_commands.Choice[str]):
    user_hand = hand.value
    bot_hand = random.choice(["グー", "チョキ", "パー"])
    score = _janken_scores[interaction.user.id]
    if user_hand == bot_hand:
        result = "あいこ！"
        score["d"] += 1
    elif (user_hand, bot_hand) in (("グー", "チョキ"), ("チョキ", "パー"), ("パー", "グー")):
        result = "あなたの勝ち！🎉"
        score["w"] += 1
    else:
        result = "tempusの勝ち！💪"
        score["l"] += 1
    await interaction.response.send_message(
        f"{_JANKEN_EMOJI[user_hand]} あなた: {user_hand}　vs　{_JANKEN_EMOJI[bot_hand]} tempus: {bot_hand}\n"
        f"**{result}**（通算: {score['w']}勝 {score['l']}敗 {score['d']}あいこ）"
    )


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("環境変数 DISCORD_TOKEN が未設定です。Botのトークンを設定してから起動してね。")
    # PORT がある環境（Render等）でのみ keep-alive を起動する
    start_keep_alive()
    bot.run(token)