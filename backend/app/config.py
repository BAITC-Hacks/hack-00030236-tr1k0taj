from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://voice_router:voice_router@localhost:5432/voice_router"


settings = Settings()
