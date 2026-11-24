"""Отправка, обновление и «подъём» карточек задач в чате."""

import logging

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from ..models import Task, UserSettings
from .ui import card_markup, render_card

logger = logging.getLogger(__name__)


def _not_modified(exc: Exception) -> bool:
    return "not modified" in str(exc).lower()


async def send_card(
    bot: Bot, t: Task, u: UserSettings, webapp: str, *, copy_from: int | None = None, fired: bool = False, markup=None
) -> int:
    """Отправляет новую карточку. Для медиа копирует сообщение copy_from (или текущую карточку)."""
    text = render_card(t, u, fired)
    markup = markup or card_markup(t, u, webapp)
    source = copy_from or t.message_id
    if t.kind != "text" and source:
        try:
            kwargs = {"caption": text, "parse_mode": ParseMode.HTML} if t.kind == "media" else {}
            msg = await bot.copy_message(t.chat_id, t.chat_id, source, reply_markup=markup, **kwargs)
            return msg.message_id
        except TelegramError as exc:
            logger.warning("copy_message %s не удалось (%s) — шлю текстом", t.id, exc)
    msg = await bot.send_message(t.chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    return msg.message_id


async def refresh_card(bot: Bot, t: Task, u: UserSettings, webapp: str) -> int:
    """Перерисовывает существующую карточку; если её нет — отправляет новую. Возвращает message_id."""
    if t.message_id is None:
        return await send_card(bot, t, u, webapp)
    text = render_card(t, u)
    markup = card_markup(t, u, webapp)
    try:
        if t.kind == "text":
            await bot.edit_message_text(text, t.chat_id, t.message_id, parse_mode=ParseMode.HTML, reply_markup=markup)
        elif t.kind == "media":
            await bot.edit_message_caption(t.chat_id, t.message_id, caption=text, parse_mode=ParseMode.HTML, reply_markup=markup)
        else:
            await bot.edit_message_reply_markup(t.chat_id, t.message_id, reply_markup=markup)
        return t.message_id
    except BadRequest as exc:
        if _not_modified(exc):
            return t.message_id
        logger.info("Карточка %s не редактируется (%s) — отправляю заново", t.id, exc)
        return await send_card(bot, t, u, webapp)


async def repost_card(bot: Bot, t: Task, u: UserSettings, webapp: str, fired: bool = True, markup=None) -> int:
    """Поднимает карточку вниз чата: новая копия + удаление (или сворачивание) старой."""
    new_id = await send_card(bot, t, u, webapp, fired=fired, markup=markup)
    if t.message_id and t.message_id != new_id:
        await retire_message(bot, t.chat_id, t.message_id)
    return new_id


async def retire_message(bot: Bot, chat_id: int, message_id: int | None, note: str = "↓ перенесено ниже") -> bool:
    """Убрать старую карточку. Telegram не даёт боту удалять сообщения старше 48 ч — тогда сворачиваем:
    заменяем текст/подпись на короткую пометку и убираем кнопки. True — удалено, False — свёрнуто."""
    if not message_id:
        return True
    try:
        await bot.delete_message(chat_id, message_id)
        return True
    except TelegramError:
        pass
    try:
        await bot.edit_message_text(note, chat_id, message_id)
    except TelegramError:
        try:
            await bot.edit_message_caption(chat_id, message_id, caption=note)
        except TelegramError:
            pass
    try:
        await bot.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
    except TelegramError:
        pass
    return False


async def safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramError as exc:
        logger.debug("delete_message %s/%s: %s", chat_id, message_id, exc)
