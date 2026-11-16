"""Live eval — does the history agent find what he asks for?

Runs `run_history_agent` once per case of a gold set — questions he would really ask, each with a
verified answer — against the real index (PgHistoryStore over lisa_search), the real media seam
and the real model, and grades every answer twice:

  - in code: the names, groups and files a right answer must mention (accent- and case-blind),
    the file it must send back, and for a "not found" or "which one?" case that it does not make
    a confident claim;
  - by a judge model (HISTORY_EVAL_JUDGE_MODEL, default claude-sonnet-5-5, enforced JSON), which is
    shown only the question, the expected answer and the agent's reply.

The gold set is PRIVATE: it holds real names and message ids, so it lives outside every repo and
is passed in by path. Nothing from it is written anywhere but stdout. The JSON report holds case
ids, categories, verdicts, check names and costs — no question, answer, name or message text.

It needs the index, ANTHROPIC_API_KEY and the Evolution settings, so it runs on the droplet, in a
throwaway container from the lisa image with this folder and the gold file mounted read-only and
the live service's env passed in (the env never touches the disk):

    docker run --rm --env-file <(docker exec lisa_brain env) \\
      -e HISTORY_EVAL_PATH=/eval/gold.json -v /root/history-eval:/eval:ro \\
      -v "/opt/personal-whatsapp-ai/Lisa flow/tests:/app/tests:ro" evolution-lisa \\
      python tests/live_history.py

Nothing is sent to anyone: the media seam's send_to_owner is replaced by a no-op here, and the
agent's `files` are graded, not sent. On-demand transcription stays on (it is how the agent reads
a voice note), and like every transcript it is cached; HISTORY_EVAL_TRANSCRIBE=0 turns it off.

Env:
  HISTORY_EVAL_PATH         the gold JSON (required; refuses to run without it)
  HISTORY_EVAL_REPORT       where the JSON report goes (default /tmp/history_eval_<time>.json)
  HISTORY_EVAL_CONCURRENCY  cases in flight at once (default 3)
  HISTORY_EVAL_THRESHOLD    exits 1 when the pass rate is below it (default 0.7)
  HISTORY_EVAL_JUDGE        0 = code checks only
  HISTORY_EVAL_JUDGE_MODEL  default claude-sonnet-5-5
  LIVE_ONLY=a01,f02         just these case ids
  REPEAT=n                  each case n times (LIVE_REPEAT also read; default 1)

Gold format (a list, or {"cases": [...]}): each case has id, question, lang, category,
answer_known and expected = {answer, behaviour ("answer" | "not_found" | "clarify"), chats,
chat_names, msg_ids, date_range, must_mention, any_of, forbid, files, file_may_be_expired}.
`must_mention`: every entry must appear in the reply; "a|b" accepts either. `any_of`: a list of
groups, passing when every entry of any one group appears; "<not_found>" stands for the built-in
"I could not find it" phrases. Cases with answer_known=false run and print, but stay out of the
pass rate.

Verdicts: PASS = the judge passes it and every hard code check holds. PARTIAL = the judge says
partial, or passes it while a hard check fails, or there is no judge verdict but the code checks
hold. FAIL = the judge fails it, the agent errored or timed out, or with no judge a hard check
fails. Pass rate = PASS / graded runs; PARTIAL counts half in the printed score only.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Awaitable, Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

JUDGE_MODEL = "claude-sonnet-5-5"
# USD per million tokens. The agent's prices are Opus 5.5's; a cache write is priced at 1.25x input.
# `usage.input` is everything sent (cache reads and writes included); see cost().
AGENT_PRICES = {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0}
JUDGE_PRICES = {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.5}

NOT_FOUND = "<not_found>"
# How an honest "it isn't there" reads, in both of his languages. Folded (see fold()).
NOT_FOUND_PHRASES = (
    "nao encontrei", "nao achei", "nao localizei", "nao consegui encontrar", "nao consegui achar",
    "nao ha registro", "nao ha nenhuma", "nao ha mensage", "nao ha mencao", "nao tem registro",
    "nenhuma mensagem", "nenhum registro", "nenhuma mencao", "nenhum resultado", "nada sobre",
    "nao aparece", "nao vejo", "nao existe", "sem registro", "sem mencao", "nao chega",
    "nao alcanca", "nao cobre", "nao vai ate", "so comeca", "comeca em",
    "couldn't find", "could not find", "didn't find", "did not find", "can't find", "cannot find",
    "no messages", "no message", "no record", "no mention", "nothing about", "not found",
    "found nothing", "no results", "doesn't go back", "does not go back", "only starts",
)
CATEGORIES = ("literal", "paraphrase", "multi_clue", "time_anchored", "group", "file", "voice",
              "negative", "ambiguous", "open")


# ============================ the gold set ===================================================

class GoldError(ValueError):
    pass


def load_gold(path: str) -> list[dict]:
    """The cases, checked enough that a typo fails here and not forty minutes into a run."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    cases = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(cases, list) or not cases:
        raise GoldError("no cases")
    seen: set[str] = set()
    for i, c in enumerate(cases):
        cid = c.get("id")
        if not cid or not isinstance(cid, str):
            raise GoldError(f"case #{i} has no id")
        if cid in seen:
            raise GoldError(f"duplicate id {cid}")
        seen.add(cid)
        if not (c.get("question") or "").strip():
            raise GoldError(f"{cid}: no question")
        exp = c.get("expected")
        if not isinstance(exp, dict):
            raise GoldError(f"{cid}: no expected")
        if exp.get("behaviour", "answer") not in ("answer", "not_found", "clarify"):
            raise GoldError(f"{cid}: unknown behaviour {exp.get('behaviour')!r}")
        for key in ("chats", "chat_names", "msg_ids", "must_mention", "files", "forbid"):
            if not isinstance(exp.get(key, []), list):
                raise GoldError(f"{cid}: expected.{key} must be a list")
        for group in exp.get("any_of") or []:
            if not isinstance(group, list) or not group:
                raise GoldError(f"{cid}: expected.any_of must be a list of non-empty lists")
    return cases


def select(cases: list[dict], only: set[str]) -> list[dict]:
    return [c for c in cases if not only or c["id"] in only]


# ============================ code checks ====================================================

def fold(text: Any) -> str:
    """Lowercase, no accents, one space between words: 'Não ACHEI' == 'nao achei'."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("’", "'").casefold()
    return re.sub(r"\s+", " ", s).strip()


def mentions(text: str, entry: str) -> bool:
    """Does the (already folded) text carry this entry? "a|b" = either; <not_found> = the phrases."""
    if entry == NOT_FOUND:
        return any(p in text for p in NOT_FOUND_PHRASES)
    return any(fold(alt) in text for alt in entry.split("|") if fold(alt))


def _check(name: str, ok: bool, *, hard: bool = True, detail: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "hard": hard, "detail": detail}


def _trail(result: dict) -> str:
    """Where the agent shows its evidence without saying it: notes, files and the tool calls'
    arguments. Searched for the expected chat keys and message ids."""
    parts = [result.get("notes") or {}, result.get("files") or [],
             [tc.get("args") for tc in (result.get("tool_calls") or []) if isinstance(tc, dict)]]
    return json.dumps(parts, ensure_ascii=False, default=str)


def deterministic_checks(case: dict, result: Optional[dict]) -> list[dict]:
    """Every code check of one reply. `detail` may quote the gold, so it goes to stdout only."""
    exp = case["expected"]
    behaviour = exp.get("behaviour", "answer")
    result = result or {}
    msg = result.get("message") or ""
    text = fold(msg)
    errored = (result.get("error_category") or "none") != "none"
    out = [_check("answered", bool(msg.strip()) and not errored,
                  detail=f"stop={result.get('stop_reason')} err={result.get('error_category')}")]

    for i, entry in enumerate(exp.get("must_mention") or []):
        out.append(_check(f"mention[{i}]", mentions(text, entry), detail=entry))

    groups = [list(g) for g in (exp.get("any_of") or [])]
    if not groups and behaviour == "not_found":
        groups = [[NOT_FOUND]]
    if not groups and behaviour == "clarify":
        groups = [["?"]]
    if groups:
        hit = any(all(mentions(text, e) for e in g) for g in groups)
        out.append(_check("any_of", hit, detail=" OR ".join("+".join(g) for g in groups)))

    for i, entry in enumerate(exp.get("forbid") or []):
        out.append(_check(f"forbid[{i}]", not mentions(text, entry), detail=entry))

    sent = [str(f) for f in (result.get("files") or [])]
    want = set(exp.get("files") or [])
    if behaviour == "answer" and want:
        out.append(_check("file", bool(want & set(sent)), hard=not exp.get("file_may_be_expired"),
                          detail=f"sent {len(sent)}"))
    if behaviour != "answer":
        out.append(_check("no_files", not sent, detail=f"sent {len(sent)}"))

    keys = list(exp.get("chats") or []) + list(exp.get("msg_ids") or []) + list(want)
    if keys and behaviour == "answer":
        trail = _trail(result)
        out.append(_check("evidence", any(k in trail for k in keys), hard=False,
                          detail="an expected chat key or msg id in notes/files/tool args"))
    return out


def combine(checks: list[dict], judge: Optional[dict], *, agent_failed: bool = False) -> str:
    if agent_failed:
        return "FAIL"
    hard_ok = all(c["ok"] for c in checks if c["hard"])
    verdict = (judge or {}).get("verdict")
    if verdict == "pass":
        return "PASS" if hard_ok else "PARTIAL"
    if verdict == "partial":
        return "PARTIAL"
    if verdict == "fail":
        return "FAIL"
    return "PARTIAL" if hard_ok else "FAIL"      # no judge verdict: the code checks alone


# ============================ cost ===========================================================

def cost(usage: Optional[dict], prices: dict) -> float:
    """The agent's `input` is everything it sent, cache reads and writes included (the house
    meaning, reasoning/anthropic.add_usage); the judge's has no cache parts. Either way the
    uncached part is what is left once the two cache counts are taken out."""
    u = usage or {}
    cr, cw = (u.get("cache_read") or 0), (u.get("cache_write") or 0)
    uncached = max(0, (u.get("input") or 0) - cr - cw)
    return (uncached * prices.get("input", 0.0) + (u.get("output") or 0) * prices.get("output", 0.0)
            + cr * prices.get("cache_read", 0.0) + cw * prices.get("cache_write", 0.0)) / 1_000_000


# ============================ the judge ======================================================

JUDGE_SYSTEM = """You grade one answer from an assistant that searches its owner's WhatsApp history.
The owner asked a question; a person who checked the messages wrote the expected answer. Compare the
assistant's reply with it.

- pass: it gives what the expected answer requires (the right person, group, project, file, date or
  time), and nothing it says contradicts it. Extra correct detail, another language or a short
  style are fine. When a file was asked for, sending it counts; the reply need not repeat its name.
- partial: right direction but incomplete or blurred: one of two required names, the right person
  with a wrong date, the right answer hedged among wrong candidates, or found but a key part asked
  for is missing.
- fail: a wrong answer, an invented fact presented as found, "not found" when the expected answer
  exists, or no answer.

When the expected behaviour is not_found, pass only a reply that says it could not find it (offering
near matches is fine); any confident claim of a sender, date or content is a fail. When it is
clarify, pass a reply that asks which one is meant, or that clearly presents every candidate
apart; picking one candidate as if it were the only one is partial at best.

Write `why` in one or two plain sentences."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["pass", "partial", "fail"]},
                   "why": {"type": "string"}},
    "required": ["verdict", "why"],
    "additionalProperties": False,
}


def judge_message(case: dict, reply: str, n_files: int) -> str:
    """Only what the judge needs: no chat keys, message ids, evidence notes or checks."""
    exp = case["expected"]
    alts = case.get("acceptable_alternatives") or []
    lines = [f"Question: {case['question']}",
             f"Expected behaviour: {exp.get('behaviour', 'answer')}",
             f"Expected answer: {exp.get('answer') or '(see behaviour)'}"]
    if alts:
        lines.append("Also acceptable: " + "; ".join(str(a) for a in alts))
    lines += [f"Files attached to the reply: {n_files}", "", "Assistant's reply:", reply or "(empty)"]
    return "\n".join(lines)


class Judge:
    def __init__(self, api_key: str, model: str = JUDGE_MODEL, client: Any = None) -> None:
        self.model = model
        self._key = api_key
        self._client = client

    def _c(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self._key)
        return self._client

    async def __call__(self, case: dict, reply: str, n_files: int) -> dict:
        """{"verdict", "why", "usage"}; verdict None when the call or the JSON failed."""
        try:
            resp = await self._c().messages.create(
                model=self.model, max_tokens=600, system=JUDGE_SYSTEM,
                messages=[{"role": "user", "content": judge_message(case, reply, n_files)}],
                thinking={"type": "disabled"},
                output_config={"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
            )
        except Exception as exc:  # transport, auth, rate limit: graded on the code checks alone
            return {"verdict": None, "why": f"judge error: {type(exc).__name__}: {str(exc)[:200]}",
                    "usage": {}}
        text = "".join(getattr(b, "text", "") for b in resp.content
                       if getattr(b, "type", None) == "text")
        u = getattr(resp, "usage", None)
        usage = {"input": getattr(u, "input_tokens", 0) or 0,
                 "output": getattr(u, "output_tokens", 0) or 0}
        try:
            data = json.loads(text)
            verdict = data.get("verdict")
            if verdict not in ("pass", "partial", "fail"):
                raise ValueError(verdict)
            return {"verdict": verdict, "why": str(data.get("why") or ""), "usage": usage}
        except (ValueError, TypeError, AttributeError):
            return {"verdict": None, "why": "judge returned unparseable JSON", "usage": usage}


# ============================ one run ========================================================

def question_turn(question: str, *, tag: str, owner: str, tz_name: str, ts: int) -> list[dict]:
    """The question as the context node hands it to a skill: one labelled user turn from the
    owner, in his chat with himself, tagged as he would tag it."""
    from app.whatsapp import build_labeled_transcript

    text = question.strip()
    if not text.startswith("@"):
        text = f"{tag} {text}"
    line = build_labeled_transcript(
        [{"text": text, "from_me": True, "ts": ts, "push_name": owner}], owner, tz_name)
    return [{"role": "user", "content": line}]


AgentFn = Callable[[dict], Awaitable[dict]]
JudgeFn = Callable[[dict, str, int], Awaitable[dict]]


async def run_one(case: dict, rep: int, agent: AgentFn, judge: Optional[JudgeFn], *,
                  timeout: float) -> dict:
    t0 = time.monotonic()
    result: Optional[dict] = None
    error = None
    try:
        result = await asyncio.wait_for(agent(case), timeout=timeout)
    except asyncio.TimeoutError:
        error = "timeout"
    except Exception as exc:  # a crash is a failed run, with its reason
        error = f"{type(exc).__name__}: {exc}"
    latency = time.monotonic() - t0
    failed = error is not None or not isinstance(result, dict)
    checks = deterministic_checks(case, result if isinstance(result, dict) else None)
    reply = (result or {}).get("message") or ""
    j = None
    if judge is not None and not failed:
        j = await judge(case, reply, len((result or {}).get("files") or []))
    verdict = combine(checks, j, agent_failed=failed)
    usage = (result or {}).get("usage") or {}
    return {
        "id": case["id"], "rep": rep, "category": case.get("category") or "?",
        "lang": case.get("lang"), "graded": bool(case.get("answer_known", True)),
        "verdict": verdict, "latency_s": round(latency, 2),
        "tool_calls": len((result or {}).get("tool_calls") or []),
        "usage": {k: int(usage.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write")},
        "cost_usd": round(cost(usage, AGENT_PRICES), 5),
        "judge_verdict": (j or {}).get("verdict"),
        "judge_cost_usd": round(cost((j or {}).get("usage"), JUDGE_PRICES), 5),
        "stop_reason": (result or {}).get("stop_reason"),
        "error": error or ((result or {}).get("error_category")
                           if (result or {}).get("error_category") not in (None, "none") else None),
        "n_files": len((result or {}).get("files") or []),
        "checks": checks,
        # stdout only — never written to the report:
        "_why": (j or {}).get("why") or "", "_reply": reply,
    }


async def run_eval(cases: list[dict], agent: AgentFn, judge: Optional[JudgeFn], *,
                   concurrency: int = 3, repeat: int = 1, timeout: float = 150.0,
                   on_done: Optional[Callable[[dict], None]] = None) -> list[dict]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def guarded(case: dict, rep: int) -> dict:
        async with sem:
            run = await run_one(case, rep, agent, judge, timeout=timeout)
        if on_done:
            on_done(run)
        return run

    return list(await asyncio.gather(*[guarded(c, r) for c in cases for r in range(repeat)]))


# ============================ results ========================================================

def summarise(runs: list[dict]) -> dict:
    graded = [r for r in runs if r["graded"]]
    verdicts = Counter(r["verdict"] for r in graded)
    n = len(graded)
    by_cat: dict = defaultdict(Counter)
    for r in graded:
        by_cat[r["category"]][r["verdict"]] += 1
    return {
        "runs": len(runs), "graded": n,
        "pass": verdicts["PASS"], "partial": verdicts["PARTIAL"], "fail": verdicts["FAIL"],
        "pass_rate": round(verdicts["PASS"] / n, 4) if n else 0.0,
        "score": round((verdicts["PASS"] + 0.5 * verdicts["PARTIAL"]) / n, 4) if n else 0.0,
        "by_category": {c: dict(v) for c, v in sorted(by_cat.items())},
        "agent_cost_usd": round(sum(r["cost_usd"] for r in runs), 4),
        "judge_cost_usd": round(sum(r["judge_cost_usd"] for r in runs), 4),
        "latency_avg_s": round(sum(r["latency_s"] for r in runs) / len(runs), 2) if runs else 0.0,
        "latency_max_s": max((r["latency_s"] for r in runs), default=0.0),
        "tool_calls_avg": round(sum(r["tool_calls"] for r in runs) / len(runs), 2) if runs else 0.0,
    }


def report_record(run: dict) -> dict:
    """A run as the report holds it: no question, reply, judge reasoning or gold strings."""
    rec = {k: v for k, v in run.items() if not k.startswith("_")}
    rec["checks"] = [{"name": c["name"], "ok": c["ok"], "hard": c["hard"]} for c in run["checks"]]
    return rec


def write_report(path: str, runs: list[dict], summary: dict, meta: dict) -> None:
    body = {"meta": meta, "summary": summary,
            "runs": [report_record(r) for r in sorted(runs, key=lambda r: (r["id"], r["rep"]))]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(body, fh, ensure_ascii=False, indent=2)


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def format_run(run: dict, *, verbose: bool = True) -> str:
    u = run["usage"]
    tag = "" if run["graded"] else " (open)"
    head = (f"[{run['verdict']}]{tag} {run['id']}#{run['rep']} {run['category']} "
            f"{run['latency_s']:.1f}s tools={run['tool_calls']} in={_k(u['input'])} "
            f"out={_k(u['output'])} cr={_k(u['cache_read'])} ${run['cost_usd']:.3f}"
            + (f" err={run['error']}" if run["error"] else ""))
    if not verbose:
        return head
    lines = [head]
    if run["_why"]:
        lines.append(f"    judge({run['judge_verdict']}): {run['_why']}")
    bad = [c for c in run["checks"] if not c["ok"]]
    for c in bad:
        lines.append(f"    - {'hard' if c['hard'] else 'soft'} {c['name']} failed: {c['detail']}")
    if run["verdict"] != "PASS" or not run["graded"]:
        reply = " ".join(run["_reply"].split())
        lines.append(f"    reply: {reply[:600]}{'…' if len(reply) > 600 else ''}")
    return "\n".join(lines)


def format_summary(s: dict) -> str:
    lines = ["", "by category:"]
    for cat, v in s["by_category"].items():
        tot = sum(v.values())
        lines.append(f"  {cat:<14} {v.get('PASS', 0)}/{tot} pass, {v.get('PARTIAL', 0)} partial, "
                     f"{v.get('FAIL', 0)} fail")
    lines.append(f"\n{s['pass']} passed, {s['partial']} partial, {s['fail']} failed of {s['graded']} "
                 f"graded runs — pass rate {s['pass_rate']:.0%}, score {s['score']:.0%}")
    lines.append(f"cost: agent ${s['agent_cost_usd']:.2f} + judge ${s['judge_cost_usd']:.2f}; "
                 f"latency avg {s['latency_avg_s']:.1f}s max {s['latency_max_s']:.1f}s; "
                 f"tool calls avg {s['tool_calls_avg']}")
    return "\n".join(lines)


# ============================ live wiring ====================================================

class NoSendMedia:
    """The media seam, minus the one thing an eval must never do: send a file to anyone."""

    def __init__(self, inner: Any, *, allow_transcribe: bool = True) -> None:
        self.inner = inner
        self.allow_transcribe = allow_transcribe
        self.blocked_sends: list = []

    async def transcribe(self, msg_id: str) -> dict:
        if not self.allow_transcribe:
            return {"ok": False, "text": "", "error": "disabled"}
        return await self.inner.transcribe(msg_id)

    async def fetch(self, msg_id: str, **kw) -> dict:
        return await self.inner.fetch(msg_id, **kw)

    async def send_to_owner(self, msg_id: str, *, caption: str = "") -> dict:
        self.blocked_sends.append(msg_id)
        return {"ok": True, "error": None, "sent_id": None}

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


async def build_live(settings):
    """The store, media seam and settings the way the app builds them (runtime.build_runtime),
    with HISTORY_ENABLED forced on for this process only. Returns (runtime, media, closers)."""
    from app.cache import TranscriptionService, TranscriptStore
    from app.clients.evolution import Evolution
    from app.history.runtime import build_runtime
    from app.transcription import build_transcriber

    s = settings.model_copy(update={"history_enabled": True})
    evolution = Evolution(s.evolution_url, s.evolution_apikey, s.evolution_instance)
    transcription = TranscriptionService(evolution, build_transcriber(s), s)
    closers: list = []
    if s.database_url and s.transcription_enabled:
        tstore = TranscriptStore(s.database_url, schema=s.log_schema)
        try:
            await tstore.open()
            transcription.store = tstore
            closers.append(tstore)
        except Exception as exc:
            print(f"(transcript cache off: {exc})")
    rt = build_runtime(s, evolution, transcription)
    try:
        if rt is None:
            raise RuntimeError("no history runtime: DATABASE_URL is unset")
        await rt.store.open()
    except BaseException:
        for c in closers:
            await c.aclose()
        raise
    closers.append(rt.store)
    media = NoSendMedia(rt.media, allow_transcribe=os.environ.get("HISTORY_EVAL_TRANSCRIBE", "1") != "0")
    return rt, media, closers


def live_agent(rt, media) -> AgentFn:
    from app.history.agent import run_history_agent

    s = rt.settings

    async def agent(case: dict) -> dict:
        now = int(time.time())
        convo = question_turn(case["question"], tag=s.primary_tag, owner=s.owner_name,
                              tz_name=s.calendar_timezone, ts=now)
        return await run_history_agent(convo=convo, store=rt.store, media=media, settings=s,
                                       notes=None, session_lang=None, now_ts=now)

    return agent


def _env_int(name: str, default: int, *alts: str) -> int:
    for key in (name, *alts):
        if os.environ.get(key):
            return int(os.environ[key])
    return default


async def main() -> int:
    path = os.environ.get("HISTORY_EVAL_PATH")
    if not path:
        print("Needs HISTORY_EVAL_PATH: the private gold JSON. See the top of this file.")
        return 2
    try:
        cases = load_gold(path)
    except (OSError, ValueError) as exc:
        print(f"Cannot read the gold set: {exc}")
        return 2
    only = {x for x in os.environ.get("LIVE_ONLY", "").split(",") if x}
    cases = select(cases, only)
    if not cases:
        print("No case matches LIVE_ONLY.")
        return 2
    repeat = _env_int("REPEAT", 1, "LIVE_REPEAT")
    concurrency = _env_int("HISTORY_EVAL_CONCURRENCY", 3)
    threshold = float(os.environ.get("HISTORY_EVAL_THRESHOLD", "0.7"))
    report = os.environ.get("HISTORY_EVAL_REPORT") or f"/tmp/history_eval_{time.strftime('%Y%m%d-%H%M%S')}.json"

    from app.config import load_settings

    settings = load_settings()
    if not settings.anthropic_api_key or not settings.database_url:
        print("Needs ANTHROPIC_API_KEY and DATABASE_URL in the env. See the top of this file.")
        return 2
    try:
        rt, media, closers = await build_live(settings)
    except Exception as exc:
        print(f"Cannot open the history index: {type(exc).__name__}: {exc}")
        return 2
    judge = None
    if os.environ.get("HISTORY_EVAL_JUDGE", "1") != "0":
        judge = Judge(settings.anthropic_api_key, os.environ.get("HISTORY_EVAL_JUDGE_MODEL") or JUDGE_MODEL)
    try:
        cov = await rt.store.coverage()
    except Exception as exc:
        cov = {"error": str(exc)[:200]}
    print(f"model={rt.settings.history_model} effort={rt.settings.history_effort} cases={len(cases)} "
          f"repeat={repeat} concurrency={concurrency} judge={judge.model if judge else 'off'}")
    print(f"index coverage: {cov}")
    t0 = time.monotonic()
    try:
        runs = await run_eval(cases, live_agent(rt, media), judge, concurrency=concurrency,
                              repeat=repeat, timeout=rt.settings.history_max_seconds + 30,
                              on_done=lambda r: print(format_run(r), flush=True))
    finally:
        for c in reversed(closers):
            try:
                await c.aclose()
            except Exception:
                pass
    summary = summarise(runs)
    print(format_summary(summary))
    if media.blocked_sends:
        print(f"(blocked {len(media.blocked_sends)} send_to_owner call(s): files are graded, never sent)")
    meta = {"model": rt.settings.history_model, "effort": rt.settings.history_effort,
            "judge_model": judge.model if judge else None, "repeat": repeat,
            "concurrency": concurrency, "threshold": threshold, "only": sorted(only),
            "wall_s": round(time.monotonic() - t0, 1), "started": int(time.time()),
            "coverage": {k: v for k, v in cov.items() if isinstance(v, (int, float))}}
    write_report(report, runs, summary, meta)
    print(f"report: {report}")
    return 0 if summary["pass_rate"] >= threshold else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
