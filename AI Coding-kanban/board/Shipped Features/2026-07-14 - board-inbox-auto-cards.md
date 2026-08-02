# Plan: feature specs and bugfix plans land on the backlog by themselves

**Planned against:** `6af7f60d2c479e7ff6b1eb108691102e0e26889f` (`main`, 2026-07-14).

Scope: `SCOPE.md` in this card folder, read as a specification. Nothing here adds to it.
Where the scope left a format or an implementation shape open, this plan settles it and says
so out loud (see **Decisions the scope left to the plan**, below).

**The board this card consumes is now the board in git.** The backlog and `kind`-on-create landed
in `aa3ef36` ("upgrade the board to 0.15.0 — ship without killing work, and a backlog"); the
earlier revision of this plan carried a PRECONDITION saying they were uncommitted, and that
precondition is **cleared**. Re-verified at this SHA, against the committed tree and not a dirty
one: `POST /api/card` accepts `kind`, defaults `pipeline` to `BACKLOG`, returns `{id, kind}`, and
spawns `Manager.triage_card` **only** when `kind not in KINDS`; `board.add_card(…, pipeline=BACKLOG,
kind="")`; `GET /api/card/{id}` → `card_view()` → `abs_dir`; `GET /api/board` → `snapshot()` →
`id`, `title`, `description`, `pipeline`, `kind` per **live** card.

**Line numbers under `AI Coding-kanban/` are worthless as anchors** — it is a vendored system
folder that `./update.sh` replaces wholesale (its own `run.sh` header says so). This plan therefore
cites the board **by endpoint and function name, never by line**, and the drain **verifies at
runtime** that the card it created actually landed unrouted and typed (drain step b2, below). That
tripwire is not a leftover from the precondition: it is what protects us the next time the board is
updated underneath us.

---

## Where this feature's files live — a constraint from the human, stated so it cannot be missed

**Everything this card builds lives in the working folder, OUTSIDE `AI Coding-kanban/`.** Every
path, end to end:

| What | Where | Inside `AI Coding-kanban/`? |
|---|---|---|
| The droplet spool the secretary writes specs into | `secretary/specs/` | **No** |
| Where pulled specs land (the feature-request contents) | `New Features Plans/` | **No** |
| Where triaged bugfix plans land | `Bugs and Malfunctions/` | **No** |
| The queue, the ledger, the delivered archive, the lockfile | `Board Inbox/` | **No** |
| The ingest, the wrapper, the timer | `scripts/` | **No** |

**Not one of them may be placed inside `AI Coding-kanban/`** — not the spool, not the queue, not
the ingest script, not the timer. The two systems run in parallel and update on their own
schedules: the secretary feeds the working folder, and the kanban picks it up from there across
its HTTP API. That separation is exactly why `./update.sh` replacing the board wholesale cannot
destroy this feature's state. **`AI Coding-kanban/` is read-only to this card — not one line, not
one new file.**

## A staging instruction for the builder

`AI Coding-kanban/` currently holds **uncommitted changes belonging to another card**. They do not
touch the card API and they do not affect this build. **Stage only the files this plan names —
never `git add -A`.** That other work is not ours to land. (The rest of the repo — `secretary/`,
`scripts/`, `.claude/`, `.gitignore`, `New Features Plans/`, `Bugs and Malfunctions/` — is clean at
this SHA, so every file in your diff should be one this plan named.)

---

## Decisions the scope left to the plan

Four things the scope deliberately did not pin down. Each is settled here so the coder makes
**zero** design decisions. **All four have now been ratified by the manager — they are closed.
Do not reopen them.**

**D1 — SETTLED. The spool filename keeps the `feature-` PREFIX and takes the timestamp as a SUFFIX:
`feature-<slug>-<YYYY-MM-DDTHH-MM-SS>.md`.**
The scope states two normative things that a prefixed timestamp would contradict: the filename
is timestamped (link 1), *and* the enqueue candidate glob is `New Features Plans/feature-*.md`
(link 3). A file named `2026-07-14T09-12-03-feature-x.md` does **not** match `feature-*.md` —
the enqueue would never see it and the card would never appear. The suffix form satisfies both
statements as written; only the scope's illustrative footer example
(`source: 2026-07-14T09-12-03-feature-calendar-conflicts.md`) shows the other order, and an
example is not a contract. **Uniqueness is unaffected** — the timestamp still carries it, and
the `wx` + numeric-suffix loop still closes the same-second case. **The manager ratified this: a
timestamp prefix would never match the enqueue glob, and the card would silently never appear.**

**D2 — The machine-readable header is YAML-shaped frontmatter at the top of the file**,
`---` … `---`, for **both** producers (the spooled spec and the bugfix plan), parsed by one
line-based parser in the ingest. No YAML dependency (no new deps are allowed), no prose parsing.
The scope says the plan "**opens with**" the header; frontmatter is the only shape that is both
literally "opens with" and unambiguous to parse, and it leaves the human-readable header table
in the bugfix plans untouched. The reviewer's alternative (new rows in the existing table) was
rejected for one concrete reason: `reports:` is a **list**, and a list in a padded markdown table
cell is exactly the kind of thing an LLM formats three different ways.

**D3 — Edge 7 and edge 8 can co-occur (spool write fails AND the WhatsApp send fails). The
send-failure message wins, and the owner gets exactly one failure reply.** He needs to know he
never received the file at all; "sent but not filed" would be a lie in that branch. Implemented
as a single if/else-if in `finalize()`.

**D4 — SETTLED. The drain's lock is an exclusive-create lockfile carrying the holder's PID, and a
lock whose PID is dead is broken and re-taken, loudly.** macOS ships no `flock(1)`, and Node has no
`flock` binding, so a lockfile is the mechanism. A lockfile with no staleness rule turns one
crashed drain into a **permanently** un-drained queue — a silent drop of everything, which is
the exact property this card exists to guarantee. Liveness is checked with `process.kill(pid, 0)`
(no signal sent), with a 60-minute mtime backstop for a recycled PID. **The manager ratified the
stale-lock rule explicitly: a drain that dies holding the lock would otherwise un-drain the queue
forever, silently — the exact failure class this card exists to abolish. Break a dead lock loudly.**

---

## Files

### Droplet side — the skill layer (needs a production deploy)

| File | Status | What changes |
|---|---|---|
| `secretary/2. Skills/4. Feature Requests/skill.js` | modify | `finalize()` spools the spec **before** `evolution.sendMedia`. Adds three module-local helpers: `saoPauloStamp()`, `spoolFileName(draft)`, `spoolSpec(draft, md)`. The attachment's `fileName` (`feature-<slug>.md`) is **unchanged**. |
| `secretary/2. Skills/4. Feature Requests/prompt.js` | modify | **One** new localized string, `en` + `pt`: `specFileFailed()` (edge 7). `slugify` is **not** touched. `CLARIFY_SCHEMA` is **not** touched. |
| `secretary/specs/.gitkeep` | **NEW** | The spool directory, tracked as an empty folder — exactly the shape `secretary/improvements/.gitkeep` already uses. Must live inside `secretary/`: the container only mounts `/opt/secretary:/app`. |
| `.gitignore` | modify | Two lines after the existing `secretary/improvements/*.md` block, same load-bearing reason (line 18): `secretary/specs/*.md` and `secretary/specs/_synced/`. |

### Mac side — the pull, the ingest, the timer

| File | Status | What changes |
|---|---|---|
| `scripts/self-learning-pull.sh` | **restructure** | Two spools pulled independently via one `pull_spool()` function; an empty spool is a **skip, not `exit 0`**; the archive moves **only the names captured before the transfer**, replacing the blind `mv *.md _synced/` (line 38). Exits non-zero only if a spool actually **failed**, and only after **both** were attempted. |
| `scripts/self-learning-daily.sh` | **restructure** | Both early exits go (lines 32-35 `exit 1` on a failed pull; lines 38-43 `exit 0` on an empty inbox). Enqueue and drain run **unconditionally** afterwards. Final exit status reflects every failure that happened. |
| `scripts/board-ingest.mjs` | **NEW** | The ingest: `seed`, `enqueue`, `drain`. Deterministic, no model call, no dependency. Exports its functions so the selftest can drive them. |
| `scripts/board-ingest.sh` | **NEW** | Thin wrapper: sets `PATH` (launchd starts with a minimal one), `cd`s to the repo, `exec node scripts/board-ingest.mjs "$@"`. One place where `node` is found, used by both the daily job and the timer. |
| `scripts/com.marcelo.board-ingest.plist` | **NEW** | The drain timer. `StartInterval` 300 + `RunAtLoad true` — **not** the `StartCalendarInterval` / `RunAtLoad false` shape of `com.marcelo.secretary-triage.plist`, which is a naming/logging template only. |
| `scripts/board-ingest-selftest.mjs` | **NEW** | Offline. The ingest's invariants (below). |
| `scripts/pull-archive-selftest.mjs` | **NEW** | Offline. Drives `self-learning-pull.sh` with stub `ssh`/`rsync` on `PATH`. Proves the archive fix and the funnel independence. |
| `.claude/commands/triage-failures.md` | modify | §3 plan template gains the frontmatter header; §2 step 4 gains the filename-uniqueness rule and the declined-report bookkeeping corollary. |
| `Board Inbox/README.md` | **NEW** | The queue, the ledger, the delivered archive, the lockfile — what each is and how to read it. |
| `Board Inbox/.gitignore` | **NEW** | `queue/`, `delivered/`, `.drain.lock` are runtime state, not repo content. **`ledger.tsv` is tracked** — it is the thing that must survive a fresh clone, and losing it re-opens cards for work already done. |

### Untouched, and named so nobody wonders

`AI Coding-kanban/` — **not one line.** `secretary/1. Orchestrator/` — **not one line.**
`secretary/1. Orchestrator/lib/selflearning.js` — **not one line** (the skill borrows the *shape*
of `writeUnique`, not the module; the scope closed this and it is not reopened). Note the real
path: this module lives **inside the rails folder**. The plan below refers to it in full, and
**only ever reads it** — the ingest selftest imports it, exactly as `scripts/selflearning-selftest.mjs`
already does (`await import("../secretary/1. Orchestrator/lib/selflearning.js")`, with
`SELF_LEARNING_DIR` pointed at a temp dir). Importing a rails module in an offline test is not a
rails change; **modifying** it would be, and nothing here does.

---

## Interfaces

### `secretary/2. Skills/4. Feature Requests/skill.js` (skill-local, all module-private)

```js
// ESM PRELUDE — REQUIRED. secretary/package.json is "type": "module", so this file is an
// ES module and `__dirname` DOES NOT EXIST. It must be built, and node:fs/promises and
// node:path/node:url must be imported, EXACTLY as secretary/1. Orchestrator/lib/selflearning.js:20-27
// already does (re-verified at this SHA). Omitting any of these throws `ReferenceError:
// __dirname is not defined` at IMPORT TIME, so the orchestrator fails to load the whole
// feature_request skill at boot. Today skill.js imports NONE of these — the coder adds all four:
import { mkdir, writeFile } from "node:fs/promises";     // the fs calls spoolSpec/writeUnique-shape need
import path from "node:path";
import { fileURLToPath } from "node:url";
const __dirname = path.dirname(fileURLToPath(import.meta.url));  // the shim — no bare __dirname in ESM

// Spool dir. Same shape as REPORTS_DIR in secretary/1. Orchestrator/lib/selflearning.js:31-33
// — env override, else inside secretary/. The `../../specs` arithmetic is correct: from
// secretary/2. Skills/4. Feature Requests/ it resolves to secretary/specs.
const SPEC_DIR = process.env.FEATURE_SPEC_DIR
  || path.join(__dirname, "..", "..", "specs");          // secretary/specs

function saoPauloStamp(d = new Date()): string           // "2026-07-14 09:12:03"  (sv-SE, America/Sao_Paulo)
function specHeader(draft, when): string                 // the frontmatter block (D2)
async function spoolSpec(draft, md): Promise<string|null> // absolute path written, or null. NEVER THROWS.
```

`spoolSpec` — `mkdir(SPEC_DIR, {recursive:true})`; base name
`feature-${slugify(draft.title)}-${stamp.replace(" ","T").replace(/:/g,"-")}` (D1); write
`specHeader(draft, when) + "\n" + md` with `{ flag: "wx" }`, on `EEXIST` take `-2`, `-3` … up
to 50 (the `writeUnique` shape); return the path. Every other error
is caught, logged, and returns `null`. **It never throws** — a spool failure must not break the
send. (`writeUnique` is at `secretary/1. Orchestrator/lib/selflearning.js:164-177`, re-verified at
this SHA; its shape is copied, the module is not imported by the skill.)

`specHeader` collapses newlines in `title` / `one_liner` to single spaces, so the ingest's
line-based parser cannot be broken by a multi-line title:

```
---
title: <draft.title || "">
one_liner: <draft.one_liner || "">
when: 2026-07-14 09:12:03 (America/Sao_Paulo)
---
```

**`finalize()` — the new body, in order** (replacing lines 225-250):

```js
const slug = slugify(draft.title);
const fileName = `feature-${slug}.md`;      // THE ATTACHMENT NAME — unchanged
const spooled = await spoolSpec(draft, md); // BEFORE the send (scope: "written before it is sent")
const base64 = ...; const caption = ...;    // unchanged
let ok = false;
try { ok = await evolution.sendMedia(number, {...}); } catch { ok = false; }   // unchanged
await sessions.clear(remoteJid);
if (!ok) await ctx.sendFailure(number, reply(ctx.lang).sendFailed());          // D3: send failure wins
else if (!spooled) await ctx.sendFailure(number, reply(ctx.lang).specFileFailed());
```

### `secretary/2. Skills/4. Feature Requests/prompt.js`

One new key in **both** language maps of `REPLY`.

**The key is named `specFileFailed`, and the name is load-bearing — this corrects an error in the
previous revision of this plan.** That revision called the key `specNotFiled` and claimed it matched
`FAILURE_KEY_RE`. It does not. Re-verified at this SHA,
`secretary/1. Orchestrator/lib/selflearning.js:78` is:

```js
export const FAILURE_KEY_RE = /(error|failed|failure|unavailable|noMatch|noAction)/i;
```

`specNotFiled` contains `Filed`, not `failed` — **it does not match**, so the lint in
`scripts/selflearning-selftest.mjs` would have silently skipped it and the key would have shipped
with no guard at all. Renaming it to **`specFileFailed`** makes it match (`Failed`), which puts it
under the lint and *enforces* the `ctx.sendFailure` call this plan already requires. The reply text
is unchanged; only the key name is. Send it with `ctx.sendFailure` (see `finalize()` above) — the
owner asked for something and did not fully get it.

```js
en: { specFileFailed: () =>
  "I sent you the spec, but I couldn't file my own copy — so it won't reach the board. Save the file yourself." },
pt: { specFileFailed: () =>
  "Te mandei a spec, mas não consegui salvar minha cópia — então ela não vai chegar no board. Guarde o arquivo." },
```

### `scripts/board-ingest.mjs`

CLI: `node scripts/board-ingest.mjs <seed|enqueue|drain>`. Also exports every function, so the
selftest drives it in-process with a temp repo and a stub `fetch`:

```js
export function parseHeader(text): { title, one_liner, reports: string[], had: boolean }
export async function seed(opts): Promise<{ seeded: number }>
export async function enqueue(opts): Promise<{ queued: string[] }>
export async function drain(opts): Promise<{ delivered: string[], waiting: number, boardDown: boolean, lockHeld: boolean }>
// opts = { repoDir, boardUrl, fetch }  — all defaulted from env/cwd in the CLI path.
```

Board URL default `http://127.0.0.1:4173`, honouring `MANAGER_HOST` / `MANAGER_PORT` exactly as
`AI Coding-kanban/manager/__main__.py` does (re-verified at this SHA:
`os.environ.get("MANAGER_HOST", "127.0.0.1")`, `int(os.environ.get("MANAGER_PORT", "4173"))` —
cited by name, not by line, per the vendored-folder rule above).

**On-disk layout** (all under `Board Inbox/`):

| Path | What |
|---|---|
| `ledger.tsv` | Append-only, **tracked in git**. One line per source file accounted for: `<iso>\t<repo-relative path>\t<why>`, `why ∈ seed \| enqueued \| planned`. Membership is tested on the **path** column. This — not the directory listing, not "what this run transferred" — is the authority. |
| `queue/<basename>.json` | One queued card request. Basenames are unique across both funnels (`feature-…` vs `bugfix-…` vs timestamped reports), so the queue filename is also the natural de-dup key. |
| `delivered/<basename>.json` | The archive. **An archived entry is never reconsidered** (edges 15, 19). |
| `.drain.lock` | The single-flight lockfile (D4). |

**Queue entry:**

```json
{ "source": "New Features Plans/feature-x-2026-07-14T09-12-03.md",
  "basename": "feature-x-2026-07-14T09-12-03.md",
  "title": "…", "one_liner": "…", "kind": "feature",
  "cardId": null, "queuedAt": "2026-07-14T09:12:03Z" }
```

`kind` is `feature` or `maintenance` — the two values `AI Coding-kanban/manager/models.py` defines
as `KINDS` (re-verified at this SHA: `KINDS = (FEATURE, MAINTENANCE)`). Sending it is what stops the
board spending an LLM call on `Manager.triage_card`, which `POST /api/card` spawns **only** when
`kind not in KINDS` — also re-verified at this SHA. A card created with a valid `kind` therefore
costs **nothing**.

**`seed(opts)`** — writes `ledger.tsv` with `why=seed` for every file already on disk:
`New Features Plans/*.md` (all 4 — the glob is deliberately wider than the enqueue's, so
`Self-Leaning-Final-Steps.md` and `reminders-followups.md` can never be reconsidered either),
`Bugs and Malfunctions/bugfix-*.md` (2), `Bugs and Malfunctions/_reports/*.md` (2). **Refuses to
run if `ledger.tsv` already exists** (prints what it would have done, exits 0). Idempotent.

**`enqueue(opts)`** — **hard interlock: if `ledger.tsv` does not exist, it prints
`ledger missing — run 'node scripts/board-ingest.mjs seed' first` and exits 1 without queuing
anything.** An absent ledger is never read as an empty one; that mistake is exactly edge 16, and
it opens 6+ cards for work already done. Then, three candidate sources, each skipped if its path
is already in the ledger:

1. `New Features Plans/feature-*.md` → `kind: "feature"`.
   Title: header `title:` → header `one_liner:` → `Feature request (<YYYY-MM-DD from the file's
   mtime>)`. **Never `null`, never `feature`** (edge 9).
2. `Bugs and Malfunctions/bugfix-*.md` → `kind: "maintenance"`.
   Title: header `title:` → the file's first `# ` heading (edge 13's fallback), and the missing
   header is logged with `console.error("board-ingest: <f> has no header — falling back to # H1")`.
   Loud, never silent.
3. `Bugs and Malfunctions/_reports/*.md` where **all three** hold → `kind: "maintenance"`
   (edge 11):
   - not in the ledger, **and**
   - named by no plan's `reports:` header (every `bugfix-*.md` in `Bugs and Malfunctions/` is
     parsed each run; `reports:` values are compared **by basename**, so `_reports/x.md` and
     `x.md` both match), **and**
   - **owner-reported**, by the predicate that matches what the generator really writes: a header
     table row `| Trigger     | reported |` (`secretary/1. Orchestrator/lib/selflearning.js:206`)
     **or** a `| Source      | **OWNER-REPORTED** (human-verified) |` row (`:207`) — matched with a
     regex tolerant of column padding (`/^\|\s*Trigger\s*\|\s*reported\s*\|/mi` and
     `/^\|\s*Source\s*\|.*OWNER-REPORTED/mi`). **Both rows re-verified verbatim against the
     generator at this SHA.**
     **Never grep for `Source: OWNER-REPORTED`** — that string exists in no report; it is prose in
     `.claude/commands/triage-failures.md:16`, re-verified at this SHA.
   Title: the report's first `# ` heading. One-liner: the `**What the owner says went wrong:**`
   line if present, else empty.
   A report that a plan **does** name is written to the ledger with `why=planned` — it is
   accounted for and can never be reconsidered, which keeps edge 11 stable even if a future card
   starts archiving bugfix plans (a fragility the scope review flagged as currently accidental).
   A **machine** report with no plan is neither queued nor ledgered — it stays a no-op forever
   (edge 12: the noise filter).

   Each queued file is appended to the ledger with `why=enqueued` **before** its queue entry is
   written, so a crash mid-enqueue can only ever lose a *card*, never duplicate one — and the
   next run's log says which file was ledgered without a queue entry. *(This is the one place the
   design prefers a missed card to a double card; it is logged loudly and the file is one `+ New
   card` away.)*

**`drain(opts)`** — the whole run is under the lock (D4):

**EVERY board call in this function is wrapped with a timeout — no exceptions.** Node's global
`fetch` has **no default timeout**, and an untimed call defeats the single-flight lock: a hung
`POST /api/card` (the scope itself notes a board busy spawning agents makes POSTs slow) keeps a
**live** drain holding the lock past the 60-minute mtime backstop (step 0); the backstop then
breaks a live holder's lock, a second drain reads `cardId: null`, reconciles against a board that
has not been POSTed to yet, and POSTs a **duplicate card** — the exact failure the lock exists to
prevent. A hung call also hangs the whole nightly job (step 4 never returns). Therefore:
- Pass `{ signal: AbortSignal.timeout(BOARD_TIMEOUT_MS) }` on **every** board call — the
  `POST /api/card` (step b) **and** both `GET`s (`GET /api/board` step 2, `GET /api/card/{id}`
  steps c and d). `const BOARD_TIMEOUT_MS = 30_000;` — 30 s per request.
- **The whole run is bounded well under the 60-minute backstop, provably**, not just per-request:
  the drain also carries a run-wide wall-clock ceiling `const DRAIN_RUN_MS = 30 * 60_000;` (30 min),
  captured once at entry from `Date.now()`. Before starting each queue entry it checks elapsed time
  and, if the ceiling is reached, **stops the loop and leaves the remaining entries queued**
  (logged: `board-ingest: run budget reached — N entrie(s) left for next tick`). With the run
  capped at 30 min and each request at 30 s, a live drain can **never** outlive the 60-min backstop,
  so the backstop only ever breaks a genuinely dead holder.
- **A timed-out request is an ordinary failure, handled by the existing design:** the `AbortSignal`
  makes `fetch` reject; the entry keeps `cardId: null` (nothing was recorded), stays queued, and is
  retried on the next 5-minute tick. A timeout on the `POST` is indistinguishable from a down board
  (edge 1) and takes the same path — no id recorded, no duplicate, no data loss.

0. **Acquire** `Board Inbox/.drain.lock` with `writeFile(…, {flag:"wx"})` containing
   `{pid, startedAt}`. On `EEXIST`: read it; if `process.kill(pid, 0)` throws `ESRCH` **or** the
   file's mtime is > 60 min old → log `stale lock from pid N — breaking it`, unlink, retry **once**.
   Otherwise **log `another drain holds the lock — nothing to do` and return `{lockHeld:true}`,
   exit 0.** A held lock is a success (edge 21). Released in a `finally`.
1. Read `queue/*.json` in filename order. Empty → silent no-op, exit 0 (edge 20).
2. `GET /api/board` **with `{ signal: AbortSignal.timeout(BOARD_TIMEOUT_MS) }`** (→
   `Board.snapshot()`, which returns `id`, `title`, `description`, `kind` and
   `pipeline` for every **live** card — trashed ones are skipped, which is the one residual hole,
   Risk 5). Connection refused / any network error / **timeout** → log `board not running — N
   card(s) waiting`, **exit 0** (edge 1). A down board — or one too slow to answer inside the
   budget — is not an error.
3. Per entry:
   - **a. Reconcile** — if `entry.cardId` is null, scan the snapshot's cards for one whose
     `description` contains the exact line `source: <basename>`. Found → **adopt its `id`, persist
     it to the entry on disk, do not POST** (edge 4 — the lost-ack close).
   - **b. Create** — still no id → `POST /api/card` **with `{ signal:
     AbortSignal.timeout(BOARD_TIMEOUT_MS) }`** and body `{title, description, kind}`. No
     `pipeline` (the board defaults it to `BACKLOG` — `add_card(…, pipeline: str = BACKLOG)`,
     re-verified at this SHA), no `manager_id` (the board assigns one). **If the POST times out,
     it rejects before returning an id: nothing is recorded (`cardId` stays null), the entry stays
     queued, and the next tick retries — identical to the down-board path, so no duplicate can
     arise from a slow POST.**
     `description` = `<one_liner>\n\nsource: <basename>` — the footer is the last line, and it is
     what step (a) matches on. **The id is written into the queue entry on disk the instant POST
     returns, before anything else** (edge 5, edge 3).
   - **b2. THE TRIPWIRE — verify the board honoured it.** The response gives `{id, kind}`. If
     `kind` comes back absent or different from what we asked, **log loudly**:
     `board-ingest: this board did not accept the 'kind' we sent — it is not the board this feature
     was built against (card <id> may be mis-typed and mis-filed)`. Same check against the next
     `GET /api/board`: **if the new card's `pipeline` is not the backlog, say so, just as loudly.**
     **It does not retry and does not create a second card** — the card exists; the operator is
     simply told the board is the wrong version.

     **Keep this. It is not a leftover from a cleared precondition — it stands on its own merit.**
     `AI Coding-kanban/` is a **vendored** folder and `./update.sh` replaces it **wholesale**, so
     the card API can move underneath us between one run and the next, with no diff in our repo to
     warn us. Without this check, a board that quietly stopped honouring `kind` would file every
     card into a pipeline, routed and mis-typed, and **nobody would find out**. The tripwire is the
     difference between "it silently filed six cards into the wrong pipeline" and "it told you."
   - **c. Copy** — `GET /api/card/{id}` **with `{ signal: AbortSignal.timeout(BOARD_TIMEOUT_MS) }`**
     (→ `board.card_view()` → `abs_dir`) → copy the source file
     into `<abs_dir>/<basename>`. A **404** here means the card was deleted mid-drain → archive the
     entry as delivered-then-deleted, do **not** re-POST (edge 19). A **timeout** here leaves the
     entry queued with its `cardId` already recorded — the retry re-uses that id and only re-copies,
     never a second POST.
   - **d. Confirm, then archive** — re-fetch `abs_dir` **(again with `{ signal:
     AbortSignal.timeout(BOARD_TIMEOUT_MS) }`)** (`Board.move_card` `shutil.move`s the card's
     folder when it is routed, so a path fetched a moment ago can be stale) and confirm the file is
     present. Absent → copy once into the new `abs_dir` and re-confirm (edge 6). Confirmed → move
     the entry to `delivered/`. **Not confirmed → leave it queued.** The retry re-uses the recorded
     `cardId` and only re-copies; it never creates a second card.
4. Exit 0 on a clean run, on a down board, and on a held lock. Exit 1 only if the board was **up**
   and an entry actually errored (a 500, a failed copy) — the entry stays queued and is retried on
   the next tick regardless; the non-zero status is there so the nightly job's final status is honest.

### `scripts/self-learning-pull.sh`

```bash
pull_spool <remote_dir> <local_dest> <label>   # 0 = pulled or empty; 1 = failed
```

`set -uo pipefail` — **`-e` is dropped deliberately, and the reason is written in the header
comment**: with `set -e`, a function invoked as `pull_spool … || rc=1` runs with errexit
*suppressed*, so a failing `rsync` would fall through to the archive `mv` and destroy reports
that were never transferred. Every step therefore checks its own status explicitly:

1. `names=$(ssh "$REMOTE" "ls -1 <dir>/*.md 2>/dev/null | xargs -n1 basename")` — `|| return 1`.
   **This list, captured before the transfer, is what gets archived** (fixing line 38).
2. Empty list → `echo "no new <label>."`; **`return 0`** — a skip, not an `exit 0` (this is the
   line that made the feature half unreachable on a quiet day).
3. `printf '%s\n' "$names" | rsync -az --ignore-existing --files-from=- "$REMOTE:<dir>/" "<dest>/"`
   — `|| { echo "rsync FAILED for <label> — NOTHING archived"; return 1; }`. Only the captured
   names cross.
4. `printf '%s\n' "$names" | ssh "$REMOTE" "cd <dir> && mkdir -p _synced && xargs -I{} mv -- {} _synced/"`
   — `|| { echo "archive FAILED for <label> (files stay; next run re-pulls)"; return 1; }`.
   **Only the captured names are archived.** A file written into the spool between steps 1 and 4
   is not in the list, is not archived, and is pulled tomorrow (edge 18). **`rsync
   --remove-source-files` is forbidden** — it deletes the droplet's copy instead of staging it
   into `_synced/`, destroying the recoverability the script's own header promises (lines 9-11).

Body: call `pull_spool /opt/secretary/improvements "$REPO/Bugs and Malfunctions/inbox" reports`
and `pull_spool /opt/secretary/specs "$REPO/New Features Plans" specs`, **each into its own `rc`**;
neither one's failure or emptiness skips the other. `exit 1` iff either returned 1 — after both ran.

### `scripts/self-learning-daily.sh`

`rc=0`, and then, in order — **no step is skipped because an earlier one failed or was empty**:

1. `./scripts/self-learning-pull.sh` → on failure: `echo "pull FAILED (droplet unreachable?) — continuing: work already on the Mac still gets delivered"`, `rc=1`. **No `exit 1`** (edges 17, 23).
2. Inbox non-empty → run `claude -p "/triage-failures"` headless, allow/deny lists **unchanged**
   (it still cannot `curl`, `ssh`, `push` or `docker`). Empty → `echo "inbox empty — nothing to triage."`, **skip**, do not exit. Non-zero triage → `rc=1`.
3. `./scripts/board-ingest.sh enqueue` — **always**. Non-zero → `rc=1`.
4. `./scripts/board-ingest.sh drain` — **always**. Non-zero → `rc=1`. Board down → 0, and it says so.
5. `exit $rc`.

The enqueue and the drain are **plain shell/node in the script**, never something the headless
agent does — its deny-list forbids `curl` and `ssh` (lines 56-58) and that stays true.

### `scripts/com.marcelo.board-ingest.plist`

`Label` `com.marcelo.board-ingest`; `ProgramArguments` = `/bin/bash <repo>/scripts/board-ingest.sh drain`;
**`StartInterval` `300`** (poll every 5 min); **`RunAtLoad` `true`**; `StandardOutPath` /
`StandardErrorPath` `~/Library/Logs/board-ingest.log`; `WorkingDirectory` the repo. Header comment
carries the same install/unload/tail instructions as `com.marcelo.secretary-triage.plist`.

### `.claude/commands/triage-failures.md`

Two edits, both small, both to an **LLM contract** (edge 13 is the honest degradation):

- **§2 step 4** (currently line 37-38's list) becomes three rules:
  - *Write the plan to `Bugs and Malfunctions/bugfix-<slug>.md`.* **NEW: if that name already
    exists, take the next free one — `bugfix-<slug>-2.md`, `-3`. NEVER overwrite an existing plan.**
    (Edge 22 — and the two archived reports in `_reports/` are literally the same slug, so this
    input already exists in the repo.)
  - *Move the raw report to `_reports/`.* **NEW corollary: a report you decline to plan is still
    moved to `_reports/`, and is named by no plan.** (That is what makes edge 11 derivable; today
    a declined report's fate is undefined.)
- **§3** gains the plan header, above the `# H1`, with an explicit "every plan, every time":

  ```
  ---
  title: <the plan's title — this becomes the card's title on the board>
  one_liner: <one sentence — this becomes the card's description>
  reports:
    - _reports/<the raw report file this plan was written from>
    - _reports/<…and any other it was merged from>
  ---
  ```

---

## Rails changes

**None.**

Verified against the code, not assumed:

- The skill writes with `node:fs/promises` and reads its spool path from `process.env` — the
  shape `secretary/1. Orchestrator/lib/selflearning.js:31-33` (`REPORTS_DIR`) already uses.
  `finalize()` (`skill.js:209-251`, re-verified at this SHA — the replaced body is lines 225-250)
  closes over everything it needs, and `ctx.sendFailure` already exists and is used five times in
  that file. **No new `ctx` field** (the two rails changes that don't look like one — a `ctx` field
  and a new `lib/` module — are both explicitly avoided). **No new `lib/` module**: lifting
  `writeUnique` / `REPORTS_DIR` into a shared `lib/spool.js` would be rails from birth, and the
  feature skill needs none of `captureFailure`'s dedupe, hourly caps or redaction. It copies the
  *shape*, not the module. The scope closed this; the manager has re-confirmed it (**skill layer
  only, no `lib/spool.js`**); it is not reopened.
- **The ingest selftest `await import`s `secretary/1. Orchestrator/lib/selflearning.js` to generate
  its fixture reports** (test 8). That module is inside the rails folder, but the test only
  **reads** it — `scripts/selflearning-selftest.mjs` already imports it exactly this way. Reading a
  rails module from an offline test is not a rails change; **not one line of it is modified.**
- **`manifest.description` is NOT changed** (`skill.js:43-49`). No new skill is added.
  `router/prompt.js` is not touched. **Therefore: no live router check, and no money spent.**
  (Conventions §1 consequence 3 / §5.)
- `evolution.sendMedia` is reused **unchanged**; no new Evolution client method.
- Everything else this card builds — `scripts/`, `Board Inbox/`, `.claude/commands/` — is outside
  `secretary/` and cannot break a skill by construction. `.gitignore` is repo hygiene, not rails.

There are **no rails callers to keep working**, because there is no rails change.

---

## Sequence

The tree is working — and the board is card-free — at every step. The ingest is built and
seeded *before* anything can feed it, which is what keeps the first run from opening six cards.

0. **The gate is already open.** The board upgrade (backlog + `kind`-on-create) is committed
   (`aa3ef36`) and this plan is verified against `6af7f60`. **Nothing to wait for — start at 1.**
   One standing rule for every step: `AI Coding-kanban/` holds another card's uncommitted work, so
   **stage only the files named in this plan; never `git add -A`.**
1. **`scripts/board-ingest.mjs` + `scripts/board-ingest.sh`** — the module, inert. Nothing calls
   it yet. `Board Inbox/README.md` + `Board Inbox/.gitignore`.
2. **`scripts/board-ingest-selftest.mjs`** — green before anything is wired. This is the step that
   proves "nothing dropped, nothing created twice" while it is still cheap to be wrong.
3. **Seed the ledger, once, by hand:** `node scripts/board-ingest.mjs seed` → commits
   `Board Inbox/ledger.tsv` with the 8 files already on disk. **Nothing that predates this card can
   ever become a card from here on.** (`enqueue` refuses to run before this — the interlock.)
4. **`.claude/commands/triage-failures.md`** — the header + the uniqueness rule. Independent of the
   code; nothing breaks if it lands first, and every plan written after it is ingestible.
5. **`scripts/self-learning-pull.sh`** — the restructure + the archive fix (both spools). The spec
   spool does not exist on the droplet yet: `ls` on a missing dir yields an empty list → a clean
   skip. Safe to ship before step 7.
6. **`scripts/pull-archive-selftest.mjs`** — green.
7. **`scripts/self-learning-daily.sh`** — the restructure; wires in `enqueue` + `drain`.
   From here the malfunction half is **live end to end** with no deploy: a bugfix plan written
   tonight is a card tomorrow.
8. **`scripts/com.marcelo.board-ingest.plist`** — install the timer (`launchctl load`). Cards now
   appear within ~5 min of the board starting instead of at the next 09:00.
9. **The skill:** `prompt.js` (the string) → `skill.js` (the spool, **with the ESM prelude —
   the three imports and the `__dirname` shim; a bare `__dirname` crashes the skill at boot**) →
   `secretary/specs/.gitkeep` → `.gitignore`. Run `node scripts/selflearning-selftest.mjs` (its
   lint reads every skill's `skill.js` as **source text** and never imports it, so it enforces the
   `ctx.sendFailure` string contract but **CANNOT catch a load-time `ReferenceError`**). The real
   guard for the load-time crash is a boot smoke check: `cd secretary && ANTHROPIC_API_KEY=dummy
   npm start` — expect the line `skill loaded: feature_request` in the output. If the ESM prelude
   is wrong, the orchestrator throws at import and that line never appears.
10. **Docs** (all of them — the build is not done until they are).
11. **Deploy** — the human's gated call: `git pull` + `docker compose restart secretary` on the
    droplet (`PROJECT_LOG.md` §2). **The feature half does not work until this happens.** Nothing
    else in this card needs it.

---

## Tests

Two new standalone offline scripts. No framework, no runner, no new dependency — the shape of
`scripts/selflearning-selftest.mjs` and `scripts/history-selftest.mjs` (a `check(name, cond)`
counter, `PASS`/`FAIL (n)`, `process.exit(failures ? 1 : 0)`).

### `node scripts/board-ingest-selftest.mjs` — the promise itself

Drives the exported functions against a temp repo fixture (`mkdtemp`), with a **stub board**: an
in-memory object exposing `fetch(url, init)` that implements `POST /api/card` (mints an id,
stores title/description/kind), `GET /api/board` (returns the live cards) and
`GET /api/card/{id}` (returns an `abs_dir` under the temp dir). Every failure mode is produced by
telling the stub to misbehave. No network, no keys, no board.

**Nothing is created twice:**
1. **The lost ack** (edge 4) — the stub creates the card and *then* throws instead of returning.
   The entry has no `cardId`. The next drain reconciles against `GET /api/board` by the
   `source:` footer, adopts the id, and **POSTs zero times**. Assert: the stub board holds
   **exactly one** card, and the entry is archived.
2. **Failed copy, then retry** (edge 5) — the copy fails (the stub's `abs_dir` does not exist).
   Assert: the entry is **not** archived, `cardId` **is** recorded, and the retry copies without a
   second POST. One card.
3. **Two drains** (edge 21) — the lock is taken by a live PID; a second `drain()` returns
   `{lockHeld:true}`, POSTs nothing, exits 0. Then: a lock whose PID is dead is **broken**, and
   the drain proceeds. *(This test is why the queue can never deadlock.)*
4. **Enqueue is idempotent** (edge 14) — running it twice over the same tree yields the same
   entries, not double.
5. **Delivered never resurrects** (edges 15, 19) — an archived entry is not re-queued when its
   source file is edited; a `GET /api/card/{id}` 404 mid-drain archives rather than re-POSTs.

**Nothing is silently dropped:**
6. **The seed** (edge 16) — seed the 8-file fixture, then enqueue: **zero** entries. Then drop a
   new `feature-*.md` and a new `bugfix-*.md` in: **exactly two** entries, kinds `feature` and
   `maintenance`. And `reminders-followups.md` / `Self-Leaning-Final-Steps.md` never match.
7. **The interlock** — `enqueue()` with no `ledger.tsv` **throws / exits 1 and queues nothing**.
   (An unseeded first run is the six-unwanted-cards bug.)
8. **The owner-reported predicate** (edge 11) — the fixture report is **generated by the real
   `captureFailure`**, imported the way `scripts/selflearning-selftest.mjs` already imports it:
   `await import("../secretary/1. Orchestrator/lib/selflearning.js")` with
   `process.env.SELF_LEARNING_DIR` pointed at the fixture's `_reports/` **before** the import,
   `phase:"reported"`, no `ctx.anthropic` → no network. This is the point: the predicate is tested
   against **what the generator actually writes**, not against a string copied by hand. Assert: an
   owner report no plan names → **one** maintenance card carrying the report; the same report once
   a plan's `reports:` header names it → **no** card (edge 10); a **machine** report
   (`phase:"soft"`) no plan names → **no** card (edge 12). *A regression that re-introduces
   `Source: OWNER-REPORTED` turns test 8 red.*
9. **Board down** (edge 1) — the stub refuses the connection. Entries stay queued, `drain()`
   returns `boardDown:true`, exit code **0**.
10. **Titles** (edges 9, 13) — a spec with no title falls back to the one-liner, then to
    `Feature request (<date>)`: **never `null`, never `feature`**. A plan with no header falls back
    to its `# H1` and **logs**.
11. **The footer** — the created card's `description` ends with the exact line
    `source: <basename>`, which is what test 1 reconciles on. If someone reformats the footer,
    both tests go red together.
12. **The wrong-board tripwire** (drain step b2) — a stub board that **ignores `kind`** and files
    the card into a pipeline instead of the backlog. Assert: the drain **still delivers exactly one
    card**, and it **reports the mismatch** (`drain()` returns it; the CLI logs it). It must never
    fail silently against a board that is not the one this was built against. **This test is not
    tied to the old precondition — it guards against `./update.sh` swapping the vendored board out
    from under us**, which is a live, permanent risk.

### `node scripts/pull-archive-selftest.mjs` — the restructured pull

Runs the **real `self-learning-pull.sh`** in a subprocess, with a temp dir prepended to `PATH`
holding stub `ssh` and `rsync` executables that read a fixture "droplet" from disk and log every
invocation. Offline by construction — there is no droplet.

1. **The blind archive is gone** (edge 18) — the stub `rsync` creates a **new file in the fixture
   spool mid-transfer**. Assert: the archive `mv` is invoked with **exactly the names captured
   before the transfer**, and the mid-pull file is **still in the spool**, not in `_synced/`.
   *Against today's `mv *.md _synced/` this test fails.* This is the pre-existing silent-drop bug.
2. **Nothing is archived when the transfer fails** — the stub `rsync` exits 1. Assert: **no `mv`
   is issued at all**, and the script exits non-zero.
3. **The funnels are independent** — an **empty report spool** and a **non-empty spec spool**:
   assert the specs are still pulled (today's line 27-31 `exit 0` makes this fail), and vice
   versa. And a **failing** report pull still lets the spec pull run.
4. **`--remove-source-files` is never passed** — a grep over the recorded `rsync` argv. It is
   forbidden, and this is the tripwire.

### Not testable offline, and said so rather than faked

The triage prompt's header contract and its uniqueness rule are **prompt instructions to an LLM**
— no offline test can prove an agent will honour them, and a fabricated one would be worse than
none. What *is* tested offline is the deterministic layer around it: the ingest parses a correct
header (test 8), and **degrades loudly and correctly when the header is missing** (test 10, edge
13). The human check is the next real `/triage-failures` run: the plan it writes opens with the
frontmatter block.

**No live router check is needed** — no skill is added, no `manifest.description` changes,
`router/prompt.js` is untouched. **No API spend.**

---

## Documentation changes

| File | What changes |
|---|---|
| `PROJECT_LOG.md` **§10** | **Required — a dated entry** (`YYYY-MM-DD` = the ship date). It states the flow (spec/plan → spool → pull → queue → card in the backlog, unrouted, typed), that the board is untouched and the ingest costs nothing, **and — explicitly — that the droplet archive was a pre-existing SILENT-DROP bug that shipped**: `self-learning-pull.sh:38` blind-`mv`'d `*.md` to `_synced/`, so any report written in the window between the rsync and the archive was moved out of the spool **having never been transferred** — destroyed, unreported. Latent for reports; on the happy path for specs. Closed by this card. |
| `PROJECT_LOG.md` **§4** | Repo layout gains `Board Inbox/` (queue + ledger + delivered) and `scripts/board-ingest.*`. |
| `PROJECT_LOG.md` **§9** | Testing list gains the two new selftests, with the one-line "what it exists to catch" each — matching the style of the existing entries. |
| `PROJECT_LOG.md` **§2** | One line: the spec spool is a **droplet-side skill change and needs a deploy**; until it is deployed, the malfunction half runs and the feature half does not. |
| `ARCHITECTURE.md` **§8b** | The `feature_request` delivery section gains the hop: the same markdown is spooled to `secretary/specs/` (timestamped filename + frontmatter header) **before** the send; the attachment is byte-for-byte what it was, and its filename is unchanged. |
| `ARCHITECTURE.md` — *Self-learning* section (line 430) | The loop gained an end: a triaged plan (and an owner report triage declined to plan) becomes a **card on the kanban backlog**. Name `Board Inbox/`, the ledger, and the fact that the board is consumed through its existing HTTP API and is not modified. |
| `README.md` | "Skills (today)" → the `feature_request` bullet says the spec also lands on the board as a card. "Repository layout" gains `Board Inbox/`. |
| `Bugs and Malfunctions/README.md` | "How a file gets here" gains its ending: the plan becomes a card. **And the plan-header contract lives here** (`title` / `one_liner` / `reports:`), because it is what the ingest depends on. Also: a declined report is still filed to `_reports/`, and it earns a card if it was owner-reported. |
| `secretary/README.md` | The structure tree gains `specs/` — the feature-spec spool, alongside `improvements/`. |
| `secretary/2. Skills/4. Feature Requests/SKILL.md` | Behaviour changed: the spec is written to the spool **before** it is sent (so a failed send no longer loses it), the spool copy carries a header and a timestamped name while **the attachment does not change**, and there is one new failure reply (sent, but not filed). |
| `Board Inbox/README.md` (**NEW**) | What the queue, the ledger, the delivered archive and the lockfile are; that the ledger is **tracked** and losing it re-opens old cards; how to hand-drain (`./scripts/board-ingest.sh drain`). |
| `secretary/1. Orchestrator/ORCHESTRATOR.md` | **No change needed.** The rails do not change — no `ctx` field, no `lib/` module, no dispatch change. Stated explicitly, per convention 4. |
| `AI Coding-kanban/**` | **No change. Read-only to this card.** Not one line, including its docs. |

**The plan doc, and its archive on ship** (convention 4): the build writes this plan into the repo
as **`New Features Plans/board-inbox-auto-cards.md`**, and on ship it is archived with
`git mv "New Features Plans/board-inbox-auto-cards.md" "Shipped Features/YYYY-MM-DD - board-inbox-auto-cards.md"`
(`git mv`, real ship date). **The filename deliberately does not start with `feature-`** — a plan doc
named `feature-*.md` would match the enqueue's own glob and open a card for itself.

---

## Migrations / config

- **One-time, before the first enqueue ever runs:** `node scripts/board-ingest.mjs seed`, then
  **commit `Board Inbox/ledger.tsv`**. This is sequence step 3 and it is not optional — the
  `enqueue` interlock refuses to run without it.
- **One-time:** install the drain timer —
  `cp scripts/com.marcelo.board-ingest.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.marcelo.board-ingest.plist`.
  Log: `~/Library/Logs/board-ingest.log`. (The existing triage timer stays exactly as it is.)
- **No new dependency, in either `package.json`.** Node 25 is on the Mac (`node -v` → `v25.2.1`);
  global `fetch` is available, so the ingest needs no HTTP client.
- **No new env var is required.** `FEATURE_SPEC_DIR` (droplet) and `MANAGER_HOST` / `MANAGER_PORT`
  (Mac) are **optional overrides with working defaults** — `secretary/specs` and
  `http://127.0.0.1:4173`. Nothing to add to `.env` on the droplet, which keeps the deploy to
  `git pull` + `docker compose restart secretary`.
- **The deploy is the human's**, and only the feature half needs it.

---

## Risks — where this plan is most likely to be wrong

0. **The previous revision's PRECONDITION is CLEARED, and the risk it named is gone.** The backlog
   and `kind`-on-create landed in `aa3ef36`; every board fact this plan depends on is re-verified
   against the **committed** tree at `6af7f60`. What replaces it is a smaller, permanent risk —
   **the board is vendored and `./update.sh` replaces it wholesale**, so the card API can change
   with no diff in our repo. That is what drain step b2 and test 12 exist for, and it is why they
   must not be dropped as "precondition leftovers". See Risk 6.

1. **The `specFileFailed` key name — a real error caught in this lap.** The previous revision named
   it `specNotFiled` and asserted it matched `FAILURE_KEY_RE`. It does not (`Filed` ≠ `failed`), so
   the `selflearning-selftest.mjs` lint would have silently skipped the key. Renamed here. **If a
   future reviewer "restores" the old name, the lint stops guarding the call and nobody is told.**
   *(D1 — the timestamp suffix — was the risk that sat here; the manager has settled it. The
   suffix form is final, and the enqueue glob stays `feature-*.md`.)*
2. **The stub-`PATH` pull test is the most fragile thing here.** Faking `ssh` and `rsync` on
   `PATH` is legitimate and offline, but it tests the script's *shape* (which names it captured,
   what it archived, what it never passed), not real rsync semantics. It would not catch a
   `--files-from` quoting bug against a real droplet. Mitigation: after the first real 09:00 run,
   read `~/Library/Logs/secretary-triage.log` and confirm the archived names match the pulled
   ones. If the test proves flakier than it is worth, **say so and delete it** — do not weaken it
   into something that passes on the bug.
3. **The plan header is a soft contract an LLM must keep** (the manager accepted this knowingly).
   If the triage agent omits `reports:` on a plan whose report was owner-reported, edge 11 fires
   and opens a **spurious extra card** carrying the raw report. It is noisy, not lossy, it is
   logged, and it is one click to delete. The alternative (a schema-validating post-step) is a
   bigger mechanism and its own card.
4. **`--files-from` with a remote source.** `rsync --files-from=- "$REMOTE:$dir/" "$dest/"` is the
   right shape and rsync supports it, but the exact quoting of a remote source with `--files-from`
   is the kind of thing that works on the first try or costs an hour. If it fights back, the
   fallback is to pass the explicit file list as multiple remote paths — **never** to fall back to
   `*.md` + a blind archive, which is the bug being fixed.
5. **The residual duplicate window the scope admits, restated honestly:** if the POST's ack is lost
   **and** the card is trashed or purged before the next drain tick, `snapshot()` cannot see it
   (`Board.snapshot()` skips trashed cards; `purge_card` removes them entirely) and the card is re-created
   once. Both halves must happen inside one 5-minute window, on a card he has barely seen. No
   mechanism is planned for it, deliberately.
6. **Board API drift.** The ingest is coded against `POST /api/card` → `{id, kind}`,
   `GET /api/card/{id}` → `abs_dir`, `GET /api/board` → `cards[].description`. All three are
   verified at this SHA. `AI Coding-kanban/` is a **vendored system folder that an update replaces
   wholesale** — if a future board update changes those shapes, the drain fails **loudly** (a 4xx,
   or a missing field) and the queue is retained, which is the correct failure. It does not
   silently drop.
