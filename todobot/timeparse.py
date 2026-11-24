"""Разбор времени из фраз на русском: «позвонить завтра в 10», «через 15 мин», «по будням в 9:30».

Это быстрый детерминированный разбор без LLM. Поддерживает ISO-формат, поэтому им же
пользуются MCP-инструменты и webapp.
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .models import UserSettings
from .timeutil import is_workday, local_now, to_utc, zone

_I = re.IGNORECASE

REPEATS = [
    (re.compile(r"\b(?:каждый\s+день|ежедневно)\b", _I), "daily"),
    (re.compile(r"\b(?:по\s+будням|каждый\s+будний\s+день)\b", _I), "workdays"),
    (re.compile(r"\b(?:каждую\s+неделю|еженедельно)\b", _I), "weekly"),
    (re.compile(r"\b(?:каждый\s+месяц|ежемесячно)\b", _I), "monthly"),
]
WORKHOURS = re.compile(r"\bв\s+рабочее\s+время\b", _I)

RELATIVE_HALF = re.compile(r"\bчерез\s+полчаса\b", _I)
RELATIVE = re.compile(
    r"\bчерез\s+(?:(\d+)\s*)?(минут[уы]?|мин|м|час(?:а|ов)?|ч|дн(?:я|ей)|день|недел[юиь]|нед)(?=\W|$)", _I
)

DAY_WORDS = re.compile(r"\b(сегодня|завтра|послезавтра)\b", _I)
WEEKDAY = re.compile(
    r"\b(?:в|во|каждый|каждую|каждое)\s+(понедельник|вторник|среду|среда|четверг|пятницу|пятница|субботу|суббота|воскресенье)\b",
    _I,
)
WEEKDAY_IDX = {
    "понедельник": 0, "вторник": 1, "среду": 2, "среда": 2, "четверг": 3,
    "пятницу": 4, "пятница": 4, "субботу": 5, "суббота": 5, "воскресенье": 6,
}
ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?")
# Дата: 15.10, 05.10.2026, 1.10.26. Без года месяц должен быть двузначным, иначе «ТДД 1.5» станет датой.
DMY = re.compile(r"\b(\d{1,2})[./](\d{2}|\d{1,2}(?=[./]\d{2,4}))(?:[./](\d{2,4}))?(?!\d|[:./]\d)")

TIME_V = re.compile(
    r"\b(?:в|к)\s+(\d{1,2})(?:[:.](\d{2}))?(?:\s*(утра|дня|вечера|ночи|час(?:а|ов)?|ч))?(?=\s*(?:[,.;!?)]|$))"
    r"|\b(?:в|к)\s+(\d{1,2})(?:[:.](\d{2}))?\s*(утра|дня|вечера|ночи|час(?:а|ов)?|ч)\b"
    r"|\b(?:в|к)\s+(\d{1,2})[:.](\d{2})\b",
    _I,
)
TIME_BARE = re.compile(r"(?<![\d.:/-])(\d{1,2}):(\d{2})(?![\d:])")
DAYPART = re.compile(r"\b(утром|днём|днем|вечером)\b", _I)
DAYPART_TIME = {"утром": time(9), "днём": time(13), "днем": time(13), "вечером": time(19)}

# --- планы на недели / месяцы / год и идеи без срока
MONTH_RX = (
    r"(январ[ьяе]|феврал[ьяе]|март[ае]?|апрел[ьяе]|ма[йяе]|июн[ьяе]|июл[ьяе]|август[ае]?|"
    r"сентябр[ьяе]|октябр[ьяе]|ноябр[ьяе]|декабр[ьяе])"
)
MONTH_IDX = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
YEAR_SUFFIX = r"(?:\s+(\d{4})(?:\s*(?:года|году|год|г\.?))?)?"
DAY_MONTH = re.compile(rf"\b(\d{{1,2}})\s+{MONTH_RX}{YEAR_SUFFIX}\b", _I)
IN_MONTH = re.compile(rf"\b(?:в|на|к|до)\s+(?:начале\s+|середине\s+|конце\s+)?{MONTH_RX}{YEAR_SUFFIX}\b", _I)
PERIODS = [
    (re.compile(r"\b(?:на|в)\s+следующ(?:ей|ую)\s+недел[еюи]\b", _I), "next_week"),
    (re.compile(r"\b(?:на\s+этой\s+неделе|до\s+конца\s+недели)\b", _I), "this_week"),
    (re.compile(r"\b(?:в|к)\s+конц[еу]\s+недели\b", _I), "end_week"),
    (re.compile(r"\b(?:в|на)\s+следующ(?:ем|ий)\s+месяц[еу]?\b", _I), "next_month"),
    (re.compile(r"\b(?:в|к)\s+конц[еу]\s+месяца\b", _I), "end_month"),
    (re.compile(r"\b(?:в\s+этом\s+месяце|до\s+конца\s+месяца)\b", _I), "this_month"),
    (re.compile(r"\b(?:в|на)\s+следующ(?:ем|ий)\s+год[уа]?\b", _I), "next_year"),
    (re.compile(r"\b(?:в\s+этом\s+году|до\s+конца\s+года)\b", _I), "this_year"),
    (re.compile(r"\b(?:в|на)\s+(\d{4})\s*(?:год[уа]?|г\.?)(?=\W|$)", _I), "year"),
]
PERIOD_KIND = {
    "next_week": "week", "this_week": "week", "end_week": "week", "next_month": "month", "end_month": "month",
    "this_month": "month", "month": "month", "next_year": "year", "this_year": "year", "year": "year",
}
IDEA = re.compile(r"^\s*(?:💡\s*|иде[яи]\s*[:\-—]\s*)|\b(?:когда-нибудь|как-нибудь|при\s+случае)\b", _I)


@dataclass
class Parsed:
    text: str
    remind_at: datetime | None = None  # UTC
    repeat: str | None = None
    workhours_only: bool = False
    period: str | None = None  # week | month | year — план на период, а не на точное время
    idea: bool = False  # идея без срока: без напоминаний

    @property
    def found(self) -> bool:
        return bool(self.remind_at or self.repeat or self.workhours_only)


def _cut(text: str, m: re.Match) -> str:
    return text[: m.start()] + " " + text[m.end():]


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" ,.;:-–—").strip()


def _hour_with_part(h: int, part: str | None) -> int:
    part = (part or "").lower()
    if part in ("дня", "вечера") and h < 12:
        return h + 12
    if part == "ночи" and h == 12:
        return 0
    return h


def _month_num(word: str) -> int:
    word = word.lower()
    for stem, num in MONTH_IDX.items():
        if word.startswith(stem):
            return num
    raise ValueError(word)


def _first_workday(d: date, s: UserSettings) -> date:
    for _ in range(14):
        if is_workday(d, s):
            return d
        d += timedelta(days=1)
    return d


def _last_workday(d: date, s: UserSettings) -> date:
    for _ in range(14):
        if is_workday(d, s):
            return d
        d -= timedelta(days=1)
    return d


def _period(rest: str, today: date, s: UserSettings) -> tuple[date | None, str | None, str]:
    """Сроки-периоды: «на следующей неделе», «в ноябре», «в конце месяца», «в 2027 году»… → (дата, вид, остаток)."""
    tomorrow = today + timedelta(days=1)
    if m := DAY_MONTH.search(rest):  # «15 ноября» — это точная дата
        mo, y = _month_num(m.group(2)), m.group(3)
        try:
            d = date(int(y) if y else today.year, mo, int(m.group(1)))
            if not y and d < today:
                d = date(today.year + 1, mo, int(m.group(1)))
            return d, None, _cut(rest, m)
        except ValueError:
            pass
    for rx, code in PERIODS:
        if not (m := rx.search(rest)):
            continue
        monday = today - timedelta(days=today.weekday())
        if code == "next_week":
            d = _first_workday(monday + timedelta(days=7), s)
        elif code == "this_week":
            d = _first_workday(tomorrow, s)
        elif code == "end_week":
            d = _last_workday(monday + timedelta(days=6), s)
            if d < today:
                d = _last_workday(monday + timedelta(days=13), s)
        elif code == "next_month":
            y, mo = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            d = _first_workday(date(y, mo, 1), s)
        elif code == "end_month":
            d = _last_workday(date(today.year, today.month, calendar.monthrange(today.year, today.month)[1]), s)
            if d < today:
                y, mo = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
                d = _last_workday(date(y, mo, calendar.monthrange(y, mo)[1]), s)
        elif code in ("this_month", "this_year"):
            d = _first_workday(tomorrow, s)
        elif code == "next_year":
            d = _first_workday(date(today.year + 1, 1, 1), s)
        else:  # «в 2027 году»
            year = int(m.group(1))
            d = _first_workday(tomorrow if year == today.year else date(year, 1, 1), s)
            if year < today.year:
                continue
        return d, PERIOD_KIND[code], _cut(rest, m)
    if m := IN_MONTH.search(rest):
        mo, y = _month_num(m.group(1)), m.group(2)
        year = int(y) if y else today.year
        if not y and mo < today.month:
            year += 1
        if (year, mo) == (today.year, today.month):
            d = _first_workday(tomorrow, s)
        else:
            day1 = 1
            phrase = m.group(0).lower()
            if "конце" in phrase:
                day1 = calendar.monthrange(year, mo)[1]
                return _last_workday(date(year, mo, day1), s), "month", _cut(rest, m)
            if "середине" in phrase:
                day1 = 15
            d = _first_workday(date(year, mo, day1), s)
        return d, "month", _cut(rest, m)
    return None, None, rest


def parse(text: str, s: UserSettings, now: datetime | None = None) -> Parsed:
    now_local = (now.astimezone(zone(s.tz)) if now else local_now(s)).replace(second=0, microsecond=0)
    rest = text
    repeat = None
    workhours = False
    day: date | None = None
    tm: time | None = None
    weekday: int | None = None
    explicit_tz_dt: datetime | None = None
    delta: timedelta | None = None
    period: str | None = None
    idea = False

    if m := IDEA.search(rest):
        idea, rest = True, _cut(rest, m)
    if m := WORKHOURS.search(rest):
        workhours, rest = True, _cut(rest, m)
    for rx, rep in REPEATS:
        if m := rx.search(rest):
            repeat, rest = rep, _cut(rest, m)
            break

    if m := RELATIVE_HALF.search(rest):
        delta, rest = timedelta(minutes=30), _cut(rest, m)
    elif m := RELATIVE.search(rest):
        n = int(m.group(1) or 1)
        unit = m.group(2).lower()
        if unit.startswith("м"):
            delta = timedelta(minutes=n)
        elif unit.startswith("ч"):
            delta = timedelta(hours=n)
        elif unit.startswith("нед"):
            delta = timedelta(weeks=n)
        else:
            delta = timedelta(days=n)
        rest = _cut(rest, m)

    if delta is None:
        day, period, rest = _period(rest, now_local.date(), s)
        if day is None and (m := ISO.search(rest)):
            y, mo, d, hh, mm, tzs = m.groups()
            day = date(int(y), int(mo), int(d))
            if hh is not None:
                tm = time(int(hh), int(mm))
            if tzs and hh is not None:
                iso = m.group(0).replace(" ", "T").replace("Z", "+00:00")
                try:
                    explicit_tz_dt = datetime.fromisoformat(iso)
                except ValueError:
                    explicit_tz_dt = None
            rest = _cut(rest, m)
        elif day is None and (m := DMY.search(rest)):
            d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
            year = int(y) if y else now_local.year
            if year < 100:
                year += 2000
            try:
                day = date(year, mo, d)
                if not y and day < now_local.date():
                    day = date(year + 1, mo, d)
                rest = _cut(rest, m)
            except ValueError:
                day = None
        if day is None and (m := DAY_WORDS.search(rest)):
            word = m.group(1).lower()
            offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[word]
            day = now_local.date() + timedelta(days=offset)
            rest = _cut(rest, m)
        if day is None and (m := WEEKDAY.search(rest)):
            weekday = WEEKDAY_IDX[m.group(1).lower()]
            if m.group(0).lower().startswith("кажд"):
                repeat = repeat or "weekly"
            rest = _cut(rest, m)

        if tm is None:
            if m := TIME_V.search(rest):
                g = m.groups()
                if g[0] is not None:
                    h, mi, part = g[0], g[1], g[2]
                elif g[3] is not None:
                    h, mi, part = g[3], g[4], g[5]
                else:
                    h, mi, part = g[6], g[7], None
                hour = _hour_with_part(int(h), part)
                if hour <= 23 and int(mi or 0) <= 59:
                    tm = time(hour, int(mi or 0))
                    rest = _cut(rest, m)
            elif m := TIME_BARE.search(rest):
                if int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
                    tm = time(int(m.group(1)), int(m.group(2)))
                    rest = _cut(rest, m)
            if tm is None and (m := DAYPART.search(rest)):
                tm = DAYPART_TIME[m.group(1).lower()]
                rest = _cut(rest, m)

    remind_local: datetime | None = None
    if delta is not None:
        remind_local = now_local + delta
    elif explicit_tz_dt is not None:
        remind_local = explicit_tz_dt
    elif weekday is not None:
        ahead = (weekday - now_local.weekday()) % 7 or 7
        target = now_local.date() + timedelta(days=ahead)
        remind_local = datetime.combine(target, tm or s.work_start, tzinfo=now_local.tzinfo)
    elif day is not None:
        remind_local = datetime.combine(day, tm or s.work_start, tzinfo=now_local.tzinfo)
    elif tm is not None:
        remind_local = datetime.combine(now_local.date(), tm, tzinfo=now_local.tzinfo)
        if remind_local <= now_local:
            remind_local += timedelta(days=1)
    elif repeat:
        remind_local = datetime.combine(now_local.date(), s.work_start, tzinfo=now_local.tzinfo)
        if remind_local <= now_local:
            remind_local += timedelta(days=1)

    if idea and remind_local is None:
        repeat = None
    if remind_local is not None and repeat == "workdays":
        while not is_workday(remind_local.date(), s):
            remind_local += timedelta(days=1)

    cleaned = _clean(rest)
    return Parsed(
        text=cleaned or text.strip(),
        remind_at=remind_local.astimezone(timezone.utc) if remind_local else None,
        repeat=repeat,
        workhours_only=workhours,
        period=period,
        idea=idea and remind_local is None,
    )


def parse_when(value: str, s: UserSettings, now: datetime | None = None) -> datetime:
    """Строгий разбор момента времени (ISO или фраза). Бросает ValueError, если не понятно."""
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return to_utc(dt, s)
    except ValueError:
        pass
    p = parse(value, s, now)
    if p.remind_at is None:
        raise ValueError(f"Не понял время: {value!r}. Используйте ISO 8601 или фразу вроде «завтра в 10:00».")
    return p.remind_at
