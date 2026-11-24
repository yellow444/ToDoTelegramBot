"""Настройки приложения из переменных окружения / .env."""

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram ---
    TOKEN: str = ""
    # Через запятую; пусто — бот отвечает всем.
    ALLOWED_USER_IDS: Annotated[list[int], NoDecode] = []
    # Чат, в который по умолчанию пишут MCP-инструменты (обычно ваш личный id).
    OWNER_CHAT_ID: int | None = None
    RUN_BOT: bool = True

    # --- Хранилище ---
    STORAGE: str = "mongo"  # mongo | memory
    MONGO_URI: str = ""
    MONGO_HOST: str = ""
    MONGO_PORT: int = 27017
    MONGO_USER: str = ""
    MONGO_PASS: str = ""
    MONGO_AUTH_DB: str = "admin"
    DB_NAME: str = "todobot"
    # Старая коллекция с напоминаниями: задачи живут в ней же, старые записи мигрируются.
    COLLECTION_NAME: str = "tasks"

    # --- Время / рабочий день (дефолты для новых пользователей) ---
    TZ: str = "Europe/Moscow"
    WORK_START: str = "09:00"
    WORK_END: str = "18:00"
    WORKDAYS: str = "1-5"  # 1=Пн ... 7=Вс
    # Напоминание по умолчанию для задач из чата, если время не указано (0 — без напоминания)
    DEFAULT_REMIND_HOURS: float = 24
    NAG_MINUTES: int = 10  # повтор напоминания, пока задача не закрыта
    MAX_NAGS: int = 6

    # --- Web / Mini App ---
    WEB_ENABLED: bool = True
    WEB_HOST: str = "0.0.0.0"
    WEB_PORT: int = 8080
    # Держать соединение дольше паузы опроса воркера (3 с), иначе короткая задержка рвёт его на полпути
    WEB_KEEP_ALIVE: int = 75
    # Писать в лог замирания event loop дольше этого, с (0 — выключить), см. watchdog.py
    STALL_WARN_SECONDS: float = 1.0
    # Публичный https-адрес мини-приложения (как его видит Telegram), напр. https://example.org/app/
    WEBAPP_URL: str = ""
    # Только для локальной отладки без Telegram: запросы без initData считаются от этого пользователя.
    WEBAPP_DEV_USER_ID: int | None = None

    # --- LLM (OpenAI-совместимый endpoint, например llm-gateway) ---
    LLM_BASE_URL: str = ""  # http://llmgw-gateway:8080/v1
    LLM_API_KEY: str = "not-needed"
    LLM_MODEL: str = "general35"
    LLM_TIMEOUT: float = 300.0  # с запасом на холодную загрузку модели в llama-swap
    LLM_MAX_TOKENS: int = 1500
    LLM_HISTORY: int = 20  # сколько последних реплик держать в контексте
    # Доп. поля в тело запроса (JSON), напр. {"chat_template_kwargs":{"enable_thinking":false}}
    LLM_EXTRA_BODY: dict = {}
    # Запасная модель, если основная недоступна (например бесплатная облачная: free / tag:free)
    LLM_FALLBACK_MODEL: str = ""

    # --- Агенты-коворкеры (Claude Code / Codex по подписке) ---
    # Рабочие папки: алиас=путь через «;», напр. todo=F:\repos\ToDoTelegramBot;gw=F:\repos\llm-gateway
    AGENT_WORKDIRS: str = ""
    # Папка для заданий без проекта («оркестрация жизни», исследования)
    AGENT_HOME: str = ""
    # Воркер (python -m todobot.worker) на машине, где установлены агенты
    WORKER_API: str = "http://localhost:8088"
    WORKER_AGENTS: str = "claude,codex"
    AGENT_TIMEOUT: int = 3600
    ORCHESTRATION_TTL_HOURS: float = 12  # невзятый разбор дня старше этого — отменяется
    CLAUDE_CMD: str = "claude"
    CLAUDE_PERMISSION_MODE: str = "acceptEdits"  # default | acceptEdits | bypassPermissions
    CLAUDE_ALLOWED_TOOLS: str = ""  # напр. "Bash(git status) Bash(npm test*)"
    CODEX_CMD: str = "codex"
    CODEX_SANDBOX: str = "workspace-write"  # read-only | workspace-write | danger-full-access

    # --- Расшифровка голоса/видео ---
    # telegram — расшифровка Telegram Premium от имени вашего аккаунта (нужен userbot-вход),
    # local — faster-whisper в контейнере, openai — OpenAI-совместимый /audio/transcriptions, off
    STT_PROVIDER: str = "telegram"
    STT_FALLBACK: str = "local"  # если основной способ недоступен
    STT_MODEL: str = "small"  # local: tiny/base/small/medium/large-v3-turbo; openai: имя модели
    STT_BASE_URL: str = ""  # для openai, напр. http://llmgw-gateway:8080/v1
    STT_API_KEY: str = "not-needed"
    STT_LANGUAGE: str = "ru"
    STT_DEVICE: str = "cpu"  # cpu | cuda
    STT_MAX_MB: int = 20  # лимит Bot API на скачивание файла
    STT_PROMPT: str = ""  # общий словарь подсказок через запятую (имена, проекты, термины)
    STT_LLM_FIX: bool = True  # исправлять явные ошибки распознавания через LLM по словарю

    # --- Userbot для расшифровки Telegram Premium ---
    TG_API_ID: int | None = None
    TG_API_HASH: str = ""
    TG_SESSION: str = "data/userbot.session"

    # --- Текст с картинок: vision-модель через тот же LLM-шлюз ---
    OCR_MODEL: str = "vision"  # пусто — не распознавать

    # --- Файлы вложений (для агентов) ---
    FILES_DIR: str = "data/files"  # куда бот/MCP сохраняет вложения (путь внутри процесса)
    FILES_HOST_DIR: str = ""  # тот же каталог, как его видит хост (для агентов на ПК)
    # Журнал контекста коворкеров между чатами (на машине воркера)
    AGENT_CONTEXT_DIR: str = "agent-context"

    # --- MCP ---
    MCP_HTTP_ENABLED: bool = True
    # Bearer-токен для /mcp. Пусто — /mcp принимает только запросы с localhost.
    MCP_TOKEN: str = ""
    # Пускать /mcp без токена с этой машины: loopback и шлюз docker-сети (так приходят запросы через
    # проброшенный порт 127.0.0.1:8088). Другим контейнерам и сети токен по-прежнему нужен.
    MCP_TRUST_HOST: bool = True

    @field_validator("ALLOWED_USER_IDS", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.replace(";", ",").split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @field_validator("OWNER_CHAT_ID", "WEBAPP_DEV_USER_ID", "TG_API_ID", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        return None if v == "" else v

    @property
    def workdirs(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in self.AGENT_WORKDIRS.split(";"):
            alias, sep, path = part.partition("=")
            if sep and alias.strip() and path.strip():
                out[alias.strip()] = path.strip().strip('"')
        return out

    @property
    def llm_enabled(self) -> bool:
        return bool(self.LLM_BASE_URL)

    @property
    def webapp_enabled(self) -> bool:
        return bool(self.WEBAPP_URL)

    def mongo_uri(self) -> str:
        if self.MONGO_URI:
            return self.MONGO_URI
        if not self.MONGO_HOST:
            raise ValueError("Не задан ни MONGO_URI, ни MONGO_HOST")
        auth = f"{self.MONGO_USER}:{self.MONGO_PASS}@" if self.MONGO_USER else ""
        return f"mongodb://{auth}{self.MONGO_HOST}:{self.MONGO_PORT}/?authSource={self.MONGO_AUTH_DB}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
