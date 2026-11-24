"""Инструменты управления задачами — общие для MCP-сервера и LLM-агента.

Каждая функция асинхронная, возвращает JSON-совместимые данные и при ошибке бросает ValueError.
Пользователь определяется так: явный chat_id → контекст LLM-диалога → OWNER_CHAT_ID.
"""

import inspect
from contextvars import ContextVar
from datetime import date, datetime
from typing import Any, Literal

from pydantic import create_model

from .models import AGENT_LABELS, JOB_STATUS_LABELS, REPEAT_LABELS, AgentJob, Task, UserSettings
from .service import KEEP, TaskNotFound, TaskService
from .timeparse import parse_when
from .timeutil import WEEKDAYS_SHORT, local_now, to_local

current_chat_id: ContextVar[int | None] = ContextVar("current_chat_id", default=None)
_service: TaskService | None = None

RepeatArg = Literal["none", "daily", "workdays", "weekly", "monthly"]
AgentArg = Literal["claude", "codex"]


def bind(service: TaskService) -> None:
    global _service
    _service = service


def svc() -> TaskService:
    if _service is None:
        raise RuntimeError("tools.bind(service) не вызван")
    return _service


async def _chat(chat_id: int | None) -> int:
    return chat_id or current_chat_id.get() or await svc().default_chat_id()


def _task_view(t: Task, u: UserSettings) -> dict:
    d = t.public()
    if t.scheduled_at:
        loc = to_local(t.scheduled_at, u)
        d["when_local"] = f"{loc:%Y-%m-%d %H:%M} ({WEEKDAYS_SHORT[loc.weekday()]})"
    if t.repeat:
        d["repeat_human"] = REPEAT_LABELS[t.repeat]
    return d


async def _task(task_id: str, chat_id: int | None) -> tuple[Task, UserSettings]:
    """task_id: полный id, «#префикс» (как на карточке) или номер сообщения-карточки."""
    cid = await _chat(chat_id)
    try:
        t = await svc().resolve(cid, task_id)
    except TaskNotFound:
        raise ValueError(f"Задача {task_id} не найдена") from None
    return t, await svc().user(cid)


def task_context(t: Task, u: UserSettings) -> str:
    """Текстовое описание задачи для LLM/агентов: текст, подпись, расшифровка, время, вложение."""
    lines = [f"Задача #{t.id} (сообщение-карточка {t.message_id}): {t.text}"]
    if t.caption_text and t.caption_text != t.text:
        lines.append(f"Подпись пользователя: {t.caption_text}")
    if t.extracted_text:
        label = "Расшифровка звука" if t.extracted_kind == "transcript" else "Текст с картинки"
        lines.append(f"{label}: {t.extracted_text}")
    if t.scheduled_at:
        loc = to_local(t.scheduled_at, u)
        lines.append(f"Напоминание: {loc:%Y-%m-%d %H:%M} ({WEEKDAYS_SHORT[loc.weekday()]})")
    if t.repeat:
        lines.append(f"Повтор: {REPEAT_LABELS[t.repeat]}")
    if t.media_type:
        name = f" «{t.file_name}»" if t.file_name else ""
        lines.append(f"Вложение: {t.media_type}{name} — скачать: todo.download_attachment('{t.id}')")
    lines.append(f"Статус: {t.status}")
    return "\n".join(lines)


def _when(value: str | None, u: UserSettings) -> datetime | None:
    if value is None or not value.strip() or value.strip().lower() in ("none", "null", "нет"):
        return None
    return parse_when(value, u)


# ---------------------------------------------------------------- инструменты

async def list_tasks(status: Literal["open", "done", "all"] = "open", limit: int = 50, chat_id: int | None = None) -> list[dict]:
    """Список задач пользователя. status: open (по умолчанию), done или all. Отсортировано по времени напоминания."""
    cid = await _chat(chat_id)
    u = await svc().user(cid)
    tasks = await svc().list_tasks(cid, None if status == "all" else status, limit)
    return [_task_view(t, u) for t in tasks]


async def search_tasks(query: str, include_done: bool = False, chat_id: int | None = None) -> list[dict]:
    """Найти задачи по подстроке в тексте (без учёта регистра)."""
    cid = await _chat(chat_id)
    u = await svc().user(cid)
    q = query.lower().strip()
    tasks = await svc().list_tasks(cid, None if include_done else "open", 500)
    return [_task_view(t, u) for t in tasks if q in t.text.lower()]


async def get_agenda(day: str | None = None, chat_id: int | None = None) -> dict:
    """План на день: просроченные, запланированные на день, ближайшие (upcoming), без срока, сделанное за день
    и рабочий график.
    day — дата YYYY-MM-DD или фраза («завтра»); по умолчанию сегодня."""
    cid = await _chat(chat_id)
    u = await svc().user(cid)
    d: date | None = None
    if day:
        try:
            d = date.fromisoformat(day.strip())
        except ValueError:
            d = to_local(parse_when(day, u), u).date()
    a = await svc().agenda(cid, d)
    done = await svc().done_on(cid, a["day"])
    return {
        "day": a["day"].isoformat(),
        "now_local": local_now(u).strftime("%Y-%m-%d %H:%M"),
        "workday": u.public(),
        "overdue": [_task_view(t, u) for t in a["overdue"]],
        "scheduled": [_task_view(t, u) for t in a["today"]],
        "no_date": [_task_view(t, u) for t in a["undated"]],
        "upcoming": [_task_view(t, u) for t in a["later"][:30]],
        "done": [_task_view(t, u) for t in done],
    }


async def add_task(
    text: str,
    when: str | None = None,
    repeat: RepeatArg = "none",
    workhours_only: bool = False,
    chat_id: int | None = None,
) -> dict:
    """Создать задачу и отправить её карточку в Telegram.
    when — ISO 8601 (2026-10-09T10:00, без зоны = местное время пользователя) или фраза «завтра в 10», «через 30 мин».
    Если when не указан, время ищется в самом тексте; «идея: …»/«когда-нибудь» — идея без срока.
    Сроки-периоды тоже понимаются: «на следующей неделе», «в ноябре», «в 2027 году». repeat: none|daily|workdays|weekly|monthly.
    workhours_only — напоминать только в рабочие часы."""
    cid = await _chat(chat_id)
    u = await svc().user(cid)
    source = "llm" if current_chat_id.get() else "mcp"
    t = await svc().create(
        cid,
        text,
        when=_when(when, u),
        repeat=None if repeat == "none" else repeat,
        workhours_only=workhours_only,
        source=source,
        parse_text=when is None,
        post_card=True,
    )
    return _task_view(t, u)


async def add_tasks(items: list[dict], chat_id: int | None = None) -> list[dict]:
    """Добавить сразу много задач — для планов на дни, недели, месяцы, год и списков идей.
    items: [{"text": "что сделать", "when": "срок словами или ISO: «завтра в 10», «на следующей неделе»,
    «в ноябре», «в конце месяца», «в 2027 году», «15 ноября» (можно null)", "idea": true — идея без срока,
    "repeat": "none|daily|workdays|weekly|monthly"}]. Срок «в ноябре» ставит задачу на начало месяца
    и показывается как «📆 ноябрь». Карточки появятся в Telegram."""
    from .split import item_phrase

    cid = await _chat(chat_id)
    u = await svc().user(cid)
    source = "llm" if current_chat_id.get() else "mcp"
    out = []
    for item in items[:100]:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        rep = item.get("repeat")
        t = await svc().create(
            cid,
            item_phrase({"text": str(item["text"]), "when": item.get("when"), "idea": bool(item.get("idea"))}),
            repeat=None if rep in (None, "none") else rep,
            parse_text=True,
            post_card=True,
            source=source,
        )
        out.append(_task_view(t, u))
    return out


async def list_ideas(chat_id: int | None = None) -> list[dict]:
    """Идеи без срока (их можно потом запланировать через set_reminder)."""
    cid = await _chat(chat_id)
    u = await svc().user(cid)
    return [_task_view(t, u) for t in await svc().list_tasks(cid, "open", 500) if t.idea]


async def complete_task(task_id: str, chat_id: int | None = None) -> dict:
    """Отметить задачу выполненной (повторяющаяся переедет на следующее срабатывание)."""
    t, u = await _task(task_id, chat_id)
    return _task_view(await svc().complete(t, touch_card=True), u)


async def reopen_task(task_id: str, chat_id: int | None = None) -> dict:
    """Вернуть выполненную задачу в работу."""
    t, u = await _task(task_id, chat_id)
    return _task_view(await svc().reopen(t, touch_card=True), u)


async def delete_task(task_id: str, chat_id: int | None = None) -> dict:
    """Удалить задачу вместе с карточкой в чате."""
    t, _ = await _task(task_id, chat_id)
    await svc().delete(t, delete_card=True)
    return {"deleted": t.id, "text": t.text}


async def edit_task(task_id: str, text: str, chat_id: int | None = None) -> dict:
    """Изменить текст задачи."""
    t, u = await _task(task_id, chat_id)
    return _task_view(await svc().edit_text(t, text, touch_card=True), u)


async def set_reminder(
    task_id: str,
    when: str | None,
    repeat: Literal["keep", "none", "daily", "workdays", "weekly", "monthly"] = "keep",
    workhours_only: bool | None = None,
    chat_id: int | None = None,
) -> dict:
    """Назначить/изменить напоминание. when — ISO 8601 или фраза; пусто/none — снять напоминание.
    repeat: keep (не менять) | none | daily | workdays | weekly | monthly."""
    t, u = await _task(task_id, chat_id)
    rep = KEEP if repeat == "keep" else (None if repeat == "none" else repeat)
    t = await svc().set_reminder(t, _when(when, u), repeat=rep, workhours_only=workhours_only, touch_card=True)
    return _task_view(t, u)


async def snooze_task(task_id: str, minutes: int = 30, chat_id: int | None = None) -> dict:
    """Отложить напоминание на N минут."""
    t, u = await _task(task_id, chat_id)
    return _task_view(await svc().snooze(t, minutes, touch_card=True), u)


async def get_settings(chat_id: int | None = None) -> dict:
    """Настройки пользователя: часовой пояс, рабочие часы и дни (1=Пн..7=Вс), дайджесты."""
    cid = await _chat(chat_id)
    u = await svc().user(cid)
    return {
        **u.public(),
        "now_local": local_now(u).strftime("%Y-%m-%d %H:%M"),
        "agent_workdirs": list(svc().settings.workdirs),
    }


async def update_settings(
    work_start: str | None = None,
    work_end: str | None = None,
    workdays: str | None = None,
    tz: str | None = None,
    morning_digest: bool | None = None,
    evening_review: bool | None = None,
    chat_id: int | None = None,
) -> dict:
    """Изменить рабочий график. work_start/work_end — HH:MM; workdays — «1-5» или «1,2,3,4,5,6» (1=Пн);
    tz — IANA, напр. Europe/Moscow; morning_digest/evening_review — утренний план и вечерний итог."""
    cid = await _chat(chat_id)
    fields = {
        k: v
        for k, v in dict(
            work_start=work_start, work_end=work_end, workdays=workdays, tz=tz,
            morning_digest=morning_digest, evening_review=evening_review,
        ).items()
        if v is not None
    }
    u = await svc().update_user(cid, **fields)
    return u.public()


async def notify(text: str, chat_id: int | None = None) -> dict:
    """Отправить пользователю сообщение в Telegram (например, «сборка завершена», «нужно ревью»)."""
    cid = await _chat(chat_id)
    await svc().notify(cid, text)
    return {"queued": True, "chat_id": cid}


async def get_task(task_ref: str, chat_id: int | None = None) -> dict:
    """Полные данные задачи: текст, исходная подпись, расшифровка голоса/видео или текст с картинки, время,
    вложение. task_ref — id, «#префикс» с карточки или номер сообщения-карточки в Telegram."""
    t, u = await _task(task_ref, chat_id)
    d = _task_view(t, u)
    d.update(
        caption_text=t.caption_text,
        extracted_text=t.extracted_text,
        file_name=t.file_name,
        mime=t.mime,
        context=task_context(t, u),
    )
    return d


async def download_attachment(task_ref: str, chat_id: int | None = None) -> dict:
    """Скачать вложение задачи (фото, голосовое, видео, документ) в общую папку и вернуть путь к файлу,
    чтобы его можно было открыть/обработать. Лимит Bot API — 20 МБ."""
    from pathlib import Path

    from telegram import Bot

    t, _ = await _task(task_ref, chat_id)
    if not t.file_id:
        raise ValueError(f"У задачи #{t.id[:6]} нет вложения")
    s = svc().settings
    folder = Path(s.FILES_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    name = t.file_name or f"{t.media_type}{_ext(t)}"
    local = folder / f"{t.id}-{name}"
    if not local.exists():
        async with Bot(s.TOKEN) as bot:
            f = await bot.get_file(t.file_id)
            await f.download_to_drive(local)
    host = str(Path(s.FILES_HOST_DIR) / local.name) if s.FILES_HOST_DIR else str(local.resolve())
    return {"task_id": t.id, "path": host, "size": local.stat().st_size, "mime": t.mime}


def _ext(t: Task) -> str:
    return {
        "photo": ".jpg", "voice": ".ogg", "video_note": ".mp4", "video": ".mp4", "audio": ".mp3", "animation": ".mp4",
    }.get(t.media_type or "", "")


# ---------------------------------------------------------------- агенты-коворкеры

def _job_view(j: AgentJob) -> dict:
    d = j.public()
    d["context_file"] = f"{svc().settings.AGENT_CONTEXT_DIR}/{j.agent}.md"
    d["status_human"] = JOB_STATUS_LABELS.get(j.status, j.status)
    d["workdir_path"] = svc().settings.workdirs.get(j.workdir) if j.workdir else (svc().settings.AGENT_HOME or None)
    return d


async def delegate_to_agent(
    text: str,
    agent: AgentArg = "claude",
    workdir: str | None = None,
    task_id: str | None = None,
    chat_id: int | None = None,
) -> dict:
    """Поручить сложную работу агенту-коворкеру (Claude Code или Codex на компьютере пользователя, по подписке):
    программирование, правки в репозиториях, исследование, многошаговое планирование. text — полное задание.
    workdir — алиас рабочей папки проекта (см. get_settings → agent_workdirs), пусто — без проекта.
    task_id — привязать к существующей задаче, иначе будет создана новая. Результат придёт в Telegram."""
    cid = await _chat(chat_id)
    task = None
    if task_id:
        task, _ = await _task(task_id, cid)
    source = "llm" if current_chat_id.get() else "mcp"
    t, job = await svc().delegate(cid, agent, text, workdir=workdir or None, task=task, source=source)
    return {"job": _job_view(job), "task_id": t.id, "queued_for": AGENT_LABELS[agent]}


async def list_agent_jobs(status: Literal["all", "queued", "running", "done", "failed"] = "all", limit: int = 10, chat_id: int | None = None) -> list[dict]:
    """Последние поручения агентам и их статусы/результаты."""
    cid = await _chat(chat_id)
    jobs = await svc().repo.list_jobs(cid, None if status == "all" else status, limit)
    return [_job_view(j) for j in jobs]


async def agent_inbox(agent: AgentArg, worker: str = "desktop") -> dict:
    """Для агента-коворкера: взять следующее задание из очереди (оно переходит в статус «работает»).
    Возвращает {"job": null}, если очередь пуста. После выполнения обязательно вызови agent_report."""
    job = await svc().claim_job([agent], worker)
    if job is None:
        return {"job": None}
    out = {"job": _job_view(job), "history": await svc().job_history(job)}
    if job.task_id and (task := await svc().repo.get_task(job.task_id)):
        out["task"] = task_context(task, await svc().user(task.chat_id))
    return out


async def agent_report(job_id: str, result: str, ok: bool = True, session_id: str | None = None) -> dict:
    """Для агента-коворкера: сдать результат задания. result — краткий отчёт для человека (что сделано,
    что проверить, вопросы). ok=false — если не получилось. Отчёт уйдёт пользователю в Telegram."""
    job = await svc().finish_job(job_id, ok, result, session_id)
    return {"job": _job_view(job)}


ALL_TOOLS = [
    list_tasks, search_tasks, get_agenda, add_task, add_tasks, list_ideas, complete_task, reopen_task, delete_task,
    edit_task, set_reminder, snooze_task, get_settings, update_settings, notify,
    get_task, download_attachment,
    delegate_to_agent, list_agent_jobs, agent_inbox, agent_report,
]
# LLM внутри бота и так общается с пользователем (notify не нужен) и сам задания не исполняет.
LLM_TOOLS = [t for t in ALL_TOOLS if t not in (notify, agent_inbox, agent_report, download_attachment)]


def openai_schema(fn) -> dict:
    """JSON-schema инструмента для OpenAI-совместимого API (без служебного chat_id)."""
    fields: dict[str, Any] = {}
    for name, p in inspect.signature(fn).parameters.items():
        if name == "chat_id":
            continue
        default = ... if p.default is inspect.Parameter.empty else p.default
        fields[name] = (p.annotation, default)
    schema = create_model(f"{fn.__name__}_args", **fields).model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return {
        "type": "function",
        "function": {"name": fn.__name__, "description": inspect.getdoc(fn) or "", "parameters": schema},
    }


async def call(name: str, args: dict) -> Any:
    fn = {f.__name__: f for f in ALL_TOOLS}.get(name)
    if fn is None:
        raise ValueError(f"Неизвестный инструмент {name}")
    args = {k: v for k, v in (args or {}).items() if k != "chat_id"}
    return await fn(**args)
