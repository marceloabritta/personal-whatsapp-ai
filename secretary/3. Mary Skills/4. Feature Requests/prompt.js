// ============================================================================
//  Skill "Feature Requests" — PROMPT + user-facing STRINGS.  CONVERTED (pure task).
//  Prompt text/rules and localized reply strings only — no logic (that's skill.js).
//
//  The clarifying INTERVIEW that used to live here (buildClarifySystem/User, CLARIFY_SCHEMA)
//  is gone: the ORCHESTRATOR runs it over `listen` turns and hands the finished brief in
//  ctx.info. What remains is the DOCUMENT generation (always English, by design — the artifact
//  is destined for the owner's English codebase), the filename slug, and the skill's OWN
//  outcome strings. Keep BOTH en + pt for every user-facing message.
// ============================================================================

// ---- Document generation (ALWAYS English) ------------------------------------
// Returns PROSE (markdown), not JSON — skill.js reads the text blocks directly.
export function buildDocSystem() {
  return `You are a product writer. Turn the feature DRAFT (JSON) into a clean Markdown
feature-spec document, written from the POINT OF VIEW OF THE USER.

Output ONLY the Markdown document — no preamble, no code fences around the whole thing.
Write in ENGLISH regardless of the draft's language (translate any non-English content;
keep proper nouns). Use exactly this skeleton, omitting a section only if it would be
empty:

# <Feature title>

## Summary
<one-sentence summary>

## Problem / motivation
<the pain this solves and why it matters>

## User flow (from the user's point of view)
1. <how the user triggers / starts it>
2. <next step the user takes or sees>
...

## Actors
- <who is involved>

## Data & services touched
<systems, data, integrations involved>

## Edge cases & open questions
- <edge case>
- **Open:** <unresolved question>

---
*Drafted by @assistant on WhatsApp. Save to the repo and refine.*

Keep it concise and concrete. Do not add sections that aren't in the skeleton.`;
}

export function buildDocUser({ draftJson }) {
  return `Feature DRAFT (JSON):
${draftJson}`;
}

// ---- slug for the filename ---------------------------------------------------
// Lowercase, strip accents, non-alphanumeric -> "-", collapse/trim. Fallback "feature".
export function slugify(title) {
  const s = String(title || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "") // strip diacritics
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return s || "feature";
}

// ============================================================================
//  USER-FACING OUTCOME STRINGS (localized).
//  Keep BOTH en + pt; any other language is produced from the `en` copy by the
//  orchestrator's send() translation fallback.
// ============================================================================
const REPLY = {
  en: {
    thinkingError: () => "I hit an error while thinking. Try again?",
    renderError: () =>
      "I couldn't generate the document. Your notes are safe — say \"write it up\" to try again.",
    sendFailed: () =>
      "I wrote the spec but couldn't send the file. Error in the log — try again?",
    specFileFailed: () =>
      "I sent you the spec, but I couldn't file my own copy — so it won't reach the board. Save the file yourself.",
    docCaption: ({ title }) =>
      `Here's the spec for "${title}". Save it and drop it into your repo. 📄`,
  },
  pt: {
    thinkingError: () => "Tive um erro ao processar. Pode tentar de novo?",
    renderError: () =>
      'Não consegui gerar o documento. Suas anotações estão salvas — diga "pode escrever" para tentar de novo.',
    sendFailed: () =>
      "Escrevi a spec mas não consegui enviar o arquivo. O erro está no log — tentar de novo?",
    specFileFailed: () =>
      "Te mandei a spec, mas não consegui salvar minha cópia — então ela não vai chegar no board. Guarde o arquivo.",
    docCaption: ({ title }) =>
      `Aqui está a spec de "${title}". Salve e coloque no seu repositório. 📄`,
  },
};

// Pick the reply set for a language, falling back to English (which the
// orchestrator's send() then translates for any non-en/pt language).
export function reply(lang) {
  return REPLY[lang] || REPLY.en;
}
