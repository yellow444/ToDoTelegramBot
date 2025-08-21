#!/usr/bin/env python3
#
# A library that allows to create an inline calendar keyboard.
# grcanosa https://github.com/grcanosa
#
"""
Base methods for calendar keyboard creation and processing.
"""

import calendar
import datetime

import dateutil.relativedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import messages
import utils


def create_callback_data(action, year, month, day, curHour, curMin):
    """Create the callback data associated to each button"""
    return (
        messages.CALENDAR_CALLBACK
        + ";"
        + ";".join([action, str(year), str(month), str(day), str(curHour), str(curMin)])
    )


def create_calendar(cur=None):
    """
    Create an inline keyboard with the provided year and month
    """
    now = datetime.datetime.now()
    if cur == None:
        cur = now
    data_ignore = create_callback_data(
        "IGNORE", cur.year, cur.month, 0, cur.hour, cur.minute
    )
    keyboard = []
    row = []
    row.append(
        InlineKeyboardButton(
            "<",
            callback_data=create_callback_data(
                "PREV-YEAR", cur.year, cur.month, cur.day, cur.hour, cur.minute
            ),
        )
    )
    row.append(InlineKeyboardButton(f"YEAR:{cur.year}", callback_data=data_ignore))
    row.append(
        InlineKeyboardButton(
            ">",
            callback_data=create_callback_data(
                "NEXT-YEAR", cur.year, cur.month, cur.day, cur.hour, cur.minute
            ),
        )
    )
    keyboard.append(row)

    # Second row - Week Days
    row = []
    for day in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]:
        row.append(InlineKeyboardButton(day, callback_data=data_ignore))
    keyboard.append(row)

    my_calendar = calendar.monthcalendar(cur.year, cur.month)
    for week in my_calendar:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data=data_ignore))
            else:
                row.append(
                    InlineKeyboardButton(
                        str(day),
                        callback_data=create_callback_data(
                            "DAY", cur.year, cur.month, day, cur.hour, cur.minute
                        ),
                    )
                )
        keyboard.append(row)
    # row - Buttons
    row = []
    row.append(
        InlineKeyboardButton(
            "<",
            callback_data=create_callback_data(
                "PREV-MONTH", cur.year, cur.month, cur.day, cur.hour, cur.minute
            ),
        )
    )
    row.append(
        InlineKeyboardButton(
            f"{calendar.month_name[cur.month]}", callback_data=data_ignore
        )
    )
    row.append(
        InlineKeyboardButton(
            ">",
            callback_data=create_callback_data(
                "NEXT-MONTH", cur.year, cur.month, cur.day, cur.hour, cur.minute
            ),
        )
    )
    keyboard.append(row)
    row = []
    row.append(
        InlineKeyboardButton(
            "<",
            callback_data=create_callback_data(
                "PREV-HOUR", cur.year, cur.month, cur.day, cur.hour, cur.minute
            ),
        )
    )
    row.append(InlineKeyboardButton(f"HOUR:{cur.hour}", callback_data=data_ignore))
    row.append(
        InlineKeyboardButton(
            ">",
            callback_data=create_callback_data(
                "NEXT-HOUR", cur.year, cur.month, cur.day, cur.hour, cur.minute
            ),
        )
    )
    keyboard.append(row)
    row = []
    row.append(
        InlineKeyboardButton(
            "<",
            callback_data=create_callback_data(
                "PREV-MIN", cur.year, cur.month, cur.day, cur.hour, cur.minute
            ),
        )
    )
    row.append(InlineKeyboardButton(f"MIN:{cur.minute}", callback_data=data_ignore))
    row.append(
        InlineKeyboardButton(
            ">",
            callback_data=create_callback_data(
                "NEXT-MIN", cur.year, cur.month, cur.day, cur.hour, cur.minute
            ),
        )
    )
    keyboard.append(row)
    row = []
    row.append(
        InlineKeyboardButton(
            "CANCEL",
            callback_data=create_callback_data(
                "CANCEL", cur.year, cur.month, cur.day, cur.hour, cur.minute
            ),
        )
    )
    keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


async def _safe_edit_calendar(query, context, new_date):
    try:
        await context.bot.edit_message_text(
            text=query.message.text,
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            reply_markup=create_calendar(new_date),
        )
    except Exception:
        await context.bot.answer_callback_query(
            callback_query_id=query.id,
            text="Не удалось обновить календарь. Попробуйте ещё раз.",
            show_alert=True,
        )


async def process_calendar_selection(update, context):
    """
    Process the callback_query for the calendar.
    """
    ret_data = (False, None)
    query = update.callback_query
    await query.answer()
    (_, action, curYear, curMonth, curDay, curHour, curMin) = (
        utils.separate_callback_data(query.data)
    )
    curr = datetime.datetime(
        year=int(curYear),
        month=int(curMonth),
        day=int(curDay),
        hour=int(curHour),
        minute=int(curMin),
    )
    if action == "IGNORE":
        pass
    elif action == "DAY":
        try:
            await context.bot.edit_message_text(
                text=query.message.text,
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
            )
            ret_data = (True, curr)
        except Exception:
            await context.bot.answer_callback_query(
                callback_query_id=query.id,
                text="Не удалось обновить календарь. Попробуйте ещё раз.",
                show_alert=True,
            )
    elif action == "PREV-MONTH":
        pre = curr - dateutil.relativedelta.relativedelta(months=1)
        await _safe_edit_calendar(query, context, pre)
    elif action == "NEXT-MONTH":
        ne = curr + dateutil.relativedelta.relativedelta(months=1)
        await _safe_edit_calendar(query, context, ne)
    elif action == "PREV-HOUR":
        pre = curr - dateutil.relativedelta.relativedelta(hours=1)
        await _safe_edit_calendar(query, context, pre)
    elif action == "NEXT-HOUR":
        ne = curr + dateutil.relativedelta.relativedelta(hours=1)
        await _safe_edit_calendar(query, context, ne)
    elif action == "PREV-MIN":
        pre = curr - dateutil.relativedelta.relativedelta(minutes=1)
        await _safe_edit_calendar(query, context, pre)
    elif action == "NEXT-MIN":
        ne = curr + dateutil.relativedelta.relativedelta(minutes=1)
        await _safe_edit_calendar(query, context, ne)
    elif action == "PREV-YEAR":
        pre = curr - dateutil.relativedelta.relativedelta(years=1)
        await _safe_edit_calendar(query, context, pre)
    elif action == "NEXT-YEAR":
        ne = curr + dateutil.relativedelta.relativedelta(years=1)
        await _safe_edit_calendar(query, context, ne)
    elif action == "CANCEL":
        ret_data = (True, "CANCEL")
    else:
        await context.bot.send_message(
            chat_id=query.message.chat.id, text="Something went wrong!"
        )
    return ret_data
