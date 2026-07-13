# manager-kanban — setup handoff & next-phase decisions

Written 2026-07-12, after a session that got the board running for the first time.
Two parts: **what was broken and what changed**, then **four decisions to implement**.

---

## Part 1 — Getting it running

### Current state

The board runs at `http://127.0.0.1:4173` in **live** mode with four idea cards seeded.
Live mode is authenticated through the **Claude Code CLI's existing login — there is no
API key anywhere.** That works because `MANAGER_MOCK=0` forces live even with
`ANTHROPIC_API_KEY` unset, and the Agent SDK then falls back to the CLI's OAuth session.

### Problems faced, and what fixed them

**1. Python 3.9 vs. the Agent SDK (blocked startup entirely).**
The default `python3` on this machine is 3.9.6. `claude-agent-sdk` requires 3.10+, so the
first `./run.sh` died at `pip install` with *"Could not find a version that satisfies the
requirement claude-agent-sdk"*. Fixed by deleting `.venv` and rebuilding it with Homebrew's
`python3.12`.

> **Still latent.** `run.sh` hardcodes `python3 -m venv`. If `.venv` is ever deleted it will
> rebuild with 3.9 and fail the same way. It should probe for a 3.10+ interpreter and fail
> with a clear message if none exists.

**2. No `.env`, so it defaulted to mock mode.**
Created `AI Coding-kanban/.env` (gitignored) containing `MANAGER_MOCK=0` and an empty
`ANTHROPIC_API_KEY`. This is the whole live-mode configuration.

**3. `run.sh` lies about the mode.**
It prints `⚠ No ANTHROPIC_API_KEY set — starting in MOCK mode` based only on the key's
presence, *before* `MANAGER_MOCK` is ever consulted. The warning fires even when the server
then correctly starts live. The authoritative signals are the `manager: LIVE (Agent SDK)`
line and `GET /api/config` → `"mock": false`. The check should respect `MANAGER_MOCK`.

**4. The real bug: both chats silently dropped every message.**
This is why the board looked dead — messages typed to the manager did nothing, no error
anywhere. In `web/index.html`, both drawer-opening functions set the "which chat is open"
variable and then immediately wiped it:

```js
function openManager(id){
  closeAll(true); openMgr=id; seenTs=0;
  show('d-mgr');        // show() calls closeAll(), and closeAll() ends with openMgr=null
  ...
  send({type:'manager_open', manager_id:id});   // works — uses the local `id`, not openMgr
}
```

`show()` → `closeAll()` → `openCard=null; openMgr=null`. So by the time the user typed,
`openMgr` was `null`. The composer is guarded on that variable —
`t => openMgr && send({...})` — so it **short-circuited and no websocket frame was ever
sent**. The same bug sat in `openCardDrawer`, breaking card chats identically. The thread
also never rendered, because the inbound handler checks `m.id === openMgr`.

The tell was that `manager_open` frames reached the server but `manager_message` frames
never did — `manager_open` passes the local `id` parameter, so it survived the wipe.

**Fix (the only code change left in the tree):** set the id *after* `show()`.

```js
function openManager(id){
  show('d-mgr');                  // show() → closeAll(), which clears openMgr: set it after
  openMgr=id; seenTs=0;
  ...
}
```

Verified end-to-end by driving the real page in headless Chrome over CDP — clicking the
chip, typing, clicking Send — then confirming the `manager_message` frame reached the
server and the manager replied.

### Two things that made this far harder to diagnose than it should have been

Both are unfixed, and both will hide the *next* bug too:

- **The server swallows errors silently.** `server.py` catches bare `Exception` in the
  websocket endpoint and just disconnects, and the agent runs via fire-and-forget
  `asyncio.create_task(...)` whose exception nobody ever reads. A crashed handler and a
  never-sent frame look **identical** from outside. Log both.
- **A dropped message is invisible in the UI.** The user's own text is only rendered when
  the server echoes it back, so a failed send silently eats what was typed. Echo locally on
  send (or mark it pending until the server confirms).

---

## Part 2 — Decisions to implement

### The one idea everything else follows from: system vs. working folder

The current design assumes **the kanban folder lives inside the repo it works on** and
derives everything from its own location on disk. Every decision below pulls against that:
the **system** must be installable and upgradable on its own, while the **working folder**
holds everything that belongs to *this* project and survives every upgrade untouched.

Draw that line explicitly and make it the first commit. Everything else gets easy.

| | **System** (comes from GitHub, replaced on update) | **Working folder** (yours, never overwritten) |
|---|---|---|
| Contains | `manager/`, `web/`, `run.sh`, `requirements.txt`, default worker templates, **migrations** | Cards + their folders, board structure (pipelines, columns, gates), **agent/worker prompts**, `.env`, manager threads |
| Owned by | the upstream repo | this project |
| On update | wholesale replaced | *migrated*, never clobbered |

> **This resolves the `workers/` question.** Worker prompts are **state**, not system. The
> README currently calls them "source" to be versioned with the system; that's now wrong and
> the README should be corrected. They live on the working-folder side and an update must
> never overwrite them.

The paths must stop being derived from the folder's location and become explicit config —
`MANAGER_DATA_DIR`, `MANAGER_WORKERS_DIR`, `MANAGER_REPO_DIR` all already exist as env vars,
so this is mostly a matter of making them first-class and documented rather than incidental.

### 1. Card folders must live outside the kanban folder

**Already supported, just undocumented.** `MANAGER_DATA_DIR` is read in `server.py` and is
the single lever: `board.json` *and* every card folder (`cards/<pipeline>/<column>/…`, plus
`cards/trash/`) are written under it. Set it and both move together.

To do:
- Document it in `README.md` and `.env.example` (it appears in neither).
- Decide the default location (e.g. `~/.manager-kanban/<project>/`) and migrate the
  existing `data/` there rather than stranding it.
- **Trap:** `board.py` has a fallback that derives the workers dir from
  `dirname(data_dir)`. It's dead today because the server always passes `MANAGER_WORKERS_DIR`
  explicitly — but once `data_dir` moves outside the folder, that fallback would resolve
  somewhere wrong. Remove it or make it explicit.

### 2. The system must live in its own GitHub repo, versioned

Today it is 17 files tracked *inside* the `Personal Whatsapp AI` repo. Extract it to a
standalone repo containing **only the system side** of the table above.

- Keep `data/`, `.env`, `.venv/` out of it (already in `.gitignore`).
- Ship default worker prompts as **templates** (e.g. `workers.default/`), used only to
  scaffold a *new* working folder. They are never copied over an existing one. See
  decision 4 for how upstream prompt improvements still reach you.
- Add a real version (a `VERSION` file plus git tags). Decision 4 needs something to compare
  against, and "what version am I on / what version is this working folder at" must both be
  answerable offline.

### 3. It must survive being shut down and reopened — no work lost

**This already works.** Board state is written atomically (`tempfile.mkstemp` + replace) to
`board.json`, and this session verified it empirically: the server was killed and restarted,
and the manager's conversation thread came back intact, session id and all.

So this is a **regression not to introduce** rather than a feature to build. The requirement
is that relocating the data dir (decision 1) and adding updates (decision 4) must both
preserve it. Worth adding a test that kills and restarts the server and asserts cards,
threads, and card folders all survive.

One honest gap: an **in-flight** agent turn is lost if you kill the server mid-run. The card
and its folder survive; the turn the manager was working on does not. Decide whether that's
acceptable or whether runs need to be resumable.

### 4. It must be updatable — and the update must tell the working folder what to do

This is the substantial one. The requirement, stated precisely:

> Upgrade the system upstream (say, teach it to run remotely on a VM), come back to any
> working folder, pull, and **that folder gains the new capability** — while its cards, its
> column structure, and its agent prompts all survive untouched. The update itself carries
> the instructions for what the working folder must do to come along.

That last clause is the design. **An update is not just new code — it is new code plus an
ordered set of migrations that adapt the working folder.** The version in the working folder
and the version of the system are two different numbers, and the migrations are what close
the gap between them.

**Mechanics:**

- The working folder records the system version it was last migrated to (in `board.json`, or
  a sibling `.kanban-version`). The system repo carries a `VERSION` and a `migrations/`
  directory.
- `./update.sh` (or `run.sh --update`) pulls, reinstalls deps, then runs **every migration
  between the folder's recorded version and the new one, in order**, and stamps the new
  version on success.
- Migrations must be **idempotent, ordered, and forward-only**. Each one is a small script
  that mutates the working folder: add a config key with a safe default, reshape
  `board.json`, add a new per-card file, create a directory the new feature expects.
- **Back up the working folder before migrating, and fail loudly.** A half-applied migration
  that leaves cards in a broken state is the worst outcome available here — worse than
  refusing to update.
- Start recording a schema version in `board.json` **now**, before there is data in the wild
  worth migrating. This costs one line today and is painful to retrofit later.

**Your VM example, concretely.** "Run remotely on a VM" arrives as: new machinery in
`manager/`, plus a migration that adds the new config keys (host binding, auth, remote repo
path) to the working folder's `.env` with safe local-only defaults. Pull, update, and the
capability is present and off; turn it on when you want it. No card is touched.

**The prompts problem, and the honest trade-off.** You've said agent prompts must survive an
update — so a migration can never simply overwrite `workers/`. But that means upstream
improvements to a default prompt would *never* reach a folder you've customized, and the
prompts are where most of the system's quality lives. The way out is a **three-way merge**,
exactly like `git`:

- Keep the pristine default the folder was scaffolded from.
- On update, compare *your* worker against *that old default* against the *new default*.
- Untouched by you → take the new default silently.
- Touched by you → **keep yours**, and report that upstream changed it, showing the diff so
  you can merge deliberately.

This is the only way to satisfy both "my prompts survive" and "my system actually improves."
If that's more machinery than you want at first, the acceptable v1 is: never touch prompts,
but have the update **print** which default prompts changed upstream — the notification alone
recovers most of the value.

### Suggested order

1, then 2 — they are what make 4 safe; updates are only sound once system and state are
cleanly separated. 3 is a constraint on all of them, not a separate task: after every
migration, cards, threads, and folders must still be there.

Do the boring bookkeeping first, in this order — it is cheap now and expensive later:
**record a version and a schema version in the working folder before anything else ships.**
Without that number, no update can ever know what to do.

---

## Operational notes for whoever picks this up

- Start it: `cd "AI Coding-kanban" && ./run.sh` → `http://127.0.0.1:4173`.
- The venv must be Python 3.10+. Rebuild with `python3.12 -m venv .venv` if it breaks.
- The manager acts **only when messaged**. It is not a daemon; nothing advances on its own.
- It runs with `bypassPermissions` against the parent repo — workers can edit files and run
  commands autonomously between gates. **Work on a branch**, especially before pushing a card
  through the *Plan Ready* gate into the build pipeline.
- `python tests/smoke.py` runs headless with no API key.
