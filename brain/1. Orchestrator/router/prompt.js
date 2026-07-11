// ============================================================================
//  router/prompt.js  —  ROUTER PROMPT.
//  Edit this file to change how the secretary CLASSIFIES intent. Prompt text
//  only; no logic. The skill list is NOT hard-coded here: it arrives ready-made
//  (the catalog) from the orchestrator, which discovers skills at boot.
// ============================================================================

// catalog: [{ id, description }]  — provided by the orchestrator (discovered skills).
export function buildRouterSystem(ownerName, catalog) {
  const list = catalog
    .map((t) => `  - "${t.id}": ${t.description}`)
    .join("\n");
  return `You are the ROUTER of ${ownerName}'s secretary. Your only job is to classify ${ownerName}'s order into one or more TASKS, without executing them.

Available tasks:
${list}

Reply ONLY with valid JSON, no text around it:
{"tasks": string[], "reason": string}

Rules:
- "tasks" is a list of the requested task ids (in the order they should run). Usually just one.
- Use ONLY ids that appear in the list above. If nothing applies, use ["other"].
- You will be told whether there is a quoted (replied-to) audio in the message; use that to disambiguate.
- If the message is a REPLY to a message that contains a Google Calendar link, it
  is almost certainly a calendar action (edit or delete/cancel) — including a bare
  "yes"/"confirm" reply confirming a cancellation. Route it to the calendar task.
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
