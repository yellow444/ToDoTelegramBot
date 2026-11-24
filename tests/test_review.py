"""/review: обзор, чистка ботом и разбор задач по одной."""

from datetime import timedelta
from types import SimpleNamespace

from todobot.bot import handlers, review
from todobot.models import utcnow

from .conftest import CHAT
from .test_bot import FakeBot, callback_update, make_context


class OldMessagesBot(FakeBot):
    """Telegram не даёт удалять сообщения старше 48 ч: удаление падает, остаётся «свернуть»."""

    def __init__(self, undeletable=()):
        super().__init__()
        self.undeletable = set(undeletable)

    async def delete_message(self, chat_id, message_id):
        if message_id in self.undeletable:
            from telegram.error import BadRequest

            raise BadRequest("Message can't be deleted for everyone")
        await super().delete_message(chat_id, message_id)


async def seed(service):
    now = utcnow()
    overdue = await service.create(CHAT, "просроченная", when=now - timedelta(days=3), message_id=11)
    await service.repo.update_task(overdue.id, remind_at=None)
    stale = await service.create(CHAT, "давно без срока", message_id=12)
    await service.repo.update_task(stale.id, created_at=now - timedelta(days=10))
    fresh = await service.create(CHAT, "свежая без срока", message_id=13)
    planned = await service.create(CHAT, "на будущее", when=now + timedelta(days=2), message_id=14)
    orch = await service.create(CHAT, "☀️ Разбор утра", source="orchestrator", message_id=15)
    await service.repo.update_task(orch.id, status="done", done_at=now)
    old_done = await service.create(CHAT, "давно сделано", message_id=16)
    await service.repo.update_task(old_done.id, status="done", done_at=now - timedelta(days=20))
    return SimpleNamespace(overdue=overdue, stale=stale, fresh=fresh, planned=planned, orch=orch, old_done=old_done)


async def test_candidates(service):
    s = await seed(service)
    c = await service.review_candidates(CHAT)
    assert [t.id for t in c["triage"]] == [s.overdue.id, s.stale.id] or {t.id for t in c["triage"]} == {s.overdue.id, s.stale.id}
    assert c["reasons"][s.overdue.id] == "overdue" and c["reasons"][s.stale.id] == "stale"
    assert [t.id for t in c["service"]] == [s.orch.id] and [t.id for t in c["done_old"]] == [s.old_done.id]
    text, markup = review.summary(c)
    assert "служебные карточки" in text and "Нужно ваше решение:</b> 2" in text
    assert [row[0].callback_data for row in markup.inline_keyboard] == ["rv|-|clean", "rv|-|start", "rv|-|list"]


async def test_clean_deletes_or_collapses(service, settings):
    s = await seed(service)
    bot = OldMessagesBot(undeletable={15})
    ctx = make_context(service, settings, bot)
    upd, answers = callback_update("rv|-|clean", 900)
    upd.callback_query.edit_message_text = _noop
    await handlers.on_callback(upd, ctx)
    assert ("delete", 16) in bot.calls  # свежее удаляется
    assert any(c[0] == "edit_text" and c[1] == 15 for c in bot.calls)  # старое сворачивается
    assert (await service.get(s.orch.id)).message_id is None and (await service.get(s.old_done.id)).status == "done"
    assert "Убрано: 1" in answers[-1] and "свёрнуто" in answers[-1]


async def test_triage_one_by_one(service, settings):
    s = await seed(service)
    bot = FakeBot()
    ctx = make_context(service, settings, bot)
    await review.start_triage(ctx, CHAT)
    first = (await service.user(CHAT)).review_current
    sent = [c for c in bot.calls if c[0] == "send"][-1]
    assert "1/2" in sent[3].inline_keyboard[0][0].text

    # первая — «через неделю»
    upd, _ = callback_update(f"v|{first}|week", 101)
    await handlers.on_callback(upd, ctx)
    t = await service.get(first)
    assert t.scheduled_at - utcnow() > timedelta(days=6)
    second = (await service.user(CHAT)).review_current
    assert second and second != first
    assert "2/2" in [c for c in bot.calls if c[0] == "send"][-1][3].inline_keyboard[0][0].text

    # вторая — обычной кнопкой карточки «Готово»: разбор тоже идёт дальше
    t2 = await service.get(second)
    upd, _ = callback_update(f"t|{second}|done", t2.message_id)
    await handlers.on_callback(upd, ctx)
    u = await service.user(CHAT)
    assert u.review_current is None and u.review_queue == []
    final = [c for c in bot.calls if c[0] == "send"][-1][2]
    assert "Разбор закончен" in final and "назначен срок — 1" in final and "сделано — 1" in final
    assert (await service.get(s.fresh.id)).status == "open"  # не трогали


async def test_triage_idea_and_delete(service, settings):
    await seed(service)
    bot = FakeBot()
    ctx = make_context(service, settings, bot)
    await review.start_triage(ctx, CHAT)
    first = (await service.user(CHAT)).review_current
    await handlers.on_callback(callback_update(f"v|{first}|idea", 101)[0], ctx)
    assert (await service.get(first)).idea
    second = (await service.user(CHAT)).review_current
    await handlers.on_callback(callback_update(f"v|{second}|del", 102)[0], ctx)
    assert not [t for t in await service.list_tasks(CHAT, None) if t.id == second]
    assert "удалено — 1" in [c for c in bot.calls if c[0] == "send"][-1][2]


async def _noop(*a, **kw):
    return None
