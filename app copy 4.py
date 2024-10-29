#!/usr/bin/python3
import asyncio
import datetime
import json
import logging
from pathlib import Path

import uvicorn
from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, Request
from pydantic import BaseModel, BaseSettings
from pymongo import MongoClient
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardRemove, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import messages
import telegramcalendar
import utils

tg_app = None

bot_message_text = None
bot_message_caption = None
all_keyboards = []
user_states = {}
g_months = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


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

app = FastAPI()

MONGO_HOST = config.MONGO_HOST
MONGO_PORT = config.MONGO_PORT
MONGO_USER = config.MONGO_USER
MONGO_PASS = config.MONGO_PASS
client = None
db = None
collection = None
try:
    client = MongoClient(
        MONGO_HOST,
        MONGO_PORT,
        username=MONGO_USER,
        password=MONGO_PASS,
        serverSelectionTimeoutMS=3000,
    )
    try:
        client.server_info()
    except Exception as e:
        print(e)
        client = MongoClient(
            "127.0.0.1",
            MONGO_PORT,
            username=MONGO_USER,
            password=MONGO_PASS,
            serverSelectionTimeoutMS=3000,
        )
        client.server_info()
    db = client[config.DB_NAME]
    collection = db[config.COLLECTION_NAME]
except Exception as e:
    print(e)
    client = None
TOKEN = config.TOKEN
HOSTNAME = config.MYHOSTNAME
PORT = 80


class HealthCheck(BaseModel):
    status: str = "OK"


@app.get("/")
async def web_html(request: Request):
    print("web_html")


@app.get("/hc")
async def hc(request: Request):
    print("hc")
    return HealthCheck(status="OK")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update if update.callback_query is None else update.callback_query
    bot = query.get_bot()
    chat_id = context._chat_id
    msg_text = r"_It is not the message you are looking for\.\.\._"
    await bot.send_message(
        chat_id,
        msg_text,
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="MarkdownV2",
    )
    print("start")


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global user_states
    query = update if update.callback_query is None else update.callback_query
    bot = update.get_bot()
    user_id = (
        query.from_user.id
        if query.message is not None and query.message.from_user.id == bot.id
        else query.message.from_user.id
    )
    bot_message_id = query.message._id_attrs[0]
    bot_message_text = query.message.text
    if user_id not in user_states:
        user_states[user_id] = {}

    user_states[user_id]["data_chat_id"] = query.message.chat.id
    user_states[user_id]["bot_message_caption"] = query.message.caption

    message_text = query.message.text if query.message else ""
    if message_text == messages.calendar_message or (
        user_id in user_states
        and "data_message_id" in user_states[user_id]
        and bot_message_id == user_states[user_id]["data_message_id"]
    ):
        await inline_handler(update, context)
        return
    # clear user_states[user_id]
    try:
        await context.bot.delete_message(
            user_states[user_id]["data_chat_id"],
            user_states[user_id]["data_message_id"],
        )
    except Exception as e:
        print(e)
        pass
    finally:
        user_states[user_id] = {}

    user_states[user_id]["bot_message_id"] = bot_message_id
    user_states[user_id]["bot_message_text"] = bot_message_text
    if query.message.text is not None:
        source = query.message.text.replace("~~", "")
    else:
        source = query.message.caption.replace("~~", "")
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
            print(e)
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
        text = None if not source else source[1:] if source.startswith("✅") else source
        keyboard = [
            [
                InlineKeyboardButton("✔️ Выполнить", callback_data="done"),
                InlineKeyboardButton("📅 Напомнить", callback_data="date"),
                InlineKeyboardButton("❌ Удалить", callback_data="del"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

    if text != query.message.text:
        if query.message.text is not None:
            await bot.editMessageText(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                text=text,
                reply_markup=reply_markup,
            )
        else:
            await bot.editMessageCaption(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                caption=text,
                reply_markup=reply_markup,
            )


async def selectDate(update, context, date):
    global user_states
    try:
        query = update if update.callback_query is None else update.callback_query
    except Exception as e:
        print(e)
        pass
    bot = update.get_bot()
    user_id = (
        query.from_user.id
        if query.message is not None and query.message.from_user.id == bot.id
        else query.message.from_user.id
    )
    bot_message_id = query.message._id_attrs[0]
    print("selected")
    _chat_id = query.message.chat.id
    if collection is not None:
        if db.mycollections.count_documents({"message_id": bot_message_id}) == 0:
            today = " :" + datetime.datetime.today().strftime("%d-%m-%Y %H:%M")
            collection.insert_one(
                {
                    "chat_id": _chat_id,
                    "message_id": bot_message_id,
                    "message": bot_message_text,
                    "caption": bot_message_caption,
                    "date": (user_states[user_id]["date"]).strftime("%d-%m-%Y %H:%M"),
                    "today": today,
                }
            )
        else:
            collection.find_one_and_update(
                {"message_id": bot_message_id},
                {
                    "$set": {
                        "date": (user_states[user_id]["date"]).strftime(
                            "%d-%m-%Y %H:%M"
                        )
                    }
                },
            )
    bot_message_text = user_states[user_id]["bot_message_text"]
    text = (
        None
        if not bot_message_text
        else bot_message_text[1:]
        if bot_message_text.startswith("✅")
        else bot_message_text
    )
    text = text.split(" ::")[0] + " ::" + (date.strftime("%d-%m-%Y %H:%M"))
    keyboard = [
        [
            InlineKeyboardButton("✔️ Выполнить", callback_data="done"),
            InlineKeyboardButton("📅 Напомнить", callback_data="date"),
            InlineKeyboardButton("❌ Удалить", callback_data="del"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if text != bot_message_text:
        await context.bot.editMessageText(
            chat_id=_chat_id,
            message_id=bot_message_id,
            text=text,
            reply_markup=reply_markup,
        )
    # del user_states[user_id]
    return


async def remove_calendar_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("")


async def handle_datepicker_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    try:
        global user_states
        query = update if update.callback_query is None else update.callback_query
        bot = update.get_bot()
        user_id = update.message.from_user.id
        text = update.message.text
        if text is None or text.isspace() or messages.calendar_message in text:
            try:
                await bot.delete_message(
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                )
            except Exception as e:
                print(e)
            finally:
                user_states[user_id] = {}
        else:
            await echo(update, context)

    except Exception as e:
        print(e)
        pass


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("help_command")
    await update.callback_query.message.reply_text("Use /start to test this bot.")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global user_states
    query = update if update.callback_query is None else update.callback_query
    bot = update.get_bot()
    user_id = (
        query.from_user.id
        if query.message is not None and query.message.from_user.id == bot.id
        else query.message.from_user.id
    )
    try:
        await context.bot.delete_message(
            user_states[user_id]["data_chat_id"],
            user_states[user_id]["data_message_id"],
        )
    except Exception as e:
        print(e)
        pass
    finally:
        user_states[user_id] = {}
    first = None
    second = None
    if update.message.video is not None:
        first = await update.message.reply_video(
            video=update.message.video, caption=update.message.caption
        )
    elif update.message.audio is not None:
        first = await update.message.reply_audio(
            audio=update.message.audio, caption=update.message.caption
        )
    # elif update.message.photo is not None:
    #     first = await update.message.reply_photo(update.message.photo)
    elif update.message.document is not None:
        first = await update.message.reply_document(
            document=update.message.document, caption=update.message.caption
        )
    elif update.message.sticker is not None:
        first = await update.message.reply_sticker(
            sticker=update.message.sticker, caption=update.message.caption
        )
    elif update.message.voice is not None:
        first = await update.message.reply_voice(
            voice=update.message.voice, caption=update.message.caption
        )
    elif update.message.location is not None:
        first = await update.message.reply_location(
            location=update.message.location, caption=update.message.caption
        )
    elif update.message.contact is not None:
        first = await update.message.reply_contact(
            contact=update.message.contact, caption=update.message.caption
        )
    elif update.message.venue is not None:
        first = await update.message.reply_venue(
            venue=update.message.venue, caption=update.message.caption
        )
    else:
        first = await update.message.reply_text(update.message.text)
    # first = await update.message.reply_text(update.message.text)
    keyboard = [
        [
            InlineKeyboardButton("✔️ Выполнить", callback_data="done"),
            InlineKeyboardButton("📅 Напомнить", callback_data="date"),
            InlineKeyboardButton("❌ Удалить", callback_data="del"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    today = " ::" + datetime.datetime.today().strftime("%d-%m-%Y %H:%M")
    if update.message.video is not None:
        second = await bot.editMessageCaption(
            chat_id=first.chat_id,
            message_id=first.message_id,
            caption=first.caption + today,
            reply_markup=reply_markup,
        )
    elif update.message.audio is not None:
        second = await bot.editMessageCaption(
            chat_id=first.chat_id,
            message_id=first.message_id,
            caption=first.caption + today,
            reply_markup=reply_markup,
        )
    # elif update.message.photo is not None:
    #     first = await update.message.reply_photo(update.message.photo)
    elif update.message.document is not None:
        second = await bot.editMessageCaption(
            chat_id=first.chat_id,
            message_id=first.message_id,
            caption=first.caption + today,
            reply_markup=reply_markup,
        )

    elif update.message.sticker is not None:
        second = await bot.editMessageCaption(
            chat_id=first.chat_id,
            message_id=first.message_id,
            caption=first.caption + today,
            reply_markup=reply_markup,
        )
    elif update.message.voice is not None:
        second = await bot.editMessageCaption(
            chat_id=first.chat_id,
            message_id=first.message_id,
            caption=first.caption + today,
            reply_markup=reply_markup,
        )
    elif update.message.location is not None:
        second = await bot.editMessageCaption(
            chat_id=first.chat_id,
            message_id=first.message_id,
            caption=first.caption + today,
            reply_markup=reply_markup,
        )
    elif update.message.contact is not None:
        second = await bot.editMessageCaption(
            chat_id=first.chat_id,
            message_id=first.message_id,
            caption=first.caption + today,
            reply_markup=reply_markup,
        )
    elif update.message.venue is not None:
        second = await bot.editMessageCaption(
            chat_id=first.chat_id,
            message_id=first.message_id,
            caption=first.caption + today,
            reply_markup=reply_markup,
        )
    else:
        second = await bot.editMessageText(
            chat_id=first.chat_id,
            message_id=first.message_id,
            text=first.text + today,
            reply_markup=reply_markup,
        )
    print(second)
    await update.message.delete()


async def calendar_handler(update, context):
    global user_states
    try:
        query = update if update.callback_query is None else update.callback_query
        bot = update.get_bot()
        user_id = (
            query.from_user.id
            if query.message is not None and query.message.from_user.id == bot.id
            else query.message.from_user.id
        )
        msg = await bot.send_message(
            chat_id=query.message.chat.id,
            text=messages.calendar_message,
            reply_markup=telegramcalendar.create_calendar(),
        )
        user_states[user_id]["data_chat_id"] = msg.chat_id
        user_states[user_id]["data_message_id"] = msg._id_attrs[0]
        user_states[user_id]["waiting_for_date"] = messages.calendar_message
    except Exception as e:
        print(e)
        pass


async def received_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("received_data")
    data = json.loads(update.message.web_app_data.data)
    dates = []
    for date_str in data:
        datetime_obj = datetime.datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        dates.append(datetime_obj)
    print(dates)
    bot = update.get_bot()
    query = update if update.callback_query is None else update.callback_query
    try:
        await bot.delete_message(
            chat_id=query.message.chat.id, message_id=query.message.message_id
        )
    except Exception as e:
        print(e)

    reply_markup = query.message.reply_markup
    if query.message.text is not None:
        await bot.editMessageText(
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            text=f"{query.message.text} Напомнить:{dates}",
            reply_markup=reply_markup,
        )
    else:
        await bot.editMessageCaption(
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            caption=f"{query.message.caption} Напомнить:{dates}",
            reply_markup=reply_markup,
        )


async def remove_chat_buttons(bot, chat_id):
    msg_text = r"_It is not the message you are looking for\.\.\._"
    msg = await bot.send_message(
        chat_id,
        msg_text,
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="MarkdownV2",
    )
    await msg.delete()


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("stop")
    query = update if update.callback_query is None else update.callback_query
    bot = query.get_bot()
    chat_id = context._chat_id
    await remove_chat_buttons(bot, chat_id)


async def inline_handler(update, context):
    query = update if update.callback_query is None else update.callback_query
    (kind, _, _, _, _, _, _) = utils.separate_callback_data(query.data)
    await inline_calendar_handler(update, context)


async def inline_calendar_handler(update, context):
    global user_states
    query = update if update.callback_query is None else update.callback_query
    bot = update.get_bot()
    user_id = (
        query.from_user.id
        if query.message is not None and query.message.from_user.id == bot.id
        else query.message.from_user.id
    )
    selected, date = await telegramcalendar.process_calendar_selection(update, context)
    if selected:
        user_states[user_id]["date"] = date

        print(date.strftime("%d-%m-%Y %H:%M"))
        # await selectDate(update, context, date)
        try:
            await context.bot.delete_message(
                user_states[user_id]["data_chat_id"],
                user_states[user_id]["data_message_id"],
            )

            text = (
                user_states[user_id]["bot_message_text"].split(" ::")[0]
                + " ::"
                + (date.strftime("%d-%m-%Y %H:%M"))
            )
            keyboard = [
                [
                    InlineKeyboardButton("✔️ Выполнить", callback_data="done"),
                    InlineKeyboardButton("📅 Напомнить", callback_data="date"),
                    InlineKeyboardButton("❌ Удалить", callback_data="del"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            if (
                text is not None
                and not text.isspace()
                and text != messages.calendar_message
            ):
                await context.bot.editMessageText(
                    chat_id=user_states[user_id]["data_chat_id"],
                    message_id=user_states[user_id]["bot_message_id"],
                    text=text,
                    reply_markup=reply_markup,
                )
        except Exception as e:
            print(e)
            pass
        finally:
            user_states[user_id] = {}


async def pop_task():
    global user_states
    while True:
        await asyncio.sleep(59)
        try:
            if collection is not None:
                cur = collection.find()
                for rec in cur:
                    if rec["date"] is not None:
                        date = datetime.datetime.strptime(
                            rec["date"], "%Y-%m-%dT%H:%M:%S.%fZ"
                        )
                        if date.strftime(
                            "%d-%m-%Y %H:%M"
                        ) == datetime.datetime.now().strftime("%d-%m-%Y %H:%M"):
                            new_rec = await tg_app.bot.copyMessage(
                                chat_id=rec["chat_id"],
                                from_chat_id=rec["chat_id"],
                                message_id=rec["message_id"],
                            )
                            keyboard = [
                                [
                                    InlineKeyboardButton(
                                        "✔️ Выполнить", callback_data="done"
                                    ),
                                    InlineKeyboardButton(
                                        "📅 Напомнить", callback_data="date"
                                    ),
                                    InlineKeyboardButton(
                                        "❌ Удалить", callback_data="del"
                                    ),
                                ]
                            ]
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
                                print(e)

                            collection.find_one_and_update(
                                {"message_id": rec["message_id"]},
                                {
                                    "$set": {
                                        "message_id": new_rec.message_id,
                                        "date": (
                                            datetime.datetime.now()
                                            + relativedelta(minutes=10)
                                        ).strftime("%d-%m-%Y %H:%M"),
                                    }
                                },
                            )
                        elif date < datetime.datetime.now():
                            if collection is not None:
                                collection.delete_many(
                                    {"message_id": rec["message_id"]}
                                )
        except Exception as e:
            print(e)
            pass


async def main() -> None:
    global tg_app
    print("main")
    server = uvicorn.Server(
        config=uvicorn.Config(
            f"{Path(__file__).stem}:app",
            port=PORT,
            host="0.0.0.0",
            reload=True,
        )
    )
    if not TOKEN:
        await server.serve()
        return
    tg_app = Application.builder().token(TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("stop", stop))
    tg_app.add_handler(CallbackQueryHandler(button))
    tg_app.add_handler(CommandHandler("help", help_command))

    tg_app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_datepicker_input)
    )
    async with tg_app:
        await tg_app.updater.start_polling()
        await tg_app.start()
        await server.serve()
        await tg_app.updater.stop()
        await tg_app.stop()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    cors = asyncio.wait([pop_task(), main()])
    loop.run_until_complete(cors)
