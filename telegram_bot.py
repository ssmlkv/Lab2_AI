from __future__ import annotations

import asyncio
import os

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .app import open_app


def run_telegram(settings):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed = {int(s.strip()) for s in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if s.strip()}
    if not token or not allowed:
        raise RuntimeError("Для Telegram заполни TELEGRAM_BOT_TOKEN и TELEGRAM_ALLOWED_USER_IDS в .env")

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id in allowed:
            await update.effective_message.reply_text("Пришли артикул, количество по документам и фактическое количество. Можно указать несколько товаров.")

    async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in allowed:
            return
        session_id = f"tg:{update.effective_chat.id}:{update.effective_user.id}"
        try:
            result = await context.application.bot_data["warehouse"].process(
                update.effective_message.text, session_id, f"telegram:{update.update_id}")
            text = result["reply"]
        except Exception:
            text = "Не удалось подтвердить завершение обработки. Проверь журнал и журнал запуска перед повторной отправкой."
        for offset in range(0, len(text), 3900):
            await update.effective_message.reply_text(text[offset:offset + 3900])

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, message))
    async def serve():
        async with open_app(settings, verbose=True) as warehouse:
            async with application:
                application.bot_data["warehouse"] = warehouse
                await application.start()
                try:
                    await application.updater.start_polling(drop_pending_updates=False)
                    print(f"Telegram-бот @{application.bot.username} запущен; ожидание сообщений.", flush=True)
                    await asyncio.Event().wait()
                finally:
                    if application.updater.running:
                        await application.updater.stop()
                    await application.stop()
    asyncio.run(serve())

