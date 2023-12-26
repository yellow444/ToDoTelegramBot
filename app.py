#!/home/hoppingturtles/.pyenv/shims/python
import asyncio
import calendar
import datetime
import json
import logging
import os
import traceback
from pathlib import Path
from urllib.parse import quote

import uvicorn
from dateutil.relativedelta import *
from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pymongo import MongoClient
from starlette.routing import Mount
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
                      Update, WebAppInfo)
from telegram.ext import (Application, CallbackContext, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler,
                          Updater, filters)

# Provide the connection details
hostname = os.getenv('mongodb')
# hostname = '127.0.0.1'
port = 27017  # Default MongoDB port
username = os.getenv('root')  # If authentication is required
password = os.getenv('password123')  # If authentication is required

# Create a MongoClient instance
client = MongoClient(hostname, port, username=username, password=password)
db = client[os.getenv('teleg')]
collection = db[os.getenv('msg')]
tg_app = None
# BOT_TOKEN = '6531252215:AAFu4lIS43TwjEDJ7Y65EIrUt3PNKqLtiCw'
data_chat_id = None
data_message_id = None
bot_chat_id = None
bot_message_id = None
bot_message_text = None
bot_message_caption = None
all_keyboards = []
user_states = {}
g_months = {
        1:"January", 2:"February", 3:"March", 4:"April",
        5:"May", 6:"June", 7:"July", 8:"August",
        9:"September", 10:"October", 11:"November", 12:"December"
    }
cur_date = datetime.datetime.now()
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

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    global bot_message_id, bot_message_text,bot_message_caption
    # try:
    # print('button')
    query = update.callback_query
    bot_message_id = query.message.message_id
    bot_message_text = query.message.text
    bot_message_caption = query.message.caption
    # print(HOSTNAME)
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
        # options = {"range": False, "timepicker": True, "locale": "ru", "message": query.message.message_id}
        # url = f"{HOSTNAME}/calendar?options=" + quote(json.dumps(options))  # url encoded JSON string
        # print(url)
        # but = InlineKeyboardButton("Pick a date", web_app=WebAppInfo(url))
        # await bot.sendMessage(chat_id=query.message.chat_id,text = "Choose a date range", reply_markup=ReplyKeyboardMarkup.from_button(but))
        
        # if source.startswith('<del>') and source.endswith('</del>'):
        #     source = source[5:-6]
        # text = None if not source else source[1:] if source.startswith('✅') else source
        # keyboard = [
        #     [InlineKeyboardButton("✔️ Выполнить", callback_data="done" ),InlineKeyboardButton("📅 Напомнить", callback_data="date" ), InlineKeyboardButton("❌ Удалить", callback_data="del")]
        # ]
        # reply_markup = InlineKeyboardMarkup(keyboard)
        await datepicker(update,context)  
        return      
    if query.data in 'del':
        collection.delete_many({"message_id": query.message.message_id})
        await bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)        
        return
        # await query.message.delete()        
    if query.data in 'done':
        text = f'✅ {source}' if source else '✅'
        keyboard = [
            [InlineKeyboardButton("Выполнено", callback_data="undone"),InlineKeyboardButton("📅 Напомнить", callback_data="date" ), InlineKeyboardButton( "❌ Удалить", callback_data="del")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard) 
        collection.delete_many({"message_id": query.message.message_id})     
    else:        
        if source.startswith('<del>') and source.endswith('</del>'):
            source = source[5:-6]
        text = None if not source else source[1:] if source.startswith('✅') else source
        keyboard = [
            [InlineKeyboardButton("✔️ Выполнить", callback_data="done" ),InlineKeyboardButton("📅 Напомнить", callback_data="date" ), InlineKeyboardButton("❌ Удалить", callback_data="del")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
    # await query.message.reply_text(text, reply_markup=reply_markup)
    # try:
    if text != query.message.text:
        if query.message.text is not None:        
            await bot.editMessageText(chat_id=query.message.chat_id, message_id=query.message.message_id, text=text, reply_markup=reply_markup) 
        else:
            await bot.editMessageCaption(chat_id=query.message.chat_id, message_id=query.message.message_id, caption=text, reply_markup=reply_markup)
    # except Exception as err:
    #     print(traceback.format_exc())
    #     pass       
    # except:    
    #     pass
async def handle_datepicker_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:        
        global cur_date, data_chat_id, data_message_id,bot_message_id,bot_message_caption
        bot = update.get_bot()
        user_id = update.message.from_user.id
        if user_id not in user_states or user_states[user_id] != 'waiting_for_date':
            await echo(update,context)
            return
        date = datetime.datetime.now()
        selected_date = update.message.text
        if 'now' in selected_date:
            cur_date = datetime.datetime.now()
        elif 'select' in selected_date:
            date = cur_date
            # await update.message.reply_text(date.strftime('%d.%m.%Y'), reply_markup=ReplyKeyboardRemove())
            if db.mycollections.count_documents({"message_id": bot_message_id}) == 0:
                today = ' :' + datetime.datetime.today().strftime('%d-%m-%Y %H:%M:%S')
                collection.insert_one({
                    'chat_id': update.message.chat_id,
                    'message_id': bot_message_id,
                    'message': bot_message_text,
                    'caption': bot_message_caption,
                    'date': (date).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                    'today': today
                })
            else:
                collection.find_one_and_update(
            {"message_id": bot_message_id},
            {"$set": {"date": (date).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}})
            
            await context.bot.delete_message(update.message.chat_id, update.message._id_attrs[0])
            await context.bot.delete_message(data_chat_id, data_message_id) 
            text = None if not bot_message_text else bot_message_text[1:] if bot_message_text.startswith('✅') else bot_message_text
            keyboard = [
            [InlineKeyboardButton("✔️ Выполнить", callback_data="done" ),InlineKeyboardButton("📅 Напомнить", callback_data="date" ), InlineKeyboardButton("❌ Удалить", callback_data="del")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            if text != bot_message_text:
                await context.bot.editMessageText(chat_id=update.message.chat_id, message_id=bot_message_id, text = text, reply_markup=reply_markup)
            del user_states[user_id] 
            # context.bot.delete_message(bot_chat_id, bot_message_id)
            return
        elif selected_date == 'cancel': 
            msg = await update.message.reply_text("cancel", reply_markup=ReplyKeyboardRemove())       
            await context.bot.delete_message(update.message.chat_id, update.message._id_attrs[0])
            await context.bot.delete_message(msg.chat_id, msg._id_attrs[0])
            await context.bot.delete_message(data_chat_id, data_message_id)        
            del user_states[user_id]        
            # context.bot.delete_message(bot_chat_id, bot_message_id)
            return
        elif 'day-' in selected_date:
            cur_date = cur_date + relativedelta(days=-1)
        elif 'day+' in selected_date:
            cur_date = cur_date + relativedelta(days=+1)
        elif 'year-' in selected_date:
            cur_date = cur_date + relativedelta(years=-1)
        elif 'year+' in selected_date:
            cur_date = cur_date + relativedelta(years=+1)
        elif 'month-' in selected_date:
            cur_date = cur_date + relativedelta(months=-1)
        elif 'month+' in selected_date:
            cur_date = cur_date + relativedelta(months=+1)
        elif 'hour-' in selected_date:
            cur_date = cur_date + relativedelta(hours=-1)
        elif 'hour+' in selected_date:
            cur_date = cur_date + relativedelta(hours=+1)
        elif  'min-' in selected_date:
            cur_date = cur_date + relativedelta(minutes=-1)
        elif 'min+' in selected_date:
            cur_date = cur_date + relativedelta(minutes=+1)
        else:
            await echo(update,context)
        await context.bot.delete_message(update.message.chat_id, update.message.id)
        await context.bot.delete_message(data_chat_id, data_message_id)
        await _datepicker(update, context) 
    except Exception as e:
        await echo(update,context)
        pass 
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays info on how to use the bot."""
    print('help_command')    
    await update.callback_query.message.reply_text("Use /start to test this bot.")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Echo the user message."""
    # try:
    # print('echo')
    options = {"range": False, "timepicker": True, "locale": "ru"}
    bot = update.get_bot()
    url = f"{HOSTNAME}/calendar?options=" + quote(json.dumps(options))
    first = None
    second = None
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
    today = ' :' + datetime.datetime.today().strftime('%d-%m-%Y %H:%M:%S')
    if update.message.video is not None:
        second = await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
    elif update.message.audio is not None:
       second = await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
    # elif update.message.photo is not None:
    #     first = await update.message.reply_photo(update.message.photo)
    elif update.message.document is not None:
        second = await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)   
    elif update.message.text is not None:
        second = await bot.editMessageText(chat_id=first.chat_id, message_id=first.message_id, text=first.text + today, reply_markup=reply_markup)   
    elif update.message.sticker is not None:
        second = await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
    elif update.message.voice is not None:
        second = await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
    elif update.message.location is not None:
        second = await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
    elif update.message.contact is not None:
        second = await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
    elif update.message.venue is not None:
        second = await bot.editMessageCaption(chat_id=first.chat_id, message_id=first.message_id, caption=first.caption + today, reply_markup=reply_markup)  
    # collection.insert_one({
    #     'chat_id': first.chat_id,
    #     'message_id': first.message_id,
    #     'message': first.text,
    #     'caption': first.caption,
    #     'date': None,
    #     'today': today
    # })
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
#     await update.callback_query.message.reply_text(
#         "Choose a date range", reply_markup=ReplyKeyboardMarkup.from_button(but)
#     )
async def datepicker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global cur_date
    bot = update.get_bot()
    user_id =  None
    try:
        # await update.callback_query.message.delete()
        user_id = update.callback_query.message.from_user.id
    except Exception as e:
        pass    
    if user_id in user_states and user_states[user_id] == 'waiting_for_date':
        await _datepicker(update, context)
        return handle_datepicker_input(update, context)      
    cur_date = datetime.datetime.now()
    await _datepicker(update, context)
def generate_datepicker_keyboard(date:datetime.datetime) -> list:
    global g_months,all_keyboards
    all_keyboards = all_keyboards.clear()
    years_row = [KeyboardButton(f"year-"),KeyboardButton("cancel"), KeyboardButton(f"year+")]
    month_row =  [KeyboardButton(f"month-"),KeyboardButton(f"day-"),KeyboardButton(f"now"), KeyboardButton(f"day+"), KeyboardButton(f"month+")]
    # day_row =  [KeyboardButton(f"day {(date + relativedelta(days=-1)).strftime('%d.%m.%Y %H:%M')}"),KeyboardButton(f"reset_today:{datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}"), KeyboardButton(f"{(date + relativedelta(days=+1)).strftime('%d.%m.%Y %H:%M')} day")]
    hour_row =  [KeyboardButton(f"hour-"),KeyboardButton(f"min-"), KeyboardButton(f"min+"), KeyboardButton(f"hour+")]
    select_row =  [KeyboardButton(f"select:{date.strftime('%d.%m.%Y %H:%M')}")]
    # min_row =  [KeyboardButton(f"min {(date + relativedelta(days=-1)).strftime('%d.%m.%Y %H:%M')}"),KeyboardButton(f"cur_day:{date.strftime('%d.%m.%Y %H:%M')}"), KeyboardButton(f"{(date + relativedelta(days=+1)).strftime('%d.%m.%Y %H:%M')} min")]
    keyboard = []
    keyboard.append(years_row)
    keyboard.append(month_row)
    # keyboard.append(day_row)
    keyboard.append(hour_row)
    # keyboard.append(min_row)
    keyboard.append(select_row)
    all_keyboards = keyboard
    return keyboard
async def _datepicker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global  data_chat_id, data_message_id, cur_date,bot_chat_id,bot_message_id
    bot = update.get_bot()
    user_id =  None
    try:
        query = update if update.callback_query == None else update.callback_query
    except Exception as e:
        pass   
    user_id = query.from_user.id if query.message != None and query.message.from_user.id == bot.id  else query.message.from_user.id
    # bot_message_id = query.message.message_id if query.message != None and query.message.from_user.id == bot.id  else query.message.message_id
    date = cur_date
    keyboard = generate_datepicker_keyboard(date)
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    # bot.sendMessage
    msg = await bot.send_message(chat_id = query.message.chat_id,text = "datepicker",reply_markup=reply_markup)
    # msg = await update.callback_query.message.reply_text("Please select a date:", reply_markup=reply_markup)
    data_chat_id = msg.chat_id 
    data_message_id = msg._id_attrs[0]
    # bot_chat_id = update.callback_query.message.chat_id
    # bot_message_id = update.callback_query.message._id_attrs[0]
    user_states[user_id] = 'waiting_for_date'
async def received_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """This is the data received from the web app. It is a JSON string containing a list of dates,
    and it is timezone naive."""
    print('received_data')
    data = json.loads(update.message.web_app_data.data)
    message = data['message']
    dates = []  # Can be a range of dates if `options["range"] = True` was passed
    for date_str in data:
        # Convert the string to datetime object
        datetime_obj = datetime.datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        dates.append(datetime_obj)
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
    global tg_app
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
    # tg_app.add_handler(CommandHandler("datepicker", datepicker))
    tg_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_datepicker_input))
    # tg_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, echo))

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
            cur = collection.find()
            for rec in cur:
                if rec['date'] != None:
                    date = datetime.datetime.strptime(rec['date'], "%Y-%m-%dT%H:%M:%S.%fZ")
                    if date.strftime('%d.%m.%Y %H:%M') == datetime.datetime.now().strftime('%d.%m.%Y %H:%M'):
                        new_rec = await tg_app.bot.copyMessage(chat_id=rec['chat_id'], from_chat_id=rec['chat_id'], message_id=rec['message_id'])
                        keyboard = [
                            [InlineKeyboardButton("✔️ Выполнить", callback_data="done" ),InlineKeyboardButton("📅 Напомнить", callback_data="date" ), InlineKeyboardButton("❌ Удалить", callback_data="del")]
                        ]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        await tg_app.bot.edit_message_reply_markup(chat_id=rec['chat_id'], message_id=new_rec.message_id, reply_markup=reply_markup) 
                        await tg_app.bot.delete_message(chat_id=rec['chat_id'], message_id=rec['message_id'])
                        collection.find_one_and_update(
                        {"message_id": rec['message_id']},
                        {"$set": {"message_id": new_rec.message_id, 'date': (datetime.datetime.now() + relativedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}})                   
                    elif date < datetime.datetime.now():
                        collection.delete_many({"message_id": rec['message_id']})
        except Exception as e:
            pass


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    cors = asyncio.wait([ pop_task(),main()])
    loop.run_until_complete(cors)
    # asyncio.run(main())
