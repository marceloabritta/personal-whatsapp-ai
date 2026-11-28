# fixer: the improvement loop's side on the Mac

When the judge files a mistake against one of Lisa's sessions, a case is opened on the droplet.
Once a day a scheduled task in the Claude desktop app starts an ordinary Claude Code session,
which shows in the app's sidebar and can be opened and steered like any other. It does the two
jobs that need a coding agent:

1. **Investigate** each new case and hand in a plan: issue, diagnosis, fix. Lisa sends it to
   his chat with himself.
2. **Fix** each plan he answered with "fix it": a checkout of its own, a test that reproduces
   the mistake, a second read of the diff by a sub-agent, then `ship`, which puts it live.
   Lisa tells him how it went.
3. **Take back** a live fix he answered "undo" to: `undo` reverts it on main and deploys that.

`task-prompt.md` is that session's prompt. `run.py` is the tool it calls for everything around
the thinking: taking a case, laying out the evidence, and handing work in.

The other half lives in `Lisa flow/app/fixdesk/` (the queue, the messages, reading "fix it") and
in the dispatcher (forwarding his answer). Nothing there calls a model.

## What is enforced, whatever the session does

The session runs with the app's normal permissions, so its limits while it works are
instructions in its prompt. What reaches GitHub is not left to it: a change gets there only
through `run.py ship`, which refuses one that touches anything outside `Lisa flow/app/`,
`Lisa flow/tests/`, `dispatcher/app.py` and `dispatcher/tests/`; that carries what looks like a
real person's number, WhatsApp id or name from the chat; that changes no test; or that fails an
offline test or a live check. `ship` makes the commit itself, on `fix/case-<id>`, merges it into
main and deploys it (`remote/deploy.sh`, run on the droplet): one deploy at a time under a lock,
the running image kept as `evolution-<service>:pre-fix-case-<id>`, the new image tried before
the swap, the version lines written into the live override with a backup, and the old image
and override put back if the new container does not come up. When a deploy fails after the
merge, main is reverted too, so main always says what production runs, and the case is closed
as failed. `FIXER_DEPLOY=0` stops at the pushed branch.

Her prompt version moves when her prompt changed (`--prompt-changed`, or a `prompt.py` in the
diff) and the judge's when its rubric did; both go in a commit of their own on top of the
merge, so that taking the fix back later reverts only the code.

A plan is checked the same way before it reaches the queue: a known kind, and three paragraphs
of at most 300 characters each.

## Install

    fixer/install.sh        # copies the tool to ~/.lisa-fixer; run again after changing fixer/

Then the scheduled task, once: in the desktop app, a task that runs every day at 08:00 with
`task-prompt.md` as its prompt (Claude can create it: "create the fix-loop scheduled task from
fixer/task-prompt.md"). After changing `task-prompt.md`, update the task's prompt too: the task
keeps its own copy.

Scheduled tasks run while the app is open; one that came due while it was closed runs at the
next launch.

## The commands

    python3 ~/.lisa-fixer/app/run.py pull
    python3 ~/.lisa-fixer/app/run.py investigate <id>
    python3 ~/.lisa-fixer/app/run.py plan <id>
    python3 ~/.lisa-fixer/app/run.py fix <id>
    python3 ~/.lisa-fixer/app/run.py ship <id> --summary "..." --review "..." [--prompt-changed]
    python3 ~/.lisa-fixer/app/run.py undo <id>
    python3 ~/.lisa-fixer/app/run.py fail <id> --step "..." --detail "..."
    python3 ~/.lisa-fixer/app/run.py release <id> --error "..."

Each prints one JSON object and exits 0 only when it did what was asked. Log:
`~/.lisa-fixer/logs/`. Evidence is laid out in the case's checkout under `.fixer/`, which git is
told to ignore, with a copy in `~/.lisa-fixer/cases/<id>/`; it holds real chat text and stays on
the Mac. A case a session took and never handed in is put back by the next `pull` after six
hours.

## Settings (`~/.lisa-fixer/config.env`, all optional)

| Setting | Default | |
|---|---|---|
| `FIXER_MAX_NEW` / `FIXER_MAX_FIX` | 5 / 3 | investigations / fixes offered per run |
| `FIXER_LIVE_CHECKS` | 1 | 0 = offline tests only: no API spend, but not end to end |
| `FIXER_DEPLOY` | 1 | 0 = stop at a tested, pushed branch; nothing is merged or deployed |
| `FIXER_STALE_HOURS` | 6 | when a taken case is put back |

On the droplet, in Lisa's environment: `FIX_LOOP_ENABLED` (the off switch for everything),
`FIX_DAILY_CAP`, `FIX_QUIET_FROM` / `FIX_QUIET_UNTIL`, `FIX_RUN_AT`, `FIX_EXPIRE_DAYS`; and the
dispatcher's `FIX_REPLY_URL`.

## What bills where

The session runs in the Claude desktop app, on the Claude subscription. The live checks inside
`ship` run Lisa herself against the real model on the droplet, on the development API key, as
every live check does.

## Tests

    python3 fixer/tests/run_fixer.py

Runs every command against a stand-in for the droplet, in a throwaway git repository. No
network, no model.
