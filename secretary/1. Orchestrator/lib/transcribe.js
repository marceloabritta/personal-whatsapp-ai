// ============================================================================
//  lib/transcribe.js  —  SYSTEM-SIDE AUDIO TRANSCRIPTION (rails, additive).
//
//  The Claude API has no audio content block: the model cannot ingest audio bytes. So an
//  inbound audio the owner asks about is transcribed HERE, by the system, to text — and that
//  text is folded into the turn prompt as a marked transcript (server.js MEDIA PREP +
//  ctx.audioTranscript). The model then handles it as ordinary prose.
//
//  This is the AssemblyAI path lifted VERBATIM from "3. Mary Skills/2. Audio transcriptions/
//  skill.js" (aaiUpload/aaiTranscribe) so the orchestrator never imports a skill. It reads the
//  SAME env vars the audio skill already used (ASSEMBLYAI_API_KEY / ASSEMBLYAI_LANGUAGE); no new
//  secret. The audio skill's code is left dormant (unrouted), and could later re-use this helper.
// ============================================================================

const AAI_BASE = "https://api.assemblyai.com/v2";

// Uploads the audio bytes to AssemblyAI and returns the upload_url.
async function aaiUpload(apiKey, buffer) {
  const res = await fetch(`${AAI_BASE}/upload`, {
    method: "POST",
    headers: { authorization: apiKey },
    body: buffer,
  });
  if (!res.ok)
    throw new Error(`AAI upload ${res.status}: ${await res.text().catch(() => "")}`);
  const data = await res.json();
  return data.upload_url;
}

// Creates the transcript and polls until it completes.
// Returns { text, durationSec } — durationSec is the audio's length as measured by AssemblyAI
// (null if it doesn't report one).
async function aaiTranscribe(apiKey, uploadUrl, language) {
  const create = await fetch(`${AAI_BASE}/transcript`, {
    method: "POST",
    headers: { authorization: apiKey, "Content-Type": "application/json" },
    body: JSON.stringify({ audio_url: uploadUrl, language_code: language || "en" }),
  });
  if (!create.ok)
    throw new Error(`AAI transcript ${create.status}: ${await create.text().catch(() => "")}`);
  const { id } = await create.json();

  // Polling: up to ~2 min (40 x 3s). WhatsApp audios are usually short.
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    const poll = await fetch(`${AAI_BASE}/transcript/${id}`, {
      headers: { authorization: apiKey },
    });
    if (!poll.ok) continue;
    const data = await poll.json();
    if (data.status === "completed")
      return { text: data.text || "", durationSec: Number(data.audio_duration) || null };
    if (data.status === "error") throw new Error(`AAI status=error: ${data.error}`);
  }
  throw new Error("AAI timeout (transcription took too long)");
}

// transcribeAudio(env, buffer, lang) -> { text, durationSec }
//   env    : reads env.ASSEMBLYAI_API_KEY (required) and env.ASSEMBLYAI_LANGUAGE (fallback lang).
//   buffer : the decrypted audio bytes (a Node Buffer, from evolution.getMediaBase64).
//   lang   : the detected conversation language, preferred over the static env default.
// Throws on a missing key or an AssemblyAI failure — the caller (server.js MEDIA PREP) catches
// it, sends a plain notice, and continues text-only. NEVER returns partial garbage.
export async function transcribeAudio(env = {}, buffer, lang) {
  const apiKey = env.ASSEMBLYAI_API_KEY;
  if (!apiKey) throw new Error("ASSEMBLYAI_API_KEY missing");
  const language = lang || env.ASSEMBLYAI_LANGUAGE || "en";
  const uploadUrl = await aaiUpload(apiKey, buffer);
  return aaiTranscribe(apiKey, uploadUrl, language);
}
