"""run_history_agent — answers one turn of a history conversation. The signature is the contract
the skill codes against; types.AgentResult is what it returns.

A manual client-tool loop on AsyncAnthropic, one request per hop:
  - claude-opus-5-5 with adaptive thinking (it cannot be turned off) and effort from settings;
    the custom tools are strict; the answer is enforced JSON (output_config.format). The two
    compose in one request, so the turn that stops calling tools IS the answer.
  - A hop that calls tools has them all run at once, and every tool_result goes back in ONE user
    message (a failed tool is an is_error result, never a dropped one).
  - Bounded twice: settings.history_max_tool_calls tool calls and settings.history_max_seconds of
    wall clock. A bound reached ends the search with one more request, tools disabled
    (tool_choice "none", the same tools still declared so nothing before it changes), that asks
    for the best answer so far and says the search was cut short.
  - Refusals: the request opts into Anthropic's server-side fallback ("default"), so a policy
    decline is retried on another model inside the same call. A refusal that still comes back
    ends the turn silent, like the other skills.
  - Caching: tools, then the stable system prompt (marked), then the turn's own block (clock,
    coverage, notes), then the conversation, with the top-level mark moving to its end on every
    hop. Each hop reads the last one back; the next turn reads back tools + stable prompt.
  - Never raises. Any provider error comes back as error_category "provider" with no message.
  - on_event gets one small dict per tool call: {"tool","args","n","ms","error"}. The args are
    the model's own query; the RESULTS never leave this module, since they are other people's
    messages and must not reach a log."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

from ..reasoning.anthropic import add_usage
from .prompt import ANSWER_NOTE, BUDGET_NOTE, OUTPUT_SCHEMA, system_blocks
from .tools import FILE_KINDS, TOOLS, Outcome, ToolBox, safe_args, zone
from .types import AgentResult

log = logging.getLogger("mary.history.agent")

MAX_TOKENS = 16_000          # per hop: thinking + the answer; non-streaming stays under timeouts
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_CACHE = {"type": "ephemeral"}
_STORE_TIMEOUT = 20.0
_TOOL_TIMEOUTS = {"transcribe_voice_note": 75.0, "open_file": 40.0}
_STATES = ("keep_listening", "awaiting_answer", "close")
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_BLOCK_TYPES = ("text", "image", "document")


async def run_history_agent(
    *, convo: list[dict], store: Any, media: Any, settings: Any,
    notes: Optional[dict] = None, session_lang: Optional[str] = None,
    now_ts: Optional[int] = None, client: Any = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> AgentResult:
    """`convo` is the chat thread as neutral [{role, content}] (what the reason node gives every
    skill); the last user turn holds the owner's question. `notes` is what the previous turn of
    this loop left behind. `on_event` receives one small dict per tool call for the log."""
    started = time.monotonic()
    try:
        loop = _Loop(store=store, media=media, settings=settings, notes=notes,
                     session_lang=session_lang, now_ts=now_ts, on_event=on_event, started=started)
    except Exception as exc:
        log.error("history agent misconfigured: %s", type(exc).__name__)
        return AgentResult(state="keep_listening", message=None, lang=session_lang, files=[],
                           notes=notes if isinstance(notes, dict) else {},
                           usage={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                           tool_calls=[], stop_reason="error", error_category="provider",
                           model=None, provider_request_id=None)
    own = None
    try:
        if client is None:
            import anthropic

            own = client = anthropic.AsyncAnthropic(
                api_key=getattr(settings, "anthropic_api_key", None) or None)
        loop.client = client
        result = await loop.run(convo)
    except Exception as exc:  # never raises: the skill gets a silent, logged failure
        log.error("history agent failed: %s: %s", type(exc).__name__, str(exc)[:300])
        result = loop.result(stop_reason="error", error_category="provider")
    finally:
        if own is not None:
            try:
                await own.close()
            except Exception:
                pass
    log.info(json.dumps({"history_agent": {
        "calls": len(result.get("tool_calls") or []), "hops": loop.hops,
        "ms": int((time.monotonic() - started) * 1000), "stop": result.get("stop_reason"),
        "error": result.get("error_category"), "model": result.get("model"),
        "usage": result.get("usage")}}))
    return result


class _Loop:
    def __init__(self, *, store, media, settings, notes, session_lang, now_ts, on_event,
                 started) -> None:
        s = settings
        self.client: Any = None
        self.store = store
        self.media = media
        self.notes_in = notes if isinstance(notes, dict) else {}
        self.session_lang = session_lang
        self.now_ts = float(now_ts if now_ts is not None else time.time())
        self.on_event = on_event
        self.model = getattr(s, "history_model", None) or "claude-opus-5-5"
        effort = getattr(s, "history_effort", None) or "medium"
        self.effort = effort if effort in _EFFORTS else "medium"
        self.max_calls = max(1, int(getattr(s, "history_max_tool_calls", 25) or 25))
        self.max_seconds = float(getattr(s, "history_max_seconds", 90) or 90)
        self.max_files = max(0, int(getattr(s, "history_max_files", 3) or 0))
        self.cache = bool(getattr(s, "prompt_cache_enabled", True))
        self.tz_name = getattr(s, "calendar_timezone", None) or "UTC"
        self.owner = getattr(s, "owner_name", None) or "the owner"
        # The last stretch of the clock is kept for the answer: no hop starts inside it, and a
        # hop or a tool still running when it begins is stopped.
        self.deadline = started + self.max_seconds
        self.reserve = min(25.0, self.max_seconds * 0.25)
        self.final_min = min(15.0, self.max_seconds * 0.2)
        self.box = ToolBox(store, media, tz=zone(self.tz_name), owner_name=self.owner)
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self.trace: list[dict] = []
        self.n_calls = 0
        self.hops = 0
        self.system: Any = None
        self.question = ""
        self.model_used: Optional[str] = None
        self.request_id: Optional[str] = None

    # --- the loop -------------------------------------------------------------------------

    async def run(self, convo: list) -> dict:
        msgs = _normalize(convo)
        if not msgs or msgs[-1]["role"] != "user":
            return self.result(stop_reason="no_question")
        self.question = " ".join(str(b.get("text") or "") for b in msgs[-1]["content"]
                                 if b.get("type") == "text")
        coverage = await self._coverage()
        self.system = system_blocks(
            owner=self.owner, max_files=self.max_files, tz_name=self.tz_name, now_ts=self.now_ts,
            coverage=coverage, notes=self.notes_in, session_lang=self.session_lang,
            cache=self.cache)

        cut: Optional[str] = None
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= self.reserve:
                cut = "time"
                break
            if self.hops >= self.max_calls + 4:  # pause_turn or tool-less hops cannot spin
                cut = "calls"
                break
            self.hops += 1
            try:
                resp = await self._create(msgs, final=False, timeout=remaining - self.reserve)
            except asyncio.TimeoutError:
                cut = "time"
                break
            self._account(resp)
            stop = _get(resp, "stop_reason")
            content = list(_get(resp, "content") or [])
            if stop == "refusal":
                return self._refused(resp)
            if stop == "pause_turn":
                msgs.append({"role": "assistant", "content": content})
                continue
            if stop == "max_tokens":  # a cut-off hop is dropped: its tool call may be partial
                cut = "max_tokens"
                break
            uses = [b for b in content if _get(b, "type") == "tool_use"]
            if uses:
                msgs.append({"role": "assistant", "content": content})
                msgs.append({"role": "user", "content": await self._run_tools(uses)})
                if self.n_calls >= self.max_calls:
                    cut = "calls"
                    break
                continue
            data = _parse(content)
            if data is None:
                cut = "no_answer"
                break
            return await self._answer(data, stop_reason=stop or "end_turn")
        return await self._final(msgs, cut)

    async def _final(self, msgs: list, cut: Optional[str]) -> dict:
        """One last request, tools off, for the best answer so far."""
        why = {"calls": f"it reached the limit of {self.max_calls} tool calls",
               "time": f"it reached the time limit of {int(self.max_seconds)} seconds",
               "max_tokens": "the last step ran too long"}.get(cut or "")
        note = BUDGET_NOTE.format(why=why) if why else ANSWER_NOTE
        # Appended to the last user message (after its tool results), never inserted earlier:
        # everything before it, thinking included, stays byte for byte what the model saw.
        msgs[-1] = {"role": "user",
                    "content": [*msgs[-1]["content"], {"type": "text", "text": note}]}
        timeout = max(self.deadline - time.monotonic(), self.final_min)
        self.hops += 1
        try:
            resp = await self._create(msgs, final=True, timeout=timeout)
        except asyncio.TimeoutError:
            return self._out_of_time()
        self._account(resp)
        stop = _get(resp, "stop_reason")
        if stop == "refusal":
            return self._refused(resp)
        data = _parse(list(_get(resp, "content") or []))
        if data is None:
            log.error("history agent: no parsable answer (stop=%s)", stop)
            return self.result(stop_reason=stop or "error", error_category="provider")
        if cut in ("calls", "time"):
            out = await self._answer(data, stop_reason="budget")
            # It was told to offer to keep looking; an offer holds the window for his reply.
            if out.get("message") and out.get("state") == "keep_listening":
                out["state"] = "awaiting_answer"
            return out
        return await self._answer(data, stop_reason=stop or "end_turn")

    def _out_of_time(self) -> dict:
        """Not even the final answer came in time. He has already been told she is searching,
        so silence is the worst reply: say it plainly and offer to go on, keeping what was
        searched in the notes for the next turn."""
        lang = self.session_lang or _guess_lang(self.question)
        message = (_OUT_OF_TIME["en"] if (lang or "").startswith("en") else _OUT_OF_TIME["pt"])
        notes = clean_notes(self.notes_in) if self.notes_in else clean_notes({})
        tried = [" ".join(ev["args"].get("terms") or []) for ev in self.trace
                 if ev.get("tool") == "search_messages" and isinstance(ev.get("args"), dict)]
        notes["searched"] = (notes["searched"] + [t for t in tried if t])[:15]
        return self.result(state="awaiting_answer", message=message,
                           lang="en" if (lang or "").startswith("en") else "pt", notes=notes,
                           stop_reason="budget", error_category="budget")

    async def _create(self, messages: list, *, final: bool, timeout: float):
        kw: dict = dict(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=self.system,
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort,
                           "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
        if self.cache:
            kw["cache_control"] = _CACHE
        if final:
            kw["tool_choice"] = {"type": "none"}
        beta = getattr(getattr(self.client, "beta", None), "messages", None)
        if beta is not None:
            coro = beta.create(**kw, betas=[FALLBACK_BETA], fallbacks="default")
        else:
            coro = self.client.messages.create(**kw)
        return await asyncio.wait_for(coro, timeout=max(0.5, timeout))

    async def _coverage(self) -> Optional[dict]:
        try:
            cov = await asyncio.wait_for(self.store.coverage(), timeout=5.0)
            return cov if isinstance(cov, dict) else None
        except Exception as exc:
            log.warning("history coverage unavailable: %s", type(exc).__name__)
            return None

    # --- tools ----------------------------------------------------------------------------

    async def _run_tools(self, uses: list) -> list[dict]:
        room = max(0, self.max_calls - self.n_calls)
        run, skip = uses[:room], uses[room:]
        self.n_calls += len(run)
        budget = self.deadline - time.monotonic() - self.reserve
        results = list(await asyncio.gather(*(self._one(b, budget) for b in run)))
        for b in skip:
            self._event({"tool": _get(b, "name"), "args": safe_args(_get(b, "input")), "n": 0,
                         "ms": 0, "error": "budget"})
            results.append({"type": "tool_result", "tool_use_id": _get(b, "id"), "is_error": True,
                            "content": f"Not run: the limit of {self.max_calls} tool calls for "
                                       f"this turn is reached."})
        return results

    async def _one(self, block: Any, budget: float) -> dict:
        name = _get(block, "name") or "?"
        args = _get(block, "input")
        limit = min(_TOOL_TIMEOUTS.get(name, _STORE_TIMEOUT), max(0.1, budget))
        t0 = time.monotonic()
        try:
            out = await asyncio.wait_for(self.box.run(name, args), timeout=limit)
        except asyncio.TimeoutError:
            out = Outcome(f"{name} did not finish in time and was stopped.", 0, "timeout")
        except Exception as exc:
            log.warning("history tool %s failed: %s", name, type(exc).__name__)
            out = Outcome(f"{name} failed ({type(exc).__name__}). Try another way, or go on "
                          f"without it.", 0, "failed")
        self._event({"tool": name, "args": safe_args(args), "n": out.n,
                     "ms": int((time.monotonic() - t0) * 1000), "error": out.error})
        result = {"type": "tool_result", "tool_use_id": _get(block, "id"), "content": out.content}
        if out.error:
            result["is_error"] = True
        return result

    def _event(self, ev: dict) -> None:
        self.trace.append(ev)
        if self.on_event is not None:
            try:
                self.on_event(dict(ev))
            except Exception as exc:
                log.warning("history on_event failed: %s", type(exc).__name__)

    # --- results --------------------------------------------------------------------------

    def _account(self, resp: Any) -> None:
        _fold_usage(self.usage, _get(resp, "usage"))
        self.model_used = _get(resp, "model") or self.model_used
        self.request_id = getattr(resp, "_request_id", None) or self.request_id
        if any(_get(b, "type") == "fallback" for b in (_get(resp, "content") or [])):
            log.info("history agent: served by fallback model %s", self.model_used)

    def _refused(self, resp: Any) -> dict:
        details = _get(resp, "stop_details")
        log.warning("history agent: refusal (category=%s)", _get(details, "category"))
        return self.result(stop_reason="refusal")

    async def _answer(self, data: dict, *, stop_reason: str) -> dict:
        state = data.get("state") if data.get("state") in _STATES else "keep_listening"
        message = data.get("message")
        message = message.strip() if isinstance(message, str) else None
        lang = data.get("lang") if isinstance(data.get("lang"), str) and data.get("lang") else None
        reasoning = data.get("reasoning") if isinstance(data.get("reasoning"), str) else None
        return self.result(state=state, message=message or None, lang=lang, reasoning=reasoning,
                           files=await self._files(data.get("files")),
                           notes=clean_notes(data.get("notes")), stop_reason=stop_reason)

    async def _files(self, raw: Any) -> list[str]:
        """The files to send: real file messages only, each once, at most max_files."""
        out: list[str] = []
        for mid in raw if isinstance(raw, list) else []:
            if len(out) >= self.max_files:
                break
            if not isinstance(mid, str) or not mid.strip() or mid.strip() in out:
                continue
            mid = mid.strip()
            hit = self.box.seen.get(mid)
            if hit is None:
                try:
                    hit = await asyncio.wait_for(self.store.get_message(mid), timeout=5.0)
                except Exception:
                    hit = None
            if hit and hit.get("kind") in FILE_KINDS:
                out.append(mid)
            else:
                log.info("history agent: dropped a file id that is not a file message")
        return out

    def result(self, *, state: str = "keep_listening", message: Optional[str] = None,
               lang: Optional[str] = None, reasoning: Optional[str] = None,
               files: Optional[list] = None, notes: Optional[dict] = None, stop_reason: str,
               error_category: str = "none") -> dict:
        return AgentResult(
            state=state, message=message, lang=lang or self.session_lang, reasoning=reasoning,
            files=list(files or []),
            # A turn that produced no answer keeps the last notes, so the next one loses nothing.
            notes=notes if notes is not None else dict(self.notes_in),
            usage=dict(self.usage), tool_calls=list(self.trace), stop_reason=stop_reason,
            error_category=error_category, model=self.model_used or self.model,
            provider_request_id=self.request_id,
        )


# --- helpers ----------------------------------------------------------------------------------

_OUT_OF_TIME = {
    "pt": "Não consegui terminar a busca a tempo. Quer que eu continue procurando?",
    "en": "I couldn't finish the search in time. Want me to keep looking?",
}
_PT_HINTS = ("ã", "õ", "ç", "ê", "é", "á", "ó", "você", "quem", "não", "qual", "quando", "onde",
             "manda", "conversa", "falei", "lembro")


def _guess_lang(question: str) -> str:
    """pt or en, from his question's words: only for the canned out-of-time line."""
    text = (question or "").lower()
    return "pt" if any(h in text for h in _PT_HINTS) else "en"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _fold_usage(usage: dict, u: Any) -> None:
    """A response's tokens into the turn's totals. With a server-side fallback the top-level
    usage covers only the attempt that answered; `iterations` lists every attempt."""
    if u is None:
        return
    its = _get(u, "iterations")
    if isinstance(its, list) and len(its) > 1:
        for it in its:
            add_usage(usage, it)
    else:
        add_usage(usage, u)


def _parse(content: list) -> Optional[dict]:
    text = "".join(_get(b, "text") or "" for b in content if _get(b, "type") == "text").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        start = text.find("{")
        if start < 0:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def _as_blocks(content: Any) -> list[dict]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, str):
                if c.strip():
                    out.append({"type": "text", "text": c})
            elif isinstance(c, dict) and c.get("type") in _BLOCK_TYPES:
                if c["type"] == "text" and not str(c.get("text") or "").strip():
                    continue
                out.append({k: v for k, v in c.items() if k != "cache_control"})
        return out
    return [{"type": "text", "text": str(content)}]


def _normalize(convo: Any) -> list[dict]:
    """The neutral thread as Anthropic messages: user/assistant alternating, starting and (for a
    question to answer) ending with the user. Earlier assistant turns keep their text only.

    Text only, on both sides. The thread is Lisa's view of his chat with himself, and with media on
    the context pass puts the newest images and PDFs there (up to 8) as blocks: his photos, not the
    question. The search reads files through open_file when it needs one; carried here they went
    to the model on every hop of the loop. On 2026-11-16 eight photos made the first search's
    request ~40k tokens bigger than the question needed, about three quarters of its cost. The
    labelled transcript already marks each one ("[image]", "[PDF: name]"), so nothing is lost."""
    out: list[dict] = []
    for m in convo or []:
        role = "assistant" if _get(m, "role") in ("assistant", "ai") else "user"
        blocks = [b for b in _as_blocks(_get(m, "content")) if b.get("type") == "text"]
        if not blocks:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def clean_notes(raw: Any) -> dict:
    """The notes the model wrote, in their shape and cut to size: they ride in the checkpoint and
    come back into the next turn's prompt."""
    if not isinstance(raw, dict):
        return {}

    def s(v: Any, n: int) -> str:
        return v.strip()[:n] if isinstance(v, str) else ""

    def strs(v: Any, n: int, each: int) -> list[str]:
        return [x.strip()[:each] for x in v if isinstance(x, str) and x.strip()][:n] \
            if isinstance(v, list) else []

    cands = []
    for c in raw.get("candidates") or []:
        if isinstance(c, dict):
            cands.append({"chat": s(c.get("chat"), 100), "name": s(c.get("name"), 100),
                          "msg_ids": strs(c.get("msg_ids"), 8, 100), "why": s(c.get("why"), 300)})
    files = []
    for f in raw.get("files") or []:
        if isinstance(f, dict) and s(f.get("msg_id"), 100):
            files.append({"msg_id": s(f.get("msg_id"), 100), "name": s(f.get("name"), 150),
                          "chat": s(f.get("chat"), 100)})
    return {
        "looking_for": s(raw.get("looking_for"), 300),
        "established": s(raw.get("established"), 800),
        "candidates": cands[:6],
        "files": files[:10],
        "searched": strs(raw.get("searched"), 15, 120),
        "open_question": s(raw.get("open_question"), 300),
    }
