"""run_history_agent — answers one turn of a history conversation. STUB: built in the `agent`
front. The signature is the contract the skill codes against."""
from __future__ import annotations

from typing import Any, Callable, Optional

from .types import AgentResult


async def run_history_agent(
    *, convo: list[dict], store: Any, media: Any, settings: Any,
    notes: Optional[dict] = None, session_lang: Optional[str] = None,
    now_ts: Optional[int] = None, client: Any = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> AgentResult:
    """`convo` is the chat thread as neutral [{role, content}] (what the reason node gives every
    skill); the last user turn holds the owner's question. `notes` is what the previous turn of
    this loop left behind. `on_event` receives one small dict per tool call for the log."""
    return {"state": "keep_listening", "message": "(history agent not built yet)",
            "lang": session_lang or "en", "files": [], "notes": notes or {}, "usage": {},
            "tool_calls": [], "stop_reason": "stub", "error_category": "none"}
