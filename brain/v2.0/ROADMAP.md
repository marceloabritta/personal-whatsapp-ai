# Roadmap — next implementations

Forward-looking plan for the calendar features still to build. The stateful
mechanism these rely on is described in [STATEFUL_ARCHITECTURE_PLAN.md](STATEFUL_ARCHITECTURE_PLAN.md).

## Status snapshot (as of 2026-07-10)

Done and live:
- `calendar_action` skill: **create** and **cancel/delete** events.
- **Stateful conversation layer** (Redis sessions): a flow starts on `@brain`, then
  continues without the tag; the brain uses the LLM to detect the awaited answer and
  ignores normal chatter. `awaitFrom` selects who may answer (owner / contact / any).
- Delete uses a confirm-first session: the owner just types `yes`/`no`.
- Message polish: `[AI Brain]:` header, `SECRETARY_TAG` single source, clean invite text.

Remaining: **smart scheduling (Phase C)** and **edit/reschedule (Phase B)**.

---

## Phase C — Smart scheduling (create improvements)

The original goal: name the event by its topic, know how many people should attend,
and collect any missing emails — asking the owner *or the attendee themselves*.
Built in three testable steps.

### C1 — Topic-based event naming (no state)
- **Scope:** name the event by its inferred subject instead of `Owner & Guest`.
- **Files:** `2. Skills/1. Calendar Actions/prompt.js` (add a `topic` field + rule),
  `skill.js` (`title = topic || Owner & names` fallback in `handleCreate`).
- **Test:** "@brain schedule a Q3 budget review with ana@x.com tomorrow 3pm" → event
  titled **Q3 budget review**; a topic-less order → falls back to `Owner & Ana`.
- **Done when:** titled by topic when present; clean fallback otherwise.

### C2 — Headcount + missing emails, ask the OWNER (`awaitFrom: "owner"`)
- **Scope:** detect how many attendees the conversation implies and which emails are
  missing; if any are missing, ask the owner for the *specific* person, and capture it
  from the owner's normal reply (session continuation — no re-tag).
- **Files:** `prompt.js` (add `expected_count`; per-participant `{name,email}`),
  `skill.js` (`handleCreate` computes `missingEmails = participants without email`;
  opens an `await_clarification` session holding the pending event draft; a resume
  handler LLM-extracts the email(s) from the next owner message and fills the draft;
  when complete, creates the event).
- **Session shape:** `{ skill:"calendar_action", intent:"create", stage:"await_clarification",
  awaitFrom:"owner", data:{ draft, missingFor:[names] } }`.
- **Test:** order names two people, one email given → brain asks "what's Bruno's
  email?"; owner types "bruno@x.com" (no tag) → event created with both. Unrelated
  chatter in between is ignored.
- **Done when:** asks by name; fills from a plain reply; ignores chatter; creates when complete.

### C3 — Ask the CONTACT for their email (`awaitFrom: "contact"`)
- **Scope:** the marquee stateful case. When scheduling in a 1:1 chat with the very
  person who's missing an email, the brain can ask *them* and capture the email from
  their normal message (not a reply, no tag). First real use of `awaitFrom:"contact"`.
- **Files:** `skill.js` (`handleCreate`: when the missing attendee *is* the current
  chat contact, open a session with `awaitFrom:"contact"` and message the chat asking
  for their email; a resume handler — run for the contact's messages — LLM-extracts an
  email and, when found, creates the event and confirms to the owner).
- **Design notes:** the brain messages the shared 1:1 chat (visible to the contact via
  the owner's account). Only extract a clearly-provided email; ignore everything else
  silently. Bound with the session TTL. Consider a note back to the owner when asking.
- **Test:** "@brain schedule with this person tomorrow 3pm" (no email in chat) → brain
  asks the contact for their email → contact replies "it's ana@x.com" → event created,
  owner notified. Contact chit-chat before the email is ignored.
- **Done when:** contact's plain reply supplies the email and the event is created;
  non-email messages are ignored.

---

## Phase B — Edit / reschedule via reply

- **Scope:** reply to an event's calendar link with a change ("move to 4pm",
  "make it 30 min", "add carlos@x.com", "rename to Kickoff"); apply it, asking for
  clarification when ambiguous.
- **Files:** `prompt.js` (interpret returns `action:"edit"` + a `changes` object and a
  `clarify` field), `skill.js` (`handleEdit`: resolve the event id from the quoted
  link via the existing `resolveEventId`; `events.get` current state; build a patch;
  `events.patch({ sendUpdates:"all" })`; if `clarify` is set, open an
  `await_clarification` session — `awaitFrom:"owner"` — and resume with the answer).
- **Reuses:** `resolveEventId`, `getEvent`, the session/clarification pattern from C2.
- **Test:** reply to an invite with "@brain move it to 4pm" → patched + attendees
  notified; "add carlos@x.com" → added; ambiguous "move it earlier" → asks for the
  time, then applies the plain reply.
- **Done when:** reschedule / relength / add-remove attendee / rename all work;
  ambiguous requests clarify instead of guessing.

---

## Suggested order
C1 (quick win, no state) → C2 (owner clarification) → C3 (contact answering, the
headline feature) → Phase B (edit). Each step: implement → deploy → test → next.

## Notes / smaller follow-ups
- Remove the temporary `QUOTED>>>` diagnostic log in `server.js` once the calendar
  flows are settled.
- The per-message LLM "is this the answer?" check runs only while a session is open;
  if cost matters, add a cheap pre-filter (e.g. only invoke when the message looks
  like it could contain the awaited info).
