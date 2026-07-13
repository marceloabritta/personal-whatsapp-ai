// ============================================================================
//  identity.js  —  the secretary's TAGS + reply HEADER, in one place.
//  Single source of truth for (a) which trigger tags start a flow and (b) the
//  header the secretary stamps on every outgoing message. Both the orchestrator
//  and any skill that frames its own message (e.g. Feature Requests' doc caption)
//  import from here so the tag list and header can never drift between files.
// ============================================================================

// Accepted trigger tags (lowercase). SECRETARY_TAG is comma-separated so more than
// one tag can trigger the secretary (e.g. a PT and an EN spelling). Order is
// irrelevant — matchedTag() returns whichever the message actually starts with.
export const TAGS = (process.env.SECRETARY_TAG || "@assistente,@assistant")
  .split(",")
  .map((s) => s.trim().toLowerCase())
  .filter(Boolean);

const OWNER = process.env.OWNER_NAME || "User";

// Language-aware reply header. en/pt are written natively; any other language
// falls back to the English header (the body is still translated by send()).
const HEADERS = {
  en: `[${OWNER}'s AI Assistant]:`,
  pt: `[Assistente IA do ${OWNER}]:`,
};

// The header to stamp on an outgoing message for `lang`.
export function headerFor(lang) {
  return HEADERS[(lang || "en").toLowerCase()] || HEADERS.en;
}

// Every header the assistant could EVER have emitted — current variants plus every
// retired one ("[AI Brain]:", and the "AI Secretary"/"Secretaria IA" pair) so its own
// older messages, still sitting in fetched history / the in-memory buffer, are
// recognized as its own and never re-consumed as an owner reply.
// Keep legacy entries here forever; they cost nothing.
const LEGACY_HEADERS = [
  "[AI Brain]:",
  `[${OWNER}'s AI Secretary]:`,
  `[Secretaria IA do ${OWNER}]:`,
];
const ALL_HEADERS = [...Object.values(HEADERS), ...LEGACY_HEADERS];

// Is this text one of the secretary's OWN messages? Since its replies arrive with
// fromMe=true (same WhatsApp account as the owner), this header check is the ONLY
// thing separating a bot message from a genuine owner message — it MUST recognize
// every header variant or the bot can read its own reply as a continuation.
//
// The header goes out BOLD (`*[...]:*` — see lib/format.js), so the text starts with
// a `*`. We strip leading WhatsApp markers before matching, which recognizes both the
// bold header and the older unbolded ones still sitting in history / the buffer.
export function isOwnMessage(text) {
  const t = (text || "").replace(/^[*_~\s]+/, "");
  return ALL_HEADERS.some((h) => t.startsWith(h));
}

// The trigger tag this message starts with (for slicing it off the order), or null.
// Tags can differ in length (e.g. "@secretaria"=11 vs "@secretary"=10), so callers
// must slice by the RETURNED tag's length, not a fixed constant.
export function matchedTag(text) {
  const low = text.toLowerCase();
  return TAGS.find((t) => low.startsWith(t)) || null;
}
