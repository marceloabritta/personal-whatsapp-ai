// ============================================================================
//  lib/nativeTools.js  —  NATIVE SERVER-SIDE TOOL BUNDLE (rails, additive).
//  Builds the `tools` array attached to the answer pass (router.answer). Config-driven
//  so enabling/disabling a tool is a one-line env change, never a code change.
//
//  The bundle ships web_search + web_fetch (the `_20260209` dynamic-filtering variants,
//  supported by claude-sonnet-5 with NO beta header). code_execution_20260521 is gated
//  behind NATIVE_CODE_EXEC and defaults OFF: Anthropic warns that declaring the standalone
//  code_execution tool alongside the dynamic-filtering web tools "creates a second execution
//  environment that can confuse the model" (the web tools already run code under the hood).
//  Keeping it a single env flag makes turning it on a one-line change if the human wants it.
//
//  Version strings re-verified against the claude-api skill at build time (card 6c09b8ab):
//  web_search_20260209 / web_fetch_20260209 / code_execution_20260521, no beta header on
//  claude-sonnet-5.
//
//  ON/OFF semantics: NATIVE_TOOLS is the master switch. A value is "on" when it is a
//  non-empty string other than the usual falsey words (off/false/0/no). Unset/empty/off ->
//  the answer pass runs tool-less (an empty array still lets it answer from context), so the
//  feature degrades safely. Master off overrides NATIVE_CODE_EXEC.
// ============================================================================

const DEFAULT_MAX_USES = 5; // WEB_SEARCH_MAX_USES default (the shipped ceiling)

// Env flags are strings. Treat any non-empty value other than the standard falsey words as ON.
function enabled(v) {
  if (v == null) return false;
  const s = String(v).trim().toLowerCase();
  if (!s) return false;
  return !["0", "false", "off", "no"].includes(s);
}

// buildNativeTools(env) -> the tools array for the answer pass.
//   env.NATIVE_TOOLS      — master on/off (off/unset -> []).
//   env.WEB_SEARCH_MAX_USES — max_uses on the search/fetch tool defs (default 5).
//   env.NATIVE_CODE_EXEC  — append code_execution_20260521 when on (default off).
export function buildNativeTools(env = {}) {
  if (!enabled(env.NATIVE_TOOLS)) return [];

  const maxUses = Number(env.WEB_SEARCH_MAX_USES) || DEFAULT_MAX_USES;
  const tools = [
    { type: "web_search_20260209", name: "web_search", max_uses: maxUses },
    { type: "web_fetch_20260209", name: "web_fetch", max_uses: maxUses },
  ];

  if (enabled(env.NATIVE_CODE_EXEC)) {
    tools.push({ type: "code_execution_20260521", name: "code_execution" });
  }

  return tools;
}
