// ============================================================================
//  Skill "Audio transcriptions" — reply TEXTS.
//  This skill does NOT use an LLM (transcription is done by AssemblyAI), so this
//  file only holds the user-facing TEXTS, isolated from the logic — same spirit
//  as the other skills' prompt.js: change behavior without touching the code.
//
//  LOCALIZATION: strings are a per-language map { en, pt }, selected at send time
//  with ctx.lang (see msg()). English is the canonical source; any language the
//  the secretary doesn't maintain a map for is produced from `en` by the orchestrator's
//  send() translation fallback. Add BOTH en + pt for every new message.
// ============================================================================

export const MSG = {
  en: {
    noAudio: (tag) =>
      `To transcribe, reply to the audio you want and call ${tag} again. E.g.: press and hold the audio, tap Reply and type "${tag} transcribe".`,
    processing: "Got the audio, transcribing... ~1 min.",
    downloadFailed:
      "I couldn't download that audio from WhatsApp. It may be too old or was not saved. Try a more recent audio.",
    transcriptionFailed:
      "I downloaded the audio, but the transcription failed. Error in the log. Try again?",
    empty: "I transcribed it, but no text came out (silent or very short audio).",
    // Wraps the final reply with the transcript (the transcript text itself is
    // never translated — it is the audio's own words).
    transcript: (text) => `Audio transcript:\n\n${text}`,
  },
  pt: {
    noAudio: (tag) =>
      `Para transcrever, responda ao áudio que você quer e chame ${tag} de novo. Ex.: segure o áudio, toque em Responder e digite "${tag} transcrever".`,
    processing: "Recebi o áudio, transcrevendo... ~1 min.",
    downloadFailed:
      "Não consegui baixar esse áudio do WhatsApp. Pode ser muito antigo ou não ter sido salvo. Tente um áudio mais recente.",
    transcriptionFailed:
      "Baixei o áudio, mas a transcrição falhou. O erro está no log. Pode tentar de novo?",
    empty:
      "Transcrevi, mas não saiu nenhum texto (áudio silencioso ou muito curto).",
    transcript: (text) => `Transcrição do áudio:\n\n${text}`,
  },
};

// Pick the message set for a language, falling back to English (which the
// orchestrator's send() then translates for any non-en/pt language).
export function msg(lang) {
  return MSG[lang] || MSG.en;
}
