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

    # Tools. The read-back loop (list/find/failure -> reason) is bounded so it can't spin.
    max_tool_actions: int = 4

    # Transcription — provider-neutral seam (app/transcription/), AssemblyAI first.
    transcription_enabled: bool = True
    transcription_provider: str = "assemblyai"
    assemblyai_api_key: str = Field(
        default="", validation_alias=AliasChoices("ASSEMBLYAI_API_KEY")
    )
    # Language for the transcript. "auto" → the provider detects it (drives the fast-path
    # reply header without a reasoning pass); or pin an ISO code.
    assemblyai_language: str = "auto"
    transcription_max_poll_seconds: int = 120   # provider poll ceiling
    transcription_request_timeout: float = 60.0  # per-HTTP-call timeout
    transcription_cache_max: int = 512           # in-process LRU size (wa_id -> transcript)
    long_audio_seconds: int = 120                # past this, deliver a .txt instead of inline
    max_context_transcriptions: int = 8          # per-turn cap in the context pass
    transcription_concurrency: int = 4           # semaphore width for the context gather
    # Reactive fast path: how a reply-to-audio is recognised WITHOUT the model.
    transcribe_fuzzy_threshold: float = 0.82     # difflib ratio a token must clear
    transcribe_on_empty_reply: bool = True       # bare @mary on a voice note → transcribe

    # Media context — images & PDFs passed to the model as inline base64 blocks (the twin of the
    # transcription pass; reuses transcription_concurrency for the download gather).
    media_enabled: bool = False                  # master switch; ships off until verified
    max_context_media: int = 8                   # per-turn image/PDF cap in the context pass
    media_max_item_bytes: int = 15_000_000       # per-file ceiling before a marker fallback
    media_request_budget_bytes: int = 28_000_000 # total media/turn — headroom under Claude's 32MB

    # Google Calendar tool — OAuth2 refresh-token client on the owner's own account.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"
    default_meeting_minutes: int = 45
    calendar_timezone: str = "America/Sao_Paulo"

    prompt_version: str = "2026-08-03.4-no-web-claim"

    @property
    def tags(self) -> list[str]:
        return [t.strip().lower() for t in self.mary_trigger_tag.split(",") if t.strip()]

    @property
    def primary_tag(self) -> str:
        tags = self.tags
        return tags[0] if tags else "@mary"


def load_settings() -> Settings:
    return Settings()
