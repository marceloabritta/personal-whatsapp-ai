"""The history skill — "@lisa com quem eu falei sobre…", in his chat with himself.

Drives the REAL compiled graph with an in-memory checkpointer, a fake WhatsApp that stores what is
sent the way Evolution does, a stub reasoner (for routing and the other skills), a fake history
runtime (store + media) and a fake `run_history_agent` swapped in for the real one. No network, no
Postgres, no Anthropic key. The agent itself is tested by its own front.

    cd "Lisa flow" && python tests/run_history_skill.py
Exits non-zero on any failed check.

  H1  the registry copes with the new kind
  H2  HISTORY_ENABLED=false is the pre-feature build; on, only the self-chat classifier changes
  H3  the matcher and the name, both directions, PT/EN/ES
  H4  the router: where it may go, who may send it there, held follow-ups, fresh tags
  H5  through the graph: the reply, the notes, the window
  H6  the "searching…" note: none when fast, exactly one when slow, never read back
  H7  files: sent after the reply, capped, echoes recorded, only to the self-chat
  H8  when it cannot answer: the guard, the store down, the agent failing
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

import app.history.agent as agent_mod  # noqa: E402
import app.prompt as prompt_mod  # noqa: E402
from app.config import Settings  # noqa: E402
from app.deps import Deps  # noqa: E402
from app.echoes import InMemoryEchoes  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.history.runtime import HistoryRuntime, build_runtime  # noqa: E402
from app.identity import is_own_message  # noqa: E402
from app.nodes.act import act_node, window_ttl  # noqa: E402
from app.nodes.reason import reason_node  # noqa: E402
from app.sessions import InMemorySessions  # noqa: E402
from app.skills import (  # noqa: E402
    SKILLS, callable_skills, confirm_policies, context_block_for, handlers, has_actions,
    output_schema_for, reason_runtime_for, render_policies, resolve_gates, routable,
    server_tools_for, side_effect_verbs, skill_enabled, system_prompt_for,
)
from app.nodes.context import context_node  # noqa: E402
from app.skills.history import HISTORY, guess_lang, history_invoked, history_matcher  # noqa: E402
from app.skills.router import _HISTORY_RULE, _classify_system, route_domain  # noqa: E402
from app.threads import make_thread_id  # noqa: E402
from app.trace import build_trace  # noqa: E402

OWNER_JID = "5511999990000@s.whatsapp.net"
FRIEND_JID = "5511999990001@s.whatsapp.net"
GROUP_JID = "120363000000000000@g.us"
T0 = 1794357700
EXISTING = ["contacts", "calendar", "setup", "web"]

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


def settings(**kw) -> Settings:
    base = dict(evolution_apikey="x", MARY_TRIGGER_TAG="@lisa", owner_jid=OWNER_JID,
                history_enabled=True, history_ack_after_seconds=0.05, history_max_files=3,
                loop_ttl_seconds=60, await_window_seconds=300, anthropic_api_key="",
                database_url=None, redis_url=None)
    base.update(kw)
    return Settings(**base)


CONTACTS_ON = dict(contacts_enabled=True, contacts_crud_enabled=True,
                   google_contacts_refresh_token="t")


class frozen_clock:
    """The prompt's date line, held still, so two renders a moment apart are the same text."""

    def __init__(self) -> None:
        self.when = datetime(2026, 11, 10, 12, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))

    def __enter__(self):
        self._real = prompt_mod.now_in
        prompt_mod.now_in = lambda tz_name: (self.when, tz_name)

    def __exit__(self, *exc):
        prompt_mod.now_in = self._real


# ============================ fakes =======================================================

class StubReasoner:
    """Scripted reasoner for the non-history skills, and a classifier that says what it was shown."""

    def __init__(self, domain: str = "web") -> None:
        self.calls: list = []
        self.script: list = []
        self.domain = domain
        self.classified: list = []

    async def respond(self, *, system, messages, output_schema=None, server_tools=None,
                      model=None, effort=None, think=False):
        self.calls.append({"system": system, "messages": list(messages)})
        base = {"state": "keep_listening", "message": None, "lang": "pt", "actions": [],
                "workflow": None, "usage": {"input": 1, "output": 1},
                "provider_request_id": "req_stub", "stop_reason": "end_turn",
                "tool_calls": [], "error_category": "none"}
        if self.script:
            base.update(self.script.pop(0))
        return base

    async def classify(self, *, system, messages, schema, max_tokens=32, effort="low"):
        enum = schema["properties"]["domain"]["enum"]
        self.classified.append({"system": system, "enum": list(enum)})
        return {"domain": self.domain if self.domain in enum else enum[-1]}


class StoredEvolution:
    """A WhatsApp that keeps what was sent, like Evolution's database."""
    instance = "secretaria"

    def __init__(self) -> None:
        self.sent: list = []
        self.history: dict = {}

    def store(self, jid, mid, text, *, from_me, ts=None, media=None) -> dict:
        rows = self.history.setdefault(jid, [])
        row = {"id": mid, "from_me": from_me, "text": text, "push_name": "Marcelo",
               "ts": ts if ts is not None else T0 + len(rows), "is_audio": False,
               "media_type": None, "media_mimetype": None, "media_filename": None,
               "contact_cards": []}
        if media:
            row.update(media)
        rows.append(row)
        return row

    async def send_text(self, number, text, *, quoted=None):
        mid = f"lisa{len(self.sent)}"
        self.sent.append((number, text, mid))
        jid = number if "@" in number else f"{number}@s.whatsapp.net"
        self.store(jid, mid, text, from_me=True)
        return mid

    async def fetch_history(self, jid):
        return list(self.history.get(jid, []))

    async def get_media_base64(self, message_id, *, convert_to_mp4=False):
        return {"base64": "JVBERi0xLjQK", "mimetype": "application/pdf"}

    async def find_chats(self):
        return []

    async def fetch_groups(self):
        return []


class FakeMedia:
    """HistoryMedia: send_to_owner re-sends into his chat with himself, the only place it can."""

    def __init__(self, evo: StoredEvolution) -> None:
        self.evo = evo
        self.sent: list = []
        self.expired: set = set()
        self.no_id: set = set()

    async def send_to_owner(self, msg_id, *, caption=""):
        self.sent.append(msg_id)
        if msg_id in self.expired:
            return {"ok": False, "error": "expired"}
        sent_id = "" if msg_id in self.no_id else f"file-{msg_id}"
        # What Evolution then stores: his own message, a document with no text.
        self.evo.store(OWNER_JID, sent_id or f"noid-{msg_id}", "", from_me=True,
                       media={"media_type": "document", "media_mimetype": "application/pdf",
                              "media_filename": "doc.pdf"})
        return {"ok": True, "sent_id": sent_id, "mimetype": "application/pdf",
                "file_name": "doc.pdf"}

    async def fetch(self, msg_id, *, max_bytes=15_000_000):
        return {"ok": False, "error": "not_media"}

    async def transcribe(self, msg_id):
        return {"ok": False, "text": None, "error": "failed"}


class FakeStore:
    """The agent is faked, so nothing calls this; it is only handed through."""


class FakeAgent:
    """Stands in for app.history.agent.run_history_agent. Each script entry is a partial result,
    an Exception to raise, and may carry `_delay` (seconds) to be slow."""

    def __init__(self) -> None:
        self.calls: list = []
        self.script: list = []

    async def __call__(self, *, convo, store, media, settings, notes=None, session_lang=None,
                       now_ts=None, client=None, on_event=None):
        self.calls.append({"convo": list(convo), "store": store, "media": media,
                           "settings": settings, "notes": notes, "session_lang": session_lang,
                           "now_ts": now_ts})
        step = self.script.pop(0) if self.script else {}
        if on_event:
            on_event({"tool": "search", "args": {"terms": ["feira"]}, "n": 2, "ms": 5,
                      "node": "spoofed", "loop_id": "spoofed"})
        if isinstance(step, dict) and step.get("_delay"):
            await asyncio.sleep(step["_delay"])
        if isinstance(step, Exception):
            raise step
        out = {"state": "keep_listening", "message": "Foi no grupo da feira, com o Contato A.",
               "lang": "pt", "reasoning": "two chats matched", "files": [],
               "notes": {"turn": len(self.calls)}, "usage": {"input": 10, "output": 5},
               "tool_calls": [{"tool": "search", "args": {"terms": ["feira"]}, "n": 2, "ms": 5}],
               "stop_reason": "end_turn", "error_category": "none", "model": "claude-opus-5-5",
               "provider_request_id": "req_hist"}
        out.update({k: v for k, v in step.items() if not k.startswith("_")})
        return out


class Chat:
    """His WhatsApp: a self-chat, a friend's chat and a group, wired to the real graph."""

    def __init__(self, s: Settings | None = None, *, history: bool = True,
                 domain: str = "web") -> None:
        self.settings = s or settings()
        self.evo = StoredEvolution()
        self.stub = StubReasoner(domain)
        self.media = FakeMedia(self.evo)
        self.store = FakeStore()
        self.runtime = HistoryRuntime(store=self.store, media=self.media, settings=self.settings)
        self.deps = Deps(settings=self.settings, evolution=self.evo,
                         sessions=InMemorySessions(ttl=60), echoes=InMemoryEchoes(ttl=3600),
                         trace=build_trace(), reasoner=self.stub, redis=None,
                         tools={}, confirm_policies=confirm_policies(),
                         render_policies=render_policies(), resolve_gates=resolve_gates(),
                         history=self.runtime if history else None)
        self.graph = build_graph(self.deps, MemorySaver())
        self.n = 0

    def payload(self, jid, mid, text, *, me=True, message=None) -> dict:
        return {"data": {"key": {"remoteJid": jid, "fromMe": me, "id": mid},
                         "message": message or {"conversation": text},
                         "messageTimestamp": T0 + self.n, "pushName": "Marcelo"}}

    async def say(self, text: str, *, jid: str = OWNER_JID, me: bool = True,
                  mid: str | None = None) -> dict:
        self.n += 1
        mid = mid or f"m{self.n}"
        self.evo.store(jid, mid, text, from_me=me, ts=T0 + self.n)
        return await self.wake(self.payload(jid, mid, text, me=me), jid)

    async def wake(self, raw: dict, jid: str = OWNER_JID) -> dict:
        config = {"configurable": {"thread_id": make_thread_id("secretaria", jid)}}
        return await self.graph.ainvoke({"raw": raw}, config=config)

    def records(self) -> list:
        return [e for e in self.deps.trace.events if e.get("node") == "record"]

    def events(self, node: str) -> list:
        return [e for e in self.deps.trace.events if e.get("node") == node]


class patched_agent:
    """Swap the real run_history_agent for a fake for the length of a block."""

    def __init__(self, fake: FakeAgent) -> None:
        self.fake = fake

    def __enter__(self):
        self._real = agent_mod.run_history_agent
        agent_mod.run_history_agent = self.fake
        return self.fake

    def __exit__(self, *exc):
        agent_mod.run_history_agent = self._real


def thread_text(st: dict) -> str:
    out = []
    for m in st.get("messages") or []:
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        out.append(c if isinstance(c, str) else str(c))
    return "\n".join(out)


# ============================ H1 — the registry ===========================================

def h1_registry() -> None:
    print("H1 — the registry copes with the new kind")
    on = settings(**CONTACTS_ON)
    check("history is registered, first", list(SKILLS)[0] == "history" and SKILLS["history"] is HISTORY)
    check("it is the agent kind, self-chat only, owner only, holds its loop, behind its switch",
          HISTORY.kind == "agent" and HISTORY.only_self_chat and HISTORY.owner_only
          and HISTORY.holds_loop and HISTORY.enabled_by == "history_enabled")
    check("no handler is built for it", "history" not in handlers(on))
    check("no confirm, render or gate policy", confirm_policies()["history"] is None
          and render_policies()["history"] is None and "history" not in resolve_gates())
    check("no side effects", side_effect_verbs()["history"] == set())
    check("no other skill can call it, and it calls nobody",
          all("history" not in callable_skills(n, on) for n in SKILLS)
          and callable_skills("history", on) == [])
    sc = output_schema_for("history", settings=on)
    check("its schema is the lean base", set(sc["properties"]) == {"reasoning", "state", "message", "lang"}
          and not has_actions("history", settings=on))
    check("its prompt renders like any other", "WhatsApp history" in str(system_prompt_for("history", on)))
    check("no server tools, a runtime resolves", server_tools_for("history", on) is None
          and reason_runtime_for("history", on)["model"] == on.claude_model)
    check("no context block", context_block_for("history", {}, {"settings": on}) == (None, {}))
    check("it holds its loop, with setup", sorted(n for n, sk in SKILLS.items() if sk.holds_loop)
          == ["history", "setup"])


# ============================ H2 — off is the pre-feature build ===========================

def h2_gating() -> None:
    print("H2 — HISTORY_ENABLED=false is the pre-feature build")
    base = {k: v for k, v in SKILLS.items() if k != "history"}  # the registry before the feature
    grid = {"plain": {}, "contacts": CONTACTS_ON, "presets": {"presets_enabled": True},
            "media": {"media_enabled": True},
            "all": {**CONTACTS_ON, "presets_enabled": True, "media_enabled": True,
                    "busy_check_enabled": True}}
    same_schema = same_prompt = same_on_off = True
    detail = ""
    with frozen_clock():
        for gname, kw in grid.items():
            off, on = settings(history_enabled=False, **kw), settings(history_enabled=True, **kw)
            for name in EXISTING:
                for s in (off, on):
                    if output_schema_for(name, SKILLS, s) != output_schema_for(name, base, s):
                        same_schema, detail = False, f"{gname}/{name}"
                    for lang, called in ((None, None), ("pt", "@lisa x")):
                        a = system_prompt_for(name, s, session_lang=lang, skills=SKILLS, called_for=called)
                        b = system_prompt_for(name, s, session_lang=lang, skills=base, called_for=called)
                        if str(a) != str(b) or a.stable_ends != b.stable_ends:
                            same_prompt, detail = False, f"{gname}/{name}/{lang}"
                if (output_schema_for(name, settings=off) != output_schema_for(name, settings=on)
                        or str(system_prompt_for(name, off)) != str(system_prompt_for(name, on))):
                    same_on_off, detail = False, f"{gname}/{name} on vs off"
    check("every existing schema is the one without the feature, off and on", same_schema, detail)
    check("every existing prompt is the one without the feature, off and on", same_prompt, detail)
    check("switching history on changes no existing schema or prompt", same_on_off, detail)

    off, on = settings(history_enabled=False), settings()
    me_self = {"is_self_chat": True, "from_me": True}
    check("off: not routable anywhere", "history" not in routable(me_self, SKILLS, off)
          and "history" not in routable({"is_self_chat": False}, SKILLS, off))
    check("on: routable in the self-chat only", "history" in routable(me_self, SKILLS, on)
          and "history" not in routable({"is_self_chat": False, "from_me": True}, SKILLS, on))
    check("no settings: fails closed", "history" not in routable(me_self, SKILLS)
          and not skill_enabled(HISTORY))
    check("off: no runtime is built", build_runtime(settings(history_enabled=False,
                                                              database_url="postgresql://x/y"),
                                                     None, None) is None)

    o = on.owner_name
    without = ["contacts", "calendar", "setup", "web"]
    plain = _classify_system(o, without)
    told = _classify_system(o, ["history", *without])
    check("the classifier is told about history only when it is offered",
          '"history"' not in plain and '"history"' in told)
    check("the rule is the only difference", told.replace(_HISTORY_RULE.replace("{owner}", o), "") == plain)
    check("presets and the rule live together",
          _classify_system(o, ["history", *without], presets=True)
          .replace(_HISTORY_RULE.replace("{owner}", o), "") == _classify_system(o, without, presets=True))
    check("the rule says a bare search in his own chat is history",
          "busca Zentara" in told and "Looking something up on the internet" in told)


# ============================ H3 — matcher and name =======================================

YES = [
    # Portuguese
    "@lisa com quem eu falei sobre visitar uma feira em Lisboa? a gente tava falando da Zentara",
    "@lisa com quem eu conversei sobre o investimento na semana passada?",
    "@lisa quem me mandou o contrato da casa?",
    "@lisa quem foi que me falou daquele restaurante japonês?",
    "@lisa quem foi que comentou sobre a vaga de CTO?",
    "@lisa quem mandou o pdf do aluguel?",
    "@lisa quem era aquele cara que falou da Zentara?",
    "@lisa pra quem eu mandei o orçamento?",
    "@lisa acha aquele pdf que o fulano mandou",
    "@lisa acha a conversa onde eu falei do aluguel",
    "@lisa acha o contrato que o Pedro mandou",
    "@lisa acha o contato do encanador que o João me mandou",
    "@lisa encontra o áudio que a Ana mandou ontem",
    "@lisa em qual grupo falaram do churrasco?",
    "@lisa em que grupo alguém mandou o link da planilha?",
    "@lisa lembra quando a gente falou sobre a viagem pra Lisboa?",
    "@lisa lembra daquela conversa sobre o apartamento?",
    "@lisa busca a mensagem sobre a Zentara",
    "@lisa procura o arquivo que o contador mandou",
    "@lisa o que o João me disse sobre o carro?",
    "@lisa me manda aquele pdf do contrato que o Pedro enviou",
    "@lisa busca no histórico Zentara",
    "@lisa procura nas minhas conversas quem falou da Zentara",
    # English
    "@lisa I spoke to someone about visiting a trade fair in Lisbon, we were discussing Zentara, who was it?",
    "@lisa who sent me the lease pdf?",
    "@lisa who did I talk to about the CTO role?",
    "@lisa find the message where Ana mentioned the venue",
    "@lisa find the pdf João sent last week",
    "@lisa find that spreadsheet",
    "@lisa which group was the barbecue link posted in?",
    "@lisa search my chats for Zentara",
    "@lisa remember when we talked about Lisbon?",
    "@lisa what did Pedro send me about the car?",
    # Spanish
    "@lisa con quién hablé sobre la feria?",
    "@lisa quién me mandó el contrato?",
    "@lisa busca el mensaje donde Ana habló del lugar",
    "@lisa en qué grupo mandaron el link?",
    "@lisa encuentra el pdf que me envió Pedro",
]

NO = [
    # the calendar's
    "@lisa marca reunião com o João amanhã às 10h",
    "@lisa agenda um almoço com a Ana na sexta",
    "@lisa com quem é a reunião de amanhã?",
    "@lisa com quem eu marquei a reunião de amanhã?",
    "@lisa quem vai na reunião das 15h?",
    "@lisa lembra de me avisar da consulta amanhã",
    "@lisa me lembra de ligar pro contador amanhã",
    "@lisa lembra quando é a reunião com a Ana?",
    "@lisa cancela a reunião com o Pedro",
    "@lisa marca reunião com quem me mandou o pdf",
    "@lisa what's on my calendar tomorrow?",
    "@lisa schedule a call with Ana friday 3pm",
    "@lisa schedule a meeting with whoever sent me the deck",
    "@lisa who's in tomorrow's 10am meeting?",
    "@lisa remind me to call the accountant tomorrow",
    # the address book's
    "@lisa qual o email do Felipe?",
    "@lisa qual o telefone da Ana?",
    "@lisa salva esse número como Pedro encanador",
    "@lisa salva o número que o João me mandou",
    "@lisa atualiza o email do Felipe",
    "@lisa apaga esse contato",
    "@lisa acha o contato do encanador",
    "@lisa what's Ana's phone number?",
    # setup's
    "@lisa setup",
    "@lisa configurações: reuniões de 30 minutos",
    "@lisa quais conversas estão com transcrição automática?",
    "@lisa em qual grupo a transcrição está ligada?",
    "@lisa which groups have transcription on?",
    "@lisa transcreve esse áudio",
    # the web's, and plain talk
    "@lisa busca o preço do dólar hoje",
    "@lisa procura um restaurante japonês perto de casa",
    "@lisa pesquisa quem ganhou o jogo ontem",
    "@lisa search the latest news about Zentara",
    "@lisa find a good hotel in Lisbon",
    "@lisa find a video about cats",
    "@lisa quem foi o primeiro presidente do Brasil?",
    "@lisa busca um vídeo de receita de bolo",
    "@lisa busca um link de tutorial de python",
    "@lisa procura o vídeo do gol de ontem",
    "@lisa quem mandou fechar a rua?",
    "@lisa quem disse que vai chover amanhã?",
    "@lisa traduz essa mensagem pro inglês",
    "@lisa resume essa conversa",
    "@lisa what did the central bank say today?",
    "@lisa o que você acha que o mercado vai fazer?",
    "@lisa pra quem eu mando o relatório?",
    "@lisa manda uma mensagem pro João dizendo que eu chego atrasado",
    # a bare search says nothing either way: the classifier decides
    "@lisa busca Zentara",
    "@lisa search Zentara",
]


def h3_matcher() -> None:
    print("H3 — the matcher and the name")
    missed = [t for t in YES if history_matcher(t) != "yes"]
    stolen = [t for t in NO if history_matcher(t) != "no"]
    check(f"all {len(YES)} recall phrasings match (PT/EN/ES)", not missed, "; ".join(missed))
    check(f"none of {len(NO)} other requests match", not stolen, "; ".join(stolen))
    check("never 'maybe'", {history_matcher(t) for t in YES + NO} <= {"yes", "no"})
    check("empty and tag-only are no", history_matcher("") == "no" and history_matcher("@lisa") == "no")
    for text in ("@lisa histórico: Zentara", "@lisa, history Zentara", "@lisa historial de la feria",
                 "@lisa Historico"):
        check(f"by name: {text!r}", history_invoked(text))
    for text in ("@lisa busca Zentara", "@lisa o histórico do dólar", "@lisa setup",
                 "@lisa busca no histórico", ""):
        check(f"not by name: {text!r}", not history_invoked(text))

    # The language of her own few words, before the agent has said any.
    for text, want in (("@lisa quem me mandou o pdf do aluguel?", "pt"),
                       ("@lisa who sent me the lease pdf?", "en"),
                       ("@lisa quién me mandó el contrato?", "es"),
                       ("@lisa Zentara", None)):
        check(f"her language for {text!r} is {want}", guess_lang(text) == want, str(guess_lang(text)))


# ============================ H4 — the router =============================================

async def h4_router() -> None:
    print("H4 — the router")
    on = settings(**CONTACTS_ON)
    off = settings(history_enabled=False, **CONTACTS_ON)
    me = {"is_self_chat": True, "from_me": True, "loop_opened": True}

    async def route(state, s=on, domain="web"):
        r = StubReasoner(domain)
        d, how = await route_domain(state, s, reasoner=r)
        return d, how, r

    wrong = []
    for text in YES:
        d, how, _ = await route({**me, "text": text})
        if (d, how) != ("history", "matcher"):
            wrong.append(f"{text!r} -> {d}/{how}")
    check("every recall phrasing routes to history in the self-chat, no model call", not wrong,
          "; ".join(wrong))
    d, how, _ = await route({**me, "text": "@lisa histórico: Zentara na feira"})
    check("by name -> history", (d, how) == ("history", "invoked"), f"{d}/{how}")

    # The other skills keep what is theirs, in the self-chat with history on.
    for text, want in (("@lisa marca reunião com o João amanhã às 10h", "calendar"),
                       ("@lisa marca reunião com quem me mandou o pdf", "calendar"),
                       ("@lisa setup", "setup"),
                       ("@lisa configurações: reuniões de 30 minutos", "setup"),
                       ("@lisa acha o contato do encanador", "contacts"),
                       ("@lisa apaga esse contato", "contacts")):
        d, how, _ = await route({**me, "text": text}, domain="web")
        check(f"{want} keeps {text!r}", d == want, f"{d}/{how}")
    stolen = []
    for text in NO:
        d, _, _ = await route({**me, "text": text}, domain="web")
        if d == "history" and text not in ("@lisa busca Zentara", "@lisa search Zentara"):
            stolen.append(text)
    check("nothing else reaches history (classifier saying web)", not stolen, "; ".join(stolen))

    # Order: history's phrases beat the others' single words.
    for text, other in (("@lisa quem me mandou o contato do encanador?", "contacts"),
                        ("@lisa com quem eu falei sobre a reunião de sexta?", "calendar"),
                        ("@lisa quem me mandou as configurações do roteador?", "setup")):
        d, how, _ = await route({**me, "text": text})
        check(f"before {other}: {text!r}", (d, how) == ("history", "matcher"), f"{d}/{how}")

    # A bare search is the classifier's, and it is told about history only where history can go.
    d, how, r = await route({**me, "text": "@lisa busca Zentara"}, domain="history")
    seen = r.classified[-1] if r.classified else {}
    check("a bare search goes to the classifier, which is offered history",
          how == "classifier" and "history" in seen.get("enum", [])
          and "busca Zentara" in seen.get("system", ""), f"{d}/{how} {seen.get('enum')}")
    check("...and lands there when it says so", d == "history")
    d, how, r = await route({**me, "text": "@lisa busca o preço do dólar hoje"}, domain="web")
    check("a web search stays web", d == "web" and how == "classifier", f"{d}/{how}")

    # Where it may not go.
    text = "@lisa com quem eu falei sobre a feira?"
    for where, state in (("a friend's chat", {"is_self_chat": False, "from_me": True}),
                         ("a group", {"is_self_chat": False, "from_me": True}),
                         ("the self-chat, not his message", {"is_self_chat": True, "from_me": False})):
        d, how, r = await route({**state, "loop_opened": True, "text": text}, domain="history")
        check(f"never in {where}", d != "history"
              and all("history" not in c["enum"] and '"history"' not in c["system"]
                      for c in r.classified), f"{d}/{how}")
    d, how, r = await route({**me, "text": text}, s=off, domain="history")
    check("switched off: never, and the classifier is not told",
          d != "history" and all("history" not in c["enum"] and '"history"' not in c["system"]
                                 for c in r.classified), f"{d}/{how}")
    d, how, _ = await route({**me, "text": "@lisa histórico: Zentara"}, s=off)
    check("switched off: not even by name", d != "history", f"{d}/{how}")

    # Held follow-ups, and a fresh tag.
    held = {"is_self_chat": True, "from_me": True, "loop_opened": False, "loop_domain": "history"}
    for text in ("e quando foi isso?", "não, a outra Ana", "e a reunião que ele marcou?",
                 "manda o pdf", "setup", "qual o email dele?"):
        d, how, _ = await route({**held, "text": text})
        check(f"an untagged follow-up stays in the search: {text!r}", (d, how) == ("history", "loop"),
              f"{d}/{how}")
    d, how, _ = await route({**held, "loop_opened": True, "text": "@lisa marca reunião amanhã 10h"})
    check("a fresh tag re-decides", (d, how) == ("calendar", "matcher"), f"{d}/{how}")
    d, how, _ = await route({**held, "text": "e quando foi isso?"}, s=off)
    check("a loop left on history is not held once it is switched off", d != "history", f"{d}/{how}")
    d, how, _ = await route({**held, "is_self_chat": False, "text": "e quando foi isso?"})
    check("nor outside the self-chat", d != "history", f"{d}/{how}")


# ============================ H5 — through the graph ======================================

async def h5_graph() -> None:
    print("H5 — through the graph: the reply, the notes, the window")
    fake = FakeAgent()
    with patched_agent(fake):
        c = Chat(settings(history_ack_after_seconds=5.0))
        c.evo.store(OWNER_JID, "old1", "nota pra mim: ligar pro banco", from_me=True, ts=T0 - 100)
        fake.script = [{"state": "keep_listening", "notes": {"chats": ["c1"]}}]
        st = await c.say("@lisa com quem eu falei sobre a feira em Lisboa?")
        call = fake.calls[-1] if fake.calls else {}
        check("the tag runs the search agent once", len(fake.calls) == 1 and st.get("domain") == "history")
        check("...with the thread as neutral turns, his question last",
              call.get("convo") and call["convo"][-1]["role"] == "user"
              and "feira em Lisboa" in call["convo"][-1]["content"])
        check("...with the store, the media and the settings handed through",
              call.get("store") is c.store and call.get("media") is c.media
              and call.get("settings") is c.settings)
        check("...no notes yet, no language locked, a clock",
              call.get("notes") is None and call.get("session_lang") is None
              and isinstance(call.get("now_ts"), int))
        check("the reasoner was not asked to answer", c.stub.calls == [])
        sent = c.evo.sent[-1][1] if c.evo.sent else ""
        check("the answer is framed and sent", sent.startswith("*[Assistente IA do Marcelo]:*")
              and "Foi no grupo da feira" in sent, sent)
        check("one message only: a fast search sends no note", len(c.evo.sent) == 1)
        check("it is recorded as an echo", c.deps.echoes.is_ours(OWNER_JID, c.evo.sent[-1][2]))
        check("the language is locked from the agent", st.get("session_lang") == "pt")
        check("the notes are kept for the next turn", st.get("history_notes") == {"chats": ["c1"]})
        check("the window is the conversational one", c.deps.sessions.ttl_left(OWNER_JID) <= 60)
        reason_ev = [e for e in c.events("reason") if e.get("domain") == "history"]
        check("a reasoning record like any reason pass, without tool results",
              reason_ev and reason_ev[-1].get("tools") == [{"tool": "search", "args": {"terms": ["feira"]},
                                                           "n": 2, "ms": 5}]
              and reason_ev[-1].get("model") == "claude-opus-5-5"
              and reason_ev[-1].get("message") == "Foi no grupo da feira, com o Contato A.")
        hist_ev = c.events("history")
        check("each agent event is traced under the loop, its own keys cannot move it",
              hist_ev and hist_ev[0].get("tool") == "search"
              and hist_ev[0].get("loop_id") == st.get("loop_id") and hist_ev[0].get("domain") == "history")
        rec = c.records()[-1]
        check("the activation record carries the agent's usage and model",
              rec.get("model") == "claude-opus-5-5" and rec.get("input_tokens") == 10
              and rec.get("provider_request_id") == "req_hist" and rec.get("state") == "keep_listening")
        check("...and nothing about files on a turn without any", "files_sent" not in rec)

        # An untagged follow-up, even with calendar words, stays in the search with its notes.
        fake.script = [{"state": "awaiting_answer", "message": "Quer que eu mande o pdf?",
                        "notes": {"chats": ["c1"], "asked": "pdf"}}]
        st = await c.say("e a reunião que ele marcou depois?")
        call = fake.calls[-1]
        check("the follow-up is held in the search", len(fake.calls) == 2 and st.get("domain") == "history")
        check("...given the last turn's notes and the locked language",
              call["notes"] == {"chats": ["c1"]} and call["session_lang"] == "pt")
        check("...and its own answer, as the model wrote it, in the thread",
              [m["role"] for m in call["convo"]][-3:] == ["user", "assistant", "user"]
              and call["convo"][-2]["content"] == "Foi no grupo da feira, com o Contato A.")
        check("a question holds the five-minute window", 60 < c.deps.sessions.ttl_left(OWNER_JID) <= 300)
        check("window_ttl agrees", window_ttl({"llm_state": "awaiting_answer", "loop_domain": "history"},
                                              c.settings) == 300
              and window_ttl({"llm_state": "keep_listening", "loop_domain": "history"}, c.settings) == 60)

        # A fresh tag re-decides, and the search's notes are gone.
        c.stub.script = [{"message": "Marquei.", "lang": "pt"}]
        st = await c.say("@lisa marca reunião amanhã às 10h")
        check("a fresh tag to the calendar leaves the search", st.get("domain") == "calendar"
              and len(fake.calls) == 2)
        check("...and clears its notes", st.get("history_notes") is None)
        fake.script = [{}]
        st = await c.say("@lisa quem me mandou o pdf do aluguel?")
        check("a new search starts with no notes", fake.calls[-1]["notes"] is None
              and fake.calls[-1]["session_lang"] is None)

        # Close drops the window.
        fake.script = [{"state": "close", "message": "Achei. Até mais."}]
        await c.say("valeu")
        check("close drops the window", not c.deps.sessions.is_open(OWNER_JID))

        # Silence: the agent may let a line pass; nothing is sent, the window is kept.
        fake.script = [{"message": None}]
        await c.say("@lisa em qual grupo falaram do churrasco?")
        n_sent = len(c.evo.sent)
        fake.script = [{"message": None}]
        st = await c.say("comprar pão")
        check("a silent turn sends nothing and keeps the window",
              len(c.evo.sent) == n_sent and c.deps.sessions.is_open(OWNER_JID))

    # The other skills, in the same chat, with history on.
    fake = FakeAgent()
    with patched_agent(fake):
        c = Chat()
        c.stub.script = [{"message": "Menu de configuração.", "lang": "pt"}]
        st = await c.say("@lisa setup")
        check("setup is still setup in the self-chat", st.get("domain") == "setup" and not fake.calls)
        c.stub.script = [{"message": "Ok.", "lang": "pt"}]
        st = await c.say("@lisa marca reunião com o João amanhã às 10h")
        check("a booking is still the calendar", st.get("domain") == "calendar" and not fake.calls)

        # His friend's chat: the same words never reach the search.
        c.stub.script = [{"message": "Não sei.", "lang": "pt"}]
        st = await c.say("@lisa com quem eu falei sobre a feira?", jid=FRIEND_JID)
        check("in a friend's chat the search never runs", st.get("domain") != "history" and not fake.calls)
        c.stub.script = [{"message": "Não sei.", "lang": "pt"}]
        st = await c.say("@lisa com quem eu falei sobre a feira?", jid=GROUP_JID)
        check("nor in a group", st.get("domain") != "history" and not fake.calls)

    # Switched off, end to end: the pre-feature reply path.
    fake = FakeAgent()
    with patched_agent(fake):
        c = Chat(settings(history_enabled=False), history=False, domain="web")
        c.stub.script = [{"message": "Não tenho como saber.", "lang": "pt"}]
        st = await c.say("@lisa com quem eu falei sobre a feira em Lisboa?")
        check("off: the search never runs, the classifier picks among the old skills",
              not fake.calls and st.get("domain") == "web"
              and all("history" not in x["enum"] for x in c.stub.classified))
        check("off: the turn is the old one, answered by the reasoner, with the old record",
              len(c.stub.calls) == 1 and "files_sent" not in c.records()[-1]
              and "files_asked" not in c.records()[-1])


# ============================ H6 — the "searching…" note ==================================

async def h6_ack() -> None:
    print("H6 — the searching note")
    fake = FakeAgent()
    with patched_agent(fake):
        c = Chat(settings(history_ack_after_seconds=0.05))
        fake.script = [{}]
        await c.say("@lisa quem me mandou o pdf do aluguel?")
        check("a fast answer: no note", len(c.evo.sent) == 1 and "🔎" not in c.evo.sent[0][1])

        c = Chat(settings(history_ack_after_seconds=0.05))
        fake.script = [{"_delay": 0.3}]
        st = await c.say("@lisa quem me mandou o pdf do aluguel?")
        acks = [x for x in c.evo.sent if "🔎" in x[1]]
        check("a slow answer: exactly one note, then the answer",
              len(acks) == 1 and len(c.evo.sent) == 2 and c.evo.sent[0] is acks[0]
              and "Foi no grupo" in c.evo.sent[1][1])
        number, text, ack_id = acks[0] if acks else ("", "", "")
        check("the note is framed, in his language, to the chat itself",
              text == "*[Assistente IA do Marcelo]:*\n\n🔎 Procurando…" and number == "5511999990000", text)
        check("its id is recorded as an echo", c.deps.echoes.is_ours(OWNER_JID, ack_id))
        check("it is not in the thread", "Procurando" not in thread_text(st))
        check("the reasoning record says a note went out",
              any(e.get("acked") for e in c.events("reason") if e.get("domain") == "history"))
        calls = len(fake.calls)
        st = await c.wake(c.payload(OWNER_JID, ack_id, text))
        check("its webhook is stopped at the gate as hers", st.get("decision") == "stop"
              and len(fake.calls) == calls)

        c = Chat(settings(history_ack_after_seconds=0.05))
        fake.script = [{"_delay": 0.4, "lang": "en", "message": "It was in the fair group."}]
        await c.say("@lisa who sent me the lease pdf?")
        acks = [x for x in c.evo.sent if "🔎" in x[1]]
        check("a slower one still sends only one, in English",
              len(acks) == 1 and acks[0][1] == "*[Marcelo's AI Assistant]:*\n\n🔎 Searching…",
              str(acks))

        c = Chat(settings(history_ack_after_seconds=0.05))
        fake.script = [{"_delay": 0.3, "lang": "es", "message": "Fue en el grupo de la feria."}]
        await c.say("@lisa quién me mandó el contrato?")
        acks = [x for x in c.evo.sent if "🔎" in x[1]]
        check("in Spanish, accents and all", len(acks) == 1 and acks[0][1].endswith("🔎 Buscando…"),
              str(acks))


# ============================ H7 — files ==================================================

async def h7_files() -> None:
    print("H7 — files go back to his chat with himself")
    fake = FakeAgent()
    with patched_agent(fake):
        c = Chat(settings(media_enabled=True))
        c.media.expired = {"f2"}
        fake.script = [{"message": "Achei três; um expirou.", "files": ["f1", "f2", "f3", "f4", "f1"]}]
        st = await c.say("@lisa me manda aquele pdf do contrato que o Pedro enviou")
        check("capped at history_max_files, no duplicates, in order", c.media.sent == ["f1", "f2", "f3"],
              str(c.media.sent))
        check("the text went first", c.evo.sent and "Achei três" in c.evo.sent[0][1])
        check("each sent file's id is an echo", c.deps.echoes.is_ours(OWNER_JID, "file-f1")
              and c.deps.echoes.is_ours(OWNER_JID, "file-f3"))
        file_ev = [e for e in c.events("act") if e.get("history_file")]
        check("each send is logged, the expired one as an error",
              [(e["history_file"], e["ok"], e["error"]) for e in file_ev]
              == [("f1", True, None), ("f2", False, "expired"), ("f3", True, None)], str(file_ev))
        check("no file contents or names in the log", all("base64" not in e and "file_name" not in e
                                                          for e in file_ev))
        check("the cap is logged", any(e.get("history_files_dropped") == 1 and e.get("reason") == "cap"
                                       for e in c.events("act")))
        rec = c.records()[-1]
        check("the record counts asked and sent", rec.get("files_asked") == 4 and rec.get("files_sent") == 2)
        check("the files are this turn's only", st.get("history_files") == [])

        # The file comes back through the webhook as his own message, with no text at all.
        calls = len(fake.calls)
        doc = {"documentMessage": {"fileName": "doc.pdf", "mimetype": "application/pdf"}}
        st = await c.wake(c.payload(OWNER_JID, "file-f1", "", message=doc))
        check("a sent file's echo wakes nothing: no search, nothing said",
              len(fake.calls) == calls and st.get("nothing_new") is True and len(c.evo.sent) == 1)
        check("...and resends nothing", c.media.sent == ["f1", "f2", "f3"])

        # Without the echo, the same file WOULD have read as his and woken the search.
        c.media.no_id = {"f9"}
        fake.script = [{"message": "Aqui.", "files": ["f9"]}, {"message": None}]
        await c.say("@lisa acha aquele pdf da feira")
        calls = len(fake.calls)
        st = await c.wake(c.payload(OWNER_JID, "noid-f9", "", message=doc))
        check("(a file with no id to record is read as his: why the echo matters)",
              len(fake.calls) == calls + 1)

    # Only ever into the self-chat, even if a file list reached act anywhere else.
    s = settings()
    evo = StoredEvolution()
    media = FakeMedia(evo)
    trace = build_trace()
    echoes = InMemoryEchoes()
    base = {"trace_id": "t", "remote_jid": FRIEND_JID, "number": "5511999990001",
            "llm_state": "keep_listening", "reply_body": None, "history_files": ["f1"],
            "is_self_chat": False, "loop_id": "L"}
    await act_node(base, evolution=evo, sessions=InMemorySessions(), echoes=echoes, settings=s,
                   trace=trace, history=HistoryRuntime(store=None, media=media, settings=s))
    check("outside the self-chat no file is sent", media.sent == []
          and any(e.get("reason") == "not_self_chat" for e in trace.events))
    up = await act_node({**base, "remote_jid": OWNER_JID, "is_self_chat": True}, evolution=evo,
                        sessions=InMemorySessions(), echoes=echoes, settings=s, trace=trace,
                        history=None)
    check("with the store down none is sent either", media.sent == [] and up.get("history_files") == [])

    # A turn that never reaches `reason` must not send the last turn's files: context clears them.
    evo2 = StoredEvolution()
    evo2.store(OWNER_JID, "m1", "oi", from_me=True)
    up = await context_node({"trace_id": "t", "remote_jid": OWNER_JID, "msg_id": "m1",
                             "trigger": "window", "initialized": True, "history_files": ["f1"],
                             "history_notes": {"k": 1}, "loop_id": "L"},
                            evolution=evo2, echoes=InMemoryEchoes(), settings=s, trace=build_trace())
    check("every activation starts with no files to send", up.get("history_files") == [])
    check("...and a window continuation keeps the notes", "history_notes" not in up)
    up = await context_node({"trace_id": "t", "remote_jid": OWNER_JID, "msg_id": "m1",
                             "trigger": "tag", "tag": "@lisa", "initialized": True,
                             "history_notes": {"k": 1}, "loop_id": "L"},
                            evolution=evo2, echoes=InMemoryEchoes(), settings=s, trace=build_trace())
    check("a fresh tag clears the notes", "history_notes" in up and up["history_notes"] is None)


# ============================ H8 — when it cannot answer ==================================

async def h8_failures() -> None:
    print("H8 — the guard, the store down, the agent failing")
    s = settings()
    fake = FakeAgent()
    with patched_agent(fake):
        trace = build_trace()
        evo = StoredEvolution()
        rt = HistoryRuntime(store=FakeStore(), media=FakeMedia(evo), settings=s)
        base = {"trace_id": "t", "domain": "history", "text": "@lisa com quem eu falei sobre a feira?",
                "messages": [{"role": "user", "content": "x"}], "remote_jid": FRIEND_JID,
                "loop_id": "L"}
        up = await reason_node({**base, "is_self_chat": False, "from_me": True}, reasoner=StubReasoner(),
                               settings=s, trace=trace, history=rt, evolution=evo,
                               echoes=InMemoryEchoes())
        check("his message outside the self-chat: a short refusal, no search",
              not fake.calls and up["reply_body"] == "Só busco no seu histórico na sua conversa com você mesmo."
              and up["actions"] == [] and up["call"] is None)
        up = await reason_node({**base, "remote_jid": OWNER_JID, "is_self_chat": True, "from_me": False},
                               reasoner=StubReasoner(), settings=s, trace=trace, history=rt,
                               evolution=evo, echoes=InMemoryEchoes())
        check("anyone else's message: silence, no search", not fake.calls and up["reply_body"] is None)
        check("both refusals are logged", sum(1 for e in trace.events if e.get("refused")) == 2)

        r = StubReasoner()
        r.script = [{"message": "web answer"}]
        up = await reason_node({**base, "remote_jid": OWNER_JID, "is_self_chat": True, "from_me": True},
                               reasoner=r, settings=settings(history_enabled=False), trace=trace,
                               history=rt, evolution=evo, echoes=InMemoryEchoes())
        check("a loop still on history after it is switched off gets the pre-feature turn",
              not fake.calls and len(r.calls) == 1 and up["reply_body"] == "web answer")

        # Switched on, but the store did not open at boot.
        c = Chat(history=False)
        st = await c.say("@lisa quem me mandou o pdf do aluguel?")
        check("store down: a short message, no search",
              not fake.calls and "indisponível" in (c.evo.sent[-1][1] if c.evo.sent else ""))
        check("...and the window is dropped", not c.deps.sessions.is_open(OWNER_JID)
              and st.get("llm_state") == "close")

        # The agent raises: a short apology, the window and the notes kept.
        c = Chat()
        fake.script = [{"notes": {"chats": ["c1"]}}, RuntimeError("boom")]
        await c.say("@lisa com quem eu falei sobre a feira?")
        st = await c.say("e quando foi?")
        check("the agent failing: a short apology", "Não consegui terminar" in c.evo.sent[-1][1])
        check("...the window kept", c.deps.sessions.is_open(OWNER_JID) and st.get("llm_state") == "keep_listening")
        check("...the notes kept", st.get("history_notes") == {"chats": ["c1"]})
        check("...and the record says so", c.records()[-1].get("error_category") == "provider"
              and c.records()[-1].get("stop_reason") == "error")

        # The agent gives up without a word.
        fake.script = [{"message": None, "error_category": "budget", "stop_reason": "budget"}]
        await c.say("e o pdf?")
        check("an agent that gives up silently still gets him an answer",
              "Não consegui terminar" in c.evo.sent[-1][1]
              and c.records()[-1].get("error_category") == "budget")


async def main() -> int:
    h1_registry()
    print()
    h2_gating()
    print()
    h3_matcher()
    print()
    for part in (h4_router, h5_graph, h6_ack, h7_files, h8_failures):
        await part()
        print()
    print(f"{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
