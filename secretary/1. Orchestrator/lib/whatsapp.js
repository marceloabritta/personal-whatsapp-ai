// ============================================================================
//  lib/whatsapp.js  —  SHARED WhatsApp message utilities.
//  Text extraction, quoted-audio detection, in-memory buffer and transcript
//  building. No agent logic lives here.
// ============================================================================
import { isOwnMessage } from "./identity.js";

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
// returns the quoted message id, whether it contains audio, the quoted TEXT and
// any Google Calendar link found in it (used to delete/edit an event by replying
// to the message that carries its calendar link).
//   -> { id, hasAudio, mediaType, text, calendarLink } | null
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
  const text = extractText(q).trim();
  return { id, hasAudio, mediaType, text, calendarLink: findCalendarLink(text) };
}

// Finds a Google Calendar event link inside a text (google.com/calendar or
// calendar.google.com, carrying an `eid` param). Returns the URL or null.
export function findCalendarLink(text) {
  if (!text) return null;
  const m = text.match(
    /https?:\/\/\S*(?:google\.com\/calendar|calendar\.google\.com)\/\S*eid=\S+/i
  );
  return m ? m[0] : null;
}

// ---------------------------------------------------------------------------
//  In-memory buffer (short-term context). Cleared when the container is
//  recreated; the real history lives in Evolution's Postgres (via fetchHistory,
//  which reads a 1:1 chat under BOTH its phone JID and its @lid JID — see the
//  note there. Read under one key only, the durable history comes back empty and
//  this volatile buffer silently becomes the secretary's whole memory).
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

// Builds the transcript with THREE speakers, for the orchestrator's turn call — which must be
// able to tell the secretary's OWN past messages from the owner's, because both arrive with
// fromMe=true (she sends from his account). The discriminator is isOwnMessage (the reply header),
// the same one server.js uses to avoid re-consuming her own messages:
//   fromMe && isOwnMessage(text) -> SECRETARY   (her own reply, echoed back by Evolution)
//   fromMe                       -> OWNER
//   otherwise                    -> CONTACT
// ADDITIVE: buildTranscript (and therefore ctx.transcript) is unchanged, so all seven skills'
// own LLM prompts see today's exact bytes. Only the orchestrator's turn call uses this renderer.
export function buildLabeledTranscript(conv) {
  return conv
    .map((m) => {
      const who = m.fromMe ? (isOwnMessage(m.text) ? "SECRETARY" : "OWNER") : "CONTACT";
      return `${who}: ${m.text}`;
    })
    .join("\n");
}

// Finds the contact name of this conversation (last pushName from OTHER).
export function contactName(conv) {
  return [...conv].reverse().find((m) => !m.fromMe && m.pushName)?.pushName;
}
