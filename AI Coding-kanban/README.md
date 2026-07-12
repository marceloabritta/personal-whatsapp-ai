# manager-kanban

A drop-in **manager/worker** kanban for product development. You hand the manager
a task; it plans and builds it by delegating to specialized worker subagents,
and it drives a card across a kanban board so you can watch — and steer — at a
high level. Click any card to open a live, two-way chat with the manager about
that specific task.

It's a self-contained folder. Drop it into any repository, run one command, and
a board appears at `http://127.0.0.1:4173`, visible only to your machine. The
manager operates on the repo the folder sits in.

---

## Quick start

```bash
# 1. put this folder inside the repo you want the manager to work on:
#    your-app/
#    └── manager-kanban/

# 2. from inside the folder:
cd manager-kanban
cp .env.example .env         # then paste your ANTHROPIC_API_KEY into .env
./run.sh                     # first run creates a venv and installs deps
```

Open `http://127.0.0.1:4173`. Without an API key it starts in **mock mode** — a
scripted pipeline that moves cards through every column so you can try the UI. Add
your key to `.env` to run the **live** Agent SDK manager.

---

## The board

Six columns, matching a two-stage planning→building process:

`Ideas → Planning → Plans Ready → Building → Build ready for review → Shipped`

- **New idea** creates a card in *Ideas*.
- Open a card and say **start** (or use the ▶ button). The manager runs the
  **planning** stage and moves the card to *Plans Ready*, then **stops at a human
  gate**.
- Reply **approve**. The manager runs the **building** stage and moves the card to
  *Build ready for review*, then **stops at the second human gate**.
- Reply **ship it**. The card moves to *Shipped*.

You can also drag cards between columns yourself.

---

## Architecture

**One manager conversation per card.** Each card is backed by its own persistent
Claude Agent SDK session (resumed by session id, so context survives restarts).
The manager operates at a high level: it decides *what* to do and delegates the
heavy, specific work to isolated **worker subagents**, then reports status.

**Workers** (fresh context each, minimal tools per role):

| Worker | Job |
|---|---|
| `scoper` | Explore the repo, define scope as a concrete user flow → `SCOPE.md` |
| `critic` | Independent adversarial review of scope and of plan (author ≠ reviewer) |
| `planner` | Draft the implementation plan (files, functions, sequence) → `PLAN.md` |
| `drift_checker` | Check the repo hasn't drifted from what the plan assumed |
| `preflight` | Verify build preconditions (deps, env, test runner) — GO / NO-GO |
| `test_writer` | Write the tests from the plan (before the code exists) |
| `coder` | Implement to make the tests pass |

**The pipeline the manager follows**

```
PLANNING   scope → critic (1 round) → plan → critic (1 round) → [HUMAN GATE: approve plan]
BUILDING   drift-check → preflight → write tests → write code → run tests → [HUMAN GATE: approve ship]
SHIP       move to Shipped
```

The manager cannot cross a human gate without your explicit approval message.

**Artifacts** for each card are written under
`manager-kanban/data/cards/<card-id>/` (`SCOPE.md`, `PLAN.md`) and linked from the
card drawer. Only the actual code changes touch your repo's real source.

**How the manager moves cards.** It has three in-process tools —
`move_card`, `set_stage`, `note` — exposed via an SDK MCP server. These are its
hands on the board; every call updates `data/board.json` and pushes a live update
to the browser over a WebSocket.

---

## Files

```
manager-kanban/
├── run.sh                 one-command start (venv + deps + server)
├── requirements.txt
├── .env.example           config (API key, port, model, permissions)
├── manager/
│   ├── models.py          Card / Column data model
│   ├── board.py           state store + JSON persistence + broadcast
│   ├── agents.py          worker definitions + the manager playbook
│   ├── manager.py         Agent SDK service: one session per card, streaming
│   └── server.py          FastAPI app: REST + WebSocket + static UI
├── web/index.html         the kanban UI (single self-contained file)
└── tests/smoke.py         headless end-to-end pipeline test (no key needed)
```

---

## Configuration (`.env`)

| Var | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for live mode; absent → mock mode |
| `MANAGER_PORT` | `4173` | Board port (bound to `127.0.0.1` only) |
| `MANAGER_REPO_DIR` | parent folder | Repo the manager operates on |
| `MANAGER_MODEL` | SDK default | e.g. `claude-sonnet-4-5`, `claude-opus-4-1` |
| `MANAGER_PERMISSION_MODE` | `bypassPermissions` | `default` / `acceptEdits` / `dontAsk` / `bypassPermissions` |
| `MANAGER_MOCK` | auto | `1` forces mock, `0` forces live |

---

## Notes and limitations

- **Autonomy vs. safety.** The default `bypassPermissions` lets the workers run
  commands and edit files without prompting — necessary for a headless service,
  but it means the manager can change your repo on its own between gates. Run it on
  a branch, and tighten `MANAGER_PERMISSION_MODE` if you want more control.
- **Shipping goes straight to prod** by design here; the "ship" step is where a
  missing staging/test environment would bite. Keep the human gate meaningful.
- The manager acts when you message it in a card; it is **not** a background daemon
  that works on its own between your messages.
- Run the smoke test any time: `python tests/smoke.py` (no API key required).
