# manager-kanban

A **manager/worker** kanban for product development. You hand a manager an idea; it
supervises that idea across two pipelines — **plan** and **build** — delegating each
column's work to that column's worker, and stopping at the gates where you decide.

The board runs at `http://127.0.0.1:4173`, visible only to your machine.

> Requires **Python 3.10+** — the Claude Agent SDK does. `./run.sh` finds a suitable
> interpreter itself and tells you plainly if there isn't one.

---

## The one idea everything else follows from: system vs. working folder

| | **System** — this repo | **Working folder** — yours |
|---|---|---|
| Holds | `manager/`, `web/`, `run.sh`, `update.sh`, the default worker templates, **the migrations** | cards + their folders, board state, your columns and gates, **your worker prompts**, `.env` |
| Owned by | upstream | this project |
| On update | replaced wholesale | **migrated, never clobbered** |

The system is installable and upgradable on its own. The working folder holds everything
that belongs to *your* project and survives every upgrade untouched. Nothing is derived
from where the system folder happens to sit on disk — that was the old design, and it is
what made the thing unupgradable.

```
~/.manager-kanban/your-repo/        ← the working folder (the default location)
├── .kanban-version                 which system + schema version it is at
├── .env                            this project's config
├── board.json                      cards, threads, managers
├── pipelines.json                  your columns and gates
├── cards/plan/scoping/a1b2-dark-mode/    every card's folder
├── workers/plan/scoping.md         YOUR prompts. An update never overwrites these.
├── .defaults/workers/              what they were scaffolded from (the merge baseline)
└── .backups/                       taken automatically before every migration
```

Find yours with `./run.sh where`. Move it anywhere with `MANAGER_WORKSPACE=/some/path`.

---

## Quick start

```bash
git clone <this repo> manager-kanban && cd manager-kanban
./run.sh                    # first run: builds the venv, creates your working folder
```

That's it. Live mode needs **either** an `ANTHROPIC_API_KEY` in your working folder's
`.env`, **or** a logged-in Claude Code CLI — the Agent SDK falls back to the CLI's OAuth
session, so no key does not mean no live mode. With neither, the board runs a scripted
**mock** manager so you can shape the pipelines before spending a token.

The server prints which mode it chose *and why*. Trust that line; nothing else guesses.

```
  manager-kanban 0.2.0  (schema v1)
  board:    http://127.0.0.1:4173
  repo:     /Users/you/your-app
  folder:   /Users/you/.manager-kanban/your-app
  manager:  LIVE (Agent SDK) — no API key, but the Claude Code CLI is installed (using its login)
```

---

## Updating

```bash
./update.sh              # pull, reinstall deps, migrate the working folder
./update.sh --check      # what would happen? changes nothing
```

**An update is not just new code — it is new code plus an ordered set of migrations that
adapt your working folder to it.** The system carries a `VERSION`; your folder records the
schema version it was last migrated to; the migrations close the gap. So you can upgrade
upstream (teach it to run on a VM, say), come back to any working folder, pull, and *that
folder gains the new capability* — with its cards, its columns and its prompts intact.

Every update, in this order:

1. **Backs up** the whole working folder to `<workspace>/.backups/` before touching anything.
2. **Migrates** it — every migration between its version and the system's, in order.
   Idempotent, forward-only, and it stops at the first failure *loudly*. A half-applied
   migration that leaves your cards broken is worse than refusing to update at all.
3. **Merges the prompts**, three-way, like git (below).

A new capability always arrives **off**, with a safe default. You turn it on when you want it.

### Your prompts survive — and still improve

Worker prompts are **state, not source**. They are where most of this system's quality
lives, so a folder that simply refused to be touched would never get better either. So:

|  | |
|---|---|
| you never edited it, upstream improved it | you get the improvement, silently |
| **you edited it**, upstream improved it | **yours is kept** — and the diff is written to `<workspace>/PROMPT_CHANGES.md` so you can merge deliberately |
| upstream changed nothing | nothing happens |

Nothing in the update path can overwrite a prompt you have touched. The worst it can do is
tell you something.

---

## Surviving death mid-run

The board used to lose work if the process died while the manager was working — most easily
by **closing the editor window whose terminal was running the server**, which tears down the
whole process group. A four-minute scoping run was lost that way, one second before its file
hit disk, and nothing could recover it: the run existed only in RAM, so on restart nothing
even knew it had happened. The card just span "working" forever.

Now:

- **Every run is written down before it starts** (`<workspace>/inflight.json`) and struck off
  when it ends. Anything still in that file at boot was killed mid-flight.
- **On startup the manager resumes it by itself.** It re-enters the card's SDK session — which
  is persisted, so it gets back everything it knew — and is told, in as many words: *the
  process died, your memory of this run is not evidence, list the card folder and pick up from
  what actually reached disk*. If a worker died before writing its artifact, it re-delegates it.
- **A run that keeps killing the process is not retried forever.** After three interruptions
  the board stops, says so, and hands it back to you — a restart loop would be worse than the
  bug.
- **There is a log file**, at `<workspace>/logs/manager.log`. It survives the terminal.

You do not have to do anything for any of this. Kill the server mid-run and the card finishes
by itself.

### Keeping it alive (optional, macOS)

The above recovers a killed server; it does not *restart* one. To have the OS do that — so the
board survives closing VS Code entirely, and comes back within seconds of a crash:

```bash
./scripts/install-launchagent.sh                  # always on: at login, and after any exit
./scripts/install-launchagent.sh --on-crash-only  # you start it; launchd only resurrects crashes
./scripts/install-launchagent.sh --uninstall      # undo, any time
```

> **The system folder cannot live in `~/Desktop`, `~/Documents` or `~/Downloads` for this.**
> macOS gives launchd agents no consent to read those folders, so the agent cannot even
> `exec` `run.sh` — it fails with *"Operation not permitted"* and then crash-loops every five
> seconds. The installer detects this and **refuses**, rather than leaving you with the loop.
> Move the system folder somewhere else (`~/manager-kanban` is fine) and re-run it; your
> working folder is unaffected and does not move, because the system is disposable and the
> state lives elsewhere.

Without launchd, the board is a child of the terminal that started it, and closing that
terminal kills it. Your work is still safe — the killed run is journalled and resumes the next
time you run `./run.sh` — but nothing brings the server back on its own.

---

## The two pipelines

**Plan** — Ideas → Scoping → Scope Review → Planning → Plan Review → **Plan Ready** ⏸
**Build** — Preflight → Tests → Coding → **Build Review** ⏸ → Shipped

⏸ is a **gate**: the manager stops there and waits for you. A card never crosses from
*plan* into *build* on its own — that hand-off is yours to authorize, and so is the ship.

**Those columns are just the default.** Add, rename, reorder, gate or delete any of them
from the board (`+ column`, or ⚙ on a column header). The pipelines themselves are fixed;
everything inside them is not.

---

## A column is a contract

Each column answers two questions:

1. **Entry criteria** — what must a card already *have* to be worked in this column?
2. **Exit criteria** — what must be *true* for the card to leave?

Each column has exactly one **worker**: it checks (1) on arrival, does the work, checks (2)
before it finishes, and reports both to the manager. A worker that finds its entry criteria
unmet **stops and reports BLOCKED** rather than quietly doing the previous column's job —
that's a success, not a failure.

```
ENTRY:  PASS | BLOCKED   (what was missing, and which column owes it)
WORK:   what it actually did
OUTPUT: every file written
EXIT:   MET | NOT MET    (which criterion failed, and what would fix it)
FLAGS:  anything the manager must decide
```

### Worker and manager are different jobs

| | **Worker** | **Manager** |
|---|---|---|
| Scope | one column | the whole card |
| Sees | the card folder + the codebase | the reports + the board |
| Does | the work | the deciding |
| Can move cards? | no | yes |
| Can delegate? | no | yes |

When a worker reports back, the manager **supervises** it — spot-checks the artifact against
the contract, then decides:

- `ENTRY: BLOCKED` → move the card **back** to the column that owes the material.
- `EXIT: NOT MET` → send it back to the same worker with the specific gaps (twice, then
  escalate to you).
- `FLAGS` raised → judgement calls come to you, not to the manager alone.
- `EXIT: MET` → post a note, advance to the next column, delegate again.

You watch all of it in the card's chat: the delegation, the worker's raw report, the
manager's decision.

---

## Every card has a folder, and the folder travels

```
<workspace>/cards/plan/scoping/a1b2c3-dark-mode-toggle/
<workspace>/cards/build/coding/a1b2c3-dark-mode-toggle/     ← same card, later
<workspace>/cards/trash/a1b2c3-dark-mode-toggle/            ← archived, not destroyed
```

The folder tree **mirrors the board**: move a card and its folder physically moves too;
rename a column and every folder under it is renamed.

This is also how the hand-off works. The folder accumulates as it goes — by the time a card
reaches *Coding* it holds `IDEA.md`, `SCOPE.md`, `SCOPE_REVIEW.md`, `PLAN.md`,
`PLAN_REVIEW.md`, `PREFLIGHT.md`, `TESTS.md` — so **the folder is the material each worker
inherits from the columns before it**. No prompt-stuffing; the worker just reads its folder.

**Trash** archives a card: it leaves the board, its folder moves to `cards/trash/`, nothing
is lost. Restore puts it back in the column it left.

---

## The workers are markdown files you own

```
<workspace>/workers/plan/scoping.md
<workspace>/workers/plan/market-research.md    ← created when you add a "Market Research" column
```

Each file *is* the worker: tools and model in the frontmatter, the contract in the body
(`## Entry criteria`, `## Work`, `## Exit criteria`, `## Output`). Nothing is hardcoded.

Three equivalent ways to change one:

- **In the UI** — 🧠 on any column header opens its worker file in an editor.
- **In your editor** — they're just files in your working folder.
- **By asking** — tell the manager *"the scoper should also list competitors"*, or *"write
  the worker for the Research column I just added"*, and it rewrites the file.

Edits take effect on the **next delegation** — worker definitions are rebuilt from disk on
every run. The system's untouched copies live in `workers.default/` and are only ever used
to scaffold a column that doesn't have a worker yet.

---

## Configuration

Your config is `<workspace>/.env` (`./run.sh where` finds it). Shell variables beat it.

| Var | Default | Meaning |
|---|---|---|
| `MANAGER_WORKSPACE` | `~/.manager-kanban/<repo>` | The working folder. `MANAGER_DATA_DIR` is the old name, still honoured |
| `MANAGER_REPO_DIR` | the folder this repo sits in | The repo the manager operates on |
| `MANAGER_WORKERS_DIR` | `<workspace>/workers` | Where your worker prompts live |
| `ANTHROPIC_API_KEY` | — | Live mode. Not needed if the Claude Code CLI is logged in |
| `MANAGER_MOCK` | auto | `1` forces mock, `0` forces live |
| `MANAGER_PORT` | `4173` | Board port (bound to `127.0.0.1` only) |
| `MANAGER_MODEL` | SDK default | e.g. `claude-opus-4-8`, `claude-sonnet-5` |
| `MANAGER_PERMISSION_MODE` | `bypassPermissions` | `default` / `acceptEdits` / `bypassPermissions` |
| `MANAGER_PYTHON` | newest ≥3.10 on PATH | Interpreter used to build the venv |

---

## Files

Everything here is SYSTEM — disposable, replaced wholesale on update. There is no state in
this folder, which is the entire point: you can delete it, re-clone it, or copy a newer one
over it, and your board is untouched.

```
manager-kanban/
├── VERSION                    what an update compares against
├── CLAUDE.md                  orientation + hard rules for an AI working in here
├── UPGRADING.md               how to upgrade an existing install without breaking it
├── run.sh                     start the board   (also: status | where | adopt <old install>)
├── update.sh                  pull → back up → migrate → three-way-merge the prompts
├── manager/
│   ├── models.py              Card / Column / ManagerAgent
│   ├── pipelines.py           the user-defined column list for each pipeline
│   ├── workers.py             the worker .md files — read, write, → AgentDefinitions
│   ├── board.py               state + the folder tree + trash + broadcast
│   ├── agents.py              the manager's playbook (card-level and board-level)
│   ├── manager.py             Agent SDK service: one session per card, one per manager
│   ├── server.py              FastAPI: REST + WebSocket + static UI
│   ├── workspace.py           WHERE YOUR STATE LIVES — the system/working-folder line
│   ├── version.py             system version vs. schema version
│   ├── prompts.py             the three-way merge that keeps your prompts yours
│   ├── update.py              backup → migrate → merge
│   ├── migrations/            what an update tells a working folder to do
│   ├── journal.py             every run, written down BEFORE it starts
│   ├── recovery.py            resuming a run the process died in the middle of
│   └── logs.py                a log file that outlives the terminal
├── workers.default/           default prompts. Templates ONLY — never copied over yours.
├── scripts/                   install-launchagent.sh — keep the board alive (macOS, opt-in)
├── web/index.html             the board UI (single self-contained file)
├── tests/                     run_all.py — no API key needed by any of them
└── docs/                      the postmortems the design came out of. Worth reading before
                               you "simplify" something in here.
```

Two things that are generated per-install and must **never** be copied from one machine to
another: `.workspace` (a pointer to your working folder) and `.venv/`. Both are gitignored;
if you copy a folder by hand, delete them. See [UPGRADING.md](UPGRADING.md) §0.

---

## Tests

```bash
python tests/run_all.py
```

- `smoke.py` — the board, the folder tree, the mock pipeline, the trash, the column editor.
- `update_test.py` — migrations, backups, a deliberately failing migration, and the
  three-way prompt merge.
- `restart_test.py` — starts the real server, drives a real card, `kill -9`s it **mid-run**,
  restarts it, and asserts the cards, threads and folders all came back.
- `ui_test.py` — drives the real page in headless Chrome: opens the drawer, types, clicks
  Send, and checks the frame reached the server. (Skipped if Chrome isn't installed.)

---

## Notes and limitations

- **Autonomy vs. safety.** `bypassPermissions` lets workers run commands and edit files
  without prompting — necessary for a headless service, but it means the build pipeline can
  change your repo on its own between gates. **Run it on a branch**, especially before
  pushing a card through the *Plan Ready* gate.
- **A gate is only as good as its criteria.** The manager will not cross one without you,
  but it *will* believe a worker's `EXIT: MET` if the exit criteria are vague enough to be
  unfalsifiable. Write criteria that can fail.
- The manager acts when you message it. It is **not** a daemon; nothing advances on its own.
- **An in-flight turn does not survive a restart.** The card, its folder, its artifacts and
  its thread all do — but if you kill the server mid-run, that turn is lost, and the card
  says so in its thread. Send the message again.
