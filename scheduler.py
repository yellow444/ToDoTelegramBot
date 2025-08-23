import datetime
import logging
from typing import Optional

from dateutil.relativedelta import relativedelta
from telegram.ext import ContextTypes

from db import delete_reminders, fetch_reminders, update_reminder
import utils

logger = logging.getLogger(__name__)

# Сколько секунд считаем «попаданием» во время
TRIGGER_WINDOW_SECONDS = 59


def _is_due(date_obj: datetime.datetime, now: datetime.datetime) -> bool:
    """Напоминание «пора» — если now в пределах [date, date+window]."""
    if now < date_obj:
        return False
    return (now - date_obj).total_seconds() <= TRIGGER_WINDOW_SECONDS


async def _trigger_reminder(context: ContextTypes.DEFAULT_TYPE, rec: dict, now: datetime.datetime) -> Optional[int]:
    """
    Триггерим напоминание: копируем исходное сообщение, вешаем клавиатуру,
    удаляем старое, переносим дату на +10 минут. Возвращаем новый message_id.
    """
    bot = context.application.bot
    try:
        new_msg = await bot.copy_message(
            chat_id=rec["chat_id"],
            from_chat_id=rec["chat_id"],
            message_id=rec["message_id"],
        )
        await bot.edit_message_reply_markup(
            chat_id=rec["chat_id"],
            message_id=new_msg.message_id,
            reply_markup=utils.task_markup(),
        )
        try:
            await bot.delete_message(chat_id=rec["chat_id"], message_id=rec["message_id"])
        except Exception as exc:
            logger.warning("Не удалось удалить старое сообщение %s: %s", rec["message_id"], exc)

        # Переносим дату на +10 минут — чтобы не дёргать пользователя чаще, чем нужно
        update_reminder(
            rec["message_id"],
            {
                "message_id": new_msg.message_id,
                "date": (now + relativedelta(minutes=10)).strftime("%d-%m-%Y %H:%M"),
            },
        )
        return new_msg.message_id
    except Exception as exc:
        logger.error("Ошибка триггера напоминания для msg_id=%s: %s", rec.get("message_id"), exc)
        return None


async def pop_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодически проверяет расписание и триггерит напоминания."""
    now = datetime.datetime.now()
    try:
        records = fetch_reminders()
        logger.debug("pop_job: всего записей=%d, now=%s", len(records), now.strftime("%d-%m-%Y %H:%M:%S"))
        for rec in records:
            date_str = rec.get("date")
            msg_id = rec.get("message_id")
            chat_id = rec.get("chat_id")

            if not date_str or not msg_id or not chat_id:
                logger.warning("Пропускаю битую запись: %r", rec)
                continue

            try:
                date_obj = datetime.datetime.strptime(date_str, "%d-%m-%Y %H:%M")
            except Exception as exc:
                logger.warning("Некорректная дата '%s' у записи %r: %s", date_str, rec, exc)
                continue

            if _is_due(date_obj, now):
                logger.info("TRIGGER: chat_id=%s msg_id=%s date=%s now=%s",
                            chat_id, msg_id, date_obj, now)
                new_id = await _trigger_reminder(context, rec, now)
                if new_id is None:
                    # Если не получилось — чтобы не зависнуть навечно, отложим ещё на минуту
                    update_reminder(msg_id, {"date": (now + relativedelta(minutes=1)).strftime("%d-%m-%Y %H:%M")})
            elif date_obj < (now - relativedelta(days=1)):
                # Санитарная очистка: если запись старше суток — удаляем
                logger.info("CLEANUP: удаляю просроченное напоминание chat_id=%s msg_id=%s date=%s",
                            chat_id, msg_id, date_obj)
                delete_reminders({"message_id": msg_id})
    except Exception as exc:
        logger.error("Ошибка в pop_job: %s", exc)
