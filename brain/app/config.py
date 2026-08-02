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

    # Trigger tag(s). Accepts the old SECRETARY_TAG_NEW var as an alias.
    mary_trigger_tag: str = Field(
        default="@mary",
        validation_alias=AliasChoices("MARY_TRIGGER_TAG", "SECRETARY_TAG_NEW"),
    )

    owner_name: str = "Marcelo"

    # Listening loop + webhook dedup. In-memory fallback when redis_url is unset.
    redis_url: str | None = None
    loop_ttl_seconds: int = 60  # the listening-window TTL

    # Memory: how many WhatsApp messages seed a fresh thread.
    context_window_messages: int = 30

    # Echo store: how long a sent message id is remembered so we never re-ingest our
    # own reply. Only needs to outlive the reseed window; default 7 days.
    echo_ttl_seconds: int = 604800

    # LangGraph checkpointer (Postgres). In-memory fallback when unset (dev/tests).
    database_url: str | None = None

    # Durable loop log (reuses DATABASE_URL; disabled when that is unset). Isolated in
    # its own schema so it never collides with Evolution's or the checkpointer's tables.
    log_enabled: bool = True
    log_schema: str = "mary_log"
    log_queue_max: int = 10_000
    log_retention_events_days: int = 90
    log_retention_loops_days: int = 365

    # Reasoning — provider-neutral selection + Anthropic knobs.
    llm_provider: str = "anthropic"
    anthropic_api_key: str = Field(
        default="", validation_alias=AliasChoices("ANTHROPIC_API_KEY")
    )
    claude_model: str = "claude-opus-4-8"
    claude_effort: str = "high"
    claude_max_tokens: int = 8192
    web_search_max_uses: int = 5

    prompt_version: str = "2026-08-02.4"

    @property
    def tags(self) -> list[str]:
        return [t.strip().lower() for t in self.mary_trigger_tag.split(",") if t.strip()]

    @property
    def primary_tag(self) -> str:
        tags = self.tags
        return tags[0] if tags else "@mary"


def load_settings() -> Settings:
    return Settings()
