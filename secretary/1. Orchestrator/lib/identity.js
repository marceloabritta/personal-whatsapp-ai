// ============================================================================
//  identity.js  —  the secretary's TAGS + reply HEADER, in one place.
//  Single source of truth for (a) which trigger tags start a flow and (b) the
//  header the secretary stamps on every outgoing message. Both the orchestrator
//  and any skill that frames its own message (e.g. Feature Requests' doc caption)
//  import from here so the tag list and header can never drift between files.
// ============================================================================

// Accepted trigger tags (lowercase). SECRETARY_TAG is the SEED — comma-separated, so more
// than one tag can trigger the secretary (e.g. a PT and an EN spelling). The owner can then
// CHANGE the list at runtime by asking her ("change your tag to @assist" — the
// assistant_settings skill), and server.js loads the stored list over this seed at boot.
//
// ORDER MATTERS, in one specific way: TAGS[0] is the PRIMARY tag — server.js:306 falls back
// to it for ctx.tag. So the owner's order is preserved as given, and matchedTag() sorts a
// COPY when it needs length order. Never sort this array itself.
export const TAGS = seed();

function seed() {
  return (process.env.SECRETARY_TAG || "@assistente,@assistant")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
}

// Minimum viable tag: "@" + 2 chars. Below that a stray "@a" in a sentence would start
// hijacking orders, and she can never be left unsummonable.
const MIN_TAG_LEN = 3;

// Validate + canonicalize a proposed tag list. PURE — no Redis, no env, no side effects, so
// the skill can check a proposal BEFORE offering it and the store can refuse a bad write,
// both from the one definition that sits next to the thing it constrains.
// -> { ok: true, tags } | { ok: false, problem }
export function normalizeTags(list) {
  if (!Array.isArray(list)) return { ok: false, problem: "not a list of tags" };

  const tags = [];
  for (const raw of list) {
    if (typeof raw !== "string") return { ok: false, problem: "not a list of tags" };
    const t = raw.trim().toLowerCase();
    if (!t) continue;
    if (!t.startsWith("@"))
      return { ok: false, problem: `"${t}" must start with @` };
    if (t.length < MIN_TAG_LEN)
      return {
        ok: false,
        problem: `"${t}" is too short — a tag needs @ plus at least ${MIN_TAG_LEN - 1} characters`,
      };
    if (/\s/.test(t)) return { ok: false, problem: `"${t}" cannot contain spaces` };
    if (!tags.includes(t)) tags.push(t); // dedupe, first spelling wins
  }

  if (!tags.length)
    return { ok: false, problem: "the tag list would be empty — you could not summon me" };

  return { ok: true, tags };
}

// Replace the accepted tags at runtime. MUTATES THE ARRAY IN PLACE — `TAGS` stays a `const`
// export and every reader that already holds the array sees the change. This is not a style
// choice: `scripts/identity-selftest.mjs:46` destructures on import (`const { TAGS } = await
// import(...)`), which SNAPSHOTS the binding, so an `export let` + reassign would be invisible
// to it — and to any other reader that grabbed the array once. In-place mutation is visible
// to all of them, including server.js's per-turn ctx build.
// Refuses an invalid list (returns false) rather than leaving her unsummonable.
export function setTags(list) {
  const { ok, tags } = normalizeTags(list);
  if (!ok) return false;
  TAGS.length = 0;
  TAGS.push(...tags);
  return true;
}

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

// A tag ends where the word ends: end-of-message, a space, a newline, a comma, a colon.
// Another LETTER OR DIGIT means the message opened with a different word that merely BEGINS
// with a tag — not the tag itself.
const endsTag = (ch) => ch === "" || !/[\p{L}\p{N}_]/u.test(ch);

// The trigger tag this message starts with (for slicing it off the order), or null.
// Tags can differ in length (e.g. "@secretaria"=11 vs "@secretary"=10), so callers
// must slice by the RETURNED tag's length, not a fixed constant.
//
// TWO PREFIX TRAPS, both live now that the owner can CHANGE his tags at runtime and may
// legitimately land on a list where one tag is a prefix of another (e.g. @assist + @assistente):
//
//   1. LONGEST FIRST. First-match-wins would match "@assistente marque uma reunião" against
//      the shorter "@assist", server.js:285 would slice 7 chars, and the router would be handed
//      "ente marque uma reunião" — every Portuguese command silently corrupted. So: longest
//      tag first. Sort a COPY — TAGS[0] is the PRIMARY tag (server.js:306 falls back to it for
//      ctx.tag) and must keep the owner's order; sorting TAGS itself would quietly re-elect it.
//
//   2. THE TAG MUST END. Length order alone is not enough, because the trap also springs on a
//      word that is NOT in the list: with "@assist" live, the RETIRED "@assistant do X" still
//      starts with "@assist", so she would answer a tag she no longer has and hand the router
//      "ant do X". A retired tag must be GONE, not half-working. Hence endsTag().
export function matchedTag(text) {
  const low = (text || "").toLowerCase();
  return (
    [...TAGS]
      .sort((a, b) => b.length - a.length)
      .find((t) => low.startsWith(t) && endsTag(low.charAt(t.length))) || null
  );
}
