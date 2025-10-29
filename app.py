import os
import logging
from collections import defaultdict, deque

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, Update
from aiogram.client.default import DefaultBotProperties

from dotenv import load_dotenv
from openai import OpenAI

# -----------------------------
# Env & basic config
# -----------------------------
load_dotenv(override=False)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_TEMPERATURE = float(os.environ.get("OPENAI_TEMPERATURE", "0.7"))
MAX_TURNS = int(os.environ.get("MAX_TURNS", "6"))  # how many back-and-forths to keep

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL")  # e.g. https://<your-app>.up.railway.app
PORT = int(os.getenv("PORT", "8000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# -----------------------------
# aiogram setup
# -----------------------------
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
router = Router()
dp = Dispatcher()
dp.include_router(router)

# -----------------------------
# OpenAI client
# -----------------------------
client = OpenAI(api_key=OPENAI_API_KEY)

# Per-chat rolling memory: { chat_id: deque([{"role":"user"/"assistant","content":...}, ...]) }
histories = defaultdict(lambda: deque(maxlen=2 * MAX_TURNS))

# System prompt to make the bot a top agronomist / γεωπόνος
SYSTEM_PROMPT = (
    "You are an elite agronomist (γεωπόνος) and crop specialist. "
    "Be friendly, clear, and structured like ChatGPT. Ask clarifying questions when needed, "
    "and keep answers practical and concise, with numbered steps or bullets where helpful. "
    "Expertise: plant nutrition & fertilization (macro/micro), irrigation, soil science, IPM, "
    "diagnosis & control of diseases and pests, scouting thresholds, and timing by phenological stage. "
    "Prefer active substances and IPM strategies over brand names; flag local regulations & label compliance. "
    "Default language: Greek unless the user clearly requests English."
)

async def ask_llm(chat_id: int, user_prompt: str) -> str:
    """
    Call OpenAI Responses API with a ChatGPT-like conversational format
    and per-chat rolling memory.
    """
    try:
        # Build message list: system + (history) + new user
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(list(histories[chat_id]))  # user/assistant alternating
        messages.append({"role": "user", "content": user_prompt})

        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=messages,                   # responses API accepts role-based list via 'input'
            temperature=OPENAI_TEMPERATURE,  # conversational tone
            max_output_tokens=800,           # enough room for practical guidance
            top_p=1,
        )

        answer = resp.output_text.strip()

        # Update memory
        histories[chat_id].append({"role": "user", "content": user_prompt})
        histories[chat_id].append({"role": "assistant", "content": answer})

        return answer
    except Exception as e:
        log.exception("OpenAI error")
        return f"⚠️ Σφάλμα AI: {e}"

# -----------------------------
# Handlers
# -----------------------------
@router.message(Command("start"))
async def on_start_cmd(message: Message):
    # Clear chat memory on new start for a fresh session
    histories.pop(message.chat.id, None)
    log.info(f"/start from {message.from_user.id} @{message.from_user.username}")
    await message.answer(
        "👋 Καλώς ήρθες! Είμαι ο γεωπόνος σου.\n"
        "Στείλε <code>/ai την ερώτησή σου</code> και θα απαντήσω σαν κορυφαίος ειδικός.\n"
        "Παράδειγμα: <code>/ai Πρόγραμμα λίπανσης για ντομάτα θερμοκηπίου;</code>"
    )

@router.message(Command("ai"))
async def on_ai(message: Message, command: CommandObject):
    if not command.args:
        await message.reply(
            "Δώσε ερώτημα μετά την εντολή. Παράδειγμα: "
            "<code>/ai Συμπτώματα έλλειψης μαγνησίου στην ελιά;</code>"
        )
        return

    await message.chat.do("typing")
    chat_id = message.chat.id
    log.info(f"/ai from {message.from_user.id} @{message.from_user.username}: {command.args}")
    answer = await ask_llm(chat_id, command.args)
    await message.answer(answer)

# -----------------------------
# FastAPI app & webhook
# -----------------------------
app = FastAPI()

@app.get("/")
async def health():
    return {
        "status": "ok",
        "port": PORT,
        "model": OPENAI_MODEL,
        "temperature": OPENAI_TEMPERATURE,
        "max_turns": MAX_TURNS,
    }

# Use only the numeric part of the token in the URL path
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN.split(':', 1)[0]}"

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    # Optional secret verification
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret token")

    data = await request.json()
    try:
        update = Update.model_validate(data)
    except Exception:
        raise HTTPException(status_code=400, detail="Bad update payload")

    await dp.feed_update(bot, update)
    return JSONResponse({"ok": True})

# Simple request logging for webhook hits (helpful for debugging)
@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.url.path.startswith("/webhook/"):
        log.info(f"Webhook hit: {request.method} {request.url.path}")
    return await call_next(request)

# -----------------------------
# Webhook lifecycle
# -----------------------------
@app.on_event("startup")
async def on_startup():
    if not PUBLIC_URL:
        log.warning("PUBLIC_URL not set; webhook not registered.")
        return
    url = PUBLIC_URL.rstrip("/") + WEBHOOK_PATH
    await bot.set_webhook(url=url, secret_token=(WEBHOOK_SECRET or None))
    log.info(f"Webhook set to: {url} (listening on port {PORT})")

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await bot.delete_webhook()
    except Exception:
        pass
