"""The graph: parse -> gate -> (context -> reason -> {execute -> (reason|act) | act} | stop).

`reason` decides; if it wants tools it emits `actions` and routes to `execute`, which runs
them and either loops back to `reason` (a read, a failure, or a blocked gate — so the model
reads the result and replies truthfully) or goes on to `act` (a clean write). Compiled WITH a
checkpointer so each chat's thread state persists per thread_id. LangGraph OSS library."""
from __future__ import annotations

from functools import partial

from langgraph.graph import END, StateGraph

from .deps import Deps
from .nodes.act import act_node
from .nodes.context import context_node
from .nodes.execute import execute_node, route_after_execute
from .nodes.gate import gate_node, route_after_gate
from .nodes.parse import parse_node
from .nodes.reason import reason_node, route_after_reason
from .nodes.transcribe import transcribe_node
from .state import MessageState


def build_graph(deps: Deps, checkpointer=None):
    g = StateGraph(MessageState)

    g.add_node(
        "parse",
        partial(parse_node, trace=deps.trace, tags=deps.settings.tags,
                owner_name=deps.settings.owner_name, settings=deps.settings),
    )
    g.add_node("gate", partial(gate_node, sessions=deps.sessions, trace=deps.trace))
    g.add_node(
        "transcribe",
        partial(transcribe_node, evolution=deps.evolution, transcription=deps.transcription,
                echoes=deps.echoes, settings=deps.settings, trace=deps.trace),
    )
    g.add_node(
        "context",
        partial(context_node, evolution=deps.evolution, echoes=deps.echoes,
                settings=deps.settings, trace=deps.trace, transcription=deps.transcription),
    )
    g.add_node(
        "reason",
        partial(reason_node, reasoner=deps.reasoner, settings=deps.settings,
                trace=deps.trace, tools_prompt=deps.tools_prompt,
                task_prompts=deps.task_prompts),
    )
    g.add_node(
        "execute",
        partial(execute_node, tools=deps.tools or {},
                confirm_first=deps.confirm_first or {}, settings=deps.settings,
                trace=deps.trace),
    )
    g.add_node(
        "act",
        partial(act_node, evolution=deps.evolution, sessions=deps.sessions,
                echoes=deps.echoes, settings=deps.settings, trace=deps.trace),
    )

    g.set_entry_point("parse")
    g.add_edge("parse", "gate")
    g.add_conditional_edges(
        "gate", route_after_gate,
        {"run": "context", "transcribe": "transcribe", "stop": END},
    )
    g.add_edge("transcribe", END)
    g.add_edge("context", "reason")
    g.add_conditional_edges("reason", route_after_reason, {"execute": "execute", "act": "act"})
    g.add_conditional_edges("execute", route_after_execute, {"reason": "reason", "act": "act"})
    g.add_edge("act", END)

    return g.compile(checkpointer=checkpointer)
