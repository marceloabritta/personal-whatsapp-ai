// ============================================================================
//  LANGUAGE PIN POLICY (rails) — card 3ec5be77.
//  Pure, dependency-free so the regression test can import it offline
//  (server.js self-starts an Express listener with top-level await and cannot
//  be imported). Owns the maintained-language set and the pin/hold rule.
// ============================================================================

// The two languages the repo writes natively; everything else goes through the
// localizeBody translation fallback. Moved here from server.js so the pin policy
// and the maintained-language set live in one rails module.
export const MAINTAINED_LANGS = new Set(["en", "pt"]);

const norm = (v) =>
  typeof v === "string" && v.trim() ? v.trim().toLowerCase() : null;

// THE PIN/HOLD RULE. `pinnedLang` is the conversation's opening language (from the
// session marker, read BEFORE any clear); `routerLang` is the router's per-turn
// detected lang. An ongoing conversation HOLDS its opening language; only the first
// turn of a NEW conversation (no pin yet) adopts the router's lang. Default "en".
export function resolveTurnLang(pinnedLang, routerLang) {
  return norm(pinnedLang) || norm(routerLang) || "en";
}

// Whether the model-authored `reply.say` prose must be force-translated to the
// pinned language. Only the maintained-language residual (en↔pt) needs it —
// localizeBody already translates say into any NON-maintained pinned lang inside
// send(). True only when both langs are maintained and differ.
export function shouldForceTranslateSay(sayLang, targetLang) {
  const s = norm(sayLang), t = norm(targetLang);
  return !!s && !!t && s !== t && MAINTAINED_LANGS.has(s) && MAINTAINED_LANGS.has(t);
}

// Whether localizeBody must actually invoke the translate model. Returns FALSE (→ caller
// returns the text unchanged, no LLM call) when the body is already in the target — the
// no-op guard that stops the cheap model being asked to "translate X into X" (card bea6dea5)
// — OR when the existing maintained-language early-return applies. `sourceLang` is the
// body's known source language when the caller has one (undefined otherwise).
export function translationNeeded(sourceLang, targetLang, { force = false } = {}) {
  const s = norm(sourceLang), t = norm(targetLang) || "en";
  if (s && s === t) return false;                                  // NEW: never translate X→X
  if (!force && (MAINTAINED_LANGS.has(t) || t === "en")) return false; // preserve existing early-return
  return true;
}
