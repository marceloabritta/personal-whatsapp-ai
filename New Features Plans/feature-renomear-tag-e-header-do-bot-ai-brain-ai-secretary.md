# Rename bot tag and header: AI Brain → AI Secretary

## Summary
Change the invocation tag from @brain to @secretaria/@secretary (both working) and the reply header to [Marcelo's AI Secretary]: or [Secretaria IA do Marcelo]: depending on the conversation language.

## Problem / motivation
The current name (@brain / [AI Brain]) doesn't reflect the assistant's role as Marcelo's personal secretary.

## User flow (from the user's point of view)
1. Marcelo mentions the bot using @secretaria or @secretary in any conversation.
2. The bot identifies the language from the overall context of the conversation (not just the last message).
3. The bot replies in the same conversation with the header in the matching language: [Secretaria IA do Marcelo]: if the conversation is in PT, [Marcelo's AI Secretary]: if it's in EN.
4. @brain stops working as an invocation tag — if someone types @brain out of habit, the bot silently ignores it, with no warning.

## Actors
- Marcelo
- AI Secretary (bot)

## Data & services touched
Bot name/tag configuration and header template (language-detection logic based on conversation context).

## Edge cases & open questions
- @brain typed out of habit is silently ignored by the bot.
- Mixed-language conversations — which language prevails when reading context (not yet defined).

---
*Drafted by @brain on WhatsApp. Save to the repo and refine.*

---

# Implementation plan

> Grounded in the current code. Paths are relative to the repo root; the app folder is `brain/` today and becomes `secretary/` (see the de-brand section). Line numbers are from the state at planning time — re-verify before editing.
>
> **Scope:** two layers. (1) The feature itself — tag + language-aware header (below). (2) A full **de-brand**: remove *every* occurrence of "Brain" from the codebase — folder, container, identifiers, comments, docs — so the product is uniformly "the Secretary" (an **orchestrator** + **skills**). The de-brand is the larger, riskier half; do it as its own commit **after** the tag/header change lands and is verified, so the two concerns stay separable in history and review.

## Current state (what exists today)

- **Trigger tag** is a single string: `const TAG = (process.env.SECRETARY_TAG || "@brain").toLowerCase()` — [`1. Orchestrator/server.js:38`](brain/1. Orchestrator/server.js#L38). It's matched with `text.toLowerCase().startsWith(TAG)` ([server.js:219](brain/1. Orchestrator/server.js#L219)) and the order is sliced with `text.slice(TAG.length)` ([server.js:243](brain/1. Orchestrator/server.js#L243)). `.env.example:27` documents `SECRETARY_TAG=@brain` as the "single source of truth".
- **Header** is a fixed constant `const HEADER = "[AI Brain]:"` — [server.js:44](brain/1. Orchestrator/server.js#L44) — prepended to every reply in `send()` ([server.js:147](brain/1. Orchestrator/server.js#L147)). The **Feature Requests skill has its own duplicate** `const HEADER = "[AI Brain]:"` ([`2. Skills/4. Feature Requests/skill.js:43`](brain/2. Skills/4. Feature Requests/skill.js#L43)) used to frame the doc caption ([skill.js:288](brain/2. Skills/4. Feature Requests/skill.js#L288)).
- **Language is already detected from conversation context**, not just the last message: the router returns `lang` judged from "the order + recent conversation … the owner's OWN words" ([`1. Orchestrator/router/prompt.js:44-47`](brain/1. Orchestrator/router/prompt.js#L44-L47)), and it persists per-session for continuations (`ctx.lang = session?.lang` — [server.js:287](brain/1. Orchestrator/server.js#L287), [server.js:330](brain/1. Orchestrator/server.js#L330)). **This already satisfies the spec's "identify language by overall conversation context" requirement — no new language-detection logic is needed.**
- **Self-message detection depends on the header.** The bot posts into the owner's own WhatsApp, so its replies arrive with `fromMe: true`. The *only* thing distinguishing a bot message from a genuine owner message is `const isBrainMsg = text.startsWith(HEADER)` ([server.js:213](brain/1. Orchestrator/server.js#L213)). `isBrainMsg` gates continuation detection ([server.js:228](brain/1. Orchestrator/server.js#L228)): a `fromMe` message that is *not* recognized as the bot's own becomes eligible to be read as a continuation answer.

## ⚠️ Critical correctness risk (drives the whole design)

If the header becomes language-variable but `isBrainMsg` still matches only one variant, then a bot reply whose header is in the *other* language will not be recognized as self → with an active owner-awaiting session it gets treated as a continuation → **the bot answers its own message (self-trigger / loop).**

**Therefore self-detection must be decoupled from any single header string.** Detect "is this my own message" against **all** headers the bot can ever emit — both language variants **and the legacy `[AI Brain]:`** (so messages sent before the rename, still present in fetched history/buffers, are recognized).

> **Note (re-verified 2026-07-11, post calendar-edit deploy):** the recently-shipped calendar edit/reschedule feature *widened* this surface — it opens more owner-awaiting sessions (multiple `awaitFrom:"owner"`) and the scheduling flow uses `awaitFrom:"any"` ([Calendar Actions/skill.js:604](brain/2. Skills/1. Calendar Actions/skill.js#L604)), where any `fromMe` message not recognized as the bot's own is eligible as a continuation. This makes robust `isOwnMessage()` detection *more* load-bearing, not less. No new header/tag code was introduced — the feature reuses the existing session mechanism and sends via `ctx.send`, so the language-aware header applies to it automatically.

## Changes

### 1. Centralize identity into one module — `1. Orchestrator/lib/identity.js` (new)
Single source of truth for tags and headers, imported by both the orchestrator and the skill (kills the duplicated `HEADER` const).

```js
// Accepted trigger tags (lowercase). Env is comma-separated; order doesn't matter.
export const TAGS = (process.env.SECRETARY_TAG || "@secretaria,@secretary")
  .split(",").map(s => s.trim().toLowerCase()).filter(Boolean);

// Language-aware reply header. en/pt defined; everything else falls back to en.
const OWNER = process.env.OWNER_NAME || "User";
const HEADERS = {
  en: `[${OWNER}'s AI Secretary]:`,
  pt: `[Secretaria IA do ${OWNER}]:`,
};
export function headerFor(lang) {
  return HEADERS[(lang || "en").toLowerCase()] || HEADERS.en;
}

// Every header the bot can EVER have emitted — current variants + legacy.
// Used only to recognize the bot's own messages; keep legacy forever (cheap).
const LEGACY_HEADERS = ["[AI Brain]:"];
const ALL_HEADERS = [...Object.values(HEADERS), ...LEGACY_HEADERS];
export function isOwnMessage(text) {
  return ALL_HEADERS.some(h => text.startsWith(h));
}

// Returns the matched tag (so callers can slice the right length), or null.
export function matchedTag(text) {
  const low = text.toLowerCase();
  return TAGS.find(t => low.startsWith(t)) || null;
}
```

### 2. `1. Orchestrator/server.js`
- Delete the local `TAG` ([:38](brain/1. Orchestrator/server.js#L38)) and `HEADER` ([:44](brain/1. Orchestrator/server.js#L44)) consts; import `{ TAGS, headerFor, isOwnMessage, matchedTag }` from `lib/identity.js`.
- `send()` ([:145-148](brain/1. Orchestrator/server.js#L145-L148)): `const full = \`${headerFor(lang)}\n\n${body}\`;` — header now follows the reply language. (Body translation via `localizeBody` is unchanged; header is chosen, not translated, and non-en/pt langs get the en header by design.)
- Self-detection ([:213](brain/1. Orchestrator/server.js#L213)): `const isBrainMsg = isOwnMessage(text);`
- Trigger match ([:219](brain/1. Orchestrator/server.js#L219)): `const tag = fromMe ? matchedTag(text) : null; const isTagged = !!tag;`
- Order slice ([:243](brain/1. Orchestrator/server.js#L243)): `const order = isTagged ? text.slice(tag.length).trim() : text.trim();` — uses the matched tag's own length (the two tags differ: `@secretaria`=11, `@secretary`=10).
- `ctx.tag` ([:264](brain/1. Orchestrator/server.js#L264)) is currently a single string. Set it to the matched tag on a fresh command; expose `ctx.tags = TAGS` for any prompt that wants to show all accepted tags. (Grep confirms no skill currently reads `ctx.tag` at runtime, so this is forward-compat only.)

### 3. `2. Skills/4. Feature Requests/skill.js`
- Delete the duplicate `const HEADER` ([:43](brain/2. Skills/4. Feature Requests/skill.js#L43)); import `headerFor` from `../../1. Orchestrator/lib/identity.js` (or wherever the module lands — see note below).
- Doc caption ([:288](brain/2. Skills/4. Feature Requests/skill.js#L288)): `const caption = \`${headerFor(ctx.lang)}\n\n${reply(ctx.lang).docCaption({...})}\`;` so the attachment caption matches the conversation language too.
- *Module-location note:* skills importing from `1. Orchestrator/lib` couples the layers. Cleaner option: put `identity.js` at a shared top-level `brain/lib/` (or `brain/2. Skills/_shared/`) and have both import it. Pick one and keep it consistent; the folder-name spaces already work elsewhere in imports.

### 4. Config — resolve the `SECRETARY_TAG` drift first (compose:63)

There is a **live config drift** that must be settled before touching anything, because it determines what "retire `@brain`" even means in production:

- **Committed compose** sets `environment: SECRETARY_TAG: "@secretary"` ([evolution/docker-compose.yml:63](evolution/docker-compose.yml#L63)).
- **Effective live behavior is `@brain`.** Log evidence (last 96h): **103** owner messages triggered the router via `@brain`; **zero** via `@secretary`/`@secretaria` (the 31+31 occurrences of those strings are *draft text* inside the feature-request spec, not invocations).
- **Root cause — confirmed by droplet reads (2026-07-11):**
  - `/opt/brain/.env` sets **only `OWNER_NAME=Marcelo`** — it does **not** set `SECRETARY_TAG` at all.
  - The live container's env is **`SECRETARY_TAG=@brain`**, **`OWNER_NAME=Marcelo`**.
  - Therefore the tag comes from the compose `environment:` block **baked in when the container was created from an older compose (`@brain`)**. The committed `@secretary` edit ([compose:63](evolution/docker-compose.yml#L63)) was **never deployed** (no recreate since). The drift is an *undeployed compose edit*, not an env_file override.
  - Consequence: the real source of truth for the tag is the **compose file (git-tracked)**, not the env_file. `.env.example:25`'s claim that the env_file is the "SINGLE source of truth" for the tag is **inaccurate** and should be corrected.

**Confirmed values to lock in:**
- `OWNER_NAME=Marcelo` → the header derives cleanly as `[Marcelo's AI Secretary]:` / `[Secretaria IA do Marcelo]:` from `headerFor()`. **No hardcoding needed**; keep `OWNER`-derived.
- Effective tag today is `@brain` (matches the 103-vs-0 log evidence).

**Target end-state — compose is the single source of truth (revised):** the tag already lives in compose (git-tracked, reviewable), and the env_file doesn't set it — so standardize *there*, don't move it to the env_file.
- Set [compose:63](evolution/docker-compose.yml#L63): `SECRETARY_TAG: "@secretaria,@secretary"` (keep the line; it's the SSOT). Leave `OWNER_NAME` in the env_file as-is.
- Do **not** add `SECRETARY_TAG` to the env_file — one place only.
- `.env.example:25-27`: fix the misleading "single source of truth" comment (the tag is set in compose, not here), and show the multi-tag form; note `@brain` is intentionally retired.
- **⚠️ Deploy-safety (important):** the committed compose currently says `@secretary` (single). Any `docker compose up -d` — including the container recreate the de-brand needs — would **immediately flip the live tag to `@secretary`-only**, dropping `@brain` before `@secretaria` is even available. So the **very first change to land** must be setting compose to the `@secretaria,@secretary` list; only recreate the container after both the compose list and the multi-tag-accepting code are in place.

### 5. Cosmetic prompt text
- The doc footer literal "Drafted by @brain on WhatsApp" ([`2. Skills/4. Feature Requests/prompt.js:146`](brain/2. Skills/4. Feature Requests/prompt.js#L146)) still says `@brain`. Low priority; update to `@secretary` (or drop the tag) for consistency. Comments referencing `@brain` (e.g. sessions.js, skill.js headers) are non-functional — update opportunistically, not required.

## "@brain is retired → silently ignored" — already free
Once `@brain` is not in `TAGS`, an owner message starting with `@brain` fails `matchedTag()` → `isTagged` is false → with no active session it hits the `if (!isTagged && !isContinuation) return;` early-out ([server.js:236](brain/1. Orchestrator/server.js#L236)) and is silently dropped (still buffered as context by `remember`). **No warning path, no code, exactly as the spec's edge case requires.**

## Open item carried from the spec
Mixed-language conversations — which language wins for the header — is delegated to the router's existing `lang` judgment ("the owner's OWN words; if unsure, `en`"). No special handling added; if this proves wrong in practice it's a router-prompt tweak, not a structural change.

## Test plan (before deploy)
1. **New tags fire**: `@secretaria <order>` and `@secretary <order>` both trigger; the sliced order excludes the tag exactly (no leading space/letter), including the 10 vs 11 char difference.
2. **Header by language**: a PT conversation replies with `[Secretaria IA do Marcelo]:`; an EN conversation with `[Marcelo's AI Secretary]:`; a long-tail language (e.g. `es`) gets the EN header with a translated body.
3. **No self-trigger (the critical one)**: start a flow that opens an owner-awaiting session (e.g. a clarify loop), let the bot reply, and confirm the bot's *own* reply is not consumed as the continuation — verify with both PT and EN headers, and with a legacy `[AI Brain]:` message sitting in history.
4. **@brain retired**: `@brain <order>` produces no reply and no session, and is present only as buffered context.
5. **Continuation still works**: after a real `@secretary` command opens a session, a plain owner reply (no tag) is still picked up as the continuation.

## Deploy & follow-up
- Ship via the usual git pull + restart; the `SECRETARY_TAG` production env change is the one explicit production write to confirm first.
- **After deploy, update memory**: the `[[brain-trigger-tag]]` memory currently states the trigger is `@brain` — it will be stale. Update it to `@secretaria` / `@secretary` (and that `@brain` is retired) once shipped.
- Per the archive-shipped-plans convention, move this file to `Shipped Features/YYYY-MM-DD - rename-tag-ai-secretary.md` on ship.

---

# Full de-brand: remove every "Brain" from the codebase

Goal: the codebase names the product uniformly as **the Secretary**, composed of an **orchestrator** and **skills**. No "Brain" / "brain" / "@brain" / "AI Brain" survives anywhere — folders, container, identifiers, comments, docs, config. Run this as a **separate commit after** the tag/header feature above is merged and verified.

## Inventory (verify with a fresh `grep -rniE "brain"` excluding `node_modules` before starting)

| Location | Occurrences | Kind |
|---|---|---|
| `brain/` (top-level folder) | 1 | **path — highest blast radius** |
| `brain/1. Orchestrator/server.js` | 12 | 1 var (`isBrainMsg`) + banner + `@brain`/header strings + comments |
| `brain/1. Orchestrator/lib/sessions.js` | 3 | **1 functional (`PREFIX="brain:session:"`)** + comments |
| `brain/1. Orchestrator/lib/evolution.js` | 2 | comments |
| `brain/2. Skills/4. Feature Requests/skill.js` | 3 | 1 `HEADER` string (already handled in feature) + comments |
| `brain/2. Skills/4. Feature Requests/prompt.js` | 2 | doc-footer string + comment |
| `brain/2. Skills/1. Calendar Actions/skill.js` | 3 | comments |
| `brain/2. Skills/1. Calendar Actions/prompt.js` | 3 | comments |
| `brain/2. Skills/2. Audio transcriptions/prompt.js` | 1 | comment |
| `brain/.env.example` | 3 | comments + `@brain` (tag handled in feature) |
| `evolution/docker-compose.yml` | 5 | **service/container name, mount + env_file paths, comments** |
| `ARCHITECTURE.md` | ~23 | docs |
| `README.md` | ~23 | docs |
| `PROJECT_LOG.md` | ~70 | docs (historical log — see note) |

## Naming decisions (confirm before running)
- **App folder:** `brain/` → **`secretary/`**. (Sub-folders `1. Orchestrator/` and `2. Skills/` already carry the right names — leave them.)
- **Prose noun:** "the brain" → **"the secretary"**; "the brain's" → "the secretary's".
- **Container / compose service:** `brain` → **`secretary`**. Note `EVOLUTION_INSTANCE` is *already* `secretary` — that's the WhatsApp instance, a different thing; don't collapse the two.
- **Redis session prefix:** `brain:session:` → **`secretary:session:`** (see operational note).

## The three functional (behavior-affecting) changes — do these carefully
1. **Folder rename `brain/` → `secretary/`** — use `git mv "brain" "secretary"` to preserve history. In-repo imports are all relative (`__dirname`-based, e.g. `SKILLS_DIR` at [server.js:32](brain/1. Orchestrator/server.js#L32)), so **no import paths change**. What *does* change lives outside the JS:
   - `evolution/docker-compose.yml`: `service brain:` → `secretary:`, `container_name: brain` → `secretary`, mount `/opt/brain:/app` → `/opt/secretary:/app`, `env_file: /opt/brain/.env` → `/opt/secretary/.env`, and the two comments ([lines 2, 46-57](evolution/docker-compose.yml#L46-L57)). (Keep the `SECRETARY_TAG` line at [:63](evolution/docker-compose.yml#L63) — it's the tag's source of truth; its **value** is set to `@secretaria,@secretary` by the config step above.)
   - **Production host paths** (explicit go-ahead required): the code is mounted from `/opt/brain` (a symlink → `/opt/personal-whatsapp-ai/brain`). After the folder rename + `git pull`, the on-disk path becomes `/opt/personal-whatsapp-ai/secretary`; update the `/opt/brain` symlink to `/opt/secretary` → `…/secretary` (or repoint the compose mount). Then recreate the container under the new name (`docker compose up -d`, old `brain` container removed).
2. **Redis prefix `brain:session:` → `secretary:session:`** ([sessions.js:17](brain/1. Orchestrator/lib/sessions.js#L17)) — **operational:** any session open at deploy time is keyed under the old prefix and becomes unreachable (its owner's next reply won't be seen as a continuation; they'd just re-issue the command). Sessions are short-lived with a TTL, so the clean move is to **deploy when no flow is mid-conversation**, or accept the one-time orphan. Old keys expire on their own via TTL — no manual cleanup needed.
3. **`isBrainMsg` variable** ([server.js:213, 228](brain/1. Orchestrator/server.js#L213)) — already removed by the feature's identity refactor (replaced by `isOwnMessage(text)`); listed here so the rename sweep doesn't expect it to remain.

## The rest — mechanical text replacements (no behavior change)
- **Comments & banners in `.js`**: the banner `BRAIN (v2.0) — ORCHESTRATOR` ([server.js:2](brain/1. Orchestrator/server.js#L2)) → `SECRETARY — ORCHESTRATOR`; every "the brain …" comment across server.js, sessions.js, evolution.js, and the four skill/prompt files → "the secretary …"; `@brain` in comments/examples → `@secretary`.
- **Doc-footer string** in the Feature Requests skill ([prompt.js:146](brain/2. Skills/4. Feature Requests/prompt.js#L146)): "Drafted by @brain on WhatsApp" → "Drafted by @secretary on WhatsApp".
- **`.env.example`** comments ([lines 1, 25](brain/.env.example#L1)) → say "the secretary / router"; the `SECRETARY_TAG` value is handled in the feature section.
- **Docs** — `ARCHITECTURE.md`, `README.md`: replace "brain" → "secretary" throughout, including the README's setup instruction that says to put the contents of `brain/` in `/opt/brain` (→ `secretary/` in `/opt/secretary`). **`PROJECT_LOG.md` is a historical log** — do *not* rewrite past entries (they record what happened, when the thing was called "brain"); instead add a new dated entry noting the rename. Decide this explicitly rather than blanket-replacing.

## Suggested execution order (as one dedicated commit/PR)
1. `git mv brain secretary`; confirm the app still boots locally (`npm install && npm start` from `secretary/`) — proves the relative imports survived.
2. Sweep JS comments/banners/strings (functional `PREFIX` + cosmetic text) with review, not blind `sed` — several "brain" words sit next to code you don't want to touch.
3. Update `evolution/docker-compose.yml` (names + paths).
4. Update `.env.example`, `ARCHITECTURE.md`, `README.md`; add a `PROJECT_LOG.md` entry.
5. Grep-gate: `grep -rniE "brain" . | grep -v node_modules | grep -v PROJECT_LOG.md` must return **zero** (aside from intentionally-preserved history) before opening the PR.
6. Deploy: host path/symlink update + container recreate under the `secretary` name (production writes — explicit go-ahead).

## Verification specific to the de-brand
- App boots from `secretary/` and the container comes up healthy under the new name.
- A `@secretary` command round-trips end-to-end (proves mount path, env_file, and Redis prefix all line up after the moves).
- The self-message / continuation flow still works (re-run the feature's test #3 — the folder + prefix churn shouldn't affect it, but it's the highest-consequence path).
- **Memory:** after ship, `[[brain-trigger-tag]]` and `[[droplet-access]]` both reference "brain"/`/opt/brain` — update both (or delete/replace) to the new names.
