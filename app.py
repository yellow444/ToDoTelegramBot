import asyncio
import datetime
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

import uvicorn
from dateutil.relativedelta import relativedelta
from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, Request
from pymongo import MongoClient
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
                      Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

tg_app = None

data_chat_id = None
data_message_id = None
bot_chat_id = None
bot_message_id = None
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
cur_date = datetime.datetime.now()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

logger = logging.getLogger(__name__)

load_dotenv(find_dotenv())


app = FastAPI()

hostname = "mongodb"

port = 27017
username = "root"
password = "password123"


client = None
db = None
collection = None
try:
    client = MongoClient(
        hostname,
        port,
        username=username,
        password=password,
        serverSelectionTimeoutMS=3000,
    )
    client.server_info()
    db = client["teleg"]
    collection = db["msg"]
except Exception as e:
    print(e)
    client = None
TOKEN = os.getenv("BOT_TOKEN")
HOSTNAME = os.getenv("MYHOSTNAME")
PORT = 80


@app.get("/")
async def web_html(request: Request):
    print("web_html")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("start")
    """Send a message when the command /start is issued."""


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    global bot_message_id, bot_message_text, bot_message_caption

    query = update.callback_query
    bot_message_id = query.message.message_id
    bot_message_text = query.message.text
    bot_message_caption = query.message.caption

    bot = query.get_bot()
    if query.message.text is not None:
        source = query.message.text.replace("~~", "")
    else:
        source = query.message.caption.replace("~~", "")
    reply_markup = None
    text = None
    if query.data in "date":
        await datepicker(update, context)
        return
    if query.data in "del":
        if collection is not None:
            collection.delete_many({"message_id": query.message.message_id})
        await bot.delete_message(
            chat_id=query.message.chat_id, message_id=query.message.message_id
        )
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
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                text=text,
                reply_markup=reply_markup,
            )
        else:
            await bot.editMessageCaption(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                caption=text,
                reply_markup=reply_markup,
            )


async def handle_datepicker_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    try:
        global \
            cur_date, \
            data_chat_id, \
            data_message_id, \
            bot_message_id, \
            bot_message_caption
        bot = update.get_bot()
        user_id = update.message.from_user.id
        if user_id not in user_states or user_states[user_id] != "waiting_for_date":
            await echo(update, context)
            return
        date = datetime.datetime.now()
        selected_date = update.message.text
        if "now" in selected_date:
            cur_date = datetime.datetime.now()
        elif "select" in selected_date:
            date = cur_date
            if collection is not None:
                if (
                    db.mycollections.count_documents({"message_id": bot_message_id})
                    == 0
                ):
                    today = " :" + datetime.datetime.today().strftime(
                        "%d-%m-%Y %H:%M:%S"
                    )
                    collection.insert_one(
                        {
                            "chat_id": update.message.chat_id,
                            "message_id": bot_message_id,
                            "message": bot_message_text,
                            "caption": bot_message_caption,
                            "date": (date).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                            "today": today,
                        }
                    )
                else:
                    collection.find_one_and_update(
                        {"message_id": bot_message_id},
                        {"$set": {"date": (date).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}},
                    )

            await context.bot.delete_message(
                update.message.chat_id, update.message._id_attrs[0]
            )
            await context.bot.delete_message(data_chat_id, data_message_id)
            text = (
                None
                if not bot_message_text
                else bot_message_text[1:]
                if bot_message_text.startswith("✅")
                else bot_message_text
            )
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
                    chat_id=update.message.chat_id,
                    message_id=bot_message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
            del user_states[user_id]
            return
        elif selected_date == "cancel":
            msg = await update.message.reply_text(
                "cancel", reply_markup=ReplyKeyboardRemove()
            )
            await context.bot.delete_message(
                update.message.chat_id, update.message._id_attrs[0]
            )
            await context.bot.delete_message(msg.chat_id, msg._id_attrs[0])
            await context.bot.delete_message(data_chat_id, data_message_id)
            del user_states[user_id]
            return
        elif "day-" in selected_date:
            cur_date = cur_date + relativedelta(days=-1)
        elif "day+" in selected_date:
            cur_date = cur_date + relativedelta(days=+1)
        elif "year-" in selected_date:
            cur_date = cur_date + relativedelta(years=-1)
        elif "year+" in selected_date:
            cur_date = cur_date + relativedelta(years=+1)
        elif "month-" in selected_date:
            cur_date = cur_date + relativedelta(months=-1)
        elif "month+" in selected_date:
            cur_date = cur_date + relativedelta(months=+1)
        elif "hour-" in selected_date:
            cur_date = cur_date + relativedelta(hours=-1)
        elif "hour+" in selected_date:
            cur_date = cur_date + relativedelta(hours=+1)
        elif "min-" in selected_date:
            cur_date = cur_date + relativedelta(minutes=-1)
        elif "min+" in selected_date:
            cur_date = cur_date + relativedelta(minutes=+1)
        else:
            await echo(update, context)
        await context.bot.delete_message(
            update.message.chat_id, update.message.message_id
        )
        await context.bot.delete_message(data_chat_id, data_message_id)
        await _datepicker(update, context)
    except Exception as e:
        await echo(update, context)
        pass


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays info on how to use the bot."""
    print("help_command")
    await update.callback_query.message.reply_text("Use /start to test this bot.")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Echo the user message."""
    options = {"range": False, "timepicker": True, "locale": "ru"}
    bot = update.get_bot()
    url = f"{HOSTNAME}/calendar?options=" + quote(json.dumps(options))
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
    elif update.message.document is not None:
        first = await update.message.reply_document(
            document=update.message.document, caption=update.message.caption
        )
    elif update.message.text is not None:
        first = await update.message.reply_text(update.message.text)
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
        return
    keyboard = [
        [
            InlineKeyboardButton("✔️ Выполнить", callback_data="done"),
            InlineKeyboardButton("📅 Напомнить", callback_data="date"),
            InlineKeyboardButton("❌ Удалить", callback_data="del"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    today = " :" + datetime.datetime.today().strftime("%d-%m-%Y %H:%M:%S")
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
    elif update.message.document is not None:
        second = await bot.editMessageCaption(
            chat_id=first.chat_id,
            message_id=first.message_id,
            caption=first.caption + today,
            reply_markup=reply_markup,
        )
    elif update.message.text is not None:
        second = await bot.editMessageText(
            chat_id=first.chat_id,
            message_id=first.message_id,
            text=first.text + today,
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

    await update.message.delete()


async def datepicker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global cur_date
    bot = update.get_bot()
    user_id = None
    try:
        user_id = update.callback_query.message.from_user.id
    except Exception as e:
        pass
    if user_id in user_states and user_states[user_id] == "waiting_for_date":
        await _datepicker(update, context)
        return handle_datepicker_input(update, context)
    cur_date = datetime.datetime.now()
    await _datepicker(update, context)


def generate_datepicker_keyboard(date: datetime.datetime) -> list:
    global g_months, all_keyboards
    all_keyboards = all_keyboards.clear()
    years_row = [
        KeyboardButton(f"year-"),
        KeyboardButton("cancel"),
        KeyboardButton(f"year+"),
    ]
    month_row = [
        KeyboardButton(f"month-"),
        KeyboardButton(f"day-"),
        KeyboardButton(f"now"),
        KeyboardButton(f"day+"),
        KeyboardButton(f"month+"),
    ]
    hour_row = [
        KeyboardButton(f"hour-"),
        KeyboardButton(f"min-"),
        KeyboardButton(f"min+"),
        KeyboardButton(f"hour+"),
    ]
    select_row = [KeyboardButton(f"select:{date.strftime('%d.%m.%Y %H:%M')}")]
    keyboard = []
    keyboard.append(years_row)
    keyboard.append(month_row)
    keyboard.append(hour_row)
    keyboard.append(select_row)
    all_keyboards = keyboard
    return keyboard


async def _datepicker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global data_chat_id, data_message_id, cur_date, bot_chat_id, bot_message_id
    bot = update.get_bot()
    user_id = None
    try:
        query = update if update.callback_query == None else update.callback_query
    except Exception as e:
        pass
    user_id = (
        query.from_user.id
        if query.message != None and query.message.from_user.id == bot.id
        else query.message.from_user.id
    )

    date = cur_date
    keyboard = generate_datepicker_keyboard(date)
    reply_markup = ReplyKeyboardMarkup(
        keyboard, resize_keyboard=True, one_time_keyboard=True
    )

    msg = await bot.send_message(
        chat_id=query.message.chat_id, text="datepicker", reply_markup=reply_markup
    )

    data_chat_id = msg.chat_id
    data_message_id = msg._id_attrs[0]

    user_states[user_id] = "waiting_for_date"


async def received_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """This is the data received from the web app. It is a JSON string containing a list of dates,
    and it is timezone naive."""
    print("received_data")
    data = json.loads(update.message.web_app_data.data)
    message = data["message"]
    dates = []
    for date_str in data:
        datetime_obj = datetime.datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        dates.append(datetime_obj)
    print(dates)
    bot = update.get_bot()
    query = update.callback_query
    await bot.delete_message(
        chat_id=query.message.chat_id, message_id=query.message.message_id
    )
    reply_markup = query.message.reply_markup
    if query.message.text is not None:
        await bot.editMessageText(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            text=f"{query.message.text} Напомнить:{dates}",
            reply_markup=reply_markup,
        )
    else:
        await bot.editMessageCaption(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id,
            caption=f"{query.message.caption} Напомнить:{dates}",
            reply_markup=reply_markup,
        )


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


async def pop_task():
    while True:
        await asyncio.sleep(59)
        try:
            if collection is not None:
                cur = collection.find()
                for rec in cur:
                    if rec["date"] != None:
                        date = datetime.datetime.strptime(
                            rec["date"], "%Y-%m-%dT%H:%M:%S.%fZ"
                        )
                        if date.strftime(
                            "%d.%m.%Y %H:%M"
                        ) == datetime.datetime.now().strftime("%d.%m.%Y %H:%M"):
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
                            await tg_app.bot.delete_message(
                                chat_id=rec["chat_id"], message_id=rec["message_id"]
                            )
                            collection.find_one_and_update(
                                {"message_id": rec["message_id"]},
                                {
                                    "$set": {
                                        "message_id": new_rec.message_id,
                                        "date": (
                                            datetime.datetime.now()
                                            + relativedelta(minutes=10)
                                        ).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                                    }
                                },
                            )
                        elif date < datetime.datetime.now():
                            if collection is not None:
                                collection.delete_many(
                                    {"message_id": rec["message_id"]}
                                )
        except Exception as e:
            pass


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    cors = asyncio.wait([pop_task(), main()])
    loop.run_until_complete(cors)
