from datetime import date

from pydantic_settings import BaseSettings

# «Сегодня» набора данных (ADR 0002). date.today() в доменной логике не используем.
DATASET_TODAY = date(2026, 10, 1)


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://voice_router:voice_router@localhost:5432/voice_router"
    # Starter kit (read-only mount in compose). Loaded into kit_records on startup if empty.
    datasets_dir: str = "/datasets"
    # Провайдеры этапов (ADR 0008). mock — запуск без ключей с понятными ошибками.
    stt_provider: str = "mock"
    llm_provider: str = "mock"
    tts_provider: str = "mock"


settings = Settings()
