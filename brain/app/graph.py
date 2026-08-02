"""The Step-3 graph:

  parse → gate → (context → reason → { execute → (reason | act) | act } | stop)

`reason` decides message/loop_state/actions/workflow. If it emits actions, `execute`
runs them; a read (search) or a failure loops back to `reason` (read-back), a clean
write goes to `act`. `act` posts the message and applies loop_state. Compiled WITH a
checkpointer so each chat's thread + cursor + workflow persist per thread_id."""
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
from .state import MessageState


def build_graph(deps: Deps, checkpointer=None):
    g = StateGraph(MessageState)

    g.add_node("parse", partial(parse_node, trace=deps.trace, tags=deps.settings.tags,
                                owner_name=deps.settings.owner_name))
    g.add_node("gate", partial(gate_node, sessions=deps.sessions, trace=deps.trace))
    g.add_node("context", partial(context_node, evolution=deps.evolution,
                                  settings=deps.settings, trace=deps.trace))
    g.add_node("reason", partial(reason_node, reasoner=deps.reasoner, settings=deps.settings,
                                 tools_prompt=deps.tools_prompt, task_prompts=deps.task_prompts,
                                 trace=deps.trace))
    g.add_node("execute", partial(execute_node, tools=deps.tools, settings=deps.settings,
                                  trace=deps.trace))
    g.add_node("act", partial(act_node, evolution=deps.evolution, sessions=deps.sessions,
                              settings=deps.settings, trace=deps.trace))

    g.set_entry_point("parse")
    g.add_edge("parse", "gate")
    g.add_conditional_edges("gate", route_after_gate, {"run": "context", "stop": END})
    g.add_edge("context", "reason")
    g.add_conditional_edges("reason", route_after_reason, {"execute": "execute", "act": "act"})
    g.add_conditional_edges("execute", route_after_execute, {"reason": "reason", "act": "act"})
    g.add_edge("act", END)

    return g.compile(checkpointer=checkpointer)
