// ============================================================================
//  Skill "Feedback" (the owner reports a mistake) — LOGIC.
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
//  confirm in one line. No slot-filling, no doc render.
//
//  Flow:
//    @secretary you made a mistake here  (ideally REPLYING to the bad message)
//       -> extract (1 structured call) -> WRITE THE REPORT -> confirm
//       -> if the note was vague AND nothing was quoted: ask ONE question, keep the path
//    owner's answer -> append it to the report that already exists -> done
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description }
//    export async function run(ctx)
// ============================================================================
import {
  buildFeedbackSchema,
  buildFeedbackSystem,
  buildFeedbackUser,
  reply,
} from "./prompt.js";
import { isOwnMessage } from "../../1. Orchestrator/lib/identity.js";
import { jsonFormat, readReply } from "../../1. Orchestrator/lib/llm.js";
import {
  captureFailure,
  appendToReport,
} from "../../1. Orchestrator/lib/selflearning.js";

// `inputs: null` — NO declared inputs (see 1. Orchestrator/lib/inputs.js). This skill re-reads
// the conversation itself, so there is nothing for the router's merged call to pre-extract, and
// nothing may be handed to it: a task with no declaration is never given a payload.
export const manifest = {
  id: "feedback",
  inputs: null,
  description:
    "the owner is telling you that YOU, the secretary, did something WRONG — a mistake, a " +
    "false positive, a wrong answer, bad behaviour. Use for 'you made a mistake', 'that's " +
    "wrong', 'you got X wrong', 'that shouldn't have happened', 'você errou'. This is about " +
    "a DEFECT in the secretary's own past output, and it gets FILED for investigation. " +
    "NOT for asking to build something new (feature_request), NOT for a fresh calendar/task " +
    "order (calendar_action/task_action). If he ALSO wants it fixed now, return this AND the " +
    "skill that does the fixing.",
};

const SESSION_TTL = 900; // 15-min window for the single follow-up question

// ---- Extraction: the owner's claim, restated for an engineer ------------------
async function extract(ctx, { quotedText, quotedIsSecretary }) {
  const { anthropic, model, owner, order, transcript, nowStr, catalog } = ctx;
  const msg = await anthropic.messages.create({
    model,
    max_tokens: 1000,
    system: buildFeedbackSystem(owner, catalog),
    output_config: jsonFormat(buildFeedbackSchema(catalog)),
    messages: [
      {
        role: "user",
        content: buildFeedbackUser(owner, {
          order,
          quotedText,
          quotedIsSecretary,
          transcript,
          nowStr,
        }),
      },
    ],
  });
  const out = readReply(msg, "feedback");
  console.log("FEEDBACK EXTRACT:", JSON.stringify(out));
  return out;
}

export async function run(ctx) {
  const { session } = ctx;
  // CONTINUATION: his answer to the one clarifying question.
  if (session?.skill === "feedback" && session.stage === "clarifying") {
    return resumeFeedback(ctx, session);
  }
  return startFeedback(ctx);
}

async function startFeedback(ctx) {
  const { number, send, quoted } = ctx;

  // The gold path: he REPLIED to the offending message, so we have the secretary's wrong
  // output verbatim. isOwnMessage() is what tells a secretary message apart from his own —
  // they share a WhatsApp account, so the header is the only signal (lib/identity.js).
  const quotedText = quoted?.text || null;
  const quotedIsSecretary = !!(quotedText && isOwnMessage(quotedText));

  // Best-effort. A failed extraction must NOT cost us the complaint: we file the raw note
  // instead. A report the triage agent has to work harder on beats one that never existed.
  let out = null;
  try {
    out = await extract(ctx, { quotedText, quotedIsSecretary });
  } catch (e) {
    console.error("feedback/extract error:", e?.message || e);
  }

  const title = out?.title || "owner-reported mistake (unclassified)";

  // WRITE FIRST, ASK SECOND — the ordering is the whole design.
  // The obvious flow (ask -> wait -> file on his answer) LOSES the complaint: server.js
  // clears any open session on the next tagged order, so if he gets distracted and types
  // "@secretary schedule lunch", the session — and his bug report — vanish silently. He'd
  // never know, and he'd stop reporting after the second one that went nowhere.
  const reportPath = await captureFailure(ctx, {
    phase: "reported",
    taskId: out?.suspected_skill || "feedback",
    report: {
      note: ctx.order,
      whatWentWrong: out?.what_went_wrong || null,
      expected: out?.expected || null,
      quotedText,
      quotedIsSecretary,
    },
  });

  if (!reportPath) {
    // Deduped is impossible here (reported is exempt), so this is a real write failure or
    // the disk backstop. Either way: never confirm a note that isn't on disk.
    await send(number, reply(ctx.lang).logFailed());
    return;
  }

  // Vague note AND nothing quoted -> one question, exactly once. The report already exists;
  // his answer only ENRICHES it. Someone annoyed enough to report a bug will not sit through
  // an interview about it, so there is no second question, ever.
  if (!quotedText && out?.enough_context === false) {
    await ctx.sessions.set(
      ctx.remoteJid,
      {
        skill: "feedback",
        intent: "clarify-mistake",
        stage: "clarifying",
        awaitFrom: "owner",
        lang: ctx.lang,
        reportPath,
      },
      SESSION_TTL
    );
    await send(number, reply(ctx.lang).loggedAndAsk());
    return;
  }

  await send(number, reply(ctx.lang).logged({ title }));
}

// ---- His answer to the one question: append it to the existing report ---------
async function resumeFeedback(ctx, session) {
  const { number, send, sessions, remoteJid, order, nowStr } = ctx;

  const section = `## Owner's follow-up (${nowStr})
He was asked which message was wrong and what it should have said. His answer:

> ${String(order).split("\n").join("\n> ")}`;

  const ok = await appendToReport(session.reportPath, section);

  if (!ok) {
    // The append failed (file moved by a sync, disk error). Do NOT drop his answer: file it
    // as its own report, linked back to the first one so triage can merge them.
    await captureFailure(ctx, {
      phase: "reported",
      taskId: "feedback",
      report: {
        note: order,
        whatWentWrong: `Follow-up detail for an earlier report that could not be appended to (${session.reportPath}).`,
        expected: null,
        followUpTo: session.reportPath,
        quotedText: null,
        quotedIsSecretary: false,
      },
    });
  }

  await sessions.clear(remoteJid);
  await send(number, reply(ctx.lang).enriched());
}
