"""Settings — one place, read from env / .env. No secrets in code."""
from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Evolution API (the WhatsApp gateway we call directly).
    evolution_url: str = "http://api:8080"
    evolution_apikey: str = ""
    evolution_instance: str = "secretaria"

    # Trigger tag(s). Accepts the old SECRETARY_TAG_NEW var as an alias so the
    # existing compose keeps working without a rename.
    mary_trigger_tag: str = Field(
        default="@mary",
        validation_alias=AliasChoices("MARY_TRIGGER_TAG", "SECRETARY_TAG_NEW"),
    )

    owner_name: str = "Marcelo"
    ack_text: str = "🌿 Mary here — listening."

    # Session marker + webhook dedup. In-memory fallback when redis_url is unset.
    redis_url: str | None = None
    session_ttl: int = 1800

    @property
    def tags(self) -> list[str]:
        """The accepted trigger tags, lowercased and de-blanked."""
        return [t.strip().lower() for t in self.mary_trigger_tag.split(",") if t.strip()]


def load_settings() -> Settings:
    return Settings()
