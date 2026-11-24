"""Бизнес-логика задач. Ею пользуются бот, webapp, LLM-агент и MCP-сервер."""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Settings
from .models import AGENT_LABELS, AgentJob, Task, UserSettings, utcnow
from .storage import Repository
from .timeparse import parse
from .timeutil import (
    in_work_hours,
    local_now,
    next_occurrence,
    next_work_start,
    parse_hhmm,
    parse_workdays,
    shift_into_work_hours,
    to_local,
)

logger = logging.getLogger(__name__)

REPEAT_VALUES = ("daily", "workdays", "weekly", "monthly")
KEEP = "keep"


class TaskNotFound(LookupError):
    pass


def _check_repeat(repeat: str | None) -> None:
    if repeat and repeat not in REPEAT_VALUES:
        raise ValueError(f"repeat должен быть одним из {REPEAT_VALUES} или пустым")


class TaskService:
    def __init__(self, repo: Repository, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings
        self.worker_seen: dict[str, datetime] = {}  # агент → когда чат-коворкер последний раз спрашивал очередь

    def worker_online(self, agent: str, within: timedelta = timedelta(minutes=2)) -> bool:
        seen = self.worker_seen.get(agent)
        return seen is not None and utcnow() - seen < within

    # ---------- пользователи ----------
    def default_user(self, chat_id: int) -> UserSettings:
        s = self.settings
        return UserSettings(
            chat_id=chat_id,
            tz=s.TZ,
            work_start=parse_hhmm(s.WORK_START),
            work_end=parse_hhmm(s.WORK_END),
            workdays=parse_workdays(s.WORKDAYS),
        )

    async def user(self, chat_id: int) -> UserSettings:
        u = await self.repo.get_user(chat_id)
        if u is None:
            u = self.default_user(chat_id)
            await self.repo.save_user(u)
        return u

    async def update_user(self, chat_id: int, **fields) -> UserSettings:
        u = await self.user(chat_id)
        if "tz" in fields:
            ZoneInfo(fields["tz"])  # бросит исключение на неизвестной зоне
        for key in ("work_start", "work_end"):
            if isinstance(fields.get(key), str):
                fields[key] = parse_hhmm(fields[key])
        if isinstance(fields.get("workdays"), str):
            fields["workdays"] = parse_workdays(fields["workdays"])
        u = UserSettings.model_validate({**u.model_dump(), **fields})
        if u.work_end <= u.work_start:
            raise ValueError("Конец рабочего дня должен быть позже начала")
        await self.repo.save_user(u)
        return u

    async def default_chat_id(self) -> int:
        if self.settings.OWNER_CHAT_ID:
            return self.settings.OWNER_CHAT_ID
        users = await self.repo.list_users()
        if len(users) == 1:
            return users[0].chat_id
        raise ValueError("Не задан OWNER_CHAT_ID: укажите chat_id явно или в .env")

    # ---------- задачи ----------
    async def get(self, task_id: str, chat_id: int | None = None) -> Task:
        t = await self.repo.get_task(task_id)
        if t is None or (chat_id is not None and t.chat_id != chat_id):
            raise TaskNotFound(task_id)
        return t

    async def create(
        self,
        chat_id: int,
        text: str,
        *,
        when: datetime | None = None,
        repeat: str | None = None,
        workhours_only: bool = False,
        source: str = "telegram",
        parse_text: bool = False,
        post_card: bool = False,
        kind: str = "text",
        message_id: int | None = None,
        default_remind: bool = False,
        **extra,
    ) -> Task:
        u = await self.user(chat_id)
        if parse_text:
            p = parse(text, u)
            text = p.text
            when = when or p.remind_at
            repeat = repeat or p.repeat
            workhours_only = workhours_only or p.workhours_only
            if p.period and "period" not in extra:
                extra["period"] = p.period
            if p.idea and "idea" not in extra:
                extra["idea"] = True
        if extra.get("idea"):
            default_remind = False
        _check_repeat(repeat)
        if repeat and when is None:
            when = next_work_start(utcnow(), u)
        if default_remind and when is None and self.settings.DEFAULT_REMIND_HOURS > 0:
            when = utcnow() + timedelta(hours=self.settings.DEFAULT_REMIND_HOURS)
        if when and workhours_only:
            when = shift_into_work_hours(when, u)
        task = Task(
            chat_id=chat_id,
            text=text.strip() or "(без текста)",
            scheduled_at=when,
            remind_at=when,
            repeat=repeat or None,
            workhours_only=workhours_only,
            source=source,
            kind=kind,
            message_id=message_id,
            card_dirty=post_card,
            **extra,
        )
        await self.repo.insert_task(task)
        return task

    async def resolve(self, chat_id: int, ref: str | int) -> Task:
        """Задача по ссылке: id, «#префикс id» или номер сообщения-карточки (message_id)."""
        ref = str(ref).strip().lstrip("#")
        if not ref:
            raise TaskNotFound(ref)
        t = await self.repo.get_task(ref)
        if t and t.chat_id == chat_id:
            return t
        if ref.isdigit():
            t = await self.repo.find_by_message(chat_id, int(ref))
            if t:
                return t
        if len(ref) >= 4:
            matches = [x for x in await self.repo.list_tasks(chat_id, None, 2000) if x.id.startswith(ref.lower())]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ValueError(f"Ссылка #{ref} неоднозначна — укажите больше символов id")
        raise TaskNotFound(ref)

    async def apply_extracted(self, task: Task, text: str | None, kind: str) -> Task:
        """Расшифровка/текст с картинки дополняют задачу. Время ищется в подписи и в распознанном тексте;
        если его нет и напоминание ещё не стоит — ставится через DEFAULT_REMIND_HOURS."""
        u = await self.user(task.chat_id)
        text = (text or "").strip()
        fields: dict = {"extracted_text": text or None, "extracted_kind": kind if text else "failed"}
        p = parse(f"{task.caption_text or ''} {text}".strip(), u)
        if not task.caption_text and text:
            fields["text"] = parse(text, u).text or text  # без подписи задача — это сам распознанный текст
        when = task.scheduled_at
        if p.remind_at or p.repeat:
            when = p.remind_at or next_work_start(utcnow(), u)
            if p.workhours_only:
                when = shift_into_work_hours(when, u)
            fields.update(repeat=p.repeat, workhours_only=p.workhours_only)
        elif when is None and self.settings.DEFAULT_REMIND_HOURS > 0:
            when = utcnow() + timedelta(hours=self.settings.DEFAULT_REMIND_HOURS)
        if when != task.scheduled_at:
            fields.update(scheduled_at=when, remind_at=when, nag_count=0)
        return await self._update(task, False, **fields)

    async def _update(self, task: Task, touch_card: bool, **fields) -> Task:
        if touch_card:
            fields["card_dirty"] = True
        t = await self.repo.update_task(task.id, **fields)
        if t is None:
            raise TaskNotFound(task.id)
        return t

    async def complete(self, task: Task, touch_card: bool = False) -> Task:
        """Закрыть задачу. Повторяющаяся остаётся открытой и переезжает на следующее срабатывание."""
        now = utcnow()
        if task.repeat:
            u = await self.user(task.chat_id)
            nxt = next_occurrence(task.scheduled_at or now, task.repeat, u, after=now)
            return await self._update(
                task, touch_card, scheduled_at=nxt, remind_at=nxt, nag_count=0, done_at=now
            )
        return await self._update(task, touch_card, status="done", done_at=now, remind_at=None, nag_count=0)

    async def reopen(self, task: Task, touch_card: bool = False) -> Task:
        remind = task.scheduled_at if task.scheduled_at and task.scheduled_at > utcnow() else None
        return await self._update(task, touch_card, status="open", done_at=None, remind_at=remind)

    async def set_reminder(
        self,
        task: Task,
        when: datetime | None,
        repeat: str | None = KEEP,
        workhours_only: bool | None = None,
        touch_card: bool = False,
    ) -> Task:
        fields: dict = {"nag_count": 0, "idea": False} if when else {"nag_count": 0}
        if repeat == KEEP:
            # «снять напоминание» у повторяющейся задачи снимает и повтор
            new_repeat = task.repeat if when is not None else None
        else:
            new_repeat = repeat or None
        _check_repeat(new_repeat)
        fields["repeat"] = new_repeat
        if workhours_only is not None:
            fields["workhours_only"] = workhours_only
        effective_wh = task.workhours_only if workhours_only is None else workhours_only
        u = await self.user(task.chat_id)
        if new_repeat and when is None:
            when = task.scheduled_at if task.scheduled_at and task.scheduled_at > utcnow() else next_work_start(utcnow(), u)
        if when and effective_wh:
            when = shift_into_work_hours(when, u)
        fields["scheduled_at"] = when
        fields["remind_at"] = when
        if task.status == "done" and when:
            fields["status"] = "open"
            fields["done_at"] = None
        return await self._update(task, touch_card, **fields)

    async def snooze(self, task: Task, minutes: int, touch_card: bool = False) -> Task:
        """Отложить ближайшее срабатывание, не меняя план (для повторяющихся)."""
        when = utcnow() + timedelta(minutes=minutes)
        if task.repeat:
            return await self._update(task, touch_card, remind_at=when, nag_count=0)
        return await self.set_reminder(task, when, touch_card=touch_card)

    async def edit_text(self, task: Task, text: str, touch_card: bool = False) -> Task:
        return await self._update(task, touch_card, text=text.strip())

    async def delete(self, task: Task, delete_card: bool = False) -> None:
        await self.repo.delete_task(task.id)
        if delete_card and task.message_id:
            await self.repo.push_outbox(task.chat_id, delete_message_id=task.message_id)

    async def list_tasks(self, chat_id: int, status: str | None = "open", limit: int = 200) -> list[Task]:
        return await self.repo.list_tasks(chat_id, status, limit)

    async def agenda(self, chat_id: int, day: date | None = None) -> dict:
        """Срез на день: просроченные, на этот день, без срока, позже."""
        u = await self.user(chat_id)
        now = utcnow()
        day = day or local_now(u).date()
        overdue, today, undated, later = [], [], [], []
        for t in await self.list_tasks(chat_id, "open"):
            if t.idea:
                continue  # идеи живут в /ideas
            if t.scheduled_at is None:
                undated.append(t)
            elif t.scheduled_at < now and to_local(t.scheduled_at, u).date() <= day:
                overdue.append(t)
            elif to_local(t.scheduled_at, u).date() == day:
                today.append(t)
            else:
                later.append(t)
        return {"day": day, "overdue": overdue, "today": today, "undated": undated, "later": later, "user": u}

    async def done_on(self, chat_id: int, day: date) -> list[Task]:
        u = await self.user(chat_id)
        return [
            t
            for t in await self.list_tasks(chat_id, None, limit=1000)
            if t.done_at and to_local(t.done_at, u).date() == day
        ]

    # ---------- срабатывание напоминаний ----------
    async def due(self, now: datetime | None = None) -> list[Task]:
        return await self.repo.due_tasks(now or utcnow())

    async def defer_if_outside_work_hours(self, task: Task, now: datetime | None = None) -> bool:
        """Задачу «только в рабочее время» вне его не дёргаем, а переносим на начало дня."""
        if not task.workhours_only:
            return False
        u = await self.user(task.chat_id)
        now = now or utcnow()
        if in_work_hours(now, u):
            return False
        await self._update(task, False, remind_at=next_work_start(now, u))
        return True

    async def after_fired(self, task: Task, message_id: int | None, now: datetime | None = None) -> Task:
        """Планирует следующее срабатывание: повторный «пинок» через NAG_MINUTES, пока не закроют."""
        now = now or utcnow()
        nag_at = now + timedelta(minutes=self.settings.NAG_MINUTES)
        can_nag = task.nag_count < self.settings.MAX_NAGS
        fields: dict = {"message_id": message_id or task.message_id, "card_dirty": False}
        if task.repeat:
            u = await self.user(task.chat_id)
            nxt = next_occurrence(task.scheduled_at or now, task.repeat, u, after=now)
            if can_nag and nag_at < nxt:
                fields.update(remind_at=nag_at, nag_count=task.nag_count + 1)
            else:
                fields.update(remind_at=nxt, scheduled_at=nxt, nag_count=0)
        elif can_nag:
            fields.update(remind_at=nag_at, nag_count=task.nag_count + 1)
        else:
            fields.update(remind_at=None)
        return await self._update(task, False, **fields)

    # ---------- произвольные уведомления ----------
    async def notify(self, chat_id: int, text: str) -> None:
        await self.repo.push_outbox(chat_id, text)

    # ---------- агенты-коворкеры ----------
    async def delegate(
        self,
        chat_id: int,
        agent: str,
        prompt: str,
        *,
        workdir: str | None = None,
        task: Task | None = None,
        parent: AgentJob | None = None,
        source: str = "telegram",
    ) -> tuple[Task, AgentJob]:
        """Поручить задачу Claude/Codex: создаёт (или берёт) задачу и ставит задание в очередь воркера."""
        if agent not in AGENT_LABELS:
            raise ValueError(f"agent должен быть одним из {tuple(AGENT_LABELS)}")
        if not prompt.strip():
            raise ValueError("Пустое задание")
        if workdir and workdir not in self.settings.workdirs:
            known = ", ".join(self.settings.workdirs) or "не настроены (AGENT_WORKDIRS)"
            raise ValueError(f"Неизвестная рабочая папка {workdir!r}. Доступные: {known}")
        if task is None:
            title = prompt.strip().splitlines()[0][:200]
            task = await self.create(chat_id, title, source=source, post_card=True)
        job = AgentJob(
            chat_id=chat_id,
            task_id=task.id,
            agent=agent,
            workdir=workdir,
            prompt=prompt.strip(),
            parent_id=parent.id if parent else None,
            session_id=parent.session_id if parent and parent.agent == agent else None,
        )
        await self.repo.insert_job(job)
        task = await self._update(
            task, True, assignee=agent, agent_status="queued", agent_workdir=workdir, status="open", done_at=None
        )
        return task, job

    async def job_history(self, job: AgentJob, depth: int = 3) -> list[dict]:
        """Предыдущие шаги разговора с агентом (от старых к новым)."""
        chain = []
        parent_id = job.parent_id
        while parent_id and len(chain) < depth:
            parent = await self.repo.get_job(parent_id)
            if parent is None:
                break
            chain.append({"prompt": parent.prompt, "result": parent.result})
            parent_id = parent.parent_id
        return list(reversed(chain))

    async def _sync_task_status(self, job: AgentJob) -> None:
        if job.task_id and (task := await self.repo.get_task(job.task_id)):
            await self._update(task, True, agent_status=job.status)

    async def claim_job(self, agents: list[str], worker: str, claim_id: str | None = None) -> AgentJob | None:
        job = await self.repo.claim_job(agents, worker, claim_id)
        if job:
            await self._sync_task_status(job)
        return job

    async def finish_job(self, job_id: str, ok: bool, result: str, session_id: str | None = None) -> AgentJob:
        job = await self.repo.get_job(job_id)
        if job is None:
            raise ValueError(f"Задание {job_id} не найдено")
        if job.status not in ("queued", "running"):
            raise ValueError(f"Задание {job_id} уже завершено ({job.status})")
        job = await self.repo.update_job(
            job_id,
            status="done" if ok else "failed",
            finished_at=utcnow(),
            result=(result or "").strip()[:20000],
            session_id=session_id or job.session_id,
            notify="finished",
        )
        await self._sync_task_status(job)
        if ok and job.task_id and (task := await self.repo.get_task(job.task_id)) and task.source == "orchestrator":
            await self.complete(task, touch_card=True)  # служебный разбор дня закрывается вместе с отчётом
        return job

    async def cancel_job(self, job_id: str) -> AgentJob:
        job = await self.repo.get_job(job_id)
        if job is None:
            raise ValueError(f"Задание {job_id} не найдено")
        if job.status in ("queued", "running"):
            job = await self.repo.update_job(job_id, status="cancelled", finished_at=utcnow(), notify="none")
            await self._sync_task_status(job)
        return job

    async def fail_stale_jobs(self) -> None:
        """Задание «работает» дольше таймаута — чат-коворкер, видимо, закрыли.
        Служебные разборы дня, которые никто не взял за ORCHESTRATION_TTL_HOURS, тихо отменяются:
        десктопные приложения бывают закрыты, копить устаревшие разборы незачем."""
        now = utcnow()
        limit = timedelta(seconds=self.settings.AGENT_TIMEOUT + 300)
        for job in await self.repo.list_jobs(status="running", limit=100):
            if job.started_at and now - job.started_at > limit:
                if job.title:  # служебный разбор — без сообщения «не получилось»
                    await self.repo.update_job(job.id, status="cancelled", finished_at=now, notify="none")
                else:
                    await self.finish_job(job.id, False, "Чат-коворкер не ответил вовремя (его закрыли?). Попробуйте ещё раз.")
        ttl = timedelta(hours=self.settings.ORCHESTRATION_TTL_HOURS)
        for job in await self.repo.list_jobs(status="queued", limit=500):
            if job.title and now - job.created_at > ttl:
                await self.repo.update_job(job.id, status="cancelled", finished_at=now, notify="none")

    async def review_candidates(self, chat_id: int) -> dict:
        """Что убрать из чата (бот сам) и что требует решения человека (по одной)."""
        now = utcnow()
        tasks = await self.list_tasks(chat_id, None, 3000)
        service = [t for t in tasks if t.source == "orchestrator" and t.message_id]
        done_old = [
            t for t in tasks
            if t.status == "done" and t.source != "orchestrator" and t.message_id
            and t.done_at and now - t.done_at > timedelta(days=7)
        ]
        triage, reasons = [], {}
        for t in tasks:
            if t.status != "open" or t.source == "orchestrator" or t.idea:
                continue
            if t.extracted_kind == "failed":
                reason = "failed"
            elif t.scheduled_at and not t.repeat and t.scheduled_at < now - timedelta(hours=1) and (
                t.remind_at is None or t.scheduled_at < now - timedelta(days=1)
            ):
                reason = "overdue"  # напоминания кончились или просрочена больше суток
            elif t.scheduled_at is None and now - t.created_at > timedelta(days=3):
                reason = "stale"
            else:
                continue
            triage.append(t)
            reasons[t.id] = reason
        triage.sort(key=lambda t: t.created_at)
        return {"service": service, "done_old": done_old, "triage": triage, "reasons": reasons}

    async def enqueue_orchestration(self, chat_id: int, kind: str) -> AgentJob | None:
        """Утренний/вечерний разбор дня отдаём коворкеру (если пользователь включил /orchestrate)."""
        u = await self.user(chat_id)
        if not u.orchestrator:
            return None
        if kind == "morning":
            prompt = (
                "Утренняя оркестрация дня. Через MCP todo (или HTTP API бота) посмотри get_agenda и list_agent_jobs. "
                "Просроченное, что ещё актуально, перенеси на сегодня в рабочие часы; если задач слишком много — "
                "предложи, что перенести. Ничего не удаляй и не закрывай. В отчёт: план дня из 3–7 пунктов по "
                "приоритету с временем, что перенесено, что ждёт от агентов. Задачи упоминай как #id. "
                "Отчёт КОРОТКИЙ (до 6 строк): не перечисляй все задачи — изменения и так видны на карточках."
            )
        else:
            prompt = (
                "Вечерний разбор дня. Через get_agenda: что сделано, что осталось. Несделанные задачи без повтора "
                "перенеси на начало следующего рабочего дня. Ничего не удаляй. В отчёт: итоги, что перенесено, "
                "1–3 приоритета на завтра, что ждёт проверки от агентов. Задачи упоминай как #id. "
                "Отчёт КОРОТКИЙ (до 6 строк): не перечисляй все задачи — изменения и так видны на карточках."
            )
        # новый разбор заменяет старый, который никто не взял
        for old in await self.repo.list_jobs(chat_id, "queued", limit=500):
            if old.title:
                await self.repo.update_job(old.id, status="cancelled", finished_at=utcnow(), notify="none")
        job = AgentJob(
            chat_id=chat_id,
            agent=u.orchestrator,
            prompt=prompt,
            title="☀️ Разбор утра" if kind == "morning" else "🌇 Разбор вечера",
        )
        await self.repo.insert_job(job)  # без задачи-карточки: в чат придёт только отчёт
        return job
