# Calendar Actions — Edit / reschedule via reply (SHIPPED 2026-07-11)

Shipped record for **Phase B** of the calendar plan: editing an existing event by
replying to its invite. Extracted from `New Features Plans/calendar-actions.md` when it
shipped and was **confirmed working in production on 2026-07-11**.

Behavior reference lives in [`brain/2. Skills/1. Calendar Actions/SKILL.md`](../brain/2.%20Skills/1.%20Calendar%20Actions/SKILL.md);
this doc is the delivery record (what shipped, how it was built, why).

## Scope

Reply to an event's calendar link with a change and `@brain`:
`move to 4pm` · `make it 30 min` · `add carlos@x.com` · `remove ana@x.com` ·
`rename to Kickoff`. The event to change is resolved straight from the replied-to invite
link. Ambiguous requests ("move it earlier") are clarified before anything is shown.

## As built — confirm-first, and it stays open

Edit reuses **create's confirm/modify machinery**. It is **confirm-first**: the change is
folded into a **draft** of the event's target state, shown for confirmation, and written
to Google only on the owner's `yes`. While the confirm session is open the owner keeps
refining the *same* event **tagless** ("actually 4:30", "also add bruno@x.com") — each
change re-shows the draft; nothing is written until confirmed.

> A first cut (earlier the same day) applied an unambiguous edit immediately and opened no
> session, so a *second* change had to re-tag `@brain`. First-test feedback drove the
> rework to the confirm-first, stays-open design above.

## Flow

- **`interpret`** gains `action:"edit"` and only **classifies** it (enum is now
  `create | delete | edit | other`). The change itself is pulled by a **focused pass**
  `interpretEdit` (`buildEditSystem` / `EDIT_SCHEMA`) reading the request against the
  event's real current state (`eventForLLM`) → a structured patch (`new_start_iso`,
  `new_duration_min`, `new_title`, `new_summary`, `add_emails[]`, `remove_emails[]`,
  `clarify`). Mirrors the create resolver rather than stuffing a `changes` object into the
  broad extraction.
- **`handleEdit`:** `resolveEventId(quoted.calendarLink)` → `getEvent` (must be
  `confirmed`) → `interpretEdit`. Concrete change → `applyPatchToDraft(editDraftFromEvent
  (ev), patch)` → **`openEditConfirm`** (session `stage:"await_confirmation"`,
  `awaitFrom:"owner"`, holds `{eventId, draft}`). Ambiguous (`clarify`, no change) →
  `await_clarification` session (holds only `eventId`).
- **`resumeEditClarify`** (ambiguous first request): re-`getEvent` + `interpretEdit` on the
  answer; resolves → build draft → `openEditConfirm`; else silent.
- **`resumeEditConfirm`** (the confirm loop): one `reviewEdit` call
  (`buildEditReviewSystem` / `EDIT_REVIEW_SCHEMA`) → `confirm | modify | cancel | unrelated`
  judged against the proposed draft (`draftAsEventJson`). `confirm` → `applyEditDraft`
  (`events.patch`, `sendUpdates:"all"`) + clear; `modify` → fold onto draft + re-show,
  **keep open**; `cancel` → clear + "leave it as it was"; `unrelated`/null → silent.

## Reuses / adds

Reuses `resolveEventId`, `getEvent`, and the session/review pattern (mirrors
`reviewCreate`). Adds `patchEvent`, `EDIT_SCHEMA` + `EDIT_REVIEW_SCHEMA`,
`buildEditSystem/User` + `buildEditReviewSystem/User`, `editDraftFromEvent` /
`applyPatchToDraft` / `draftAsEventJson`, and the `editConfirm` / `editCancelled` /
`editDone` / `editClarify` / `editNeedSignal` / `editNoMatch` reply strings (en + pt).
No router change — it already routes calendar-link replies and reads the manifest.

## Done — verified in production

Reschedule / relength / add-remove attendee / rename all work; changes chain tagless
until confirmed; ambiguous requests clarify; nothing is written until the owner confirms.
Shipped and confirmed working by Marcelo on **2026-07-11** (commits `8036a4b` initial,
`55891fe` confirm-first rework).
