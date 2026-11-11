"""confirm — the skill-owned confirmation gate + composer.

For each action the routed skill's confirm policy `needs`, this node checks whether it may run.
Reads and already-confirmed writes pass through to `execute`. An unconfirmed write is held: the
skill's policy **composes the confirmation prompt** (no model writes it), the node stores the
action as `state["pending_action"]`, sets that prompt as `reply_body`, and routes to `act` to send
it. Next turn `resolve_pending` runs the write on a clean "yes".

If the skill can't compose a prompt (returns None) the turn falls back to the model (a readback so
it asks). Skills with no confirm policy pass straight through. The resolved-id gate stays in
`execute` (tool safety, not user confirmation).

Before a card is composed the policy may look first (`precheck`): the calendar reads his calendar
for the time being booked. A write it holds back gets no card and is not kept as pending; the
model is given the policy's note and answers in its own words."""
from __future__ import annotations

import hashlib
import json
import logging
import re

from ..prompt import today_in
from ..state import HANDOFF_MARK, MessageState
from ..trace import Trace
from .act import AWAITING

log = logging.getLogger("mary.confirm")


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _verb_of(action: dict) -> str:
    return (action or {}).get("task", "").partition(".")[2]


def _loop_text(state: dict) -> str:
    """Everything said in THIS loop, as one string, for the grounding check.

    `turn_text` is only the messages ingested by THIS activation — on a window continuation that
    is just the latest line. Grounding against it alone broke the ordinary shape of a booking:
    "marca com a Ana, ana@acme.com" / "que horas?" / "15h" puts the address one activation back,
    so the remember was silently discarded and the address never learned. The checkpointed
    `messages` are what the model actually read, so they are the honest haystack."""
    parts = [state.get("turn_text") or "", state.get("text") or ""]
    for m in state.get("messages") or []:
        # Lisa's OWN replies are in here too (act appends every sent message). They are not
        # evidence: an address she invented into a confirmation card would otherwise ground the
        # very `remember` this gate exists to refuse — the model would be citing itself.
        if isinstance(m, dict):
            if m.get("role") == "assistant":
                continue
            content = m.get("content")
        else:
            if getattr(m, "type", "") == "ai":
                continue
            content = getattr(m, "content", "")
        if isinstance(content, str):
            # A skill's request to another skill is the model talking too, under a "user" role.
            if not content.startswith(HANDOFF_MARK):
                parts.append(content)
        elif isinstance(content, list):  # media turns carry a block list
            parts.extend(b.get("text", "") for b in content if isinstance(b, dict))
    return "\n".join(p for p in parts if p)


def _grounded(action: dict, turn_text: str) -> bool:
    """Is this side effect's address actually present in the conversation?

    The exact analogue of the resolved-id gate ("no invented ids reach Google"), applied HERE
    rather than in a resolve_gate, because a side-effect verb is stripped before the action loop
    and so never reaches the execute node where those gates run. Without it `remember` would be the
    first mutating verb in the codebase with neither a confirmation nor a gate, and a hallucinated
    pair would land in the owner's real address book unchallenged."""
    email = (action.get("email") or "").strip().lower()
    if not email or not _EMAIL_RE.fullmatch(email):
        return False
    return email in (turn_text or "").lower()


def _signature(action: dict, text: str) -> str:
    """Fingerprint of a confirmation actually sent — the task plus the exact words."""
    task = (action or {}).get("task", "")
    return hashlib.sha1(f"{task}|{text}".encode("utf-8")).hexdigest()[:16]


def action_fingerprint(action: dict) -> str:
    """Fingerprint of the write itself, bookkeeping keys aside. It ties a sent card to the exact
    proposal it asked about, so a card can never vouch for a different write held later."""
    body = {k: v for k, v in (action or {}).items() if not k.startswith("_")}
    return hashlib.sha1(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


async def confirm_node(
    state: MessageState, *, confirm_policies: dict, settings, reasoner, trace: Trace,
    side_effects: dict | None = None, tools: dict | None = None, directory=None,
    presets=None,
) -> dict:
    tid = state["trace_id"]
    domain = state.get("domain") or ""
    policy = confirm_policies.get(domain)
    actions = state.get("actions") or []
    hops = int(state.get("tool_hops") or 0)
    ctx = {"settings": settings, "reasoner": reasoner, "directory": directory}

    # --- side-effect verbs: stripped BEFORE the loop, gated here, never executed ------------
    # They must not gate, render, produce an observation or reach `respond`, so they leave the
    # action list entirely. Stripping first also keeps them out of the routing decision below.
    verbs = (side_effects or {}).get(domain) or set()
    stripped = [a for a in actions if _verb_of(a) in verbs]
    actions = [a for a in actions if _verb_of(a) not in verbs]
    haystack = _loop_text(state)
    dropped = [a for a in stripped if not _grounded(a, haystack)]
    stripped = [a for a in stripped if _grounded(a, haystack)]

    approved: list = []
    observations: list = []
    pending: dict | None = None
    ask_message: str | None = None
    held_back: dict | None = None   # what the policy's look before the card held a write back with
    looked: list = []               # every such look this turn, for the trace

    for action in actions:
        task = (action or {}).get("task", "")
        _, _, verb = task.partition(".")
        needs = getattr(policy, "needs", set()) if policy else set()
        if policy is None or verb not in needs:
            approved.append(action)  # reads / ungated verbs / no policy -> straight through
            continue
        decision = await policy.confirm(action=action, state=state, deps=ctx)
        if decision.get("ok"):
            approved.append(action)
            continue
        # A write awaiting go-ahead gets the owner's presets filled in FIRST, so the action that
        # is shown, held, fingerprinted and finally run is one object. Only here, never on an
        # approved action: that one already carries what its card showed, and re-filling it
        # under presets changed since would run something he did not approve.
        if hasattr(policy, "prepare"):
            action = policy.prepare(action, presets)
        # A write awaiting go-ahead: compose the confirmation in code and hold it as pending.
        # The node's RETURN value is invisible to code running inside the node, so the composer is
        # handed the stripped pairs directly rather than being expected to read what we return.
        # The owner's date goes in the same way, so the card can say "(amanhã)" next to the day,
        # and so do the presets, so a setup card can say what a setting is changing FROM.
        # A policy that already said why this cannot be proposed gets no card for it.
        carded = pending is None and not decision.get("message")
        # The policy looks before the card is shown, at the write exactly as it would run. For
        # the calendar that is whether he is free then. Only the write that would get the card
        # is looked at, and once one is held back nothing else is proposed in its place.
        if carded and held_back is None and hasattr(policy, "precheck"):
            found = await policy.precheck(action, state,
                                          {**ctx, "handler": (tools or {}).get(domain)})
            if found and found.get("trace"):
                looked.append({"task": task, **found["trace"]})
            if found and found.get("hold"):
                held_back = found
                observations.append({"role": "user",
                                     "content": f"[{task} not proposed] {found.get('note', '')}"})
                continue
        composed = (policy.compose(action, {**state, "side_effects": stripped,
                                            "owner_today": today_in(settings.calendar_timezone),
                                            "presets": presets.snapshot() if presets else None})
                    if carded and held_back is None else None)
        if composed:
            pending, ask_message = action, composed
        else:
            msg = decision.get("message") or (
                f"Not executed — {task} needs {settings.owner_name}'s go-ahead first. "
                f"Restate the plan and wait for confirmation."
            )
            observations.append({"role": "user", "content": f"[{task} result] {msg}"})

    # Routing: approved actions run; else a composed confirmation is sent (holding the pending);
    # else a bare block reads back so the model asks (bounded); else nothing to do.
    if approved:
        route = "execute"
    elif ask_message:
        route = "act"
    elif observations and hops < settings.max_tool_actions:
        route = "reason"
        hops += 1
    else:
        route = "act"

    # A call sent with actions is what happens AFTER them, so it is spent here either way. With a
    # read, the result comes back to the model, which simply asks again if it still wants to.
    update: dict = {"actions": approved, "tool_hops": hops, "confirm_route": route,
                    "side_effects": stripped, "call": None}
    if observations:
        update["messages"] = observations
    repeated = False
    close_overridden = False
    if ask_message and route == "act":
        # Backstop against the defect this whole path was built around: asking the SAME question
        # twice in one session. The composer is stateless, so a retry that reaches here again
        # produces byte-identical text and, to the person in the chat, an assistant that ignored
        # their answer. If that happens, stay silent and keep the proposal standing — the owner's
        # yes still resolves it — and log loudly, because arriving here means something upstream
        # did not report its failure.
        sig = _signature(pending, ask_message)
        # A card is a question, and this turn's window decision is the code's, not the model's: it
        # composed the card, so it knows he has to answer it. The model once returned "close" with
        # the very booking the card was asking about, `act` honoured it, and the owner's "sim" five
        # seconds later met a closed window and was dropped.
        close_overridden = state.get("llm_state") == "close"
        update["llm_state"] = AWAITING
        if sig == state.get("last_confirm_sig"):
            repeated = True
            route = "act"
            update["confirm_route"] = "act"
            update["pending_action"] = pending   # keep it live; just do not re-ask
            update["reply_body"] = None
            log.warning(
                '{"confirm":"suppressed_repeat","task":"%s","loop_id":"%s"}',
                (pending or {}).get("task"), state.get("loop_id"),
            )
        else:
            update["pending_action"] = pending
            update["reply_body"] = ask_message
            update["last_confirm_sig"] = sig
            update["card_out"] = action_fingerprint(pending)  # `act` records which message it was
    elif ask_message and approved:
        # The routing bug this hides: `if approved: route = "execute"` above wins, and the composed
        # confirmation was thrown away with nothing storing the pending — so a find/list batched
        # with a create silently lost the question. Keep the proposal alive so the read can run and
        # the question still gets asked on the next pass.
        #
        # `last_confirm_sig` is deliberately NOT written here. It fingerprints a confirmation
        # ACTUALLY SENT (see _signature), and this branch sends nothing — route is "execute".
        # Recording it would make the repeat-suppressor above swallow the real ask on the next
        # pass, and the owner would get silence in answer to "book a meeting".
        update["pending_action"] = pending
        update["pending_card"] = None  # this proposal has no card yet; an older one is not its

    if held_back is not None:
        # There is nothing for him to say yes to. A card from earlier in the loop asked about a
        # time this turn has moved on from ("muda pra 14h", and 14h is taken): left standing, his
        # next "sim" would be read against it, and the same card asked again later would be
        # swallowed as a repeat.
        update["pending_action"] = None
        update["pending_card"] = None
        update["pending_side_effects"] = []
        update["pending_call"] = None
        update["last_confirm_sig"] = None
        if route == "act":
            # The read-backs are spent, so the model cannot be asked. He still has to hear it.
            update["reply_body"] = held_back.get("say")
            update["llm_state"] = AWAITING

    # A side effect batched with a gated write RIDES WITH THE PROPOSAL and fires only on the
    # owner's yes — otherwise a proposal he corrects ("no, her old address") would already have
    # written the rejected address into his real address book, permanently and silently.
    #
    # The riders belong to THIS proposal, so they are written whenever a proposal is stored, even
    # when empty. Writing them only `if stripped` left the previous proposal's riders standing:
    # correct Lisa's address, say yes to the correction, and the address you REJECTED is what
    # reached Google — and since a contact that already has one is never appended to, it became
    # the address she used for that person forever.
    if update.get("pending_action") is not None:
        update["pending_side_effects"] = stripped
        update["pending_call"] = state.get("call")  # rides with it too, for the same reason
        # A new card is a new question: whatever was to follow the last write waits for no one now.
        update["next_call"] = None
        update["resume_caller"] = False
    elif stripped and directory is not None and held_back is None:
        # Not with a write that was held back: its riders would have waited for his yes, and no
        # yes was asked for. The model sends them again with the booking it proposes next.
        handler = (tools or {}).get(domain)
        if handler is not None:
            directory.spawn(_run_side_effects(handler, stripped, state))

    trace.code(
        tid, node="confirm", loop_id=state.get("loop_id"), domain=domain,
        approved=len(approved), blocked=len(observations), pending=bool(update.get("pending_action")),
        route=route, repeated=repeated, close_overridden=close_overridden or None,
        side_effects=[a.get("task") for a in stripped] or None,
        side_effects_dropped=[a.get("task") for a in dropped] or None,
        looked=looked or None, held_back=held_back is not None or None,
    )
    return update


async def _run_side_effects(handler, actions: list, state: dict) -> None:
    """Run stripped side effects through the domain handler, out of band.

    Awaits each result so a failure is visible rather than discarded — `run()` never raises, it
    returns ok=False, which a fire-and-forget dispatch would throw on the floor."""
    for a in actions:
        inputs = {k: v for k, v in (a or {}).items()
                  if k != "task" and not k.startswith("_")}
        inputs.setdefault("_phone", state.get("phone"))
        res = await handler.run(_verb_of(a), inputs)
        if not (res or {}).get("ok"):
            log.warning('{"side_effect":"failed","task":"%s","error":"%s"}',
                        (a or {}).get("task"), (res or {}).get("error"))


def route_after_confirm(state: MessageState) -> str:
    return state.get("confirm_route") or "act"
