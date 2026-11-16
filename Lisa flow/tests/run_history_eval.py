"""Offline verification for the history eval runner (tests/live_history.py).

The live runner needs the index, the model and a private gold set, so none of it can run here. What
can: its grading. This drives the runner's own functions with a tiny INVENTED gold set (made-up
people, groups and message ids), a fake agent and a fake judge. No network, no Postgres, no key.

    cd "Lisa flow" && .venv/bin/python tests/run_history_eval.py
Exits non-zero if any check fails.

Covers: reading and validating a gold file, accent-blind matching, every code check (mentions,
alternatives, the not-found phrases, forbidden claims, files, expired files, evidence), how code
checks and the judge's verdict combine, cost, what the judge is shown, the judge's JSON handling,
concurrency / repeat / timeouts / crashes, the summary, a report that carries no private text, the
question as the context node would label it, the no-send media guard, and the refusal to run without
a gold path."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import live_history as lh  # noqa: E402

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    _checks["pass" if cond else "fail"] += 1
    tail = f" — {detail}" if (detail and not cond) else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{tail}")


# An invented gold set. Nobody here exists.
JOANA = "5511999990000@s.whatsapp.net"
GROUP = "120363000000000000@g.us"
GOLD = {
    "version": 1,
    "cases": [
        {"id": "t01", "question": "quem me mandou o link da feira de robótica?", "lang": "pt",
         "category": "literal", "answer_known": True,
         "expected": {"answer": "Joana Ficticia, 3 mar.", "behaviour": "answer", "chats": [JOANA],
                      "chat_names": ["Joana Ficticia"], "msg_ids": ["TESTMSG0001"],
                      "date_range": ["2026-03-03", "2026-03-03"], "must_mention": ["Joana"],
                      "files": []},
         "evidence": "invented", "acceptable_alternatives": []},
        {"id": "t02", "question": "em qual grupo falaram da Pão Quente Labs?", "lang": "pt",
         "category": "group", "answer_known": True,
         "expected": {"answer": "Grupo Ficção Founders.", "chats": [GROUP], "msg_ids": ["TESTMSG0002"],
                      "must_mention": ["Ficcao|Ficção"], "files": []}},
        {"id": "t03", "question": "me manda o deck da Pão Quente", "lang": "pt", "category": "file",
         "answer_known": True,
         "expected": {"answer": "Deck_PaoQuente_v3.pdf", "msg_ids": ["TESTFILE0003"],
                      "must_mention": [], "files": ["TESTFILE0003", "TESTFILE0003B"]}},
        {"id": "t04", "question": "acha o PDF antigo do plano de voo", "lang": "pt", "category": "file",
         "answer_known": True,
         "expected": {"answer": "plano_voo.pdf, pode ter expirado", "msg_ids": ["TESTFILE0004"],
                      "must_mention": ["plano"], "files": ["TESTFILE0004"], "file_may_be_expired": True}},
        {"id": "t05", "question": "who sent me the Zorblax demo link?", "lang": "en", "category": "negative",
         "answer_known": True,
         "expected": {"answer": "Nobody; not in the history.", "behaviour": "not_found",
                      "forbid": ["Joana"]}},
        {"id": "t06", "question": "o que o Bento achou?", "lang": "pt", "category": "ambiguous",
         "answer_known": True,
         "expected": {"answer": "Dois Bentos: perguntar qual.", "behaviour": "clarify",
                      "must_mention": ["Bento"],
                      "any_of": [["?"], ["cafe|café", "barco"]]}},
        {"id": "t07", "question": "@lisa someone told me about a hackathon, who?", "lang": "en",
         "category": "open", "answer_known": False,
         "expected": {"answer": "Unknown; candidate Joana.", "chats": [JOANA], "must_mention": []}},
    ],
}


def tmp_gold(obj) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    return path


def case(cid: str) -> dict:
    return next(c for c in GOLD["cases"] if c["id"] == cid)


def hard_ok(checks: list[dict]) -> bool:
    return all(c["ok"] for c in checks if c["hard"])


def by_name(checks: list[dict], name: str) -> dict:
    return next((c for c in checks if c["name"] == name), {})


# ============================ 1. the gold file ===============================================

def p1_gold() -> None:
    print("1. reading the gold set")
    cases = lh.load_gold(tmp_gold(GOLD))
    check("a {cases: [...]} file loads every case", len(cases) == 7)
    check("a bare list loads too", len(lh.load_gold(tmp_gold(GOLD["cases"]))) == 7)
    check("LIVE_ONLY keeps just those ids", [c["id"] for c in lh.select(cases, {"t02", "t05"})] == ["t02", "t05"])
    check("no filter keeps them all", len(lh.select(cases, set())) == 7)
    bad = [
        ("a duplicate id", {"cases": [case("t01"), case("t01")]}),
        ("a case with no question", {"cases": [{**case("t01"), "question": " "}]}),
        ("an unknown behaviour", {"cases": [{**case("t01"), "expected": {"behaviour": "guess"}}]}),
        ("must_mention that is not a list", {"cases": [{**case("t01"), "expected": {"must_mention": "Joana"}}]}),
        ("an any_of group that is empty", {"cases": [{**case("t01"), "expected": {"any_of": [[]]}}]}),
        ("no cases at all", {"cases": []}),
    ]
    for label, obj in bad:
        try:
            lh.load_gold(tmp_gold(obj))
            check(f"refused: {label}", False, "loaded")
        except lh.GoldError:
            check(f"refused: {label}", True)


# ============================ 2. matching ====================================================

def p2_matching() -> None:
    print("2. accent- and case-blind matching")
    check("fold strips accents and case", lh.fold("Não ACHEI  nada") == "nao achei nada")
    check("an accented entry matches unaccented text", lh.mentions(lh.fold("grupo ficcao"), "Ficção"))
    check("'a|b' accepts either", lh.mentions(lh.fold("às 15:00"), "15h|15:00"))
    check("'a|b' with neither present fails", not lh.mentions(lh.fold("às 16h"), "15h|15:00"))
    for reply in ("Não encontrei nada sobre isso.", "I couldn't find any message about it.",
                  "Não há registro disso no histórico.", "My history only starts in June."):
        check(f"not-found phrase: {reply!r}", lh.mentions(lh.fold(reply), lh.NOT_FOUND))
    check("a confident answer is not a not-found phrase",
          not lh.mentions(lh.fold("Foi a Joana, em 3 de março."), lh.NOT_FOUND))


# ============================ 3. code checks =================================================

def p3_checks() -> None:
    print("3. code checks")
    ok = lh.deterministic_checks(case("t01"), {"message": "Foi a joana ficticia, no dia 3/3.",
                                               "notes": {"chat": JOANA}, "files": [],
                                               "error_category": "none"})
    check("[literal] the right name passes every hard check", hard_ok(ok))
    check("[literal] the chat key in notes counts as evidence", by_name(ok, "evidence").get("ok") is True)
    miss = lh.deterministic_checks(case("t01"), {"message": "Foi o Carlos.", "error_category": "none"})
    check("[literal] a wrong name fails mention[0]", by_name(miss, "mention[0]").get("ok") is False)
    check("[literal] no evidence is a soft failure only",
          by_name(miss, "evidence") == {**by_name(miss, "evidence"), "ok": False, "hard": False})
    via_tools = lh.deterministic_checks(case("t01"), {
        "message": "Joana.", "tool_calls": [{"tool": "read_chat", "args": {"chat": JOANA}, "n": 3}]})
    check("[literal] a chat read in the tool calls counts as evidence", by_name(via_tools, "evidence")["ok"])
    errored = lh.deterministic_checks(case("t01"), {"message": "Joana", "error_category": "provider"})
    check("[any] a provider error is not an answer", by_name(errored, "answered")["ok"] is False)
    empty = lh.deterministic_checks(case("t01"), None)
    check("[any] no result at all fails 'answered'", by_name(empty, "answered")["ok"] is False)

    grp = lh.deterministic_checks(case("t02"), {"message": "No grupo Ficção Founders."})
    check("[group] the accented group name matches its alternatives", hard_ok(grp))

    sent = lh.deterministic_checks(case("t03"), {"message": "Aqui está.", "files": ["TESTFILE0003B"]})
    check("[file] any acceptable copy of the file passes", hard_ok(sent))
    wrong = lh.deterministic_checks(case("t03"), {"message": "Aqui está.", "files": ["OTHER"]})
    check("[file] the wrong file is a hard failure", by_name(wrong, "file") == {**by_name(wrong, "file"), "ok": False, "hard": True})
    gone = lh.deterministic_checks(case("t04"), {"message": "Achei o plano_voo.pdf de setembro, mas o WhatsApp não serve mais o arquivo.", "files": []})
    check("[file] an expired file not sent is a soft failure only", hard_ok(gone) and not by_name(gone, "file")["ok"])

    nf = lh.deterministic_checks(case("t05"), {"message": "I couldn't find anything about Zorblax.", "files": []})
    check("[negative] saying it is not there passes", hard_ok(nf))
    claim = lh.deterministic_checks(case("t05"), {"message": "Joana sent it on 3 March.", "files": []})
    check("[negative] a confident claim fails any_of", by_name(claim, "any_of")["ok"] is False)
    check("[negative] a forbidden name fails forbid[0]", by_name(claim, "forbid[0]")["ok"] is False)
    files_nf = lh.deterministic_checks(case("t05"), {"message": "Not found.", "files": ["X"]})
    check("[negative] sending a file on a not-found fails", by_name(files_nf, "no_files")["ok"] is False)
    check("[negative] no evidence check where nothing is expected", not by_name(nf, "evidence"))

    ask = lh.deterministic_checks(case("t06"), {"message": "Qual Bento: o do café ou o do barco?"})
    check("[clarify] asking which one passes", hard_ok(ask))
    both = lh.deterministic_checks(case("t06"), {"message": "Bento (café) gostou; o Bento do barco achou caro."})
    check("[clarify] presenting both candidates passes", hard_ok(both))
    one = lh.deterministic_checks(case("t06"), {"message": "O Bento do café gostou."})
    check("[clarify] picking one silently fails", not hard_ok(one))
    default_nf = lh.deterministic_checks({"id": "x", "question": "q", "expected": {"behaviour": "not_found"}},
                                         {"message": "Foi a Joana."})
    check("[negative] with no any_of given, the not-found phrases are required",
          by_name(default_nf, "any_of")["ok"] is False)


# ============================ 4. verdicts ====================================================

def p4_combine() -> None:
    print("4. code checks + judge → verdict")
    good = [{"name": "answered", "ok": True, "hard": True}, {"name": "evidence", "ok": False, "hard": False}]
    bad = [{"name": "answered", "ok": True, "hard": True}, {"name": "mention[0]", "ok": False, "hard": True}]
    table = [
        ("judge pass + hard checks hold → PASS (a soft miss does not matter)", good, {"verdict": "pass"}, False, "PASS"),
        ("judge pass + a hard check fails → PARTIAL", bad, {"verdict": "pass"}, False, "PARTIAL"),
        ("judge partial → PARTIAL", good, {"verdict": "partial"}, False, "PARTIAL"),
        ("judge fail → FAIL even when the code checks hold", good, {"verdict": "fail"}, False, "FAIL"),
        ("no judge verdict + checks hold → PARTIAL", good, {"verdict": None}, False, "PARTIAL"),
        ("no judge at all + a hard check fails → FAIL", bad, None, False, "FAIL"),
        ("an agent crash or timeout → FAIL whatever the judge says", good, {"verdict": "pass"}, True, "FAIL"),
    ]
    for label, checks, judge, failed, want in table:
        got = lh.combine(checks, judge, agent_failed=failed)
        check(label, got == want, f"got {got}")


# ============================ 5. cost and the judge ==========================================

class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    input_tokens = 900
    output_tokens = 60


class _Resp:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]
        self.usage = _Usage()


class FakeClient:
    """Stands in for anthropic.AsyncAnthropic: records the call, returns a scripted reply."""

    def __init__(self, reply=None, raise_exc=None) -> None:
        self.calls: list = []
        self.reply = reply
        self.raise_exc = raise_exc
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        if self.raise_exc:
            raise self.raise_exc
        return _Resp(self.reply)


async def p5_cost_and_judge() -> None:
    print("5. cost and the judge")
    c = lh.cost({"input": 3_000_000, "output": 100_000, "cache_read": 2_000_000, "cache_write": 0},
                lh.AGENT_PRICES)
    check("Opus 5.5 prices: $4/M in, $20/M out, $0.20/M cache read (input counts the cache read)", abs(c - (4 + 2 + 0.4)) < 1e-9, f"{c}")
    j = lh.cost({"input": 1_000_000, "output": 1_000_000}, lh.JUDGE_PRICES)
    check("Sonnet 5.5 judge prices: $2/M in, $10/M out", abs(j - 12) < 1e-9, f"{j}")
    check("missing usage costs nothing", lh.cost(None, lh.AGENT_PRICES) == 0)

    msg = lh.judge_message(case("t01"), "Foi a Joana.", 0)
    check("the judge sees the question, the expected answer and the reply",
          case("t01")["question"] in msg and "Joana Ficticia, 3 mar." in msg and "Foi a Joana." in msg)
    check("the judge never sees chat keys, message ids or evidence notes",
          JOANA not in msg and "TESTMSG0001" not in msg and "invented" not in msg)

    fake = FakeClient(reply=json.dumps({"verdict": "partial", "why": "names her, wrong date"}))
    judge = lh.Judge("k", client=fake)
    out = await judge(case("t01"), "Foi a Joana em abril.", 0)
    check("a JSON verdict is read back", out["verdict"] == "partial" and out["why"] == "names her, wrong date")
    check("the judge defaults to claude-sonnet-5-5", fake.calls[0]["model"] == "claude-sonnet-5-5")
    fmt = fake.calls[0].get("output_config", {}).get("format", {})
    check("its output is enforced JSON with the verdict schema",
          fmt.get("type") == "json_schema" and fmt.get("schema") == lh.JUDGE_SCHEMA)
    check("its usage is kept for the cost", out["usage"] == {"input": 900, "output": 60})
    garbled = await lh.Judge("k", client=FakeClient(reply="I think it passes"))(case("t01"), "x", 0)
    check("unparseable JSON → no verdict, not a crash", garbled["verdict"] is None)
    odd = await lh.Judge("k", client=FakeClient(reply=json.dumps({"verdict": "great", "why": ""})))(case("t01"), "x", 0)
    check("a verdict outside pass/partial/fail → no verdict", odd["verdict"] is None)
    down = await lh.Judge("k", client=FakeClient(raise_exc=RuntimeError("503")))(case("t01"), "x", 0)
    check("a failed call → no verdict, with the reason", down["verdict"] is None and "503" in down["why"])


# ============================ 6. running the set =============================================

REPLIES = {
    "t01": {"message": "Foi a Joana Ficticia (3/3).", "notes": {"chats": [JOANA]}},
    "t02": {"message": "No grupo Ficção Founders."},
    "t03": {"message": "Aqui está o deck.", "files": ["TESTFILE0003"]},
    "t04": {"message": "Achei o plano de voo, mas não consigo mais baixar.", "files": []},
    "t05": {"message": "Não encontrei nada sobre Zorblax."},
    "t06": "slow",
    "t07": {"message": "Probably Joana, but I am not sure."},
}


class FakeAgent:
    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0
        self.calls: list = []

    async def __call__(self, c: dict) -> dict:
        self.calls.append(c["id"])
        nth = self.calls.count(c["id"])          # decided now, before any other run interleaves
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            r = REPLIES[c["id"]]
            if r == "slow":
                await asyncio.sleep(5)
            if c["id"] == "t02" and nth == 2:
                raise RuntimeError("tool blew up")
            return {"state": "keep_listening", "lang": c.get("lang"), "files": [], "notes": {},
                    "usage": {"input": 6000, "output": 200, "cache_read": 5000, "cache_write": 0},
                    "tool_calls": [{"tool": "search", "args": {}, "n": 1, "ms": 5}] * 4,
                    "stop_reason": "end_turn", "error_category": "none", **r}
        finally:
            self.in_flight -= 1


async def fake_judge(c: dict, reply: str, n_files: int) -> dict:
    verdict = {"t01": "pass", "t02": "pass", "t03": "pass", "t04": "partial", "t05": "pass",
               "t07": "partial"}.get(c["id"], "fail")
    return {"verdict": verdict, "why": f"secret reasoning about {c['expected'].get('answer')}",
            "usage": {"input": 500, "output": 40}}


async def p6_run() -> None:
    print("6. running the set: concurrency, repeat, timeouts, crashes, summary, report")
    cases = lh.load_gold(tmp_gold(GOLD))
    agent = FakeAgent()
    printed: list = []
    runs = await lh.run_eval(cases, agent, fake_judge, concurrency=2, repeat=2, timeout=0.5,
                             on_done=lambda r: printed.append(lh.format_run(r)))
    check("every case runs REPEAT times", len(runs) == 14 and sorted(agent.calls).count("t01") == 2)
    check("no more than HISTORY_EVAL_CONCURRENCY in flight", agent.peak <= 2, f"peak {agent.peak}")
    check("each finished run is printed as it lands", len(printed) == 14)
    v = {(r["id"], r["rep"]): r for r in runs}
    check("a slow case times out → FAIL", v[("t06", 0)]["verdict"] == "FAIL" and v[("t06", 0)]["error"] == "timeout")
    crashed = [r for r in runs if r["id"] == "t02" and r["error"]]
    check("a crash is a FAIL with its reason", len(crashed) == 1 and crashed[0]["verdict"] == "FAIL"
          and "tool blew up" in crashed[0]["error"])
    check("a right answer → PASS", all(v[("t01", i)]["verdict"] == "PASS" for i in (0, 1)))
    check("an expired file found but not sent → PARTIAL (the judge's call)", v[("t04", 0)]["verdict"] == "PARTIAL")
    check("tool calls, tokens and cost are recorded",
          v[("t01", 0)]["tool_calls"] == 4 and v[("t01", 0)]["usage"]["cache_read"] == 5000
          and abs(v[("t01", 0)]["cost_usd"] - (0.004 + 0.004 + 0.001)) < 1e-6)
    check("the open case runs but is not graded", v[("t07", 0)]["graded"] is False)

    s = lh.summarise(runs)
    check("summary counts graded runs only (open case left out)", s["graded"] == 12 and s["runs"] == 14)
    # t01×2, t02×1, t03×2, t05×2 pass = 7; t04×2 partial; t02 crash + t06×2 fail = 3
    check("pass / partial / fail tallies", (s["pass"], s["partial"], s["fail"]) == (7, 2, 3), f"{s}")
    check("pass rate = PASS / graded", abs(s["pass_rate"] - 7 / 12) < 1e-3)
    check("score counts PARTIAL as half", abs(s["score"] - 8 / 12) < 1e-3)
    check("by-category breakdown", s["by_category"]["file"] == {"PASS": 2, "PARTIAL": 2})
    text = lh.format_summary(s)
    check("the printed summary has the category lines and the totals",
          "file" in text and "7 passed, 2 partial, 3 failed of 12" in text)
    check("a non-pass line shows the judge's reason and the reply on stdout",
          any("secret reasoning" in p and "reply:" in p for p in printed if "[PARTIAL]" in p))

    path = tempfile.mkstemp(suffix=".json")[1]
    lh.write_report(path, runs, s, {"model": "fake"})
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    body = json.loads(raw)
    check("the report holds every run and the summary", len(body["runs"]) == 14 and body["summary"]["pass"] == 7)
    leaks = [w for w in ("Joana", "Ficção", "Zorblax", "secret reasoning", "Aqui está", "feira",
                         JOANA, "TESTMSG0001", "Bento") if w in raw]
    check("the report carries no question, reply, judge reasoning or gold strings", not leaks, f"leaked {leaks}")
    check("the report keeps check names and outcomes",
          {"name", "ok", "hard"} == set(body["runs"][0]["checks"][0]))


# ============================ 7. live wiring that can run offline ============================

class _Inner:
    def __init__(self) -> None:
        self.sent: list = []

    async def transcribe(self, msg_id):
        return {"ok": True, "text": "olá", "error": None}

    async def fetch(self, msg_id, **kw):
        return {"ok": True, "base64": "AA=="}

    async def send_to_owner(self, msg_id, *, caption=""):
        self.sent.append(msg_id)
        return {"ok": True, "sent_id": "REAL"}


async def p7_wiring() -> None:
    print("7. the question turn, the no-send guard, the refusal")
    turn = lh.question_turn("quem me mandou o link?", tag="@lisa", owner="Owner", tz_name="America/Sao_Paulo",
                            ts=1_794_000_000)
    content = turn[0]["content"]
    check("one user turn", len(turn) == 1 and turn[0]["role"] == "user")
    check("labelled like the context node: day divider, time, owner, tagged question",
          content.startswith("──") and "Owner: @lisa quem me mandou o link?" in content)
    tagged = lh.question_turn("@lisa already tagged", tag="@lisa", owner="Owner", tz_name="UTC", ts=1_794_000_000)
    check("a question already tagged is not tagged twice", "@lisa @lisa" not in tagged[0]["content"])

    inner = _Inner()
    media = lh.NoSendMedia(inner)
    res = await media.send_to_owner("F1", caption="x")
    check("send_to_owner never reaches the real seam", inner.sent == [] and media.blocked_sends == ["F1"]
          and res["ok"] and res["sent_id"] is None)
    check("fetch and transcribe pass through", (await media.fetch("F1"))["ok"] and (await media.transcribe("V1"))["text"] == "olá")
    off = lh.NoSendMedia(inner, allow_transcribe=False)
    check("HISTORY_EVAL_TRANSCRIBE=0 blocks transcription", (await off.transcribe("V1"))["error"] == "disabled")

    saved = os.environ.pop("HISTORY_EVAL_PATH", None)
    try:
        code = await lh.main()
    finally:
        if saved is not None:
            os.environ["HISTORY_EVAL_PATH"] = saved
    check("no HISTORY_EVAL_PATH → refuses to run (exit 2)", code == 2)
    os.environ["HISTORY_EVAL_PATH"] = "/nonexistent/gold.json"
    try:
        code = await lh.main()
    finally:
        os.environ.pop("HISTORY_EVAL_PATH", None)
        if saved is not None:
            os.environ["HISTORY_EVAL_PATH"] = saved
    check("an unreadable gold path → refuses to run (exit 2)", code == 2)


async def main() -> int:
    p1_gold()
    p2_matching()
    p3_checks()
    p4_combine()
    await p5_cost_and_judge()
    await p6_run()
    await p7_wiring()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
