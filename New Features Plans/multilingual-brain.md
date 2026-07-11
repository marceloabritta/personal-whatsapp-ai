# Multi-lingual Brain — Implementation Plan

## Goal

The brain must detect the language the owner is writing in on a given chat and
reply in that **same language**, system-wide across every feature.

Example: a reply that would say "Invite sent" must come back as "Convite enviado"
when the conversation is in Portuguese.

## Decisions (agreed)

- **Language is decided centrally, prose lives with each skill.**
  - The **language** for a message is decided in one place (the router detects it,
    it rides in `ctx.lang`, and the `send()` choke point enforces it).
  - The **prose** stays with the skill that owns it — each skill keeps its own
    user-facing strings in its `prompt.js`, keyed by language. No shared/central
    catalog module.
- **Per-skill EN + PT maps.** Each skill's `prompt.js` holds an `en` + `pt`
  version of its strings (same pattern the Audio skill already uses with `MSG`).
  This keeps Portuguese **deterministic and tuned** for the common case, so the
  carefully-formatted calendar messages (bullet lists, blank lines, event link,
  emails, times, durations) are never reflowed by a model.
- **LLM-translation fallback for the long tail.** For any language you did *not*
  write a map for (not `en`/`pt`), the single `send()` choke point translates the
  **body** with a cheap model. So the system still works in any language the owner
  happens to type, without maintaining a map for it.
- **Maintained languages: English (canonical/source) + Portuguese (PT-BR).**
  Anything else is handled by the fallback.

### Why not a central `i18n.js` catalog

Considered and rejected. A central catalog gives a single-file audit view, but it
pulls each skill's prose out of the skill. We chose **locality over the single
view**: prose stays next to the logic that emits it (in the skill's `prompt.js`),
exactly like the Audio skill's `MSG` today. The trade-off we accept: adding a
third maintained language edits every skill's map. Since the owner realistically
uses PT + EN, that cost is low.

## Reconciliation with shipped code (checked 2026-07-11)

This plan predates two things that shipped since: the calendar skill's stateful
Phase-C flows and the **enforced-JSON response model** (structured outputs). Neither
invalidates the design — they *reinforce* it — but they change specifics below.

- **Enforced JSON response model ("authoritative" outputs) — calendar only, not
  system-wide.** The 4 calendar LLM calls now use `output_config.format` with JSON
  Schemas (`CAL_SCHEMA`, `CONFIRM_SCHEMA`, `REVIEW_SCHEMA`, `RESOLVE_SCHEMA` in the
  calendar `prompt.js`), with `parseJsonReply` + a `stop_reason:"refusal"` guard as
  fallback. **Key point for us:** those schemas are *extraction/decision* shapes —
  **none carries user-facing prose.** The model returns data; code renders the text.
  That is exactly the boundary this plan localizes at, so the core approach is
  unaffected and, if anything, confirmed.
- **The router is the one LLM call still hand-parsing.** `router/prompt.js` still
  asks for `{"tasks","reason"}` and `router/router.js` regex-extracts it — it did
  **not** adopt structured outputs. So adding `lang` there is now also the natural
  moment to bring the router into the enforced-response convention (see step 1).
- **The calendar skill is much larger.** Phase C added confirm-first create, a
  modify path, and a stateful missing-info chase (`awaitFrom:"any"`). The old "~8
  strings" is now **~14**, several with English-grammar assembly and pluralization
  (see below) — more work than the original plan implied, and more reason to use
  per-language render functions rather than string tables.
- **The convention we chose is already half-documented.** `PROJECT_LOG.md` §6
  already states *"prompt/text lives in the skill's `prompt.js`… to change wording
  or language, edit only its `prompt.js`."* The Audio skill obeys this; **the
  calendar skill currently violates it** (its prose is inline in `skill.js`). So the
  calendar conversion here also closes a pre-existing doc-vs-code gap — we're
  *extending* an existing convention, not inventing one.
- **The session-record shape** is documented as a JSON block in
  `brain/1. Orchestrator/ORCHESTRATOR.md` ("Sessions — shape & skill contract") — the
  right home for the new `lang` field.

## Key facts about the current architecture

- **Every outgoing message funnels through one choke point:**
  `ctx.send(number, text)` → `send()` in `1. Orchestrator/server.js`, which
  prepends the constant `[AI Brain]:` header. Only the **body** is localized.
- **The header must stay constant** — the orchestrator uses
  `text.startsWith(HEADER)` to ignore the brain's own messages
  (`server.js`, isBrainMsg). Never translate the header.
- **Language detection is nearly free:** the router already makes an LLM call and
  returns JSON on every fresh command — add a `lang` field to its output.
  Continuations bypass the router, so persist `lang` in the Redis session and
  reuse it.

### Where user-facing text lives today

- Inline template strings in `2. Skills/1. Calendar Actions/skill.js` — now **~14**,
  many interpolated (`title` / `emails` / `when` / duration / event link) with
  carefully tuned formatting. Inline in the logic (violates `PROJECT_LOG.md` §6).
  They fall in three buckets that shape the localization work:
  - **Plain interpolation** — the create-confirm block, "Done! Invite created and
    sent…" / "That event already exists…", the delete-confirm block, "Cancelled
    …", "Okay, I'll keep…"/"Okay, I won't create…", and the several error strings
    ("I hit an error while thinking", "…checking the calendar", "…failed to create
    it in Google", "…failed to cancel it in Google", "I couldn't find a matching
    event…", the "To cancel an event, reply…" hint, "I didn't identify a calendar
    action").
  - **English-grammar assembly** — `joinList` ("X, Y, and Z") inside `renderInquiry`,
    plus the "Ana, I'm missing your email" name-addressed special case. Grammar
    differs by language → each language needs its own render function, not a lookup.
  - **Pluralization** — `countNote` (`(N matching copies)`) and `dupNote`
    (`(removed N copies)`). Plural rules are language-specific → render per language.
- The `MSG` object + `formatTranscript` in
  `2. Skills/2. Audio transcriptions/prompt.js` (already isolated, single-language,
  and already compliant with the §6 convention).
- Inline error/help strings in `1. Orchestrator/server.js`
  ("I didn't understand…", router error, continuation error, skill error).
- Locale-bound date formatting: `toLocaleString("en-US", …)` in
  `Calendar Actions/skill.js` (`whenStr`) and `server.js` (`nowStr`).

---

## Implementation

### 1. Central language detection (near-zero cost)

- **Fresh commands:** extend the router to also return the conversation language.
  - In `router/prompt.js` add `"lang"` to the required JSON
    (`"lang": "pt" | "en" | <ISO code>`) with an instruction: *"detect the
    language OWNER is writing in from the order + recent conversation."*
  - In `router/router.js` read `parsed.lang`, default `"en"`, and return it
    alongside `tasks`. **No extra LLM call** — it piggybacks the existing router
    request.
  - **Adopt the enforced-response model here (the router is the last hand-parsed
    call).** Mirror the calendar pattern: define a `ROUTER_SCHEMA`
    (`{ tasks: string[], reason: string, lang: string }`, all `required`,
    `additionalProperties:false`) in `router/prompt.js` and pass it via
    `output_config.format` in `router.js`. Keep the existing regex parse as the
    `parseJsonReply`-style fallback + a `stop_reason:"refusal"` guard (default
    `lang:"en"`, `tasks:[]`), so detection degrades gracefully on a model swap.
    This both adds `lang` and closes the one remaining un-enforced JSON call — do it
    as one change, not two.
- **Continuations** (bypass the router): persist `lang` inside the Redis session
  when it's created, and read it back on resume. So a "yes" cancellation reply
  answers in the language the flow started in. (Add `lang` to the session-record
  shape documented in `brain/1. Orchestrator/ORCHESTRATOR.md`.)
- Put `ctx.lang` into the shared context so **every** skill and error path has it.

### 2. Bound `ctx.send` + LLM-translation fallback at the choke point

- Give skills a `ctx.send` that already knows `ctx.lang`, so skill call sites like
  `send(number, text)` don't each have to pass the language.
- In `server.js` `send()`: when `ctx.lang` is a language we **wrote a map for**
  (`en`/`pt`) the text is already localized by the skill — send as-is. When it's
  **not** (and isn't English), translate the **body** (never the header) via a
  cheap model (Haiku) with strict guardrails: *preserve URLs, emails, numbers,
  times, newlines, bullets, and proper nouns; translate prose only.*
  - English → no call.
  - Maintained languages (`en`/`pt`) → no call (already localized by the skill).
  - So the fallback only fires for the long tail.

### 3. Per-skill language maps (prose stays in the skills)

Each skill localizes its own strings inside its `prompt.js`, keyed by language,
and its `skill.js` selects the entry with `ctx.lang`. The Audio skill's `MSG` is
the template for the pattern.

- **Audio** `2. Skills/2. Audio transcriptions/prompt.js`: turn `MSG` and
  `formatTranscript` into `{ en, pt }` shapes (e.g. `MSG.pt.noAudio(tag)`), and
  have `skill.js` read `MSG[ctx.lang] ?? MSG.en`. Already §6-compliant, so this is
  the lightest conversion — do it first as the reference implementation.
  - **Opportunity (small):** with `ctx.lang` in hand, derive AssemblyAI's
    `language_code` from `ctx.lang` (falling back to `ASSEMBLYAI_LANGUAGE`) so a PT
    chat transcribes in PT automatically, instead of the static env alone.
- **Calendar** `2. Skills/1. Calendar Actions/`: move the **~14** inline strings out
  of `skill.js` into `prompt.js` as per-language render functions taking vars — this
  also **brings the calendar skill into `PROJECT_LOG.md` §6 compliance** (prose
  belongs in `prompt.js`), not just multilingual. Handle the three buckets from
  "Where user-facing text lives today":
  - **Plain interpolation** → `{ en, pt }` render functions (`renderCreateConfirm`,
    the "Done!…"/"already exists…" block, the delete-confirm block, the "Cancelled
    …"/"Okay…" lines, and each error string).
  - **English-grammar assembly** → give `renderInquiry` (and its `joinList`, plus
    the "Ana, I'm missing your email" case) a **separate render function per
    language**; do not share one list-builder across languages.
  - **Pluralization** → `countNote`/`dupNote` are computed per language inside their
    render function (PT and EN pluralize differently), not passed in as a prebuilt
    English fragment.
  - Also swap `whenStr` to `localizeDate(ctx.lang, …)` (step 4), and persist `lang`
    in the sessions this skill opens (`openCreateConfirm`, `openInquiry`,
    `handleDelete`) so the confirm/cancel/gather continuations answer in-language.
- **Server** `1. Orchestrator/server.js`: the "I didn't understand… Available
  skills", router-error, continuation-error, and skill-error strings get an
  `en`/`pt` map living in the orchestrator (not a shared module), selected by
  `ctx.lang`.
  - The router-error path fires *before* we have the router's `lang` — use a
    stored session lang if present, else default English; the fallback covers any
    other case.

### 4. Localized dates

- Add a small `localizeDate(lang, date, opts)` helper (maps `pt → "pt-BR"` 24h /
  `en → "en-US"` hh:mm AM/PM) and use it in place of the hardcoded `"en-US"` in
  `Calendar Actions/skill.js` (`whenStr`) and `server.js` (`nowStr`).
- This is the one genuinely cross-cutting utility. Since only two call sites need
  it, keep it minimal — a tiny shared `lib/` helper, or duplicate the few lines
  per skill. Decide at build time; not worth a module of its own.

### 5. Notes / edge cases

- **Header stays constant** — never translated — because
  `text.startsWith(HEADER)` is used to skip the brain's own messages.
- **Classification prompts** (`buildConfirmSystem`, calendar/router system
  prompts) stay in English — they already accept PT/EN affirmatives and are
  internal, not user-facing.
- **Adding a maintained language** = add a `pt`-sibling entry to every skill's map
  (and the orchestrator's). **Adding a new skill string** = add its `en` + `pt`
  entries in that skill's `prompt.js`; any uncovered language is still handled by
  the fallback.

---

## Documentation changes

Multilingual is only durable if the docs teach it. Otherwise the next skill ships
English-only inline strings and silently regresses to "everything gets
LLM-translated," which is exactly the hot-path/formatting cost we chose to avoid.
So the same PR updates the docs that steer development.

### The convention to propagate (state it once, link to it)

> **Skill localization convention.** Every user-facing string a skill sends lives
> in that skill's `prompt.js` as a **per-language map** (`{ en, pt }`), selected at
> send time with `ctx.lang` (fall back to `en`). **Every new message sequence must
> ship its `en` *and* `pt` entries.** English is the canonical source. Do not write
> user-facing prose inline in `skill.js`. The orchestrator's `send()` will
> LLM-translate the body for any language you did *not* write a map for (the long
> tail) — that fallback is a safety net for unmaintained languages, **not** a
> substitute for authoring `en`/`pt`. Never translate the `[AI Brain]:` header.
> Internal/classification prompts (router + skill system prompts) stay English.

`ARCHITECTURE.md` is the canonical home for the full block; `PROJECT_LOG.md` §6
already carries the seed of it (*"to change wording or language, edit only its
`prompt.js`"*), so that line is **extended**, not written from scratch. The other
docs reference `ARCHITECTURE.md`.

### Per-file edits

- **`ARCHITECTURE.md`**
  - "Adding a skill" section: extend the contract snippet/note with the convention
    block above (the authoritative copy). Show `run(ctx)` reading `ctx.lang` and a
    `prompt.js` `{ en, pt }` map.
  - Flow section #3 (router): note the router now returns `"lang"` and is
    schema-enforced (`output_config.format` with `ROUTER_SCHEMA`) like the calendar
    calls — sample output `{ "tasks": [...], "lang": "pt", "reason": "..." }`,
    default `"en"`.
  - Env vars: note `ASSEMBLYAI_LANGUAGE` is now a fallback for the transcription
    call only, not the reply language (replies follow `ctx.lang`; transcription may
    also follow `ctx.lang` if the step-3 opportunity is taken).

- **`PROJECT_LOG.md`**
  - §6 ("How a skill works — the contract"): upgrade the existing "prompt/text lives
    in `prompt.js`" line into the full localization convention — prose is a
    `{ en, pt }` map selected by `ctx.lang`, both languages required per message,
    header never translated, `send()` fallback for the long tail. Add `lang` to the
    documented `ctx` field list.
  - §10 (changelog): add a dated entry when this ships.

- **`brain/README.md`**
  - "How a skill is discovered": add `lang` to the documented `ctx` field list.
  - Add a short **Localization** subsection: one-paragraph pointer to the
    convention in `ARCHITECTURE.md` — new skills keep prose in `prompt.js` as
    `{ en, pt }` and must add both for every message.

- **`brain/1. Orchestrator/ORCHESTRATOR.md`**
  - `send(number, text)` section: document that it localizes — pass-through for
    maintained languages (`en`/`pt`, already localized by the skill), LLM-translate
    the **body only** (header preserved) for the long tail; English never calls.
  - Webhook pipeline #11 (build `ctx`): add `lang` to the listed ctx fields, and
    note where it comes from (router on fresh commands; session on continuations;
    default `en`).
  - Router dispatch (#12 / "Anthropic" touchpoint): note `route()` returns `lang`,
    persisted into the session so continuations reply in the flow's language.
  - "Messages the orchestrator itself sends": note these four strings now come from
    an `en`/`pt` map (still `[AI Brain]:`-prefixed); the router-error one may fire
    before `lang` is known → session lang if present, else English, else fallback.

- **`brain/2. Skills/2. Audio transcriptions/SKILL.md`**
  - "For AI / maintainers": note `prompt.js` now holds `MSG` / `formatTranscript`
    as `{ en, pt }`, selected by `ctx.lang` (fallback `en`); the human-facing
    "what you'll see" strings are the `en` copy and the reply mirrors the chat
    language. Clarify `ASSEMBLYAI_LANGUAGE` drives the *transcription*, not the
    reply prose.

- **`brain/2. Skills/1. Calendar Actions/SKILL.md`**
  - "For AI / maintainers": note the reply strings moved out of `skill.js` into
    `prompt.js` as per-language render functions selected by `ctx.lang`; dates use
    `localizeDate(ctx.lang, …)`; the sessions it opens persist `lang` so the
    confirm/cancel continuations answer in the same language.

- **`brain/1. Orchestrator/ORCHESTRATOR.md`** ("Sessions — shape & skill contract")
  - In the session-record shape JSON block, add `lang` as a stored field (written when
    a skill opens a session, read back on resume) so continuations stay in the flow's
    language.

### Definition of done for docs

A new contributor reading only `ARCHITECTURE.md` + `brain/README.md` should come
away knowing: replies follow `ctx.lang`; skill prose lives in `prompt.js` as
`{ en, pt }`; both languages are required for every new message; the header is
never translated; and the `send()` fallback exists but isn't an excuse to skip
`pt`.

---

## Files touched

- **Edit:**
  - `1. Orchestrator/router/prompt.js` (add `lang` to output spec + a `ROUTER_SCHEMA`)
  - `1. Orchestrator/router/router.js` (return `lang`; wire `output_config.format`
    with `ROUTER_SCHEMA`; keep the regex parse + refusal guard as fallback)
  - `1. Orchestrator/server.js` (ctx.lang, bound send + fallback, localized dates,
    keyed error map, store/read `lang` in the session)
  - `2. Skills/1. Calendar Actions/skill.js` + `prompt.js` (move the ~14 prose
    strings into a per-language map — also §6 compliance; per-language render for
    `joinList`/`countNote`/`dupNote`; select by `ctx.lang`; localizeDate; persist
    `lang` in the sessions it opens)
  - `2. Skills/2. Audio transcriptions/prompt.js` (`MSG`/`formatTranscript` → `{ en, pt }`)
    and its `skill.js` call sites (optionally derive `language_code` from `ctx.lang`)
- **New:** none required (a tiny `localizeDate` helper may live in `lib/` — decide
  at build time). No central `i18n.js`.
- **Docs (same PR — see "Documentation changes"):**
  - `ARCHITECTURE.md` (convention block, router `lang` + schema, env note)
  - `PROJECT_LOG.md` (§6 convention upgrade + `ctx.lang`; §10 changelog entry)
  - `brain/README.md` (`ctx.lang` + Localization pointer)
  - `brain/1. Orchestrator/ORCHESTRATOR.md` (`send()` localization, ctx.lang,
    router `lang`, orchestrator message map)
  - `brain/2. Skills/2. Audio transcriptions/SKILL.md`
  - `brain/2. Skills/1. Calendar Actions/SKILL.md`
  - `brain/1. Orchestrator/ORCHESTRATOR.md` (session `lang` field — already lists the session shape)

## Suggested build order

1. Router language detection **+ schema** (`prompt.js` `ROUTER_SCHEMA` + `router.js`
   `output_config.format`, regex fallback kept), `ctx.lang` in the context, and
   `lang` session persistence in `server.js`.
2. Bound `ctx.send` + LLM-translation fallback in `server.js`.
3. `localizeDate` helper; swap the two hardcoded `"en-US"` sites.
4. Convert the **Audio** skill's `MSG`/`formatTranscript` to `{ en, pt }` **first** —
   it already obeys §6, so it's the smallest, cleanest reference implementation of
   the pattern; optionally derive `language_code` from `ctx.lang`.
5. Move the **Calendar** skill's ~14 strings into per-language render functions in
   its `prompt.js` (also §6 compliance; per-language `joinList`/`countNote`/
   `dupNote`); persist `lang` in the sessions it opens.
6. Orchestrator's own error/help strings → `en`/`pt` map.
7. Test end-to-end with a Portuguese message (create invite, cancel flow,
   transcribe), then a non-maintained language to exercise the fallback.
8. Update the docs (see "Documentation changes") in the same PR — the convention
   block in `ARCHITECTURE.md` + `PROJECT_LOG.md` §6 first, then the per-file
   pointers — so the next skill inherits the pattern instead of regressing to
   English-only.
