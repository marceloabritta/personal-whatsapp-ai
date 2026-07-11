# Calendar — Conflict / Availability Check on Create (Implementation Plan)

Split out of the now-retired `calendar-actions.md` backlog (2026-07-11). The `calendar_action`
skill (create / edit / delete, all stateful and confirm-first, structured outputs, multilingual)
is **shipped and live**; this plan adds one bounded improvement on top of it.

Skill source: [`skill.js`](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js) (logic) +
[`prompt.js`](../secretary/2.%20Skills/1.%20Calendar%20Actions/prompt.js) (prompts, JSON Schemas,
localized reply strings). Behaviour reference:
[`SKILL.md`](../secretary/2.%20Skills/1.%20Calendar%20Actions/SKILL.md).

## Goal

Before the owner confirms a **new** event, check the target slot against the calendar and
**warn on an overlap** — so a double-booking is caught at the confirm step, where the owner
is already reviewing the draft, instead of surfacing only after the invite emails go out.

Non-goal (MVP): hard-blocking. A conflict is a **warning**, never a veto — the owner can still
reply `yes`. People deliberately overlap events; the skill's job is to surface, not to police.

## Why here, why now

- Create is already confirm-first (`openCreateConfirm` shows a draft and waits for `yes` —
  [skill.js:451](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js#L451)). The confirm
  message is the natural, zero-extra-round-trip place to add a warning line.
- The calendar-read primitive already exists: `findConfirmedDuplicates` and `matchEventTargets`
  both do windowed `events.list` calls
  ([skill.js:172](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js#L172),
  [skill.js:207](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js#L207)) — the new
  overlap query is the same shape over a wider window.

## Behaviour (what the owner sees)

Unchanged flow, one added line in the confirm bubble when the slot is busy:

> Confirm this event:
> - Q3 budget review
> - ana@example.com
> - Jul 12, 2026, 3:00 PM (45 min)
>
> ⚠️ Heads up — this overlaps **"1:1 with Bruno" (2:30–3:30 PM)** already on your calendar.
>
> Reply "yes" to confirm and I'll send the invites, or tell me what to change and I'll adjust.

- No overlap → the confirm bubble is exactly as today (no warning line).
- Multiple overlaps → list them (localized list grammar, `joinListEn` / `joinListPt`).
- The warning is informational; `yes` still creates, a change ("move to 4pm") still re-drafts
  and re-checks the new slot.

### Stretch (Phase 2, optional): offer the next free slot
When there's an overlap, append a suggestion computed from a same-day free/busy scan:
> The next free 45-min slot today is **4:15 PM**. Want that instead? Just say "yes, 4:15".
The owner's plain reply is already handled by the existing modify path (`resumeCreate` →
`applyDraftUpdate`), so this is purely a message-composition + slot-search add-on.

## Implementation

### 1. New helper: `findOverlaps` (skill.js)
Mirror `findConfirmedDuplicates` but over the event's real span and return *distinct* meetings,
excluding the draft's own would-be duplicate.

```js
// Confirmed events whose [start,end) intersects [startIso, startIso+durationMin).
// Excludes zero-overlap boundary touches and (via title+start) the event being created.
async function findOverlaps(env, { title, startIso, durationMin }) {
  if (!startIso) return [];
  const cal = calendarClient(env);
  const start = new Date(startIso).getTime();
  const end = start + (Number(durationMin) > 0 ? durationMin : 45) * 60000;
  const r = await cal.events.list({
    calendarId: calId(env),
    timeMin: new Date(start).toISOString(),
    timeMax: new Date(end).toISOString(),
    singleEvents: true,
    showDeleted: false,
    maxResults: 50,
  });
  return (r.data.items || []).filter((e) => {
    if (e.status !== "confirmed" || !e.start?.dateTime || !e.end?.dateTime) return false; // skip all-day / cancelled
    const s = new Date(e.start.dateTime).getTime();
    const en = new Date(e.end.dateTime).getTime();
    if (en <= start || s >= end) return false;                 // touching edges ≠ overlap
    if (e.summary === title && s === start) return false;       // the draft's own dupe (idempotency)
    return true;
  });
}
```
Note on the window: `events.list` with `timeMin/timeMax` returns events that *intersect* the
window, but Google is inclusive at the boundary — hence the explicit `en <= start || s >= end`
recheck to drop back-to-back meetings that merely touch.

### 2. Wire it into the confirm step (`openCreateConfirm`, skill.js:451)
Call `findOverlaps` just before rendering, pass the result to the reply renderer, wrap in
try/catch so a calendar hiccup never blocks the confirm (degrade to "no warning"):
```js
let conflicts = [];
try {
  conflicts = await findOverlaps(env, { title: draft.title, startIso: draft.start_iso, durationMin: draft.duration_min });
} catch (e) { console.error("Calendar overlap check error:", e?.response?.data || e?.message || e); }
// pass conflicts -> reply(ctx.lang).createConfirm({ ..., conflicts })
```
`conflicts` carries `{ summary, start.dateTime, end.dateTime }` per event; the renderer
localizes the times with `localizeDate`.

### 3. Reply string (prompt.js `REPLY.en` / `REPLY.pt`, ~L392)
Extend `createConfirm(...)` to accept `conflicts` and prepend the ⚠️ line when non-empty,
per language (times via `localizeDate(lang, …)`, list via `joinListEn`/`joinListPt`). Keep the
existing body untouched when `conflicts` is empty so today's output is byte-identical.

### 4. (Stretch) next-free-slot search
Add `firstFreeSlot(env, { afterIso, durationMin, windowHours = 8 })` — a `freebusy.query`
(or a single `events.list` for the day) that walks the busy blocks and returns the first gap
≥ `durationMin`. Compose into the same warning line. Ship only after the warn-only MVP is live.

## Edge cases
- **All-day events** (`e.start.date`, no `dateTime`) — skipped (don't have a comparable instant).
- **Owner is an attendee of the overlapping event vs. owner of it** — both count; we're checking
  *the owner's* calendar, so any confirmed event on it is a conflict signal.
- **Timezone** — all comparisons are epoch-ms, so `CAL_TZ` / offsets don't distort them.
- **Edit/reschedule** — the same overlap check is desirable in `openEditConfirm`
  ([skill.js:870](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js#L870)); reuse
  `findOverlaps` there with `excludeId = draft's own eventId` (add an `excludeId` param mirroring
  `findConfirmedDuplicates`). Land create first, then apply the same call to edit.

## Testing
1. Seed a real event at 3:00–3:30 PM. `@secretary schedule ... 3:15pm 45min` → confirm bubble
   shows the ⚠️ overlap line; `yes` still creates.
2. Slot with nothing on it → confirm bubble is unchanged (no warning).
3. Back-to-back (existing 2:00–3:00, new 3:00–3:45) → **no** warning (edge touch, not overlap).
4. Two overlapping events → both listed, localized grammar (en + pt).
5. Force an `events.list` error (bad creds in a scratch run) → confirm still shows, sans warning.

## Deploy & done-when
- Deploy = git pull + restart on the droplet (see PROJECT deploy workflow); production write —
  get an explicit go-ahead before shipping.
- **Done when:** creating into a busy slot shows the overlap warning at confirm (en + pt),
  a free slot shows the unchanged bubble, `yes` still creates in both, and no calendar error
  can block the confirm. Then update `SKILL.md` and archive this plan to `Shipped Features/`.
