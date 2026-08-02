"""FastAPI entry — the webhook Evolution POSTs every message to.

Replies 200 immediately (so Evolution never resends), then runs the graph in the
background. Dedup by message id is an infra concern and lives here, not in the graph."""
from __future__ import annotations

import logging
from collections import OrderedDict

from fastapi import BackgroundTasks, FastAPI, Request, Response

from .deps import build_deps
from .graph import build_graph

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("mary.webhook")

deps = build_deps()
graph = build_graph(deps)

app = FastAPI(title="Mary brain", version="0.1.0")

# Message-id dedup (Evolution may deliver a message more than once). Bounded LRU.
_seen: "OrderedDict[str, None]" = OrderedDict()
_SEEN_MAX = 1000


def _already_seen(msg_id: str | None) -> bool:
    if not msg_id:
        return False
    if msg_id in _seen:
        return True
    _seen[msg_id] = None
    if len(_seen) > _SEEN_MAX:
        _seen.popitem(last=False)
    return False


@app.get("/")
async def health() -> dict:
    return {"ok": True, "service": "mary-brain", "tags": deps.settings.tags}


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks) -> Response:
    body = await request.json()
    data = body.get("data") or {}
    msg_id = (data.get("key") or {}).get("id")

    if _already_seen(msg_id):
        return Response(status_code=200)

    background.add_task(_run, body)
    return Response(status_code=200)


async def _run(body: dict) -> None:
    try:
        await graph.ainvoke({"raw": body})
    except Exception:  # a bad payload must never crash the worker
        log.exception("graph run failed")
