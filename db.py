import logging

from pymongo import MongoClient

from config import settings


logger = logging.getLogger(__name__)


def _get_collection():
    """Initialize connection to MongoDB and return the collection."""

    try:
        client = MongoClient(
            settings.MONGO_HOST,
            settings.MONGO_PORT,
            username=settings.MONGO_USER,
            password=settings.MONGO_PASS,
            serverSelectionTimeoutMS=30000,
        )
        try:
            client.server_info()
        except Exception:
            client = MongoClient(
                "127.0.0.1",
                settings.MONGO_PORT,
                username=settings.MONGO_USER,
                password=settings.MONGO_PASS,
                serverSelectionTimeoutMS=30000,
            )
            client.server_info()
        db = client[settings.DB_NAME]
        logger.info("Подключение к Mongo успешно")
        return db[settings.COLLECTION_NAME]
    except Exception as exc:  # pragma: no cover - logging only
        logger.error("Ошибка подключения к Mongo: %s", exc)
        return None


collection = _get_collection()


def insert_reminder(record: dict) -> None:
    """Insert a reminder record."""

    if collection is not None:
        collection.insert_one(record)


def update_reminder(message_id: int, data: dict) -> None:
    """Update reminder data for a specific message."""

    if collection is not None:
        collection.find_one_and_update({"message_id": message_id}, {"$set": data})


def delete_reminders(filter_dict: dict) -> None:
    """Delete reminders that match the given filter."""

    if collection is not None:
        collection.delete_many(filter_dict)


def count_reminders(filter_dict: dict) -> int:
    """Count reminders matching the filter."""

    if collection is not None:
        return collection.count_documents(filter_dict)
    return 0


def fetch_reminders() -> list[dict]:
    """Return all reminders stored in the collection."""

    if collection is not None:
        return list(collection.find())
    return []

