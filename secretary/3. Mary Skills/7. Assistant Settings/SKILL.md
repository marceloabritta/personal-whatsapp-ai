# Skill: `assistant_settings`

> **@mary tree — the converted PILOT, copied VERBATIM.** This is the `secretary/3. Mary Skills/`
> copy of the pure-task reference. No code change was needed (it was already
> `conversation:"orchestrator"` with declared `inputs`); it exists as a physical copy so the @mary
> stack is self-contained, and it mutates the NEW flow's tag list (`setNewTags` / `newSettings`).
> The rest of this doc already describes the converted shape.

> **For humans — quick read.**
>
> Change how you summon her, by asking her.
>
> Until now, the tag you call her with lived in `SECRETARY_TAG` on the droplet: to change it
> you edited the compose file and recreated the container. Now you just tell her.
>
> ```
> ME:        @assistant, change your tag to @assist
> ASSISTANT: You call me @assistant in English and @assistente in Portuguese. @assist is the
>            natural short form of both, so I'd answer to just @assist in both languages and
>            retire the other two.
>
>            My tags would then be: @assist.
>
>            Confirm? I'll hold this for 15 minutes.
> ME:        yes
> ASSISTANT: Done. Call me with @assist. @assistant and @assistente no longer work.
> ```
>
> **The middle message is the point.** You said nothing about Portuguese. She *deduced* what
> should happen to the other language's call, **said her reasoning out loud**, and showed you
> the **complete** list she'd end up with — so you confirm something you can actually see. There
> is no tag→language table anywhere in the product; she reasons about it from the words, in the
> moment, every time.
>
> **Who runs that conversation changed (card 55e00052).** This skill is now a **converted skill**
> (`conversation: "orchestrator"`): the *orchestrator's model* runs the proposal and the
> confirmation, and only dispatches this skill once you've agreed. This skill no longer asks or
> confirms anything — it just applies the change, tells you the outcome, and is done. From your
> side the exchange above is unchanged.
>
> **What's guarded:**
> - **Nothing is applied without a yes.** The model proposes and waits; it only executes this
>   skill after your agreeing message. A read-back turn can never trigger a second write (the
>   orchestrator's write invariant).
> - **She can't be left unsummonable.** A tag with no `@`, a tag under 3 characters, a tag with
>   a space, an empty list — all rejected by `normalizeTags()`, and the payload never even
>   reaches this skill (the orchestrator gates on it before dispatch).
> - **She never claims a save she didn't get.** If the settings store is unreachable she says
>   the change is live *but not saved* and will not survive a restart. See "Persistence".
>
> **If you ever lock yourself out** — a tag you can't type, a tag you forgot — clear the stored
> setting on the droplet and restart; she goes back to the `SECRETARY_TAG` seed:
>
> ```
> docker exec evolution_redis redis-cli DEL secretary:settings:tags
> ```
>
> A restart **alone does not** undo a tag change: the store outlives it. That command is the
> recovery path.

---

## Contract

```
manifest = {
  id: "assistant_settings",
  conversation: "orchestrator",          // the model runs the dialogue; this skill just acts
  inputs: {                              // the ONE thing it needs, declared for the turn call
    fields: { tags: { type: "array", of: { type: "string" }, desc: … } },
    consistency: [{ name: …, test: (info) => normalizeTags(info.tags).ok }],
    rulebook: () => …,
  },
  description: …,
}
run(ctx) -> { ok, persisted, tags, retired }   // the return value is read back by the model
```

Auto-discovered by the orchestrator (`server.js` `loadSkills()`); no orchestrator or router
edit is needed to reach it — the router's menu is built from the discovered catalog.

**A converted skill (`conversation: "orchestrator"`).** The proposal, the reasoning about the
other language's tag, and the confirmation are all run by the **orchestrator's model** (see
`../../1. Orchestrator/ORCHESTRATOR.md` → "The conversation loop"). By the time `run(ctx)` is
called, the model has proposed and the owner has agreed, and the **validated** tag list arrives in
`ctx.info.tags` (the orchestrator gated it on `checkPayload`'s `ok` tier — shape *and*
`normalizeTags` consistency — before dispatching). So `run()` does exactly this:

1. `normalizeTags(ctx.info.tags)` — a defensive re-check (belt to the gate's braces); a failure is
   a genuine malfunction and goes through `ctx.sendFailure` (`thinkingError`).
2. Snapshot the **retired** tags against the **live** list (`ctx.tags`, never `process.env`).
3. `settings.saveTags()` (persist) then `setNewTags()` (apply live) — in that order. **Dual-tag
   run:** as the NEW (@mary) flow's pilot, this converted skill mutates the NEW tag list
   (`NEW_TAGS` via `setNewTags`) and its own namespaced store, **never** the legacy (@assistant)
   `TAGS`/`setTags` — that separation is what lets @mary be tested without touching @assistant. `ctx.tags`
   and `ctx.settings` are already the new flow's (the orchestrator builds `ctx` per flow). The frozen
   legacy propose/confirm skill under `1. Orchestrator/legacy/` is the one that still calls `setTags`.
4. Send **exactly one** outcome message (`applied` / `appliedNotSaved`), then **return**
   `{ ok, persisted, tags, retired }` for the model to read back.

No session, no `classifyConfirmation`, no propose/resume machinery, no LLM call of its own — all of
that moved to the orchestrator, and the dead code + dead reply keys (`propose`, `invalid`,
`declined`, `PROPOSE_SCHEMA`, `buildProposeSystem/User`) were deleted from `skill.js` / `prompt.js`.

## Persistence

`SECRETARY_TAG` is the **seed**. The stored value, when present, **wins** — `server.js` reads it
at boot (`await settings.ready` first, or the read would race the Redis connect and silently fall
back to the seed) and logs which source won:

```
tags: @assist (source: stored setting)
tags: @assistente, @assistant (source: SECRETARY_TAG seed)
```

The store is `lib/settings.js` — the same Redis the sessions already use (`--appendonly yes` on a
named volume, so it survives restart *and* redeploy), key `secretary:settings:tags`, **no TTL**.

If Redis is unreachable it degrades to memory exactly as `lib/sessions.js` does, and
**`saveTags()` returns `false`**. That boolean is the only thing that decides what she claims:
the success message is sent from the branch that actually wrote the store, and the other branch
tells him the truth. This is also why a bad tag can never become permanent by accident.

## The prefix trap (`lib/identity.js`)

The owner may legitimately land on a list where one tag is a **prefix** of another — `@assist`
alongside `@assistente` is exactly what the example above produces. `matchedTag()` therefore
matches the **longest tag first**, and a tag must **end** at a word boundary:

- Longest-first, or `"@assistente marque uma reunião"` would match the shorter `@assist`, and
  `server.js` — which slices the order by the matched tag's length — would hand the router
  `"ente marque uma reunião"`. Every Portuguese command, silently corrupted.
- The tag must end, or a **retired** tag that merely *extends* a live one keeps working: with
  `@assist` live, `"@assistant do X"` still starts with `@assist`, and the router would get
  `"ant do X"`. A retired tag has to be *gone*, not half-working.

It sorts a **copy**: `TAGS[0]` is the primary tag (`ctx.tag` falls back to it), so the owner's
order is preserved. `setTags()` mutates `TAGS` **in place** for the same reason — every reader
that already holds the array (including `server.js`'s per-turn `ctx` build) must see the change.

Guarded by `scripts/settings-tag-selftest.mjs` (the apply→live→persist flow) and assertion 9 of
`scripts/identity-selftest.mjs`.

## Not here

- **No alias and no grace period.** The confirmed list replaces the old one outright.
- **No tag→language data model.** `TAGS` is a flat list; the deduction happens in prose, per turn.
- **The reply header is untouched.** `headerFor()`/`HEADERS` derive from `OWNER_NAME`, not from
  the tag — a different value with different wiring.
- **Per-chat tags.** The tag list is global to the assistant.
