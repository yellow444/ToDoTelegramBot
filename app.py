#!/usr/bin/python3
import asyncio
import datetime
import json
import logging
import os
import time

from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, BaseSettings
from pymongo import MongoClient
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardRemove, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import messages
import telegramcalendar
import utils

# Настройка временной зоны
os.environ["TZ"] = "Europe/Moscow"
time.tzset()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Настройки приложения через Pydantic
class Settings(BaseSettings):
    MONGO_HOST: str
    MONGO_PORT: int
    MONGO_USER: str
    MONGO_PASS: str
    DB_NAME: str
    COLLECTION_NAME: str
    TOKEN: str
    MYHOSTNAME: str
    PORT: str

    class Config:
        env_file = ".env"

config = Settings()

# Инициализация MongoDB
try:
    client = MongoClient(
        config.MONGO_HOST,
        config.MONGO_PORT,
        username=config.MONGO_USER,
        password=config.MONGO_PASS,
        serverSelectionTimeoutMS=30000,
    )
    try:
        client.server_info()
    except Exception as e:
        logger.error("Первичное подключение к Mongo не удалось: %s", e)
        client = MongoClient(
            "127.0.0.1",
            config.MONGO_PORT,
            username=config.MONGO_USER,
            password=config.MONGO_PASS,
            serverSelectionTimeoutMS=30000,
        )
        client.server_info()
    db = client[config.DB_NAME]
    collection = db[config.COLLECTION_NAME]
    logger.info("Подключение к Mongo успешно")
except Exception as e:
    logger.error("Ошибка подключения к Mongo: %s", e)
    collection = None

TOKEN = config.TOKEN
HOSTNAME = config.MYHOSTNAME
PORT = 80
logger.info("Hostname: %s", HOSTNAME)

class HealthCheck(BaseModel):
    status: str = "OK"

# Обработчик команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    except Exception as e:
        logger.error("Ошибка в start: %s", e)

# Глобальное хранилище состояний пользователей
user_states = {}
g_months = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May",
    6: "June", 7: "July", 8: "August", 9: "September", 10: "October",
    11: "November", 12: "December",
}


async def calendar_timeout(bot, chat_id, message_id, user_id):
    await asyncio.sleep(30)
    state = user_states.get(user_id, {})
    if state.get("data_message_id") == message_id and state.get("waiting_for_date"):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.warning("Ошибка удаления сообщения календаря: %s", e)
        try:
            await bot.send_message(chat_id=chat_id, text=messages.calendar_timeout)
        except Exception as e:
            logger.warning("Ошибка отправки сообщения тайм-аута: %s", e)
        user_states[user_id] = {}

# Обработчик нажатий на inline-кнопки
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        query = update.callback_query or update
        bot = context.bot
        user_id = query.from_user.id
        # Используем стандартное message_id
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
            except Exception as e:
                logger.warning("Ошибка удаления сообщения: %s", e)
        user_states[user_id] = {
            "bot_message_id": bot_message_id,
            "bot_message_text": bot_message_text,
        }
        # Получение исходного текста без "~~"
        source = (query.message.text or query.message.caption or "").replace("~~", "")
        reply_markup = None
        text = None
        if query.data in "date":
            await calendar_handler(update, context)
            return
        if query.data in "del":
            if collection is not None:
                collection.delete_many({"message_id": query.message.message_id})
            try:
                await bot.delete_message(
                    chat_id=query.message.chat.id, message_id=query.message.message_id
                )
            except Exception as e:
                logger.warning("Ошибка удаления сообщения 'del': %s", e)
            return
        if query.data in "done":
            text = f"✅ {source}" if source else "✅"
            keyboard = [
                [
                    InlineKeyboardButton("Выполнено", callback_data="undone"),
                    InlineKeyboardButton("📅 Напомнить", callback_data="date"),
                    InlineKeyboardButton("❌ Удалить", callback_data="del"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            if collection is not None:
                collection.delete_many({"message_id": query.message.message_id})
        else:
            if source.startswith("<del>") and source.endswith("</del>"):
                source = source[5:-6]
            text = None if not source else (source[1:] if source.startswith("✅") else source)
            keyboard = [
                [
                    InlineKeyboardButton("✔️ Выполнить", callback_data="done"),
                    InlineKeyboardButton("📅 Напомнить", callback_data="date"),
                    InlineKeyboardButton("❌ Удалить", callback_data="del"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

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
    except Exception as e:
        logger.error("Ошибка в button: %s", e)

# Обработчик выбора даты
async def selectDate(update: Update, context: ContextTypes.DEFAULT_TYPE, date: datetime.datetime):
    try:
        query = update.callback_query or update
    except Exception as e:
        logger.error("Ошибка получения callback_query: %s", e)
        return
    bot = context.bot
    user_id = query.from_user.id
    bot_message_id = query.message.message_id
    bot_message_text = query.message.text or query.message.caption or ""
    logger.info("Дата выбрана")
    _chat_id = query.message.chat.id
    if collection is not None:
        if collection.count_documents({"message_id": bot_message_id}) == 0:
            today = " :" + datetime.datetime.today().strftime("%d-%m-%Y %H:%M")
            collection.insert_one({
                "chat_id": _chat_id,
                "message_id": bot_message_id,
                "message": bot_message_text,
                "caption": user_states.get(user_id, {}).get("bot_message_caption"),
                "date": user_states.get(user_id, {}).get("date").strftime("%d-%m-%Y %H:%M") if user_states.get(user_id, {}).get("date") else "",
                "today": today,
            })
        else:
            collection.find_one_and_update(
                {"message_id": bot_message_id},
                {"$set": {
                    "date": user_states[user_id]["date"].strftime("%d-%m-%Y %H:%M")
                }},
            )
    bot_message_text = user_states.get(user_id, {}).get("bot_message_text", "")
    text = (bot_message_text.split(" ::")[0] + " ::" +
            date.strftime("%d-%m-%Y %H:%M")) if bot_message_text else ""
    keyboard = [[
        InlineKeyboardButton("✔️ Выполнить", callback_data="done"),
        InlineKeyboardButton("📅 Напомнить", callback_data="date"),
        InlineKeyboardButton("❌ Удалить", callback_data="del"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if text and text != bot_message_text:
        await context.bot.edit_message_text(
            chat_id=_chat_id,
            message_id=bot_message_id,
            text=text,
            reply_markup=reply_markup,
        )

# Пустой обработчик удаления календарного сообщения (при необходимости доработать)
async def remove_calendar_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("remove_calendar_message вызван")

# Обработчик ввода даты с клавиатуры
async def handle_datepicker_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            except Exception as e:
                logger.warning("Ошибка удаления сообщения handle_datepicker_input: %s", e)
            finally:
                user_states[update.message.from_user.id] = {}
        else:
            await echo(update, context)
    except Exception as e:
        logger.error("Ошибка в handle_datepicker_input: %s", e)

# Обработчик команды /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("help_command вызван")
    try:
        await update.callback_query.message.reply_text("Use /start to test this bot.")
    except Exception as e:
        logger.error("Ошибка в help_command: %s", e)

# Обработчик эхо-сообщений
async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        bot = context.bot
        user_id = update.message.from_user.id
        if user_id in user_states and "data_chat_id" in user_states[user_id] and "data_message_id" in user_states[user_id]:
            try:
                await bot.delete_message(
                    chat_id=user_states[user_id]["data_chat_id"],
                    message_id=user_states[user_id]["data_message_id"],
                )
            except Exception as e:
                logger.warning("Ошибка удаления сообщения в echo: %s", e)
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
        keyboard = [[
            InlineKeyboardButton("✔️ Выполнить", callback_data="done"),
            InlineKeyboardButton("📅 Напомнить", callback_data="date"),
            InlineKeyboardButton("❌ Удалить", callback_data="del"),
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        today = " ::" + datetime.datetime.today().strftime("%d-%m-%Y %H:%M")
        if update.message.video or update.message.audio or update.message.document or \
           update.message.sticker or update.message.voice or update.message.location or \
           update.message.contact or update.message.venue:
            await bot.edit_message_caption(
                chat_id=first.chat_id,
                message_id=first.message_id,
                caption=(first.caption or "") + today,
                reply_markup=reply_markup,
            )
        else:
            await bot.edit_message_text(
                chat_id=first.chat_id,
                message_id=first.message_id,
                text=(first.text or "") + today,
                reply_markup=reply_markup,
            )
        await update.message.delete()
    except Exception as e:
        logger.error("Ошибка в echo: %s", e)

# Обработчик календаря
async def calendar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    except Exception as e:
        logger.error("Ошибка в calendar_handler: %s", e)

# Обработчик данных из веб-приложения
async def received_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        except Exception as e:
            logger.warning("Ошибка удаления сообщения в received_data: %s", e)
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
    except Exception as e:
        logger.error("Ошибка в received_data: %s", e)

# Удаление кнопок чата
async def remove_chat_buttons(bot, chat_id):
    msg_text = r"_It is not the message you are looking for\.\.\._"
    try:
        msg = await bot.send_message(
            chat_id,
            msg_text,
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="MarkdownV2",
        )
        await msg.delete()
    except Exception as e:
        logger.warning("Ошибка в remove_chat_buttons: %s", e)

# Обработчик команды /stop
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("stop вызван")
    try:
        chat_id = update.effective_chat.id
        await remove_chat_buttons(context.bot, chat_id)
    except Exception as e:
        logger.error("Ошибка в stop: %s", e)

# Inline обработчики
async def inline_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query or update
        # Пример использования utils.separate_callback_data
        (kind, _, _, _, _, _, _) = utils.separate_callback_data(query.data)
        await inline_calendar_handler(update, context)
    except Exception as e:
        logger.error("Ошибка в inline_handler: %s", e)

async def inline_calendar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                    if collection is not None:
                        collection.delete_many({"message_id": user_states[user_id]["data_message_id"]})
                    try:
                        await bot.send_message(
                            chat_id=user_states[user_id]["data_chat_id"],
                            text=messages.calendar_cancelled,
                        )
                    except Exception as e:
                        logger.warning("Ошибка отправки уведомления CANCEL: %s", e)
                    user_states[user_id] = {}
                    return
                text = (
                    user_states[user_id]["bot_message_text"].split(" ::")[0]
                    + " ::"
                    + date.strftime("%d-%m-%Y %H:%M")
                )
                keyboard = [[
                    InlineKeyboardButton("✔️ Выполнить", callback_data="done"),
                    InlineKeyboardButton("📅 Напомнить", callback_data="date"),
                    InlineKeyboardButton("❌ Удалить", callback_data="del"),
                ]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                if text and text.strip() and text != messages.calendar_message:
                    await context.bot.edit_message_text(
                        chat_id=user_states[user_id]["data_chat_id"],
                        message_id=user_states[user_id]["bot_message_id"],
                        text=text,
                        reply_markup=reply_markup,
                    )
                    if collection is not None:
                        if collection.count_documents({"message_id": user_states[user_id]["bot_message_id"]}) == 0:
                            today = " :" + datetime.datetime.today().strftime("%d-%m-%Y %H:%M")
                            collection.insert_one({
                                "chat_id": user_states[user_id]["data_chat_id"],
                                "message_id": user_states[user_id]["bot_message_id"],
                                "message": user_states[user_id]["bot_message_text"].split(" ::")[0],
                                "caption": user_states[user_id]["bot_message_caption"],
                                "date": date.strftime("%d-%m-%Y %H:%M"),
                                "today": today,
                            })
                        else:
                            collection.find_one_and_update(
                                {"message_id": user_states[user_id]["bot_message_id"]},
                                {"$set": {"date": date.strftime("%d-%m-%Y %H:%M")}},
                            )
            except Exception as e:
                logger.error("Ошибка в inline_calendar_handler: %s", e)
            finally:
                user_states[user_id] = {}
        else:
            user_states[user_id]["timeout_task"] = asyncio.create_task(
                calendar_timeout(bot, query.message.chat.id, query.message.message_id, user_id)
            )
    except Exception as e:
        logger.error("Ошибка в inline_calendar_handler (общая): %s", e)

# Фоновая задача для обработки напоминаний
async def pop_task():
    while True:
        await asyncio.sleep(60)
        try:
            if collection is not None:
                cur = collection.find()
                for rec in cur:
                    if rec.get("date"):
                        date_obj = datetime.datetime.strptime(rec["date"], "%d-%m-%Y %H:%M")
                        now_str = datetime.datetime.now().strftime("%d-%m-%Y %H:%M")
                        if date_obj.strftime("%d-%m-%Y %H:%M") == now_str:
                            new_rec = await tg_app.bot.copy_message(
                                chat_id=rec["chat_id"],
                                from_chat_id=rec["chat_id"],
                                message_id=rec["message_id"],
                            )
                            keyboard = [[
                                InlineKeyboardButton("✔️ Выполнить", callback_data="done"),
                                InlineKeyboardButton("📅 Напомнить", callback_data="date"),
                                InlineKeyboardButton("❌ Удалить", callback_data="del"),
                            ]]
                            reply_markup = InlineKeyboardMarkup(keyboard)
                            await tg_app.bot.edit_message_reply_markup(
                                chat_id=rec["chat_id"],
                                message_id=new_rec.message_id,
                                reply_markup=reply_markup,
                            )
                            try:
                                await tg_app.bot.delete_message(
                                    chat_id=rec["chat_id"], message_id=rec["message_id"]
                                )
                            except Exception as e:
                                logger.warning("Ошибка удаления старого сообщения в pop_task: %s", e)
                            collection.find_one_and_update(
                                {"message_id": rec["message_id"]},
                                {"$set": {
                                    "message_id": new_rec.message_id,
                                    "date": (datetime.datetime.now() + relativedelta(minutes=10)).strftime("%d-%m-%Y %H:%M"),
                                }},
                            )
                        elif date_obj < datetime.datetime.now():
                            collection.delete_many({"message_id": rec["message_id"]})
        except Exception as e:
            logger.error("Ошибка в pop_task: %s", e)

# Основная функция
async def main() -> None:
    global tg_app
    logger.info("Запуск main")
    tg_app = Application.builder().token(TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("stop", stop))
    tg_app.add_handler(CallbackQueryHandler(button))
    tg_app.add_handler(CommandHandler("help", help_command))
    tg_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_datepicker_input))
    async with tg_app:
        await tg_app.updater.start_polling()
        await tg_app.start()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    tasks = asyncio.wait([pop_task(), main()])
    loop.run_until_complete(tasks)
