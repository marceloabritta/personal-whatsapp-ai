// ============================================================================
//  legacy/assistant-settings-prompt.js  —  FROZEN. Verbatim copy of the "7. Assistant
//  Settings" prompt.js as it was at HEAD (commit before card 55e00052). Its PROPOSE_SCHEMA /
//  buildProposeSystem / buildProposeUser and the propose/declined reply keys are used ONLY by
//  the frozen legacy/assistant-settings.js (the @assistant / OLD flow). Do NOT edit.
// ============================================================================
//  Skill "Assistant Settings" — PROMPTS + localized scaffolding.
//
//  ONE model call: read the owner's order against the tags he ACTUALLY has right now, and
//  return (a) the COMPLETE new tag list and (b) the REASONING, in prose, that he will read
//  before he says yes.
//
//  The reasoning is the load-bearing half. "Change your tag to @assist" does not say what
//  should happen to the PT call he also uses — and there is no tag→language data model in
//  this product to look it up in. The model reasons about it in the moment, from the tag
//  strings themselves, and SAYS SO. He confirms a proposal he can see, not a guess he can't.
// ============================================================================

// The complete list, never a delta. `reasoning` is prose, in HIS language.
export const PROPOSE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["tags", "reasoning"],
  properties: {
    tags: {
      type: "array",
      items: { type: "string" },
      description:
        "The COMPLETE list of tags the assistant should answer to afterwards. Not a delta.",
    },
    reasoning: {
      type: "string",
      description:
        "Prose, first person, addressed to the owner, in HIS language. Why this list — including what you decided about the other language's call, and why.",
    },
  },
};

export function buildProposeSystem(owner, lang) {
  return `You are ${owner}'s WhatsApp AI assistant. He summons you by starting a message with one of your TRIGGER TAGS, and he is now asking you to CHANGE them.

Your job: work out the COMPLETE list of tags you should answer to afterwards, and explain your reasoning to him in prose.

THE LIST YOU RETURN REPLACES THE CURRENT LIST OUTRIGHT. Any tag you leave out STOPS WORKING — there is no alias and no grace period. So carry over every tag he did not ask you to retire.

THE JUDGEMENT HE IS ASKING YOU TO MAKE. He usually has more than one tag because he writes to you in more than one language (e.g. "@assistant" in English, "@assistente" in Portuguese). When he changes one, DEDUCE whether the other language's call should change too, and say why:
- If the new tag is a natural short form of BOTH (e.g. "@assist" for "@assistant"/"@assistente"), one tag can reasonably serve both languages — propose collapsing to it and retiring both old ones.
- If the new tag is clearly language-specific (e.g. he replaces "@assistant" with "@buddy"), the other language's call probably stands — keep it.
- If he explicitly says which tags to keep or drop, do exactly that. His instruction always beats your deduction.
There is no right answer stored anywhere; you are inferring it from the words. That is exactly why you state the reasoning and he confirms.

TAG RULES (a proposal that breaks one is rejected and you have to ask again):
- every tag starts with "@", is lowercase, has NO spaces, and is at least 3 characters ("@" plus 2)
- the list can never be empty — he must always be able to summon you
- no duplicates
- a tag MAY be a prefix of another tag (e.g. "@assist" alongside "@assistente"): that is supported and matched longest-first. It is not a reason to refuse.

REASONING: write it in ${lang === "pt" ? "Portuguese" : "the owner's language"}, first person, to him, in 1-3 sentences. Say what you concluded about EACH tag — the one he named and the ones he did not. Do NOT list the final tags in the reasoning and do NOT ask him to confirm: the message he receives already appends the complete list and the question.`;
}

export function buildProposeUser({ currentTags, order, transcript }) {
  return `The tags you answer to RIGHT NOW: ${(currentTags || []).join(", ")}

Recent conversation:
${transcript || "(none)"}

His order: ${order}`;
}

// ============================================================================
//  USER-FACING SCAFFOLDING (localized).
//  Fixed messages only — the REASONING itself is generated in-language by the model
//  (buildProposeSystem). Keep BOTH en + pt; any other language is produced from the `en`
//  copy by the orchestrator's send() translation fallback.
// ============================================================================

// Tags as he reads them. Bold, because a tag is a thing he has to TYPE exactly.
export const fmtTags = (tags) => (tags || []).map((t) => `*${t}*`).join(", ");

const REPLY = {
  en: {
    thinkingError: () => "I hit an error while thinking. Try again?",

    // GUIDANCE, not a malfunction: she understood him and is asking again. Plain send().
    invalid: ({ problem, current }) =>
      `I can't use that — ${problem}. I still answer to ${fmtTags(current)}. What would you like to call me?`,

    propose: ({ reasoning, tags }) =>
      `${reasoning}\n\nMy tags would then be: ${fmtTags(tags)}.\n\nConfirm? I'll hold this for 15 minutes.`,

    applied: ({ tags, retired }) =>
      `Done. Call me with ${fmtTags(tags)}.` +
      (retired.length
        ? ` ${fmtTags(retired)} no longer ${retired.length > 1 ? "work" : "works"}.`
        : ""),

    // The store did NOT take it. She says so — she never reports a change she did not persist.
    appliedNotSaved: ({ tags, retired }) =>
      `Call me with ${fmtTags(tags)} — that works right now.` +
      (retired.length
        ? ` ${fmtTags(retired)} no longer ${retired.length > 1 ? "work" : "works"}.`
        : "") +
      `\n\n⚠️ But I could NOT save it — my settings store is unreachable. This holds until I restart, and then I'll go back to the tags I booted with and you'd have to ask me again.`,

    declined: ({ current }) =>
      `Okay — nothing changed. You still call me with ${fmtTags(current)}.`,
  },

  pt: {
    thinkingError: () => "Tive um erro ao processar. Pode tentar de novo?",

    invalid: ({ problem, current }) =>
      `Não posso usar isso — ${problem}. Continuo respondendo a ${fmtTags(current)}. Como você quer me chamar?`,

    propose: ({ reasoning, tags }) =>
      `${reasoning}\n\nMinhas tags ficariam: ${fmtTags(tags)}.\n\nConfirma? Guardo isso por 15 minutos.`,

    applied: ({ tags, retired }) =>
      `Pronto. Me chame de ${fmtTags(tags)}.` +
      (retired.length
        ? ` ${fmtTags(retired)} não ${retired.length > 1 ? "funcionam" : "funciona"} mais.`
        : ""),

    appliedNotSaved: ({ tags, retired }) =>
      `Me chame de ${fmtTags(tags)} — já funciona agora.` +
      (retired.length
        ? ` ${fmtTags(retired)} não ${retired.length > 1 ? "funcionam" : "funciona"} mais.`
        : "") +
      `\n\n⚠️ Mas NÃO consegui salvar — meu armazenamento de configurações está inacessível. Isso vale até eu reiniciar; depois volto às tags com que subi e você teria que pedir de novo.`,

    declined: ({ current }) =>
      `Ok — nada mudou. Você continua me chamando de ${fmtTags(current)}.`,
  },
};

// Pick the reply set for a language, falling back to English (which the orchestrator's
// send() then translates for any non-en/pt language).
export function reply(lang) {
  return REPLY[lang] || REPLY.en;
}
