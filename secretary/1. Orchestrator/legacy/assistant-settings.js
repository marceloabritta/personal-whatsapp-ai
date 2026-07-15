// ============================================================================
//  legacy/assistant-settings.js  —  FROZEN. Verbatim copy of the "7. Assistant Settings"
//  skill.js as it was at HEAD (commit before card 55e00052): the self-driven propose /
//  classifyConfirmation / resumeConfirm flow that this card deleted. It is dispatched ONLY
//  by the legacy (@assistant / OLD) path — server.js swaps it in as LEGACY_SKILLS
//  .assistant_settings — so @assistant's tag-change behaviour is byte-for-byte the committed
//  behaviour. It mutates the LEGACY TAGS array (setTags), never the @mary NEW_TAGS. The NEW
//  (@mary) flow uses the converted "7. Assistant Settings/skill.js". Do NOT edit.
// ============================================================================
//  Skill "Assistant Settings" — CHANGE HOW THE OWNER SUMMONS HER.
//  "@assistant, change your tag to @assist" — she deduces whether the other language's
//  call should change too, states the reasoning and the COMPLETE new tag list in prose,
//  and asks. On yes: applied live, persisted, and the old tags stop working.
//
//  Confirm-first on the existing rails (lib/confirm.js + ctx.sessions), like every other
//  write-flow in the product. Two things here are deliberate and load-bearing:
//
//  1. SHE PROPOSES FROM THE LIVE TAGS (ctx.tags), never from process.env. Change the tag a
//     second time and she reasons about the tags actually in force, not the boot seed.
//
//  2. SUCCESS IS ONLY EVER REPORTED BY THE CODE PATH THAT WROTE THE STORE. saveTags()
//     returns true only on a real write; if it merely reached the memory fallback she says
//     so — the change is live but unsaved, and a restart will undo it. She never claims a
//     persistence she did not get.
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description }
//    export async function run(ctx)
//  Localized scaffolding lives in prompt.js (reply(ctx.lang)); the REASONING is generated
//  in-language by the model. ctx.send is pre-bound to the conversation language.
// ============================================================================
import {
  buildProposeSystem,
  buildProposeUser,
  PROPOSE_SCHEMA,
  reply,
  fmtTags,
} from "./assistant-settings-prompt.js";
import { jsonFormat, readReply } from "../lib/llm.js";
import { classifyConfirmation } from "../lib/confirm.js";
import { normalizeTags, setTags } from "../lib/identity.js";

// `inputs: null` — NO declared inputs (see 1. Orchestrator/lib/inputs.js). The proposal call
// needs the LIVE tag list to reason against, which the router has no business pre-extracting,
// so there is nothing to hand over: a task with no declaration is never given a payload.
export const manifest = {
  id: "assistant_settings",
  inputs: null,
  description:
    "change HOW THE OWNER SUMMONS THE ASSISTANT — the trigger tag(s) he types to call her: " +
    "'change your tag to @assist', 'I want to call you @x', 'stop answering to @assistant'. " +
    "She proposes the complete new tag list and applies it only on a yes. NOT for a feature " +
    "idea about the assistant (feature_request), a to-do (task_action) or scheduling " +
    "(calendar_action).",
};

// The confirmation window she NAMES in the proposal. Keep the two in step (prompt.js).
const SESSION_TTL = 900; // 15 min

export async function run(ctx) {
  const { session } = ctx;

  // CONTINUATION owned by this skill (set by the orchestrator on an untagged follow-up).
  if (
    session?.skill === "assistant_settings" &&
    session.stage === "await_confirmation"
  )
    return resumeConfirm(ctx, session);

  // FRESH (tagged) order.
  return propose(ctx);
}

// ---- Fresh order: reason about the tags, propose, open the confirmation ------
async function propose(ctx) {
  const {
    anthropic,
    model,
    owner,
    order,
    transcript,
    tags,
    lang,
    number,
    send,
    sessions,
    remoteJid,
  } = ctx;

  // ctx.tags is the LIVE array by reference — snapshot it so what she reasons from and what
  // she quotes back are the same list, whatever happens to it later in the turn.
  const current = [...tags];

  let out;
  try {
    const msg = await anthropic.messages.create({
      model,
      max_tokens: 1000,
      system: buildProposeSystem(owner, lang),
      output_config: jsonFormat(PROPOSE_SCHEMA),
      messages: [
        {
          role: "user",
          content: buildProposeUser({ currentTags: current, order, transcript }),
        },
      ],
    });
    out = readReply(msg, "assistant_settings");
  } catch (e) {
    console.error("assistant_settings/propose error:", e?.message || e);
    await ctx.sendFailure(number, reply(lang).thinkingError());
    return;
  }
  if (!out) {
    await ctx.sendFailure(number, reply(lang).thinkingError());
    return;
  }
  console.log("SETTINGS PROPOSE RAW:", JSON.stringify(out));

  // Same validation the store enforces — she can never PROPOSE something she could not apply.
  const { ok, tags: next, problem } = normalizeTags(out.tags);
  if (!ok) {
    // GUIDANCE, not a malfunction: she understood the order and is asking again, and nothing
    // was applied. Plain send() — ctx.sendFailure would file this as a bug report (server.js:345).
    await send(number, reply(lang).invalid({ problem, current }));
    return;
  }

  await sessions.set(
    remoteJid,
    {
      skill: "assistant_settings",
      stage: "await_confirmation",
      awaitFrom: "owner",
      lang,
      data: { tags: next },
    },
    SESSION_TTL
  );

  await send(
    number,
    reply(lang).propose({ reasoning: out.reasoning, tags: next })
  );
}

// ---- The follow-up: yes / no / neither --------------------------------------
async function resumeConfirm(ctx, session) {
  const { number, send, sessions, remoteJid, lang, settings, tags } = ctx;

  const next = session.data?.tags || [];
  if (!next.length) {
    await sessions.clear(remoteJid); // nothing pending; drop the stale session
    return;
  }

  const decision = await classifyConfirmation(ctx, {
    action: `change the tag(s) the owner summons the assistant with to ${next.join(", ")}`,
    who: "settings",
  });

  // The SAFE no-op: normal chatter, or any doubt at all. The proposal stands until it expires.
  if (decision === "unrelated") return;

  if (decision === "decline") {
    await sessions.clear(remoteJid);
    await send(number, reply(lang).declined({ current: [...tags] }));
    return;
  }

  // CONFIRM. Snapshot the OUTGOING tags before they are replaced — ctx.tags is the live array,
  // so after setTags() there is nothing left to tell him he can no longer use.
  const retired = [...tags].filter((t) => !next.includes(t));

  // The store first, then live. `persisted` is the ONLY thing that decides what she claims.
  const persisted = await settings.saveTags(next);
  setTags(next);
  await sessions.clear(remoteJid);

  console.log(
    `settings: tags -> ${next.join(", ")} (persisted: ${persisted}, retired: ${
      retired.join(", ") || "none"
    })`
  );

  await send(
    number,
    persisted
      ? reply(lang).applied({ tags: next, retired })
      : reply(lang).appliedNotSaved({ tags: next, retired })
  );
}
