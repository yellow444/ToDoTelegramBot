"""Часовые пояса, рабочий день и повторы."""

import calendar
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import UserSettings

WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def to_local(dt: datetime, s: UserSettings) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(zone(s.tz))


def to_utc(dt: datetime, s: UserSettings) -> datetime:
    """Наивное время считаем локальным временем пользователя."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone(s.tz))
    return dt.astimezone(timezone.utc)


def local_now(s: UserSettings) -> datetime:
    return datetime.now(zone(s.tz))


def fmt_local(dt: datetime | None, s: UserSettings, with_weekday: bool = True) -> str:
    if dt is None:
        return ""
    loc = to_local(dt, s)
    today = local_now(s).date()
    if loc.date() == today:
        day = "сегодня"
    elif loc.date() == today + timedelta(days=1):
        day = "завтра"
    else:
        day = loc.strftime("%d.%m")
        if loc.year != today.year:
            day += loc.strftime(".%Y")
        if with_weekday:
            day += f" {WEEKDAYS_SHORT[loc.weekday()]}"
    return f"{day} {loc:%H:%M}"


def parse_workdays(spec: str) -> list[int]:
    """'1-5' / '1,2,3,4,5' / '1-5,7' → [0..4] (0=Пн)."""
    days: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            days.update(range(a, b + 1))
        else:
            days.add(int(part))
    return sorted(d - 1 for d in days if 1 <= d <= 7)


def parse_hhmm(value: str) -> time:
    h, _, m = value.strip().partition(":")
    return time(int(h), int(m or 0))


def is_workday(d: date, s: UserSettings) -> bool:
    return d.weekday() in s.workdays


def in_work_hours(dt: datetime, s: UserSettings) -> bool:
    loc = to_local(dt, s)
    return is_workday(loc.date(), s) and s.work_start <= loc.time() < s.work_end


def next_work_start(dt: datetime, s: UserSettings) -> datetime:
    """Ближайшее начало рабочего дня строго после dt (UTC на выходе)."""
    loc = to_local(dt, s)
    d = loc.date()
    for _ in range(15):
        start = datetime.combine(d, s.work_start, tzinfo=loc.tzinfo)
        if is_workday(d, s) and start > loc:
            return start.astimezone(timezone.utc)
        d += timedelta(days=1)
    return (loc + timedelta(days=1)).astimezone(timezone.utc)


def shift_into_work_hours(dt: datetime, s: UserSettings) -> datetime:
    return dt if in_work_hours(dt, s) else next_work_start(dt, s)


def _add_month(d: datetime) -> datetime:
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    day = min(d.day, calendar.monthrange(y, m)[1])
    return d.replace(year=y, month=m, day=day)


def next_occurrence(prev: datetime, repeat: str, s: UserSettings, after: datetime | None = None) -> datetime:
    """Следующее срабатывание повторяющейся задачи после `after` (по умолчанию — сейчас)."""
    after = after or datetime.now(timezone.utc)
    loc = to_local(prev, s)
    for _ in range(400):
        if repeat == "daily":
            loc = loc + timedelta(days=1)
        elif repeat == "workdays":
            loc = loc + timedelta(days=1)
            while not is_workday(loc.date(), s):
                loc += timedelta(days=1)
        elif repeat == "weekly":
            loc = loc + timedelta(days=7)
        elif repeat == "monthly":
            loc = _add_month(loc)
        else:
            raise ValueError(f"Неизвестный повтор: {repeat}")
        # пересобираем с tz, чтобы корректно пережить переход на летнее время
        loc = loc.replace(tzinfo=None).replace(tzinfo=zone(s.tz))
        if loc.astimezone(timezone.utc) > after:
            return loc.astimezone(timezone.utc)
    raise ValueError("Не удалось вычислить следующее срабатывание")
