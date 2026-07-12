// ============================================================================
//  Skill "Audio transcriptions" — LOGIC.
//  Run by the orchestrator when the router picks "transcribe_audio".
//  Flow: take the QUOTED (replied-to) audio -> download its base64 from
//  Evolution -> send to AssemblyAI -> return the text over WhatsApp.
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description }
//    export async function run(ctx)
// ============================================================================
import { msg } from "./prompt.js";

export const manifest = {
  id: "transcribe_audio",
  description:
    "transcribe an audio message that the user REPLIED to (quoted) and asked to transcribe",
};

const AAI_BASE = "https://api.assemblyai.com/v2";

// Uploads the audio bytes to AssemblyAI and returns the upload_url.
async function aaiUpload(apiKey, buffer) {
  const res = await fetch(`${AAI_BASE}/upload`, {
    method: "POST",
    headers: { authorization: apiKey },
    body: buffer,
  });
  if (!res.ok)
    throw new Error(
      `AAI upload ${res.status}: ${await res.text().catch(() => "")}`
    );
  const data = await res.json();
  return data.upload_url;
}

// Creates the transcript and polls until it completes. Returns the text.
async function aaiTranscribe(apiKey, uploadUrl, language) {
  const create = await fetch(`${AAI_BASE}/transcript`, {
    method: "POST",
    headers: { authorization: apiKey, "Content-Type": "application/json" },
    body: JSON.stringify({
      audio_url: uploadUrl,
      language_code: language || "en",
    }),
  });
  if (!create.ok)
    throw new Error(
      `AAI transcript ${create.status}: ${await create.text().catch(() => "")}`
    );
  const { id } = await create.json();

  // Polling: up to ~2 min (40 x 3s). WhatsApp audios are usually short.
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    const poll = await fetch(`${AAI_BASE}/transcript/${id}`, {
      headers: { authorization: apiKey },
    });
    if (!poll.ok) continue;
    const data = await poll.json();
    if (data.status === "completed") return data.text || "";
    if (data.status === "error")
      throw new Error(`AAI status=error: ${data.error}`);
  }
  throw new Error("AAI timeout (transcription took too long)");
}

// ctx (from the orchestrator): { number, quoted, env, evolution, send }
//   quoted = { id, hasAudio, mediaType } | null
export async function run(ctx) {
  const { number, quoted, env, evolution, send, tag, lang } = ctx;
  const M = msg(lang); // per-language reply texts (en/pt map; en fallback)

  if (!quoted || !quoted.hasAudio) {
    await send(number, M.noAudio(tag));
    return;
  }

  const apiKey = env.ASSEMBLYAI_API_KEY;
  if (!apiKey) {
    console.error("ASSEMBLYAI_API_KEY missing in .env");
    await send(number, M.transcriptionFailed);
    return;
  }

  // 1) download the decrypted audio from Evolution (base64).
  let base64, mimetype;
  try {
    ({ base64, mimetype } = await evolution.getMediaBase64(quoted.id));
  } catch (e) {
    console.error("Transcription/download error:", e?.message || e);
    await send(number, M.downloadFailed);
    return;
  }

  await send(number, M.processing);

  // 2) transcribe via AssemblyAI. Prefer the detected conversation language so a
  //    PT chat transcribes in PT; fall back to the static env, then English.
  try {
    const buffer = Buffer.from(base64, "base64");
    const uploadUrl = await aaiUpload(apiKey, buffer);
    const language = lang || env.ASSEMBLYAI_LANGUAGE || "en";
    const text = await aaiTranscribe(apiKey, uploadUrl, language);
    const clean = (text || "").trim();
    // The transcript is the OWNER's words quoted back, not the secretary speaking —
    // send it plain so the italics keep meaning "this is the secretary".
    if (clean) await send(number, M.transcript(clean), { italic: false });
    else await send(number, M.empty);
  } catch (e) {
    console.error("Transcription/AAI error:", e?.message || e, "mime:", mimetype);
    await send(number, M.transcriptionFailed);
  }
}
