# Calendar — Deferred Micro-Optimizations (Notes, not features)

Carried over from the retired `calendar-actions.md` so they aren't lost. Neither is a user-facing
feature; both are low-priority efficiency/robustness ideas on the shipped `calendar_action` skill
([`skill.js`](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js) /
[`prompt.js`](../secretary/2.%20Skills/1.%20Calendar%20Actions/prompt.js)). Promote to a full
plan only if the cost/benefit ever justifies it.

## 1. Cheap pre-filter for the "is this the answer?" session check
While a session is open, the orchestrator runs a per-message LLM classification (e.g.
`classifyConfirmation`, `reviewCreate`, `reviewEdit`, `inspectMissing`) on **every** message from
the awaited party — including obvious chatter. If per-message LLM cost ever matters, add a cheap
pre-filter that only invokes the model when the message plausibly contains the awaited info
(e.g. short affirmative/negative tokens, an email regex, a time-like token, or a reply to the
secretary's own bubble). Skip the LLM call otherwise and treat as chatter.
- **Cost today:** these calls only run while a session is open (≤10-min TTL), so volume is
  already bounded — hence low priority.
- **Risk:** a too-aggressive pre-filter drops a real answer and the flow silently stalls until
  TTL. The filter must be strictly *additive-safe* — only skip messages that clearly can't be
  the answer.

## 2. Pure "is any present field likely wrong?" verifier
A verifier pass that flags an already-extracted field as probably-misparsed (wrong time, wrong
attendee) *before* the confirm bubble. Considered and deferred because the **confirm step already
gives the human that check for free** — the owner sees the draft and can correct it by plain
reply. Only worth adding if we ever want to *proactively* highlight a suspicious field
("did you mean 3 PM, not 3 AM?") rather than rely on the human catching it. Would slot in after
`interpret`/`inspectMissing`, before `openCreateConfirm`.

---

**Status:** both are optional. The shipped skill already behaves correctly without either; these
are here only so the ideas survive the cleanup of the original plan.
