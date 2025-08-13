import asyncio
import datetime
import logging

from dateutil.relativedelta import relativedelta

from db import delete_reminders, fetch_reminders, update_reminder
import utils


logger = logging.getLogger(__name__)


async def pop_task(app):
    """Background task that sends reminders at the scheduled time."""

    while True:
        await asyncio.sleep(60)
        try:
            for rec in fetch_reminders():
                if rec.get("date"):
                    date_obj = datetime.datetime.strptime(
                        rec["date"], "%d-%m-%Y %H:%M"
                    )
                    now_str = datetime.datetime.now().strftime("%d-%m-%Y %H:%M")
                    if date_obj.strftime("%d-%m-%Y %H:%M") == now_str:
                        new_rec = await app.bot.copy_message(
                            chat_id=rec["chat_id"],
                            from_chat_id=rec["chat_id"],
                            message_id=rec["message_id"],
                        )
                        await app.bot.edit_message_reply_markup(
                            chat_id=rec["chat_id"],
                            message_id=new_rec.message_id,
                            reply_markup=utils.task_markup(),
                        )
                        try:
                            await app.bot.delete_message(
                                chat_id=rec["chat_id"], message_id=rec["message_id"]
                            )
                        except Exception as exc:  # pragma: no cover - logging only
                            logger.warning(
                                "Ошибка удаления старого сообщения в pop_task: %s", exc
                            )
                        update_reminder(
                            rec["message_id"],
                            {
                                "message_id": new_rec.message_id,
                                "date": (
                                    datetime.datetime.now()
                                    + relativedelta(minutes=10)
                                ).strftime("%d-%m-%Y %H:%M"),
                            },
                        )
                    elif date_obj < datetime.datetime.now():
                        delete_reminders({"message_id": rec["message_id"]})
        except Exception as exc:  # pragma: no cover - logging only
            logger.error("Ошибка в pop_task: %s", exc)

