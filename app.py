#!/usr/bin/python3
import asyncio
import logging
import os
import time

from pydantic import BaseModel
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from config import settings
from handlers import button, handle_datepicker_input, help_command, start, stop
from scheduler import pop_task

# Настройка временной зоны
os.environ["TZ"] = "Europe/Moscow"
time.tzset()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


TOKEN = settings.TOKEN


class HealthCheck(BaseModel):
    status: str = "OK"


tg_app = Application.builder().token(TOKEN).build()
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("stop", stop))
tg_app.add_handler(CallbackQueryHandler(button))
tg_app.add_handler(CommandHandler("help", help_command))
tg_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_datepicker_input))


async def run_bot() -> None:
    async with tg_app:
        await tg_app.updater.start_polling()
        await tg_app.start()
        await asyncio.Event().wait()  # run forever


async def main() -> None:
    await asyncio.gather(pop_task(tg_app), run_bot())


if __name__ == "__main__":
    asyncio.run(main())

