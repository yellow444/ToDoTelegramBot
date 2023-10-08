#!/home/hoppingturtles/.pyenv/shims/python
import asyncio
import json
from datetime import datetime
import os  
from pathlib import Path
from urllib.parse import quote

import uvicorn
from dotenv import load_dotenv, find_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount
from telegram import  ReplyKeyboardMarkup, Update, WebAppInfo,KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import  Application, MessageHandler, CallbackQueryHandler, CommandHandler, ContextTypes, filters
import logging

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

logger = logging.getLogger(__name__)

load_dotenv(find_dotenv())

routes = [
    Mount("/calendar/static", StaticFiles(directory="./calendar/static"), name="static"),
    Mount("/calendar/dist", StaticFiles(directory="./calendar/dist"), name="dist"),
]

app = FastAPI(routes=routes)


TOKEN = os.getenv('BOT_TOKEN')  # bot token. Append /test to use test servers.
HOSTNAME = os.getenv('MYHOSTNAME')  # HTTP(S) URL for WebAppInfo
PORT = 80
# SSL_CERT = os.getenv('SSL_CERT')  # path to SSL certificate for https
# SSL_KEY = os.getenv('SSL_KEY')  # path to SSL key for https




@app.get("/calendar")
async def web_html(request: Request):
    print('web_html')
    with Path("calendar/static/datepicker.html").open() as f:
        return HTMLResponse(content=f.read())




async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print('start')
    """Send a message when the command /start is issued."""
    # await update.message.reply_text("Hi!")
    # """Sends a message with three inline buttons attached."""
    # keyboard = [
    #     [
    #         InlineKeyboardButton("Option 1", callback_data="1"),
    #         InlineKeyboardButton("Option 2", callback_data="2"),
    #     ],
    #     [InlineKeyboardButton("Option 3", callback_data="3")],
    # ]

    # reply_markup = InlineKeyboardMarkup(keyboard)

    # await update.message.reply_text("Please choose:", reply_markup=reply_markup)


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    # try:
    print('button')
    query = update.callback_query
    print(HOSTNAME)
    # options = {"range": False, "timepicker": True, "locale": "ru", "message": query.message.message_id}
    # url = f"{HOSTNAME}/calendar?options=" + quote(json.dumps(options))

    bot = query.get_bot()
    if query.message.text is not None:
        source = query.message.text.replace('~~', '')
    else:
        source = query.message.caption.replace('~~', '')
    reply_markup = None
    text = None
    if query.data in 'date':
        options = {"range": False, "timepicker": True, "locale": "ru", "message": query.message.message_id}
        url = f"{HOSTNAME}/calendar?options=" + quote(json.dumps(options))  # url encoded JSON string
        print(url)
        but = InlineKeyboardButton("Pick a date", web_app=WebAppInfo(url))
        await bot.sendMessage(chat_id=query.message.chat_id,text = "Choose a date range", reply_markup=ReplyKeyboardMarkup.from_button(but))
        return
    if query.data in 'del':
        await bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
        return
        # await query.message.delete()        
    if query.data in 'done':
        text = f'✅ {source}' if source else '✅'
        keyboard = [
            [InlineKeyboardButton("Выполнено", callback_data="undone"),InlineKeyboardButton("📅 Напомнить", callback_data="date" ), InlineKeyboardButton( "❌ Удалить", callback_data="del")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)        
    else:        
        if source.startswith('<del>') and source.endswith('</del>'):
            source = source[5:-6]
        text = None if not source else source[1:] if source.startswith('✅') else source
        keyboard = [
            [InlineKeyboardButton("✔️ Выполнить", callback_data="done" ),InlineKeyboardButton("📅 Напомнить", callback_data="date" ), InlineKeyboardButton("❌ Удалить", callback_data="del")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
    # await query.message.reply_text(text, reply_markup=reply_markup)

    if query.message.text is not None:
        await bot.editMessageText(chat_id=query.message.chat_id, message_id=query.message.message_id, text=text, reply_markup=reply_markup) 
    else:
        await bot.editMessageCaption(chat_id=query.message.chat_id, message_id=query.message.message_id, caption=text, reply_markup=reply_markup)


    
        
    # except:    
    #     pass


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays info on how to use the bot."""
    print('help_command')    
    await update.message.reply_text("Use /start to test this bot.")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Echo the user message."""
    # try:
    print('echo')
    options = {"range": False, "timepicker": True, "locale": "ru"}
    bot = update.get_bot()
    url = f"{HOSTNAME}/calendar?options=" + quote(json.dumps(options))
    first = None
    if update.message.video is not None:
        first = await update.message.reply_video(video=update.message.video, caption=update.message.caption)
    elif update.message.audio is not None:
        first = await update.message.reply_audio(audio=update.message.audio, caption=update.message.caption)
    # elif update.message.photo is not None:
    #     first = await update.message.reply_photo(update.message.photo)
    elif update.message.document is not None:
        first = await update.message.reply_document(document=update.message.document, caption=update.message.caption)
    elif update.message.text is not None:
        first = await update.message.reply_text(update.message.text)
    elif update.message.sticker is not None:
        first = await update.message.reply_sticker(sticker=update.message.sticker, caption=update.message.caption)
    elif update.message.voice is not None:
        first = await update.message.reply_voice(voice=update.message.voice, caption=update.message.caption)
    elif update.message.location is not None:
        first = await update.message.reply_location(location=update.message.location, caption=update.message.caption)
    elif update.message.contact is not None:
        first = await update.message.reply_contact(contact=update.message.contact, caption=update.message.caption)
    elif update.message.venue is not None:
        first = await update.message.reply_venue(venue=update.message.venue, caption=update.message.caption)
    else:
        return       
    # first = await update.message.reply_text(update.message.text)        
    keyboard = [
        [InlineKeyboardButton("✔️ Выполнить", callback_data="done" ),InlineKeyboardButton("📅 Напомнить", callback_data="date" ), InlineKeyboardButton("❌ Удалить", callback_data="del")]
        ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    today = ' ' + datetime.today().strftime('%d-%m-%Y %H:%M:%S')
    if update.message.video is not None:
        await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
    elif update.message.audio is not None:
       await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
    # elif update.message.photo is not None:
    #     first = await update.message.reply_photo(update.message.photo)
    elif update.message.document is not None:
        await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)   
    elif update.message.text is not None:
        await bot.editMessageText(chat_id=first.chat_id, message_id=first.message_id, text=first.text + today, reply_markup=reply_markup)   
    elif update.message.sticker is not None:
        await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
    elif update.message.voice is not None:
        await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
    elif update.message.location is not None:
        await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
    elif update.message.contact is not None:
        await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
    elif update.message.venue is not None:
        await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
        
    
         
    await update.message.delete()
    # await b.delete_message(chat_id=update.message.chat_id, message_id=update.message.message_id)
        
        
    # except:
    #     # Если бот не админ в чате, или если сообщение невозможно изменить (например, это стикер или сообщение через инлайн-бота)
    #     pass



# async def send_datepicker(message) -> None:
#     """Sends the web app as a KeyboardButton. We can customize the datepicker as well."""

#     # parameters to be passed to air-datepicker
#     options = {"range": False, "timepicker": True, "locale": "ru", "message": message}
#     url = f"{HOSTNAME}/calendar?options=" + quote(json.dumps(options))  # url encoded JSON string
#     but = KeyboardButton("Pick a date", web_app=WebAppInfo(url))
#     await update.message.reply_text(
#         "Choose a date range", reply_markup=ReplyKeyboardMarkup.from_button(but)
#     )


async def received_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """This is the data received from the web app. It is a JSON string containing a list of dates,
    and it is timezone naive."""
    print('received_data')
    data = json.loads(update.message.web_app_data.data)
    message = data['message']
    dates = []  # Can be a range of dates if `options["range"] = True` was passed
    for date_str in data:
        # Convert the string to datetime object
        datetime_obj = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        dates.append(datetime_obj)
    # await update.message.reply_text(f"received date(s):\n{dates}")
    print(dates)
    bot = update.get_bot()
    query = update.callback_query
    await bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)  
    reply_markup= query.message.reply_markup
    if query.message.text is not None:
        await bot.editMessageText(chat_id=query.message.chat_id, message_id=query.message.message_id, text=f"{query.message.text} Напомнить:{dates}" , reply_markup=reply_markup) 
    else:
        await bot.editMessageCaption(chat_id=query.message.chat_id, message_id=query.message.message_id, caption=f"{query.message.caption} Напомнить:{dates}" , reply_markup=reply_markup) 
 


async def main() -> None:
    print('main')
    server = uvicorn.Server(
        config=uvicorn.Config(
            f"{Path(__file__).stem}:app",
            port=PORT,
            host="0.0.0.0",
            reload=True,
            # ssl_certfile=SSL_CERT,
            # ssl_keyfile=SSL_KEY,
        )
    )

    if not TOKEN:  # If we're deploying this e.g. in Replit
        await server.serve()
        return

    # If we are testing locally, use PTB
    tg_app = Application.builder().token(TOKEN).build()

    # tg_app.add_handler(MessageHandler(filters.TEXT, send_datepicker))
    # tg_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, received_data))
    
 
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CallbackQueryHandler(button))
    tg_app.add_handler(CommandHandler("help", help_command))
    # tg_app.add_handler(MessageHandler(filters.TEXT, send_datepicker))
    # tg_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, received_data))
    # on non command i.e message - echo the message on Telegram
    tg_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, echo))
    # tg_app.run_polling(allowed_updates=Update.ALL_TYPES)


    async with tg_app:
        await tg_app.updater.start_polling()
        await tg_app.start()
        await server.serve()
        await tg_app.updater.stop()
        await tg_app.stop()


if __name__ == "__main__":
    asyncio.run(main())
