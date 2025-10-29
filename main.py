import os, asyncio, time, random
async def on_text(msg: Message):
chat_id = str(msg.chat.id)
user_id = str(msg.from_user.id) if msg.from_user else None
username = msg.from_user.username if msg.from_user else None
text = (msg.text or "").strip()


# Always store message for memory
await memory.add_message(chat_id, user_id, username, "user", text)


# Decide whether to reply
self_user = await bot.me()
is_private = msg.chat.type == ChatType.PRIVATE


mentioned = False
if msg.entities:
for e in msg.entities:
if getattr(e, "type", "") == "mention":
mentioned = True
break
if self_user and (f"@{self_user.username}" in text):
mentioned = True


is_reply_to_me = msg.reply_to_message and msg.reply_to_message.from_user and self_user and msg.reply_to_message.from_user.id == self_user.id


should_reply = is_private or mentioned or is_reply_to_me
if not should_reply:
return # silent memory only


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


async def main():
await memory.init()
print("Bot is running (polling).")
await dp.start_polling(bot)


if __name__ == "__main__":
asyncio.run(main())
