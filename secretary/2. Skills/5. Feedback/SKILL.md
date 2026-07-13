# Skill: `feedback`

> **For humans — quick read.**
>
> Tell the secretary it screwed up, and it files itself a bug report.
>
> The rest of the self-learning system only notices failures the *code* can see — a crash, an
> order it couldn't route, a skill that said "I couldn't". But the mistakes that actually
> annoy you are the silent ones: **a wrong answer delivered confidently**, an event on the
> wrong day, a false positive. Nothing throws. Nothing looks broken. The only detector is
> **you**, reading the message and thinking *that's wrong*.
>
> **How it works:**
> 1. **Reply to the message that was wrong** and say `@secretary you made a mistake here`.
>    (A bare note works too — `@secretary you got the timezone wrong on that event` — but
>    replying is better: it hands the engineer the exact bad output.)
> 2. The secretary writes a **failure report** to `secretary/improvements/` on the droplet,
>    with your note, the offending message, the conversation and its own recent logs.
> 3. It confirms in one line: *"Noted — logged as a mistake to investigate: …"*. It does
>    **not** claim to have fixed anything, because it hasn't.
> 4. Later, on the Mac, `/triage-failures` pulls the reports and turns each into an
>    implementation plan you review.
>
> **Three things worth knowing:**
> - **It files; it doesn't fix.** If you also want it fixed *now*, just say so —
>   `@secretary you got the time wrong, move it to 5pm` files the bug **and** moves the
>   event (the router returns both tasks).
> - **A vague note gets exactly one question back** ("which message was wrong, and what
>   should it have said?"). The report is written *before* it asks, so if you never answer,
>   nothing is lost.
> - **Your note is never silently dropped.** If it can't be saved, the secretary tells you
>   so instead of confirming.
>
> **No setup** — no new API key, no OAuth scope.

## What you'll see (the full conversation)

Every secretary message carries the language-aware header (`headerFor(lang)`) and a blank line.

### Report a mistake (the gold path — a reply)

1. Secretary (earlier): `Event created — "Call with Ana", tomorrow 6:00 PM.`
2. You *(replying to that message)*: `@secretary you made a mistake here, I said 5pm`
3. Secretary: `Noted — logged as a mistake to investigate: "event created at 6pm instead of
   the requested 5pm". It goes into the next improvement pass. I haven't changed anything yet.`

### Report it **and** fix it

1. You: `@secretary you got the time wrong, move it to 5pm`
2. The router returns `["feedback", "calendar_action"]` — you get the confirmation above **and**
   the event is actually rescheduled.

### A note too vague to act on

1. You: `@secretary you made a mistake`
2. Secretary: `Noted — I've logged that. To make it actionable: which message was wrong, and
   what should it have said? (Replying directly to the wrong message works best.)`
3. You: `the lunch you put on Friday, it was Thursday` → `Got it — added that to the report.`

   The report already exists by step 2 — your answer only **enriches** it. There is never a
   second question.

### Em português

1. Você: `@secretary você errou aqui, eu disse 17h` → `Anotado — registrei como um erro para
   investigar: "…". Vai entrar na próxima rodada de melhorias. Ainda não mudei nada.`

## For AI / maintainers — detailed

**`manifest.inputs: null`** — this skill declares **no inputs** for the orchestrator's merged
router+extractor call (`1. Orchestrator/lib/inputs.js`). It re-reads the conversation itself, so
there is nothing to pre-extract and it is never handed a `ctx.info` payload. (This matters on a
dual-intent turn, `["feedback","calendar_action"]`: feedback is `tasks[0]`, so no payload is
handed to anyone, and `calendar_action` correctly falls back to its own extraction call.)

Files: `skill.js` (evidence + capture + the one question), `prompt.js` (the extraction prompt,
`buildFeedbackSchema`, and the localized `reply(lang)` map). The report writing itself lives in
`1. Orchestrator/lib/selflearning.js` — this skill is a thin front door onto it.

### Why this skill exists at all

Failure capture is orchestrator **infrastructure** (`lib/selflearning.js`), deliberately *not*
a skill: every loaded skill is auto-appended to `CATALOG`, which is the router's menu, and a
skill the router must never pick is a misroute hazard with no upside.

The **one** exception is this: an owner's complaint arrives as a tagged order, so the router is
the only thing that can catch it. Hence a skill — the thinnest one in the repo.

### Contract & flow
- `manifest = { id: "feedback", description }`, `run(ctx)` — discovered at boot. No
  `capabilities` export (it neither delegates nor is delegated to).
- **Dispatch:** a live session first (`ctx.session.skill === "feedback"`, `stage:"clarifying"`)
  → `resumeFeedback`; otherwise → `startFeedback`.
- **Evidence:** `ctx.quoted.text` is the offending message when he replied to one;
  `isOwnMessage(quoted.text)` (`lib/identity.js`) confirms it really is secretary output —
  they share a WhatsApp account, so the header is the only signal. The report says which.
- **One structured call** (`buildFeedbackSchema(ctx.catalog)`) → `{ title, what_went_wrong,
  expected, suspected_skill, enough_context }`, all English. It restates **his claim** — it
  does *not* theorize about the cause. (The cause guess is the separate, clearly-labelled,
  discardable "Auto-analysis" section that `selflearning.js` adds.)
  - `suspected_skill` is a **nullable enum → `anyOf`**, not a type-union + `enum`: the
    structured-output validator rejects the latter (same as `list_mode` in
    `1. Calendar Actions/prompt.js`). Its values come from `ctx.catalog`, so a new skill is a
    valid answer the day it ships.
- **`captureFailure(ctx, { phase: "reported", … })`** → writes the report, returns its path.
- **Confirm** via `ctx.send` — one line, always "filed", never "fixed".

### WRITE FIRST, ASK SECOND — the ordering is the design
`server.js` clears any open session on the next tagged order. So the obvious flow (ask → wait →
file on his answer) **loses the complaint**: he reports a bug, gets asked "which message?", is
distracted, types `@secretary schedule lunch` — and the session, with his bug report, is gone.
Silently. He'd stop reporting after the second one that went nowhere.

So the report is written **before** the question is asked. `captureFailure` returns the path,
the skill parks it in the session, and his answer **appends** an `## Owner's follow-up` section
to a file that already exists (`appendToReport`). If the append fails, a fresh report is filed
and linked back. **The answer is never load-bearing for a report existing at all.**

### Why `reported` is exempt from dedupe and the hourly cap
Both exist to survive a *crash loop* — a machine emitting the same stack hundreds of times a
minute. A human typing on a phone cannot loop, and two notes 30 seconds apart are two distinct
complaints. `reported` keeps only a generous ~10/hour disk backstop, and if that ever rejects a
note the skill **says so** rather than confirming a lie (`reply().logFailed()`).

### Router disambiguation (the real hazard)
The router will want to send *"you scheduled that at the wrong time"* to `calendar_action` —
which would **execute the complaint as a fresh order**. Three mitigations, all needed: the
`NOT for…` clauses in the manifest above; the "COMPLAINTS ARE NOT COMMANDS" rule in
`router/prompt.js`; and `scripts/router-selftest.mjs`, which pins exactly that sentence.
Run the fixture after any edit to the router prompt or a skill manifest.

### Localization
`reply(lang)` carries en + pt; any other language is translated from the `en` copy by the
orchestrator's `send()` fallback. The **report is always English** — it's for the codebase.
The confirmation strings deliberately match none of the soft-failure patterns in
`selflearning.js`, so they can't re-trigger capture through the `ctx.send` scanner (and
`_turn.captured` would stop them anyway).

### Setup
None.
