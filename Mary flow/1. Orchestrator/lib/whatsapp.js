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

// ---------------------------------------------------------------------------
//  INBOUND MEDIA (the @mary generalized-relay path). Two ADDITIVE exports:
//  - inboundMedia(data, quoted): the turn's media LIST (type-agnostic DETECTION).
//  - mediaBlockFor(...):         THE EXTENSION POINT (supported? -> native block, or defer).
//  extractText and getQuoted above are NOT touched — every existing caller is unchanged.
// ---------------------------------------------------------------------------

// The image mimes Anthropic accepts as an image block. Anything else -> defer (null).
const IMAGE_MIMES = new Set(["image/jpeg", "image/png", "image/gif", "image/webp"]);

// Read-only peek at the quoted node's mimetype, from the SAME contextInfo getQuoted reads. It does
// NOT modify getQuoted — it only surfaces the mime getQuoted does not carry, so a quoted image/PDF
// can be validated against the allow-list. Returns the mime string, or null when absent.
function quotedMime(data, mediaType) {
  const msg = data?.message ?? data;
  const ctx =
    data?.contextInfo ||
    msg?.extendedTextMessage?.contextInfo ||
    msg?.imageMessage?.contextInfo ||
    msg?.videoMessage?.contextInfo ||
    msg?.audioMessage?.contextInfo ||
    null;
  const q = ctx?.quotedMessage;
  if (!q) return null;
  const nodeKey =
    mediaType === "image"
      ? "imageMessage"
      : mediaType === "video"
      ? "videoMessage"
      : mediaType === "audio"
      ? "audioMessage"
      : mediaType === "document"
      ? "documentMessage"
      : null;
  return (nodeKey && q[nodeKey]?.mimetype) || null;
}

// The turn's inbound media LIST for the @mary path. Type-agnostic DETECTION (interpretation is
// gated later, at mediaBlockFor). Reads the direct attachment off data.message.*Message with the
// id from data.key.id (NOT data.message), and REUSES the already-computed `quoted` (getQuoted's
// result) for the quoted file. Returns [] when the turn carries no media. video is DETECTED so it
// can be deferred (mediaBlockFor -> null), never relayed. AUDIO is NOT a relay concern and is
// OMITTED entirely — the AI can't take audio natively; audio is always handled by transcribe_audio
// via NORMAL routing (triggered by ctx.hasQuotedAudio), never intercepted by the media relay. So a
// direct audioMessage attachment and a quoted audio both yield NO entry — the turn routes normally.
//   -> Array<{ source:"attachment"|"quote", mediaType:"image"|"document"|"video",
//              id:string, caption:string, mimetype:string|null }>
export function inboundMedia(data, quoted) {
  const list = [];
  const msg = data?.message || {};

  // Attachment (at most one): first matching node wins, checked in a fixed order. The captioned
  // wrapper (documentWithCaptionMessage) is unwrapped FIRST, then the bare shapes. audioMessage is
  // deliberately NOT detected — audio reaches transcribe_audio via routing, not the relay.
  const docNode =
    msg.documentWithCaptionMessage?.message?.documentMessage || msg.documentMessage;
  let node = null;
  let mediaType = null;
  if (docNode) {
    node = docNode;
    mediaType = "document";
  } else if (msg.imageMessage) {
    node = msg.imageMessage;
    mediaType = "image";
  } else if (msg.videoMessage) {
    node = msg.videoMessage;
    mediaType = "video";
  }
  if (node) {
    list.push({
      source: "attachment",
      mediaType,
      id: data?.key?.id, // the media id lives on data.key.id, never inside data.message
      caption: node.caption || "",
      mimetype: node.mimetype || null,
    });
  }

  // Quote (at most one): reuse getQuoted's result, peek the quoted node's mime read-only. A quoted
  // AUDIO is OMITTED — it is transcribe_audio's trigger, reached by normal routing (ctx.hasQuotedAudio),
  // and must not be intercepted by the relay. ctx.hasQuotedAudio / ctx.quoted are computed elsewhere
  // and stay untouched, so transcribe_audio still fires.
  if (quoted && quoted.mediaType !== "text" && quoted.mediaType !== "audio") {
    list.push({
      source: "quote",
      mediaType: quoted.mediaType,
      id: quoted.id,
      caption: "", // a quoted file carries no caption of its own on THIS turn
      mimetype: quotedMime(data, quoted.mediaType),
    });
  }

  return list;
}

// THE EXTENSION POINT. The single "is this type supported? -> native block, or defer" decision,
// plus the two ship-now native handlers (image, document). media_type comes from `mimetype` (the
// REAL webhook/download mime), NEVER a hard-coded default — so getMediaBase64's audio/ogg fallback
// leaking onto an image is REJECTED here, not trusted. Returns an Anthropic content block, or null
// to defer (the "unsupported-type-yet" reply). A FUTURE format is added HERE (one new branch) +
// its converter — no other rails file changes. (Audio is the exception: it is NOT relayed and never
// reaches this function — inboundMedia omits audio from the list so transcribe_audio handles it via
// normal routing, not the relay.)
export function mediaBlockFor({ mediaType, mimetype, base64 }) {
  if (mediaType === "image" && IMAGE_MIMES.has(mimetype)) {
    return { type: "image", source: { type: "base64", media_type: mimetype, data: base64 } };
  }
  if (
    mediaType === "document" &&
    typeof mimetype === "string" &&
    mimetype.startsWith("application/pdf")
  ) {
    return {
      type: "document",
      source: { type: "base64", media_type: "application/pdf", data: base64 },
    };
  }
  return null; // everything else (docx/xlsx/csv/txt/audio/video/unknown/absent mime) -> defer
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
