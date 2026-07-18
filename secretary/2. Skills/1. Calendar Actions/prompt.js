// ============================================================================
//  Skill "Calendar Actions" — PROMPT.
//  Edit this file to change how the assistant interprets calendar orders.
//  Prompt text/rules only — no logic.
//
//  The output JSON must keep matching what skill.js expects.
// ============================================================================

// ---- JSON Schemas for structured outputs (output_config.format) --------------
// Single source of truth for the SHAPE of each reply. skill.js passes these to
// messages.create so the API returns ONLY schema-valid JSON — the prompts below
// describe what each field MEANS, the schema enforces its type/enum/shape.
// Structured-outputs rules: every object needs additionalProperties:false + a
// full `required` list; optional fields use a nullable type union.
const PARTICIPANT = {
  type: "object",
  additionalProperties: false,
  required: ["name", "email"],
  properties: {
    name: { type: ["string", "null"] },
    email: { type: ["string", "null"] },
  },
};

// The repeat rule for a RECURRING create — a reusable nullable-object fragment shared by
// CAL_SCHEMA and REVIEW_SCHEMA (same reason PARTICIPANT is shared). null = a ONE-OFF event
// (the default). `anyOf` null-union (not a ["object","null"] type-union) — the same reason
// list_mode/participants use anyOf: the structured-output validator is happier with the null
// branch explicit. The object's REAL validation is skill.js's toRRule; this is only the shape.
const RECURRENCE = {
  anyOf: [
    { type: "null" },
    {
      type: "object",
      additionalProperties: false,
      required: ["freq", "interval", "byday", "count", "until"],
      properties: {
        freq: { type: "string", enum: ["daily", "weekly", "monthly"] },
        interval: { type: ["number", "null"] },
        byday: {
          anyOf: [
            { type: "null" },
            { type: "array", items: { type: "string", enum: ["MO", "TU", "WE", "TH", "FR", "SA", "SU"] } },
          ],
        },
        count: { type: ["number", "null"] },
        until: { type: ["string", "null"] },
      },
    },
  ],
};

export const CAL_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: [
    "action",
    "title",
    "participants",
    "start_iso",
    "duration_min",
    "all_day",
    "all_day_end_iso",
    "summary",
    "list_mode",
    "range_start_iso",
    "range_end_iso",
    "recurrence",
  ],
  properties: {
    action: { type: "string", enum: ["create", "delete", "edit", "list", "other"] },
    title: { type: ["string", "null"] },
    participants: { type: "array", items: PARTICIPANT },
    start_iso: { type: ["string", "null"] },
    duration_min: { type: ["number", "null"] },
    // The event takes the WHOLE day ("o dia inteiro", "all day") — a real Google all-day
    // event, not a 24h timed block. start_iso is STILL filled (the day is derived from it);
    // duration_min is ignored.
    all_day: { type: ["boolean", "null"] },
    // A multi-day all-day RANGE ("segunda a quarta"): the LAST day the event still COVERS,
    // INCLUSIVE, at 00:00 -03:00. null = a single day. Google's end.date is exclusive, but
    // that conversion happens in exactly ONE place (createFromDraft) — everything else in
    // this skill, the model included, speaks inclusive days.
    all_day_end_iso: { type: ["string", "null"] },
    summary: { type: "string" },
    // action="list" only (null for every other action): the read-only query window.
    // list_mode "next" = forward-scan for the soonest event; "window" = a bounded span.
    // anyOf (not a type-union + enum): the structured-output validator rejects an enum
    // whose declared type is ["string","null"] — same nullable pattern RESOLVE_SCHEMA uses.
    list_mode: {
      anyOf: [{ type: "null" }, { type: "string", enum: ["window", "next"] }],
    },
    range_start_iso: { type: ["string", "null"] },
    range_end_iso: { type: ["string", "null"] },
    // The repeat rule for a RECURRING create ("every Monday", "a cada 2 semanas") — else null,
    // a one-off (the default). action="create" only; toRRule (skill.js) is its sole validator.
    recurrence: RECURRENCE,
  },
};

// The yes/no/unrelated classifier for a pending confirmation is shared by every
// confirm-first skill — schema + prompts live in 1. Orchestrator/lib/confirm.js.

export const REVIEW_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: [
    "decision",
    "title",
    "participants",
    "start_iso",
    "duration_min",
    "all_day",
    "all_day_end_iso",
    "summary",
    "recurrence",
  ],
  properties: {
    decision: { type: "string", enum: ["confirm", "modify", "cancel", "unrelated"] },
    title: { type: ["string", "null"] },
    participants: { type: "array", items: PARTICIPANT },
    start_iso: { type: ["string", "null"] },
    duration_min: { type: ["number", "null"] },
    // Same two fields as CAL_SCHEMA, same meaning. They are HERE so that "na verdade, o dia
    // todo" / "só até terça" works at the confirm step — and so an unrelated modify (a
    // rename) cannot silently drop either one.
    all_day: { type: ["boolean", "null"] },
    all_day_end_iso: { type: ["string", "null"] },
    summary: { type: "string" },
    // The repeat rule, HERE so the confirm step can add / change / CLEAR it. null is the
    // CLEAR value ("just once") — applyDraftUpdate reads it DIRECTLY, so the review copy must
    // make the model ECHO the current recurrence on non-clearing modifies and return null
    // ONLY to clear (a null it did not mean drops the whole series).
    recurrence: RECURRENCE,
  },
};

// The gathering pass. It carries a DECISION (the same confirm|modify|cancel|unrelated
// vocabulary every other review in this repo uses — see 6. Flight Search's
// FLIGHT_REVIEW_SCHEMA) because a message arriving mid-gathering is not always a *fill*:
// it may call the whole booking off, or be ordinary chatter. It also carries a NEGATIVE
// channel, `no_email_for` — the names the owner has ANSWERED that he has no email for.
// A required field is only legitimate if a TRUTHFUL answer can satisfy it, and before this
// "I don't have her email" was unrepresentable, so the owner could never leave the loop.
export const RESOLVE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["decision", "start_iso", "participants", "no_email_for"],
  properties: {
    decision: { type: "string", enum: ["confirm", "modify", "cancel", "unrelated"] },
    start_iso: { type: ["string", "null"] },
    participants: {
      anyOf: [{ type: "null" }, { type: "array", items: PARTICIPANT }],
    },
    no_email_for: { type: "array", items: { type: "string" } },
  },
};

// The focused EDIT pass (Phase B). Given the current event and the owner's change
// request, it returns ONLY the fields that change (null / empty when untouched) —
// or, if the request is ambiguous or missing a needed detail, a short `clarify`
// question with every change left null. Emails to add/remove are plain arrays.
//
// The `new_*` prefix is this pair of schemas' own convention: the same all-day fields
// CAL_SCHEMA / REVIEW_SCHEMA carry on the create side, named as the CHANGE they are.
// null = "not changing this" — see THE RULE on new_all_day in skill.js (applyPatchToDraft).
export const EDIT_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: [
    "new_start_iso",
    "new_duration_min",
    "new_title",
    "new_summary",
    "new_all_day",
    "new_all_day_end_iso",
    "add_emails",
    "remove_emails",
    "clarify",
  ],
  properties: {
    new_start_iso: { type: ["string", "null"] },
    new_duration_min: { type: ["number", "null"] },
    new_title: { type: ["string", "null"] },
    new_summary: { type: ["string", "null"] },
    new_all_day: { type: ["boolean", "null"] },
    new_all_day_end_iso: { type: ["string", "null"] },
    add_emails: { type: "array", items: { type: "string" } },
    remove_emails: { type: "array", items: { type: "string" } },
    clarify: { type: ["string", "null"] },
  },
};

// The confirm-step review for edit (mirrors REVIEW_SCHEMA for create): the same change
// fields as EDIT_SCHEMA PLUS a `decision`. Runs for each owner message while confirming
// a pending edit — classifies confirm/modify/cancel/unrelated and, for a modify, carries
// the further change to fold onto the draft.
export const EDIT_REVIEW_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: [
    "decision",
    "new_start_iso",
    "new_duration_min",
    "new_title",
    "new_summary",
    "new_all_day",
    "new_all_day_end_iso",
    "add_emails",
    "remove_emails",
    "clarify",
  ],
  properties: {
    decision: { type: "string", enum: ["confirm", "modify", "cancel", "unrelated"] },
    new_start_iso: { type: ["string", "null"] },
    new_duration_min: { type: ["number", "null"] },
    new_title: { type: ["string", "null"] },
    new_summary: { type: ["string", "null"] },
    // Same two fields as EDIT_SCHEMA, same meaning. They are HERE so that "na verdade, o
    // dia todo" / "só até sexta" works at the CONFIRM step too — the refinement loop.
    new_all_day: { type: ["boolean", "null"] },
    new_all_day_end_iso: { type: ["string", "null"] },
    add_emails: { type: "array", items: { type: "string" } },
    remove_emails: { type: "array", items: { type: "string" } },
    clarify: { type: ["string", "null"] },
  },
};

// The skill's own extraction RULEBOOK — everything buildSystem() says after its opening line.
// It lives on its own because it is used TWICE, and it must be the SAME text both times:
//   - buildSystem(), below, for this skill's dedicated interpret() call (the fallback);
//   - the orchestrator's merged router+extractor call, which carries it VERBATIM as the
//     rulebook of `manifest.inputs` (skill.js). The router does not read it or reword it —
//     it is opaque text to the orchestrator, which is what keeps the orchestrator generic.
// Carried whole, the merged call keeps a nameless guest on a terse order; a trimmed rulebook
// DROPS her, and a dropped guest is a person who is silently never invited. Do not trim it to
// "just the fields the router needs", and do not reword it in one place only.
export function buildExtractionRules(OWNER_NAME) {
  return `Choosing "action":
- "create": ${OWNER_NAME} wants to schedule/create a NEW meeting or event.
- "delete": ${OWNER_NAME} wants to cancel/delete/remove an EXISTING event. This
  almost always happens when the order is a REPLY to a message that contains a
  Google Calendar link. If the quoted message has a calendar link and the order
  asks to cancel/delete/remove — or is just an affirmative like "yes"/"confirm"
  right after a cancellation was proposed — choose "delete".
- "edit": ${OWNER_NAME} wants to CHANGE an EXISTING event — reschedule it (move to
  another time/date), change its length/duration, add or remove an attendee, or
  rename it. Like delete, this is almost always a REPLY to a message that contains a
  Google Calendar link, but the order asks to move/change/reschedule/rename/add/remove
  rather than call the whole event off. Choose "edit" (NOT "delete") whenever the event
  survives with a modification; choose "delete" only when the event is cancelled entirely.
- "list": ${OWNER_NAME} is ASKING what's on the calendar — a READ-ONLY query about
  existing events (e.g. "what's on my calendar tomorrow?", "do I have anything Friday
  afternoon?", "what's my next meeting?"). Nothing is created, changed, or cancelled.
  Choose "list" for any question that just READS the schedule.
- "other": none of the above.

For action="create", fill these (for action="delete", ALSO fill participants and start_iso — see below):
- title = the event's short calendar HEADING (a few words). PRIORITY:
  1. PREFER A MEANINGFUL TOPIC — what the event is ABOUT — inferred from the WHOLE
     conversation and the order, not only an explicitly stated name. "Budget 2026",
     "Q3 budget review", "Apartment viewing" are meaningful. A PARTICIPANT-SHAPED label is
     NOT a topic and must NOT be produced here: "Meeting with John", "Call with Ana" name
     WHO, not WHAT — they do not count as meaningful.
  2. ONLY if the conversation genuinely gives NO subject, set title=null. The code then
     falls back to the participants' names joined with "/", owner first (e.g. "Marcelo/John").
     You do NOT build that string — just leave title=null.
  Do NOT invent a subject the conversation doesn't support, and do NOT dress a participant
  list up as a fake topic.
- participants = ALL the people who will be in the meeting, BESIDES ${OWNER_NAME}. Use the context: if ${OWNER_NAME} talks to X about scheduling a meeting with Y, decide from context who will actually attend (it may be only Y, or X and Y). Include each person's name.
- For each participant, include the email if it appears in the conversation; otherwise email=null.
- You will receive the name of the contact ${OWNER_NAME} is currently talking to; use it when it makes sense.
- start_iso in ISO 8601 with the -03:00 offset; convert relative dates using the current date/time provided.
- duration_min = minutes if stated; otherwise null.
- all_day = true when the order says the event takes the WHOLE DAY ("o dia inteiro", "o dia todo", "all day") rather than starting at a time. STILL fill start_iso — with the FIRST day of the event at 00:00, -03:00 offset (the day is read from it); duration_min is ignored. If the order states a TIME ("amanhã 10h", "at 3pm"), all_day = false.
- all_day_end_iso: ONLY for an all-day RANGE spanning several days ("de segunda a quarta", "a semana toda", "Mon to Wed", "os dois dias"). Set it to the LAST day the event STILL COVERS, at 00:00 with the -03:00 offset — INCLUSIVE: for "segunda a quarta" it is WEDNESDAY, not Thursday. Do NOT add a day. For a single all-day event, and whenever all_day is false, set all_day_end_iso = null.
- "summary": a longer one-line agenda/description for the event BODY — distinct from the short title above; may be "" when there's nothing to add.
- recurrence = the repeat rule when the order asks for a REPEATING event ("every Monday",
  "toda segunda", "every 2 weeks", "a cada 2 semanas", "5 times", "5 vezes", "until August",
  "até agosto", "every morning", "daily", "on the 5th every month", "todo dia 5"). Otherwise
  recurrence = null — a ONE-OFF event, the default. NEVER invent a repeat from a single order.
  start_iso STAYS the FIRST occurrence. The object is {freq, interval, byday, count, until}:
  - freq: "daily" ("every day", "todo dia", "every morning", "daily"); "weekly" ("every Monday",
    "toda segunda", "every week"); "monthly" ("every month", "todo mês", "on the 5th every
    month"). Monthly repeats on start_iso's DAY-OF-MONTH. v1 has NO "first Monday of the month"
    and NO yearly — if the order needs either, set recurrence = null.
  - interval: the N in "every N days/weeks/months" ("every 2 weeks" -> 2; "a cada 2 semanas" ->
    2). null or 1 when not stated.
  - byday: WEEKLY only — the weekdays as ["MO","TU","WE","TH","FR","SA","SU"] ("every Mon & Wed"
    -> ["MO","WE"]; "toda segunda" -> ["MO"]). null for daily/monthly.
  - count: the number of occurrences ("5 times"/"5 vezes"/"for 3 sessions" -> 5/5/3); else null.
  - until: the END date ("until August"/"até 30 de ago"), ISO 8601 with the -03:00 offset,
    resolved from the current date/time; else null.
  If the order gives BOTH a count and an until, fill count and leave until null — a repeat has
  one or the other, never both.

For action="delete", also identify WHICH event to cancel so it can be matched on the calendar (the decoded link is only one signal):
- participants: the people the event is WITH — read them (and their emails) from the quoted invite message and the conversation. Include emails whenever they appear (the invite text usually lists them).
- start_iso: the event's date/time, taken from the quoted invite or the conversation, in ISO 8601 with -03:00.
- Still fill "summary" with a short note of what is being cancelled.

For action="edit", the specific CHANGE (new time, new duration, renamed title,
added/removed attendee) is extracted in a following focused step — do NOT put the new
value here. What you MUST do here is identify WHICH existing event is being changed, so it
can be MATCHED on the calendar (exactly like delete — the decoded link is only one signal;
use the WHOLE context, especially the replied-to invite/summary message):
- participants: the people the event is WITH — read them (and their emails) from the quoted
  invite/summary message and the conversation. Include emails whenever they appear.
- start_iso: the event's CURRENT date/time — the one shown in the replied-to invite/summary
  or stated in the conversation — in ISO 8601 with -03:00. This is the EXISTING start used
  to find the event, NOT the new time being requested. If the order is "move it to 4pm",
  start_iso is the event's current start (e.g. the "3:00 PM" printed in the quoted summary),
  never 4pm.
- title/summary: best effort; not used to match.

For action="list", resolve the time WINDOW the question implies and set list_mode:
- list_mode: "next" when ${OWNER_NAME} asks for the NEXT / soonest upcoming event without
  naming a day ("what's my next meeting?", "when's my next call?"). Use "window" for
  everything else (a named day, part of a day, or a range).
- range_start_iso / range_end_iso: the window the question implies, ISO 8601 with the
  -03:00 offset, converted from relative phrases using the current date/time. "tomorrow"
  → that whole day (00:00 to 23:59); "Friday afternoon" → that Friday 12:00–18:00; "this
  week" → the week's span. If NO time is expressed ("what's on my calendar?"), leave BOTH
  null with list_mode="window" (the code then defaults to the rest of today). For
  list_mode="next", leave BOTH null (the code scans forward from now).

For EVERY action other than "list", set list_mode=null, range_start_iso=null, and
range_end_iso=null.`;
}

export function buildSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s calendar assistant. Read the conversation, the order, and any replied-to (quoted) message, then decide the calendar ACTION and extract its data. (Your reply's shape is enforced separately — here, focus on getting the values right.)

${buildExtractionRules(OWNER_NAME)}`;
}

// ---- Continuation: judge whether a message answers a pending confirmation ----
// ---- Continuation: review a pending CREATE (confirm / modify / cancel) --------
// After proposing an event, the secretary shows a draft and asks the owner to confirm.
// This runs for EVERY owner message while that session is open: it BOTH classifies
// the reply AND, when the owner asks for a change, returns the full updated draft
// (one call keeps the correlated fields consistent — same reasoning as create).
export function buildCreateReviewSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s calendar assistant. You already PROPOSED an event and asked ${OWNER_NAME} to confirm it. Read the current DRAFT, the recent conversation, and ${OWNER_NAME}'s LATEST message, then decide what that latest message means for the pending event.
Choose the "decision" and, for a modify, return the updated draft fields (title, participants, start_iso, duration_min, all_day, all_day_end_iso, summary, recurrence):

- "confirm": the latest message clearly approves the event as proposed (e.g. yes, confirm, go ahead, send it, sim, pode, isso).
- "modify": the latest message asks to CHANGE something (time, date, title, duration, attendees, emails, agenda). Return the FULL updated draft with the change applied, carrying over EVERY unchanged field from the current draft exactly.
- "cancel": the latest message calls the whole thing off (e.g. no, forget it, cancel, deixa, esquece).
- "unrelated": normal conversation, NOT a response to this confirmation. If unsure, choose "unrelated".

For "modify", apply the change on top of the current draft:
- keep ISO 8601 with the -03:00 offset for start_iso; convert relative times using the current date/time provided;
- all_day / all_day_end_iso: CARRY THEM OVER FROM THE DRAFT UNCHANGED unless the latest message asks to change them. Set all_day=true if ${OWNER_NAME} now says it is the whole day ("na verdade, o dia todo"), and false if he gives it a time ("na verdade às 10h") — in which case all_day_end_iso becomes null. all_day_end_iso is the LAST day the event STILL COVERS (INCLUSIVE, 00:00 -03:00) when it spans several days ("só até terça" → TUESDAY), and null for a single day. Changing only the title, the duration or the attendees changes NEITHER field;
- when adding/removing an attendee, keep the others; each participant is {name, email|null};
- "participants" is the FULL attendee list. An empty array [] means the event has NO outside guests — return [] ONLY when ${OWNER_NAME} says nobody should be invited. NEVER return [] when you are only changing the time, the title or the duration: echo the draft's attendees exactly.
- recurrence: the repeat rule {freq, interval, byday, count, until} or null, SAME shape and rules as the first extraction. On any modify that is NOT about the repetition (a rename, a new time, an added guest), ECHO the draft's current recurrence EXACTLY — carry it over unchanged. Change it only when the latest message changes the repeat ("make it every other Monday" -> interval 2; "only until August" -> set until; "add Wednesdays" -> add "WE"). Return recurrence = null ONLY when the owner cancels the repetition ("actually just once", "na verdade só uma vez", "not recurring") — that clears it to a single event. NEVER return null just because the modify was about something else: a null you did not mean DROPS the whole series.
- change ONLY what the latest message asks to change; echo everything else from the draft.
For any decision other than "modify", the draft fields are ignored — you may echo the current draft.`;
}

export function buildCreateReviewUser({ draftJson, transcript, latest, nowStr }) {
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
Current DRAFT of the pending event:
${draftJson}

Recent conversation:
${transcript || "(none)"}

Latest message: ${latest}`;
}

// ---- Targeted resolver: find ONLY the fields still missing --------------------
// Every create is stateful. After the broad extraction, if a REQUIRED field is
// missing we make a FOCUSED second pass that looks precisely for those fields — in
// the conversation and the latest message — BEFORE asking a human. Re-run on each
// incoming message while gathering. Higher resolution than the broad pass because
// it is told exactly what to look for. The latest message may come from the owner
// OR from an attendee (awaitFrom:"any").
export function buildResolveSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s calendar assistant. An event is being prepared and some REQUIRED details are still missing. You are told exactly WHICH details are missing. Inspect the current draft, the recent conversation, and the LATEST message. FIRST decide what the latest message IS, then resolve precisely the missing details it gives you:

"decision":
- "confirm" / "modify": the latest message ANSWERS the question you asked, or changes the event (a date/time, who is coming, an email, "nobody else", "I don't have her email"). Both are treated the same — resolve the fields below.
- "cancel": the latest message calls the whole booking off ("esquece", "deixa pra lá", "forget it", "no, drop it").
- "unrelated": ordinary conversation that is NOT an answer to the pending event. When in doubt, choose "unrelated" — silence is the safe default.

- Resolve ONLY the items marked MISSING in "Still missing" below; leave everything else null.
- start_iso: ISO 8601 with the -03:00 offset; resolve relative times ("tomorrow 3pm", "next Tuesday") using the current date/time given. null if genuinely not stated anywhere.
- participants: the FULL, AUTHORITATIVE guest list (everyone besides ${OWNER_NAME}), carrying over the people and emails already in the draft and adding any name or email you can now determine. Each is {name, email|null}.
  - An empty array [] means the event has NO outside guests — return it when ${OWNER_NAME} says "ninguém", "só pra mim", "just me", or "don't invite Laura" and Laura was the only guest. Zero guests is a perfectly ordinary event.
  - null means "I have no information that improves the list" — the draft's current list is kept.
  - [] and null are NOT the same: null keeps the list, [] empties it.
- no_email_for: when ${OWNER_NAME} says he does NOT have someone's email ("não tenho o e-mail dela", "agenda assim mesmo", "book it anyway", "just create it without her"), put that person's NAME here and KEEP them in participants with email: null. This IS an answer — decision "modify", NEVER "unrelated".
- The latest message may come from ${OWNER_NAME} OR from an attendee. If an attendee gives their OWN email ("it's ana@x.com", "sou eu, ana@x.com"), attach it to them — that is an ANSWER ("modify"), never "unrelated". If EXACTLY ONE person still needs an email and the latest message has a single bare email, attach it to that person.
- NEVER invent an email or a time. If it is not clearly present, leave it null — a human will be asked.`;
}

export function buildResolveUser({
  draftJson,
  needsTime,
  needEmailFor,
  gathering,
  transcript,
  latest,
  nowStr,
}) {
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
Current DRAFT: ${draftJson}

Still missing (resolve ONLY these):
- start_iso (event date/time): ${needsTime ? "MISSING" : "already set"}
- email address for these attendees: ${needEmailFor && needEmailFor.length ? needEmailFor.join(", ") : "(none missing)"}

This message is: ${gathering ? "an answer to a question you asked" : "the original order"}

Recent conversation:
${transcript || "(none)"}

Latest message: ${latest}`;
}

// ---- Phase B: focused EDIT resolver -----------------------------------------
// The event to edit is already identified (from the replied-to calendar link) and
// its CURRENT state is given. This pass reads the owner's change request and returns
// ONLY what changes — or a `clarify` question when the request is ambiguous or missing
// a needed detail (e.g. "move it earlier" without saying to when). One call keeps the
// correlated fields (time + duration) consistent, same reasoning as create/review.
export function buildEditSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s calendar assistant. ${OWNER_NAME} wants to CHANGE an existing event. You are given the event's CURRENT state, the recent conversation, and ${OWNER_NAME}'s latest change request. Return ONLY the fields that should change; leave everything else null or empty.

- new_start_iso: the event's NEW start, ISO 8601 with the -03:00 offset, resolving relative times ("4pm", "tomorrow", "move it 30 min later") against the current date/time and the event's current start. null if the time/date is NOT changing. For an ALL-DAY event this is the new FIRST day at 00:00, -03:00.
- new_duration_min: the NEW length in minutes if the request changes it ("make it 30 min", "an hour instead"). null if the duration is NOT changing. Changing only the start does NOT change the duration.
- new_title: the NEW short calendar heading if the request renames it. null otherwise.
- new_summary: a NEW one-line agenda/description if the request changes it. null otherwise.
- new_all_day: the event's WHOLE-DAY state, and ONLY when the request CHANGES it.
  - true when it becomes a whole-day event ("na verdade é o dia todo", "o dia inteiro", "make it all day"). Also fill new_start_iso with the day it lands on IF the day is also changing; if the day stays the same, leave new_start_iso null.
  - false when it stops being one and GETS A TIME ("na verdade é às 10h", "at 3pm instead") — and then you MUST also fill new_start_iso with that time. Turning all-day off ALWAYS means giving the event a time.
  - null in EVERY other case. A rename, a duration change, adding or removing an attendee, or moving an all-day event to another DAY all leave the whole-day state ALONE — null, not false.
- new_all_day_end_iso: ONLY when the request changes the RANGE of an all-day event ("na verdade vai até sexta", "só quarta mesmo", "a semana toda"). The LAST day the event STILL COVERS, at 00:00 with the -03:00 offset — INCLUSIVE: "até sexta" is FRIDAY, do not add a day. null when the range is not changing, and null when the event collapses back to a single day. A rename/duration/attendee change touches NEITHER this field NOR new_all_day.
- add_emails: array of email addresses to ADD as attendees (["carlos@x.com"]). Empty array if none. Only include addresses that actually appear in the request/conversation — NEVER invent one.
- remove_emails: array of email addresses to REMOVE from the attendees. Empty array if none.
- clarify: if the request is AMBIGUOUS or missing a detail you need (e.g. "move it earlier"/"push it back" with no target time, or "add João" with no email on record), set this to a SHORT question asking for exactly that, and leave every change field null/empty. Otherwise clarify=null.

Rules: change ONLY what the latest request asks; never guess a time or an email; if you cannot resolve a needed value, ask via clarify instead of guessing.`;
}

export function buildEditUser({ eventJson, transcript, latest, nowStr }) {
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
Current EVENT being edited:
${eventJson}

Recent conversation:
${transcript || "(none)"}

Change request: ${latest}`;
}

// ---- Phase B: confirm-step review (edit) ------------------------------------
// After proposing the edited event, the secretary shows the target state and asks the owner
// to confirm. This runs for EVERY owner message while that session is open: it BOTH
// classifies the reply AND, when the owner asks for a further change, returns the change
// to fold onto the draft (one call keeps the correlated fields consistent — same as
// create's review). The "event" shown is the PROPOSED target (with changes so far).
export function buildEditReviewSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s calendar assistant. You already PROPOSED an edit to an existing event and asked ${OWNER_NAME} to confirm it. Read the PROPOSED event (its current target state, including changes so far), the recent conversation, and ${OWNER_NAME}'s LATEST message, then decide what that latest message means for the pending edit.

Choose the "decision":
- "confirm": the latest message approves the edited event as shown (e.g. yes, confirm, go ahead, save it, sim, pode, isso).
- "modify": the latest message asks for a FURTHER change (a different time, duration, title, or adding/removing an attendee). Return the change fields to apply ON TOP of the proposed event; leave the others null/empty. If the further change is ambiguous (e.g. "earlier" with no target), set "clarify" to a short question and leave the change fields null.
- "cancel": the latest message calls the edit off / wants to keep the event as it was (e.g. no, leave it, forget it, deixa, mantém).
- "unrelated": normal conversation, NOT a response to this confirmation. If unsure, choose "unrelated".

Change fields (used only for "modify"): new_start_iso (ISO 8601, -03:00; resolve relative times against the current date/time and the proposed start), new_duration_min, new_title, new_summary, new_all_day, new_all_day_end_iso, add_emails[], remove_emails[]. Change ONLY what the latest message asks; never invent a time or an email — ask via clarify instead. For confirm/cancel/unrelated, leave every change field null/empty.

The WHOLE-DAY fields, same rules as the first pass:
- new_all_day = true when ${OWNER_NAME} now says it is the whole day ("na verdade, o dia todo"); false ONLY when he gives it a TIME ("na verdade às 10h") — and then you MUST also fill new_start_iso with that time. In EVERY other case leave it null. A rename, a duration change, an attendee change, or moving an all-day event to another DAY all leave it null — NEVER false.
- new_all_day_end_iso = the LAST day the event STILL COVERS (INCLUSIVE, 00:00 -03:00) when the RANGE changes ("só até sexta" → FRIDAY, do not add a day); null when the range is not changing or the event collapses back to a single day.`;
}

export function buildEditReviewUser({ eventJson, transcript, latest, nowStr }) {
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
PROPOSED event (target state, changes applied so far):
${eventJson}

Recent conversation:
${transcript || "(none)"}

Latest message: ${latest}`;
}

// Builds the "user" message sent along with the system prompt.
export function buildUserPrompt(
  OWNER_NAME,
  { order, transcript, nowStr, contact, quoted }
) {
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
Contact of this conversation: ${contact || "(yourself)"}
Replied-to (quoted) message: ${quoted?.text || "(none)"}
Quoted message contains a Google Calendar link: ${quoted?.calendarLink ? "YES" : "NO"}
Recent conversation:
${transcript || "(no history)"}

${OWNER_NAME}'s order: ${order}`;
}

// ============================================================================
//  USER-FACING REPLY STRINGS (localized).
//  Per-language render functions for EVERY message this skill sends, selected at
//  send time with ctx.lang via reply(). English is canonical; pt is maintained;
//  any other language is produced from the `en` copy by the orchestrator's send()
//  translation fallback. Keep BOTH en + pt for every new message. Interpolated
//  dates arrive pre-formatted (localizeDate); list grammar and pluralization are
//  done per language here — never share an English list-builder across languages.
// ============================================================================

const REPLY_TZ = "America/Sao_Paulo";

// Localized date/time for USER-FACING strings. Bare, zero-padded 24-hour time
// (HH:MM, no AM/PM) and a 3-letter month; the locale sets the day/month ORDER —
// en-US "Jul 5, 2026, 15:00" (month-day), pt-BR "5 de jul. de 2026, 15:00"
// (day-month). São Paulo, no seconds. (The LLM-facing nowStr in server.js stays en.)
export function localizeDate(lang, dateTime) {
  if (!dateTime) return lang === "pt" ? "(sem horário)" : "(no time)";
  const locale = lang === "pt" ? "pt-BR" : "en-US";
  return new Date(dateTime).toLocaleString(locale, {
    timeZone: REPLY_TZ,
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false, // bare 24-hour, no AM/PM
  });
}

// The WHEN-line of a create draft, from the draft itself. Three shapes:
//   timed          -> "14 de jul. de 2026, 10:00"           (localizeDate, unchanged)
//   all-day, 1 day -> "14 de jul. de 2026 · Dia todo"
//   all-day range  -> "13 de jul. de 2026 – 15 de jul. de 2026 · Dia todo (3 dias)"
// Both endpoints are INCLUSIVE (the last day the event still covers) and the DAY COUNT is
// printed. The count is the owner's sanity check: a wrong range that READS like a right one
// is the real danger here, and "(3 dias)" is what catches it before he says "sim". The words
// are the ones the READ side already prints (eventBlock): "All day" / "Dia todo".
export function localizeWhen(lang, draft) {
  if (!draft?.all_day) return localizeDate(lang, draft?.start_iso);
  const startMs = Date.parse(draft.start_iso || "");
  if (!Number.isFinite(startMs)) return localizeDate(lang, null);
  const allDay = lang === "pt" ? "Dia todo" : "All day";
  const endMs = Date.parse(draft.all_day_end_iso || "");
  if (!Number.isFinite(endMs) || endMs <= startMs) {
    return `${localizeDay(lang, startMs)} · ${allDay}`;
  }
  const days = Math.round((endMs - startMs) / 86400000) + 1;
  const unit =
    lang === "pt" ? (days === 1 ? "dia" : "dias") : days === 1 ? "day" : "days";
  return `${localizeDay(lang, startMs)} – ${localizeDay(lang, endMs)} · ${allDay} (${days} ${unit})`;
}

// List grammar, per language. EN: "A", "A and B", "A, B, and C".
function joinListEn(items) {
  if (items.length === 1) return items[0];
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return `${items.slice(0, -1).join(", ")}, and ${items[items.length - 1]}`;
}
// PT: "A", "A e B", "A, B e C" (no Oxford comma; "e").
function joinListPt(items) {
  if (items.length === 1) return items[0];
  if (items.length === 2) return `${items[0]} e ${items[1]}`;
  return `${items.slice(0, -1).join(", ")} e ${items[items.length - 1]}`;
}

// ---- LIST (read-only) render helpers ----------------------------------------
// Time-only (bare, zero-padded 24-hour HH:MM, no AM/PM) in the reply TZ — used for
// event lines inside a single-day window, where the header already states the date.
export function localizeTime(lang, dateTime) {
  if (!dateTime) return "";
  return new Date(dateTime).toLocaleTimeString(lang === "pt" ? "pt-BR" : "en-US", {
    timeZone: REPLY_TZ,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

// Date-only (no time) in the reply TZ — window headers, empty-state, and all-day /
// multi-day event lines.
function localizeDay(lang, ms) {
  return new Date(ms).toLocaleDateString(lang === "pt" ? "pt-BR" : "en-US", {
    timeZone: REPLY_TZ,
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

// Short localized weekday labels for a recurrence phrase (module-private). Canonical
// RRULE codes -> the 3-letter name in each maintained language.
const WEEKDAY_SHORT = {
  en: { MO: "Mon", TU: "Tue", WE: "Wed", TH: "Thu", FR: "Fri", SA: "Sat", SU: "Sun" },
  pt: { MO: "seg", TU: "ter", WE: "qua", TH: "qui", FR: "sex", SA: "sáb", SU: "dom" },
};

// The confirm/done RECURRENCE line, localized. Assumes `rec` is COMPILABLE — the caller
// (recurrenceLineFor) gates on toRRule first, so freq is one of daily/weekly/monthly and any
// until is in the future. Returns a capitalized phrase, e.g. "Every week on Mon, 5 times",
// "A cada 2 semanas às seg, qua", "Todo dia até 30 de ago. de 2026". All-day carries no clock
// time, so these phrases never embed a time — the when-line still prints "Dia todo".
export function describeRecurrence(rec, lang) {
  const pt = lang === "pt";
  const iv = Number(rec.interval) > 1 ? Number(rec.interval) : 1;
  const ORDER = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
  const map = WEEKDAY_SHORT[lang] || WEEKDAY_SHORT.en;
  const set = new Set((Array.isArray(rec.byday) ? rec.byday : []).map((d) => String(d).toUpperCase()));
  const days = ORDER.filter((d) => set.has(d)).map((d) => map[d]).join(", ");

  let core;
  if (rec.freq === "daily") {
    core = iv > 1 ? (pt ? `a cada ${iv} dias` : `every ${iv} days`) : pt ? "todo dia" : "every day";
  } else if (rec.freq === "monthly") {
    core = iv > 1 ? (pt ? `a cada ${iv} meses` : `every ${iv} months`) : pt ? "todo mês" : "every month";
  } else {
    // weekly
    const base = iv > 1 ? (pt ? `a cada ${iv} semanas` : `every ${iv} weeks`) : pt ? "toda semana" : "every week";
    core = days ? (pt ? `${base} às ${days}` : `${base} on ${days}`) : base;
  }

  let suffix = "";
  const count = Number(rec.count);
  if (Number.isFinite(count) && count > 0) {
    suffix = pt ? `, ${count} vezes` : `, ${count} times`;
  } else if (rec.until) {
    const ms = Date.parse(rec.until);
    if (Number.isFinite(ms)) {
      suffix = pt ? ` até ${localizeDay("pt", ms)}` : ` until ${localizeDay("en", ms)}`;
    }
  }

  const phrase = core + suffix;
  return phrase.charAt(0).toUpperCase() + phrase.slice(1);
}

// Do two instants fall on the same calendar day in the reply TZ? Used only for the
// empty-state wording (single day vs a spanning range).
function sameLocalDay(aMs, bMs) {
  return localizeDay("en", aMs) === localizeDay("en", bMs);
}

// The instant used to place an event on a day (all-day events carry dayMs; timed ones
// their start), so events can be grouped under a per-day header.
function eventDayMs(ev) {
  return ev.allDay ? ev.dayMs : new Date(ev.startIso).getTime();
}

// One event rendered as its block: a "time - title" line, then (only if the event has
// external attendees) their emails on the next line. Time is hh:mm for timed events, an
// "all day" label otherwise — the DATE is the group header, not repeated per line.
function eventBlock(lang, ev) {
  const title = ev.title || (lang === "pt" ? "(sem título)" : "(no title)");
  const time = ev.allDay ? (lang === "pt" ? "Dia todo" : "All day") : localizeTime(lang, ev.startIso);
  const head = `${time} - ${title}`;
  return ev.emails.length ? `${head}\n${ev.emails.join(", ")}` : head;
}

// Group start-sorted events into consecutive day buckets and render each as a header
// (the date) followed by its event blocks. Blank line between blocks in a day AND before
// each new day: blocks join with "\n\n", days join with "\n\n", header sits on the line
// directly above its first block.
function renderDays(lang, events) {
  const days = [];
  for (const ev of events) {
    const key = localizeDay("en", eventDayMs(ev)); // locale-neutral grouping key
    let g = days[days.length - 1];
    if (!g || g.key !== key) {
      g = { key, ms: eventDayMs(ev), items: [] };
      days.push(g);
    }
    g.items.push(ev);
  }
  return days
    .map((g) => `${localizeDay(lang, g.ms)}\n${g.items.map((ev) => eventBlock(lang, ev)).join("\n\n")}`)
    .join("\n\n");
}

const REPLY = {
  en: {
    thinkingError: () => "I hit an error while thinking. Try again?",
    noAction: ({ summary }) =>
      `I didn't identify a calendar action. ${summary || ""}`.trim(),
    // An event may legitimately have NO outside guests, and a named guest may be left out
    // because the owner told us he hasn't got their email. Both are stated OUT LOUD: an
    // empty "- " bullet says nothing, and a person must NEVER be dropped silently.
    // `duration` is null for an all-day event (the caller passes null) — `when` already
    // says "All day", and "(1440 min)" is exactly the thing the owner should never see.
    createConfirm: ({ title, emails, when, duration, uninvited, recurrence }) => {
      const guests = emails || "(no guests)";
      const without = uninvited?.length
        ? `\n- Without ${joinListEn(uninvited)} — I don't have their email.`
        : "";
      const rec = recurrence ? `\n- ${recurrence}` : "";
      return `Confirm this event:
- ${title}
- ${guests}${without}
- ${when}${duration ? ` (${duration} min)` : ""}${rec}

Reply "yes" to confirm and I'll send the invites, or tell me what to change and I'll adjust.`;
    },
    createDone: ({ reused, title, emails, when, duration, link, uninvited, recurrence }) => {
      const guests = emails || "(no guests)";
      const without = uninvited?.length
        ? `\n\nI created it without inviting ${joinListEn(uninvited)} — I don't have their email.`
        : "";
      const rec = recurrence ? `\n- ${recurrence}` : "";
      return `${
        reused
          ? "That event already exists — here it is (no duplicate created):"
          : "Done! Invite created and sent:"
      }\n\n- ${title}\n- ${guests}\n- ${when}${duration ? ` (${duration} min)` : ""}${rec}${without}\n\nHere is a link for the event:\n${link}`;
    },
    createCancelled: ({ title }) => `Okay, I won't create "${title}".`,
    createGoogleError: () =>
      "I understood the request but failed to create it in Google. Error in the log.",
    inquiry: (m) => {
      if (!m.noTime && m.emailNames.length === 1) {
        return `${m.emailNames[0]}, I'm missing your email. Can you send it so I can add you to the invite?`;
      }
      const asks = [];
      if (m.noTime) asks.push("the date and time");
      if (m.emailNames.length === 1) asks.push(`${m.emailNames[0]}'s email`);
      else if (m.emailNames.length > 1)
        asks.push(`emails for ${joinListEn(m.emailNames)}`);
      return `Before I can set this up, I still need ${joinListEn(
        asks
      )}. Send it here and I'll continue.`;
    },
    deleteNeedSignal: ({ tag }) =>
      `To cancel an event, reply to its invite message, or tell me which meeting (who and when) and call ${tag} again.`,
    deleteCheckError: () => "I hit an error checking the calendar. Try again?",
    deleteNoMatch: () =>
      "I couldn't find a matching event — it may already be cancelled, or I'm not sure which one you mean. Reply to its invite message and try again.",
    deleteConfirm: ({ title, when, count }) => {
      const countNote = count > 1 ? `\n- (${count} matching copies)` : "";
      return `Confirm the cancelation of this event?\n- ${title}\n- ${when}${countNote}\n\nReply "yes" to confirm, or "no" to keep it.`;
    },
    deleteKeep: ({ title }) => `Okay, I'll keep "${title}".`,
    deleteCancelled: ({ title, removed }) => {
      const dupNote = removed > 1 ? ` (removed ${removed} copies)` : "";
      return `Cancelled "${title}"${dupNote} and notified the attendees.`;
    },
    deleteGoogleError: () =>
      "I found the event but failed to cancel it in Google. Error in the log.",
    editNeedSignal: ({ tag }) =>
      `To change an event, reply to its invite message with the change (e.g. "move it to 4pm") and call ${tag}.`,
    editNoMatch: () =>
      "I couldn't find that event — it may have been cancelled, or the invite link didn't resolve. Reply to its invite message and try again.",
    editCheckError: () => "I hit an error reading the calendar. Try again?",
    editClarify: (question) => question,
    editNoChange: () =>
      "I couldn't tell what to change. Tell me the new time, duration, title, or which attendee to add/remove.",
    // `when` arrives PRE-RENDERED by localizeWhen (all-day -> "14 de jul. de 2026 · All
    // day (3 days)"), and `duration` is null for an all-day event — the same contract
    // createConfirm has. "(1440 min)" is the bug, not the event.
    editConfirm: ({ title, emails, when, duration }) =>
      `Here's the updated event:
- ${title}
- ${emails}
- ${when}${duration ? ` (${duration} min)` : ""}

Reply "yes" to save and notify everyone, or tell me what else to change.`,
    editCancelled: ({ title }) => `Okay, I'll leave "${title}" as it was.`,
    editDone: ({ title, when, duration, emails, link }) =>
      `Done! Updated the event and notified the attendees:\n\n- ${title}\n- ${emails}\n- ${when}${duration ? ` (${duration} min)` : ""}\n\nHere is a link for the event:\n${link}`,
    editGoogleError: () =>
      "I understood the change but failed to update it in Google. Error in the log.",
    listEvents: ({ startMs, endMs, events, capped }) => {
      if (!events.length) {
        return sameLocalDay(startMs, endMs)
          ? `Nothing on your calendar for ${localizeDay("en", startMs)}.`
          : `Nothing on your calendar between ${localizeDay("en", startMs)} and ${localizeDay("en", endMs)}.`;
      }
      const capNote = capped ? "\n\n(Showing the first 50.)" : "";
      return `${renderDays("en", events)}${capNote}`;
    },
    listNext: ({ event }) => {
      if (!event) return "Nothing coming up on your calendar in the next two weeks.";
      return `Your next event:\n${renderDays("en", [event])}`;
    },
    listError: () => "I hit an error reading the calendar. Try again?",
  },
  pt: {
    thinkingError: () => "Tive um erro ao processar. Pode tentar de novo?",
    noAction: ({ summary }) =>
      `Não identifiquei uma ação de calendário. ${summary || ""}`.trim(),
    createConfirm: ({ title, emails, when, duration, uninvited, recurrence }) => {
      const guests = emails || "(ninguém convidado)";
      const without = uninvited?.length
        ? `\n- Sem convidar ${joinListPt(uninvited)} — não tenho o e-mail.`
        : "";
      const rec = recurrence ? `\n- ${recurrence}` : "";
      return `Confirme este evento:
- ${title}
- ${guests}${without}
- ${when}${duration ? ` (${duration} min)` : ""}${rec}

Responda "sim" para confirmar e eu envio os convites, ou me diga o que mudar que eu ajusto.`;
    },
    createDone: ({ reused, title, emails, when, duration, link, uninvited, recurrence }) => {
      const guests = emails || "(ninguém convidado)";
      const without = uninvited?.length
        ? `\n\nCriei sem convidar ${joinListPt(uninvited)} — não tenho o e-mail.`
        : "";
      const rec = recurrence ? `\n- ${recurrence}` : "";
      return `${
        reused
          ? "Esse evento já existe — aqui está ele (nenhuma cópia criada):"
          : "Pronto! Convite criado e enviado:"
      }\n\n- ${title}\n- ${guests}\n- ${when}${duration ? ` (${duration} min)` : ""}${rec}${without}\n\nAqui está o link do evento:\n${link}`;
    },
    createCancelled: ({ title }) => `Ok, não vou criar "${title}".`,
    createGoogleError: () =>
      "Entendi o pedido, mas não consegui criar no Google. O erro está no log.",
    inquiry: (m) => {
      if (!m.noTime && m.emailNames.length === 1) {
        return `${m.emailNames[0]}, estou sem o seu e-mail. Pode me enviar para eu te incluir no convite?`;
      }
      const asks = [];
      if (m.noTime) asks.push("a data e o horário");
      if (m.emailNames.length === 1) asks.push(`o e-mail de ${m.emailNames[0]}`);
      else if (m.emailNames.length > 1)
        asks.push(`os e-mails de ${joinListPt(m.emailNames)}`);
      return `Antes de agendar, ainda preciso do seguinte: ${joinListPt(
        asks
      )}. Envie aqui que eu continuo.`;
    },
    deleteNeedSignal: ({ tag }) =>
      `Para cancelar um evento, responda à mensagem do convite, ou me diga qual reunião (quem e quando) e chame ${tag} de novo.`,
    deleteCheckError: () =>
      "Tive um erro ao verificar o calendário. Pode tentar de novo?",
    deleteNoMatch: () =>
      "Não encontrei um evento correspondente — pode já ter sido cancelado, ou não tenho certeza de qual você quer dizer. Responda à mensagem do convite e tente de novo.",
    deleteConfirm: ({ title, when, count }) => {
      const countNote = count > 1 ? `\n- (${count} cópias correspondentes)` : "";
      return `Confirmar o cancelamento deste evento?\n- ${title}\n- ${when}${countNote}\n\nResponda "sim" para confirmar, ou "não" para manter.`;
    },
    deleteKeep: ({ title }) => `Ok, vou manter "${title}".`,
    deleteCancelled: ({ title, removed }) => {
      const dupNote = removed > 1 ? ` (removi ${removed} cópias)` : "";
      return `Cancelado "${title}"${dupNote} e avisei os participantes.`;
    },
    deleteGoogleError: () =>
      "Encontrei o evento, mas não consegui cancelar no Google. O erro está no log.",
    editNeedSignal: ({ tag }) =>
      `Para alterar um evento, responda à mensagem do convite com a mudança (ex.: "muda para 16h") e chame ${tag}.`,
    editNoMatch: () =>
      "Não encontrei esse evento — pode ter sido cancelado, ou o link do convite não resolveu. Responda à mensagem do convite e tente de novo.",
    editCheckError: () =>
      "Tive um erro ao ler o calendário. Pode tentar de novo?",
    editClarify: (question) => question,
    editNoChange: () =>
      "Não consegui entender o que mudar. Me diga o novo horário, a duração, o título, ou qual participante adicionar/remover.",
    editConfirm: ({ title, emails, when, duration }) =>
      `Aqui está o evento atualizado:
- ${title}
- ${emails}
- ${when}${duration ? ` (${duration} min)` : ""}

Responda "sim" para salvar e avisar todo mundo, ou me diga o que mais mudar.`,
    editCancelled: ({ title }) => `Ok, vou deixar "${title}" como estava.`,
    editDone: ({ title, when, duration, emails, link }) =>
      `Pronto! Atualizei o evento e avisei os participantes:\n\n- ${title}\n- ${emails}\n- ${when}${duration ? ` (${duration} min)` : ""}\n\nAqui está o link do evento:\n${link}`,
    editGoogleError: () =>
      "Entendi a mudança, mas não consegui atualizar no Google. O erro está no log.",
    listEvents: ({ startMs, endMs, events, capped }) => {
      if (!events.length) {
        return sameLocalDay(startMs, endMs)
          ? `Nada na sua agenda para ${localizeDay("pt", startMs)}.`
          : `Nada na sua agenda entre ${localizeDay("pt", startMs)} e ${localizeDay("pt", endMs)}.`;
      }
      const capNote = capped ? "\n\n(Mostrando os primeiros 50.)" : "";
      return `${renderDays("pt", events)}${capNote}`;
    },
    listNext: ({ event }) => {
      if (!event) return "Nada na sua agenda nas próximas duas semanas.";
      return `Seu próximo evento:\n${renderDays("pt", [event])}`;
    },
    listError: () => "Tive um erro ao ler o calendário. Pode tentar de novo?",
  },
};

// Pick the reply set for a language, falling back to English (which the
// orchestrator's send() then translates for any non-en/pt language).
export function reply(lang) {
  return REPLY[lang] || REPLY.en;
}
