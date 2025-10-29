import os, asyncio, time, random
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, Update
from aiogram.enums import ParseMode, ChatType, MessageEntityType
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.client.default import DefaultBotProperties

import aiosqlite
from openai import OpenAI, RateLimitError, APIError

# ---------- Load environment ----------
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

MEMORY_DB = os.getenv("MEMORY_DB", "memory.db")
MAX_REPLY_CHARS = int(os.getenv("MAX_REPLY_CHARS", "4096"))
RECENT_CHARS = int(os.getenv("RECENT_CHARS", "6000"))
SUMMARY_EVERY_N_MESSAGES = int(os.getenv("SUMMARY_EVERY_N_MESSAGES", "80"))

WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://app.up.railway.app/webhook/abc123
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # random secret header token

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY missing")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL missing")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET missing")

client = OpenAI(api_key=OPENAI_API_KEY)

# aiogram v3.7+ default parse mode
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
)
dp = Dispatcher()

app = FastAPI(title="Telegram Webhook Bot")

# ---------- Memory Layer (SQLite) ----------
INIT_SQL = """
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  user_id TEXT,
  username TEXT,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, ts);

CREATE TABLE IF NOT EXISTS summaries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  content TEXT NOT NULL,
  ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_chat_ts ON summaries(chat_id, ts);
"""

class Memory:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(INIT_SQL)
            await db.commit()

    async def add_message(self, chat_id: str, user_id: Optional[str], username: Optional[str], role: str, content: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO messages(chat_id,user_id,username,role,content,ts) VALUES(?,?,?,?,?,?)",
                (chat_id, user_id, username, role, content, int(time.time()))
            )
            await db.commit()

    async def get_recent_text(self, chat_id: str, limit_chars: int = RECENT_CHARS) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            rows = await db.execute_fetchall(
                "SELECT role, username, content FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT 300",
                (chat_id,)
            )
        rows = rows[::-1]
        out, total = [], 0
        for role, username, content in rows:
            name = username or role
            line = f"{role.upper()}({name}): {content}\n"
            total += len(line)
            out.append(line)
            if total >= limit_chars:
                break
        return "".join(out)

    async def get_latest_summary(self, chat_id: str) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            row = await db.execute_fetchone(
                "SELECT content FROM summaries WHERE chat_id=? ORDER BY ts DESC LIMIT 1",
                (chat_id,)
            )
        return row[0] if row else ""

    async def save_summary(self, chat_id: str, content: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO summaries(chat_id, content, ts) VALUES(?,?,?)",
                (chat_id, content, int(time.time()))
            )
            await db.commit()

    async def count_messages(self, chat_id: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            row = await db.execute_fetchone(
                "SELECT COUNT(*) FROM messages WHERE chat_id=?",
                (chat_id,)
            )
        return row[0] if row else 0

memory = Memory(MEMORY_DB)

SYSTEM_PROMPT = (
    "You are a helpful assistant in a Telegram GROUP. "
    "Use the provided memory summary + recent messages to stay consistent with names, preferences, and decisions. "
    "Be concise unless asked for detail."
)

# ---------- OpenAI helpers ----------
async def llm_reply(user_text: str, system_prompt: Optional[str], context: Optional[str]) -> str:
    backoff = 0.8
    for attempt in range(6):
        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            if context:
                messages.append({"role": "system", "content": f"Conversation memory summary:\n{context}"})
            messages.append({"role": "user", "content": user_text})

            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
            )
            return (response.choices[0].message.content or "").strip()
        except (RateLimitError, APIError):
            if attempt == 5:
                raise
            sleep_s = backoff + random.random() * backoff
            await asyncio.sleep(sleep_s)
            backoff = min(backoff * 2, 8)
        except Exception:
            if attempt == 5:
                raise
            await asyncio.sleep(1.2)
    return "(no response)"

# ---------- Utilities ----------
async def build_context(chat_id: str) -> str:
    summary = await memory.get_latest_summary(chat_id)
    recent = await memory.get_recent_text(chat_id, limit_chars=RECENT_CHARS)
    if summary and recent:
        return f"{summary}\n--- Recent:\n{recent}"
    return summary or recent or ""

async def maybe_summarize(chat_id: str):
    count = await memory.count_messages(chat_id)
    if count > 0 and count % SUMMARY_EVERY_N_MESSAGES == 0:
        long_context = await memory.get_recent_text(chat_id, limit_chars=max(RECENT_CHARS * 3, 16000))
        try:
            summary_text = await llm_reply(
                user_text=long_context,
                system_prompt=(
                    "Summarize the group conversation into durable notes: participants, preferences, decisions, tasks, links, and stable facts. "
                    "Keep it under 600 words. Update/merge rather than repeat."
                ),
                context=None,
            )
            await memory.save_summary(chat_id, summary_text)
        except Exception:
            pass

# ---------- Commands ----------
@dp.message(CommandStart())
async def on_start(msg: Message):
    await memory.add_message(str(msg.chat.id), str(msg.from_user.id), msg.from_user.username, "user", msg.text or "")
    await msg.answer(
        "Hi! I’m alive in this chat. I remember context across messages.\n"
        "In groups, mention me or reply to me when you want an answer.\n"
        "Or use /ai <prompt> (or reply to a message with /ai).\n"
        "Admins: /memory to view, /wipe to delete stored memory for this chat."
    )

@dp.message(Command("memory"))
async def on_memory(msg: Message):
    chat_id = str(msg.chat.id)
    context = await build_context(chat_id)
    preview = context[:2000] if context else "(no memory yet)"
    await msg.answer(f"Current memory preview:\n\n{preview}")

@dp.message(Command("wipe"))
async def on_wipe(msg: Message):
    chat_id = str(msg.chat.id)
    async with aiosqlite.connect(MEMORY_DB) as db:
        await db.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        await db.execute("DELETE FROM summaries WHERE chat_id=?", (chat_id,))
        await db.commit()
    await msg.answer("Memory wiped for this chat.")

@dp.message(Command("ai"))
async def on_ai(msg: Message, command: CommandObject):
    chat_id = str(msg.chat.id)
    user_id = str(msg.from_user.id) if msg.from_user else None
    username = msg.from_user.username if msg.from_user else None

    prompt = (command.args or "").strip() if command else ""
    if not prompt and msg.reply_to_message and msg.reply_to_message.text:
        prompt = msg.reply_to_message.text.strip()

    if not prompt:
        await msg.answer("Usage: `/ai your prompt` or reply to a message with `/ai`.")
        return

    await memory.add_message(chat_id, user_id, username, "user", f"/ai {prompt}")

    context = await build_context(chat_id)
    try:
        reply = await llm_reply(
            user_text=prompt,
            system_prompt=SYSTEM_PROMPT,
            context=context,
        )
        await msg.answer(reply[:MAX_REPLY_CHARS])
        self_user = await bot.me()
        await memory.add_message(chat_id, None, self_user.username if self_user else "assistant", "assistant", reply)
    except Exception as e:
        await msg.answer(f"LLM error: `{e}`")

    await maybe_summarize(chat_id)

# Optional fallback: mentions/replies (keeps memory fresh even if we don't respond)
@dp.message(F.text & (F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP, ChatType.PRIVATE})))
async def on_text(msg: Message):
    chat_id = str(msg.chat.id)
    user_id = str(msg.from_user.id) if msg.from_user else None
    username = msg.from_user.username if msg.from_user else None
    text = (msg.text or "").strip()

    # Always store for memory
    await memory.add_message(chat_id, user_id, username, "user", text)

    # Only auto-reply in DMs, mentions, or replies-to-me
    self_user = await bot.me()
    is_private = msg.chat.type == ChatType.PRIVATE

    mentioned = False
    if msg.entities:
        for e in msg.entities:
            if getattr(e, "type", None) == MessageEntityType.MENTION:
                mentioned = True
                break
    if self_user and (f"@{self_user.username}" in text):
        mentioned = True

    is_reply_to_me = (
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and self_user
        and msg.reply_to_message.from_user.id == self_user.id
    )

    should_reply = is_private or mentioned or is_reply_to_me
    if not should_reply:
        return

    context = await build_context(chat_id)
    try:
        reply = await llm_reply(
            user_text=text,
            system_prompt=SYSTEM_PROMPT,
            context=context,
        )
        await msg.answer(reply[:MAX_REPLY_CHARS])
        await memory.add_message(chat_id, None, self_user.username if self_user else "assistant", "assistant", reply)
    except Exception as e:
        await msg.answer(f"LLM error: `{e}`")

    await maybe_summarize(chat_id)

# ---------- FastAPI webhook endpoint ----------
@app.post("/webhook/{path_token}")
async def telegram_webhook(request: Request, path_token: str):
    # Verify Telegram secret header (optional but recommended)
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if header_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    update = Update.model_validate(data)  # pydantic v2
    await dp.feed_update(bot, update)
    return {"ok": True}

# ---------- FastAPI lifecycle ----------
@app.on_event("startup")
async def on_app_startup():
    await memory.init()
    # Set webhook on startup (idempotent). Drop pending to avoid backlog.
    await bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=["message"]
    )

@app.on_event("shutdown")
async def on_app_shutdown():
    # Optional: keep webhook set. If you want to remove it on shutdown:
    # await bot.delete_webhook(drop_pending_updates=False)
    pass
