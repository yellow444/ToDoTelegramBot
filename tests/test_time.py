from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from todobot.timeparse import parse, parse_when
from todobot.timeutil import in_work_hours, next_occurrence, next_work_start, parse_workdays

MSK = ZoneInfo("Europe/Moscow")
# среда, 8 октября 2026, 14:00 по Москве
NOW = datetime(2026, 10, 8, 14, 0, tzinfo=MSK)


def local(dt):
    return dt.astimezone(MSK).replace(tzinfo=None) if dt else None


@pytest.mark.parametrize(
    "text, clean, expected, repeat",
    [
        ("позвонить маме завтра в 10", "позвонить маме", datetime(2026, 10, 9, 10, 0), None),
        ("отчёт через 2 часа", "отчёт", datetime(2026, 10, 8, 16, 0), None),
        ("чай через полчаса", "чай", datetime(2026, 10, 8, 14, 30), None),
        ("купить хлеб в 18:30", "купить хлеб", datetime(2026, 10, 8, 18, 30), None),
        ("созвон в 9:00", "созвон", datetime(2026, 10, 9, 9, 0), None),  # 9:00 уже прошло → завтра
        ("стендап по будням в 10:00", "стендап", datetime(2026, 10, 9, 10, 0), "workdays"),
        ("оплатить счёт 15.10 в 12", "оплатить счёт", datetime(2026, 10, 15, 12, 0), None),
        ("ретро в пятницу в 5 вечера", "ретро", datetime(2026, 10, 9, 17, 0), None),
        ("планёрка каждый понедельник в 11", "планёрка", datetime(2026, 10, 12, 11, 0), "weekly"),
        ("отчёт послезавтра", "отчёт", datetime(2026, 10, 10, 9, 0), None),
        ("дедлайн 2026-11-01T15:45", "дедлайн", datetime(2026, 11, 1, 15, 45), None),
        ("полить цветы вечером", "полить цветы", datetime(2026, 10, 8, 19, 0), None),
        ("просто заметка", "просто заметка", None, None),
        ("зайти в магазин", "зайти в магазин", None, None),
        ("увеличить в 3 раза", "увеличить в 3 раза", None, None),
        ("передать ТДД 1.5 Сергею", "передать ТДД 1.5 Сергею", None, None),
        ("релиз 2.10.3", "релиз 2.10.3", None, None),
        ("сдать 5.11.2026", "сдать", datetime(2026, 11, 5, 9, 0), None),
    ],
)
def test_parse(user, text, clean, expected, repeat):
    p = parse(text, user, NOW)
    assert p.text == clean
    assert local(p.remind_at) == expected
    assert p.repeat == repeat


def test_parse_workhours_flag(user):
    p = parse("ревью PR в рабочее время", user, NOW)
    assert p.workhours_only and p.text == "ревью PR"


def test_parse_when_iso_with_zone(user):
    dt = parse_when("2026-10-09T07:00:00Z", user)
    assert dt == datetime(2026, 10, 9, 7, 0, tzinfo=timezone.utc)


def test_parse_when_naive_is_local(user):
    assert local(parse_when("2026-10-09T10:00", user)) == datetime(2026, 10, 9, 10, 0)


def test_parse_when_garbage(user):
    with pytest.raises(ValueError):
        parse_when("когда-нибудь", user)


def test_workdays_spec():
    assert parse_workdays("1-5") == [0, 1, 2, 3, 4]
    assert parse_workdays("1,3,5-7") == [0, 2, 4, 5, 6]


def test_work_hours(user):
    assert in_work_hours(NOW, user)
    friday_evening = datetime(2026, 10, 9, 19, 0, tzinfo=MSK)
    assert not in_work_hours(friday_evening, user)
    assert local(next_work_start(friday_evening, user)) == datetime(2026, 10, 12, 9, 0)  # понедельник


def test_next_occurrence(user):
    fri = datetime(2026, 10, 9, 10, 0, tzinfo=MSK)
    assert local(next_occurrence(fri, "workdays", user, after=fri)) == datetime(2026, 10, 12, 10, 0)
    assert local(next_occurrence(fri, "daily", user, after=fri)) == datetime(2026, 10, 10, 10, 0)
    jan31 = datetime(2027, 1, 31, 9, 0, tzinfo=MSK)
    assert local(next_occurrence(jan31, "monthly", user, after=jan31)) == datetime(2027, 2, 28, 9, 0)
    # пропущенные срабатывания не догоняем: следующее — после «сейчас»
    old = datetime(2026, 10, 1, 10, 0, tzinfo=MSK)
    assert local(next_occurrence(old, "daily", user, after=NOW)) == datetime(2026, 10, 9, 10, 0)


def test_user_defaults(user):
    assert user.work_start == time(9) and user.workdays == [0, 1, 2, 3, 4]


@pytest.mark.parametrize(
    "text, clean, expected, period, idea",
    [
        ("обновить резюме на следующей неделе", "обновить резюме", datetime(2026, 10, 12, 9, 0), "week", False),
        ("отпуск спланировать в декабре", "отпуск спланировать", datetime(2026, 12, 1, 9, 0), "month", False),
        ("налоги в конце месяца", "налоги", datetime(2026, 10, 30, 9, 0), "month", False),
        ("отчёт в конце недели", "отчёт", datetime(2026, 10, 9, 9, 0), "week", False),
        ("купить машину в 2027 году", "купить машину", datetime(2027, 1, 1, 9, 0), "year", False),
        ("выучить испанский в следующем году", "выучить испанский", datetime(2027, 1, 1, 9, 0), "year", False),
        ("сдать проект в середине ноября", "сдать проект", datetime(2026, 11, 16, 9, 0), "month", False),
        ("день рождения мамы 15 ноября в 12", "день рождения мамы", datetime(2026, 11, 15, 12, 0), None, False),
        ("идея: бот для семьи", "бот для семьи", None, None, True),
        ("сходить в театр когда-нибудь", "сходить в театр", None, None, True),
    ],
)
def test_periods_and_ideas(user, text, clean, expected, period, idea):
    p = parse(text, user, NOW)  # среда 08.10.2026
    assert (p.text, local(p.remind_at), p.period, p.idea) == (clean, expected, period, idea)
