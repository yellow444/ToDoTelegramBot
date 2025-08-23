import logging
from typing import List
from pymongo import MongoClient, errors

from config import settings

logger = logging.getLogger(__name__)


def _get_collection():
    """Initialize connection to MongoDB and return the collection."""

    try:
        if settings.MONGO_URI:
            # Предпочтительно подключаемся по URI (с репликасетом/параметрами/SSL и т.д.)
            client = MongoClient(settings.MONGO_URI, serverSelectionTimeoutMS=30000)
            client.server_info()  # проверка соединения/аутентификации
        else:
            # Хост/порт + authSource
            if not settings.MONGO_HOST:
                raise ValueError("MONGO_HOST is empty and MONGO_URI not provided")
            client = MongoClient(
                host=settings.MONGO_HOST,
                port=settings.MONGO_PORT,
                username=settings.MONGO_USER or None,
                password=settings.MONGO_PASS or None,
                authSource=settings.MONGO_AUTH_DB,
                serverSelectionTimeoutMS=30000,
            )
            client.server_info()

        db = client[settings.DB_NAME]
        coll = db[settings.COLLECTION_NAME]
        logger.info("Mongo connected: db=%s collection=%s", settings.DB_NAME, settings.COLLECTION_NAME)
        return coll

    except (errors.PyMongoError, Exception) as exc:  # pragma: no cover - logging only
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


def fetch_reminders() -> List[dict]:
    """Return all reminders stored in the collection."""
    if collection is not None:
        return list(collection.find())
    return []
