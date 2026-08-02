"""The Step-2 graph: parse -> gate -> (context -> reason -> act | stop).

Compiled WITH a checkpointer so each chat's thread state (messages + ingestion
cursor + initialized) persists per thread_id. Built with the LangGraph OSS library."""
from __future__ import annotations

from functools import partial

from langgraph.graph import END, StateGraph

from .deps import Deps
from .nodes.act import act_node
from .nodes.context import context_node
from .nodes.gate import gate_node, route_after_gate
from .nodes.parse import parse_node
from .nodes.reason import reason_node
from .state import MessageState


def build_graph(deps: Deps, checkpointer=None):
    g = StateGraph(MessageState)

    g.add_node(
        "parse",
        partial(parse_node, trace=deps.trace, tags=deps.settings.tags,
                owner_name=deps.settings.owner_name),
    )
    g.add_node("gate", partial(gate_node, sessions=deps.sessions, trace=deps.trace))
    g.add_node(
        "context",
        partial(context_node, evolution=deps.evolution, settings=deps.settings,
                trace=deps.trace),
    )
    g.add_node(
        "reason",
        partial(reason_node, reasoner=deps.reasoner, settings=deps.settings,
                trace=deps.trace),
    )
    g.add_node(
        "act",
        partial(act_node, evolution=deps.evolution, sessions=deps.sessions,
                settings=deps.settings, trace=deps.trace),
    )

    g.set_entry_point("parse")
    g.add_edge("parse", "gate")
    g.add_conditional_edges("gate", route_after_gate, {"run": "context", "stop": END})
    g.add_edge("context", "reason")
    g.add_edge("reason", "act")
    g.add_edge("act", END)

    return g.compile(checkpointer=checkpointer)
