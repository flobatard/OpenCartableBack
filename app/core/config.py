from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # App
    PROJECT_NAME: str = "CartableBack"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = False

    # Database — must be an async driver, e.g. postgresql+asyncpg://...
    DATABASE_URL: str

    # Security
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
