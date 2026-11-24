from datetime import timedelta

from todobot import tools
from todobot.bot import ui
from todobot.models import utcnow

from .conftest import CHAT


async def test_create_parses_text(service):
    t = await service.create(CHAT, "отчёт через 30 мин", parse_text=True)
    assert t.text == "отчёт"
    assert t.scheduled_at == t.remind_at
    assert abs((t.remind_at - utcnow()) - timedelta(minutes=30)) < timedelta(minutes=1)


async def test_nag_then_stop(service):
    t = await service.create(CHAT, "x", when=utcnow() - timedelta(seconds=1))
    assert [x.id for x in await service.due()] == [t.id]
    for expected_nag in (1, 2):
        t = await service.after_fired(t, message_id=10)
        assert t.nag_count == expected_nag and t.remind_at > utcnow()
        t = await service.repo.update_task(t.id, remind_at=utcnow())
    t = await service.after_fired(t, message_id=11)  # MAX_NAGS=2 исчерпан
    assert t.remind_at is None and t.status == "open"


async def test_complete_repeating_moves_forward(service):
    first = utcnow() + timedelta(hours=1)
    t = await service.create(CHAT, "стендап", when=first, repeat="daily")
    t = await service.complete(t)
    assert t.status == "open"
    assert t.scheduled_at - first == timedelta(days=1)


async def test_repeating_nag_does_not_drift_plan(service):
    plan = utcnow() - timedelta(minutes=1)
    t = await service.create(CHAT, "вода", when=plan, repeat="daily")
    t = await service.after_fired(t, 5)
    assert t.scheduled_at == plan and t.nag_count == 1
    t = await service.after_fired(t, 6)
    t = await service.after_fired(t, 7)  # пинки кончились → следующее плановое
    assert t.scheduled_at == plan + timedelta(days=1) and t.nag_count == 0


async def test_complete_and_reopen(service):
    t = await service.create(CHAT, "a", when=utcnow() + timedelta(hours=2))
    t = await service.complete(t)
    assert t.status == "done" and t.remind_at is None
    t = await service.reopen(t)
    assert t.status == "open" and t.remind_at == t.scheduled_at


async def test_tools_roundtrip(service):
    created = await tools.add_task("созвон завтра в 11")
    assert created["text"] == "созвон" and created["when_local"].endswith(":00 (" + created["when_local"][-3:-1] + ")")
    listed = await tools.list_tasks()
    assert [x["id"] for x in listed] == [created["id"]]
    task = await service.get(created["id"])
    assert task.card_dirty and task.source == "mcp"

    await service.repo.update_task(created["id"], message_id=42)
    await tools.delete_task(created["id"])
    assert await tools.list_tasks() == []
    assert service.repo.outbox == [{"chat_id": CHAT, "text": None, "delete_message_id": 42}]


async def test_tools_set_reminder_and_agenda(service):
    t = await tools.add_task("без срока", when=None)
    assert t["remind_at"] is None
    t = await tools.set_reminder(t["id"], "через 10 минут", repeat="workdays")
    assert t["repeat"] == "workdays" and t["remind_at"]
    agenda = await tools.get_agenda()
    assert {"overdue", "scheduled", "no_date", "done", "workday"} <= agenda.keys()


async def test_tools_unknown_task(service):
    import pytest

    with pytest.raises(ValueError):
        await tools.complete_task("nope")


def test_openai_schema_hides_chat_id():
    schema = tools.openai_schema(tools.add_task)["function"]
    assert "chat_id" not in schema["parameters"]["properties"]
    assert schema["parameters"]["required"] == ["text"]


async def test_callback_data_fits(service, user):
    t = await service.create(CHAT, "x")
    markups = [ui.card_markup(t, user), ui.when_menu(t, user), ui.repeat_menu(t), ui.calendar_markup(t, user)]
    for m in markups:
        for row in m.inline_keyboard:
            for b in row:
                assert b.callback_data is None or len(b.callback_data.encode()) <= 64


def test_presets(user):
    now = utcnow()
    assert ui.preset_time("off", user) is None
    assert abs(ui.preset_time("1h", user, now) - now - timedelta(hours=1)) < timedelta(seconds=1)
    assert ui.preset_time("tom", user, now) > now
