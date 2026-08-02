// ============================================================================
//  Skill "Assistant Settings" — CHANGE HOW THE OWNER SUMMONS HER.
//  "@assistant, change your tag to @assist" — the ORCHESTRATOR runs the dialogue now: it
//  reasons about the other language's call, proposes the complete new tag list, and gets his
//  agreement before it dispatches this skill. By the time run() is called, the model has already
//  proposed and he has already agreed.
//
//  CONVERTED SKILL (conversation: "orchestrator"). It does NOT ask, NOT confirm, NOT extract:
//    - the model runs the conversation and hands the validated payload in ctx.info;
//    - run() does exactly two things — it ACTS (saves + applies the tags), and it SENDS exactly
//      one outcome message. Then it RETURNS what it did, for the model to read back.
//
//  Two things here are deliberate and load-bearing:
//
//  1. SHE APPLIES FROM THE LIVE TAGS (ctx.tags), never from process.env — so the "retired" list
//     she reports is computed against the tags actually in force.
//
//  2. SUCCESS IS ONLY EVER REPORTED BY THE CODE PATH THAT WROTE THE STORE. saveTags() returns
//     true only on a real write; if it merely reached the memory fallback she says so — the
//     change is live but unsaved, and a restart will undo it. She never claims a persistence she
//     did not get.
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description, conversation, inputs }
//    export async function run(ctx) -> a JSON-serializable value (the read-back)
//  Localized OUTCOME strings live in prompt.js (reply(ctx.lang)); the conversational prose (the
//  proposal, the reasoning) is written by the model, in-language, and never enters the repo.
// ============================================================================
import { reply } from "./prompt.js";
// setNewTags (NOT setTags): this converted skill is the NEW (@mary) flow's pilot, and it must
// mutate the NEW_TAGS list only. Using setTags here would change the LEGACY (@assistant) summon
// tag from the new flow — the exact cross-flow leak the parallel run must never allow. ctx.tags
// and ctx.settings are already the new flow's (server.js builds ctx per-flow).
import { normalizeTags, setNewTags } from "../../1. Orchestrator/lib/identity.js";

// `conversation: "orchestrator"` — the model runs the dialogue; this skill just acts and returns.
// `inputs` declares the ONE thing it needs: the COMPLETE new tag list (an array of strings). The
// orchestrator extracts it in the turn call and gates it on `ok` (all three tiers) before it ever
// dispatches this skill, so ctx.info is guaranteed shape- and consistency-valid on arrival.
export const manifest = {
  id: "assistant_settings",
  conversation: "orchestrator",
  inputs: {
    discriminator: null,
    fields: {
      tags: {
        type: "array",
        of: { type: "string" },
        desc: "the COMPLETE new list of trigger tags in force after the change, each with its leading @",
      },
    },
    requiredWhen: {},
    consistency: [
      { name: "tags are valid trigger tags", test: (info) => normalizeTags(info.tags).ok },
    ],
    rulebook: () =>
      "Return the COMPLETE tag list the owner should summon the assistant with afterwards, never a delta. " +
      "Each tag starts with @, is lowercase, has no spaces, and is at least 3 chars. Carry over every tag " +
      "he did not ask to retire. If a new tag is a natural short form of the tags in more than one language, " +
      "you may collapse to it and retire the old ones — but say so before you execute.",
  },
  description:
    "change HOW THE OWNER SUMMONS THE ASSISTANT — the trigger tag(s) he types to call her: " +
    "'change your tag to @assist', 'I want to call you @x', 'stop answering to @assistant'. " +
    "She proposes the complete new tag list and applies it only on a yes. NOT for a feature " +
    "idea about the assistant (feature_request), a to-do (task_action) or scheduling " +
    "(calendar_action).",
};

// The model has already proposed and he has already agreed (the orchestrator gated on `ok` before
// dispatching). run() validates defensively, applies live, persists, reports ONE outcome, RETURNS.
export async function run(ctx) {
  const { number, lang, settings } = ctx;

  const tags = ctx.info?.tags; // the orchestrator hands the validated payload
  const norm = normalizeTags(tags);
  if (!norm.ok) {
    // ctx.info is guaranteed valid by the `ok` gate; this is belt-and-braces. A failure here is a
    // genuine malfunction, so it goes through sendFailure (declared, self-learning captures it).
    await ctx.sendFailure(number, reply(lang).thinkingError());
    return { ok: false };
  }

  // Snapshot the OUTGOING tags BEFORE setTags replaces them — ctx.tags is the live array.
  const retired = [...ctx.tags].filter((t) => !norm.tags.includes(t));

  // The store first, then live. `persisted` is the ONLY thing that decides what she claims.
  // settings is the NEW flow's namespaced store; setNewTags mutates NEW_TAGS — neither touches
  // the legacy (@assistant) tag or its store.
  const persisted = await settings.saveTags(norm.tags);
  setNewTags(norm.tags);

  console.log(
    `settings: tags -> ${norm.tags.join(", ")} (persisted: ${persisted}, retired: ${
      retired.join(", ") || "none"
    })`
  );

  await ctx.send(
    number,
    persisted
      ? reply(lang).applied({ tags: norm.tags, retired })
      : reply(lang).appliedNotSaved({ tags: norm.tags, retired })
  );

  return { ok: true, persisted, tags: norm.tags, retired };
}
