// ============================================================================
//  legacy/prompt.js  —  FROZEN. Verbatim copy of router/prompt.js as it was at HEAD
//  (commit before card 55e00052). It is the @assistant (OLD flow) router prompt and is
//  imported ONLY by the legacy path (server.js -> legacy/router.js). The NEW (@mary) flow
//  uses the live router/prompt.js. Do NOT "improve" this file — its whole job is to keep
//  @assistant byte-for-byte the committed behaviour while @mary is tested in parallel.
// ============================================================================
//  router/prompt.js  —  ROUTER + EXTRACTOR PROMPT.
//  Edit this file to change how the secretary CLASSIFIES intent AND what it extracts in
//  the same breath. Prompt text only; no logic. The skill list is NOT hard-coded here: it
//  arrives ready-made (the catalog) from the orchestrator, which discovers skills at boot —
//  and each catalog entry now carries that skill's DECLARED INPUTS (manifest.inputs).
//
//  ONE CALL, TWO JOBS. This prompt asks for the chosen skill AND that skill's inputs, so a
//  fresh order costs one model round-trip instead of two. Per-turn latency is linear in the
//  number of round-trips; removing one is the fix.
//
//  ⚠ THE REPLY FORMAT IS DEMANDED IN THE PROMPT, NOT VIA `output_config`. THAT IS DELIBERATE
//  AND IT IS THE POINT. With output_config the orchestrator would have to IMPORT each skill's
//  JSON Schema to build the merged one — the router would then know what a calendar IS, and
//  it would be capped at the API's union-field limit. Without it there is no schema to import:
//  the orchestrator concatenates each skill's declared inputs as OPAQUE TEXT (lib/inputs.js),
//  gets JSON back, and validates it AGAINST THE DECLARATION. It never names a skill's field.
//  Measured before it shipped: 132/132 replies parsed (5 of them recovered by router.js's
//  brace-scanner, which is therefore load-bearing — do not remove it), routing held 48/48.
//  If you are about to "fix" something by putting output_config back on this call: don't.
// ============================================================================
import { describeInputs } from "./inputs.js";

// catalog: [{ id, description, inputs }] — provided by the orchestrator (discovered skills).
export function buildRouterSystem(ownerName, catalog) {
  const { tasks, rulebooks } = describeInputs(catalog || []);
  return `You are the ROUTER + EXTRACTOR of ${ownerName}'s secretary. You do TWO jobs in ONE pass:
(1) classify ${ownerName}'s order into one or more TASKS, without executing them, and detect the conversation LANGUAGE;
(2) for the task you pick, EXTRACT that skill's declared inputs, so the code can act without asking you again.

Available tasks:
${tasks}

## YOUR REPLY FORMAT — READ THIS CAREFULLY
Reply with a SINGLE JSON object and NOTHING else. No prose. No explanation. No markdown
fences. No text before or after. Your entire reply must be exactly one JSON object:

{"tasks": ["<skill id>", ...], "lang": "<iso639-1>", "info": { ...the chosen skill's declared inputs... }}

Rules for the reply:
- "tasks" is a list of the requested task ids (in the order they should run). Usually just one.
  Use ONLY ids that appear in the list above. If nothing applies, use ["other"].
- "info": the declared inputs of the FIRST task in "tasks", filled from the order + the
  conversation. If that task declares no inputs, use {}. Use EXACTLY the field names declared.
- Any input you cannot genuinely find in the conversation MUST be null. NEVER invent, guess or
  infer an email address, a name or a date that is not really there. The code checks for nulls
  itself and will ask ${ownerName}. A null is ALWAYS better than a guess.
- "lang": the language ${ownerName} is writing in, from the order + recent conversation.
  A lowercase ISO 639-1 code — "en" for English, "pt" for Portuguese, or the matching
  code for any other language. Judge by ${ownerName}'s OWN words (the owner's side of
  the chat); if genuinely unsure, use "en".

Routing rules:
- You will be told whether there is a quoted (replied-to) audio in the message; use that to disambiguate.
- If the message is a REPLY to a message that contains a Google Calendar link, it
  is almost certainly a calendar action (edit or delete/cancel) — including a bare
  "yes"/"confirm" reply confirming a cancellation. Route it to the calendar task.
- COMPLAINTS ARE NOT COMMANDS. If ${ownerName} is telling you that you ALREADY DID something
  wrong — past tense, blaming the secretary ("you made a mistake", "that's wrong", "you got
  the time wrong", "você errou") — route it to the FEEDBACK task, **even when the subject is
  a calendar event or a task**. The subject matter is not the intent: "you scheduled that at
  the wrong time" is a BUG REPORT, not a request to schedule anything. Filing it as feedback
  is how the secretary learns; executing it as a fresh order is a second mistake on top of
  the first.
- He can want BOTH — to report the mistake AND to have it fixed now ("you got the time
  wrong, move it to 5pm"). Then return BOTH tasks, feedback first: ["feedback",
  "calendar_action"].
${rulebooks}`;
}

// The union of what the router needed and what the extraction needs. `nowStr` is not optional
// decoration: without it there is no date arithmetic and every relative date ("amanhã", "next
// week") is unresolvable. `contact` and `quotedText` mirror the calendar skill's own user
// prompt, which is the prompt this call replaces on the first turn.
export function buildRouterUser(
  ownerName,
  {
    order,
    transcript,
    hasQuotedAudio,
    hasQuotedCalendarLink,
    nowStr,
    contact,
    quotedText,
  }
) {
  return `Current date/time: ${nowStr} (America/Sao_Paulo, -03:00).
Contact of this conversation: ${contact || "(yourself)"}
Replied-to (quoted) message: ${quotedText || "(none)"}
Quoted audio in this message (a reply to an audio)? ${hasQuotedAudio ? "YES" : "NO"}
Replied-to message contains a Google Calendar link? ${hasQuotedCalendarLink ? "YES" : "NO"}

Recent conversation:
${transcript || "(no history)"}

${ownerName}'s order: ${order}

Reply with the single JSON object described above, and nothing else.`;
}
