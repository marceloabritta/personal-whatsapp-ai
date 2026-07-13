# Skill: `assistant_settings`

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
> **What's guarded:**
> - **Nothing is applied without a yes.** Anything ambiguous is a no-op (`lib/confirm.js`
>   returns `"unrelated"` on any doubt) and the proposal just stands until it expires.
> - **She can't be left unsummonable.** A tag with no `@`, a tag under 3 characters, a tag with
>   a space, an empty list — all rejected, with the reason, and nothing changes.
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
manifest = { id: "assistant_settings", inputs: null, description: … }
run(ctx)
```

Auto-discovered by the orchestrator (`server.js` `loadSkills()`); no orchestrator or router
edit is needed to reach it — the router's menu is built from the discovered catalog.

- **Fresh (tagged) order** → one LLM call (`prompt.js`, schema `{ tags, reasoning }`) given the
  **live** tag list (`ctx.tags`, never `process.env`) and the order. The model returns the
  **complete** new list — never a delta — plus the prose reasoning. The list is validated with
  `normalizeTags()`; invalid → she sends the reason and asks again (**guidance, not a
  malfunction** — plain `ctx.send`, not `ctx.sendFailure`). Valid → a 15-minute session
  (`stage: "await_confirmation"`, `awaitFrom: "owner"`) and the proposal.
- **Continuation** → `classifyConfirmation()`. `"confirm"` applies; `"decline"` clears and
  acknowledges; `"unrelated"` does **nothing at all** (the safe no-op) and the proposal stands.

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
