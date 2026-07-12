# Self-Learning — Final Steps (what only YOU can do)

> **Status: BUILT, DEPLOYED, LIVE — but not yet verified in the chat.**
> The code is in production (droplet restarted 2026-07-12, five skills loaded incl. `feedback`).
> Everything below is what a human has to do, because it needs your WhatsApp, your API key, or
> your judgement. No code changes are pending.

**Context for a future agent:** this was built in Claude Code session
`afe44b80-14f1-4262-95d6-be6320f6d7ff`. Commits: `14e9c17` (the feature), `4a8c92d` (deploy +
plan archived), `086a752` (docs). The full design lives in
`Shipped Features/2026-07-12 - self-learning.md`; the taxonomy is in `ARCHITECTURE.md`
("Self-learning") and `secretary/1. Orchestrator/ORCHESTRATOR.md`.

---

## What the system does (one paragraph)

The secretary now writes **failure reports about itself** into `secretary/improvements/` on the
droplet. You pull them to the Mac and a coding agent turns each into an implementation plan.
A malfunction is **exactly three things**: a **code error**, a **soft landing of an uncompleted
task** (declared by the skill via `ctx.sendFailure` — 29 call sites), and **you telling it that
it made a mistake** (`@secretary you made a mistake here` → the new `feedback` skill).
Everything else it says is **guidance** ("which task did you mean?", "your list is empty") and
files nothing.

---

## STEP 1 — Verify it live in WhatsApp  ⬅ do this first

Nothing here needs a developer. Send five messages and read the replies. Ask Claude Code to
tail the droplet logs while you do it (`ssh secretaria-droplet 'docker logs -f --tail 30 secretary'`)
and it can tell you exactly what routed where.

| # | Send this | Expected |
|---|---|---|
| 1 | **Reply to a past secretary message** with `@secretary you made a mistake here, I said 5pm` | *"Noted — logged as a mistake to investigate: …"*. It must **never** claim to have fixed it. |
| 2 | `@secretary you scheduled that at the wrong time` | Must route to **`feedback`** and file a report. **Must NOT create or move any event.** ⚠️ **This is the riskiest test — see Step 2.** |
| 3 | `@secretary you got the time wrong, move it to 5pm` | **Both**: the "noted" confirmation **and** the event actually moves (router returns `["feedback","calendar_action"]`). |
| 4 | `@secretary book me a flight to Rio` | The "I didn't understand" reply **and** an `unrouted` report (you decided these keep filing — they're the missing-capability signal). |
| 5 | `@secretary transcribe` **without** replying to an audio | The "reply to the audio" guidance and **NO report**. This is the negative test for the whole taxonomy. |
| 6 | `@secretary you made a mistake` (vague, nothing quoted) | One follow-up question. Answer it → *"added that to the report"*. Or **ignore it** — the report was already written **before** the question was asked, so nothing is lost. |

**Then confirm the reports actually landed:**
```bash
ssh secretaria-droplet 'ls -la /opt/secretary/improvements/'
ssh secretaria-droplet 'cat /opt/secretary/improvements/*.md | head -60'
```
Check the report has: your note, the **quoted offending message**, the transcript, the logs —
and that **no API key appears anywhere** (redaction). Expect `Source: OWNER-REPORTED`.

> **If test 2 misroutes** (it schedules something instead of filing a bug) — that's the one
> known unverified risk. The fix is prompt-only: the "COMPLAINTS ARE NOT COMMANDS" rule in
> `secretary/1. Orchestrator/router/prompt.js` and the `NOT for…` clauses in
> `secretary/2. Skills/5. Feedback/skill.js`. Tell Claude Code and it will tighten them.

---

## STEP 2 — Run the router fixture (needs YOUR API key)

**This never ran.** There is no `ANTHROPIC_API_KEY` on the Mac, and I refused to pull the
production key out of the container to run a test. It is the automated version of test 2 above:
it calls the **live router** and asserts that a *complaint* is **filed**, not **executed**.

```bash
cd "/Users/marceloabritta/Library/CloudStorage/GoogleDrive-marceloabritta@gmail.com/My Drive/Claude Projects/Personal Whatsapp AI"
ANTHROPIC_API_KEY=sk-ant-… node scripts/router-selftest.mjs
```
Costs a few cents. **Re-run it after any edit to `router/prompt.js` or to a skill manifest** —
every protection there is a prompt, and prompts regress silently.

*(Optional, permanent: put the key in a local `.env` / your shell profile so this and other
local tests just work.)*

---

## STEP 3 — Learn the loop (the payoff)

Reports pile up on the droplet. Pull them and triage whenever you feel like it — nothing is
automatic or scheduled, by design.

```bash
./scripts/self-learning-pull.sh     # droplet -> Improvements/inbox/ (and archives them remotely)
```
Then in Claude Code:
```
/triage-failures
```
It reads each report, investigates the codebase, writes a plan to `Improvements/<date>-<slug>.md`,
moves the raw report to `Improvements/_reports/`, and commits. **It does not push and does not
deploy** — you review, then ship.

Owner-reported files are triaged **first** and treated as ground truth. The "Auto-analysis"
section in a report is a cheap model's *guess* — the triage prompt is told to discard it freely.

---

## STEP 4 — The one habit that makes this work

**Report mistakes the moment you see them, and prefer replying to the wrong message.**

The other five triggers only fire when the code *knows* it failed. The failures that actually
annoy you — the wrong time, the false positive, the confidently wrong answer — crash nothing and
look like success. **You are the only detector.** A note you don't send is a bug that does not
exist as far as the system is concerned.

Replying to the offending message is worth the extra tap: it hands the engineer the secretary's
exact bad output. A bare note still works.

---

## What you do NOT need to do

- **No new env var, no API key, no OAuth scope, no dependency.** The feature adds none.
- **No `.gitignore` work** — `secretary/improvements/*.md` is already ignored and verified on the
  droplet (this was load-bearing: `/opt/secretary` symlinks *into* the production git tree).
- **No cron.** Triage is deliberately manual; an unattended agent with commit access was judged
  the riskiest part of the design and buys nothing until capture is proven.

---

## Open decisions you may want to revisit later

1. **Guidance stays silent.** "Reply to the audio", "which task did you mean?", "your list is
   empty" file nothing. If you later decide a *needSignal* message ("reply to the invite to
   cancel") is really a capability gap worth learning from, it's a one-line change per call site
   (`ctx.send` → `ctx.sendFailure`).
2. **Owner reports never dedupe and never hit the normal hourly cap.** Deliberate: a human can't
   loop, and a silently dropped note is the worst failure this system could have. There's still a
   ~10/hour disk backstop, and if it ever rejects a note the secretary **tells you** instead of
   confirming a lie.
3. **`unrouted` / `noAction` are filed** (your call, 2026-07-12). If they turn out to be noise,
   drop the `fireCapture` in the `notUnderstood` branch of `server.js`.

---

## Health checks (any time)

```bash
# is it up, and did all five skills load?
ssh secretaria-droplet 'docker logs --tail 20 secretary | grep "available skills"'
#   expect: calendar_action, transcribe_audio, task_action, feature_request, feedback

# the offline test suite (capture invariants + the call-site lint)
node scripts/selflearning-selftest.mjs
```

The lint is the guard that keeps this honest: if anyone adds a skill and sends a failure reply
with plain `send()` instead of `ctx.sendFailure()`, the test run fails with the file and line.
