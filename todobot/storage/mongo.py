"""MongoDB-хранилище на асинхронном драйвере pymongo."""

import logging
from datetime import datetime, time, timezone

from pymongo import ASCENDING, AsyncMongoClient, ReturnDocument

from ..models import AgentJob, Task, UserSettings, new_id, utcnow
from ..timeutil import zone

logger = logging.getLogger(__name__)


def _task_from_doc(doc: dict) -> Task:
    doc = dict(doc)
    doc["id"] = doc.pop("_id")
    return Task.model_validate(doc)


def _task_to_doc(task: Task) -> dict:
    doc = task.model_dump()
    doc["_id"] = doc.pop("id")
    return doc


def _user_to_doc(u: UserSettings) -> dict:
    doc = u.model_dump()
    doc["_id"] = doc.pop("chat_id")
    for key in ("work_start", "work_end"):
        doc[key] = doc[key].strftime("%H:%M")
    for key in ("last_morning", "last_evening"):
        doc[key] = doc[key].isoformat() if doc[key] else None
    return doc


def _user_from_doc(doc: dict) -> UserSettings:
    doc = dict(doc)
    doc["chat_id"] = doc.pop("_id")
    return UserSettings.model_validate(doc)


def _serialize(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if isinstance(v, time):
            v = v.strftime("%H:%M")
        out[k] = v
    return out


class MongoRepository:
    def __init__(self, uri: str, db_name: str, tasks_collection: str, default_tz: str = "Europe/Moscow") -> None:
        self._client = AsyncMongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=30000)
        db = self._client[db_name]
        self.tasks = db[tasks_collection]
        self.users = db["users"]
        self.history = db["chat_history"]
        self.outbox = db["outbox"]
        self.jobs = db["agent_jobs"]
        self._default_tz = default_tz

    async def init(self) -> None:
        await self._client.admin.command("ping")
        await self._migrate_legacy()
        await self.tasks.create_index([("status", ASCENDING), ("remind_at", ASCENDING)])
        await self.tasks.create_index([("chat_id", ASCENDING), ("status", ASCENDING)])
        await self.tasks.create_index([("chat_id", ASCENDING), ("message_id", ASCENDING)])
        await self.tasks.create_index("card_dirty", sparse=True)
        await self.history.create_index([("chat_id", ASCENDING), ("ts", ASCENDING)])
        await self.jobs.create_index([("status", ASCENDING), ("agent", ASCENDING), ("created_at", ASCENDING)])
        await self.jobs.create_index([("chat_id", ASCENDING), ("result_message_id", ASCENDING)])
        # история диалога живёт 30 дней
        await self.history.create_index("ts", expireAfterSeconds=30 * 24 * 3600, name="ttl_ts")
        logger.info("Mongo готов: %s", self.tasks.full_name)

    async def _migrate_legacy(self) -> None:
        """Старый формат: {chat_id, message_id, message, caption, date:'%d-%m-%Y %H:%M', today}."""
        count = 0
        async for doc in self.tasks.find({"status": {"$exists": False}}):
            text = (doc.get("message") or "").split(" ::")[0].strip() or (doc.get("caption") or "").split(" ::")[0].strip()
            remind_at = None
            if doc.get("date"):
                try:
                    local = datetime.strptime(doc["date"], "%d-%m-%Y %H:%M").replace(tzinfo=zone(self._default_tz))
                    remind_at = local.astimezone(timezone.utc)
                    if remind_at < utcnow():
                        remind_at = utcnow()
                except ValueError:
                    pass
            task = Task(
                id=new_id(),
                chat_id=int(doc["chat_id"]),
                text=text or "(без текста)",
                message_id=doc.get("message_id"),
                kind="text" if doc.get("message") else "media",
                scheduled_at=remind_at,
                remind_at=remind_at,
                card_dirty=True,
                source="legacy",
            )
            await self.tasks.insert_one(_task_to_doc(task))
            await self.tasks.delete_one({"_id": doc["_id"]})
            count += 1
        if count:
            logger.info("Мигрировано старых напоминаний: %d", count)

    async def close(self) -> None:
        await self._client.close()

    async def insert_task(self, task: Task) -> None:
        await self.tasks.insert_one(_task_to_doc(task))

    async def get_task(self, task_id: str) -> Task | None:
        doc = await self.tasks.find_one({"_id": task_id})
        return _task_from_doc(doc) if doc else None

    async def find_by_message(self, chat_id: int, message_id: int) -> Task | None:
        doc = await self.tasks.find_one({"chat_id": chat_id, "message_id": message_id})
        return _task_from_doc(doc) if doc else None

    async def update_task(self, task_id: str, **fields) -> Task | None:
        doc = await self.tasks.find_one_and_update(
            {"_id": task_id}, {"$set": _serialize(fields)}, return_document=ReturnDocument.AFTER
        )
        return _task_from_doc(doc) if doc else None

    async def delete_task(self, task_id: str) -> bool:
        res = await self.tasks.delete_one({"_id": task_id})
        return res.deleted_count > 0

    async def list_tasks(self, chat_id: int, status: str | None = "open", limit: int = 200) -> list[Task]:
        query: dict = {"chat_id": chat_id}
        if status:
            query["status"] = status
        docs = await self.tasks.find(query).limit(limit).to_list()
        items = [_task_from_doc(d) for d in docs]
        items.sort(key=lambda t: (t.remind_at is None, t.remind_at or t.created_at, t.created_at))
        return items

    async def due_tasks(self, now: datetime) -> list[Task]:
        docs = await self.tasks.find({"status": "open", "remind_at": {"$ne": None, "$lte": now}}).to_list()
        return [_task_from_doc(d) for d in docs]

    async def dirty_tasks(self) -> list[Task]:
        docs = await self.tasks.find({"card_dirty": True}).to_list()
        return [_task_from_doc(d) for d in docs]

    async def get_user(self, chat_id: int) -> UserSettings | None:
        doc = await self.users.find_one({"_id": chat_id})
        return _user_from_doc(doc) if doc else None

    async def save_user(self, user: UserSettings) -> None:
        doc = _user_to_doc(user)
        await self.users.replace_one({"_id": doc["_id"]}, doc, upsert=True)

    async def list_users(self) -> list[UserSettings]:
        return [_user_from_doc(d) for d in await self.users.find().to_list()]

    async def add_history(self, chat_id: int, role: str, content: str) -> None:
        await self.history.insert_one({"chat_id": chat_id, "role": role, "content": content, "ts": utcnow()})

    async def get_history(self, chat_id: int, limit: int) -> list[dict]:
        docs = await self.history.find({"chat_id": chat_id}).sort("ts", -1).limit(limit).to_list()
        return [{"role": d["role"], "content": d["content"]} for d in reversed(docs)]

    async def clear_history(self, chat_id: int) -> None:
        await self.history.delete_many({"chat_id": chat_id})

    async def push_outbox(self, chat_id: int, text: str | None = None, delete_message_id: int | None = None) -> None:
        await self.outbox.insert_one(
            {"chat_id": chat_id, "text": text, "delete_message_id": delete_message_id, "ts": utcnow()}
        )

    async def pop_outbox(self, limit: int = 20) -> list[dict]:
        items = []
        for _ in range(limit):
            doc = await self.outbox.find_one_and_delete({}, sort=[("ts", ASCENDING)])
            if not doc:
                break
            items.append(
                {"chat_id": doc["chat_id"], "text": doc.get("text"), "delete_message_id": doc.get("delete_message_id")}
            )
        return items

    # ---------- задания агентам ----------
    @staticmethod
    def _job(doc: dict | None) -> AgentJob | None:
        if not doc:
            return None
        doc = dict(doc)
        doc["id"] = doc.pop("_id")
        return AgentJob.model_validate(doc)

    async def insert_job(self, job: AgentJob) -> None:
        doc = job.model_dump()
        doc["_id"] = doc.pop("id")
        await self.jobs.insert_one(doc)

    async def get_job(self, job_id: str) -> AgentJob | None:
        return self._job(await self.jobs.find_one({"_id": job_id}))

    async def update_job(self, job_id: str, **fields) -> AgentJob | None:
        doc = await self.jobs.find_one_and_update(
            {"_id": job_id}, {"$set": fields}, return_document=ReturnDocument.AFTER
        )
        return self._job(doc)

    async def claim_job(self, agents: list[str], worker: str, claim_id: str | None = None) -> AgentJob | None:
        # ответ на прошлый запрос мог не дойти — повтор с тем же claim_id отдаёт уже взятое задание
        if claim_id and (doc := await self.jobs.find_one({"status": "running", "claim_id": claim_id})):
            return self._job(doc)
        doc = await self.jobs.find_one_and_update(
            {"status": "queued", "agent": {"$in": agents}},
            {"$set": {"status": "running", "worker": worker, "claim_id": claim_id, "started_at": utcnow(), "notify": "started"}},
            sort=[("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return self._job(doc)

    async def list_jobs(self, chat_id: int | None = None, status: str | None = None, limit: int = 20) -> list[AgentJob]:
        query: dict = {}
        if chat_id is not None:
            query["chat_id"] = chat_id
        if status:
            query["status"] = status
        docs = await self.jobs.find(query).sort("created_at", -1).limit(limit).to_list()
        return [self._job(d) for d in docs]

    async def jobs_to_notify(self) -> list[AgentJob]:
        return [self._job(d) for d in await self.jobs.find({"notify": {"$ne": "none"}}).to_list()]

    async def find_job_by_message(self, chat_id: int, message_id: int) -> AgentJob | None:
        return self._job(await self.jobs.find_one({"chat_id": chat_id, "result_message_id": message_id}))
