import asyncio
import datetime
import json
import logging

from telegram import ReplyKeyboardRemove, Update
from telegram.ext import ContextTypes

import messages
import telegramcalendar
import utils
from db import count_reminders, delete_reminders, insert_reminder, update_reminder

logger = logging.getLogger(__name__)

# Глобальное хранилище состояний пользователей
user_states: dict = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start."""
    chat_id = update.effective_chat.id
    msg_text = r"_It is not the message you are looking for\.\.\._"
    try:
        await context.bot.send_message(
            chat_id,
            msg_text,
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="MarkdownV2",
        )
        logger.info("Команда /start выполнена для чата %s", chat_id)
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Ошибка в start: %s", exc)


async def calendar_timeout(bot, chat_id, message_id, user_id):
    """Удаляет календарь, если пользователь не выбрал дату вовремя."""
    await asyncio.sleep(30)
    state = user_states.get(user_id, {})
    if state.get("data_message_id") == message_id and state.get("waiting_for_date"):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as exc:  # pragma: no cover - logging only
            logger.warning("Ошибка удаления сообщения календаря: %s", exc)
        try:
            await bot.send_message(chat_id=chat_id, text=messages.calendar_timeout)
        except Exception as exc:  # pragma: no cover - logging only
            logger.warning("Ошибка отправки сообщения тайм-аута: %s", exc)
        user_states[user_id] = {}


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатий на inline-кнопки."""
    try:
        query = update.callback_query or update
        bot = context.bot
        user_id = query.from_user.id
        bot_message_id = query.message.message_id
        bot_message_text = query.message.text or query.message.caption or ""
        if user_id not in user_states:
            user_states[user_id] = {}

        user_states[user_id]["data_chat_id"] = query.message.chat.id
        user_states[user_id]["bot_message_caption"] = query.message.caption

        if bot_message_text == messages.calendar_message or (
            user_states[user_id].get("data_message_id") == bot_message_id
        ):
            await inline_handler(update, context)
            return

        # Удаляем предыдущие служебные сообщения, если они существуют
        if "data_message_id" in user_states[user_id]:
            try:
                await bot.delete_message(
                    chat_id=user_states[user_id]["data_chat_id"],
                    message_id=user_states[user_id]["data_message_id"],
                )
            except Exception as exc:  # pragma: no cover - logging only
                logger.warning("Ошибка удаления сообщения: %s", exc)
        user_states[user_id] = {
            "bot_message_id": bot_message_id,
            "bot_message_text": bot_message_text,
        }

        source = (query.message.text or query.message.caption or "").replace("~~", "")
        reply_markup = None
        text = None

        if query.data == "date":
            await calendar_handler(update, context)
            return
        if query.data == "del":
            delete_reminders({"message_id": query.message.message_id})
            try:
                await bot.delete_message(
                    chat_id=query.message.chat.id, message_id=query.message.message_id
                )
            except Exception as exc:  # pragma: no cover - logging only
                logger.warning("Ошибка удаления сообщения 'del': %s", exc)
            return
        if query.data == "done":
            text = f"✅ {source}" if source else "✅"
            reply_markup = utils.task_markup(done=True)
            delete_reminders({"message_id": query.message.message_id})
        elif query.data == "undone":
            if source.startswith("<del>") and source.endswith("</del>"):
                source = source[5:-6]
            text = (source[1:] if source.startswith("✅") else source) if source else None
            reply_markup = utils.task_markup()
        else:
            # Игнорируем другие callback'и (например, календарь)
            return

        if text and text != query.message.text:
            if query.message.text:
                await bot.edit_message_text(
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
            else:
                await bot.edit_message_caption(
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                    caption=text,
                    reply_markup=reply_markup,
                )
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Ошибка в button: %s", exc)


async def select_date(
    update: Update, context: ContextTypes.DEFAULT_TYPE, date: datetime.datetime
):
    """Обработчик выбора даты."""
    try:
        query = update.callback_query or update
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Ошибка получения callback_query: %s", exc)
        return
    bot_message_id = query.message.message_id
    bot_message_text = query.message.text or query.message.caption or ""
    logger.info("Дата выбрана")
    chat_id = query.message.chat.id
    if count_reminders({"message_id": bot_message_id}) == 0:
        today = " :" + datetime.datetime.today().strftime("%d-%m-%Y %H:%M")
        insert_reminder(
            {
                "chat_id": chat_id,
                "message_id": bot_message_id,
                "message": bot_message_text,
                "caption": user_states.get(query.from_user.id, {}).get(
                    "bot_message_caption"
                ),
                "date": user_states.get(query.from_user.id, {})
                .get("date", date)
                .strftime("%d-%m-%Y %H:%M"),
                "today": today,
            }
        )
    else:
        update_reminder(
            bot_message_id,
            {"date": user_states[query.from_user.id]["date"].strftime("%d-%m-%Y %H:%M")},
        )
    bot_message_text = user_states.get(query.from_user.id, {}).get(
        "bot_message_text", ""
    )
    text = (
        bot_message_text.split(" ::")[0] + " ::" + date.strftime("%d-%m-%Y %H:%M")
    ) if bot_message_text else ""
    if text and text != bot_message_text:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=bot_message_id,
            text=text,
            reply_markup=utils.task_markup(),
        )


async def handle_datepicker_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Обработчик ввода даты с клавиатуры."""
    try:
        if not update.message:
            return
        text = update.message.text
        if not text or text.isspace() or messages.calendar_message in text:
            try:
                await context.bot.delete_message(
                    chat_id=update.message.chat.id,
                    message_id=update.message.message_id,
                )
            except Exception as exc:  # pragma: no cover - logging only
                logger.warning(
                    "Ошибка удаления сообщения handle_datepicker_input: %s", exc
                )
            finally:
                user_states[update.message.from_user.id] = {}
        else:
            await echo(update, context)
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Ошибка в handle_datepicker_input: %s", exc)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help."""
    logger.info("help_command вызван")
    try:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Use /start to test this bot.")
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Ошибка в help_command: %s", exc)


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Эхо всех сообщений пользователя."""
    try:
        bot = context.bot
        user_id = update.message.from_user.id
        if (
            user_id in user_states
            and "data_chat_id" in user_states[user_id]
            and "data_message_id" in user_states[user_id]
        ):
            try:
                await bot.delete_message(
                    chat_id=user_states[user_id]["data_chat_id"],
                    message_id=user_states[user_id]["data_message_id"],
                )
            except Exception as exc:  # pragma: no cover - logging only
                logger.warning("Ошибка удаления сообщения в echo: %s", exc)
            finally:
                user_states[user_id] = {}
        first = None
        if update.message.video:
            first = await update.message.reply_video(
                video=update.message.video, caption=update.message.caption
            )
        elif update.message.audio:
            first = await update.message.reply_audio(
                audio=update.message.audio, caption=update.message.caption
            )
        elif update.message.document:
            first = await update.message.reply_document(
                document=update.message.document, caption=update.message.caption
            )
        elif update.message.sticker:
            first = await update.message.reply_sticker(
                sticker=update.message.sticker, caption=update.message.caption
            )
        elif update.message.voice:
            first = await update.message.reply_voice(
                voice=update.message.voice, caption=update.message.caption
            )
        elif update.message.location:
            first = await update.message.reply_location(
                location=update.message.location, caption=update.message.caption
            )
        elif update.message.contact:
            first = await update.message.reply_contact(
                contact=update.message.contact, caption=update.message.caption
            )
        elif update.message.venue:
            first = await update.message.reply_venue(
                venue=update.message.venue, caption=update.message.caption
            )
        else:
            first = await update.message.reply_text(update.message.text)
        today = " ::" + datetime.datetime.today().strftime("%d-%m-%Y %H:%M")
        if (
            update.message.video
            or update.message.audio
            or update.message.document
            or update.message.sticker
            or update.message.voice
            or update.message.location
            or update.message.contact
            or update.message.venue
        ):
            await bot.edit_message_caption(
                chat_id=first.chat_id,
                message_id=first.message_id,
                caption=(first.caption or "") + today,
                reply_markup=utils.task_markup(),
            )
        else:
            await bot.edit_message_text(
                chat_id=first.chat_id,
                message_id=first.message_id,
                text=(first.text or "") + today,
                reply_markup=utils.task_markup(),
            )
        await update.message.delete()
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Ошибка в echo: %s", exc)


async def calendar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик календаря."""
    try:
        query = update.callback_query or update
        bot = context.bot
        user_id = query.from_user.id
        msg = await bot.send_message(
            chat_id=query.message.chat.id,
            text=messages.calendar_message,
            reply_markup=telegramcalendar.create_calendar(),
        )
        user_states[user_id]["data_chat_id"] = msg.chat.id
        user_states[user_id]["data_message_id"] = msg.message_id
        user_states[user_id]["waiting_for_date"] = messages.calendar_message
        if task := user_states[user_id].get("timeout_task"):
            task.cancel()
        user_states[user_id]["timeout_task"] = asyncio.create_task(
            calendar_timeout(bot, msg.chat.id, msg.message_id, user_id)
        )
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Ошибка в calendar_handler: %s", exc)


async def received_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик данных из веб-приложения."""
    try:
        data = json.loads(update.message.web_app_data.data)
        dates = []
        for date_str in data:
            dt_obj = datetime.datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            dates.append(dt_obj)
        logger.info("Получены даты: %s", dates)
        bot = context.bot
        message = update.message
        try:
            await bot.delete_message(
                chat_id=message.chat.id, message_id=message.message_id
            )
        except Exception as exc:  # pragma: no cover - logging only
            logger.warning("Ошибка удаления сообщения в received_data: %s", exc)
        reply_markup = message.reply_markup
        if message.text:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=message.message_id,
                text=f"{message.text} Напомнить:{dates}",
                reply_markup=reply_markup,
            )
        else:
            await bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=message.message_id,
                caption=f"{message.caption} Напомнить:{dates}",
                reply_markup=reply_markup,
            )
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Ошибка в received_data: %s", exc)


async def remove_chat_buttons(bot, chat_id):
    """Удаление кнопок чата."""
    msg_text = r"_It is not the message you are looking for\.\.\._"
    try:
        msg = await bot.send_message(
            chat_id,
            msg_text,
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="MarkdownV2",
        )
        await msg.delete()
    except Exception as exc:  # pragma: no cover - logging only
        logger.warning("Ошибка в remove_chat_buttons: %s", exc)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /stop."""
    logger.info("stop вызван")
    try:
        chat_id = update.effective_chat.id
        await remove_chat_buttons(context.bot, chat_id)
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Ошибка в stop: %s", exc)


async def inline_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline обработчики."""
    try:
        query = update.callback_query or update
        utils.separate_callback_data(query.data)
        await inline_calendar_handler(update, context)
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Ошибка в inline_handler: %s", exc)


async def inline_calendar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий календаря."""
    try:
        query = update.callback_query or update
        bot = context.bot
        user_id = query.from_user.id
        selected, date = await telegramcalendar.process_calendar_selection(update, context)
        if task := user_states.get(user_id, {}).pop("timeout_task", None):
            task.cancel()
        if selected:
            user_states[user_id]["date"] = date
            try:
                await context.bot.delete_message(
                    chat_id=user_states[user_id]["data_chat_id"],
                    message_id=user_states[user_id]["data_message_id"],
                )
                if date == "CANCEL":
                    delete_reminders(
                        {"message_id": user_states[user_id]["data_message_id"]}
                    )
                    try:
                        await bot.send_message(
                            chat_id=user_states[user_id]["data_chat_id"],
                            text=messages.calendar_cancelled,
                        )
                    except Exception as exc:  # pragma: no cover - logging only
                        logger.warning("Ошибка отправки уведомления CANCEL: %s", exc)
                    user_states[user_id] = {}
                    return
                text = (
                    user_states[user_id]["bot_message_text"].split(" ::")[0]
                    + " ::"
                    + date.strftime("%d-%m-%Y %H:%M")
                )
                if text and text.strip() and text != messages.calendar_message:
                    await context.bot.edit_message_text(
                        chat_id=user_states[user_id]["data_chat_id"],
                        message_id=user_states[user_id]["bot_message_id"],
                        text=text,
                        reply_markup=utils.task_markup(),
                    )
                    if count_reminders(
                        {"message_id": user_states[user_id]["bot_message_id"]}
                    ) == 0:
                        today = " :" + datetime.datetime.today().strftime(
                            "%d-%m-%Y %H:%M"
                        )
                        insert_reminder(
                            {
                                "chat_id": user_states[user_id]["data_chat_id"],
                                "message_id": user_states[user_id]["bot_message_id"],
                                "message": user_states[user_id][
                                    "bot_message_text"
                                ].split(" ::")[0],
                                "caption": user_states[user_id]["bot_message_caption"],
                                "date": date.strftime("%d-%m-%Y %H:%M"),
                                "today": today,
                            }
                        )
                    else:
                        update_reminder(
                            user_states[user_id]["bot_message_id"],
                            {"date": date.strftime("%d-%m-%Y %H:%M")},
                        )
            except Exception as exc:  # pragma: no cover - logging only
                logger.error("Ошибка в inline_calendar_handler: %s", exc)
            finally:
                user_states[user_id] = {}
        else:
            user_states[user_id]["timeout_task"] = asyncio.create_task(
                calendar_timeout(bot, query.message.chat.id, query.message.message_id, user_id)
            )
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Ошибка в inline_calendar_handler (общая): %s", exc)
