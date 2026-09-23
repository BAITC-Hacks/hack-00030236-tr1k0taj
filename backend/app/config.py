from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://voice_router:voice_router@localhost:5432/voice_router"
    # Starter kit (read-only mount in compose). Loaded into kit_records on startup if empty.
    datasets_dir: str = "/datasets"


settings = Settings()
