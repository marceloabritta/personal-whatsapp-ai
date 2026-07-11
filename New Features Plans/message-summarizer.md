# Message Summarizer / "Catch Me Up" — Implementation Plan

## Goal

Summarize a long message, a forwarded thread, or a busy chat on demand.

- Reply to a long message: `@brain summarize this`
- Catch up on a chat: `@brain me atualiza o que rolou aqui`
- Extract commitments: `@brain what did I agree to in this chat?`

Returns a tight summary with any **action items** and **dates** pulled out, in the
chat's language.

## Why it fits the architecture

- The orchestrator already builds `transcript` (via `buildTranscript`) and passes
  `quoted` (the replied-to message) in `ctx`.
- It's a single Claude call + `send()` — **no new infrastructure and no new auth.**
  This is the closest cousin to the existing skills and the cheapest to ship.

## New skill — `2. Skills/4. Summarizer/`

Standard skill contract (`manifest` + `run`), auto-discovered at boot.

- `manifest`:
  ```
  { id: "summarize",
    description: "summarize a replied-to message/thread or the recent conversation, extracting key points, action items and dates" }
  ```
- `run(ctx)`:
  1. Choose the **source text**:
     - If `ctx.quoted?.text` exists → summarize the quoted message/thread.
     - Else → summarize the recent `transcript`.
  2. One Claude call (`prompt.js`) → structured summary.
  3. `send(number, <formatted summary>)`.

### Source selection detail

- Replying to a single long message → that message is the source (`quoted.text`).
- "catch me up / o que rolou" with no reply → use `transcript` (the recent window
  the orchestrator already assembles).
- Optionally support a range hint ("summarize today") by instructing the model to
  focus on the most recent portion — no extra fetch needed beyond what `transcript`
  already contains.

### Prompt (`2. Skills/4. Summarizer/prompt.js`)

- System: "Summarize the provided conversation/message for `OWNER`. Be concise.
  Output JSON:
  `{ summary: string, bullets: string[], action_items: string[], dates: string[] }`.
  Write the human-facing content in the SAME LANGUAGE as the source."
- `skill.js` formats the JSON into a clean WhatsApp message (headline summary +
  bullets, then an "Action items" / "Dates" block only when non-empty).

## Output shape (example)

```
[AI Brain]:

Summary: João confirmed the contract but wants the SP meeting moved.

- Contract approved on his side
- Wants to reschedule the São Paulo meeting
- Asked for the updated address

Action items:
- Send updated address
- Propose 2 new times for the SP meeting

Dates:
- Original meeting: Fri 15:00
```

## Multi-lingual

- Two layers, both consistent with the localization convention in `../ARCHITECTURE.md`:
  1. The model writes the summary body in the **source language** (instructed in the
     prompt) — this naturally matches the chat.
  2. Fixed labels ("Summary", "Action items", "Dates", error strings) come from the
     i18n catalog keyed by `ctx.lang` (`summarize.header`, `summarize.actionItems`,
     `summarize.dates`, `summarize.failed`).

## Composability

- Pairs with **Task capture** (idea 5): after a summary with action items, offer to
  save them — open a confirmation **session** ("save these 3 as tasks?") reusing the
  existing session/continuation flow. Keep this as a phase-2 enhancement.

## Files touched

- **New:** `2. Skills/4. Summarizer/skill.js`, `2. Skills/4. Summarizer/prompt.js`
- **Edit:** i18n catalog (new summarizer label keys)

## Build order

1. Skill + prompt with the quoted-message path.
2. Add the transcript ("catch me up") path.
3. Formatting + i18n labels.
4. (Phase 2) Hand-off to Task capture for action items.

## Notes

- No new API keys or infra — lowest-risk feature to build first.
- Keep `max_tokens` modest; summaries should be short. Truncate very large sources
  before the call if needed.
