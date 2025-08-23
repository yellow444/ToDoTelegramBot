#!/usr/bin/python3
import logging
import os
import time

from pydantic import BaseModel
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from config import settings
from handlers import button, handle_datepicker_input, help_command, start, stop
from scheduler import pop_job  # фон как JobQueue-джоба

# ЧАСОВОЙ ПОЯС: под вашу локацию (Германия)
os.environ["TZ"] = "Europe/Moscow"
time.tzset()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.DEBUG
)
logger = logging.getLogger(__name__)

TOKEN = settings.TOKEN


class HealthCheck(BaseModel):
    status: str = "OK"


async def _post_init(app: Application) -> None:
    """Регистрируем фоновую задачу через JobQueue в том же event loop, что и бот."""
    app.job_queue.run_repeating(pop_job, interval=10, first=5)  # окно 10s, старт через 5s
    logger.info("JobQueue pop_job scheduled: first=5s, interval=10s")


def build_app() -> Application:
    tg_app = (
        Application.builder()
        .token(TOKEN)
        .post_init(_post_init)
        .build()
    )
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("stop", stop))
    tg_app.add_handler(CallbackQueryHandler(button))
    tg_app.add_handler(CommandHandler("help", help_command))
    tg_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_datepicker_input))
    return tg_app


if __name__ == "__main__":
    app = build_app()
    app.run_polling()
