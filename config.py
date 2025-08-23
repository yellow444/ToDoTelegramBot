from pydantic import BaseSettings


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    # Базовые параметры Mongo
    MONGO_HOST: str = ""
    MONGO_PORT: int = 27017
    MONGO_USER: str = ""
    MONGO_PASS: str = ""
    DB_NAME: str
    COLLECTION_NAME: str

    # Опционально: полноценный URI важнее MONGO_HOST/PORT/USER/PASS
    MONGO_URI: str = ""          # например: mongodb://user:pass@host:27017/db?authSource=admin
    MONGO_AUTH_DB: str = "admin" # authSource, если используем хост/порт

    # Telegram/прочее
    TOKEN: str
    MYHOSTNAME: str = ""
    PORT: str = "8080"

    class Config:
        env_file = ".env"


settings = Settings()
