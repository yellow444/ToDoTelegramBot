from ..config import Settings
from .base import Repository
from .memory import MemoryRepository


def create_repository(settings: Settings) -> Repository:
    if settings.STORAGE == "memory":
        return MemoryRepository()
    from .mongo import MongoRepository

    return MongoRepository(settings.mongo_uri(), settings.DB_NAME, settings.COLLECTION_NAME, settings.TZ)


__all__ = ["Repository", "MemoryRepository", "create_repository"]
