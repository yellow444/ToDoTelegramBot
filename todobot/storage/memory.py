"""In-memory хранилище: тесты и локальный запуск без Mongo."""

from datetime import datetime

from ..models import AgentJob, Task, UserSettings, utcnow


class MemoryRepository:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.users: dict[int, UserSettings] = {}
        self.history: dict[int, list[dict]] = {}
        self.outbox: list[dict] = []
        self.jobs: dict[str, AgentJob] = {}

    async def init(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def insert_task(self, task: Task) -> None:
        self.tasks[task.id] = task.model_copy()

    async def get_task(self, task_id: str) -> Task | None:
        t = self.tasks.get(task_id)
        return t.model_copy() if t else None

    async def find_by_message(self, chat_id: int, message_id: int) -> Task | None:
        for t in self.tasks.values():
            if t.chat_id == chat_id and t.message_id == message_id:
                return t.model_copy()
        return None

    async def update_task(self, task_id: str, **fields) -> Task | None:
        t = self.tasks.get(task_id)
        if t is None:
            return None
        updated = t.model_copy(update=fields)
        self.tasks[task_id] = updated
        return updated.model_copy()

    async def delete_task(self, task_id: str) -> bool:
        return self.tasks.pop(task_id, None) is not None

    async def list_tasks(self, chat_id: int, status: str | None = "open", limit: int = 200) -> list[Task]:
        items = [t for t in self.tasks.values() if t.chat_id == chat_id and (status is None or t.status == status)]
        items.sort(key=lambda t: (t.remind_at is None, t.remind_at or t.created_at, t.created_at))
        return [t.model_copy() for t in items[:limit]]

    async def due_tasks(self, now: datetime) -> list[Task]:
        return [t.model_copy() for t in self.tasks.values() if t.status == "open" and t.remind_at and t.remind_at <= now]

    async def dirty_tasks(self) -> list[Task]:
        return [t.model_copy() for t in self.tasks.values() if t.card_dirty]

    async def get_user(self, chat_id: int) -> UserSettings | None:
        u = self.users.get(chat_id)
        return u.model_copy() if u else None

    async def save_user(self, user: UserSettings) -> None:
        self.users[user.chat_id] = user.model_copy()

    async def list_users(self) -> list[UserSettings]:
        return [u.model_copy() for u in self.users.values()]

    async def add_history(self, chat_id: int, role: str, content: str) -> None:
        self.history.setdefault(chat_id, []).append({"role": role, "content": content, "ts": utcnow()})

    async def get_history(self, chat_id: int, limit: int) -> list[dict]:
        return [{"role": h["role"], "content": h["content"]} for h in self.history.get(chat_id, [])[-limit:]]

    async def clear_history(self, chat_id: int) -> None:
        self.history.pop(chat_id, None)

    async def push_outbox(self, chat_id: int, text: str | None = None, delete_message_id: int | None = None) -> None:
        self.outbox.append({"chat_id": chat_id, "text": text, "delete_message_id": delete_message_id})

    async def pop_outbox(self, limit: int = 20) -> list[dict]:
        items, self.outbox = self.outbox[:limit], self.outbox[limit:]
        return items

    async def insert_job(self, job: AgentJob) -> None:
        self.jobs[job.id] = job.model_copy()

    async def get_job(self, job_id: str) -> AgentJob | None:
        j = self.jobs.get(job_id)
        return j.model_copy() if j else None

    async def update_job(self, job_id: str, **fields) -> AgentJob | None:
        j = self.jobs.get(job_id)
        if j is None:
            return None
        self.jobs[job_id] = j.model_copy(update=fields)
        return self.jobs[job_id].model_copy()

    async def claim_job(self, agents: list[str], worker: str, claim_id: str | None = None) -> AgentJob | None:
        if claim_id:
            for j in self.jobs.values():
                if j.status == "running" and j.claim_id == claim_id:
                    return j.model_copy()
        queued = sorted(
            (j for j in self.jobs.values() if j.status == "queued" and j.agent in agents), key=lambda j: j.created_at
        )
        if not queued:
            return None
        return await self.update_job(
            queued[0].id, status="running", worker=worker, claim_id=claim_id, started_at=utcnow(), notify="started"
        )

    async def list_jobs(self, chat_id: int | None = None, status: str | None = None, limit: int = 20) -> list[AgentJob]:
        items = [
            j for j in self.jobs.values()
            if (chat_id is None or j.chat_id == chat_id) and (status is None or j.status == status)
        ]
        items.sort(key=lambda j: j.created_at, reverse=True)
        return [j.model_copy() for j in items[:limit]]

    async def jobs_to_notify(self) -> list[AgentJob]:
        return [j.model_copy() for j in self.jobs.values() if j.notify != "none"]

    async def find_job_by_message(self, chat_id: int, message_id: int) -> AgentJob | None:
        for j in self.jobs.values():
            if j.chat_id == chat_id and j.result_message_id == message_id:
                return j.model_copy()
        return None
