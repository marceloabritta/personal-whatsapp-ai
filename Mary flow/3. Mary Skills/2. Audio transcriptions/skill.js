// ============================================================================
//  Skill "Audio transcriptions" — LOGIC.
//  Run by the orchestrator when the router picks "transcribe_audio".
//  Flow: take the QUOTED (replied-to) audio -> download its base64 from
//  Evolution -> send to AssemblyAI -> return the text over WhatsApp: inline for
//  a short audio, as a .txt document for a long one (LONG_AUDIO_SEC).
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description }
//    export async function run(ctx)
// ============================================================================
import { msg } from "./prompt.js";
import { headerFor } from "../../1. Orchestrator/lib/identity.js";
import { frame } from "../../1. Orchestrator/lib/format.js";

// `inputs: null` — NO declared inputs (see 1. Orchestrator/lib/inputs.js). This skill needs a
// quoted AUDIO, which is not text and cannot be declared this way, so there is nothing for the
// router's merged call to pre-extract and nothing may be handed to it.
export const manifest = {
  id: "transcribe_audio",
  // CONVERTED (pure task): the orchestrator runs the dialogue. There is nothing to propose or
  // confirm here — transcribing a quoted audio is not a write to the outside world — so run()
  // just acts and RETURNS a JSON result the model reads back. inputs stays null: the input is a
  // quoted AUDIO, which cannot be declared as text, so it is dispatched via the inputs:null gate
  // (server.js rails change (b)) and run() does its own `!quoted?.hasAudio -> noAudio` check.
  conversation: "orchestrator",
  inputs: null,
  description:
    "transcribe an audio message that the user REPLIED to (quoted) and asked to transcribe",
};

const AAI_BASE = "https://api.assemblyai.com/v2";

// Past this, the transcript is a wall of text in the thread — ship it as a
// .txt attachment instead of inline.
const LONG_AUDIO_SEC = 120;

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

// Creates the transcript and polls until it completes.
// Returns { text, durationSec } — durationSec is the audio's length as measured
// by AssemblyAI (null if it doesn't report one), and picks the delivery format.
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
    if (data.status === "completed")
      return {
        text: data.text || "",
        durationSec: Number(data.audio_duration) || null,
      };
    if (data.status === "error")
      throw new Error(`AAI status=error: ${data.error}`);
  }
  throw new Error("AAI timeout (transcription took too long)");
}

// Delivers a long transcript as a real, saveable .txt document. sendMedia
// bypasses the orchestrator's send(), so the caption is framed here — same bold
// header + italic body as every other secretary message. Returns false if the
// send failed, and the caller then falls back to the inline transcript: a text
// wall beats losing the transcript the owner asked for.
async function sendTranscriptFile(ctx, text, M) {
  const { number, evolution, lang } = ctx;
  const stamp = new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-");
  try {
    return await evolution.sendMedia(number, {
      mediatype: "document",
      mimetype: "text/plain",
      media: Buffer.from(text, "utf8").toString("base64"),
      fileName: `audio-transcript-${stamp}.txt`,
      caption: frame(headerFor(lang), M.longAudio),
    });
  } catch (e) {
    console.error("Transcription/sendMedia error:", e?.message || e);
    return false;
  }
}

// ctx (from the orchestrator): { number, quoted, env, evolution, send }
//   quoted = { id, hasAudio, mediaType } | null
export async function run(ctx) {
  const { number, quoted, env, evolution, send, tag, lang } = ctx;
  const M = msg(lang); // per-language reply texts (en/pt map; en fallback)

  if (!quoted || !quoted.hasAudio) {
    // MANAGER RULING: "reply to an audio to transcribe" is user GUIDANCE, not a malfunction, so it
    // is sent with plain ctx.send (NOT sendFailure) and files no self-learning capture — the same
    // rule as server.js:490. The read-back result records it so the model can nudge him in-line.
    await send(number, M.noAudio(tag));
    return { ok: false, reason: "noAudio" };
  }

  const apiKey = env.ASSEMBLYAI_API_KEY;
  if (!apiKey) {
    console.error("ASSEMBLYAI_API_KEY missing in .env");
    await ctx.sendFailure(number, M.transcriptionFailed);
    return { ok: false, reason: "noKey" };
  }

  // 1) download the decrypted audio from Evolution (base64).
  let base64, mimetype;
  try {
    ({ base64, mimetype } = await evolution.getMediaBase64(quoted.id));
  } catch (e) {
    console.error("Transcription/download error:", e?.message || e);
    await ctx.sendFailure(number, M.downloadFailed);
    return { ok: false, reason: "downloadFailed" };
  }

  // 2) transcribe via AssemblyAI. Prefer the detected conversation language so a
  //    PT chat transcribes in PT; fall back to the static env, then English.
  //    No "transcribing…" ack: the transcript itself is the only message.
  try {
    const buffer = Buffer.from(base64, "base64");
    const uploadUrl = await aaiUpload(apiKey, buffer);
    const language = lang || env.ASSEMBLYAI_LANGUAGE || "en";
    const { text, durationSec } = await aaiTranscribe(apiKey, uploadUrl, language);
    const clean = (text || "").trim();
    if (!clean) {
      await send(number, M.empty);
      return { ok: false, reason: "empty" };
    }
    // 3) deliver: .txt document for a long audio, inline text for a short one.
    //    An unknown duration reads as short — inline is the safe default.
    if (durationSec > LONG_AUDIO_SEC && (await sendTranscriptFile(ctx, clean, M)))
      return { ok: true, delivered: "file", chars: clean.length };
    await send(number, M.transcript(clean));
    return { ok: true, delivered: "inline", chars: clean.length };
  } catch (e) {
    console.error("Transcription/AAI error:", e?.message || e, "mime:", mimetype);
    await ctx.sendFailure(number, M.transcriptionFailed);
    return { ok: false, reason: "transcribeFailed" };
  }
}
