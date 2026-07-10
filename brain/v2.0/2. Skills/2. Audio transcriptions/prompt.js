// ============================================================================
//  Skill "Audio transcriptions" — reply TEXTS.
//  This skill does NOT use an LLM (transcription is done by AssemblyAI), so this
//  file only holds the user-facing TEXTS, isolated from the logic — same spirit
//  as the other skills' prompt.js: change behavior without touching the code.
// ============================================================================

export const MSG = {
  noAudio:
    "To transcribe, reply to the audio you want and call @secretary again. E.g.: press and hold the audio, tap Reply and type \"@secretary transcribe\".",
  processing: "Got the audio, transcribing... ~1 min.",
  downloadFailed:
    "I couldn't download that audio from WhatsApp. It may be too old or was not saved. Try a more recent audio.",
  transcriptionFailed:
    "I downloaded the audio, but the transcription failed. Error in the log. Try again?",
  empty: "I transcribed it, but no text came out (silent or very short audio).",
};

// Formats the final reply with the transcript.
export function formatTranscript(text) {
  return `Audio transcript:\n\n${text}`;
}
