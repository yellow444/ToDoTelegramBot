"""Фоновые задачи JobQueue: напоминания, синхронизация карточек, рабочий день."""

import asyncio
import logging
from datetime import datetime, time, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from ..models import AGENT_LABELS, utcnow
from ..timeutil import WEEKDAYS_SHORT, is_workday, local_now
from . import cards
from .handlers import agenda_text, deps
from .ui import job_result_markup

logger = logging.getLogger(__name__)
_lock = asyncio.Lock()  # тики не должны перекрываться


async def reminders_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    async with _lock:
        d = deps(context)
        now = utcnow()
        for t in await d.service.due(now):
            try:
                if await d.service.defer_if_outside_work_hours(t, now):
                    continue
                u = await d.service.user(t.chat_id)
                shown = t.model_copy(update={"nag_count": t.nag_count + 1})  # карточка с кнопками «отложить»
                new_id = await cards.repost_card(context.bot, shown, u, d.settings.WEBAPP_URL)
                await d.service.after_fired(t, new_id, now)
                logger.info("Напоминание %s отправлено (nag=%d)", t.id, t.nag_count)
            except Forbidden:
                logger.warning("Чат %s недоступен — снимаю напоминание %s", t.chat_id, t.id)
                await d.service.repo.update_task(t.id, remind_at=None)
            except Exception:
                logger.exception("Ошибка напоминания %s", t.id)
                await d.service.repo.update_task(t.id, remind_at=now + timedelta(minutes=1))


async def sync_cards(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Применяет изменения, сделанные вне чата (webapp / MCP / LLM), и отправляет уведомления."""
    d = deps(context)
    for t in await d.service.repo.dirty_tasks():
        try:
            u = await d.service.user(t.chat_id)
            message_id = await cards.refresh_card(context.bot, t, u, d.settings.WEBAPP_URL)
            await d.service.repo.update_task(t.id, message_id=message_id, card_dirty=False)
        except Forbidden:
            await d.service.repo.update_task(t.id, card_dirty=False)
        except Exception:
            logger.exception("Не удалось обновить карточку %s", t.id)
    await _notify_jobs(context)
    for item in await d.service.repo.pop_outbox():
        try:
            if item.get("delete_message_id"):
                await cards.safe_delete(context.bot, item["chat_id"], item["delete_message_id"])
            if item.get("text"):
                await context.bot.send_message(item["chat_id"], item["text"][:4000])
        except TelegramError as exc:
            logger.warning("outbox → %s: %s", item.get("chat_id"), exc)


async def _notify_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отчёты агентов-коворкеров → Telegram. Ответ на отчёт продолжает разговор с агентом."""
    d = deps(context)
    for job in await d.service.repo.jobs_to_notify():
        if job.notify != "finished":
            await d.service.repo.update_job(job.id, notify="none")  # «взял в работу» видно по карточке
            continue
        try:
            own = await d.service.repo.get_task(job.task_id) if job.task_id else None
            service_job = bool(job.title) or (own is not None and own.source == "orchestrator")
            head = f"🤖 {AGENT_LABELS[job.agent]}" + (f" · {job.workdir}" if job.workdir else "")
            if service_job:
                head = f"🤖 {AGENT_LABELS[job.agent]} · {job.title or own.text}"
            head += " — готово ✅" if job.status == "done" else " — не получилось ❌"
            body = job.result or "(пустой отчёт)"
            tail = f"\n\n↩️ Ответьте на это сообщение, чтобы продолжить с {AGENT_LABELS[job.agent]}."
            # у служебного разбора дня не повторяем текст задания — только отчёт
            full = f"{head}\n\n" if service_job else f"{head}\nЗадание: {job.prompt[:300]}\n\n"
            if len(full) + len(body) + len(tail) > 4000:
                body = body[: 4000 - len(full) - len(tail) - 40] + "\n…(полный отчёт во вложении)"
                await context.bot.send_document(
                    job.chat_id,
                    InputFile((job.result or "").encode("utf-8"), filename=f"report-{job.id}.md"),
                )
            mentioned = await _mentioned_tasks(d, job)
            msg = await context.bot.send_message(
                job.chat_id, full + body + tail, reply_markup=job_result_markup(job, mentioned, own)
            )
            await d.service.repo.update_job(job.id, notify="none", result_message_id=msg.message_id)
        except Forbidden:
            await d.service.repo.update_job(job.id, notify="none")
        except Exception:
            logger.exception("Не удалось отправить отчёт агента %s", job.id)


async def _mentioned_tasks(d, job) -> list:
    """Задачи, упомянутые в отчёте как #id (или их точные тексты)."""
    import re

    found, seen = [], set()
    for ref in re.findall(r"#([0-9a-f]{6,12})\b", job.result or ""):
        try:
            t = await d.service.resolve(job.chat_id, ref)
        except Exception:
            continue
        if t.id not in seen:
            seen.add(t.id)
            found.append(t)
    return found


async def cards_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    async with _lock:
        await sync_cards(context)


async def workday_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Утренний план в начале рабочего дня и итог в конце."""
    d = deps(context)
    try:
        await d.service.fail_stale_jobs()
    except Exception:
        logger.exception("fail_stale_jobs")
    for u in await d.service.repo.list_users():
        try:
            now = local_now(u)
            today = now.date()
            if not is_workday(today, u):
                continue
            # итог шлём в течение 3 часов после конца дня (не позже полуночи)
            end_window = (datetime.combine(today, u.work_end) + timedelta(hours=3)).time()
            if end_window <= u.work_end:
                end_window = time(23, 59)
            if u.morning_digest and u.last_morning != today and u.work_start <= now.time() < u.work_end:
                if u.orchestrator:
                    await d.service.enqueue_orchestration(u.chat_id, "morning")  # разбор делает коворкер
                else:
                    await _morning(context, u.chat_id)
                await d.service.update_user(u.chat_id, last_morning=today)
            elif u.evening_review and u.last_evening != today and u.work_end <= now.time() < end_window:
                if u.orchestrator:
                    await d.service.enqueue_orchestration(u.chat_id, "evening")
                else:
                    await _evening(context, u.chat_id)
                await d.service.update_user(u.chat_id, last_evening=today)
        except Forbidden:
            await d.service.update_user(u.chat_id, morning_digest=False, evening_review=False)
        except Exception:
            logger.exception("workday_tick для %s", u.chat_id)


async def _morning(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    d = deps(context)
    a = await d.service.agenda(chat_id)
    day = a["day"]
    text = agenda_text(a, f"☀️ <b>Доброе утро! План на {day:%d.%m}, {WEEKDAYS_SHORT[day.weekday()]}</b>")
    from .handlers import agenda_markup
    if d.agent and (a["overdue"] or a["today"] or a["undated"]):
        from ..tools import get_agenda

        plan = await d.agent.summarize_day(chat_id, await get_agenda(chat_id=chat_id), "morning")
        if plan:
            text += "\n\n🤖 " + plan
    await context.bot.send_message(chat_id, text[:4000], parse_mode=ParseMode.HTML, reply_markup=agenda_markup(a))


async def _evening(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    from html import escape

    d = deps(context)
    a = await d.service.agenda(chat_id)
    done = await d.service.done_on(chat_id, a["day"])
    left = a["overdue"] + a["today"]
    lines = [f"🌇 <b>Итоги дня</b>\nСделано: {len(done)}"]
    lines += [f"✅ {escape(t.text[:80])}" for t in done[:15]]
    if left:
        lines.append(f"\nОсталось на сегодня: {len(left)}")
        lines += [f"• {escape(t.text[:80])}" for t in left[:15]]
    text = "\n".join(lines)
    if d.agent and (done or left):
        from ..tools import get_agenda

        summary = await d.agent.summarize_day(chat_id, await get_agenda(chat_id=chat_id), "evening")
        if summary:
            text += "\n\n🤖 " + summary
    markup = (
        InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Перенести несделанное на утро", callback_data="w|-|move")]])
        if any(not t.repeat for t in left)
        else None
    )
    await context.bot.send_message(chat_id, text[:4000], parse_mode=ParseMode.HTML, reply_markup=markup)
