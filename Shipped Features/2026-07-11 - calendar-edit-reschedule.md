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

## Hardening — edit shares delete's matcher (2026-07-11)

`handleEdit` originally resolved its target **link-only** (`resolveEventId` → `getEvent` →
status recheck), so replying to a *summary/confirm bubble* (which carries no embedded
calendar link) failed. Generalized `matchDeletionTargets` → **`matchEventTargets`** (rename
only — signature, scoring `+100`/`+40`/`+30`, confident `≥70`, dedup, return shape all
unchanged) and pointed **both** delete and edit at it. Edit now resolves via a decoded link
**or** start-time + attendee-email match — exactly like delete — and works from the summary
bubble or a tagless "who + when" request. Removed the now-redundant `getEvent`/status recheck
in edit (the matcher returns full confirmed events). Delete's behavior is byte-for-byte
unchanged (one call-site rename). `resumeEditClarify` / `resumeEditConfirm` untouched. Net:
one rename + ~15 swapped lines in `handleEdit`, zero new prompts/strings.

**Follow-up fix — the extraction contract (2026-07-11).** The matcher change above wasn't
enough on its own: the first live test (reply to a summary bubble with "muda pra 14:30")
still returned "no match". Cause — the `interpret` prompt told the model to *leave
`start_iso` null for edits* ("not used for edits"), and when it did emit one it was the
**new** requested time (14:30), so the calendar search looked at the wrong slot and found
nothing. Fixed the edit branch of `buildSystem` (`prompt.js`) to identify the **existing**
event like delete does: `start_iso` = the event's **CURRENT** start (read from the
replied-to invite/summary or the conversation — explicitly *not* the new time), plus
`participants`/emails. The actual change is still extracted separately by `interpretEdit`.
No new reply strings; the summary/confirm bubble stays exactly as it was — resolution now
searches the calendar from the full context (link is just one more signal), rather than
trusting the link alone.

**Verified live (2026-07-11).** Both hardening commits (`980712e` matcher share, `84bc59f`
extraction contract) are deployed and confirmed working in production: replying to a
summary/confirm bubble with "muda pra 14:30" now resolves the event and opens the
edit-confirm, where it previously returned "no match". This closes out the edit/reschedule
feature — no further edit-robustness work planned.
