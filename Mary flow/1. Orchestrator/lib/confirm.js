// ============================================================================
//  Confirm-first: the shared yes/no/unrelated classifier.
//
//  Every write-flow in the secretary is confirm-first: propose the action, open a
//  session, then read EVERY message from the awaited party and decide whether it
//  answers the pending question. calendar_action and task_action each carried a
//  near-identical copy of this (schema + prompts + call); it lives here now.
//
//  The three outcomes:
//    "confirm"   — go ahead and apply the pending action.
//    "decline"   — drop it (the caller clears the session).
//    "unrelated" — normal chatter or a NEW request; do NOT touch the pending action.
//                  This is the SAFE default: any doubt, refusal or API error lands
//                  here, so an unclear message can never trigger an irreversible write.
//
//  The caller still owns the session (what's pending, its TTL, what to do on each
//  outcome) — this module only reads the latest message.
// ============================================================================
import { jsonFormat, readReply } from "./llm.js";

export const CONFIRM_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["decision"],
  properties: {
    decision: { type: "string", enum: ["confirm", "decline", "unrelated"] },
  },
};

// `action` is a short English description of what's pending ("cancel the 15:00
// meeting with Ana", "apply 2 changes to the owner's tasks") — it goes in the prompt
// so the model knows what a "yes" would be agreeing to.
export function buildConfirmSystem(action) {
  return `You decide whether the LATEST message is a response to a pending confirmation.
The assistant asked to confirm: ${action}.
Use the recent conversation only as context; judge ONLY the latest message.
Decide one "decision" value — "confirm", "decline", or "unrelated":
- "confirm": the latest message clearly agrees to proceed (e.g. yes, confirm, go ahead, sim, pode, isso).
- "decline": the latest message clearly refuses (e.g. no, don't, leave it, keep it, não, deixa).
- "unrelated": the latest message is normal conversation OR a NEW request, NOT a yes/no to this confirmation. If unsure, choose "unrelated".`;
}

export function buildConfirmUser({ transcript, latest }) {
  return `Recent conversation:
${transcript || "(none)"}

Latest message: ${latest}`;
}

// Does the latest message confirm/decline the pending action? Returns
// "confirm" | "decline" | "unrelated", defaulting to "unrelated" on doubt or error
// (the safe no-op). `who` only labels the log line.
export async function classifyConfirmation(ctx, { action, who = "confirm" }) {
  const { anthropic, model, transcript, order } = ctx;
  try {
    const msg = await anthropic.messages.create({
      model,
      max_tokens: 256,
      system: buildConfirmSystem(action),
      output_config: jsonFormat(CONFIRM_SCHEMA),
      messages: [
        { role: "user", content: buildConfirmUser({ transcript, latest: order }) },
      ],
    });
    const decision = readReply(msg, who)?.decision;
    console.log(`${who}: CONFIRM RAW: ${decision}`);
    return decision === "confirm" || decision === "decline"
      ? decision
      : "unrelated";
  } catch (e) {
    console.error(`${who}: confirm classify error:`, e?.message || e);
    return "unrelated"; // on error, do nothing (safe)
  }
}
