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
- **Continuation depends on `awaitFrom`** — because a stateful flow may involve the
  brain talking to the *other person* in the chat (through the owner's account):
  - `awaitFrom: "owner"` — the owner answers, but **only by replying to a brain
    message** (so the brain never grabs the owner's normal chatter). Used by the
    delete confirmation.
  - `awaitFrom: "contact"` — the *other person* answers with **any normal message**
    (not necessarily a reply). Used by smart scheduling: when an attendee's email is
    missing, the brain can ask the contact, and capture the email from their plain
    reply. (Phase C.)
  - `awaitFrom: "any"` — either, by the rules above.

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

isTagged = fromMe AND text starts with @brain          // only owner starts
isContinuation = session exists AND:
    (fromMe AND repliesToBrain AND awaitFrom in {owner,any})   // owner follow-up
    OR (!fromMe AND awaitFrom in {contact,any})                // contact follow-up

if isTagged:            fresh command -> clear stale session, ROUTER, dispatch
elif isContinuation:    dispatch straight to session.skill with ctx.session
else:                   ignore
```

Note the blanket `if (!fromMe) return` is gone — a non-owner message can now be a
valid continuation (contact answering), but only when a session explicitly waits on
the contact; otherwise it's still ignored.

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
