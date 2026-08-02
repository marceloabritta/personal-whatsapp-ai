// ============================================================================
//  Skill "Feedback" (the owner reports a mistake) — user-facing STRINGS.  CONVERTED.
//  Localized reply strings only — no logic (that's skill.js). The extraction that used to
//  live here (buildFeedbackSchema/System/User) is gone: the ORCHESTRATOR now restates the
//  complaint into the skill's declared inputs and asks the one clarifying question, so this
//  file holds only the skill's OWN outcome strings — the line it sends after it has filed.
//
//  These NEVER claim the mistake is fixed — only that it is FILED. The secretary saying
//  "fixed it!" when it has merely written a Markdown file would be a worse lie than the
//  original bug. Keep BOTH en + pt; any other language is produced from the `en` copy by the
//  orchestrator's send() translation fallback.
// ============================================================================
const REPLY = {
  en: {
    logged: ({ title }) =>
      `Noted — logged as a mistake to investigate: "${title}". It goes into the next improvement pass. I haven't changed anything yet.`,
    // Deliberately honest, and deliberately NOT a confirmation. If the note didn't reach the
    // disk, the owner has to know now, while he still remembers what he wanted to say.
    logFailed: () =>
      "I hit a problem filing that note, so it may not have been saved. The error is in the log — worth telling me again in a minute.",
  },
  pt: {
    logged: ({ title }) =>
      `Anotado — registrei como um erro para investigar: "${title}". Vai entrar na próxima rodada de melhorias. Ainda não mudei nada.`,
    logFailed: () =>
      "Tive um problema para registrar essa anotação, então ela pode não ter sido salva. O erro está no log — vale me contar de novo daqui a pouco.",
  },
};

export function reply(lang) {
  return REPLY[lang] || REPLY.en;
}
