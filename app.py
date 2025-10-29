import os
import logging

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

async def ask_llm(prompt: str) -> str:
    """Call OpenAI Responses API and return plain text output."""
    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=f"You are a concise assistant. Answer clearly.\n\nUser: {prompt}",
        )
        return resp.output_text.strip()
    except Exception as e:
        log.exception("OpenAI error")
        return f"⚠️ AI error: {e}"

# -----------------------------
# Handlers
# -----------------------------
@router.message(Command("start"))
async def on_start_cmd(message: Message):
    log.info(f"/start from {message.from_user.id} @{message.from_user.username}")
    await message.answer(
        "👋 Hi! Send <code>/ai your question</code> and I'll reply.\n"
        "Example: <code>/ai best pizza dough recipe?</code>"
    )

@router.message(Command("ai"))
async def on_ai(message: Message, command: CommandObject):
    if not command.args:
        await message.reply(
            "Please provide a prompt. Example: <code>/ai what is a closure in Python?</code>"
        )
        return
    await message.chat.do("typing")
    log.info(f"/ai from {message.from_user.id} @{message.from_user.username}: {command.args}")
    answer = await ask_llm(command.args)
    await message.answer(answer)

# -----------------------------
# FastAPI app & webhook
# -----------------------------
app = FastAPI()

@app.get("/")
async def health():
    return {"status": "ok", "port": PORT, "model": OPENAI_MODEL}

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
