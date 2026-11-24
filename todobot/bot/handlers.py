"""Обработчики команд, сообщений и нажатий кнопок."""

import logging
from dataclasses import dataclass
from html import escape

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop, ContextTypes

from ..config import Settings
from ..llm import Agent, LLMError
from ..models import AGENT_LABELS, REPEAT_LABELS, Task, UserSettings, utcnow
from ..service import TaskNotFound, TaskService
from ..timeparse import parse_when
from ..timeutil import WEEKDAYS_SHORT, fmt_local, local_now, next_work_start, parse_workdays
from . import cards, ui

logger = logging.getLogger(__name__)

HELP = """\
<b>Как пользоваться</b>
Просто пришлите сообщение (текст, фото, файл, голосовое) — оно станет задачей с кнопками.
Время можно указать прямо в тексте: «позвонить Ивану завтра в 11», «отчёт через 2 часа»,
«стендап по будням в 10:00», «оплатить 15.10 в рабочее время».

<b>Команды</b>
/list — открытые задачи
/today — план на сегодня
/mode — режим: кнопки в чате ↔ мини-приложение
/app — открыть мини-приложение
/work 09:00-18:00 1-5 — рабочие часы и дни
/digest on|off — утренний план и вечерний итог
/tz Europe/Moscow — часовой пояс
/ai вопрос (или «? вопрос») — спросить ассистента; он видит ваши задачи и умеет ими управлять
/smart on|off — разбирать новые задачи через LLM
/model — модель ассистента (локальная / бесплатная облачная)
/forget — очистить память диалога

<b>Коворкеры (Claude Code / Codex по подписке)</b>
/claude [папка] задание — поручить Claude, /codex [папка] задание — Codex
/agents — что сейчас делают агенты
Кнопка «🤖 Поручить» в меню «⏰ Когда» у любой задачи. Ответьте на отчёт агента — он продолжит разговор.
Ассистент сам передаёт агентам сложные задачи: «? попроси Claude разобраться с тестами в todo».
"""

MEDIA_LABELS = {
    "sticker": "Стикер", "location": "Геолокация", "venue": "Место", "contact": "Контакт",
    "poll": "Опрос", "dice": "Кубик", "video_note": "Видеосообщение",
}


@dataclass
class Deps:
    settings: Settings
    service: TaskService
    agent: Agent | None
    transcriber: object | None = None  # stt.Transcriber
    tg_transcriber: object | None = None  # userbot.TelegramTranscriber


def deps(context: ContextTypes.DEFAULT_TYPE) -> Deps:
    return context.application.bot_data["deps"]


# ---------------------------------------------------------------- доступ

async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    allowed = deps(context).settings.ALLOWED_USER_IDS
    user = update.effective_user
    if allowed and (user is None or user.id not in allowed):
        if update.callback_query:
            await update.callback_query.answer("Нет доступа", show_alert=True)
        logger.warning("Отклонён доступ: %s", user.id if user else None)
        raise ApplicationHandlerStop


# ---------------------------------------------------------------- команды

async def set_menu_button(context: ContextTypes.DEFAULT_TYPE, chat_id: int, u: UserSettings) -> None:
    d = deps(context)
    if chat_id <= 0:
        return
    try:
        if u.ui_mode == "webapp" and d.settings.webapp_enabled:
            await context.bot.set_chat_menu_button(
                chat_id, MenuButtonWebApp("Задачи", WebAppInfo(d.settings.WEBAPP_URL))
            )
        else:
            await context.bot.set_chat_menu_button(chat_id, MenuButtonCommands())
    except BadRequest as exc:
        logger.info("set_chat_menu_button: %s", exc)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = deps(context)
    chat_id = update.effective_chat.id
    u = await d.service.user(chat_id)
    await set_menu_button(context, chat_id, u)
    extra = []
    extra.append("🤖 Ассистент подключён." if d.agent else "🤖 Ассистент выключен (не задан LLM_BASE_URL).")
    extra.append(f"Режим: <b>{'мини-приложение' if u.ui_mode == 'webapp' else 'кнопки'}</b>.")
    extra.append(f"Рабочий день: {u.work_start:%H:%M}–{u.work_end:%H:%M}, {_days(u)}; пояс {u.tz}.")
    await update.effective_message.reply_text(
        "Привет! Я веду ваши задачи и напоминаю о них.\n\n" + HELP + "\n" + "\n".join(extra),
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.effective_message.reply_text("Клавиатура убрана.", reply_markup=ReplyKeyboardRemove())
    await cards.safe_delete(context.bot, msg.chat_id, msg.message_id)


def _days(u: UserSettings) -> str:
    return ", ".join(WEEKDAYS_SHORT[i] for i in u.workdays)


async def mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = deps(context)
    chat_id = update.effective_chat.id
    u = await d.service.user(chat_id)
    arg = (context.args[0].lower() if context.args else "")
    target = {"buttons": "buttons", "кнопки": "buttons", "webapp": "webapp", "app": "webapp"}.get(
        arg, "buttons" if u.ui_mode == "webapp" else "webapp"
    )
    if target == "webapp" and not d.settings.webapp_enabled:
        await update.effective_message.reply_text(
            "Мини-приложение не настроено: задайте WEBAPP_URL (публичный https-адрес) в .env."
        )
        return
    u = await d.service.update_user(chat_id, ui_mode=target)
    await set_menu_button(context, chat_id, u)
    for t in await d.service.list_tasks(chat_id, "open", 50):
        await d.service.repo.update_task(t.id, card_dirty=True)  # перерисовать кнопки
    text = (
        "Режим: <b>мини-приложение</b>. Кнопка «Задачи» в меню и «⏰ Когда» открывают приложение."
        if target == "webapp"
        else "Режим: <b>кнопки в чате</b>."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def open_app(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = deps(context)
    if not d.settings.webapp_enabled:
        await update.effective_message.reply_text("WEBAPP_URL не задан.")
        return
    if update.effective_chat.id <= 0:
        await update.effective_message.reply_text("Мини-приложение открывается только в личном чате с ботом.")
        return
    await update.effective_message.reply_text(
        "Откройте задачи:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📋 Задачи", web_app=WebAppInfo(d.settings.WEBAPP_URL))]]
        ),
    )


async def _list_text(d: Deps, chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    u = await d.service.user(chat_id)
    tasks = await d.service.list_tasks(chat_id, "open", 20)
    if not tasks:
        return "Открытых задач нет 🎉", None
    lines = [ui.task_line(i, t, u) for i, t in enumerate(tasks, 1)]
    return "<b>Открытые задачи</b>\n" + "\n".join(lines), ui.list_markup(tasks)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = await _list_text(deps(context), update.effective_chat.id)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


def agenda_text(a: dict, title: str) -> str:
    u = a["user"]
    parts = [title]

    def block(name: str, items: list[Task]) -> None:
        if items:
            parts.append(f"\n<b>{name}</b>")
            parts.extend(f"• {escape(t.text[:100])}" + (f" — {fmt_local(t.scheduled_at, u)}" if t.scheduled_at else "") for t in items)

    block("Просрочено", a["overdue"])
    block("На сегодня", a["today"])
    if not a["overdue"] and not a["today"]:
        parts.append("На сегодня задач нет 🙌")
    block("Дальше", a["later"][:10])
    block("Без срока", a["undated"][:10])
    return "\n".join(parts)


def agenda_markup(a: dict) -> InlineKeyboardMarkup | None:
    tasks = a["overdue"] + a["today"] + a["later"][:10] + a["undated"][:10]
    return ui.list_markup(tasks, refresh="", numbered=False)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = deps(context)
    a = await d.service.agenda(update.effective_chat.id)
    c = await d.service.review_candidates(update.effective_chat.id)
    hint = ""
    if c["triage"] or c["service"] or c["done_old"]:
        hint = f"\n\n🧹 Ждут решения: {len(c['triage'])}, убрать из чата: {len(c['service']) + len(c['done_old'])} — /review"
    await update.effective_message.reply_text(
        agenda_text(a, f"📅 <b>{a['day']:%d.%m}, {WEEKDAYS_SHORT[a['day'].weekday()]}</b>") + hint,
        parse_mode=ParseMode.HTML,
        reply_markup=agenda_markup(a),
    )


async def work(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/work 09:00-18:00 1-5"""
    d = deps(context)
    chat_id = update.effective_chat.id
    fields: dict = {}
    for arg in context.args or []:
        if ":" in arg and "-" in arg:
            start_s, end_s = arg.split("-", 1)
            fields["work_start"], fields["work_end"] = start_s, end_s
        else:
            fields["workdays"] = arg
    try:
        if "workdays" in fields and not parse_workdays(fields["workdays"]):
            raise ValueError("пустой список дней")
        u = await d.service.update_user(chat_id, **fields) if fields else await d.service.user(chat_id)
    except ValueError as exc:
        await update.effective_message.reply_text(f"Не понял: {exc}\nПример: /work 09:00-18:00 1-5")
        return
    await update.effective_message.reply_text(
        f"Рабочий день: {u.work_start:%H:%M}–{u.work_end:%H:%M}, {_days(u)}.\n"
        f"Утренний план: {'вкл' if u.morning_digest else 'выкл'}, вечерний итог: {'вкл' if u.evening_review else 'выкл'}.\n"
        "Изменить: /work 10:00-19:00 1-5"
    )


def _on_off(context) -> bool | None:
    arg = (context.args[0].lower() if context.args else "")
    return {"on": True, "вкл": True, "1": True, "off": False, "выкл": False, "0": False}.get(arg)


async def digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = deps(context)
    val = _on_off(context)
    if val is None:
        await update.effective_message.reply_text("Использование: /digest on|off")
        return
    await d.service.update_user(update.effective_chat.id, morning_digest=val, evening_review=val)
    await update.effective_message.reply_text(f"Утренний план и вечерний итог: {'включены' if val else 'выключены'}.")


async def smart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = deps(context)
    val = _on_off(context)
    if val is None:
        await update.effective_message.reply_text("Использование: /smart on|off")
        return
    if val and not d.agent:
        await update.effective_message.reply_text("LLM не настроена (LLM_BASE_URL).")
        return
    await d.service.update_user(update.effective_chat.id, smart_input=val)
    await update.effective_message.reply_text(f"Разбор задач через LLM: {'включён' if val else 'выключен'}.")


async def tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = deps(context)
    if not context.args:
        u = await d.service.user(update.effective_chat.id)
        await update.effective_message.reply_text(f"Часовой пояс: {u.tz}. Изменить: /tz Europe/Berlin")
        return
    try:
        u = await d.service.update_user(update.effective_chat.id, tz=context.args[0])
    except Exception:
        await update.effective_message.reply_text("Неизвестный часовой пояс. Пример: Europe/Moscow")
        return
    await update.effective_message.reply_text(f"Часовой пояс: {u.tz}, сейчас {local_now(u):%H:%M}.")


async def forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await deps(context).service.repo.clear_history(update.effective_chat.id)
    await update.effective_message.reply_text("Память диалога очищена.")


# ---------------------------------------------------------------- ассистент

async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args or []).strip()
    if not text:
        await update.effective_message.reply_text("Напишите вопрос: /ai что у меня на завтра?")
        return
    await ask_agent(update, context, text)


async def ask_agent(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, focus: Task | None = None) -> None:
    d = deps(context)
    if not d.agent:
        await update.effective_message.reply_text("Ассистент выключен: задайте LLM_BASE_URL.")
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        focus_text = None
        if focus is not None:
            from ..tools import task_context

            focus_text = task_context(focus, await d.service.user(focus.chat_id))
        reply = await d.agent.ask(update.effective_chat.id, text, focus=focus_text)
    except LLMError as exc:
        logger.warning("agent: %s", exc)
        await update.effective_message.reply_text(f"LLM недоступна: {exc}")
        return
    await update.effective_message.reply_text(reply.text[:4000])
    if reply.changed:
        from .jobs import sync_cards  # отрисовать изменения сразу, не дожидаясь тика

        await sync_cards(context)


# ---------------------------------------------------------------- новые задачи

def _kind(msg: Message) -> tuple[str, str]:
    """(kind, подпись по умолчанию)."""
    if msg.text:
        return "text", ""
    if msg.photo or msg.video or msg.document or msg.audio or msg.voice or msg.animation:
        return "media", ""
    for attr, label in MEDIA_LABELS.items():
        if getattr(msg, attr, None):
            return "media_nocaption", label
    return "media_nocaption", "Вложение"


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or (msg.from_user and msg.from_user.is_bot):
        return
    text = (msg.text or msg.caption or "").strip()
    d = deps(context)
    chat_id = msg.chat_id
    reply = getattr(msg, "reply_to_message", None)
    if reply and text:
        if job := await d.service.repo.find_job_by_message(chat_id, reply.message_id):
            await _follow_up(update, context, job, text)
            return
        if focus := await _card_task(d, chat_id, reply.message_id):
            if focus.edit_prompt_id and focus.edit_prompt_id == reply.message_id:
                # ответ на подсказку кнопки ✏️ — это новый текст задачи как есть
                await _set_text(context, focus, text)
                await d.service.repo.update_task(focus.id, edit_prompt_id=None)
                await cards.safe_delete(context.bot, chat_id, reply.message_id)
                await cards.safe_delete(context.bot, chat_id, msg.message_id)
                return
            if await _on_card_reply(update, context, focus, text):
                return
    if text.startswith("?"):
        await ask_agent(update, context, text[1:].strip())
        return

    kind, label = _kind(msg)
    if not text and kind == "text":
        return
    u = await d.service.user(chat_id)

    when = repeat = None
    parse_text = True
    if text and u.smart_input and d.agent:
        data = await d.agent.extract_task(chat_id, text)
        if data:
            parse_text = False
            text, repeat = data["text"], data.get("repeat")
            if data.get("when"):
                try:
                    when = parse_when(str(data["when"]), u)
                except ValueError:
                    when = None

    if kind == "text" and parse_text:
        from ..split import looks_multi, split_list

        items = split_list(text)
        if not items and d.agent and looks_multi(text) and len(text.split()) >= 12:
            # разбор LLM может занять минуты (холодная загрузка модели) — делаем в фоне
            status = await msg.reply_text("🧩 Разбираю план на задачи…", disable_notification=True)
            context.application.create_task(
                _split_plan_background(context, chat_id, text, msg.message_id, status.message_id), update=update
            )
            return
        if items:
            for item in items:
                t = await d.service.create(chat_id, item, parse_text=True, default_remind=True)
                mid = await cards.send_card(context.bot, t, u, d.settings.WEBAPP_URL)
                await d.service.repo.update_task(t.id, message_id=mid)
            await cards.safe_delete(context.bot, chat_id, msg.message_id)
            return

    media = _media_info(msg)
    from .extract import needs_extraction

    pending = media.get("file_id") and needs_extraction(Task(chat_id=chat_id, text="", **media))
    task = await d.service.create(
        chat_id,
        text or label,
        when=when,
        repeat=repeat,
        parse_text=parse_text and bool(text),
        kind=kind,
        default_remind=not pending,  # для медиа время ищем ещё и в распознанном тексте
        caption_text=(msg.caption or None) if kind != "text" else None,
        extracted_kind="pending" if pending else None,
        **media,
    )
    message_id = await cards.send_card(context.bot, task, u, d.settings.WEBAPP_URL, copy_from=msg.message_id)
    await d.service.repo.update_task(task.id, message_id=message_id)
    await cards.safe_delete(context.bot, chat_id, msg.message_id)
    if pending:
        from . import extract

        context.application.create_task(extract.process(context, task.id), update=update)


def _media_info(msg: Message) -> dict:
    """Метаданные вложения: тип, file_id, имя, mime, длительность."""
    for attr in ("voice", "video_note", "audio", "video", "animation", "document", "photo", "sticker"):
        obj = getattr(msg, attr, None)
        if not obj:
            continue
        if attr == "photo":
            obj = obj[-1]  # самое большое разрешение
        return {
            "media_type": attr,
            "file_id": obj.file_id,
            "file_name": getattr(obj, "file_name", None),
            "mime": getattr(obj, "mime_type", None) or ("image/jpeg" if attr == "photo" else None),
            "media_duration": getattr(obj, "duration", None),
        }
    return {}


async def _card_task(d: Deps, chat_id: int, message_id: int) -> Task | None:
    """Задача по карточке или по ответу-расшифровке под ней."""
    t = await d.service.repo.find_by_message(chat_id, message_id)
    if t:
        return t
    for x in await d.service.repo.list_tasks(chat_id, None, 500):
        if message_id in (x.extracted_message_id, x.edit_prompt_id):
            return x
    return None


async def _set_text(context, t: Task, new_text: str) -> Task:
    """Новый текст задачи от пользователя. Для медиа он становится расшифровкой (время берётся из него)."""
    d = deps(context)
    new_text = new_text.strip()
    if t.kind != "text":
        t = await d.service.repo.update_task(t.id, extracted_raw=t.extracted_text, caption_text=None) or t
        t = await d.service.apply_extracted(t, new_text, "ocr" if t.media_type == "photo" else "transcript")
    else:
        t = await d.service.edit_text(t, new_text)
    await _redraw(context, t, await d.service.user(t.chat_id))
    return t


async def _ask_new_text(context, t: Task) -> None:
    """Кнопка ✏️: подсказка с текущим текстом (тап по нему копирует) и поле ответа."""
    d = deps(context)
    if t.edit_prompt_id:
        await cards.safe_delete(context.bot, t.chat_id, t.edit_prompt_id)
    current = t.extracted_text or t.text
    msg = await context.bot.send_message(
        t.chat_id,
        f"✏️ Новый текст для #{t.id[:6]}. Вставьте расшифровку или напишите свой вариант "
        f"(время в тексте тоже поймаю).\nСейчас: <code>{escape(current[:3500])}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(selective=True, input_field_placeholder="Новый текст задачи…"),
        disable_notification=True,
    )
    await d.service.repo.update_task(t.id, edit_prompt_id=msg.message_id)


async def _on_card_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, t: Task, text: str) -> bool:
    """Ответ на карточку — работа с этой задачей: «? вопрос», «перенеси на пятницу», просто новое время."""
    d = deps(context)
    if text.startswith("="):
        # «= текст» — свой вариант текста (например, скопированная расшифровка Telegram Premium)
        if not text[1:].strip():
            return False
        await _set_text(context, t, text[1:])
        await cards.safe_delete(context.bot, t.chat_id, update.effective_message.message_id)
        return True
    question = text[1:].strip() if text.startswith("?") else text
    if d.agent:
        await ask_agent(update, context, question, focus=t)
        return True
    u = await d.service.user(t.chat_id)
    from ..timeparse import parse

    p = parse(text, u)
    if p.remind_at or p.repeat:
        t = await d.service.set_reminder(t, p.remind_at, repeat=p.repeat if p.repeat else "keep")
        await _redraw(context, t, u)
        await update.effective_message.reply_text(f"#{t.id[:6]}: напомню {fmt_local(t.scheduled_at, u)}")
        return True
    return False


# ---------------------------------------------------------------- кнопки

async def _show(context, t: Task, u: UserSettings, markup: InlineKeyboardMarkup) -> None:
    try:
        await context.bot.edit_message_reply_markup(t.chat_id, t.message_id, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def _redraw(context, t: Task, u: UserSettings) -> None:
    d = deps(context)
    message_id = await cards.refresh_card(context.bot, t, u, d.settings.WEBAPP_URL)
    if message_id != t.message_id:
        await d.service.repo.update_task(t.id, message_id=message_id)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    d = deps(context)
    chat_id = q.message.chat_id if q.message else None

    if data == ui.IGNORE:
        await q.answer()
        return
    if data in ("done", "undone", "date", "del"):  # кнопки карточек старой версии бота
        t = await d.service.repo.find_by_message(chat_id, q.message.message_id)
        if t is None:
            await q.answer("Задача не найдена")
            return
        data = f"t|{t.id}|" + {"done": "done", "undone": "undo", "date": "when", "del": "del"}[data]

    kind, task_id, arg = (data.split("|", 2) + ["", ""])[:3]
    if kind == "l":
        await _list_callback(update, context, task_id, arg)
        return
    if kind == "m":
        await _model_callback(update, context, arg)
        return
    if kind == "j":
        await _job_callback(update, context, task_id, arg)
        return
    if kind == "x":
        await _report_task_callback(update, context, task_id, arg)
        return
    if kind == "g":
        await _goto_callback(update, context, task_id)
        return
    if kind == "rv":
        from .review import review_callback

        await review_callback(update, context, arg)
        return
    if kind == "v":
        from .review import triage_callback

        await triage_callback(update, context, task_id, arg)
        return
    if kind == "w":
        await _workday_callback(update, context, arg)
        return

    try:
        t = await d.service.get(task_id, chat_id)
    except TaskNotFound:
        await q.answer("Задача уже удалена")
        return
    u = await d.service.user(t.chat_id)
    if q.message and t.message_id != q.message.message_id:
        # нажали на старую копию карточки — работаем с ней
        t = await d.service.repo.update_task(t.id, message_id=q.message.message_id) or t

    note = None
    if kind == "t":
        if arg == "done":
            t = await d.service.complete(t)
            note = f"Следующий раз: {fmt_local(t.scheduled_at, u)}" if t.repeat else "Готово ✅"
            await _redraw(context, t, u)
        elif arg == "undo":
            t = await d.service.reopen(t)
            await _redraw(context, t, u)
        elif arg == "del":
            await d.service.delete(t)
            await cards.safe_delete(context.bot, t.chat_id, t.message_id)
            note = "Удалено"
        elif arg == "when":
            await _show(context, t, u, ui.when_menu(t, u))
        elif arg == "cal":
            await _show(context, t, u, ui.calendar_markup(t, u))
        elif arg == "rep":
            await _show(context, t, u, ui.repeat_menu(t))
        elif arg == "wh":
            t = await d.service.set_reminder(t, t.scheduled_at, workhours_only=not t.workhours_only)
            note = "Только в рабочее время" if t.workhours_only else "В любое время"
            await cards.refresh_card(context.bot, t, u, d.settings.WEBAPP_URL)
            await _show(context, t, u, ui.when_menu(t, u))
        elif arg == "back":
            await _redraw(context, t, u)
        elif arg == "edit":
            await _ask_new_text(context, t)
            note = "Пришлите новый текст ответом"
    elif kind == "s":
        when = ui.preset_time(arg, u)
        if arg in ("15m", "1h") and t.nag_count > 0 and t.repeat:
            t = await d.service.snooze(t, 15 if arg == "15m" else 60)
        else:
            t = await d.service.set_reminder(t, when)
        note = f"Напомню {fmt_local(t.remind_at, u)}" if t.remind_at else "Напоминание снято"
        await _redraw(context, t, u)
    elif kind == "r":
        t = await d.service.set_reminder(t, t.scheduled_at, repeat=None if arg == "none" else arg)
        note = "Повтор: " + (REPEAT_LABELS[t.repeat] if t.repeat else "нет")
        await _redraw(context, t, u)
    elif kind == "a":
        agent, _, workdir = arg.partition(":")
        workdirs = list(d.settings.workdirs)
        if not workdir and workdirs:
            await _show(context, t, u, ui.workdir_menu(t, agent, workdirs))
        else:
            try:
                t, _job = await d.service.delegate(
                    t.chat_id, agent, t.text, workdir=None if workdir in ("", "-") else workdir, task=t
                )
                note = f"Поручено {AGENT_LABELS[agent]}"
            except ValueError as exc:
                note = str(exc)[:190]
            await _redraw(context, t, u)
    elif kind == "c":
        stamp, _, act = arg.partition("|")
        cur = ui.parse_stamp(stamp, u)
        if act == "ok":
            if cur <= local_now(u):
                await q.answer("Это время уже прошло", show_alert=True)
                return
            t = await d.service.set_reminder(t, cur)
            note = f"Напомню {fmt_local(t.scheduled_at, u)}"
            await _redraw(context, t, u)
        else:
            await _show(context, t, u, ui.calendar_markup(t, u, cur))
    await q.answer(note)

    # решение по задаче из разбора /review принято обычной кнопкой карточки — показать следующую
    outcome = None
    if kind == "t" and arg in ("done", "del"):
        outcome = "done" if arg == "done" else "deleted"
    elif kind == "s" or (kind == "c" and arg.endswith("|ok")):
        outcome = "moved"
    elif kind == "a" and ":" in arg:
        outcome = "delegated"
    if outcome:
        from .review import after_action

        await after_action(context, t, outcome)


async def _list_callback(update: Update, context, task_id: str, arg: str) -> None:
    q = update.callback_query
    d = deps(context)
    chat_id = q.message.chat_id
    note = None
    if arg == "done":
        try:
            t = await d.service.get(task_id, chat_id)
            t = await d.service.complete(t, touch_card=True)
            note = "Готово ✅"
        except TaskNotFound:
            note = "Задача не найдена"
    is_list = any(
        (b.callback_data or "") == "l|-|refresh"
        for row in (q.message.reply_markup.inline_keyboard if q.message.reply_markup else []) for b in row
    )
    try:
        if is_list:
            text, markup = await _list_text(d, chat_id)
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        else:  # /today: просто убираем строку закрытой задачи
            rows = [
                row for row in q.message.reply_markup.inline_keyboard
                if not any((b.callback_data or "").endswith(f"|{task_id}|done") for b in row)
            ]
            await q.edit_message_reply_markup(InlineKeyboardMarkup(rows) if rows else None)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
    await q.answer(note)


async def _goto_callback(update: Update, context, task_id: str) -> None:
    """📍: поднять карточку задачи вниз чата (ссылок на сообщения в личке с ботом Telegram не даёт)."""
    q = update.callback_query
    d = deps(context)
    try:
        t = await d.service.get(task_id, q.message.chat_id)
    except TaskNotFound:
        await q.answer("Задача не найдена")
        return
    u = await d.service.user(t.chat_id)
    new_id = await cards.repost_card(context.bot, t, u, d.settings.WEBAPP_URL, fired=False)
    await d.service.repo.update_task(t.id, message_id=new_id, card_dirty=False)
    await q.answer("Карточка внизу 👇")


async def _workday_callback(update: Update, context, arg: str) -> None:
    """Кнопки вечернего итога."""
    q = update.callback_query
    d = deps(context)
    chat_id = q.message.chat_id
    if arg == "move":
        u = await d.service.user(chat_id)
        a = await d.service.agenda(chat_id)
        target = next_work_start(utcnow(), u)
        moved = 0
        for t in a["overdue"] + a["today"]:
            if t.repeat:
                continue
            await d.service.set_reminder(t, target, touch_card=True)
            moved += 1
        await q.edit_message_reply_markup(None)
        await q.answer(f"Перенесено на {fmt_local(target, u)}: {moved}")
        return
    await q.answer()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке апдейта", exc_info=context.error)


# ---------------------------------------------------------------- распознать заново

async def recognize_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/recognize — распознать медиа-задачи без текста (старые карточки или неудачные попытки).
    В ответ на карточку — распознать только её."""
    d = deps(context)
    chat_id = update.effective_chat.id
    reply = getattr(update.effective_message, "reply_to_message", None)
    if reply:
        one = await _card_task(d, chat_id, reply.message_id)
        tasks = [one] if one else []
    else:
        tasks = [
            t for t in await d.service.list_tasks(chat_id, "open", 300)
            if t.kind != "text" and t.extracted_kind in (None, "failed") and t.message_id
        ]
    tasks = [t for t in tasks if t.kind != "text"]
    if not tasks:
        await update.effective_message.reply_text("Нечего распознавать.")
        return
    await update.effective_message.reply_text(f"Распознаю задач: {len(tasks)}…")
    from . import extract

    for t in tasks:
        if not t.file_id:
            # у старых карточек нет file_id — пересылаем карточку себе и берём файл из пересланной копии
            try:
                fwd = await context.bot.forward_message(chat_id, chat_id, t.message_id, disable_notification=True)
            except BadRequest as exc:
                logger.warning("Карточка %s недоступна: %s", t.id, exc)
                continue
            media = _media_info(fwd)
            await cards.safe_delete(context.bot, chat_id, fwd.message_id)
            if not media:
                continue
            # подпись карточки рисует бот; исходная подпись пользователя — это текст задачи (если он был)
            caption = t.text if t.text and not t.text.startswith("(") and t.text not in MEDIA_LABELS.values() else None
            t = await d.service.repo.update_task(t.id, caption_text=caption, **media) or t
        await d.service.repo.update_task(t.id, extracted_kind="pending")
        await extract.process(context, t.id)


# ---------------------------------------------------------------- коворкеры и модели

def _split_workdir(d: Deps, args: list[str]) -> tuple[str | None, str]:
    if args and args[0] in d.settings.workdirs:
        return args[0], " ".join(args[1:]).strip()
    return None, " ".join(args).strip()


async def _delegate_command(update: Update, context: ContextTypes.DEFAULT_TYPE, agent: str) -> None:
    d = deps(context)
    workdir, text = _split_workdir(d, context.args or [])
    reply = getattr(update.effective_message, "reply_to_message", None)
    focus = await _card_task(d, update.effective_chat.id, reply.message_id) if reply else None
    if focus is not None:
        # /claude в ответ на карточку: задание про эту задачу (с её текстом, расшифровкой и вложением)
        from ..tools import task_context

        u = await d.service.user(focus.chat_id)
        prompt = (text or "Выполни эту задачу.") + "\n\n" + task_context(focus, u)
        try:
            await d.service.delegate(focus.chat_id, agent, prompt, workdir=workdir, task=focus)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        await update.effective_message.reply_text(f"🤖 #{focus.id[:6]} поручена {AGENT_LABELS[agent]}.")
        from .jobs import sync_cards

        await sync_cards(context)
        return
    if not text:
        dirs = ", ".join(d.settings.workdirs) or "не настроены (AGENT_WORKDIRS)"
        await update.effective_message.reply_text(
            f"Использование: /{agent} [папка] задание\nПапки: {dirs}\nПример: /{agent} todo добавь тесты для парсера дат"
        )
        return
    try:
        _task, job = await d.service.delegate(update.effective_chat.id, agent, text, workdir=workdir)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    where = f" в папке {workdir}" if workdir else ""
    status = (
        "Чат-коворкер на связи: статус обновится в карточке, отчёт придёт сюда."
        if d.service.worker_online(agent)
        else f"Чат-коворкер {AGENT_LABELS[agent]} сейчас не на связи: задание ждёт в очереди, "
        "пока вы не откроете его чат в десктопном приложении."
    )
    await update.effective_message.reply_text(f"🤖 Поручено {AGENT_LABELS[agent]}{where}. {status}")
    from .jobs import sync_cards

    await sync_cards(context)


async def ideas_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ideas — идеи без срока (📍 поднять карточку, ⏰ на карточке — запланировать)."""
    d = deps(context)
    ideas = [t for t in await d.service.list_tasks(update.effective_chat.id, "open", 300) if t.idea]
    if not ideas:
        await update.effective_message.reply_text(
            "Идей пока нет. Напишите «идея: …» или «… когда-нибудь» — сохраню без напоминаний."
        )
        return
    lines = [f"{i}. {escape(t.text[:100])}" for i, t in enumerate(ideas[:30], 1)]
    await update.effective_message.reply_text(
        "💡 <b>Идеи</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=ui.list_markup(ideas, refresh=""),
    )


async def vocab_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/vocab слово, Имя Фамилия, термин — словарь для распознавания речи. /vocab -слово — убрать."""
    d = deps(context)
    chat_id = update.effective_chat.id
    u = await d.service.user(chat_id)
    raw = " ".join(context.args or []).strip()
    if not raw:
        words = ", ".join(u.vocabulary) or "пусто"
        await update.effective_message.reply_text(
            f"Словарь распознавания: {words}\n"
            "Добавить: /vocab Ревель, исполком, Kubernetes\nУбрать: /vocab -слово"
        )
        return
    vocab = list(u.vocabulary)
    for item in (x.strip() for x in raw.split(",")):
        if not item:
            continue
        if item.startswith("-"):
            vocab = [w for w in vocab if w.lower() != item[1:].strip().lower()]
        elif item.lower() not in (w.lower() for w in vocab):
            vocab.append(item)
    u = await d.service.update_user(chat_id, vocabulary=vocab[:200])
    await update.effective_message.reply_text(
        f"Словарь: {', '.join(u.vocabulary) or 'пусто'}.\nОтветьте /recognize на карточку, чтобы распознать её заново."
    )


async def orchestrate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/orchestrate claude|codex|off — кто делает утренний план и вечерний разбор."""
    d = deps(context)
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("claude", "codex", "off"):
        u = await d.service.user(update.effective_chat.id)
        who = AGENT_LABELS.get(u.orchestrator or "", "сам бот (локальная LLM)")
        await update.effective_message.reply_text(
            f"Утренний план и вечерний разбор делает: {who}.\nИзменить: /orchestrate claude | codex | off"
        )
        return
    value = None if arg == "off" else arg
    await d.service.update_user(update.effective_chat.id, orchestrator=value)
    text = (
        f"Утро и вечер рабочего дня теперь разбирает {AGENT_LABELS[value]}: задания встанут в его очередь, "
        "отчёт придёт сюда."
        if value
        else "Утро и вечер снова разбирает сам бот."
    )
    await update.effective_message.reply_text(text)


async def claude_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _delegate_command(update, context, "claude")


async def codex_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _delegate_command(update, context, "codex")


async def _follow_up(update: Update, context: ContextTypes.DEFAULT_TYPE, job, text: str) -> None:
    d = deps(context)
    task = await d.service.repo.get_task(job.task_id) if job.task_id else None
    try:
        await d.service.delegate(job.chat_id, job.agent, text, workdir=job.workdir, task=task, parent=job)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(f"🤖 Передал {AGENT_LABELS[job.agent]} продолжение.")


async def _jobs_text(d: Deps, chat_id: int):
    jobs = await d.service.repo.list_jobs(chat_id, limit=10)
    if not jobs:
        return "Поручений агентам пока не было. Пример: /claude todo обнови README", None
    return "<b>Поручения агентам</b>\n" + "\n".join(ui.job_line(j) for j in jobs), ui.jobs_markup(jobs)


async def agents_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = await _jobs_text(deps(context), update.effective_chat.id)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def _job_callback(update: Update, context, job_id: str, arg: str) -> None:
    q = update.callback_query
    d = deps(context)
    note = None
    if arg == "cancel":
        job = await d.service.repo.get_job(job_id)
        if job is None or job.chat_id != q.message.chat_id:
            note = "Задание не найдено"
        else:
            await d.service.cancel_job(job_id)
            note = "Отменено"
    text, markup = await _jobs_text(d, q.message.chat_id)
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
    await q.answer(note)


def _free_or_local(models: list[dict]) -> list[dict]:
    """Только то, что не тратит деньги: локальные модели и бесплатные облачные, с поддержкой tools."""
    out = []
    for m in models:
        tags = m.get("tags") or []
        if "tools" in tags and ("local" in tags or "free" in tags):
            out.append(m)
    return out


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = deps(context)
    if not d.agent:
        await update.effective_message.reply_text("LLM не настроена (LLM_BASE_URL).")
        return
    u = await d.service.user(update.effective_chat.id)
    current = u.llm_model or d.settings.LLM_MODEL
    models = _free_or_local(await d.agent.client.list_models())
    fallback = d.settings.LLM_FALLBACK_MODEL or "не задана"
    await update.effective_message.reply_text(
        f"Модель ассистента: <b>{escape(current)}</b>\nЗапасная: {escape(fallback)}\n"
        "💻 — локальная, ☁️ — бесплатная облачная.",
        parse_mode=ParseMode.HTML,
        reply_markup=ui.model_menu(models, current),
    )


async def _model_callback(update: Update, context, model: str) -> None:
    q = update.callback_query
    d = deps(context)
    value = None if model == "default" else model
    u = await d.service.update_user(q.message.chat_id, llm_model=value)
    current = u.llm_model or d.settings.LLM_MODEL
    await q.edit_message_text(f"Модель ассистента: <b>{escape(current)}</b>", parse_mode=ParseMode.HTML)
    await q.answer("Готово")


async def _report_task_callback(update: Update, context, task_id: str, arg: str) -> None:
    """Кнопка ✔️ под отчётом агента: закрыть упомянутую задачу, не трогая сам отчёт."""
    q = update.callback_query
    d = deps(context)
    try:
        t = await d.service.get(task_id, q.message.chat_id)
    except TaskNotFound:
        await q.answer("Задача не найдена")
        return
    if arg == "done" and t.status == "open":
        await d.service.complete(t, touch_card=True)
    rows = [
        row for row in (q.message.reply_markup.inline_keyboard if q.message.reply_markup else [])
        if not any((b.callback_data or "").startswith(f"x|{task_id}|") for b in row)
    ]
    try:
        await q.edit_message_reply_markup(InlineKeyboardMarkup(rows) if rows else None)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
    await q.answer(f"Готово: {t.text[:40]}")


async def _split_plan_background(context, chat_id: int, text: str, source_id: int, status_id: int) -> None:
    """Длинный текст-план → отдельные задачи. Не получилось разбить — одна задача, как раньше."""
    from ..split import item_phrase, split_llm

    d = deps(context)
    u = await d.service.user(chat_id)
    try:
        items = [item_phrase(i) for i in await split_llm(d.agent, chat_id, text)]
    except Exception:
        logger.exception("split plan")
        items = []
    for item in items or [text]:
        t = await d.service.create(chat_id, item, parse_text=True, default_remind=True)
        mid = await cards.send_card(context.bot, t, u, d.settings.WEBAPP_URL)
        await d.service.repo.update_task(t.id, message_id=mid)
    await cards.safe_delete(context.bot, chat_id, status_id)
    await cards.safe_delete(context.bot, chat_id, source_id)
