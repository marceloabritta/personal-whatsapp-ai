# Self-Learning — Final Steps (what only YOU can do)

> **Status (2026-07-12, updated): the droplet half is LIVE AND PROVEN. The Mac half is BLOCKED.**
> The secretary is already writing real failure reports about itself in production. What does not
> work is the *local* daily job that pulls them down and turns them into bugfix plans — macOS
> blocks it. **No code changes are pending. The remaining work is three chores on the Mac.**

**Context for a future agent:** built in Claude Code session `afe44b80-14f1-4262-95d6-be6320f6d7ff`.
Commits: `14e9c17` (feature), `4a8c92d` (deploy), `086a752` (docs). Full design in
`Shipped Features/2026-07-12 - self-learning.md`; taxonomy in `ARCHITECTURE.md` ("Self-learning")
and `secretary/1. Orchestrator/ORCHESTRATOR.md`.

---

## ⬅ START HERE — the three things left

Marcelo deferred these until his open sessions are done. Nothing is half-finished; each is a
clean, independent chore.

| # | What | Why it's pending |
|---|------|------------------|
| 1 | **Move `~/Desktop/Coding` → `~/Coding`** | Unblocks the daily job. **Decision made: move the whole `Coding` folder** (all 3 projects), not just this repo. |
| 2 | **Run the router fixture** | Needs an API key on the Mac. **Decision made: reuse the app's own key**, read out of the production container. |
| 3 | **Commit the two path fixes** | Already sitting uncommitted in the working tree (see bottom). |

Everything below explains each one.

---

## What the system does (one paragraph)

The secretary writes **failure reports about itself** into `secretary/improvements/` on the
droplet. You pull them to the Mac and a coding agent turns each into an implementation plan.
A malfunction is **exactly three things**: a **code error**, a **soft landing of an uncompleted
task** (declared by the skill via `ctx.sendFailure` — 29 call sites), and **you telling it that
it made a mistake** (`@secretary you made a mistake here` → the `feedback` skill). Everything else
it says is **guidance** ("which task did you mean?", "your list is empty") and files nothing.

**It runs across two machines, and only one of them is stuck:**

- **Droplet (production)** — detects failures, writes reports. ✅ **Working. Proven live.**
- **Mac (local)** — pulls reports, triages them into bugfix plans, commits. ❌ **Blocked by macOS.**

Triage lives on the Mac *by design*: it needs the codebase and a coding agent, and the agent that
**writes code** is deliberately kept away from the box that **runs production**. The daily job is
explicitly denied `git push`, `ssh`, `docker` and `curl`, so even a report containing hostile text
can't talk it into shipping anything. You wake up to plans; you decide what ships.

---

## STEP 1 — Verify it live in WhatsApp ✅ MOSTLY DONE

**This is no longer theoretical — it worked in the real chat.** On 2026-07-12 you sent
`anote erro` about the MedFlower scheduling loop and the system produced:

```
/opt/secretary/improvements/2026-07-12T12-19-23-reported-calendar-action.md
```

That report is **still sitting on the droplet, waiting for Step 1 to unblock the courier.** It
contains the note, the full transcript, the ROUTER/CALENDAR/RESOLVE logs, `Source: OWNER-REPORTED`,
and **no API key anywhere** (redaction verified). It correctly says the offending message wasn't
quoted, because you reported it without replying to one.

**Critically, it FILED the complaint instead of EXECUTING it** — no phantom event was created.
That was flagged as "the one known unverified risk" and it is now **empirically covered in
production**. The `feedback` skill routes correctly. All five skills load
(`calendar_action, transcribe_audio, task_action, feature_request, feedback`).

Still unexercised, if you ever want the rest of the matrix — none are blocking:

| Send this | Expected |
|-----------|----------|
| `@secretary you got the time wrong, move it to 5pm` | **Both** the "noted" confirmation **and** the event actually moves (router returns `["feedback","calendar_action"]`). |
| `@secretary book me a flight to Rio` | "I didn't understand" **and** an `unrouted` report (missing-capability signal). |
| `@secretary transcribe` **without** replying to an audio | The guidance reply and **NO report**. The negative test for the whole taxonomy. |

---

## STEP 2 — ⏳ PENDING: Run the router fixture

**Still has never run.** It calls the **live router** and asserts that a *complaint* is **filed**,
not **executed** — the automated, repeatable version of what production just proved by hand.
Not blocking, but it's the regression guard: **every protection there is a prompt, and prompts
regress silently.** Re-run it after any edit to `router/prompt.js` or a skill manifest.

**The key question is settled — reuse the application's own key.** There is no `ANTHROPIC_API_KEY`
and no `.env` on the Mac; the only copy lives inside the running production container.

Two ways to get it:

```bash
# EITHER: paste the key yourself into a local .env  (.env is gitignored — verified)
echo 'ANTHROPIC_API_KEY=sk-ant-…' >> .env && chmod 600 .env

# OR: read the app's key out of the container. This is a READ; it changes nothing on the droplet.
# NOTE FOR A FUTURE AGENT: Claude Code's permission classifier will BLOCK this unless Marcelo
# explicitly authorizes pulling the credential out of production. "use the same key" is not
# enough — he must name the container read. He has already agreed in principle (2026-07-12).
ssh secretaria-droplet 'docker exec secretary printenv ANTHROPIC_API_KEY'
```

Then:
```bash
cd "/Users/marceloabritta/Coding/Personal Whatsapp AI"   # ← path AFTER the Step 1 move
node scripts/router-selftest.mjs                          # costs a few cents
```

---

## STEP 3 — ⏳ PENDING (THE REAL BLOCKER): move the repo to `~/Coding`

The daily job is **written, installed and loaded** — but it has **never successfully run**:

```
getcwd: cannot access parent directories: Operation not permitted   # exit 126
```

**Why the earlier fix didn't work.** The repo was moved out of Google Drive to `~/Desktop/Coding`,
and the plist was correctly repointed. **This did not help: macOS protects `~/Desktop` under the
same TCC rules as CloudStorage.** `launchd`'s `/bin/bash` can't read either one. The script still
works fine from Terminal (Terminal *has* the permission); the background job does not.

**The fix (decided): move the whole `Coding` folder one level up, out of Desktop.**

```
~/Desktop/Coding/          →     ~/Coding/
    AI Coding-kanban                 AI Coding-kanban
    Mailbox Unjunker                 Mailbox Unjunker
    Personal Whatsapp AI             Personal Whatsapp AI
```

`~/Coding` = `/Users/marceloabritta/Coding` — a plain folder in the home directory, a sibling of
`Desktop`/`Documents`/`Downloads` but **not one of them**, so it is not TCC-protected and needs
**no permission grant at all**. (`fleet` and `google-cloud-sdk` already live there.) Reach it in
Finder with **Go → Home** (⇧⌘H). Git history and remotes are unaffected by a move.

**Only two files in the repo hard-code the path** — this doc and the plist — so the move is cheap.

### The procedure (hand this to Claude Code)

```bash
launchctl unload ~/Library/LaunchAgents/com.marcelo.secretary-triage.plist
mv ~/Desktop/Coding ~/Coding
# repoint BOTH paths in scripts/com.marcelo.secretary-triage.plist to ~/Coding/... , then:
cp scripts/com.marcelo.secretary-triage.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.marcelo.secretary-triage.plist

# prove it — this should now PULL THE WAITING MEDFLOWER REPORT and write a bugfix plan
launchctl start com.marcelo.secretary-triage
sleep 30 && cat ~/Library/Logs/secretary-triage.log
```

You want `self-learning daily run` → a pulled report → `Done.` — **not** `Operation not permitted`.
Also update the `cd` path in Step 2 above and re-point any Finder sidebar shortcut.

> **Rejected alternative:** granting Full Disk Access to `/bin/bash` (System Settings → Privacy &
> Security → Full Disk Access → + → ⌘⇧G → `/bin/bash`). It works and keeps the repo on the Desktop,
> but it hands **every bash script on the machine** full disk access. The move is narrower and free.

---

## STEP 4 — The loop (the payoff, once Step 3 lands)

**Daily at 09:00, automatically** (`scripts/self-learning-daily.sh`, via launchd):

1. pulls the secretary's failure reports off the droplet → `Bugs and Malfunctions/inbox/`
2. **stops right there if there's nothing** — a quiet day costs you nothing, no Claude call
3. otherwise runs `/triage-failures` headless: reads each report, investigates the codebase,
   writes **`Bugs and Malfunctions/bugfix-<slug>.md`**, files the raw report into `_reports/`,
   and **commits**.

**It never pushes and never deploys.** The plans match the two you already have —
`bugfix-task-false-positive.md` and `bugfix-lid-history-blindness.md` — which are the templates the
triage prompt points at: evidence from the logs, the real call chain, a root cause, what was *ruled
out*, and an honest limitation.

Owner-reported files are triaged **first** and treated as ground truth. The "Auto-analysis" section
in a report is a cheap model's *guess* — the triage prompt is told to discard it freely. (The
MedFlower report is a good example: its auto-analysis blames context-window injection, which may
well be wrong.)

**By hand, any time** (works TODAY from Terminal, even with Step 3 pending):
```bash
./scripts/self-learning-daily.sh          # pull + triage, same as the daily run
./scripts/self-learning-pull.sh           # just pull
/triage-failures                          # just triage, inside Claude Code
tail -f ~/Library/Logs/secretary-triage.log   # what the daily job did
launchctl unload ~/Library/LaunchAgents/com.marcelo.secretary-triage.plist   # turn it off
```

---

## STEP 5 — The one habit that makes this work

**Report mistakes the moment you see them, and prefer replying to the wrong message.**

The other triggers only fire when the code *knows* it failed. The failures that actually annoy you
— the wrong time, the false positive, the confidently wrong answer — crash nothing and look like
success. **You are the only detector.** A note you don't send is a bug that does not exist as far
as the system is concerned. The MedFlower report exists only because you typed `anote erro`.

Replying to the offending message hands the engineer the secretary's exact bad output. A bare note
still works (MedFlower was one) — it just costs the triage agent more guesswork.

---

## Uncommitted work sitting in the tree

Two path fixes from the Desktop move, **not yet committed** (they'll need updating again after the
`~/Coding` move, so it may be cleanest to do the move first and commit once):

- `scripts/com.marcelo.secretary-triage.plist` — repointed off Google Drive
- `New Features Plans/Self-Leaning-Final-Steps.md` — this file

---

## What you do NOT need to do

- **No new env var, no OAuth scope, no dependency.** The feature adds none. (The API key in Step 2
  is for a *local test*, not for the app — production already has its own.)
- **No `.gitignore` work** — `secretary/improvements/*.md` is already ignored and verified on the
  droplet (load-bearing: `/opt/secretary` symlinks *into* the production git tree). `.env` is
  ignored too (verified 2026-07-12).
- **No scheduling work.** The daily job is installed and loaded. It only needs the Step 3 move.
- **No droplet work.** That half is done and running.

---

## Open decisions you may want to revisit later

1. **Guidance stays silent.** "Reply to the audio", "which task did you mean?", "your list is
   empty" file nothing. If you later decide a *needSignal* message is really a capability gap worth
   learning from, it's a one-line change per call site (`ctx.send` → `ctx.sendFailure`).
2. **Owner reports never dedupe and never hit the normal hourly cap.** Deliberate: a human can't
   loop, and a silently dropped note is the worst failure this system could have. There's still a
   ~10/hour disk backstop, and if it ever rejects a note the secretary **tells you** instead of
   confirming a lie.
3. **`unrouted` / `noAction` are filed** (your call, 2026-07-12). If they turn out to be noise, drop
   the `fireCapture` in the `notUnderstood` branch of `server.js`.

---

## Health checks (any time)

```bash
# is it up, and did all five skills load?   [verified green 2026-07-12]
ssh secretaria-droplet 'docker logs --tail 40 secretary | grep "available skills"'
#   expect: calendar_action, transcribe_audio, task_action, feature_request, feedback

# what reports are waiting on the droplet?  [1 waiting as of 2026-07-12]
ssh secretaria-droplet 'ls -la /opt/secretary/improvements/'

# the offline test suite (capture invariants + the call-site lint)
node scripts/selflearning-selftest.mjs
```

The lint is the guard that keeps this honest: if anyone adds a skill and sends a failure reply with
plain `send()` instead of `ctx.sendFailure()`, the test run fails with the file and line.
