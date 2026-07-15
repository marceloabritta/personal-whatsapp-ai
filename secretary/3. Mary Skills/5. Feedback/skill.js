// ============================================================================
//  Skill "Feedback" (the owner reports a mistake) — LOGIC.  CONVERTED (pure task).
//
//  The only trigger in the self-learning system that a HUMAN pulls. The other four
//  (throw:*, unrouted, soft — see 1. Orchestrator/lib/selflearning.js) only fire when the
//  code KNOWS it failed. The failures that matter most are invisible to it: a false
//  positive, a confidently wrong answer, a task filed under the wrong date. Nothing throws,
//  nothing says "I couldn't" — the only detector is the owner reading the message and
//  thinking "that's wrong". This skill is that detector's front door, and its reports are
//  the highest-signal ones in the system because they are HUMAN-VERIFIED, not heuristic.
//
//  Deliberately the thinnest skill in the repo: gather evidence -> captureFailure() ->
//  confirm in one line. In the NEW (@mary) flow the ORCHESTRATOR runs the conversation: it
//  restates the complaint into the declared inputs (note / what_went_wrong / expected /
//  suspected_skill) and asks the one clarifying question — on a `listen` turn — BEFORE it
//  dispatches this skill. So run() no longer extracts, asks, or opens a session: it WRITES
//  the report and RETURNS what it filed, for the model to read back.
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description, conversation, inputs }
//    export async function run(ctx) -> a JSON-serializable value (the read-back)
// ============================================================================
import { reply } from "./prompt.js";
import { isOwnMessage } from "../../1. Orchestrator/lib/identity.js";
import {
  captureFailure,
  appendToReport,
} from "../../1. Orchestrator/lib/selflearning.js";

// `inputs` — the DECLARED input contract (see 1. Orchestrator/lib/inputs.js). The orchestrator
// restates the owner's complaint into these fields in the same call that classifies the order,
// and gates on `ok` (all three tiers) before dispatching — so ctx.info arrives shape- and
// consistency-valid. `note` is the substance and is NOT nullable; the rest sharpen the report.
export const manifest = {
  id: "feedback",
  // CONVERTED (pure task): the model runs the dialogue; run() only files + returns.
  conversation: "orchestrator",
  inputs: {
    discriminator: null,
    fields: {
      note: {
        type: "string",
        desc: "the owner's complaint restated plainly in English — WHAT he says went wrong",
      },
      what_went_wrong: {
        type: "string",
        nullable: true,
        desc: "the symptom the secretary actually produced, restated for an engineer; null if unclear",
      },
      expected: {
        type: "string",
        nullable: true,
        desc: "what the owner says should have happened instead; null if he didn't say",
      },
      suspected_skill: {
        type: "string",
        nullable: true,
        desc: "the skill id most likely responsible (calendar_action, task_action, …), or null if you can't tell",
      },
    },
    requiredWhen: {},
    consistency: [
      {
        name: "note_is_not_blank",
        test: (i) => !!i.note && String(i.note).trim() !== "",
      },
    ],
  },
  description:
    "the owner is telling you that YOU, the secretary, did something WRONG — a mistake, a " +
    "false positive, a wrong answer, bad behaviour. Use for 'you made a mistake', 'that's " +
    "wrong', 'you got X wrong', 'that shouldn't have happened', 'você errou'. This is about " +
    "a DEFECT in the secretary's own past output, and it gets FILED for investigation. " +
    "NOT for asking to build something new (feature_request), NOT for a fresh calendar/task " +
    "order (calendar_action/task_action).",
};

// The model has already restated the complaint (ctx.info) and, when the note was vague and
// nothing was quoted, asked its one clarifying question on a listen turn. run() files the report
// and returns. WRITE FIRST is preserved: the orchestrator never opens a skill session, so there
// is no ask-then-wait that a later tagged order could silently clear — the report is on disk the
// moment the owner's complaint is dispatched.
//
// `appendToReport` is imported per the plan (§B5) — the enrich-an-existing-report follow-up now
// lives with the orchestrator's conversation, so it is not called from here.
export async function run(ctx) {
  const { number, quoted, lang } = ctx;
  const info = ctx.info || {};

  // The gold path: he REPLIED to the offending message, so we have the secretary's wrong output
  // verbatim. isOwnMessage() is what tells a secretary message apart from his own — they share a
  // WhatsApp account, so the header is the only signal (lib/identity.js).
  const quotedText = quoted?.text || null;
  const quotedIsSecretary = !!(quotedText && isOwnMessage(quotedText));

  const title = String(info.what_went_wrong || info.note || "owner-reported mistake").trim();

  const reportPath = await captureFailure(ctx, {
    phase: "reported",
    taskId: info.suspected_skill || "feedback",
    report: {
      note: info.note,
      whatWentWrong: info.what_went_wrong || null,
      expected: info.expected || null,
      quotedText,
      quotedIsSecretary,
    },
  });

  if (!reportPath) {
    // Deduped is impossible here (reported is exempt), so this is a real write failure or the
    // disk backstop. Either way: never confirm a note that isn't on disk.
    await ctx.sendFailure(number, reply(lang).logFailed());
    return { ok: false, reason: "logFailed" };
  }

  await ctx.send(number, reply(lang).logged({ title }));
  return { ok: true, reportPath, title };
}
