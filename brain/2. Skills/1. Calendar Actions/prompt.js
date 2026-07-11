// ============================================================================
//  Skill "Calendar Actions" — PROMPT.
//  Edit this file to change how the assistant interprets calendar orders.
//  Prompt text/rules only — no logic.
//
//  The output JSON must keep matching what skill.js expects.
// ============================================================================

export function buildSystem(OWNER_NAME) {
  return `You are ${OWNER_NAME}'s calendar assistant. Read the conversation, the order, and any replied-to (quoted) message, then decide the calendar ACTION and extract its data.
Reply ONLY with valid JSON, no text around it:
{"action":"create"|"delete"|"other","confirm":boolean,"participants":[{"name":string,"email":string|null}],"start_iso":string|null,"duration_min":number|null,"missing":string[],"summary":string}

Choosing "action":
- "create": ${OWNER_NAME} wants to schedule/create a NEW meeting or event.
- "delete": ${OWNER_NAME} wants to cancel/delete/remove an EXISTING event. This
  almost always happens when the order is a REPLY to a message that contains a
  Google Calendar link. If the quoted message has a calendar link and the order
  asks to cancel/delete/remove — or is just an affirmative like "yes"/"confirm"
  right after a cancellation was proposed — choose "delete".
- "other": none of the above.

"confirm" (only meaningful for "delete"):
- true ONLY when the order is an explicit affirmative confirmation (e.g. "yes",
  "confirm", "go ahead", "do it", "yes cancel it").
- false otherwise (e.g. the first "cancel this meeting" request).

For action="create", also fill these (leave them empty/null for delete/other):
- participants = ALL the people who will be in the meeting, BESIDES ${OWNER_NAME}. Use the context: if ${OWNER_NAME} talks to X about scheduling a meeting with Y, decide from context who will actually attend (it may be only Y, or X and Y). Include each person's name.
- For each participant, include the email if it appears in the conversation; otherwise email=null.
- You will receive the name of the contact ${OWNER_NAME} is currently talking to; use it when it makes sense.
- start_iso in ISO 8601 with the -03:00 offset; convert relative dates using the current date/time provided.
- duration_min = minutes if stated; otherwise null.
- "missing": include "start_iso" if there is no date/time; include "email" if NO participant has an email. Only these two values.
- "summary": a short description/agenda of the meeting.`;
}

// ---- Continuation: judge whether a message answers a pending confirmation ----
// Used while a session is open. The brain sees EVERY message from the awaited
// party and must ignore normal chatter, acting only on a real yes/no.
export function buildConfirmSystem(action) {
  return `You decide whether the LATEST message is a response to a pending confirmation.
The assistant asked to confirm: ${action}.
Use the recent conversation only as context; judge ONLY the latest message.
Reply with ONLY valid JSON, no text around it: {"decision":"confirm"|"decline"|"unrelated"}
- "confirm": the latest message clearly agrees to proceed (e.g. yes, confirm, go ahead, sim, pode, isso).
- "decline": the latest message clearly refuses (e.g. no, don't, keep it, não, deixa).
- "unrelated": the latest message is normal conversation, NOT a reply to this confirmation. If unsure, choose "unrelated".`;
}

export function buildConfirmUser({ transcript, latest }) {
  return `Recent conversation:
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
