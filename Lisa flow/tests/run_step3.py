"""Step-3 (P1) verification — the tool framework skeleton.

Asserts the registry fans out correctly and the enforced-JSON output schema is well-formed
and stays under Anthropic's 16 union/array cap. No network, no Postgres, no Anthropic key,
no Google. This is the offline guard that the schema can't regress into the silent-Lisa bug.

    cd "Lisa flow" && python tests/run_step3.py
Exits non-zero on the first failed check.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.deps import build_deps  # noqa: E402
from app.reasoning.anthropic import AnthropicReasoner  # noqa: E402
from app.tools.registry import (  # noqa: E402
    TOOLS,
    build_output_schema,
    build_task_prompts,
    build_tools_prompt,
    confirm_first,
    count_unions,
    local_handlers,
)

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    tail = f"  ({detail})" if detail else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{tail}")
    _checks["pass" if cond else "fail"] += 1


def _branches(schema: dict) -> list[dict]:
    return schema["properties"]["actions"]["items"]["anyOf"]


def _task_const(branch: dict) -> str:
    return branch["properties"]["task"]["const"]


def main() -> None:
    print("Step-3 P1 — tool framework skeleton")
    schema = build_output_schema()

    # --- output schema shape ---
    top = set(schema.get("required", []))
    check("top-level required = reasoning/state/message/lang/actions/workflow",
          top == {"reasoning", "state", "message", "lang", "actions", "workflow"},
          detail=str(sorted(top)))
    check("actions is an array whose items is a single anyOf",
          schema["properties"]["actions"]["type"] == "array"
          and "anyOf" in schema["properties"]["actions"]["items"])
    check("message and workflow are the nullable fields",
          schema["properties"]["message"]["anyOf"][-1] == {"type": "null"}
          and schema["properties"]["workflow"]["anyOf"][-1] == {"type": "null"})

    # --- the verb branches ---
    tasks = {_task_const(b) for b in _branches(schema)}
    check("actions anyOf exposes exactly the 5 calendar tasks",
          tasks == {f"calendar.{v}" for v in ("create", "list", "find", "update", "delete")},
          detail=str(sorted(tasks)))

    req = {_task_const(b): set(b["required"]) for b in _branches(schema)}
    check("create requires task+title+start", req["calendar.create"] == {"task", "title", "start"})
    check("list requires only task", req["calendar.list"] == {"task"})
    check("find requires only task", req["calendar.find"] == {"task"})
    check("update requires task+event_id", req["calendar.update"] == {"task", "event_id"})
    check("delete requires task+event_id", req["calendar.delete"] == {"task", "event_id"})

    # optionals must be present-but-not-required (plain-typed), never anyOf:[T,null]
    create = next(b for b in _branches(schema) if _task_const(b) == "calendar.create")
    end_field = create["properties"]["end"]
    check("optional 'end' is plain-typed, not null-unioned",
          end_field == {"type": "string"}, detail=str(end_field))
    check("every branch sets additionalProperties:false",
          all(b.get("additionalProperties") is False for b in _branches(schema)))

    # --- the union-cap guard (the outage backstop) ---
    n = count_unions(schema)
    check("schema union/array count <= 16", n <= 16, detail=f"count={n}")

    # --- prompt fan-out ---
    tp = build_tools_prompt(owner_name="Marcelo")
    check("tools_prompt lists calendar as run-via-actions with its verbs",
          "calendar (run via actions)" in tp and "create, list, find, update, delete" in tp)
    check("tools_prompt substitutes the owner name", "Marcelo" in tp, detail=repr(tp[:60]))

    task = build_task_prompts(owner_name="Marcelo")
    check("task_prompts renders the calendar guidance", "Calendar actions" in task)
    check("task_prompts substitutes the owner name (no stray {owner_name})",
          "Marcelo" in task and "{owner_name}" not in task)
    check("empty registry -> empty task_prompts (no dangling header)",
          build_task_prompts(tools={}, owner_name="X") == "")

    # --- runtime fan-out ---
    settings = Settings()
    handlers = local_handlers(tools=TOOLS, settings=settings)
    check("local_handlers builds a 'calendar' handler", "calendar" in handlers)
    cf = confirm_first()
    check("confirm_first = {create,update,delete} for calendar",
          cf.get("calendar") == {"create", "update", "delete"}, detail=str(cf))

    # --- reasoner injection ---
    r = AnthropicReasoner(settings, output_schema=schema, mcp_servers=[])
    check("reasoner stores the injected output_schema", r.output_schema is schema)
    r2 = AnthropicReasoner(settings)
    check("reasoner falls back to building the schema from the registry",
          set(r2.output_schema.get("required", [])) == top)

    # --- deps wiring ---
    deps = build_deps(settings)
    check("deps carries the calendar handler", "calendar" in (deps.tools or {}))
    check("deps carries tools_prompt + task_prompts",
          bool(deps.tools_prompt) and bool(deps.task_prompts))
    check("deps.reasoner got the built schema",
          count_unions(deps.reasoner.output_schema) <= 16)

    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    sys.exit(1 if _checks["fail"] else 0)


if __name__ == "__main__":
    main()
