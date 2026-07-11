# Feature Requests ("Spec-a-Feature") — Implementation Plan

> **Freshness note (2026-07-11).** Aligned to the current codebase: structured
> outputs (`output_config.format` + JSON Schemas via `jsonFormat`/`readReply`), the
> stateful session/continuation layer (`ctx.sessions` + `ctx.session`, `awaitFrom`,
> sessions persist `lang`), the **live multilingual layer** (`ctx.lang` from the
> router; `ctx.send` bound to it; per-skill `{ en, pt }` maps + `localizeDate` — NOT
> a central i18n catalog), the `SKILL.md` doc convention, and skill folder numbering
> (next is `4. Feature Requests/`). Ships `en` + `pt` from day one.

## The brief (from the owner)

> The owner is running his affairs and comes upon a feature he'd like to build. He
> starts a feature request from `@brain I have a new feature idea …`. **The system
> becomes stateful and starts talking** — clarifying the feature until the owner says
> it's done. The end result is an **`.md` file describing the feature flow from the
> user's point of view**, delivered on the chat so the owner can save it and later
> drop it into his production environment to work on.

## Goal (restated as behavior)

A new skill `feature_request` that runs an **open-ended clarifying conversation** and
ends by producing and sending a **Markdown feature spec** as a real, saveable file.

- Start: `@brain I want a feature that lets me snooze a task to next week`
- Brain replies with an acknowledgement + the **most valuable clarifying questions**.
- Owner answers (no re-tag needed — a live session continues the thread).
- Loop until the owner signals **done** ("that's it", "ship the doc", "write it up").
- Brain renders `feature-<slug>.md` and **sends it as a document attachment** with a
  one-line caption; the session closes.

## What makes this skill different from Calendar/Tasks

Calendar and Tasks are **slot-fillers**: a fixed set of fields (start, participants,
due…) is chased, then one action fires. This skill is a **free-form dialogue** with no
fixed slot count — it must decide, each turn, whether the spec is clear enough or
another question is worth asking, and it must *accumulate understanding* across turns.

Design consequence: we keep a **running structured draft** in the session (updated
every turn) rather than re-deriving the whole spec from the transcript at the end. The
transcript is a 30-message window that may include unrelated chatter; a persisted draft
is robust to that and lets the model ask targeted questions about the remaining gaps.

## Decisions (confirmed with the owner)

| Decision | Choice |
|---|---|
| **Delivery** | **Real file attachment.** Send an actual `feature-<slug>.md` document (Evolution `/message/sendMedia`, base64) the owner can tap-to-save. Requires a new `sendMedia` on the Evolution client. |
| **Doc language** | **Always English.** The artifact is destined for his (English) codebase, so the `.md` is normalized to English regardless of the chat language. The **conversation** still follows `ctx.lang` — only the generated document is pinned to English. |
| **Who clarifies** | **Owner only** (`awaitFrom: "owner"`). A note-to-self flow; the other party in a chat is never a continuation here. |
| **Skill id** | `feature_request`. |

## New skill — `2. Skills/4. Feature Requests/`

`skill.js` (dialogue logic + doc render) · `prompt.js` (prompt builders + JSON Schemas
+ `reply(lang)` `{ en, pt }` string map) · `SKILL.md` (human doc). Auto-discovered at
boot by `loadSkills()` — no orchestrator edit needed for discovery.

### `manifest`

```js
export const manifest = {
  id: "feature_request",
  description:
    "capture and spec out a NEW FEATURE / product idea the owner wants to build: " +
    "hold a clarifying conversation, then produce a Markdown spec document. " +
    "Use for 'I have a feature idea', 'spec this out', 'write up a feature request' — " +
    "NOT for adding a to-do (task_action) or scheduling (calendar_action).",
};
```

> **Router disambiguation.** The description must fence this off from `task_action`
> ("add a task/todo") and `calendar_action` ("schedule/meeting"). The trigger vocabulary
> is *feature / idea / spec / build / write it up* — a product-shaping intent, not a
> single dated action. Add one PT and one EN example to the router's mental model by
> phrasing the description around those verbs.

### `run(ctx)` dispatch

```
run(ctx):
  # CONTINUATION owned by this skill (set by orchestrator when a session is live)
  if session?.skill === "feature_request" && session.stage === "clarifying":
      return resumeClarify(ctx, session)

  # FRESH START: @brain <feature idea>. No interpret/classify step — the tag already
  # told us the intent; seed the draft from the idea + transcript and ask.
  return startFeatureRequest(ctx)
```

There is **no separate `interpret` classifier** on the fresh path (unlike Tasks): the
router already chose this skill, so the first turn goes straight into the clarify step
with an empty draft.

### One structured call per turn — `clarifyTurn`

Every turn (fresh or continuation) makes a single structured call that (a) folds the
latest owner message into the running draft, (b) decides whether to keep clarifying or
finalize, and (c) writes the next conversational reply **in `ctx.lang`**.

```js
export const CLARIFY_SCHEMA = {
  type: "object", additionalProperties: false,
  required: ["status", "draft", "reply"],
  properties: {
    // clarifying = ask more; finalize = owner signalled done -> render the doc;
    // cancel = owner abandoned it.
    status: { type: "string", enum: ["clarifying", "finalize", "cancel"] },
    // The running spec, re-emitted (updated) every turn. All fields nullable/arrays
    // so an early turn can leave gaps.
    draft: {
      type: "object", additionalProperties: false,
      required: ["title","one_liner","problem","trigger","actors","steps",
                 "data_touched","edge_cases","open_questions"],
      properties: {
        title:        { type: ["string","null"] },   // short feature name -> filename slug
        one_liner:    { type: ["string","null"] },    // one-sentence summary
        problem:      { type: ["string","null"] },    // the pain / why
        trigger:      { type: ["string","null"] },    // how the user starts the flow
        actors:       { type: "array", items: { type: "string" } },
        steps:        { type: "array", items: { type: "string" } },  // user-POV flow
        data_touched: { type: ["string","null"] },    // data/services involved
        edge_cases:   { type: "array", items: { type: "string" } },
        open_questions:{ type: "array", items: { type: "string" } },  // still unresolved
      },
    },
    // The WhatsApp message to send this turn when status="clarifying" (ack + the next
    // question(s)), written in ctx.lang. Ignored on finalize/cancel.
    reply: { type: ["string","null"] },
  },
};
```

The clarify system prompt (in `prompt.js`, English/internal) instructs the model to:
- Behave like a **product manager interviewing the owner**: ask the *fewest, highest-
  value* questions, one small batch at a time — never interrogate.
- **Fold** the new answer into `draft`; carry forward everything already known.
- Set `status:"finalize"` **only** when the owner explicitly signals completion
  ("that's it", "done", "write the doc", "ship it") **or** the draft is complete and the
  owner confirms — err toward one more question over finalizing prematurely.
- Set `status:"cancel"` on "forget it / nevermind / cancel".
- Write `reply` **in the target language** (passed in as `ctx.lang`) — this is the one
  user-facing string the model authors directly, so it must be in-language (for `pt` the
  `send()` fallback does NOT translate; the model must produce pt itself).

`clarifyTurn(ctx, priorDraft)` passes the transcript, the latest `order`, `priorDraft`
(JSON), `nowStr`, and the target `ctx.lang` into the call; returns the parsed object via
the copied `jsonFormat`/`readReply` helpers.

### Start + resume

```
startFeatureRequest(ctx):
  out = clarifyTurn(ctx, EMPTY_DRAFT)          # seeds draft from ctx.order + transcript
  if !out: send(reply(lang).thinkingError()); return
  if out.status === "finalize": return finalize(ctx, out.draft)   # rare: fully-specced in one shot
  sessions.set(remoteJid, {
    skill:"feature_request", intent:"spec", stage:"clarifying",
    awaitFrom:"owner", lang: ctx.lang, draft: out.draft,
  }, 1800)                                       # 30-min window, re-armed each turn
  send(number, out.reply || reply(lang).firstFallback())

resumeClarify(ctx, session):                     # runs on EVERY owner msg while open
  out = clarifyTurn(ctx, session.draft)
  if !out: return                                # ignore on transient error (stay open)
  if out.status === "cancel":
      sessions.clear(remoteJid); send(number, reply(lang).cancelled()); return
  if out.status === "finalize":
      return finalize(ctx, out.draft)            # render + send the doc, then clear
  # still clarifying: persist the updated draft, re-arm, ask the next question
  sessions.set(remoteJid, { ...session, draft: out.draft, lang: ctx.lang }, 1800)
  send(number, out.reply || reply(lang).continueFallback())
```

Note `resumeClarify` runs on **every** owner message while the session is open — the
model itself decides "is this an answer to my question, unrelated chatter, or a
done-signal?" via `status`. Unlike Tasks' amend window there is no silent-on-chatter
branch that returns nothing; instead the model returns `clarifying` with a gentle nudge
or, if the message is truly unrelated, a short reply. (If we want strict silence on
chatter we can add a `status:"ignore"` enum that returns without sending — kept out of
v1 for simplicity.)

### Finalize — render the doc (English) and send it as a file

```
finalize(ctx, draft):
  md = await generateDoc(ctx, draft)             # dedicated LLM call -> full Markdown, ENGLISH
  if !md: send(reply(lang).renderError()); sessions.clear(); return
  slug = slugify(draft.title || "feature")       # e.g. "snooze-a-task"
  fileName = `feature-${slug}.md`
  base64 = Buffer.from(md, "utf8").toString("base64")
  ok = await ctx.evolution.sendMedia(number, {
    mediatype: "document", mimetype: "text/markdown",
    media: base64, fileName, caption: `${HEADER}\n\n${reply(lang).docCaption({title})}`,
  })
  if !ok: send(number, reply(lang).sendFailed())  # fallback: nothing saved
  sessions.clear(remoteJid)
```

- **`generateDoc(ctx, draft)`** — a second, focused call whose system prompt is pinned
  to **English output** and a fixed document skeleton (below). Better prose than a
  string template, and consistent structure. Uses the same `jsonFormat`/`readReply`? No
  — this one returns **prose**, so it's a plain `messages.create` and we read the text
  blocks directly (no schema). Input: the full `draft` JSON.

**Document skeleton (from the user's POV, per the brief):**

```markdown
# <Feature title>

## Summary
<one-liner>

## Problem / motivation
<problem>

## User flow (the point of view of the user)
1. <trigger — how the user starts>
2. <step>
3. …

## Actors
- <actor>

## Data & services touched
<data_touched>

## Edge cases & open questions
- <edge_case>
- **Open:** <open_question>

---
*Drafted by @brain on WhatsApp. Save to the repo and refine.*
```

- The caption is sent **with** the header, so it reads like a normal brain reply; the
  document itself is the attachment. (Caption is localized via `reply(lang)`; the
  document body is English.)
- `slugify`: lowercase, strip accents, non-alnum → `-`, collapse/trim; fallback
  `feature`. No date needed for uniqueness; add `-<yyyymmdd>` only if we later want it.

## Evolution client — new `sendMedia` (the one plumbing change)

`brain/1. Orchestrator/lib/evolution.js` currently exposes `sendText`, `fetchHistory`,
`getMediaBase64`. Add:

```js
// Sends a media message (document/image/…) as base64. For the feature-spec .md we use
// mediatype:"document", mimetype:"text/markdown". Caller frames the header in `caption`
// (like sendText, framing is the caller's job). Returns res.ok.
async function sendMedia(number, { mediatype, mimetype, media, fileName, caption }) {
  const res = await fetch(`${base}/message/sendMedia/${instance}`, {
    method: "POST", headers,
    body: JSON.stringify({ number, mediatype, mimetype, media, fileName, caption }),
  });
  if (!res.ok) console.error("sendMedia failed", res.status, await res.text());
  return res.ok;
}
// …expose it:
return { sendText, sendMedia, fetchHistory, getMediaBase64 };
```

> **Verify against the running Evolution version** — the v2 `/message/sendMedia`
> endpoint takes `{ number, mediatype, mimetype, media (base64 or URL), fileName,
> caption }`. Confirm the exact field names on the deployed image (a 5-min curl against
> the droplet) before shipping; adjust if the instance expects `options`/`mediaMessage`
> nesting. This is the only external-contract risk in the plan.

The orchestrator already puts `evolution` on `ctx`, so the skill calls
`ctx.evolution.sendMedia(...)` — no server.js change for delivery.

## No orchestrator changes required

- **Discovery**: `loadSkills()` picks the new folder up automatically.
- **Continuation routing**: the generic session layer already dispatches a live
  `feature_request` session's follow-ups back to this skill (`awaitFrom:"owner"`,
  `session.skill` = `feature_request`). Nothing to add.
- **Capability registry**: not used — this skill neither delegates to nor is called by
  another skill. (No `capabilities` export.)
- The only lib touch is `sendMedia` in `evolution.js`.

## Strings & multi-lingual (ship en + pt)

Follow the localization convention in `../ARCHITECTURE.md`. Two kinds of user-facing
text:

1. **Fixed scaffolding strings** → `reply(lang)` `{ en, pt }` map in `prompt.js`:
   `thinkingError`, `firstFallback`, `continueFallback`, `cancelled`, `renderError`,
   `sendFailed`, `docCaption({title})`. Each ships `en` **and** `pt`; any other language
   is translated from `en` by the orchestrator's `send()` fallback. Header never
   translated.
2. **The turn `reply`** (ack + next question) is **generated by the model in `ctx.lang`**
   — pass the language into the clarify prompt so `pt` comes back as pt directly (the
   `send()` fallback won't translate a maintained language). This is the same principle
   as Calendar/Tasks generating in-language, just for dynamic conversational text.
3. **The document body** is **always English** (a deliberate exception to `ctx.lang`,
   per the owner's decision) — `generateDoc`'s system prompt hard-codes English output.
   The **caption** around it stays localized.
4. **Sessions persist `lang: ctx.lang`** so every clarify turn answers in the language
   the flow started in even though continuations bypass the router.

## Files touched

- **New code:** `2. Skills/4. Feature Requests/skill.js`, `prompt.js`.
- **Edit code:** `1. Orchestrator/lib/evolution.js` (add `sendMedia`).
- **New/edit docs:** see **Documentation revisions** below.
- **Config:** none (no new env var, no new OAuth scope — pure Anthropic + Evolution).
- **Setup:** none.

## Build order

1. **`sendMedia`** on the Evolution client + a one-off curl against the droplet to
   confirm the `/message/sendMedia` field names on the deployed image. Land first — it's
   the only external-contract unknown, independently testable.
2. **`prompt.js`**: `CLARIFY_SCHEMA`, the clarify system/user prompt builders, the
   `generateDoc` English system prompt + skeleton, `reply(lang)` en+pt map, `slugify`.
   Copy `jsonFormat`/`readReply`/`parseJsonReply` from Tasks.
3. **`skill.js`**: `manifest`, `run` dispatch, `clarifyTurn`, `startFeatureRequest`,
   `resumeClarify`, `finalize` + `generateDoc` call + base64 + `sendMedia`.
4. **First clarify turn + session open** working end-to-end (talk, no doc yet).
5. **Finalize path**: render English doc, deliver as `feature-<slug>.md` attachment,
   clear session.
6. **en + pt** `reply(lang)` map; verify a pt conversation produces pt questions + an
   English doc with a pt caption.
7. **All doc revisions** (below).
8. **Deploy** per the deploy workflow (git pull + restart on the droplet) — production
   writes need an explicit ask first.

## Documentation revisions (all doc files)

Every doc that describes skills, the flow, the ctx/Evolution surface, or the changelog
must be updated. **Conventions to propagate:** the `feature_request` skill (open-ended
clarify → English `.md` attachment), and the new `evolution.sendMedia` capability.

1. **`ARCHITECTURE.md`** (root)
   - **Flow section:** add a step `skill → Evolution (send document)` — the
     `/message/sendMedia` call with a base64 `text/markdown` document; note the doc body
     is English by design while the conversation follows `ctx.lang`.
   - **"Adding a skill":** note this skill needs no capability registry / no new env —
     an example of a pure conversational skill.
   - **Localization convention:** add the one exception — a generated *artifact* may be
     pinned to a fixed language (English here) even though replies follow `ctx.lang`.
2. **`PROJECT_LOG.md`** (root — registry + changelog)
   - **Changelog:** dated entry — "Feature-request skill (`feature_request`): stateful
     clarify conversation → English Markdown spec delivered as a WhatsApp document;
     added `evolution.sendMedia`."
   - **Docs registry:** list the new `2. Skills/4. Feature Requests/SKILL.md`.
   - **"Next up":** reconcile.
3. **`README.md`** (root — public)
   - **"Skills (today)":** add a `feature_request` bullet.
   - **ASCII flow diagram:** add `feature_request → .md document` to the skills box.
   - **Roadmap:** note it shipped.
4. **`brain/README.md`** (developer)
   - **Structure tree:** add `4. Feature Requests/` (`skill.js`, `prompt.js`, `SKILL.md`).
   - **Evolution client surface:** append `sendMedia` next to `sendText`.
5. **`brain/1. Orchestrator/ORCHESTRATOR.md`** (maintainer)
   - **Evolution client section:** document `sendMedia({ mediatype, mimetype, media,
     fileName, caption })` and that the caller frames the header in `caption`.
6. **`brain/2. Skills/4. Feature Requests/SKILL.md`** (NEW — every skill ships one)
   - Human quick-read: the one action (spec a feature); the clarify-until-done loop; the
     English-doc/localized-chat split; how the file arrives (attachment); a worked EN and
     PT example conversation; the 30-min session window.

## Notes / risks

- **`/message/sendMedia` contract** — the single external unknown; confirm field names
  on the deployed Evolution image before shipping (build step 1). Fall back to a plain
  `sendText` dump of the Markdown only if media sending proves unavailable.
- **Knowing when to stop** — the model must not finalize prematurely nor interrogate
  forever. Prompt it to finalize only on an explicit done-signal (or a complete draft +
  confirm), and to keep question batches small. The 30-min TTL auto-closes an abandoned
  session.
- **Draft drift** — because the draft is re-emitted every turn, the prompt must
  *carry forward* known fields, not blank them; assert this in the system prompt and keep
  `priorDraft` in the context each turn.
- **Long doc in one message** — a document attachment sidesteps WhatsApp text reflow and
  length limits that a plain-text dump would hit.
- **English doc, non-English chat** — deliberate: `generateDoc` is hard-pinned to
  English; only the caption + questions localize. Called out in `SKILL.md` so it's not a
  surprise.
