"""Обработчики и фоновые задачи бота на фейковом Bot (без сети)."""

from datetime import timedelta
from types import SimpleNamespace

from todobot.bot import handlers, jobs
from todobot.bot.handlers import Deps
from todobot.models import utcnow

from .conftest import CHAT


class FakeBot:
    def __init__(self):
        self.calls = []
        self._next_id = 100

    def _msg(self):
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id, chat_id=CHAT)

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("send", chat_id, text, kw.get("reply_markup")))
        return self._msg()

    async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
        self.calls.append(("copy", chat_id, message_id, kw.get("caption")))
        return self._msg()

    async def edit_message_text(self, text, chat_id, message_id, **kw):
        self.calls.append(("edit_text", message_id, text, kw.get("reply_markup")))

    async def edit_message_caption(self, chat_id, message_id, **kw):
        self.calls.append(("edit_caption", message_id, kw.get("caption")))

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kw):
        self.calls.append(("edit_markup", message_id, reply_markup))

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("delete", message_id))

    async def send_chat_action(self, *a, **kw):
        pass

    def kinds(self):
        return [c[0] for c in self.calls]


def make_context(service, settings, bot):
    app = SimpleNamespace(bot_data={"deps": Deps(settings, service, None)})
    return SimpleNamespace(application=app, bot=bot, args=[])


def text_update(text, message_id=1):
    msg = SimpleNamespace(
        text=text, caption=None, chat_id=CHAT, message_id=message_id,
        from_user=SimpleNamespace(is_bot=False, id=CHAT),
        **{k: None for k in ("photo", "video", "document", "audio", "voice", "animation", "sticker",
                             "location", "venue", "contact", "poll", "dice", "video_note")},
    )
    msg.replies = []

    async def reply_text(text, **kw):
        msg.replies.append(text)
        return SimpleNamespace(message_id=990 + len(msg.replies), chat_id=CHAT)

    msg.reply_text = reply_text
    return SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(id=CHAT), callback_query=None)


def callback_update(data, message_id):
    answers = []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    q = SimpleNamespace(data=data, message=SimpleNamespace(chat_id=CHAT, message_id=message_id), answer=answer)
    return SimpleNamespace(callback_query=q, effective_chat=SimpleNamespace(id=CHAT)), answers


async def test_message_becomes_card(service, settings):
    bot = FakeBot()
    ctx = make_context(service, settings, bot)
    await handlers.on_message(text_update("позвонить завтра в 11"), ctx)
    assert bot.kinds() == ["send", "delete"]
    _, _, text, markup = bot.calls[0]
    assert text.startswith("позвонить\n") and "завтра 11:00" in text
    [task] = await service.list_tasks(CHAT)
    assert task.message_id == 101
    assert markup.inline_keyboard[0][0].callback_data == f"t|{task.id}|done"


async def test_callbacks_done_and_reminder(service, settings):
    bot = FakeBot()
    ctx = make_context(service, settings, bot)
    await handlers.on_message(text_update("отчёт"), ctx)
    [task] = await service.list_tasks(CHAT)

    upd, answers = callback_update(f"s|{task.id}|1h", task.message_id)
    await handlers.on_callback(upd, ctx)
    task = await service.get(task.id)
    assert task.scheduled_at and answers[-1].startswith("Напомню")

    upd, answers = callback_update(f"t|{task.id}|done", task.message_id)
    await handlers.on_callback(upd, ctx)
    assert (await service.get(task.id)).status == "done"
    assert bot.calls[-1][0] == "edit_text" and "✅" in bot.calls[-1][2]


async def test_legacy_callback(service, settings):
    bot = FakeBot()
    ctx = make_context(service, settings, bot)
    t = await service.create(CHAT, "старое", message_id=55)
    upd, _ = callback_update("done", 55)
    await handlers.on_callback(upd, ctx)
    assert (await service.get(t.id)).status == "done"


async def test_reminder_tick_reposts_card(service, settings):
    bot = FakeBot()
    ctx = make_context(service, settings, bot)
    t = await service.create(CHAT, "пора", when=utcnow() - timedelta(seconds=5), message_id=7)
    await jobs.reminders_tick(ctx)
    assert bot.kinds() == ["send", "delete"]
    assert bot.calls[0][2].startswith("🔔 пора")
    snooze_row = bot.calls[0][3].inline_keyboard[1]
    assert snooze_row[0].callback_data == f"s|{t.id}|15m"
    t = await service.get(t.id)
    assert t.message_id == 101 and t.nag_count == 1 and t.remind_at > utcnow()


async def test_sync_cards_and_outbox(service, settings):
    bot = FakeBot()
    ctx = make_context(service, settings, bot)
    t = await service.create(CHAT, "из MCP", post_card=True)
    await service.notify(CHAT, "сборка готова")
    await jobs.sync_cards(ctx)
    assert bot.kinds() == ["send", "send"]
    assert (await service.get(t.id)).card_dirty is False
    assert bot.calls[1][2] == "сборка готова"


def test_profile_limits():
    from todobot.bot.app import COMMANDS, DESCRIPTION, SHORT_DESCRIPTION

    assert len(DESCRIPTION) <= 512 and len(SHORT_DESCRIPTION) <= 120
    assert all(len(d) <= 256 and n.islower() for n, d in COMMANDS)


async def test_goto_button_reposts_card(service, settings):
    bot = FakeBot()
    ctx = make_context(service, settings, bot)
    t = await service.create(CHAT, "найти меня", message_id=40)
    from todobot.bot.handlers import agenda_markup

    markup = agenda_markup(await service.agenda(CHAT))
    assert markup.inline_keyboard[0][0].callback_data == f"g|{t.id}|show"
    upd, answers = callback_update(f"g|{t.id}|show", 300)
    await handlers.on_callback(upd, ctx)
    assert bot.kinds() == ["send", "delete"] and ("delete", 40) in bot.calls
    assert (await service.get(t.id)).message_id == 101 and "внизу" in answers[-1]
