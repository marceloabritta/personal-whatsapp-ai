// ============================================================================
//  Skill "Feedback" (the owner reports a mistake) — PROMPT + user-facing STRINGS.
//  Prompt text/rules and localized reply strings only — no logic (that's skill.js).
//
//  The extraction call restates the OWNER'S CLAIM in English for the report. It does NOT
//  theorize about the bug: that's the (separate, clearly-labelled, discardable) auto-analysis
//  in lib/selflearning.js. Keeping owner-truth and machine-guess in different sections of the
//  report is the whole point — the triage agent must always be able to tell them apart.
// ============================================================================

// ---- JSON Schema (output_config.format) --------------------------------------
// Built at CALL TIME because `suspected_skill`'s enum is the live skill catalog: a skill
// added tomorrow is a valid answer the day it ships, with no id list here to rot.
export function buildFeedbackSchema(catalog = []) {
  const ids = catalog.map((c) => c.id).filter(Boolean);
  return {
    type: "object",
    additionalProperties: false,
    required: [
      "title",
      "what_went_wrong",
      "expected",
      "suspected_skill",
      "enough_context",
    ],
    properties: {
      // Short, slug-able summary -> the report filename and the triage list.
      title: { type: "string" },
      // The owner's claim, restated plainly. NOT a theory of the cause.
      what_went_wrong: { type: "string" },
      // What he says should have happened instead; null if he didn't say.
      expected: { type: ["string", "null"] },
      // NULLABLE ENUM -> anyOf. The structured-output validator REJECTS a type-union
      // (["string","null"]) that also carries an `enum` — same pattern, and same reason, as
      // list_mode in "1. Calendar Actions/prompt.js".
      suspected_skill:
        ids.length > 0
          ? { anyOf: [{ type: "null" }, { type: "string", enum: ids }] }
          : { type: ["string", "null"] },
      // false ONLY when the note is too vague to act on AND nothing was quoted — the single
      // case where the skill asks one follow-up question (see skill.js).
      enough_context: { type: "boolean" },
    },
  };
}

export function buildFeedbackSystem(owner, catalog = []) {
  const list = catalog.map((c) => `  - "${c.id}": ${c.description}`).join("\n");
  return `${owner} is the owner of a WhatsApp AI secretary. He has just told the secretary
that it MADE A MISTAKE. Your job is to turn his complaint into a precise bug report for the
engineer who will fix it.

The secretary's skills:
${list}

Return JSON matching the schema:
- "title": a short (3-8 word) summary of the defect, in English. It becomes a filename and a
  line in a triage list — make it specific ("wrong timezone on created event", not "a bug").
- "what_went_wrong": restate ${owner}'s complaint plainly, in English, in 1-3 sentences. Use
  the quoted message (the secretary's own output he is replying to) and the conversation as
  evidence for WHAT the secretary actually did.
- "expected": what ${owner} says SHOULD have happened instead. null if he didn't say.
- "suspected_skill": the skill id most likely responsible, or null if you genuinely can't
  tell. Judge by what the secretary DID, not by the words he used to complain.
- "enough_context": true if this report is specific enough for an engineer to start
  investigating. false ONLY if the complaint is so vague that nobody could tell which
  message was wrong or what "wrong" means (e.g. just "you made a mistake" with nothing else).

CRITICAL RULES:
- Report the SYMPTOM ${owner} observed. Do NOT speculate about the root cause, the code, or
  which line is broken — a separate pass does that, and mixing the two would let a guess be
  mistaken for his testimony.
- Do NOT defend the secretary, explain the mistake away, or decide he is wrong. He is the
  user; if he says the output was wrong, it was wrong.
- Everything you write is ENGLISH, whatever language he complained in — it's destined for an
  English codebase.`;
}

export function buildFeedbackUser(
  owner,
  { order, quotedText, quotedIsSecretary, transcript, nowStr }
) {
  const quoted = quotedText
    ? `The message he is REPLYING TO (${
        quotedIsSecretary
          ? "CONFIRMED to be the secretary's own output — this is very likely the defect itself"
          : "NOT secretary output — treat as context only"
      }):
"""
${quotedText}
"""`
    : `He did NOT reply to a specific message, so the offending output must be found in the conversation below.`;

  return `Current date/time: ${nowStr}

${quoted}

Recent conversation:
${transcript || "(none)"}

${owner}'s complaint: ${order}`;
}

// ============================================================================
//  USER-FACING SCAFFOLDING STRINGS (localized).
//  Keep BOTH en + pt; any other language is produced from the `en` copy by the
//  orchestrator's send() translation fallback.
//
//  These NEVER claim the mistake is fixed — only that it is FILED. The secretary saying
//  "fixed it!" when it has merely written a Markdown file would be a worse lie than the
//  original bug.
// ============================================================================
const REPLY = {
  en: {
    logged: ({ title }) =>
      `Noted — logged as a mistake to investigate: "${title}". It goes into the next improvement pass. I haven't changed anything yet.`,
    loggedAndAsk: () =>
      `Noted — I've logged that. To make it actionable: which message was wrong, and what should it have said? (Replying directly to the wrong message works best.)`,
    enriched: () => "Got it — added that to the report. Thanks.",
    // Deliberately honest, and deliberately NOT a confirmation. If the note didn't reach the
    // disk, the owner has to know now, while he still remembers what he wanted to say.
    logFailed: () =>
      "I hit a problem filing that note, so it may not have been saved. The error is in the log — worth telling me again in a minute.",
  },
  pt: {
    logged: ({ title }) =>
      `Anotado — registrei como um erro para investigar: "${title}". Vai entrar na próxima rodada de melhorias. Ainda não mudei nada.`,
    loggedAndAsk: () =>
      `Anotado — já registrei. Para eu conseguir agir: qual mensagem estava errada, e o que ela deveria ter dito? (Responder direto à mensagem errada funciona melhor.)`,
    enriched: () => "Beleza — adicionei isso ao relatório. Obrigado.",
    logFailed: () =>
      "Tive um problema para registrar essa anotação, então ela pode não ter sido salva. O erro está no log — vale me contar de novo daqui a pouco.",
  },
};

export function reply(lang) {
  return REPLY[lang] || REPLY.en;
}
