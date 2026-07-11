# Skill: `feature_request`

> **For humans — quick read.**
>
> Turn a passing "I should build…" thought into a real spec, from WhatsApp. You message
> yourself an idea; the secretary **interviews you** until the feature is clear, then hands
> back a **Markdown spec file** you can save and drop into your repo.
>
> **How it works:**
> 1. Start it: `@secretary I want a feature that lets me snooze a task to next week`.
> 2. The secretary **becomes stateful and starts talking** — it asks the fewest, sharpest
>    questions to fill the gaps (problem, the user's flow, edge cases). You answer
>    normally, **no re-tagging** needed.
> 3. When you're done, say so — `that's it`, `write it up`, `ship the doc` — and it
>    sends you a **`feature-<name>.md` document** on the chat to save.
>
> **Two things worth knowing:**
> - The **chat** follows your language (English + Portuguese hand-tuned; others
>   auto-translated), but the **document is always written in English** — it's meant for
>   your (English) codebase.
> - The clarify session stays open ~30 minutes and re-arms on each reply; if you wander
>   off it closes on its own. `nevermind`/`cancel` drops it.
>
> **No setup** — this skill needs no new API keys or OAuth scopes (just Anthropic +
> WhatsApp, which the secretary already has).

## What you'll see (the full conversation)

Every secretary message is prefixed with the language-aware header — `[Marcelo's AI Secretary]:`
in English, `[Secretaria IA do Marcelo]:` in Portuguese (from `headerFor(lang)`) — and a blank line.

### Spec a feature end-to-end

1. You: `@secretary I have a feature idea: let me snooze a task to next week`.
2. Secretary: `Got it — let's spec this. What problem does snoozing solve for you, and how
   would you start it (a command, a reply, a button)?`
3. You: `tasks I can't do yet clutter the list. I'd reply "snooze" to the task.`
4. Secretary: `Makes sense. Which snooze options — tomorrow, next week, a custom date? And
   should it come back at a set time?`
5. You: `tomorrow / next week / custom date. morning is fine.`
6. You: `that's it, write it up`.
7. Secretary sends a **document**: `feature-snooze-a-task.md` with the caption
   `Here's the spec for "Snooze a task". Save it and drop it into your repo. 📄`

The `.md` is written from the **user's point of view** — a Summary, the Problem, the
**User flow** as numbered steps, Actors, Data & services touched, and Edge cases / open
questions.

### Change your mind

At any point: `nevermind` / `cancel` → `Okay, dropped that feature request.` (nothing is
sent). If you go quiet, the session expires quietly after ~30 minutes.

### Em português (o idioma segue a conversa; o documento sai em inglês)

1. Você: `@secretary quero uma funcionalidade pra adiar uma tarefa pra semana que vem`.
2. Secretary: `Beleza — vamos detalhar. Que problema isso resolve, e como você começaria o
   fluxo?`
3. … (a conversa segue em português) … Você: `pode escrever` →
4. Secretary envia `feature-snooze-a-task.md` (o **documento em inglês**) com uma legenda em
   português.

## For AI / maintainers — detailed

Files: `skill.js` (dialogue logic + doc render + delivery), `prompt.js` (the clarify
prompt + `CLARIFY_SCHEMA`, the English doc prompt, `slugify`, and the localized
`reply(lang)` scaffolding-string map).

### Why this skill has a different shape

Calendar and Tasks are **slot-fillers** (chase a fixed set of fields, fire one action).
`feature_request` is a **free-form dialogue** with no fixed slot count. It keeps a
**running `draft`** in the session (updated + carried forward every turn via
`mergeDraft`) so the accumulated spec survives the 30-message transcript window and any
unrelated chatter.

### Contract & flow
- `manifest = { id: "feature_request", description }`, `run(ctx)` — discovered at boot.
  No `capabilities` export (this skill neither delegates nor is delegated to).
- **Dispatch:** a live session first (`ctx.session.skill === "feature_request"`,
  `stage:"clarifying"`) → `resumeClarify`; otherwise → `startFeatureRequest` (the router
  already chose the skill, so there is **no separate interpret/classify step**).
- **`clarifyTurn`** — one structured call per owner message (`CLARIFY_SCHEMA`) returning
  `{ status, draft, reply }`:
  - `status:"clarifying"` → persist the merged draft, re-arm the session (`intent:"spec"`,
    `stage:"clarifying"`, `awaitFrom:"owner"`, TTL 1800), send `reply` (the ack + next
    question, **generated in `ctx.lang`** by the model — it is not a fixed string).
  - `status:"finalize"` → `finalize`.
  - `status:"cancel"` → clear the session, send `reply().cancelled()`.
  The model is told to finalize **only** on an explicit done-signal (or a complete draft
  + confirm) and to keep question batches small.
- **`finalize`** → `generateDoc` (a second call, **plain prose, no schema**, system prompt
  hard-pinned to **English** + a fixed skeleton) → base64 the markdown →
  `ctx.evolution.sendMedia({ mediatype:"document", mimetype:"text/markdown", media,
  fileName:"feature-<slug>.md", caption })`. The caption carries the language-aware header
  (`headerFor(lang)`; media framing is the caller's job, like `sendText`). Session is cleared after send; a
  **render** failure keeps the session so the owner can retry the write without
  re-speccing, a **send** failure replies `reply().sendFailed()`.

### Localization
- **Scaffolding** strings (`thinkingError`, `firstFallback`, `continueFallback`,
  `cancelled`, `renderError`, `sendFailed`, `docCaption`) come from `reply(ctx.lang)`
  (en + pt); any other language is translated from `en` by the orchestrator's `send()`
  fallback.
- **The per-turn question** is generated in-language by the model (the language name is
  passed into the clarify system prompt), so `pt` comes back as pt directly.
- **The document body is the deliberate exception** — always English (`buildDocSystem`
  translates any non-English draft content, keeping proper nouns), because the artifact
  is destined for the English codebase. Only the caption localizes.
- Sessions persist `lang: ctx.lang` so every clarify turn answers in the flow's language.

### Delivery — `evolution.sendMedia` (the one shared change)
This skill added `sendMedia(number, { mediatype, mimetype, media, fileName, caption })`
to `1. Orchestrator/lib/evolution.js` (`POST /message/sendMedia/{instance}`, base64
`media`). Additive — `sendText`/`fetchHistory`/`getMediaBase64` are unchanged.

### Setup
None. No new env var, no OAuth scope. If media sending is ever unavailable on the running
Evolution image, `finalize` reports `reply().sendFailed()` and logs the HTTP error — the
draft/notes are already gone with the session, so re-run from `@secretary`.
