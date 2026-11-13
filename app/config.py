from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://app_admin:AppSecurePassword2026@localhost:5433/amos_app_db"
    SECRET_KEY: str = "super-secret-jwt-key-change-this-in-production-2026!"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440 # 24 hours
    UPLOAD_DIR: str = "uploads"
    AMOSCENTRAL_CONFIG_FILE: str = ""
    SEMANTIC_MATCHING: bool = True
    SEMANTIC_MODEL: str = "BAAI/bge-small-en-v1.5"
    SEMANTIC_CACHE_DIR: str = "/tmp/spec2500-models"
    SEMANTIC_INDEX_DIR: str = "uploads/semantic-index"

    RERANK_MODEL: str = "BAAI/bge-reranker-base"
    RERANK_CACHE_DIR: str = "uploads/reranker-models"
    RERANK_TOP_N: int = Field(50, ge=2, le=100)

    class Config:
        env_file = ".env"

settings = Settings()
