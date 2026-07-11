# Structured Outputs via SDK bump — Implementation Plan

> **Status (2026-07-11): BUILT, not deployed.** Phases 0–3 done — 4 JSON Schemas in
> `prompt.js`, `output_config.format` wired into all 4 `messages.create` calls,
> prompts trimmed of the redundant schema echo, `@anthropic-ai/sdk` bumped to
> `^0.111.0`. Mocked dry-run harness 43/43; schemas statically validated. **Gate before
> deploy: the live smoke test** (`scratchpad/calendar-structured-smoke.mjs`) must pass
> against real Sonnet 5 — it needs a real API key + the installed SDK, which the build
> environment lacked. Deploy = normal `git pull` + `restart` (auto-installs the SDK).

Replace the hand-rolled JSON extraction (`parseJsonReply` + "reply ONLY with JSON"
prompts) with **native Anthropic structured outputs**, so the API *guarantees*
schema-valid JSON for every calendar LLM call. Follow-up to the 2026-07-11 prompt
pass, which shipped `parseJsonReply` as the safe, SDK-agnostic interim.

## Why
- `parseJsonReply` is a *net*, not a guarantee: it recovers most malformed replies
  but a genuinely off-shape reply still yields `null` → a silently dropped action.
- `output_config.format` with a JSON Schema makes the model emit **only** valid,
  schema-conforming JSON. No regex, no fence-stripping, no balanced-brace scan.
- Removes the hand-written schema echo from each prompt (the schema now lives in
  code and is enforced structurally), leaving prompts to describe field *meaning*.

## Current state (facts)
- SDK: `@anthropic-ai/sdk` pinned **`^0.30.1`** — predates `output_config.format`.
- Model: **`claude-sonnet-5`** (`CLAUDE_MODEL` default) — supports structured outputs.
- 4 JSON calls, all in `2. Skills/1. Calendar Actions/`: `interpret` (buildSystem),
  `classifyConfirmation` (buildConfirmSystem), `reviewCreate` (buildCreateReviewSystem),
  `inspectMissing` (buildResolveSystem). All currently parse via `parseJsonReply`.
- **Deploy mechanics (important):** brain container is `node:20-alpine`, command
  `sh -c "npm install --no-audit --no-fund && npm start"`, and there is **no
  `package-lock.json`** → `npm install` runs on every container start. So bumping the
  version range in `package.json` + the normal `git pull` + `restart` **auto-installs
  the new SDK** — no extra deploy step.

## Constraints from the Anthropic API (verified against the claude-api reference)
- **Schema rules:** every object needs `additionalProperties: false` + `required`.
  Nullable field → `{"type": ["string","null"]}`; either/or → `anyOf`. Supported:
  object/array/string/integer/number/boolean/null, enum, const, anyOf/allOf, $ref,
  string formats (date-time…). **Not** supported: min/max, minLength/maxLength,
  recursive schemas. (The TS SDK strips unsupported constraints and validates
  client-side, so keep schemas simple.)
- **First-request latency:** a new schema compiles once, then is cached ~24h. Four
  distinct schemas → four one-time compiles. Acceptable.
- **Refusal:** `stop_reason:"refusal"` → output may not match schema (content empty
  or partial). Must branch on `stop_reason` and return null (→ no-action, safe).
- **Incompatible with:** citations, assistant prefill. We use neither. Works with
  thinking, streaming, token counting.
- **Model support:** Sonnet 5 ✓ (also Opus 4.8, Haiku 4.5). If `CLAUDE_MODEL` is ever
  set to an unsupported model, structured outputs 400s — keep `parseJsonReply` as a
  fallback so a model swap degrades gracefully instead of breaking.

## Plan

### Phase 0 — Recon (read-only)
- Confirm target SDK version: latest stable `@anthropic-ai/sdk` whose `engines`
  allow Node 20 (recent versions support Node 18+ — expected fine; verify to avoid a
  base-image bump). Record the exact version to pin the range to.
- Confirm prod `CLAUDE_MODEL` is a structured-outputs model (Sonnet 5).

### Phase 1 — Author the 4 schemas (in `prompt.js`, next to each prompt)
1. **interpret** — `{action(enum create|delete|other), title(str|null),
   participants[{name(str|null),email(str|null)}], start_iso(str|null),
   duration_min(number|null), summary(str)}`.
2. **confirm** — `{decision(enum confirm|decline|unrelated)}`.
3. **review** — `{decision(enum confirm|modify|cancel|unrelated), …same draft fields
   as interpret}`.
4. **resolve** — `{start_iso(str|null), participants(anyOf null | array of
   {name,email})}`.
All objects `additionalProperties:false`; every key in `required` (optionals use a
null-union type). Export each schema alongside its `build*System`.

### Phase 2 — Wire structured outputs (in `skill.js`)
- Add `output_config: { format: { type: "json_schema", schema } }` to each of the 4
  `anthropic.messages.create(...)` calls (raw JSON schema — no zod dependency).
- Parse the (now-guaranteed-valid) first text block directly; **keep `parseJsonReply`
  as the fallback** only when `output_config` isn't honored (refusal / unsupported
  model). Add a `stop_reason === "refusal"` guard → return null.
- Trim the prompts: drop the literal `{…schema…}` echo and "reply ONLY with valid
  JSON" lines (structure is enforced now); keep the field-*semantics* rules.
- Bump `@anthropic-ai/sdk` range in `brain/package.json` to the Phase-0 version.

### Phase 3 — Test
- **State machine:** dry-run harness (mocked SDK) stays valid — 40/40 should still
  pass; the mock is unaffected by `output_config`.
- **Live smoke test (the critical new one):** a scratchpad script that, with a real
  `ANTHROPIC_API_KEY`, calls Sonnet 5 with each of the 4 schemas on a representative
  prompt and asserts the reply parses + matches the schema. This is the only way to
  confirm the bumped SDK + API actually enforce `output_config` — the mock can't.
- **Install check:** `npm install` in a throwaway worktree with the bumped
  `package.json`; `node --check` both files; run the harness against the real SDK.

### Phase 4 — Deploy (same workflow, no extra step)
- `git commit` (package.json + prompt.js + skill.js) → `git push`.
- Droplet: `git pull --ff-only` → `docker compose restart brain` — the restart re-runs
  `npm install` and picks up the new SDK automatically.
- Verify boot logs (`skill loaded … calendar_action`, `listening on port 3000`) and
  do one live create to confirm structured JSON round-trips end to end.
- **Rollback:** `git revert` the bump commit → restart (re-installs the old SDK). No
  symlink/image surgery needed.

## Risks & mitigations
| Risk | Mitigation |
|---|---|
| SDK major bump breaks `messages.create` shape | Core call is stable across the range; live smoke test + worktree install before deploy |
| Target SDK needs Node > 20 | Check `engines` in Phase 0; if so, that's a base-image bump (bigger scope — decide before starting) |
| Nullable/anyOf schema fiddliness | Keep schemas minimal; the live smoke test catches a wrong schema fast |
| Model swapped to a non-supporting model later | Keep `parseJsonReply` fallback + refusal guard so it degrades, not breaks |
| Unpinned installs drift on each boot (pre-existing) | Optional: add `package-lock.json` + switch container cmd to `npm ci` for reproducible deploys (separate improvement) |

## Decision points (for you)
1. **Raw JSON schema vs zod** — recommend raw (no new dependency, schema co-located
   with the prompt).
2. **Keep `parseJsonReply` as fallback** — recommend yes (cheap safety for
   refusal/model-swap).
3. **Add a lockfile + `npm ci`** now or defer — recommend defer (out of scope, but
   worth doing eventually for reproducible deploys).
4. **Node base-image bump** — only if Phase-0 shows the target SDK requires it.

## Suggested order
Phase 0 (pick SDK version, confirm Node) → 1 (schemas) → 2 (wire + bump) → 3 (live
smoke test on a branch) → 4 (deploy + verify). Gate on the live smoke test passing.
