// ============================================================================
//  lib/whatsapp.js  —  SHARED WhatsApp message utilities.
//  Text extraction, quoted-audio detection, in-memory buffer and transcript
//  building. No agent logic lives here.
// ============================================================================

// Extracts the text from an Evolution `message` object (several possible shapes).
export function extractText(msg) {
  if (!msg) return "";
  return (
    msg.conversation ||
    msg.extendedTextMessage?.text ||
    msg.imageMessage?.caption ||
    msg.videoMessage?.caption ||
    ""
  );
}

// Detects whether a message is a REPLY (quote) to another one and, if so,
// returns the quoted message id and whether it contains audio.
//   -> { id, hasAudio, mediaType } | null
// Pass the whole webhook `data` object. Evolution delivers the reply context in
// one of two places depending on the message shape:
//   - data.contextInfo            -> a plain-text ("conversation") reply, which is
//                                    the usual WhatsApp case: contextInfo sits as a
//                                    SIBLING of `message`, not inside it.
//   - message.<type>.contextInfo  -> some payload shapes nest it under the message.
// We check the sibling first, then fall back to the nested shapes.
export function getQuoted(data) {
  const msg = data?.message ?? data; // tolerate being called with data or data.message
  const ctx =
    data?.contextInfo ||
    msg?.extendedTextMessage?.contextInfo ||
    msg?.imageMessage?.contextInfo ||
    msg?.videoMessage?.contextInfo ||
    msg?.audioMessage?.contextInfo ||
    null;
  if (!ctx) return null;
  const id = ctx.stanzaId || ctx.quotedMessageId || null;
  if (!id) return null;
  const q = ctx.quotedMessage || {};
  const hasAudio = !!(q.audioMessage || q.pttMessage); // ptt = voice message
  const mediaType = q.audioMessage
    ? "audio"
    : q.imageMessage
    ? "image"
    : q.videoMessage
    ? "video"
    : q.documentMessage
    ? "document"
    : "text";
  return { id, hasAudio, mediaType };
}

// ---------------------------------------------------------------------------
//  In-memory buffer (short-term context). Cleared when the container is
//  recreated; the real history lives in Evolution's Postgres (via fetchHistory).
// ---------------------------------------------------------------------------
const buffers = new Map(); // remoteJid -> [{ t, fromMe, text, pushName }]

export function remember(remoteJid, e) {
  if (!e.text) return;
  const arr = buffers.get(remoteJid) || [];
  arr.push(e);
  while (arr.length > 50) arr.shift();
  buffers.set(remoteJid, arr);
}

// Merges history (Evolution) + buffer (memory), dedups by time+text, sorts and
// returns the last `limit` messages.
export function combine(remoteJid, hist, limit = 30) {
  const buf = buffers.get(remoteJid) || [];
  const all = [...hist, ...buf].filter((m) => m.text);
  const map = new Map();
  for (const m of all) map.set(`${m.t}|${m.text}`, m);
  return [...map.values()].sort((a, b) => a.t - b.t).slice(-limit);
}

// Builds the transcript in "ME: ..." / "OTHER: ..." format.
export function buildTranscript(conv) {
  return conv.map((m) => `${m.fromMe ? "ME" : "OTHER"}: ${m.text}`).join("\n");
}

// Finds the contact name of this conversation (last pushName from OTHER).
export function contactName(conv) {
  return [...conv].reverse().find((m) => !m.fromMe && m.pushName)?.pushName;
}
