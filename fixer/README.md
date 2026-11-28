# fixer: the improvement loop's worker on the Mac

When the judge files a mistake against one of Lisa's sessions, a case is opened on the droplet.
Once a day this worker picks the cases up and does the two jobs that need a coding agent, in
Claude Code, on the Claude subscription:

1. **Investigate** each new case, read-only, and report a plan: issue, diagnosis, fix. Lisa
   sends it to his chat with himself.
2. **Fix** each plan he answered with "fix it": a worktree of its own, a test that reproduces
   the mistake, every offline test, a second read of the diff, the live checks on the droplet,
   and a pushed branch `fix/case-<id>`. Lisa tells him how it went.

The other half lives in `Lisa flow/app/fixdesk/` (the queue, the messages, reading "fix it") and
in the dispatcher (forwarding his answer). Nothing there calls a model.

## Install

    fixer/install.sh            # every day at 08:00
    fixer/install.sh 07:30      # another time; set FIX_RUN_AT on the droplet to match
    fixer/install.sh --remove

Needs, once: the `claude` command installed and signed in with the account that holds the plan
(`claude`, then `/login`). If the daily job says it is not signed in although the terminal is,
launchd cannot see the sign-in: run `claude setup-token` and put the token in
`~/.lisa-fixer/config.env` as `CLAUDE_CODE_OAUTH_TOKEN=...` (file mode 600).

Run it by hand: `python3 ~/.lisa-fixer/app/run.py` (`--dry` only asks the queue and prints).
Log: `~/.lisa-fixer/logs/`. Each case keeps its evidence and the full record of every Claude
Code run in `~/.lisa-fixer/cases/<id>/`; that folder holds real chat text and stays on the Mac.

After a change to `fixer/`, run `fixer/install.sh` again from an up-to-date tree: the daily job
runs the installed copy, not the repo.

## Settings (`~/.lisa-fixer/config.env`, all optional)

| Setting | Default | |
|---|---|---|
| `FIXER_MODEL` | account default | model for all three kinds of run |
| `FIXER_MAX_NEW` / `FIXER_MAX_FIX` | 5 / 3 | investigations / fixes per daily run |
| `FIXER_LIVE_CHECKS` | 1 | 0 = offline tests only: no API spend, but not end to end |
| `FIXER_DEPLOY` | 0 | fixes stop at a tested, pushed branch |
| `CLAUDE_CODE_OAUTH_TOKEN` | | see Install |

On the droplet, in Lisa's environment: `FIX_LOOP_ENABLED` (the off switch for everything),
`FIX_DAILY_CAP`, `FIX_QUIET_FROM` / `FIX_QUIET_UNTIL`, `FIX_RUN_AT`, `FIX_EXPIRE_DAYS`; and the
dispatcher's `FIX_REPLY_URL`.

## What bills where

The investigation, the fix and the second read run in Claude Code on the subscription: the
worker removes every `ANTHROPIC_*` variable from their environment, never uses `--bare`, refuses
to start unless `claude auth status` shows the account signed in, and stops a run that reports a
key as its credential. The live checks run Lisa herself against the real model on the droplet,
on the development API key, as every live check does.

## Tests

    python3 fixer/tests/run_fixer.py

Drives the whole worker against a stand-in for Claude Code and for the droplet, in a throwaway
git repository. No network, no model.
