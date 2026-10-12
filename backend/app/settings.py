from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str
    openrouter_api_key: str = Field("", validation_alias=AliasChoices("OPEN_ROUTER_API_KEY", "OPENROUTER_API_KEY"))
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "z-ai/glm-5.3-flashx"
    llm_fallback_models: str = "z-ai/glm-5.3-flash,openai/gpt-5-mini"
    llm_timeout_s: float = 30
    pii_encryption_key: str
    pii_hash_pepper: str
    langgraph_aes_key: str
    pii_retention_days: int = 90
    jwt_private_key_path: str = str(ROOT / "infra/dev-jwt/private.pem")
    jwks_url: str = ""  # empty = use local dev issuer; on Azure point to Entra ID JWKS
    jwt_audience: str = "onboarding-api"
    langfuse_public_key: str = ""


@lru_cache
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
