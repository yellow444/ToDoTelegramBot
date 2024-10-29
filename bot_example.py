#!/usr/bin/python3
import asyncio
import logging
import os
from pathlib import Path

import uvicorn
from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, Request
from telegram import ReplyKeyboardRemove
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

import messages
import telegramcalendar
import utils

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
load_dotenv(find_dotenv())
# Go to botfather and create a bot and copy the token and paste it here in token
# TOKEN = ""  # token of the bot
TOKEN = os.getenv("BOT_TOKEN")
HOSTNAME = os.getenv("MYHOSTNAME")
PORT = 80
tg_app = None
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

logger = logging.getLogger(__name__)


def start(update, context):
    context.bot.send_message(
        chat_id=update.message.chat_id,
        text=messages.start_message.format(update.message.from_user.first_name),
        # parse_mode=ParseMode.HTML,
    )


# A simple command to display the calender
async def calendar_handler(update, context):
    await update.message.reply_text(
        text=messages.calendar_message, reply_markup=telegramcalendar.create_calendar()
    )


# def jcalendar_handler(update: Update, context: CallbackContext) -> int:
#     update.message.reply_text(
#         text=messages.jcalendar_message,
#         reply_markup=telegramjcalendar.create_calendar(),
#     )


async def inline_handler(update, context):
    query = update.callback_query
    (kind, _, _, _, _) = utils.separate_callback_data(query.data)
    # if kind == messages.CALENDAR_CALLBACK:
    await inline_calendar_handler(update, context)
    # elif kind == messages.JCALENDAR_CALLBACK:
    #     inline_jcalendar_handler(update, context)


async def inline_calendar_handler(update, context):
    selected, date = await telegramcalendar.process_calendar_selection(update, context)
    if selected:
        await context.bot.send_message(
            chat_id=update.callback_query.from_user.id,
            text=messages.calendar_response_message % (date.strftime("%d/%m/%Y")),
            reply_markup=ReplyKeyboardRemove(),
        )


# def inline_jcalendar_handler(update: Update, context: CallbackContext):
#     selected, date = telegramjcalendar.process_calendar_selection(context.bot, update)
#     if selected:
#         context.bot.send_message(
#             chat_id=update.callback_query.from_user.id,
#             text=messages.jcalendar_response_message % date,
#             reply_markup=ReplyKeyboardRemove(),
#         )


# if TOKEN == "":
#     print("Please write TOKEN into file")
# else:
#     updater = Updater(TOKEN, use_context=True)
#     dp = updater.dispatcher

#     dp.add_handler(CommandHandler("start", start))
#     dp.add_handler(CommandHandler("calendar", calendar_handler))
#     # dp.add_handler(CommandHandler("jcalendar", jcalendar_handler))
#     dp.add_handler(CallbackQueryHandler(inline_handler))

#     updater.start_polling()
#     updater.idle()
app = FastAPI()


@app.get("/")
async def web_html(request: Request):
    print("web_html")


async def main():
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

    # if not TOKEN:
    #     await server.serve()
    #     return
    tg_app = Application.builder().token(TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("calendar", calendar_handler))
    # dp.add_handler(CommandHandler("jcalendar", jcalendar_handler))
    tg_app.add_handler(CallbackQueryHandler(inline_handler))

    async with tg_app:
        await tg_app.updater.start_polling()
        await tg_app.start()
        await server.serve()
        await tg_app.updater.stop()
        await tg_app.stop()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    cors = asyncio.wait([main()])
    loop.run_until_complete(cors)
