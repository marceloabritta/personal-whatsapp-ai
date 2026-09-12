"""The graph: parse -> gate -> (context -> route -> reason -> ... | transcribe | auto | stop).

Two deterministic fast lanes leave the gate with no model call at all: `transcribe` (the owner
replied to a voice note asking for it) and `auto_transcribe` (a voice note in a chat enrolled
for automatic transcription). The automatic one rejoins the normal path when a listening window
happens to be open on that chat.

A programmatic `route` node picks the skill (state["domain"]) in code; `reason` runs that one
skill's prompt + schema (+ native tools). If it emits actions the skill's confirm/execute/respond
path runs; otherwise it goes straight to act.

  reason  -> confirm (has actions)          | act (none)
  confirm -> execute (approved)             | reason (blocked, ask) | act
  execute -> respond
  respond -> reason (LLM readback, reason ②) | act (programmatic render / nothing to read)

Confirmation and reply-assembly are the skill's policies, not graph defaults. Compiled WITH a
checkpointer so each chat's thread state persists per thread_id. LangGraph OSS library."""
from __future__ import annotations

from functools import partial

from langgraph.graph import END, StateGraph

from .deps import Deps
from .skills import resolve_gates as default_resolve_gates
from .nodes.act import act_node
from .nodes.auto_transcribe import auto_transcribe_node, route_after_auto
from .nodes.confirm import confirm_node, route_after_confirm
from .nodes.context import context_node
from .nodes.execute import execute_node
from .nodes.gate import gate_node, route_after_gate
from .nodes.parse import parse_node
from .nodes.reason import reason_node, route_after_reason
from .nodes.resolve import resolve_pending_node, route_after_resolve
from .nodes.respond import respond_node, route_after_respond
from .nodes.route import route_node
from .nodes.transcribe import transcribe_node
from .state import MessageState


def build_graph(deps: Deps, checkpointer=None):
    g = StateGraph(MessageState)
    # The resolved-id gates are TOOL SAFETY, so they default to the registry's rather than to
    # "none" — a caller that forgets to wire them must not silently lose the gate.
    gates = deps.resolve_gates if deps.resolve_gates is not None else default_resolve_gates()

    g.add_node(
        "parse",
        partial(parse_node, trace=deps.trace, tags=deps.settings.tags,
                owner_name=deps.settings.owner_name, settings=deps.settings),
    )
    g.add_node(
        "gate",
        partial(gate_node, sessions=deps.sessions, roster=deps.roster,
                settings=deps.settings, trace=deps.trace),
    )
    g.add_node(
        "transcribe",
        partial(transcribe_node, evolution=deps.evolution, transcription=deps.transcription,
                echoes=deps.echoes, settings=deps.settings, trace=deps.trace),
    )
    g.add_node(
        "auto_transcribe",
        partial(auto_transcribe_node, evolution=deps.evolution,
                transcription=deps.transcription, echoes=deps.echoes,
                settings=deps.settings, caps=deps.caps, trace=deps.trace),
    )
    g.add_node(
        "context",
        partial(context_node, evolution=deps.evolution, echoes=deps.echoes,
                settings=deps.settings, trace=deps.trace, transcription=deps.transcription),
    )
    g.add_node(
        "resolve_pending",
        partial(resolve_pending_node, confirm_policies=deps.confirm_policies or {},
                trace=deps.trace),
    )
    g.add_node(
        "route",
        partial(route_node, settings=deps.settings, reasoner=deps.reasoner, trace=deps.trace),
    )
    g.add_node(
        "reason",
        partial(reason_node, reasoner=deps.reasoner, settings=deps.settings, trace=deps.trace),
    )
    g.add_node(
        "confirm",
        partial(confirm_node, confirm_policies=deps.confirm_policies or {},
                settings=deps.settings, reasoner=deps.reasoner, trace=deps.trace),
    )
    g.add_node(
        "execute",
        partial(execute_node, tools=deps.tools or {},
                resolve_gates=gates,
                settings=deps.settings, trace=deps.trace),
    )
    g.add_node(
        "respond",
        partial(respond_node, render_policies=deps.render_policies or {},
                settings=deps.settings, trace=deps.trace),
    )
    g.add_node(
        "act",
        partial(act_node, evolution=deps.evolution, sessions=deps.sessions,
                echoes=deps.echoes, settings=deps.settings, trace=deps.trace,
                reaper=deps.reaper),
    )

    g.set_entry_point("parse")
    g.add_edge("parse", "gate")
    g.add_conditional_edges(
        "gate", route_after_gate,
        {"run": "context", "transcribe": "transcribe",
         "auto": "auto_transcribe", "stop": END},
    )
    g.add_edge("transcribe", END)
    # The automatic transcript is one-shot — unless a listening window is also open on that
    # chat, in which case the turn carries on into the normal conversation as well.
    g.add_conditional_edges(
        "auto_transcribe", route_after_auto, {"run": "context", "stop": END},
    )
    g.add_edge("context", "resolve_pending")
    g.add_conditional_edges(
        "resolve_pending", route_after_resolve,
        # "hold" — a non-owner agreed to a pending write: say nothing, keep the proposal, and
        # let `act` refresh the listening window so the owner can still answer.
        {"execute": "execute", "reason": "reason", "route": "route", "hold": "act"},
    )
    g.add_edge("route", "reason")
    g.add_conditional_edges("reason", route_after_reason, {"confirm": "confirm", "act": "act"})
    g.add_conditional_edges(
        "confirm", route_after_confirm,
        {"execute": "execute", "reason": "reason", "act": "act"},
    )
    g.add_edge("execute", "respond")
    g.add_conditional_edges(
        "respond", route_after_respond,
        {"reason": "reason", "confirm": "confirm", "act": "act"},
    )
    g.add_edge("act", END)

    return g.compile(checkpointer=checkpointer)
