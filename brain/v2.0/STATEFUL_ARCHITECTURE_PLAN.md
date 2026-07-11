# Stateful Conversation Architecture — Plan

Plan only. No code changes here. Goal: move the brain from **stateless single-shot**
(every action needs `@brain`) to **stateful conversations** — so a follow-up
(confirmation, clarification, or an edit) continues **without re-tagging `@brain`**.

## Why

Today the orchestrator only acts on messages that start with `@brain`
([server.js](1.%20Orchestrator/server.js)), and each action re-derives everything from the
transcript. That forces `@brain` on every turn and can't hold a multi-step
interaction (confirm → do; ask → answer → do; "change it" → clarify → apply).

The delete flow already needed a hack to work around this: the confirmation
message carries the calendar link, and a tagless "yes" is allowed only because the
quoted message has a link. Sessions replace that hack with real state.

## Core concept: one **session** per chat

A session is a short-lived "pending action" keyed by `remoteJid`:

```jsonc
{
  "skill": "calendar_action",     // which skill owns the follow-up
  "intent": "delete",             // delete | create | edit ...
  "stage": "await_confirmation",  // await_confirmation | await_clarification
  "awaitFrom": "owner",           // who may answer: owner | contact | any
  "data": { "eventId": "…", "title": "…", "when": "…" },  // skill-specific
  "expiresAt": 1720000900         // TTL, e.g. 15 min
}
```

### Who can start vs. continue a flow

- **Only the owner starts a flow**, and only with the `@brain` tag. Non-owner
  messages never start anything.
- **No replies or tags are needed to continue.** While a session is open, the
  owning skill is handed **every** message from the party it waits on
  (`awaitFrom`), and the **LLM decides** whether that message actually supplies the
  awaited info. Normal chatter is ignored **silently** (no nagging, no accidental
  actions); the brain acts only when it detects a real answer.
  - `awaitFrom: "owner"` — messages from the owner (`fromMe`). e.g. the delete
    confirmation: the owner just types "yes"/"no" somewhere in the chat.
  - `awaitFrom: "contact"` — messages from the other person (`!fromMe`). e.g. smart
    scheduling: the attendee types their email in a normal message. (Phase C.)
  - `awaitFrom: "any"` — either.
- **The brain never reacts to its own messages** (they start with the `[AI Brain]:`
  header), so confirmations/prompts it sends don't re-trigger the flow.
- The chatter-vs-answer judgment is a small LLM call per candidate message, given
  the pending question + recent conversation, returning e.g.
  `confirm | decline | unrelated` (delete) — defaulting to "do nothing" on doubt.

- A skill **opens** a session when it needs a follow-up (confirm/clarify).
- The skill **resumes** it on the next message in that chat, **consumes/clears** it
  when the action completes or is cancelled.
- TTL auto-expires stale sessions so an old pending action never hijacks an
  unrelated later message.

## Where state lives — **Redis** (recommended)

Redis is already in the stack (`evolution_redis`, same `evolution-net`, append-only
persistence). It's the natural fit: native TTL (`SET … EX`), survives brain
restarts/deploys, and we can namespace with a `brain:session:<remoteJid>` key
prefix so it never collides with Evolution's own cache.

- New dep: `redis` (node-redis v4). `npm install` runs on container start, so
  adding it to `package.json` + deploy installs it.
- Config: `REDIS_URL` (default `redis://evolution_redis:6379`), passed via compose
  env like the other brain vars.
- **Fallback:** a tiny in-memory `Map` store with manual TTL, used automatically
  when `REDIS_URL` is unset/unreachable — keeps local dev and no-Redis runs working.
  Same interface either way: `getSession / setSession / clearSession`.

Alternative considered — **in-memory only**: zero deps, but every deploy/restart
drops pending sessions. Since we deploy often, Redis is worth it. (We can ship the
in-memory store first and flip to Redis by config if you'd rather stage it.)

## Orchestrator changes ([server.js](1.%20Orchestrator/server.js))

New decision at the top of the webhook, after computing `remoteJid`:

```
load session for remoteJid (if any, non-expired)
isBrainMsg = text starts with "[AI Brain]:"            // the brain's own output

isTagged = fromMe AND text starts with @brain          // only owner starts
isContinuation = session AND NOT isTagged AND NOT isBrainMsg AND:
    (fromMe AND awaitFrom in {owner,any})              // owner follow-up
    OR (!fromMe AND awaitFrom in {contact,any})        // contact follow-up

if isTagged:            fresh command -> clear stale session, ROUTER, dispatch
elif isContinuation:    dispatch to session.skill; the skill LLM-judges the message
                        and acts only if it supplies the awaited info (else silent)
else:                   ignore
```

Notes:
- The blanket `if (!fromMe) return` is gone — a non-owner message can be a valid
  continuation (contact answering), but only when a session waits on the contact.
- No reply requirement: `repliesToBrain` was removed. Chatter filtering moves into
  the skill (LLM), which is where the "detect the missing info" intelligence lives.
- `isBrainMsg` guard stops the brain from reacting to its own sent messages.

- Skills receive `ctx.session` (current session or null) and a `ctx.sessions`
  store (`get/set/clear`), plus `ctx.remoteJid` (already passed).
- On a continuation we **bypass the router** and hand the message to the owning
  skill directly — the skill's own LLM step interprets the follow-up in context.
- A fresh `@brain` command **overrides** a pending session (starting over is always
  possible; you're never stuck).
- This **removes** the calendar-link-in-message hack and the
  `!quoted?.calendarLink` tagless allowance — replaced by session presence.

## Skill contract additions

A skill opts into multi-turn by using the session store:

```js
// open a follow-up
await ctx.sessions.set(remoteJid, { skill:"calendar_action", intent:"delete",
  stage:"await_confirmation", data:{ eventId, title, when } });

// on resume (ctx.session is set), read ctx.session.data, then:
await ctx.sessions.clear(remoteJid);   // when done or cancelled
```

Skills that don't use sessions behave exactly as today.

## Rollout (incremental, each testable)

- **Phase A — foundation + delete.** Build the session store (Redis + in-memory
  fallback), wire the orchestrator, and migrate the **delete confirm** flow to
  sessions. Result: `@brain cancel` (reply to invite) → clean confirmation with
  **no link line** → reply `yes` (no tag) → deleted. Drops the link hack.
  *Test:* the current cancel flow, but the confirmation is link-free and "yes"
  works by session, not by quoted link.
- **Phase B — edit/reschedule (Step 5) via sessions.** "change the time" →
  clarify if ambiguous (session `await_clarification`) → answer (no tag) → apply.
- **Phase C — create clarifications via sessions.** Missing email / headcount
  questions answered without re-tagging `@brain`.

## Edge cases & decisions

- **One session per chat** (sufficient now). A new `@brain` command replaces it.
- **Expiry:** TTL (default 15 min). After that, untagged messages are ignored.
- **Abandonment:** user ignores a pending confirm and later types `@brain <new>` →
  new command wins; old session cleared.
- **Group chats:** keyed by `remoteJid`, so per-conversation isolation holds.
- **Restart safety:** Redis-backed sessions survive brain restarts; in-memory
  fallback does not (acceptable for the fallback).
- **Self-messages:** unchanged — the brain's own sends aren't replies and won't
  resume a session.

## Open decisions
1. **Store:** Redis now (recommended), or ship in-memory first and switch by config?
2. **Scope of first increment:** Phase A only (foundation + delete), then stop for
   your test — recommended — or push straight through B/C?
