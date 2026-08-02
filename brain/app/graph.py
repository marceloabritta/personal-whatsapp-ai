"""The Step-1 graph: parse -> gate -> (ack -> send | stop).

Deliberately tiny. The gate is a conditional edge that either ends the run silently
or lets the message through to the fixed acknowledgement. Built with the LangGraph
OSS library (MIT) — no langgraph-api server, no Platform."""
from __future__ import annotations

from functools import partial

from langgraph.graph import END, StateGraph

from .deps import Deps
from .nodes.ack import ack_node
from .nodes.gate import gate_node, route_after_gate
from .nodes.parse import parse_node
from .nodes.send import send_node
from .state import MessageState


def build_graph(deps: Deps):
    g = StateGraph(MessageState)

    g.add_node("parse", partial(parse_node, trace=deps.trace, tags=deps.settings.tags))
    g.add_node("gate", partial(gate_node, sessions=deps.sessions, trace=deps.trace))
    g.add_node("ack", partial(ack_node, settings=deps.settings, trace=deps.trace))
    g.add_node("send", partial(send_node, evolution=deps.evolution, trace=deps.trace))

    g.set_entry_point("parse")
    g.add_edge("parse", "gate")
    g.add_conditional_edges("gate", route_after_gate, {"ack": "ack", "stop": END})
    g.add_edge("ack", "send")
    g.add_edge("send", END)

    return g.compile()
