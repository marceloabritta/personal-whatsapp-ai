# Calendar — Read / Query Events (Implementation Plan)

Split out of the now-retired `calendar-actions.md` backlog (2026-07-11). The `calendar_action`
skill (create / edit / delete) is **shipped and live**; this plan adds a **read-only** fourth
action so the owner can ask what's on the calendar.

Skill source: [`skill.js`](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js) +
[`prompt.js`](../secretary/2.%20Skills/1.%20Calendar%20Actions/prompt.js). Behaviour reference:
[`SKILL.md`](../secretary/2.%20Skills/1.%20Calendar%20Actions/SKILL.md).

## Goal

Answer questions like *"@secretary what's on my calendar tomorrow?"*, *"@secretary do I have
anything Friday afternoon?"*, *"@secretary what's my next meeting?"* — a **read-only `list`
action** that lists events in a resolved window. **No session, no confirm** (nothing is
written), so it's the simplest of the four actions.

## Why it's low-risk

- Read-only: it never touches `events.insert/patch/delete`, so there's no draft, no
  `await_*` session, no `awaitFrom` — `run()` just formats and replies.
- The window-resolution and formatting reuse machinery that already exists: the `interpret`
  call, `events.list` (as in `matchEventTargets`), `localizeDate`, and the per-language
  `REPLY` map.

## Behaviour (what the owner sees)

> You (`@secretary what's on my calendar tomorrow?`)
>
> [Marcelo's AI Secretary]:
> Here's Jul 12, 2026:
> - 9:00 AM — Standup (30 min)
> - 3:00 PM — Q3 budget review · ana@example.com (45 min)
> - 6:30 PM — Gym

Empty window:
> Nothing on your calendar for Jul 12, 2026.

- Single one-off ("what's my next meeting?") → resolve a forward window, show the first event.
- No time expressed ("what's on my calendar?") → default window = **today** (rest of today
  from now), stated explicitly in the reply so the owner knows what was assumed.

## Implementation

### 1. Extend the action enum + add window fields (`CAL_SCHEMA`, prompt.js:25)
Add `"list"` to the `action` enum ([prompt.js:30](../secretary/2.%20Skills/1.%20Calendar%20Actions/prompt.js#L30))
and two nullable window fields the LLM fills only for a list:
```jsonc
action: { enum: ["create", "delete", "edit", "list", "other"] },
// ...existing fields...
range_start_iso: { type: ["string","null"] },   // window start (ISO, -03:00); null → default
range_end_iso:   { type: ["string","null"] },    // window end;  null → derive from start
```
Add both to `required` (schema is `additionalProperties:false` with full `required`) — they're
`null` for every non-list action, which the handlers already ignore.

Keeping this in the **single** `interpret` call is consistent with the skill's stated
"one LLM call, don't decompose" decision — resolving "tomorrow afternoon" needs the same
date-reasoning `interpret` already does for `start_iso`.

### 2. Prompt copy (`buildSystem`, prompt.js:130)
Add a `"list"` bullet to the "Choosing action" block and a "For action=list" section:
- `"list"`: the owner is ASKING what's scheduled — querying/reading, not creating or changing.
- `range_start_iso` / `range_end_iso`: the window the question implies, converted from relative
  phrases via the provided current date/time. "tomorrow" → that whole day; "Friday afternoon"
  → Fri 12:00–18:00; "next meeting"/"what's on my calendar" with no range → leave both `null`
  (code defaults to *now → end of today*, or a forward scan for "next").

### 3. Dispatch + handler (`run`, skill.js:357; new `handleList`)
Add one dispatch line after the edit branch
([skill.js:359](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js#L359)):
```js
if (info?.action === "list") return handleList(ctx, info);
```
```js
async function handleList(ctx, info) {
  const { env, number, send } = ctx;
  const now = new Date(ctx.nowIso || Date.now());               // pass nowIso in ctx if available; else Date.now via server
  const startMs = info.range_start_iso ? new Date(info.range_start_iso).getTime() : now.getTime();
  const endMs   = info.range_end_iso   ? new Date(info.range_end_iso).getTime()
                                       : endOfLocalDay(startMs); // default: through end of that day (CAL_TZ)
  let items;
  try {
    const cal = calendarClient(env);
    const r = await cal.events.list({
      calendarId: calId(env),
      timeMin: new Date(startMs).toISOString(),
      timeMax: new Date(endMs).toISOString(),
      singleEvents: true,
      orderBy: "startTime",
      showDeleted: false,
      maxResults: 50,
    });
    items = (r.data.items || []).filter((e) => e.status === "confirmed");
  } catch (e) {
    console.error("Calendar list error:", e?.response?.data || e?.message || e);
    return send(number, reply(ctx.lang).listError());
  }
  await send(number, reply(ctx.lang).listEvents({ startMs, endMs, items, lang: ctx.lang }));
}
```
Helpers: `endOfLocalDay(ms)` (23:59:59 in `CAL_TZ`) and, for "next meeting", a forward window
(now → now+14d) capped to the first item. Reuse `CAL_TZ`/`REPLY_TZ` already defined.

### 4. Reply renderers (`REPLY.en` / `REPLY.pt`, prompt.js ~L387)
Add `listEvents({ startMs, endMs, items, lang })`, `listEmpty({ startMs })` (fold into
`listEvents` when `items` is empty), and `listError()`. Each event line: `localizeDate(lang, …)`
for the time (time-only vs. full date depending on whether the window spans one day),
title, optional attendee emails, optional `(duration min)`. All-day events (`e.start.date`)
render as a plain title with no time. List grammar per language as elsewhere.

## Edge cases
- **All-day events** — show without a time; still listed.
- **Recurring events** — `singleEvents:true` already expands them into the concrete
  instances inside the window (correct behaviour). Ties into
  [`feature-calendar-recurring-events.md`](feature-calendar-recurring-events.md) but needs
  nothing from it.
- **Large windows** ("this month") — `maxResults:50` caps it; if capped, append "(showing the
  first 50)" via a localized note rather than silently truncating.
- **Ambiguous phrasing the LLM can't resolve** → both range fields `null` → default today
  window, and the reply names the window so the owner can re-ask more specifically.
- **`nowIso` in ctx** — confirm the orchestrator passes the current instant into `ctx`
  (server.js builds `nowStr` for the LLM); reuse the same source rather than a bare `Date.now()`
  so tests are deterministic.

## Testing
1. `@secretary what's on my calendar tomorrow?` with 3 seeded events → all three, time-ordered,
   localized (en + pt).
2. Empty day → "Nothing on your calendar for …".
3. `@secretary what's my next meeting?` → single next upcoming event.
4. `@secretary do I have anything Friday afternoon?` → only 12:00–18:00 Friday events.
5. A recurring weekly event → the correct single instance appears in-window.
6. Force an `events.list` error → `listError()` reply, no crash.

## Deploy & done-when
- Deploy = git pull + restart on the droplet; get an explicit go-ahead for the production write.
- **Done when:** the four listed queries return correct, time-ordered, localized results with a
  clean empty-state and error-state, and no calendar write ever happens on a `list`. Then update
  `SKILL.md` (add the read task) and archive this plan to `Shipped Features/`.
