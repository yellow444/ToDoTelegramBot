"""Медиа-задачи: распознавание, +24 ч по умолчанию, ссылки на задачи, ответы на карточки, оркестрация."""

import shutil
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from todobot import tools
from todobot.bot import extract, handlers, ui
from todobot.bot.handlers import Deps
from todobot.models import utcnow

from .conftest import CHAT
from .test_bot import FakeBot, text_update

SAMPLE = Path(__file__).parent / "data" / "sample.mp4"


class MediaBot(FakeBot):
    async def get_file(self, file_id):
        async def download_to_drive(path):
            shutil.copy(SAMPLE, path)

        return SimpleNamespace(file_size=SAMPLE.stat().st_size, download_to_drive=download_to_drive)


class FakeSTT:
    enabled = True
    provider = "local"

    def __init__(self, text):
        self.text = text
        self.calls = 0
        self.hints = []

    async def transcribe(self, path, hint=""):
        self.calls += 1
        self.hints.append(hint)
        assert Path(path).exists()
        return self.text


def ctx_with(service, settings, bot, stt=None, created=None):
    deps = Deps(settings, service, None, stt, None)

    def create_task(coro, update=None):
        created.append(coro)

    app = SimpleNamespace(bot_data={"deps": deps}, create_task=create_task)
    return SimpleNamespace(application=app, bot=bot, args=[])


def voice_update(caption=None, message_id=5):
    upd = text_update(None, message_id)
    msg = upd.effective_message
    msg.text = None
    msg.caption = caption
    msg.voice = SimpleNamespace(file_id="VOICE1", duration=3, mime_type="audio/ogg")
    return upd


async def test_text_task_defaults_to_24h(service, settings):
    t = await service.create(CHAT, "просто дело", parse_text=True, default_remind=True)
    assert abs(t.scheduled_at - utcnow() - timedelta(hours=24)) < timedelta(minutes=1)
    t2 = await service.create(CHAT, "дело завтра в 10", parse_text=True, default_remind=True)
    assert t2.scheduled_at - utcnow() < timedelta(hours=24)


async def test_voice_is_transcribed_into_task(service, settings):
    bot, created = MediaBot(), []
    stt = FakeSTT("Позвонить в банк через 2 часа")
    ctx = ctx_with(service, settings, bot, stt, created)
    await handlers.on_message(voice_update(), ctx)

    [task] = await service.list_tasks(CHAT)
    assert task.media_type == "voice" and task.file_id == "VOICE1" and task.extracted_kind == "pending"
    assert task.scheduled_at is None  # время ждём из расшифровки
    assert bot.calls[0][0] == "copy" and "распознаю" in bot.calls[0][3]

    await created[0]  # фоновая обработка
    task = await service.get(task.id)
    assert stt.calls == 1 and task.extracted_kind == "transcript"
    assert task.text == "Позвонить в банк" and task.extracted_text == "Позвонить в банк через 2 часа"
    assert abs(task.scheduled_at - utcnow() - timedelta(hours=2)) < timedelta(minutes=1)
    edit = [c for c in bot.calls if c[0] == "edit_caption"][-1]
    assert "Позвонить в банк" in edit[2] and f"#{task.id[:6]}" in edit[2]


async def test_voice_with_caption_keeps_both(service, settings):
    bot, created = MediaBot(), []
    ctx = ctx_with(service, settings, bot, FakeSTT("детали: купить молоко и хлеб"), created)
    await handlers.on_message(voice_update(caption="Магазин"), ctx)
    await created[0]
    [task] = await service.list_tasks(CHAT)
    assert task.text == "Магазин" and task.caption_text == "Магазин"
    caption = [c for c in bot.calls if c[0] == "edit_caption"][-1][2]
    assert "Магазин" in caption and "Расшифровка" in caption and "купить молоко" in caption
    assert abs(task.scheduled_at - utcnow() - timedelta(hours=24)) < timedelta(minutes=1)


async def test_failed_recognition_marks_card(service, settings):
    class Broken(FakeSTT):
        async def transcribe(self, path, hint=""):
            raise RuntimeError("нет модели")

    bot, created = MediaBot(), []
    ctx = ctx_with(service, settings, bot, Broken(""), created)
    await handlers.on_message(voice_update(), ctx)
    await created[0]
    [task] = await service.list_tasks(CHAT)
    assert task.extracted_kind == "failed" and task.scheduled_at is not None
    assert not [c for c in bot.calls if c[0] == "send"]  # без лишних сообщений в чат
    assert "не распознано" in [c for c in bot.calls if c[0] == "edit_caption"][-1][2]


async def test_long_transcript_goes_to_reply(service, settings, user):
    t = await service.create(CHAT, "x", kind="media", caption_text="Созвон", message_id=10)
    t = await service.apply_extracted(t, "слово " * 400, "transcript")
    assert ui.card_truncated(t)
    assert len(ui.render_card(t, user)) <= ui.CAPTION_LIMIT
    parts = ui.extracted_message(t, user)
    assert parts[0].startswith(f"📝 Расшифровка #{t.id[:6]}") and "слово" in parts[-1]


async def test_resolve_refs(service):
    t = await service.create(CHAT, "отчёт", message_id=77)
    assert (await service.resolve(CHAT, t.id)).id == t.id
    assert (await service.resolve(CHAT, f"#{t.id[:6]}")).id == t.id
    assert (await service.resolve(CHAT, "77")).id == t.id
    got = await tools.get_task(f"#{t.id[:6]}")
    assert got["id"] == t.id and "отчёт" in got["context"]


async def test_reply_to_card_reschedules_without_llm(service, settings):
    bot = FakeBot()
    ctx = ctx_with(service, settings, bot, None, [])
    t = await service.create(CHAT, "отчёт", message_id=50)
    upd = text_update("завтра в 15:00", message_id=51)
    upd.effective_message.reply_to_message = SimpleNamespace(message_id=50)
    await handlers.on_message(upd, ctx)
    t = await service.get(t.id)
    assert t.scheduled_at and len(await service.list_tasks(CHAT)) == 1  # новая задача не создана


async def test_orchestration_goes_to_agent_queue(service):
    assert await service.enqueue_orchestration(CHAT, "morning") is None  # не включено
    await service.update_user(CHAT, orchestrator="claude")
    morning = await service.enqueue_orchestration(CHAT, "morning")
    job = await service.enqueue_orchestration(CHAT, "evening")
    assert job.agent == "claude" and "Вечерний разбор" in job.prompt
    assert job.task_id is None and job.title == "🌇 Разбор вечера"  # без карточки в чате
    assert not await service.list_tasks(CHAT, None)
    assert (await service.repo.get_job(morning.id)).status == "cancelled"  # невзятый старый заменён
    await service.claim_job(["claude"], "chat")
    done = await service.finish_job(job.id, True, "итоги")
    assert done.status == "done" and done.notify == "finished"


async def test_stale_orchestration_is_cancelled_quietly(service):
    await service.update_user(CHAT, orchestrator="claude")
    job = await service.enqueue_orchestration(CHAT, "morning")
    await service.repo.update_job(job.id, created_at=utcnow() - timedelta(hours=13))
    await service.fail_stale_jobs()
    old = await service.repo.get_job(job.id)
    assert old.status == "cancelled" and old.notify == "none"  # без сообщения «не получилось»


async def test_worker_online_status(service, settings):
    assert not service.worker_online("claude")
    service.worker_seen["claude"] = utcnow()
    assert service.worker_online("claude")
    service.worker_seen["claude"] = utcnow() - timedelta(minutes=5)
    assert not service.worker_online("claude")


@pytest.mark.skipif(not SAMPLE.exists(), reason="нет тестового видео")
def test_extract_voice_from_video():
    from todobot.media import decode_pcm16k, extract_voice

    data, dur = extract_voice(SAMPLE)
    assert data[:4] == b"OggS" and dur == 2
    assert decode_pcm16k(SAMPLE).shape[0] > 30000
    assert extract.needs_extraction(SimpleNamespace(media_type="document", mime="video/mp4"))


async def test_vocabulary_hint_and_command(service, settings):
    bot, created = MediaBot(), []
    stt = FakeSTT("созвон с Ревелем")
    ctx = ctx_with(service, settings, bot, stt, created)
    ctx.args = ["Ревель,", "исполком,", "Kubernetes"]
    upd = text_update("/vocab")
    await handlers.vocab_command(upd, ctx)
    assert (await service.user(CHAT)).vocabulary == ["Ревель", "исполком", "Kubernetes"]
    await service.create(CHAT, "Разобраться с правами Kubernetes")
    await handlers.on_message(voice_update(), ctx)
    await created[0]
    assert "Ревель" in stt.hints[0] and "Разобраться с правами Kubernetes" in stt.hints[0]
    ctx.args = ["-исполком"]
    await handlers.vocab_command(upd, ctx)
    assert (await service.user(CHAT)).vocabulary == ["Ревель", "Kubernetes"]


async def test_llm_fix_is_conservative(service, settings):
    class FakeClient:
        def __init__(self, answer):
            self.answer = answer

        async def chat(self, messages, **kw):
            assert "Ревель" in messages[0]["content"]
            return {"content": self.answer}

    await service.update_user(CHAT, vocabulary=["Ревель"])
    d = Deps(settings, service, SimpleNamespace(client=FakeClient("Связаться с Ревелем по ПР")), None, None)
    assert await extract.llm_fix(d, CHAT, "Связаться с ревалем по ПР") == "Связаться с Ревелем по ПР"
    d.agent.client.answer = "Конечно! Вот исправленный текст с пояснениями и дополнительными деталями о встрече"
    assert await extract.llm_fix(d, CHAT, "Связаться с ревалем по ПР") == "Связаться с ревалем по ПР"


async def test_manual_transcript_reply(service, settings):
    bot = FakeBot()
    ctx = ctx_with(service, settings, bot, None, [])
    t = await service.create(CHAT, "Проверить как тонко донрок", kind="media", media_type="voice", message_id=60)
    t = await service.apply_extracted(t, "Проверить как тонко донрок", "transcript")
    upd = text_update("= Проверить хакатон Кадерон завтра в 12", message_id=61)
    upd.effective_message.reply_to_message = SimpleNamespace(message_id=60)
    await handlers.on_message(upd, ctx)
    t = await service.get(t.id)
    assert t.text == "Проверить хакатон Кадерон" and t.extracted_raw == "Проверить как тонко донрок"
    assert t.scheduled_at - utcnow() < timedelta(hours=48)
    assert ("delete", 61) in bot.calls and len(await service.list_tasks(CHAT)) == 1


async def test_edit_button_flow(service, settings):
    from .test_bot import callback_update

    bot = FakeBot()
    ctx = ctx_with(service, settings, bot, None, [])
    t = await service.create(CHAT, "Проверить Хакатон Каденрог", kind="media", media_type="voice", message_id=70)
    t = await service.apply_extracted(t, "Проверить Хакатон Каденрог", "transcript")

    upd, answers = callback_update(f"t|{t.id}|edit", 70)
    await handlers.on_callback(upd, ctx)
    prompt = [c for c in bot.calls if c[0] == "send"][-1]
    assert "Новый текст" in prompt[2] and type(prompt[3]).__name__ == "ForceReply"
    t = await service.get(t.id)
    assert t.edit_prompt_id

    reply = text_update("Проверить хакатон Кадерон", message_id=80)
    reply.effective_message.reply_to_message = SimpleNamespace(message_id=t.edit_prompt_id)
    await handlers.on_message(reply, ctx)
    t = await service.get(t.id)
    assert t.text == "Проверить хакатон Кадерон" and t.edit_prompt_id is None
    assert ("delete", 80) in bot.calls and len(await service.list_tasks(CHAT)) == 1


def test_split_heuristics():
    from todobot.split import looks_multi, split_list

    assert split_list("- купить хлеб\n- позвонить маме завтра в 10\n- отчёт") == [
        "купить хлеб", "позвонить маме завтра в 10", "отчёт",
    ]
    assert split_list("одна задача\nс переносом строки") == []
    assert looks_multi("Во-первых, позвонить Сергею, во-вторых, отправить отчёт юристам")
    assert not looks_multi("Поменять отчет юристов")


async def test_text_list_creates_several_tasks(service, settings):
    bot = FakeBot()
    ctx = ctx_with(service, settings, bot, None, [])
    await handlers.on_message(text_update("1. купить хлеб\n2. позвонить маме завтра в 10"), ctx)
    tasks = await service.list_tasks(CHAT)
    assert sorted(t.text for t in tasks) == ["купить хлеб", "позвонить маме"]
    assert all(t.message_id for t in tasks)


async def test_voice_with_several_tasks_is_split(service, settings):
    class SplitClient:
        model = "general35"

        async def chat(self, messages, **kw):
            return {"content": '{"tasks": [{"text": "Позвонить Сергею", "when": null},'
                               ' {"text": "Отправить отчёт юристам", "when": "2030-01-02T10:00"}]}'}

    class FakeAgent:
        client = SplitClient()

        async def _context(self, chat_id):
            return {"u": await service.user(chat_id), "now": "2026-10-09 18:00", "weekday": "Пт", "work_start": "09:00"}

    bot, created = MediaBot(), []
    ctx = ctx_with(service, settings, bot, FakeSTT("Во-первых, позвонить Сергею, во-вторых, отправить отчёт юристам второго января в 10"), created)
    ctx.application.bot_data["deps"].agent = FakeAgent()
    settings.STT_LLM_FIX = False
    await handlers.on_message(voice_update(), ctx)
    await created[0]
    tasks = {t.text: t for t in await service.list_tasks(CHAT)}
    assert set(tasks) == {"Позвонить Сергею", "Отправить отчёт юристам"}
    sub = tasks["Отправить отчёт юристам"]
    assert sub.source == "voice-split" and sub.scheduled_at.year == 2030
    voice = tasks["Позвонить Сергею"]
    assert voice.media_type == "voice" and voice.scheduled_at is not None


async def test_agenda_shows_upcoming(service, settings):
    from todobot.bot.handlers import agenda_text

    await service.create(CHAT, "дело на потом", when=utcnow() + timedelta(days=3))
    a = await service.agenda(CHAT)
    text = agenda_text(a, "📅")
    assert "На сегодня задач нет" in text and "Дальше" in text and "дело на потом" in text
    got = await tools.get_agenda()
    assert [t["text"] for t in got["upcoming"]] == ["дело на потом"]


async def test_add_tasks_plans_and_ideas(service, settings, user):
    res = await tools.add_tasks([
        {"text": "обновить резюме", "when": "на следующей неделе"},
        {"text": "спланировать отпуск", "when": "в декабре"},
        {"text": "бот для семьи", "idea": True},
        {"text": "стендап", "when": "в 10:00", "repeat": "workdays"},
    ])
    by = {r["text"]: r for r in res}
    assert by["обновить резюме"]["period"] == "week" and by["спланировать отпуск"]["period"] == "month"
    assert by["бот для семьи"]["idea"] and by["бот для семьи"]["remind_at"] is None
    assert by["стендап"]["repeat"] == "workdays"
    ideas = await tools.list_ideas()
    assert [i["text"] for i in ideas] == ["бот для семьи"]
    a = await service.agenda(CHAT)
    assert "бот для семьи" not in [t.text for t in a["undated"]]
    t = await service.get(by["спланировать отпуск"]["id"])
    assert "📆 декабрь" in ui.render_card(t, user)


async def test_idea_from_chat_has_no_default_reminder(service, settings):
    bot = FakeBot()
    ctx = ctx_with(service, settings, bot, None, [])
    await handlers.on_message(text_update("идея: сделать семейный календарь"), ctx)
    [t] = await service.list_tasks(CHAT)
    assert t.idea and t.scheduled_at is None and t.text == "сделать семейный календарь"
    assert "💡 идея" in bot.calls[0][2]


async def test_long_text_plan_is_split(service, settings):
    class PlanClient:
        model = "general35"

        async def chat(self, messages, **kw):
            return {"content": '{"tasks": [{"text": "Обновить резюме", "when": "на следующей неделе"},'
                               ' {"text": "Спланировать отпуск", "when": "в декабре"},'
                               ' {"text": "Семейный бот", "when": null, "idea": true}]}'}

    class FakeAgent:
        client = PlanClient()

        async def _context(self, chat_id):
            return {"u": await service.user(chat_id)}

    bot, created = FakeBot(), []
    ctx = ctx_with(service, settings, bot, None, created)
    ctx.application.bot_data["deps"].agent = FakeAgent()
    await handlers.on_message(text_update(
        "Значит так: на следующей неделе обновить резюме, потом в декабре спланировать отпуск, "
        "и ещё когда-нибудь сделать семейного бота"), ctx)
    assert created and not await service.list_tasks(CHAT)  # разбор идёт в фоне
    await created[0]
    tasks = {t.text: t for t in await service.list_tasks(CHAT)}
    assert set(tasks) == {"Обновить резюме", "Спланировать отпуск", "Семейный бот"}
    assert tasks["Обновить резюме"].period == "week" and tasks["Семейный бот"].idea
