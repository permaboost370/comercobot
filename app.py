import os
input=f"You are a concise assistant. Answer clearly.\n\nUser: {prompt}",
)
# Python SDK helper to get concatenated text
return resp.output_text.strip()
except Exception as e:
return f"⚠️ AI error: {e}" # Keep the bot resilient


# --- Handlers ---
@router.message(Command("start"))
async def on_start(message: Message):
await message.answer(
"👋 Hi! Send <code>/ai your question</code> and I'll reply.\n"
"Example: <code>/ai best pizza dough recipe?</code>"
)


@router.message(Command("ai"))
async def on_ai(message: Message, command: CommandObject):
if not command.args:
await message.reply("Please provide a prompt. Example: <code>/ai what is a closure in Python?</code>")
return
await message.chat.do("typing")
answer = await ask_llm(command.args)
await message.answer(answer)


# --- FastAPI app + webhook endpoint ---
app = FastAPI()


@app.get("/")
async def health():
return {"status": "ok"}


# Telegram will POST updates here. Use a secret path based on your token by default.
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN.split(':', 1)[0]}" # hides full token


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
# Optional: Verify Telegram secret header if you set it when registering the webhook
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


# --- Startup/shutdown: set & delete webhook ---
@app.on_event("startup")
async def on_startup():
if not PUBLIC_URL:
# In Railway, set PUBLIC_URL in Variables after domain is generated.
print("WARNING: PUBLIC_URL not set; webhook not registered.")
return
url = PUBLIC_URL.rstrip("/") + WEBHOOK_PATH
# Register webhook with optional secret token for verification
await bot.set_webhook(url=url, secret_token=(WEBHOOK_SECRET or None))
print(f"Webhook set to: {url}")


@app.on_event("shutdown")
async def on_shutdown():
try:
await bot.delete_webhook()
except Exception:
pass
