// ============================================================================
//  router/prompt.js  —  ROUTER PROMPT.
//  Edit this file to change how the secretary CLASSIFIES intent. Prompt text
//  only; no logic. The skill list is NOT hard-coded here: it arrives ready-made
//  (the catalog) from the orchestrator, which discovers skills at boot.
// ============================================================================

// ---- JSON Schema for structured outputs (output_config.format) ---------------
// The router is schema-enforced like the calendar calls: the API returns ONLY
// valid, schema-conforming JSON. `lang` carries the detected conversation language
// so the whole system can reply in it (see server.js send()/ctx.lang). Rules:
// every object needs additionalProperties:false + a full `required` list.
export const ROUTER_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["tasks", "lang", "reason"],
  properties: {
    tasks: { type: "array", items: { type: "string" } },
    lang: { type: "string" },
    reason: { type: "string" },
  },
};

// catalog: [{ id, description }]  — provided by the orchestrator (discovered skills).
export function buildRouterSystem(ownerName, catalog) {
  const list = catalog
    .map((t) => `  - "${t.id}": ${t.description}`)
    .join("\n");
  return `You are the ROUTER of ${ownerName}'s secretary. Your only job is to classify ${ownerName}'s order into one or more TASKS, without executing them, and to detect the conversation LANGUAGE.

Available tasks:
${list}

Your reply's shape is enforced separately; focus on the values:
{"tasks": string[], "lang": string, "reason": string}

Rules:
- "tasks" is a list of the requested task ids (in the order they should run). Usually just one.
- Use ONLY ids that appear in the list above. If nothing applies, use ["other"].
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
  "calendar_action"]. Reporting a mistake never fixes anything on its own, and asking for the
  fix is just an ordinary second order.
- "lang": the language ${ownerName} is writing in, from the order + recent conversation.
  A lowercase ISO 639-1 code — "en" for English, "pt" for Portuguese, or the matching
  code for any other language. Judge by ${ownerName}'s OWN words (the owner's side of
  the chat); if genuinely unsure, use "en".
- "reason": a short sentence explaining the choice.`;
}

export function buildRouterUser(
  ownerName,
  { order, transcript, hasQuotedAudio, hasQuotedCalendarLink }
) {
  return `Quoted audio in this message (a reply to an audio)? ${hasQuotedAudio ? "YES" : "NO"}
Replied-to message contains a Google Calendar link? ${hasQuotedCalendarLink ? "YES" : "NO"}

Recent conversation:
${transcript || "(no history)"}

${ownerName}'s order: ${order}`;
}
