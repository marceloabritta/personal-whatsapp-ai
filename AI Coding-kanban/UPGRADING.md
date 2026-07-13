# Upgrading an existing install

**Read this before running anything.** It is written for whoever is doing the upgrade —
human or AI agent — and it is the only document you need. Follow it in order.

---

## 0. If you are the human: what to do

Copy this system folder over your old install (or next to it), then open an agent in that
folder and paste this:

> Read UPGRADING.md and upgrade this install. Work out which situation applies before you
> run anything, verify the card count survived, and tell me what you found.

That is the whole procedure. The agent will land on §2 below, identify the situation, and
follow it. Everything it can do is either non-destructive or backed up first.

**One thing to check before you copy** — if the new folder contains a `.workspace` file,
**delete it**. It is a pointer to whichever working folder the *previous* machine used, and
carrying it across would aim the board at the wrong one. It is generated per-install and
should never travel:

```bash
rm -f .workspace          # in the NEW folder, before or after copying. Safe either way.
```

(If you forget, the system refuses to start rather than silently serving you an empty board
— but it is cleaner not to hit that at all.) A stale `.venv/` copied from another machine is
also worth deleting; `./run.sh` rebuilds it.

The one thing you must not do is destroy someone's board. Cards, their folders, the
columns, the gates, and the hand-tuned worker prompts are **months of a human's thinking**
and they are not reproducible. Every command below is designed so that the worst outcome is
"nothing happened", never "the state is gone".

---

## 1. The model, in five lines

| | |
|---|---|
| **System** | This folder. Code, UI, default prompt templates, migrations. **Disposable** — replaced wholesale on every upgrade. |
| **Working folder** | Cards, card folders, `board.json`, `pipelines.json`, your columns and gates, **your worker prompts**, `.env`. **Precious** — never overwritten. |

An upgrade replaces the system and then *migrates* the working folder to match it. The
working folder records the schema version it is at; the system carries a `VERSION` and an
ordered set of migrations. The migrations close the gap.

**Before 0.2 there was no working folder.** State lived in `data/` and `workers/` *inside*
the system folder — exactly where an upgrade would destroy it. Getting that state out is
what most of this document is about.

---

## 2. Work out which situation you are in

Run this first. It changes nothing:

```bash
./run.sh status
```

Then find yourself in this table. **The old install is any folder containing a `data/board.json`.**

| Situation | How to tell | Go to |
|---|---|---|
| **A.** New code landed *on top of* the old install — **including "the human copied this folder over the old one"** | This folder has a `data/` or `data.migrated-*` dir in it | [§3](#3-situation-a-in-place-upgrade) |
| **B.** New folder, old install is *somewhere else* | `data/board.json` exists in some *other* directory | [§4](#4-situation-b-the-old-install-is-elsewhere) |
| **C.** Already on 0.2+, just pulling a newer version | `./run.sh status` prints a schema version ≥ 1 | [§5](#5-situation-c-a-normal-update) |
| **D.** Fresh install, no old state anywhere | No `data/board.json` exists anywhere | Just run `./run.sh`. Nothing to upgrade. |

If you cannot tell, **stop and ask the human.** Do not guess. Finding it:

```bash
find ~ -name board.json -not -path '*/.backups/*' 2>/dev/null | head
```

---

## 3. Situation A: in-place upgrade

The new code is sitting on top of the old install — the human pulled, or simply copied this
folder over the old one — and `data/` is still there next to it.

**This is handled automatically.** On first run, the system finds the old `data/`, copies it
into a proper working folder, and moves the originals aside to `data.migrated-<timestamp>/`
— copied first, moved second, never deleted.

```bash
rm -f .workspace    # if present: a pointer copied in from another machine. See §0.
./run.sh status     # confirm what it sees
./run.sh            # the adoption happens here; read what it prints
```

Two things that are normal here and are not your problem:

- A leftover `.venv/` from the old install. If it is broken or built on the wrong Python,
  `./run.sh` says so and tells you to `rm -rf .venv` — do that; it rebuilds. It holds no state.
- A system-level `.env` in this folder. It still works, but the authoritative one now lives in
  the working folder. Leave it; the adoption copies nothing over it.

Then go to [§6, verify](#6-verify-the-state-survived-do-not-skip-this).

> The old `data/` is **moved, not deleted**. Leave `data.migrated-*` alone until the human
> has confirmed the board looks right. Deleting it is their call, not yours.

---

## 4. Situation B: the old install is elsewhere

You have been handed a new system folder, and the old one — with all the cards in it — is a
different directory. A `git pull` cannot help here: the new code never landed on the old
install, so nothing knows the old state exists. **This is the case that used to strand
people's boards.**

```bash
# 1. Look at the old install without touching it.
ls /path/to/old/manager-kanban/data/cards
cat /path/to/old/manager-kanban/data/board.json | head -20

# 2. Bring its state across. This COPIES — the old install is left completely untouched.
./run.sh adopt /path/to/old/manager-kanban
```

`adopt` takes the old board, its card folders and its worker prompts, puts them in this
project's working folder, and then migrates them forward to the current schema. It **refuses**
to run if the target working folder already has a board — it will not merge two boards, and
neither should you.

To adopt into a specific place instead of the default `~/.manager-kanban/<repo>/`:

```bash
MANAGER_WORKSPACE=/where/you/want/it ./run.sh adopt /path/to/old/manager-kanban
```

Then go to [§6, verify](#6-verify-the-state-survived-do-not-skip-this).

---

## 5. Situation C: a normal update

The working folder already exists and has a schema version. This is the steady state.

```bash
./update.sh --check     # what would happen? changes nothing
./update.sh             # pull → reinstall deps → back up → migrate → merge prompts
```

`./update.sh` will not proceed past a failing migration, and it copies the entire working
folder to `<workspace>/.backups/` before running a single one.

If the code is already where you want it and you only need the working folder brought
forward (no git, or the code was copied in by hand):

```bash
./update.sh --no-pull
```

### What happens to the prompts

Worker prompts are **state, not source** — they are yours, and an update never overwrites
them. But the system's *defaults* do improve, so the update three-way-merges them like git:

- a prompt you **never edited** → silently updated to the better default;
- a prompt you **did edit** → **kept exactly as it is**, and the upstream diff is written to
  `<workspace>/PROMPT_CHANGES.md` for the human to merge by hand;
- upstream changed nothing → nothing happens.

If `PROMPT_CHANGES.md` appears, **tell the human it exists.** Do not merge it for them
unless they ask — those prompts are where the system's quality lives.

---

## 6. Verify the state survived (do not skip this)

An upgrade that "ran without errors" is not an upgrade that worked. Check the actual state:

```bash
./run.sh status                       # schema version, and no pending migrations
WS=$(./run.sh where)

ls "$WS/cards"/*/*/                   # the card folders — are the cards all there?
ls "$WS/workers"/*/                   # the worker prompts
python -c "import json;b=json.load(open('$WS/board.json'));print(len(b['cards']),'cards')"
```

Then start it and look at the board:

```bash
./run.sh                              # → http://127.0.0.1:4173
```

**Compare the card count against what the human had before.** If a single card is missing,
stop and restore ([§7](#7-if-it-went-wrong)) rather than pressing on.

Finally, the test suite proves the machinery still works end to end (no API key needed):

```bash
python tests/run_all.py
```

---

## 7. If it went wrong

Nothing is destroyed by any of the above, so recovery is always possible:

| What you lost | Where it is |
|---|---|
| The working folder, after a migration | `<workspace>/.backups/<timestamp>-v<schema>/` — copied *before* the migration ran |
| The old in-place `data/` (situation A) | `data.migrated-<timestamp>/` in the system folder |
| The old install (situation B) | Untouched, exactly where it was — `adopt` only ever copies |

To roll back a migration, restore the backup over the working folder. Migrations are
forward-only by design; there is no down-migration, and pretending otherwise would be worse
than restoring a copy.

A failed migration **stops immediately** and prints the backup path. It does not carry on to
the next one, and it does not stamp a version it did not reach.

---

## 8. Rules, for an agent doing this unsupervised

1. **Never delete anything containing `board.json`, `cards/`, or `workers/`.** Not `data/`,
   not `data.migrated-*`, not `.backups/`. If something needs removing, that is the human's
   call.
2. **Never edit files in `<workspace>/workers/`.** Those are the human's prompts. The
   updater will not touch them and neither should you.
3. **Never `git clean`, `git checkout .` or `git reset --hard` in a folder that holds an old
   `data/`.** That is the one command that can destroy a board irrecoverably.
4. **Copy before you move; move before you delete.** In that order, always.
5. **Do not merge two boards.** If `adopt` refuses because a board is already there, that is
   the tool working correctly. Ask the human which one is real.
6. **Verify with §6 and report the card count.** "It ran without errors" is not a result.
7. If anything is ambiguous — two old installs, an unexpected schema version, a card count
   that dropped — **stop and ask.** The cost of asking is a message; the cost of guessing is
   someone's month of work.
