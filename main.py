import os, asyncio, time, random
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums import ParseMode, ChatType, MessageEntityType
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

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

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY missing")

client = OpenAI(api_key=OPENAI_API_KEY)

# aiogram v3.7+ style default props
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
)
dp = Dispatcher()

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

# ---------- Utility ----------
async def build_context(chat_id: str) -> str:
    summary = await memory.get_latest_summary(chat_id)
    recent = await memory.get_recent_text(chat_id, limit_chars=RECENT_CHARS)
    if summary and recent:
        return f"{summary}\n--- Recent:\n{recent}"
    return summary or recent or ""

async def maybe_summarize(chat_id: str):
    count = await memory.count_messages(chat_id)
    if count > 0 and count % SUMMARY_EVERY_N_MESSAGES == 0:
        long_context = await memory.get_recent_text(chat_id, limit_chars=max(RECENT_CHARS * 3, 16*
