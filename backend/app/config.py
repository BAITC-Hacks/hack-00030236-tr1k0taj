from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "extra": "ignore"}
    database_url: str = "postgresql+asyncpg://voice_router:voice_router@localhost:5432/voice_router"
    openai_api_key: str = ""
    llm_model: str = "gpt-4.1-mini"
    mock_mode: bool = True
    llm_timeout_seconds: float = Field(default=30, ge=1, le=120)
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, ge=1, le=3072)
    embeddings_enabled: bool = True
    kernel_max_segments: int = Field(default=4, ge=1, le=16)
    kernel_response_timeout: float = Field(default=60, ge=1, le=180)
    kernel_background_parallelism: int = Field(default=3, ge=1, le=8)
    kernel_global_parallelism: int = Field(default=8, ge=2, le=64)
    # Starter kit (read-only mount in compose). Loaded into kit_records on startup if empty.
    datasets_dir: str = "/datasets"


settings = Settings()
