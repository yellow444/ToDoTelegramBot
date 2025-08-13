from pydantic import BaseSettings


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    MONGO_HOST: str
    MONGO_PORT: int
    MONGO_USER: str
    MONGO_PASS: str
    DB_NAME: str
    COLLECTION_NAME: str
    TOKEN: str
    MYHOSTNAME: str
    PORT: str

    class Config:
        env_file = ".env"


settings = Settings()

