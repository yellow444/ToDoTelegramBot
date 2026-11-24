"""Текст карточек и inline-клавиатуры. callback_data: «тип|id|аргумент», ≤ 64 байт."""

import calendar
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup, WebAppInfo

from ..models import AGENT_LABELS, JOB_STATUS_LABELS, REPEAT_LABELS, AgentJob, Task, UserSettings
from ..timeutil import WEEKDAYS_SHORT, fmt_local, local_now, next_work_start, to_local, zone

MONTHS = ["", "Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
IGNORE = "x"


CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
EXTRACTED_LABELS = {"transcript": "📝 Расшифровка", "ocr": "🔎 Текст с картинки"}


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def card_meta(t: Task, u: UserSettings) -> str:
    meta = []
    if t.status == "done":
        if t.done_at:
            meta.append(f"выполнено {fmt_local(t.done_at, u)}")
    else:
        if t.idea and not t.scheduled_at:
            meta.append("💡 идея")
        elif t.scheduled_at and t.period:
            meta.append(f"📆 {period_label(t, u)}")
        elif t.scheduled_at:
            meta.append(f"⏰ {fmt_local(t.scheduled_at, u)}")
        if t.repeat:
            meta.append(f"🔁 {REPEAT_LABELS[t.repeat]}")
        if t.workhours_only:
            meta.append("🕘 в рабочее время")
        if t.assignee:
            agent = f"🤖 {AGENT_LABELS[t.assignee]}" + (f" · {t.agent_workdir}" if t.agent_workdir else "")
            meta.append(agent + f" · {JOB_STATUS_LABELS.get(t.agent_status or '', t.agent_status or '')}")
    if t.extracted_kind == "pending":
        meta.append("⏳ распознаю…")
    elif t.extracted_kind == "failed":
        meta.append("⚠️ не распознано — /recognize")
    meta.append(f"#{t.id[:6]}")
    return " · ".join(meta)


MONTHS_NOM = ["", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
              "сентябрь", "октябрь", "ноябрь", "декабрь"]


def period_label(t: Task, u: UserSettings) -> str:
    loc = to_local(t.scheduled_at, u)
    if t.period == "week":
        return f"неделя с {loc:%d.%m}"
    if t.period == "month":
        return f"{MONTHS_NOM[loc.month]} {loc.year}" + f" · напомню {loc:%d.%m}"
    return f"{loc.year} год · напомню {loc:%d.%m.%Y}"


def card_parts(t: Task) -> tuple[str, str | None]:
    """(заголовок, блок распознанного текста). Без подписи задача и есть распознанный текст — блок не дублируем."""
    block = None
    if t.extracted_text and t.caption_text:
        block = t.extracted_text
    return t.text, block


def render_card(t: Task, u: UserSettings, fired: bool = False) -> str:
    """HTML карточки. Укладывается в лимит подписи (медиа) или сообщения (текст)."""
    limit = (CAPTION_LIMIT if t.kind != "text" else TEXT_LIMIT) - 40
    meta = escape(card_meta(t, u))
    head, block = card_parts(t)
    budget = limit - len(meta) - 20
    if block:
        label = EXTRACTED_LABELS.get(t.extracted_kind or "", "📝")
        head_part = _cut(head, max(60, budget // 3))
        block_part = _cut(block, max(0, budget - len(head_part) - len(label) - 4))
        body = f"{escape(head_part)}\n\n<b>{label}:</b>\n{escape(block_part)}"
    else:
        body = escape(_cut(head, budget))
    if t.status == "done":
        return f"✅ <s>{body}</s>\n<i>{meta}</i>"
    prefix = "🔔 " if fired or t.nag_count > 0 else ""
    return f"{prefix}{body}\n<i>{meta}</i>"


def card_truncated(t: Task) -> bool:
    """Полный текст не влез в карточку — нужен ответ под карточкой с полным текстом."""
    if not t.extracted_text:
        return False
    if t.kind == "media_nocaption":
        return True
    head, block = card_parts(t)
    full = len(head) + len(block or "")
    return full > (CAPTION_LIMIT if t.kind != "text" else TEXT_LIMIT) - 200


def extracted_message(t: Task, u: UserSettings) -> list[str]:
    """Ответ под карточкой: полный распознанный текст и даты (разбит по 4000 символов)."""
    label = EXTRACTED_LABELS.get(t.extracted_kind or "", "📝 Текст")
    text = t.extracted_text or ""
    meta = card_meta(t, u)
    chunks = [text[i : i + 3800] for i in range(0, len(text), 3800)] or [""]
    out = []
    for i, chunk in enumerate(chunks):
        head = f"{label} #{t.id[:6]}" + (f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else "") + ":\n"
        out.append(head + chunk + (f"\n\n{meta}" if i == len(chunks) - 1 else ""))
    return out


def webapp_url(base: str, **params) -> str:
    parts = urlparse(base)
    query = dict(parse_qsl(parts.query))
    query.update({k: str(v) for k, v in params.items() if v is not None})
    return urlunparse(parts._replace(query=urlencode(query)))


def card_markup(t: Task, u: UserSettings, webapp_base: str = "") -> InlineKeyboardMarkup:
    if t.status == "done":
        return InlineKeyboardMarkup([[B("↩️ Вернуть", callback_data=f"t|{t.id}|undo"), B("❌", callback_data=f"t|{t.id}|del")]])
    # web_app-кнопки Telegram разрешает только в личных чатах
    if u.ui_mode == "webapp" and webapp_base and t.chat_id > 0:
        when_btn = B("⏰ Когда", web_app=WebAppInfo(webapp_url(webapp_base, task=t.id)))
    else:
        when_btn = B("⏰ Когда", callback_data=f"t|{t.id}|when")
    rows = [[
        B("✔️ Готово", callback_data=f"t|{t.id}|done"),
        when_btn,
        B("✏️", callback_data=f"t|{t.id}|edit"),
        B("❌", callback_data=f"t|{t.id}|del"),
    ]]
    if t.nag_count > 0:
        rows.append(
            [
                B("💤 15 мин", callback_data=f"s|{t.id}|15m"),
                B("💤 1 час", callback_data=f"s|{t.id}|1h"),
                B("💤 завтра", callback_data=f"s|{t.id}|tom"),
            ]
        )
    return InlineKeyboardMarkup(rows)


def when_menu(t: Task, u: UserSettings) -> InlineKeyboardMarkup:
    ws = u.work_start.strftime("%H:%M")
    wh = "✅" if t.workhours_only else "☐"
    return InlineKeyboardMarkup(
        [
            [B("+15 мин", callback_data=f"s|{t.id}|15m"), B("+1 час", callback_data=f"s|{t.id}|1h"), B("+3 часа", callback_data=f"s|{t.id}|3h")],
            [B("Вечером 19:00", callback_data=f"s|{t.id}|eve"), B(f"Завтра {ws}", callback_data=f"s|{t.id}|tom")],
            [B("🏢 Начало раб. дня", callback_data=f"s|{t.id}|work"), B(f"Пн {ws}", callback_data=f"s|{t.id}|mon")],
            [B("📅 Календарь", callback_data=f"t|{t.id}|cal"), B("🔁 Повтор", callback_data=f"t|{t.id}|rep")],
            [B("🤖 Поручить Claude", callback_data=f"a|{t.id}|claude"), B("🤖 Codex", callback_data=f"a|{t.id}|codex")],
            [B(f"{wh} Только в раб. время", callback_data=f"t|{t.id}|wh"), B("🔕 Снять", callback_data=f"s|{t.id}|off")],
            [B("◀ Назад", callback_data=f"t|{t.id}|back")],
        ]
    )


def repeat_menu(t: Task) -> InlineKeyboardMarkup:
    def btn(code: str | None, label: str) -> B:
        mark = "• " if t.repeat == code else ""
        return B(mark + label, callback_data=f"r|{t.id}|{code or 'none'}")

    return InlineKeyboardMarkup(
        [
            [btn(None, "Не повторять"), btn("daily", "Каждый день")],
            [btn("workdays", "По будням"), btn("weekly", "Каждую неделю")],
            [btn("monthly", "Каждый месяц")],
            [B("◀ Назад", callback_data=f"t|{t.id}|when")],
        ]
    )


def preset_time(code: str, u: UserSettings, now: datetime | None = None) -> datetime | None:
    """Время для кнопок-пресетов (UTC). None — снять напоминание."""
    now = now or datetime.now(timezone.utc)
    loc = to_local(now, u)
    if code == "off":
        return None
    if code.endswith("m") and code[:-1].isdigit():
        return now + timedelta(minutes=int(code[:-1]))
    if code.endswith("h") and code[:-1].isdigit():
        return now + timedelta(hours=int(code[:-1]))
    if code == "eve":
        target = loc.replace(hour=19, minute=0, second=0, microsecond=0)
        if target <= loc:
            target += timedelta(days=1)
        return target.astimezone(timezone.utc)
    if code == "tom":
        d = loc.date() + timedelta(days=1)
        return datetime.combine(d, u.work_start, tzinfo=loc.tzinfo).astimezone(timezone.utc)
    if code == "mon":
        d = loc.date() + timedelta(days=(7 - loc.weekday()) or 7)
        return datetime.combine(d, u.work_start, tzinfo=loc.tzinfo).astimezone(timezone.utc)
    if code == "work":
        return next_work_start(now, u)
    raise ValueError(code)


# ------------------------------------------------------------------ календарь

def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M")


def parse_stamp(value: str, u: UserSettings) -> datetime:
    return datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=zone(u.tz))


def calendar_markup(t: Task, u: UserSettings, cur: datetime | None = None) -> InlineKeyboardMarkup:
    """Календарь с выбором времени. cur — выбранный момент в локальном времени пользователя."""
    now = local_now(u)
    if cur is None:
        base = to_local(t.scheduled_at, u) if t.scheduled_at else now + timedelta(hours=1)
        cur = base.replace(minute=(base.minute // 15) * 15, second=0, microsecond=0)

    def c(dt: datetime, act: str = "v") -> str:
        return f"c|{t.id}|{_stamp(dt)}|{act}"

    def shift_month(dt: datetime, delta: int) -> datetime:
        y, m = divmod(dt.month - 1 + delta, 12)
        y += dt.year
        m += 1
        return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))

    rows = [[
        B("«", callback_data=c(shift_month(cur, -1))),
        B(f"{MONTHS[cur.month]} {cur.year}", callback_data=IGNORE),
        B("»", callback_data=c(shift_month(cur, 1))),
    ]]
    rows.append([B(d, callback_data=IGNORE) for d in WEEKDAYS_SHORT])
    for week in calendar.monthcalendar(cur.year, cur.month):
        row = []
        for day in week:
            if day == 0:
                row.append(B(" ", callback_data=IGNORE))
                continue
            dt = cur.replace(day=day)
            if dt.date() < now.date():
                row.append(B("·", callback_data=IGNORE))
            else:
                label = f"[{day}]" if day == cur.day else str(day)
                row.append(B(label, callback_data=c(dt)))
        rows.append(row)
    rows.append([
        B("−1ч", callback_data=c(cur - timedelta(hours=1))),
        B(cur.strftime("%H:%M"), callback_data=IGNORE),
        B("+1ч", callback_data=c(cur + timedelta(hours=1))),
    ])
    rows.append([
        B("−15м", callback_data=c(cur - timedelta(minutes=15))),
        B(f"{u.work_start:%H:%M}", callback_data=c(cur.replace(hour=u.work_start.hour, minute=u.work_start.minute))),
        B("+15м", callback_data=c(cur + timedelta(minutes=15))),
    ])
    rows.append([
        B("◀ Назад", callback_data=f"t|{t.id}|when"),
        B(f"✅ {cur:%d.%m %H:%M}", callback_data=c(cur, "ok")),
    ])
    return InlineKeyboardMarkup(rows)


# ------------------------------------------------------------------ списки

def task_line(i: int, t: Task, u: UserSettings) -> str:
    when = f" — ⏰ {fmt_local(t.scheduled_at, u)}" if t.scheduled_at else ""
    rep = " 🔁" if t.repeat else ""
    return f"{i}. {escape(t.text[:100])}{when}{rep}"


def list_markup(tasks: list[Task], refresh: str = "l|-|refresh", numbered: bool = True) -> InlineKeyboardMarkup | None:
    """По строке на задачу: 📍 — поднять её карточку вниз чата, ✔️ — закрыть."""
    if not tasks:
        return None
    rows = []
    for i, t in enumerate(tasks[:20], 1):
        label = f"📍 {i}. {t.text[:32]}" if numbered else f"📍 {t.text[:34]}"
        rows.append([B(label, callback_data=f"g|{t.id}|show"), B("✔️", callback_data=f"l|{t.id}|done")])
    if refresh:
        rows.append([B("🔄 Обновить", callback_data=refresh)])
    return InlineKeyboardMarkup(rows)


# ------------------------------------------------------------------ агенты и модели

def workdir_menu(t: Task, agent: str, workdirs: list[str]) -> InlineKeyboardMarkup:
    rows, row = [], []
    for alias in workdirs:
        row.append(B(f"📁 {alias}", callback_data=f"a|{t.id}|{agent}:{alias}"[:64]))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("Без проекта", callback_data=f"a|{t.id}|{agent}:-")])
    rows.append([B("◀ Назад", callback_data=f"t|{t.id}|when")])
    return InlineKeyboardMarkup(rows)


def job_line(j: AgentJob) -> str:
    where = f" · {escape(j.workdir)}" if j.workdir else ""
    return f"🤖 {AGENT_LABELS[j.agent]}{where} · {JOB_STATUS_LABELS[j.status]} — {escape(j.prompt[:70])}"


def jobs_markup(jobs: list[AgentJob]) -> InlineKeyboardMarkup | None:
    active = [j for j in jobs if j.status in ("queued", "running")]
    if not active:
        return None
    return InlineKeyboardMarkup(
        [[B(f"⛔ Отменить: {j.prompt[:30]}", callback_data=f"j|{j.id}|cancel")] for j in active[:8]]
    )


def job_result_markup(j: AgentJob, mentioned: list[Task] | None = None, own_task: Task | None = None) -> InlineKeyboardMarkup | None:
    """Под отчётом: ✔️ на каждую упомянутую открытую задачу (+ сама задача поручения, если не служебная)."""
    tasks = [t for t in (mentioned or []) if t.status == "open"]
    if own_task and own_task.status == "open" and own_task.source != "orchestrator" and own_task.id not in {t.id for t in tasks}:
        tasks.insert(0, own_task)
    if not tasks:
        return None
    return InlineKeyboardMarkup(
        [[B(f"📍 {t.text[:34]}", callback_data=f"g|{t.id}|show"), B("✔️", callback_data=f"x|{t.id}|done")]
         for t in tasks[:12]]
    )


def model_menu(models: list[dict], current: str) -> InlineKeyboardMarkup:
    rows = []
    for m in models[:20]:
        tags = m.get("tags") or []
        icon = "💻" if "local" in tags else "☁️"
        mark = "• " if m["id"] == current else ""
        cb = f"m|-|{m['id']}"
        if len(cb.encode()) <= 64:
            rows.append([B(f"{mark}{icon} {m['id']}", callback_data=cb)])
    rows.append([B("↺ По умолчанию", callback_data="m|-|default")])
    return InlineKeyboardMarkup(rows)
