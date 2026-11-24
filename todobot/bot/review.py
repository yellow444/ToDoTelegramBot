"""/review — разбор задач: список, чистка ботом и решение по одной.

1. Обзор: что бот уберёт сам (служебные карточки разборов дня, давно выполненные задачи) и что ждёт решения.
2. 🧹 Чистка: бот удаляет эти карточки из чата (старше 48 ч Telegram удалить не даёт — тогда сворачивает их),
   сами задачи остаются в базе как история.
3. ▶️ По одной: бот заново присылает вниз чата задачу за задачей с кнопками решения и прогрессом «3/12».
"""

import logging
from datetime import datetime, timedelta, timezone
from html import escape

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..models import Task, UserSettings, utcnow
from ..timeutil import fmt_local, to_local
from . import cards, ui
from .handlers import deps

logger = logging.getLogger(__name__)

REASON_LABELS = {"overdue": "просрочена", "stale": "давно без срока", "failed": "не распознана"}
OUTCOME_LABELS = {
    "done": "сделано", "moved": "назначен срок", "idea": "в идеи", "deleted": "удалено",
    "delegated": "поручено агентам", "skipped": "пропущено",
}


# ------------------------------------------------------------------ обзор

def summary(c: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    service, done_old, triage = c["service"], c["done_old"], c["triage"]
    reasons = c["reasons"]
    lines = ["🧹 <b>Разбор задач</b>"]
    rows = []
    if service or done_old:
        lines.append("\n<b>Бот уберёт сам</b> (задачи останутся в истории):")
        if service:
            lines.append(f"• служебные карточки разборов дня — {len(service)}")
        if done_old:
            lines.append(f"• выполненные больше недели назад — {len(done_old)}")
        rows.append([B(f"🧹 Убрать из чата ({len(service) + len(done_old)})", callback_data="rv|-|clean")])
    if triage:
        counts = {}
        for t in triage:
            counts[reasons[t.id]] = counts.get(reasons[t.id], 0) + 1
        detail = ", ".join(f"{REASON_LABELS[k]} — {v}" for k, v in counts.items())
        lines.append(f"\n<b>Нужно ваше решение:</b> {len(triage)} ({detail})")
        lines.append("Бот покажет их по одной: сделано, срок, в идеи или удалить.")
        rows.append([B(f"▶️ Разобрать по одной ({len(triage)})", callback_data="rv|-|start")])
        rows.append([B("📋 Список", callback_data="rv|-|list")])
    if len(lines) == 1:
        lines.append("Всё в порядке: убирать нечего, решений не ждёт ни одна задача 🙌")
    return "\n".join(lines), InlineKeyboardMarkup(rows) if rows else None


def triage_list(c: dict, u: UserSettings) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["📋 <b>Ждут решения</b>"]
    for i, t in enumerate(c["triage"][:40], 1):
        lines.append(f"{i}. {escape(t.text[:80])} — <i>{reason_text(t, c['reasons'][t.id], u)}</i>")
    markup = ui.list_markup(c["triage"], refresh="", numbered=True) or InlineKeyboardMarkup([])
    rows = list(markup.inline_keyboard) + [
        [B("▶️ Разобрать по одной", callback_data="rv|-|start"), B("◀ Назад", callback_data="rv|-|back")]
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def reason_text(t: Task, reason: str, u: UserSettings) -> str:
    if reason == "overdue" and t.scheduled_at:
        return f"просрочена с {to_local(t.scheduled_at, u):%d.%m}"
    if reason == "stale":
        return f"без срока с {to_local(t.created_at, u):%d.%m}"
    return REASON_LABELS[reason]


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    d = deps(context)
    c = await d.service.review_candidates(update.effective_chat.id)
    text, markup = summary(c)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str) -> None:
    q = update.callback_query
    d = deps(context)
    chat_id = q.message.chat_id
    note = None
    if arg == "clean":
        c = await d.service.review_candidates(chat_id)
        removed = collapsed = 0
        for t in c["service"] + c["done_old"]:
            if await cards.retire_message(context.bot, chat_id, t.message_id, note=f"🗄 #{t.id[:6]} в архиве"):
                removed += 1
            else:
                collapsed += 1
            await d.service.repo.update_task(t.id, message_id=None, card_dirty=False)
        note = f"Убрано: {removed}" + (f", свёрнуто (старше 48 ч): {collapsed}" if collapsed else "")
    elif arg == "start":
        await start_triage(context, chat_id)
        await q.answer("Поехали: первая задача внизу 👇")
        return
    c = await d.service.review_candidates(chat_id)
    u = await d.service.user(chat_id)
    text, markup = triage_list(c, u) if arg == "list" else summary(c)
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
    await q.answer(note)


# ------------------------------------------------------------------ по одной

def triage_markup(t: Task, pos: int, total: int, reason: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [B(f"🧹 {pos}/{total} · {REASON_LABELS.get(reason, '')} — что делаем?", callback_data=ui.IGNORE)],
        [B("✔️ Сделано", callback_data=f"v|{t.id}|done"), B("❌ Удалить", callback_data=f"v|{t.id}|del")],
        [
            B("📅 Завтра", callback_data=f"v|{t.id}|tom"),
            B("📅 Пн", callback_data=f"v|{t.id}|mon"),
            B("📆 Через неделю", callback_data=f"v|{t.id}|week"),
        ],
        [B("📅 Другая дата", callback_data=f"v|{t.id}|cal"), B("💡 В идеи", callback_data=f"v|{t.id}|idea")],
        [B("⏭ Пропустить", callback_data=f"v|{t.id}|skip"), B("⏹ Хватит", callback_data=f"v|{t.id}|stop")],
    ])


async def start_triage(context, chat_id: int) -> None:
    d = deps(context)
    c = await d.service.review_candidates(chat_id)
    queue = [t.id for t in c["triage"]]
    await d.service.update_user(
        chat_id, review_queue=queue, review_total=len(queue), review_stats={}, review_reasons=c["reasons"]
    )
    await show_next(context, chat_id)


async def show_next(context, chat_id: int) -> None:
    """Присылает вниз чата следующую задачу из очереди разбора (или итог, если очередь кончилась)."""
    d = deps(context)
    u = await d.service.user(chat_id)
    queue = list(u.review_queue)
    while queue:
        t = await d.service.repo.get_task(queue[0])
        if t and t.status == "open":
            break
        queue.pop(0)  # задачу уже закрыли или удалили другим путём
    if not queue:
        await d.service.update_user(chat_id, review_queue=[])
        await finish_triage(context, chat_id)
        return
    pos = u.review_total - len(queue) + 1
    reason = u.review_reasons.get(t.id, "")
    markup = triage_markup(t, pos, u.review_total, reason)
    new_id = await cards.repost_card(context.bot, t, u, d.settings.WEBAPP_URL, fired=False, markup=markup)
    await d.service.repo.update_task(t.id, message_id=new_id, card_dirty=False)
    await d.service.update_user(chat_id, review_queue=queue, review_current=t.id)


async def finish_triage(context, chat_id: int) -> None:
    d = deps(context)
    u = await d.service.user(chat_id)
    stats = {k: v for k, v in u.review_stats.items() if v}
    await d.service.update_user(chat_id, review_queue=[], review_current=None, review_stats={})
    if not u.review_total:
        return
    parts = ", ".join(f"{OUTCOME_LABELS.get(k, k)} — {v}" for k, v in stats.items()) or "ничего не изменено"
    left = len([x for x in u.review_queue if x]) if u.review_queue else 0
    tail = f"\nОсталось неразобранными: {left}. Продолжить: /review" if left else ""
    await context.bot.send_message(chat_id, f"🧹 Разбор закончен: {parts}.{tail}")


async def after_action(context, t: Task, outcome: str) -> None:
    """Решение по текущей задаче разбора принято (любой кнопкой карточки) — показать следующую."""
    d = deps(context)
    u = await d.service.user(t.chat_id)
    if u.review_current != t.id:
        return
    stats = dict(u.review_stats)
    stats[outcome] = stats.get(outcome, 0) + 1
    queue = [x for x in u.review_queue if x != t.id]
    await d.service.update_user(t.chat_id, review_queue=queue, review_stats=stats, review_current=None)
    await show_next(context, t.chat_id)


async def triage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: str, arg: str) -> None:
    q = update.callback_query
    d = deps(context)
    chat_id = q.message.chat_id
    try:
        t = await d.service.get(task_id, chat_id)
    except Exception:
        await q.answer("Задача уже удалена")
        await d.service.update_user(chat_id, review_current=task_id)
        await after_action(context, Task(id=task_id, chat_id=chat_id, text=""), "skipped")
        return
    u = await d.service.user(chat_id)
    if q.message and t.message_id != q.message.message_id:
        t = await d.service.repo.update_task(t.id, message_id=q.message.message_id) or t

    async def redraw(task: Task) -> None:
        await cards.refresh_card(context.bot, task, u, d.settings.WEBAPP_URL)

    if arg == "done":
        t = await d.service.complete(t)
        await redraw(t)
        outcome, note = "done", "Готово ✅"
    elif arg == "del":
        await d.service.delete(t)
        await cards.retire_message(context.bot, chat_id, t.message_id, note=f"🗑 #{t.id[:6]} удалена")
        outcome, note = "deleted", "Удалено"
    elif arg in ("tom", "mon", "week"):
        when = week_later(u) if arg == "week" else ui.preset_time(arg, u)
        t = await d.service.set_reminder(t, when)
        await redraw(t)
        outcome, note = "moved", f"Напомню {fmt_local(t.scheduled_at, u)}"
    elif arg == "cal":
        await q.edit_message_reply_markup(ui.calendar_markup(t, u))  # ✅ в календаре продолжит разбор
        await q.answer("Выберите дату и время")
        return
    elif arg == "idea":
        t = await d.service.repo.update_task(t.id, idea=True, scheduled_at=None, remind_at=None, nag_count=0) or t
        await redraw(t)
        outcome, note = "idea", "В идеях: /ideas"
    elif arg == "skip":
        await redraw(t)
        outcome, note = "skipped", "Пропущено"
    else:  # stop
        await redraw(t)
        await d.service.update_user(chat_id, review_current=None)
        await q.answer("Разбор остановлен")
        await finish_triage(context, chat_id)
        return
    await q.answer(note)
    await after_action(context, t, outcome)


def week_later(u: UserSettings, now: datetime | None = None) -> datetime:
    now = now or utcnow()
    loc = to_local(now, u)
    d = loc.date() + timedelta(days=7)
    while d.weekday() not in u.workdays:
        d += timedelta(days=1)
    return datetime.combine(d, u.work_start, tzinfo=loc.tzinfo).astimezone(timezone.utc)
