import pytest

from todobot import tools
from todobot.config import Settings
from todobot.models import UserSettings
from todobot.service import TaskService
from todobot.storage import MemoryRepository

CHAT = 1001
TOKEN = "123456:TEST-TOKEN"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None, TOKEN=TOKEN, STORAGE="memory", OWNER_CHAT_ID=CHAT, TZ="Europe/Moscow",
        NAG_MINUTES=10, MAX_NAGS=2, LLM_BASE_URL="",
    )


@pytest.fixture
def user() -> UserSettings:
    return UserSettings(chat_id=CHAT, tz="Europe/Moscow")


@pytest.fixture
def service(settings) -> TaskService:
    svc = TaskService(MemoryRepository(), settings)
    tools.bind(svc)
    return svc
