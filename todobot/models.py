"""Доменные модели: задача и настройки пользователя."""

import secrets
from datetime import date, datetime, time, timezone
from typing import Literal

from pydantic import BaseModel, Field

Repeat = Literal["daily", "workdays", "weekly", "monthly"]
UiMode = Literal["buttons", "webapp"]
Agent = Literal["claude", "codex"]
AGENT_LABELS: dict[str, str] = {"claude": "Claude", "codex": "Codex"}
JOB_STATUS_LABELS: dict[str, str] = {
    "queued": "в очереди",
    "running": "работает",
    "done": "готово",
    "failed": "ошибка",
    "cancelled": "отменено",
}
REPEAT_LABELS: dict[str, str] = {
    "daily": "каждый день",
    "workdays": "по будням",
    "weekly": "каждую неделю",
    "monthly": "каждый месяц",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    # Короткий id, чтобы влезать в 64 байта callback_data.
    return secrets.token_hex(6)


class Task(BaseModel):
    id: str = Field(default_factory=new_id)
    chat_id: int
    text: str
    status: Literal["open", "done"] = "open"
    created_at: datetime = Field(default_factory=utcnow)
    done_at: datetime | None = None
    scheduled_at: datetime | None = None  # UTC, плановое время (его видит пользователь, от него считаются повторы)
    remind_at: datetime | None = None  # UTC, ближайшее срабатывание (с учётом повторных «пинков»)
    repeat: Repeat | None = None
    workhours_only: bool = False  # напоминать только в рабочее время
    period: Literal["week", "month", "year"] | None = None  # план на неделю/месяц/год (а не точное время)
    idea: bool = False  # идея без срока — без напоминаний, отдельный список /ideas
    nag_count: int = 0
    source: str = "telegram"  # telegram | webapp | mcp | llm
    # Telegram-карточка задачи: сообщение с кнопками.
    message_id: int | None = None
    kind: Literal["text", "media", "media_nocaption"] = "text"
    card_dirty: bool = False  # карточку нужно отправить/обновить (изменения из webapp/mcp/llm)
    # Вложение исходного сообщения (для расшифровки и передачи агентам)
    media_type: str | None = None  # voice | audio | video | video_note | photo | document | ...
    file_id: str | None = None
    file_name: str | None = None
    mime: str | None = None
    media_duration: int | None = None
    caption_text: str | None = None  # исходная подпись пользователя к медиа
    extracted_text: str | None = None  # расшифровка звука или текст с картинки
    extracted_kind: Literal["transcript", "ocr", "pending", "failed"] | None = None
    extracted_message_id: int | None = None
    extracted_raw: str | None = None  # сырая расшифровка до автокоррекции
    edit_prompt_id: int | None = None  # сообщение «пришлите новый текст» (кнопка ✏️)  # ответ под карточкой с полным текстом
    # Поручение агенту-коворкеру (Claude / Codex)
    assignee: Agent | None = None
    agent_status: str | None = None
    agent_workdir: str | None = None

    def public(self) -> dict:
        """Представление для API/MCP/LLM."""
        return {
            "id": self.id,
            "text": self.text,
            "status": self.status,
            "remind_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "next_ping_at": self.remind_at.isoformat() if self.remind_at else None,
            "repeat": self.repeat,
            "workhours_only": self.workhours_only,
            "period": self.period,
            "idea": self.idea,
            "created_at": self.created_at.isoformat(),
            "done_at": self.done_at.isoformat() if self.done_at else None,
            "source": self.source,
            "assignee": self.assignee,
            "agent_status": self.agent_status,
            "message_id": self.message_id,
            "media_type": self.media_type,
            "extracted_kind": self.extracted_kind,
        }


class AgentJob(BaseModel):
    """Задание агенту-коворкеру. Несколько заданий на задачу — продолжения одного разговора."""

    id: str = Field(default_factory=new_id)
    chat_id: int
    task_id: str | None = None
    agent: Agent
    workdir: str | None = None  # алиас из AGENT_WORKDIRS
    prompt: str
    status: Literal["queued", "running", "done", "failed", "cancelled"] = "queued"
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    worker: str | None = None
    claim_id: str | None = None  # номер запроса воркера: повтор после обрыва связи вернёт то же задание
    session_id: str | None = None  # для продолжения разговора с агентом
    parent_id: str | None = None
    title: str | None = None  # служебное задание без карточки (разбор дня): заголовок отчёта
    result: str | None = None
    notify: Literal["none", "started", "finished"] = "none"  # что осталось сообщить в Telegram
    result_message_id: int | None = None

    def public(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "agent": self.agent,
            "workdir": self.workdir,
            "prompt": self.prompt,
            "status": self.status,
            "session_id": self.session_id,
            "parent_id": self.parent_id,
            "result": self.result,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class UserSettings(BaseModel):
    chat_id: int
    tz: str = "Europe/Moscow"
    ui_mode: UiMode = "buttons"
    work_start: time = time(9, 0)
    work_end: time = time(18, 0)
    workdays: list[int] = [0, 1, 2, 3, 4]  # 0=Пн
    morning_digest: bool = True
    evening_review: bool = True
    smart_input: bool = False  # разбирать новые задачи через LLM
    llm_model: str | None = None  # модель ассистента (None — LLM_MODEL из настроек)
    vocabulary: list[str] = []  # имена/термины — подсказки для распознавания речи
    orchestrator: Literal["claude", "codex"] | None = None  # кому отдавать утренний/вечерний разбор дня
    # /review: очередь задач на решение «по одной»
    review_queue: list[str] = []
    review_total: int = 0
    review_current: str | None = None
    review_stats: dict[str, int] = {}
    review_reasons: dict[str, str] = {}
    last_morning: date | None = None
    last_evening: date | None = None

    def public(self) -> dict:
        return {
            "tz": self.tz,
            "ui_mode": self.ui_mode,
            "work_start": self.work_start.strftime("%H:%M"),
            "work_end": self.work_end.strftime("%H:%M"),
            "workdays": [d + 1 for d in self.workdays],
            "morning_digest": self.morning_digest,
            "evening_review": self.evening_review,
            "smart_input": self.smart_input,
            "llm_model": self.llm_model,
            "orchestrator": self.orchestrator,
        }
