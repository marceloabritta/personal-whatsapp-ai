# Multi-lingual Brain — Implementation Plan

## Goal

The brain must detect the language the owner is writing in on a given chat and
reply in that **same language**, system-wide across every feature.

Example: a reply that would say "Invite sent" must come back as "Convite enviado"
when the conversation is in Portuguese.

## Decisions (agreed)

- **Approach: Hybrid.**
  - A translation **catalog** (message key → per-language string) for the common
    languages, giving exact control over formatting.
  - Plus an **LLM-translation fallback** at the single `send()` choke point for
    any other language the owner happens to type, so it still works system-wide.
- **Catalog languages: English (canonical/source) + Portuguese (PT-BR).**
  Any other detected language is handled by the fallback.

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

- Fixed template strings in `2. Skills/1. Calendar Actions/skill.js` (~8, several
  with interpolated `title` / `emails` / `when` / event link and carefully tuned
  formatting).
- The `MSG` catalog + `formatTranscript` in
  `2. Skills/2. Audio transcriptions/prompt.js`.
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
  - In `router/router.js` parse `parsed.lang`, default `"en"`, and return it
    alongside `tasks`. **No extra LLM call** — it piggybacks the existing router
    request.
- **Continuations** (bypass the router): persist `lang` inside the Redis session
  when it's created, and read it back on resume. So a "yes" cancellation reply
  answers in the language the flow started in.
- Put `ctx.lang` into the shared context so **every** skill and error path has it.

### 2. New i18n module — `1. Orchestrator/lib/i18n.js`

- **Catalog:** `MESSAGES = { en: {...}, pt: {...} }` keyed by message id, values
  are functions/templates taking vars, e.g.
  `"calendar.created": ({title, emails, when, dur, link}) => ...`. Each language
  keeps its own literal, preserving exact formatting (blank lines, bullet lists).
- **`t(lang, key, vars)`:** returns the catalog string for `lang`; if `lang`
  isn't in the catalog, returns the **English** canonical (handled by the
  fallback in step 3).
- **`localizeDate(lang, date, opts)`:** wraps `toLocaleString`, mapping
  `pt → "pt-BR"` (24h) / `en → "en-US"` (hh:mm AM/PM). Replaces the hardcoded
  `"en-US"` in `Calendar Actions/skill.js` and `server.js`.

### 3. LLM-translation fallback at the `send()` choke point

- In `server.js` `send()`: when `ctx.lang` is **not** in the catalog (not
  `en`/`pt`) and isn't English, translate the **body** (not the header) via a
  cheap model (Haiku) with strict guardrails: *preserve URLs, emails, numbers,
  times, newlines, bullets, and proper nouns; translate prose only.*
  - English → no call.
  - Catalog-covered languages → no call (already localized by `t`).
  - So the fallback only fires for the long tail.
- Thread `lang` through by giving skills a bound `ctx.send` that already knows
  `ctx.lang`, so skill call sites like `send(number, text)` don't each have to
  pass lang.

### 4. Convert the message call sites to keys

- **Calendar** `2. Skills/1. Calendar Actions/skill.js`: replace the ~8 inline
  strings ("Done! Invite created and sent…", "Almost there…", "Confirm the
  cancelation…", "Okay, I'll keep…", "Cancelled…", the two error strings) with
  `t(ctx.lang, "calendar.xxx", vars)`. Change `whenStr` to use `localizeDate`.
- **Audio** `2. Skills/2. Audio transcriptions/prompt.js`: turn the `MSG` object
  into catalog entries (`transcribe.noAudio`, `.processing`, etc.) under both
  languages; `formatTranscript` becomes a keyed template.
- **Server** `1. Orchestrator/server.js`: the "I didn't understand… Available
  skills", router-error, continuation-error, and skill-error strings become
  `t(ctx.lang, "orchestrator.xxx", vars)`.
  - Note: the router-error path fires *before* we have the router's `lang` —
    detect from a stored session lang if present, else default English, then the
    fallback covers it.

### 5. Notes / edge cases

- **Header stays constant** — never translated — because
  `text.startsWith(HEADER)` is used to skip the brain's own messages.
- **Classification prompts** (`buildConfirmSystem`, calendar/router system
  prompts) stay in English — they already accept PT/EN affirmatives and are
  internal, not user-facing.
- **Adding a language later** = add one catalog block.
  **Adding a new skill string** = add EN + PT keys; any uncovered language is
  still handled by the fallback.

---

## Files touched

- **New:** `1. Orchestrator/lib/i18n.js`
- **Edit:**
  - `1. Orchestrator/router/prompt.js` (add `lang` to output spec)
  - `1. Orchestrator/router/router.js` (parse/return `lang`)
  - `1. Orchestrator/server.js` (ctx.lang, bound send + fallback, localized dates,
    keyed error strings)
  - `2. Skills/1. Calendar Actions/skill.js` (keyed strings, localizeDate)
  - `2. Skills/2. Audio transcriptions/prompt.js` (MSG → catalog) and its
    `skill.js` call sites
  - `1. Orchestrator/lib/sessions.js` usage (store/read `lang`)

## Suggested build order

1. `lib/i18n.js` module (catalog + `t` + `localizeDate`).
2. Router language detection (`prompt.js` + `router.js`).
3. `send()` wrapper + LLM-translation fallback in `server.js`.
4. Convert calendar + audio call sites; localize dates.
5. Test end-to-end with a Portuguese message (create invite, cancel flow,
   transcribe), then a non-catalog language to exercise the fallback.
