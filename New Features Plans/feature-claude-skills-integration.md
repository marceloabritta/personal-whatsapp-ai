# Claude Skills Integration for Secretary

## Summary
Hook the Secretary up to Claude so new features can be authored as Markdown skill
files that run in the cloud, instead of custom-coding a `skill.js` end to end for
every capability. New skills are registered automatically (dropped in, discovered
at boot) rather than hand-wired into the router.

## Problem / motivation
Today, adding a capability to the Secretary means writing a full native skill
(`SKILL.md` + `prompt.js` + `skill.js`, a `manifest`, structured-output plumbing,
error handling). That is heavy per feature and slows iteration. We want most new
features to be a hand-authored MD file that Claude executes in the cloud, with the
Secretary keeping only the routing/core rails.

## User flow (from the user's point of view)
1. User sends a request to the Secretary via chat (no special tag needed — normal
   `@secretaria` trigger + intent matching, same as native skills).
2. Secretary matches the request's intent against the skill catalog. Each entry is
   tagged as **native** (`skill.js`) or **claude** (an MD skill file).
3. If it matches a Claude skill, the Secretary invokes a single generic runner that
   sends the MD skill file + conversation context to Claude in the cloud.
4. Claude executes the skill logic and returns a result.
5. Secretary relays the result back to the user in the same conversation.
6. If the Claude call fails or times out, the user gets an error message and the
   failure is logged for Marcelo to review (no retry, no auto-fallback).

## Actors
- Marcelo (author/developer)
- Secretary system (routing / core rails)
- Claude (cloud skill runtime)
- End user (chats with the Secretary)

## Data & services touched
- **Skill catalog/manifest** — skill id, `type: native | claude`, description/intent
  used by the router. Native skills already self-register from `2. Skills/*/skill.js`;
  Claude skills register the same way (see below).
- **Claude-hosted MD skill files** — the hand-authored skill definitions.
- **Chat conversation/messages** routed through the Secretary (transcript, latest
  order, owner, lang) passed as context to the runner.

## Automated skill registration
Goal: registering a Claude skill should be **drop-in**, matching how native skills
already auto-load at boot (`server.js: loadSkills()` scans `2. Skills/*/skill.js`).

Design:
- Extend the boot scan so a skill folder can ship **either** a `skill.js` (native)
  **or** a `SKILL.md` with front-matter (`id`, `description`, `type: claude`) and no
  code.
- For each `type: claude` folder, the orchestrator registers a catalog entry whose
  `run` is a **shared generic Claude runner** (one runner, many MD skills). The
  runner loads that folder's MD file and calls Claude with a standard context
  payload (owner, transcript, latest order, lang, session).
- The router keeps classifying purely on the catalog `{ id, description }` — it never
  needs to know native vs. claude. Only the dispatcher branches on `type`.
- Net effect: **adding a skill = drop a folder with a `SKILL.md`.** No router edit,
  no server edit, no new native code. This preserves the current "no central
  registry to touch" property the native loader already gives us.

Longer term (open, not v1): a self-service authoring flow where the Secretary itself
helps draft/validate a new MD skill from a chat conversation and stages it for review.

## Suggested testing approach
Since a Claude skill is "just" an MD file + the shared runner, testing splits into
three layers:
1. **Runner unit/contract tests** (native code, runs in CI): given a fixed MD skill
   and a canned conversation context, assert the runner builds the right Claude
   request and correctly handles success, refusal, timeout, and malformed output
   (mirrors the existing structured-output/`readReply` patterns in native skills).
2. **Per-skill golden transcripts**: each MD skill ships a small set of
   `input -> expected-shape` example conversations. A test harness replays them
   through the runner (live Claude or a recorded fixture) and checks the result
   matches the expected shape/intent. This is the pre-registry validation gate for a
   new MD skill.
3. **Routing test**: assert the router selects the new skill id for its trigger
   phrases and does **not** hijack existing native intents (calendar, tasks, etc.) —
   guards against catalog-description collisions.
4. **Live smoke test on the droplet**: before marking done, drive the real flow end
   to end in the WhatsApp self-chat (the flight-search POC is the first candidate),
   including the failure path (force a timeout → confirm the user sees the error and
   a log line is written). Consistent with the project's "verify live in production"
   habit for shipped features.

## Edge cases & open questions
- Claude skill call fails or times out → user gets an error message, Marcelo gets a
  log entry. No retry, no auto-fallback (v1).
- No skill matches the user's intent → Secretary reports it doesn't understand and
  lists available skills (already observed in testing).
- First proof-of-concept Claude skill is **flight search**
  (`feature-busca-de-voos-via-chat-com-ai-brain.md`), using automatic intent
  matching rather than a tag/command.
- Open: exact validation bar an MD skill must pass before it can register (which of
  the test layers above are mandatory gates vs. advisory).
- Open: how much of skill registration eventually becomes self-service/automated vs.
  manual hand-authoring — direction is "more automated", drop-in discovery is v1.

---
*Drafted via the Secretary's feature-request skill on WhatsApp; retrieved from the
droplet draft log and refined here (testing approach + automated registration added
per Marcelo).*
