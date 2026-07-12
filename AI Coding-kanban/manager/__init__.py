"""manager-kanban: a drop-in manager/worker kanban for product development.

Submodules:
- models   : Card / Column data model
- board    : state store + JSON persistence + change broadcast
- agents   : worker subagent definitions and the manager playbook
- manager  : the Agent SDK service that drives one session per card
- server   : FastAPI app (REST + WebSocket + static UI)
"""
__all__ = ["board", "manager", "server", "models", "agents"]
