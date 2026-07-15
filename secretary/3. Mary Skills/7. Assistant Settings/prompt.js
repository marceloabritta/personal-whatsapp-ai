// ============================================================================
//  Skill "Assistant Settings" — localized OUTCOME scaffolding.
//
//  CONVERTED SKILL. The conversation — the reasoning about the other language's tag, the
//  proposal, the confirmation — is run by the ORCHESTRATOR's model now, in-language, and never
//  enters the repo. What remains here is only the skill's OWN outcome strings: the message it
//  sends after it has acted. They stay { en, pt } (the maintained languages); any other language
//  is produced from the `en` copy by the orchestrator's send() translation fallback.
// ============================================================================

// Tags as he reads them. Bold, because a tag is a thing he has to TYPE exactly.
export const fmtTags = (tags) => (tags || []).map((t) => `*${t}*`).join(", ");

const REPLY = {
  en: {
    thinkingError: () => "I hit an error while thinking. Try again?",

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
  },

  pt: {
    thinkingError: () => "Tive um erro ao processar. Pode tentar de novo?",

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
  },
};

// Pick the reply set for a language, falling back to English (which the orchestrator's
// send() then translates for any non-en/pt language).
export function reply(lang) {
  return REPLY[lang] || REPLY.en;
}
