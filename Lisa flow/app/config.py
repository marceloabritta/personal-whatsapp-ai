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

    # Programmatic domain orchestrator. `default_domain` is the router's fallback skill (web is
    # read-only, so it is the safe default on a classifier miss). `router_effort` is the cheap
    # reasoning effort for the ambiguity-only domain classifier.
    default_domain: str = "web"
    router_effort: str = "low"

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

    # Auto-transcription — the roster-driven ambient path (app/roster.py + nodes/auto_transcribe).
    # Ships OFF, the way MEDIA_ENABLED did: turn it on per service once it has been watched live.
    auto_transcribe_enabled: bool = False
    auto_transcribe_max_seconds: int = 600    # skip clips longer than this (declared length)
    auto_transcribe_daily_cap: int = 40       # per-chat transcripts/day; 0 = no cap
    auto_transcribe_report_failures: bool = False  # ambient output stays quiet when it fails
    auto_transcribe_quote_reply: bool = True  # reply attached to the audio; off = plain message
    roster_cache_ttl: float = 30.0            # seconds before the gate's snapshot re-reads

    # Setup — the self-chat configuration skill (app/skills/setup.py).
    setup_enabled: bool = True
    # The listening window while a setup loop is open. Leaving the chat, finding a contact and
    # forwarding their card takes far longer than the 60s conversational window.
    setup_window_seconds: int = 300
    setup_group_candidates: int = 5           # groups offered when nothing matches well
    # The owner's own JID, for recognising his chat with himself. Derived from Evolution at boot
    # when left empty (see main.py lifespan).
    owner_jid: str = ""

    # Google Calendar tool — OAuth2 refresh-token client on the owner's own account.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"
    default_meeting_minutes: int = 45
    default_start_hour: str = "09:00:00"   # all-day -> timed, when no hour is given
    calendar_timezone: str = "America/Sao_Paulo"

    # Contact memory — the address book (app/directory.py + app/tools/people.py).
    # Ships OFF, the way MEDIA_ENABLED and AUTO_TRANSCRIBE_ENABLED did.
    contacts_enabled: bool = False
    # A SEPARATE refresh token from the calendar one. Re-minting a single shared token is how
    # the ops step would take the live calendar down — and the same GOOGLE_REFRESH_TOKEN is
    # consumed by both the lisa and mary services, so the blast radius is two brains, not one.
    # Empty = the feature stays off no matter what contacts_enabled says.
    google_contacts_refresh_token: str = ""
    contacts_sync_seconds: float = 900.0      # incremental People sync cadence
    contacts_full_resync_hours: float = 96.0  # forced full sweep, before a sync token can expire
    contacts_max_in_prompt: int = 5           # contacts rendered into one turn's block
    contacts_default_region: str = "BR"       # phonenumbers parse region for bare numbers
    contacts_write_attempts: int = 3          # OUTBOX retries; the API call itself is always 1
    # Q1: Lisa may create people — but only on a phone-confirmed identity (see Directory).
    contacts_create_people: bool = True

    # Session review — a second model grades every turn once a session closes (app/review/).
    # OFF in code so this module ships inert to any flow that has not opted in; turned on per
    # service by env. Everything here is observation only: it never touches the reply path.
    review_enabled: bool = False
    review_scope_version: str = ""      # only review loops with this prompt_version ("" = all)
    review_judge_version: str = "v2"    # the rubric's identity; bump to re-score history
    review_model: str = "claude-opus-4-8"   # NOT claude_model — a model judging itself favours it
    review_effort: str = "medium"
    review_max_tokens: int = 2048
    review_sweep_seconds: int = 60      # how often the reaper looks for quiet loops
    review_settle_seconds: int = 30     # grace past the window TTL, so the log writer has flushed
    review_batch: int = 5               # loops reviewed per sweep
    review_concurrency: int = 4         # parallel judge calls within one loop
    review_max_context_lines: int = 60  # transcript lines shown to the judge; oldest trimmed first

    prompt_version: str = "2026-09-14-all-day-events"

    @property
    def review_settle_window(self) -> float:
        """A loop is reviewable once it has been quiet for longer than the listening window plus
        a grace margin — long enough that the window has truly expired AND the log writer has
        flushed the loop's last events."""
        return float(self.loop_ttl_seconds + self.review_settle_seconds)

    @property
    def owner_key(self) -> str:
        """The owner's chat key — what `is_self_chat` compares against. "" when unconfigured,
        which makes every chat non-self and keeps setup unreachable rather than open."""
        from .whatsapp import chat_key

        return chat_key(self.owner_jid)

    @property
    def tags(self) -> list[str]:
        return [t.strip().lower() for t in self.mary_trigger_tag.split(",") if t.strip()]

    @property
    def primary_tag(self) -> str:
        tags = self.tags
        return tags[0] if tags else "@mary"


def load_settings() -> Settings:
    return Settings()
