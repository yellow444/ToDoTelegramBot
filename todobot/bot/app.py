"""Сборка Telegram-приложения."""

import logging

from telegram import BotCommand, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from . import handlers as h
from . import jobs

logger = logging.getLogger(__name__)

COMMANDS = [
    ("list", "Открытые задачи"),
    ("today", "План на сегодня"),
    ("ai", "Спросить ассистента"),
    ("mode", "Кнопки ↔ мини-приложение"),
    ("app", "Открыть мини-приложение"),
    ("work", "Рабочие часы и дни"),
    ("digest", "Утренний план / вечерний итог"),
    ("smart", "Разбор задач через LLM"),
    ("tz", "Часовой пояс"),
    ("claude", "Поручить задачу Claude"),
    ("codex", "Поручить задачу Codex"),
    ("agents", "Что делают агенты"),
    ("model", "Модель ассистента"),
    ("recognize", "Распознать голос/фото в задачах"),
    ("orchestrate", "Кто разбирает утро и вечер"),
    ("vocab", "Словарь для распознавания речи"),
    ("ideas", "Идеи без срока"),
    ("review", "Разбор и чистка задач"),
    ("forget", "Очистить память ассистента"),
    ("help", "Справка"),
]


SHORT_DESCRIPTION = (
    "Задачи и напоминания: напишите сообщение — получите задачу с кнопками. "
    "Рабочий день, повторы, ИИ-ассистент."
)
DESCRIPTION = """Личный таск-трекер прямо в чате.

📝 Любое сообщение — задача: текст, фото, файл, голосовое.
⏰ Пишите время как обычно: «завтра в 11», «через 2 часа», «по будням в 10».
🔔 Напоминаю, пока задача не закрыта.
🏢 Рабочий день: утренний план и вечерний итог.
🤖 Ассистент: «? что у меня на завтра».
🧑‍💻 Коворкеры: /claude и /codex берут сложные задачи.
📱 Кнопки в чате или мини-приложение — /mode."""


async def _review_command(update, context):
    from .review import review_command

    await review_command(update, context)


def build_bot(deps: h.Deps) -> Application:
    app = Application.builder().token(deps.settings.TOKEN).build()
    app.bot_data["deps"] = deps

    app.add_handler(TypeHandler(Update, h.access_guard), group=-1)
    for name, fn in [
        ("start", h.start), ("help", h.help_command), ("stop", h.stop), ("mode", h.mode), ("app", h.open_app),
        ("list", h.list_command), ("today", h.today), ("work", h.work), ("digest", h.digest),
        ("smart", h.smart), ("tz", h.tz), ("ai", h.ai_command), ("forget", h.forget),
        ("claude", h.claude_command), ("codex", h.codex_command), ("agents", h.agents_command),
        ("model", h.model_command), ("recognize", h.recognize_command), ("orchestrate", h.orchestrate_command),
        ("vocab", h.vocab_command), ("ideas", h.ideas_command),
        ("review", _review_command),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(h.on_callback))
    app.add_handler(MessageHandler(~filters.COMMAND & ~filters.StatusUpdate.ALL, h.on_message))
    app.add_error_handler(h.on_error)
    return app


async def apply_profile(bot) -> None:
    """Описание и команды бота; обновляем только при изменении (у этих методов жёсткие лимиты)."""
    commands = [BotCommand(n, d) for n, d in COMMANDS]
    if [(c.command, c.description) for c in await bot.get_my_commands()] != COMMANDS:
        await bot.set_my_commands(commands)
    if (await bot.get_my_short_description()).short_description != SHORT_DESCRIPTION:
        await bot.set_my_short_description(SHORT_DESCRIPTION)
    if (await bot.get_my_description()).description != DESCRIPTION:
        await bot.set_my_description(DESCRIPTION)


async def on_startup(app: Application) -> None:
    try:
        await apply_profile(app.bot)
    except TelegramError as exc:  # профиль не критичен для работы
        if "frozen" in str(exc).lower():
            logger.error("Telegram заморозил аккаунт бота (FROZEN_METHOD_INVALID): нужна апелляция владельца, см. @SpamBot")
        else:
            logger.warning("Не удалось обновить профиль бота: %s", exc)
    jq = app.job_queue
    # опоздавший тик выполняем, а не пропускаем (APScheduler по умолчанию терпит 1 с); накопившиеся — один раз
    late = {"misfire_grace_time": 60, "coalesce": True}
    jq.run_repeating(jobs.reminders_tick, interval=15, first=3, name="reminders", job_kwargs=late)
    jq.run_repeating(jobs.cards_tick, interval=5, first=2, name="cards", job_kwargs=late)
    jq.run_repeating(jobs.workday_tick, interval=60, first=10, name="workday", job_kwargs=late)
