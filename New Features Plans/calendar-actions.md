# Calendar Actions — Smart Scheduling & Edit (Implementation Plan)

Forward-looking plan for the calendar features still to build. The stateful
mechanism these rely on is described in [STATEFUL_ARCHITECTURE_PLAN.md](../brain/STATEFUL_ARCHITECTURE_PLAN.md).

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

## Extraction architecture — decisions & improvements

Notes on *how* create pulls its data, separate from the stateful flows above.

### What create actually requires (today)
Only **two** fields gate a create ([`skill.js` `handleCreate`](../brain/2.%20Skills/1.%20Calendar%20Actions/skill.js)):
- **`start_iso`** — a date/time (blocks if absent).
- **≥1 attendee email** — at least one email among the participants (blocks if none).

Everything else is defaulted: participant names → title fallback `Owner & Guest`,
`duration_min` → 45 min, `summary` → empty. C1/C2 above upgrade the naming and the
email handling.

### Decision: keep extraction as ONE LLM call (do not split per-field)
`interpret()` extracts action + participants + emails + time + duration in a single
Claude call. This is deliberate — **don't** decompose it into independent per-field
questions ("who?", then "their email?", then "when?"). The fields are **correlated**:
- deciding *who attends* needs the whole conversation;
- attributing an *email* needs to already know *who* the person is;
- resolving "tomorrow 3pm" needs the meeting context.

Independent calls lose that shared context (worse attribution), add latency and tokens,
and add orchestration code — with no accuracy gain for a small 4–5 field extraction.
Slot-filling across turns (C2/C3) is *not* the same thing: that re-runs the same single
extraction as new info arrives, it doesn't split one message into many calls.

### Improvement: confirm before writing to Google (create)
Delete confirms first; create fires immediately on a single, unverified JSON extraction,
so a mis-parsed time or wrong attendee only surfaces after the invite emails go out.
- **Scope:** after extraction succeeds and no slots are missing, show a summary
  (title / attendees / date-time / duration) and ask the owner to confirm before
  `events.insert`.
- **Reuses:** the exact `sessions` + `classifyConfirmation` machinery already proven in
  delete — open an `await_confirmation` session holding the event draft; `yes` creates,
  `no` discards, chatter is ignored.
- **Test:** "@brain schedule with ana@x.com tomorrow 3pm" → brain shows the draft and
  asks to confirm → `yes` → event created; a mis-parsed time is catchable at that step.
- **Done when:** create never writes to Google without an explicit owner confirmation.
- **Sequencing note:** fits naturally *after* C2 (both use the same session pattern);
  can also land standalone before C2 for immediate safety.

### Gap: per-attendee email coverage (not just "≥1 email")
Today create only requires **one** email among all participants. Invite Ana + Bruno with
only Ana's email in the chat and it creates the event with just Ana — Bruno is silently
dropped. C2 addresses this by computing `missingEmails` **per participant** and asking
for each; until C2 lands, this is a known correctness gap worth calling out.

### Optional: extract → self-check (a verifier, not a field-splitter)
If single-call accuracy ever proves shaky, the right second call is a **verifier**
("given this chat, is any extracted field ambiguous or likely wrong?"), not per-field
decomposition. Lower priority than the confirm step above, which gives the human the
final check for free.

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
