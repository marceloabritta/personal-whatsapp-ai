# manager-kanban — for an agent working in this repo

## If you are here to upgrade an existing install, read UPGRADING.md first

Do that **before running any command**, including `./run.sh`. It is short, and it is the
only document you need. The rest of this file is orientation.

## The line that everything depends on

| | |
|---|---|
| **System** = this folder | code, UI, default prompt templates, migrations. **Disposable** — an upgrade replaces it wholesale. |
| **Working folder** = `./run.sh where` | cards + their folders, `board.json`, `pipelines.json`, the human's columns, gates and **worker prompts**, `.env`. **Precious** — never overwritten. |

Default location: `~/.manager-kanban/<repo-name>/`. Override with `MANAGER_WORKSPACE`.

Everything in the working folder is a human's accumulated thinking and **is not
reproducible**. Nothing in this repo may write to it except through `./update.sh`,
`python -m manager migrate`, and `python -m manager adopt`. Those three back it up first.

Before 0.2, state lived in `data/` and `workers/` *inside this folder* — where an upgrade
would destroy it. If you see either of those directories here, you are looking at an old
install: **stop and read UPGRADING.md.**

## Commands

```bash
./run.sh                  # start the board (migrates the working folder first, if needed)
./run.sh status           # system version, schema version, pending migrations. Changes nothing.
./run.sh where            # print the working folder path
./run.sh adopt <old>      # take over an OLD install's state (copies; never moves)
./update.sh               # pull → back up → migrate → three-way-merge the prompts
./update.sh --check       # what would an update do? Changes nothing.
python tests/run_all.py   # every test. No API key, no network.
```

## Hard rules

1. **Never delete anything holding `board.json`, `cards/` or `workers/`** — including
   `data/`, `data.migrated-*` and `.backups/`. Deletion is the human's call, never yours.
2. **Never edit `<workspace>/workers/*.md`.** Those are the human's prompts. Even the
   updater will not overwrite them.
3. **Never `git clean` / `git reset --hard` / `git checkout .`** in a folder that contains a
   `data/` directory. That is the one command that can destroy a board irrecoverably.
4. **Do not merge two boards.** If a tool refuses because a board already exists, it is
   working correctly. Ask which one is real.
5. **Never "fix" a card stuck at `busy: true` by clearing the flag on boot.** It is tempting
   and it is wrong. `busy=true` on a freshly-loaded board is the durable evidence that a run
   was cut off mid-flight; clearing it hides the spinner while the lost work stays lost, and
   destroys the fact the recovery path needs. It is read and acted on at startup instead —
   `manager/recovery.py` resumes the run and clears the flag only after. See
   `docs/INCIDENT-process-death-and-resume.md`.

## Crash recovery — how a killed run comes back

The process can die mid-run (closing the terminal that started it is enough). So:

- `manager/journal.py` — every run is written to `<workspace>/inflight.json` **before it
  starts** and struck off when it ends. Anything left there at boot was interrupted. If you
  add a new kind of long-running work, **journal it**, or it will not be recoverable.
- `manager/recovery.py` — at startup, re-enters the card's persisted SDK session and tells the
  manager to trust the disk, not its own memory of what it had delegated. Capped at three
  attempts so a poisoned run cannot become a restart loop.
- `<workspace>/logs/manager.log` — the log file. The first incident had to be reconstructed
  from raw session transcripts because there wasn't one.

## Shipping a change to the system

If your change requires anything of an existing working folder — a new config key, a new
per-card file, a reshaped `board.json` — **that is a migration, not a README note.** Add
`manager/migrations/mNNNN_<name>.py` (idempotent, forward-only, returns notes) and bump
`VERSION`. See the docstring in `manager/migrations/__init__.py`; a new capability must
arrive **off**, with a safe default, and turn on when the human asks.

Prove it with `python tests/run_all.py`. `tests/update_test.py` is where the migration and
prompt-merge guarantees are asserted; `tests/restart_test.py` `kill -9`s the real server
mid-run and checks the board came back.

## Layout

```
manager/workspace.py     WHERE STATE LIVES — the system/working-folder line, and `adopt`
manager/migrations/      what an update tells a working folder to do
manager/prompts.py       the three-way merge that keeps the human's prompts theirs
manager/update.py        backup → migrate → merge
manager/board.py         cards, the folder tree, trash, broadcast
manager/manager.py       the Agent SDK service (one session per card, one per manager)
manager/workers.py       the worker .md files → AgentDefinitions
workers.default/         default prompts. TEMPLATES ONLY — never copied over the human's.
web/index.html           the whole UI, one file
```
