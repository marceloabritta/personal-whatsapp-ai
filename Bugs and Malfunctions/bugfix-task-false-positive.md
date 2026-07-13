# Bug report — `task_action` creates a phantom task from untagged chatter

| Field        | Value                                                             |
|--------------|-------------------------------------------------------------------|
| When         | 2026-07-11 23:18:39 (America/Sao_Paulo) — `2026-07-12T02:18:39Z`  |
| Chat         | 1:1 with Tony Lampada                                             |
| Trigger      | false positive (owner-reported)                                   |
| Source       | OWNER-REPORTED, confirmed in production logs                      |
| Skill        | `task_action` — the stateful "engaged" window                     |
| Severity     | Medium — silent write to the owner's real Google Tasks list       |
| Status       | needs-fix                                                          |

## Summary

While talking to Tony Lampada, the owner said — **to Tony, with no `@secretaria` tag** —
"amanha vou tentar implementar o tenente dentro do VsCode" ("tomorrow I'll try to implement
the lieutenant inside VsCode"). The Tasks skill's engaged window was open, so that message
was fed to the task planner, which read it as an order and **created a real task** in Google
Tasks.

Thirteen seconds later, the owner's *next* message to Tony ("e mandar ele ter workers") was
read as an **edit** to the phantom task and applied — with no confirmation — renaming it.

The system was never addressed. It wrote to the owner's list twice off pure conversation.

## What the user asked

Nothing. That is the bug. The last thing the owner actually *asked* the secretary was, eight
minutes earlier and correctly tagged: `@secretaria que tarefas eu tenho pra amanhã?` — a
read-only list query.

## Timeline (from `docker logs secretary`, UTC)

| Time (UTC)     | Message (owner unless noted)                                     | Planner output                        | Verdict |
|----------------|------------------------------------------------------------------|---------------------------------------|---------|
| `01:35:54`     | `@secretaria crie tarefa pra mim amanhã: brincar com o sistema do Tony` | `create "brincar com o sistema do Tony"` | ✅ correct — tagged order |
| `01:36:17`–`01:41:47` | 10 untagged chatter messages                              | `ops: []` ×10                         | ✅ correct — ignored |
| *(window expires ~01:45:59; chat continues untouched)*                                                          |
| `02:10:33`     | `@secretaria que tarefas eu tenho pra amanhã?`                    | `list_requested: true, ops: []`        | ✅ correct — **but arms a 10-min window** |
| `02:11:44`–`02:15:14` | 5 untagged chatter messages                               | `ops: []` ×5                          | ✅ correct — ignored |
| **`02:18:39`** | **`amanha vou tentar implementar o tenente dentro do VsCode`** *(untagged, addressed to Tony)* | **`create "implementar o tenente dentro do VsCode"`, due `2026-07-12`** | ❌ **FALSE POSITIVE — task written** |
| **`02:18:52`** | **`e mandar ele ter workers`** *(untagged, addressed to Tony)*     | **`edit` target_index 1 → title `"implementar o tenente dentro do VsCode e mandar ele ter workers"`** | ❌ **compounded — silently applied, no confirm** |
| `02:19:05`, `02:19:28` | untagged chatter                                          | `ops: []`                             | ✅ ignored |
| `02:19:40`     | untagged                                                          | `owner_done: true`                    | window closed |

The false positive landed **8m03s into a 10-minute window** — i.e. the window had not
leaked; it was legitimately open. See "Ruled out" below.

## The offending exchange (verbatim transcript as sent to the planner)

The planner received this as `transcript`, with the last line as `order`. Note that every
line here is the owner talking **to Tony about Tony's project** — none of it is addressed to
the secretary:

```
ME: shipei aqui que todas as msgs da secretária devem ser em itálico
ME: @secretaria que tarefas eu tenho pra amanhã?
ME: *[Secretaria IA do Marcelo]:*

_Aqui estão suas tarefas em aberto:_
_12/jul - brincar com o sistema do Tony_
OTHER: Fazendo um bugfix pro bridge…
ME: esse projeto seu n parece ser mto grande. é?
ME: pq vc chama esse liutenant de litenant, mas ele é um worker rssss
ME: tira o nome e o rosto, é só mais um robo uai rsss ainda n capturei a diferença para além da interface gráfica
OTHER: Grande em termos de volume de código?
ME: de complexidade da coisa toda
OTHER: Vc não entendeu ainda rs
OTHER: O tenente cria novas sessões do Claude code e conversa com elas.
OTHER: O tenente não faz as coisas. Só delega
ME: mas vc que delegou ai uai rsss
OTHER: E eu não tô falando do subagente. Eh outro terminal mesmo que ele pilota. Por isso tem o tmux.
OTHER: Ah sim. Mas ele monitora pra mim

Quem decide as coisas sou eu ainda né rs
OTHER: O fato do tenente não implementar diretamente permite que ele funcione num nível de abstração mais alto
ME: amanha vou tentar implementar o tenente dentro do VsCode      ← the `order`
```

Planner response (`TASK PLAN RAW`, `2026-07-12T02:18:39Z`):

```json
{"list_requested":false,"owner_done":false,"ops":[{"kind":"create","target_index":null,"candidate_indices":[],"ref_text":null,"title":"implementar o tenente dentro do VsCode","due_iso":"2026-07-12T00:00:00-03:00","assignee":null}]}
```

And 13 seconds later (`2026-07-12T02:18:52Z`), on `e mandar ele ter workers`:

```json
{"list_requested":false,"owner_done":false,"ops":[{"kind":"edit","target_index":1,"candidate_indices":[],"ref_text":"implementar o tenente dentro do VsCode e mandar ele ter workers","title":"implementar o tenente dentro do VsCode e mandar ele ter workers","due_iso":null,"assignee":null}]}
```

## Call chain (what actually executed)

Both bad messages took the **continuation** path, not the fresh/tagged path:

1. **`server.js:227-228`** — `matchedTag(text)` returns `null` → `isTagged = false`.
2. **`server.js:237-242`** — a session exists, `awaitFrom: "owner"`, message is `fromMe`
   → `isContinuation = true`.
3. **`server.js:245`** — passes the `!isTagged && !isContinuation` guard (it *is* a
   continuation), so the message is processed instead of dropped.
4. **`server.js:253`** — `const order = isTagged ? text.slice(tag.length).trim() : text.trim();`
   → `order` is the raw sentence. **The tag — or its absence — is now invisible downstream.**
5. **`server.js:272-292`** — builds `ctx`. It carries `session`, but **`isTagged` is never
   put on `ctx`**, and `ctx.tag` falls back to `TAGS[0]` so it is always truthy and useless
   as a signal.
6. The router is **bypassed** on a continuation — the session dispatches straight to the
   owning skill.
7. **`3. Tasks/skill.js:179-180`** — `run(ctx)` sees `session.stage === "engaged"` →
   `resumeEngaged(ctx, session)`.
8. **`3. Tasks/skill.js:574-592`** — `resumeEngaged` → `fetchOpen(ctx)` → **`planTaskOps(ctx, open)`**.
9. **`3. Tasks/skill.js:118-131`** — `planTaskOps` calls Claude with
   `system: buildPlanSystem(owner)` — **the exact same system prompt used for tagged orders.**
   Its signature is `planTaskOps(ctx, open)`; there is no parameter for "was this tagged?".
10. **`3. Tasks/prompt.js:97-127`** — `buildPlanSystem` / `buildPlanUser`. Neither receives
    nor mentions tagged-vs-untagged. The only guardrail is one closing line:
    > *"If the latest message is normal conversation with NO task action, return ops = []…
    > When unsure, PREFER the empty plan over inventing an op — a wrong task is worse than asking."*
11. **`3. Tasks/skill.js:208+`** — `dispatchPlan` → `handleCreates` → immediate
    `tasks.insert` (self-creates are **not** confirm-first), then `armEngaged` re-opens the
    window for another 600s.
12. On the follow-up, the `edit` hit a task in `recent`, which `dispatchPlan` applies
    **frictionlessly by design** (the "post-add amend" path) — no confirmation.

## Root cause

Two independent defects. The first is the real one.

### 1. The planner cannot distinguish an order from overheard conversation

`buildPlanSystem` is shared verbatim between the tagged path (`run`) and the untagged
continuation path (`resumeEngaged`). The model is therefore asked the **same question** —
"what task ops does this message call for?" — whether the owner typed
`@secretaria crie tarefa pra mim amanhã: X` or merely said `amanha vou tentar implementar X`
to a friend. The tag is stripped at `server.js:253` and never re-surfaced.

Those two sentences are structurally near-identical in Portuguese: same language, same
"amanhã", same verb phrase. Given a prompt whose default posture is "find the ops", reading
the second as a create is not an unreasonable inference — **the model was never told that
nobody was talking to it.** This is a missing input, not merely weak prompt wording: no
amount of rewriting `buildPlanSystem` can fix it while the tagged/untagged bit is absent
from the call.

### 2. A read-only query arms a write window

`dispatchPlan` (`skill.js:353-359`) counts `plan.list_requested` toward `didSomething`, so
**merely asking "what's on my list?" opens 10 minutes** in which any untagged sentence can
become a task. At `02:10:33` the owner wrote nothing to his list — he read it — and that
alone is what put the system in a state where `02:18:39` could fire.

## Ruled out

- **Session TTL leak** — no. `armEngaged` sets TTL 600s and a no-op inside the window
  deliberately does *not* re-arm (`skill.js:367-371` returns early). The earlier window
  (armed `01:35:59`) correctly expired: the `02:10:33` message had to be re-tagged to get
  through. The failing window was armed at `02:10:36` and the false positive landed at
  `02:18:39` — 8m03s in, legitimately open.
- **Router misroute** — no. The router is bypassed entirely on a continuation.
- **Bad transcript / missing context** — no. The transcript was complete and correct; the
  model simply had no reason to treat it as non-addressed.

## Proposed fix

The three parts below are one change. Part 1 supplies the missing fact; parts 2 and 3 are
what make use of it. **Part 1 without 2–3 changes nothing, and 2–3 are impossible without 1.**

### 1. Tell the model whether the message was directly tagged

The fact already exists at every layer — it just never reaches the prompt. Thread it through:

- **`server.js`** — put it on `ctx` (one line, next to `fromMe`):
  ```js
  isTagged,   // did THIS message address the secretary by tag, or is it a continuation?
  ```
- **`3. Tasks/skill.js`** — `planTaskOps(ctx, open, { addressed })`, where `addressed` is
  `ctx.isTagged`.
  > **CORRECTED AS SHIPPED (2026-07-12).** This bullet originally said `run()` passes
  > `addressed: true`, and that `resumeEngaged()` would be "correctly `true` if the owner
  > re-tags mid-window". **Both were wrong.**
  > (1) **A hardcoded literal is forbidden**, and the shipped self-test lints for it: a
  > `{ addressed: true }` at a call site would leave the new rails field with **zero readers**
  > and could silently restore this very bug while the live test stayed green. **All three**
  > call sites pass `ctx.isTagged` and nothing else.
  > (2) `resumeEngaged` **can never see a tagged message**: a tagged message is not a
  > continuation (`server.js` gate), so `ctx.session` is `null` and it takes the fresh `run`
  > path instead. "Re-tag mid-window" is handled there, not here.
  > (3) There are **three** call sites, not two — see "Files to touch" below.
- **`3. Tasks/prompt.js`** — `buildPlanSystem(OWNER_NAME, { addressed })` and surface it in
  `buildPlanUser` as an explicit header line, so it is impossible to miss:
  ```
  Was this message addressed to you? NO — ${OWNER_NAME} did not tag the secretary.
  You are OVERHEARING a conversation with ${contact}.
  ```

`resumeConfirm`'s yes/no classifier (`lib/confirm.js`) is out of scope — a bare "yes" inside
a pending confirmation is a different, already-narrow question.

### 2. Rewrite the planner's posture for the untagged case

Today's prompt has one soft closing sentence against inventing ops. Replace it with an
explicit, asymmetric bar. Proposed text for the `addressed: false` branch:

> **This message was NOT addressed to you.** ${OWNER_NAME} is talking to ${contact}; you are
> overhearing. Your DEFAULT is the empty plan. Only act if the message is an **imperative
> addressed to you** — a direct instruction a secretary standing in the room would
> unambiguously recognise as meant for her.
>
> **A statement of intent, plan, or future action is CONVERSATION, never a task.**
> "amanhã vou tentar X", "vou fazer X", "amanhã eu tento X de novo", "I'll do X tomorrow",
> "I need to X at some point", "we should X" — these are ${OWNER_NAME} *talking about* his
> life, not *delegating*. A human secretary does not silently add a to-do every time her
> boss muses aloud about tomorrow. Return `ops: []`.
>
> Likewise: opinions, jokes, questions to the other person, and anything about the OTHER
> person's work or projects are never task ops.
>
> If you can construct any reading in which ${OWNER_NAME} was talking to the other person
> rather than to you, return the empty plan. A phantom task is far worse than a missed one —
> he can always re-tag.

The `addressed: true` branch keeps today's wording (an explicitly tagged message *is* an
order; the current bar is right there).

### 3. Few-shot examples — right and wrong — drawn from this incident

Append to the untagged branch. These are the real strings from the log, which makes them
the highest-value examples available:

```
EXAMPLES — untagged messages inside an open window:

"amanha vou tentar implementar o tenente dentro do VsCode"
  → ops: []   ← statement of intent to a FRIEND. NOT a task. (This exact message
                caused a real phantom task on 2026-07-11 — do not repeat it.)
"e mandar ele ter workers"
  → ops: []   ← continues the sentence above, still to the friend. NOT an edit.
"amanhã eu tento de novo kkkk"
  → ops: []   ← musing, with laughter. NOT a task.
"esse projeto seu n parece ser mto grande. é?"
  → ops: []   ← a question to the other person.
"mas fiz o rails de uma secretária que custaria 10k por mes"
  → ops: []   ← telling a story about his own work.

"na verdade muda essa tarefa pra sexta"
  → edit      ← imperative, second person, aimed at the secretary. ACT.
"pode marcar a de comprar leite como feita"
  → complete  ← imperative aimed at the secretary. ACT.
"cancela a do dentista"
  → delete    ← imperative aimed at the secretary. ACT.
"adiciona também: comprar pão"
  → create    ← imperative aimed at the secretary. ACT.
"pronto, é isso"
  → owner_done: true
```

The discriminator these examples teach is **grammatical person and mood**: first-person
future ("vou…", "eu tento…", "I'll…") = conversation; second-person imperative ("muda…",
"cancela…", "adiciona…", "marca…") = order.

### Deferred (hardening, not required)

Deliberately **not** doing these now — the prompt fix subsumes both, and each costs
friction the owner did not ask for:

- **Don't arm the window on a bare `list_requested`** (root cause 2). Once an untagged
  statement of intent can't become a create, an open window after a read is harmless. Revisit
  only if the prompt fix proves leaky.
- **Confirm-first on untagged creates.** This is the belt-and-braces option: a phantom task
  becomes a phantom *question* instead. It is the hard floor if the probabilistic fix isn't
  enough — but it taxes the frictionless capture that makes the window worth having. Hold in
  reserve.

## Honest limitation

This fix is probabilistic. It makes the model's job dramatically easier — "is this an
imperative aimed at me, or is Marcelo talking to Tony?" is a far cleaner classification than
the effectively unconstrained one it faces today — but it does not make false positives
impossible. If a hard guarantee is wanted, only the deferred confirm-first option provides
it.

## Verification

1. **Regression, from the log.** Replay the exact failing pair against the new prompt with an
   open engaged window: `amanha vou tentar implementar o tenente dentro do VsCode` then
   `e mandar ele ter workers`. Both must yield `ops: []`. This is the acceptance test.
2. **No regression on the tagged path.** `@secretaria crie tarefa pra mim amanhã: brincar com
   o sistema do Tony` must still produce exactly one `create` with due `2026-07-12`.
3. **The window must still work.** After a tagged create, untagged `na verdade muda pra
   sexta` must still amend it — the whole point of the window is that legitimate follow-ups
   need no re-tag. Over-correcting into "ignore everything untagged" is the failure mode to
   watch for here.
4. **Re-tag mid-window.** With the window open, a tagged `@secretaria adiciona comprar pão`
   must be treated as addressed (`addressed: true`) and create.
5. Live-check in the real chat, then confirm in `docker logs secretary` that the `TASK PLAN
   RAW` lines match.

## Files to touch

- `secretary/1. Orchestrator/server.js` — add `isTagged` to `ctx` (~1 line).
- `secretary/2. Skills/3. Tasks/skill.js` — thread `addressed` into `planTaskOps`; pass from
  `run` and `resumeEngaged` (~5 lines).
  > **CORRECTED AS SHIPPED (2026-07-12).** There are **three** call sites, not two. This list
  > missed `resumeConfirm`'s **"unrelated" re-plan** — the branch that catches a new task
  > mentioned while a confirmation is pending. Missing it would have left the phantom-create
  > path wide open on exactly the flow where the owner is most likely to be mid-conversation.
- `secretary/2. Skills/3. Tasks/prompt.js` — `buildPlanSystem(OWNER_NAME, { addressed })`:
  branch the posture, add the examples (the bulk of the change).
- `secretary/2. Skills/3. Tasks/SKILL.md` — document that untagged follow-ups only act on
  imperatives, and that statements of intent are ignored by design.

---

## Outcome — what was actually done (2026-07-12)

Built as planned above, with the three corrections marked in place. **The acceptance test is
green — the live half has now been run: 48/48, three runs out of three** (16 cases × 3 runs),
plus the 11/11 offline wiring lint. **Not yet deployed to the droplet** — that remains the
owner's call (see "Still open" below).

**What shipped.**

1. **The rails field.** `secretary/1. Orchestrator/server.js` — one **additive** field on the
   `ctx` literal: `isTagged`. The bit was already computed there and then thrown away before any
   skill could see it; `ctx.tag` is useless as a signal because it falls back to `TAGS[0]` and is
   always truthy. Nothing else in the file changed, and no existing caller needed an edit.
2. **The wiring — three call sites, no literals.** `planTaskOps(ctx, open, { addressed })` is now
   **required** (it throws a `TypeError` if omitted), and **all three** call sites — `run`,
   `resumeConfirm`'s "unrelated" re-plan, and `resumeEngaged` — pass **`ctx.isTagged`**. A
   hardcoded `addressed: true` is **forbidden and linted against**: it would be behaviourally
   correct today, pass the live test (which never sees the wiring), leave the rails field with
   zero readers, and silently restore this bug the day the continuation gate changes.
3. **The two postures.** The planner's core instructions are unchanged **byte-for-byte** for a
   tagged order (verified: the tagged prompt is the identical 3391-character string it was before).
   The untagged case **appends** a posture that **asks** whether the message was aimed at the
   secretary. It deliberately does **not** assert "you were not addressed" — the wording proposed
   at §2 of this document above was **vetoed during planning**, because *every genuine in-window
   follow-up is untagged too*, so an asserting prompt would have silently swallowed "na verdade
   muda essa pra sexta" and quietly gutted the shipped window. The bar splits by op kind: the
   **referent** governs complete/edit/delete/`list_requested` (it can only rule OUT, never
   license — which is what keeps "e mandar ele ter workers" dead once the phantom is on the list),
   and the **form of address** governs `create` (a create has *no* referent by construction, so a
   referent rule applied to creates would forbid **every** untagged create, silently). The same bar
   covers `list_requested` — reading the list aloud would print the owner's to-dos into Tony's
   chat. **`owner_done` stays exempt**: it only closes the window; it writes nothing and says
   nothing. Overheard talk now produces **silence** — no ops, no reply, no re-arm.
4. **The test.** `scripts/tasks-addressed-selftest.mjs`, two halves. The **live** half replays the
   real logged transcript from this report and proves **both directions** — the overheard chatter
   produces an empty plan, *and* genuine untagged follow-ups still produce the right ops. The
   **offline** half lints the wiring (the three call sites, the `ctx.isTagged` value, the absent
   literal, the required parameter, the rails field, and the load-bearing clauses of the posture).

**Deliberately NOT fixed — defect (2), the read-only query that arms a write window.** A
"what tasks do I have?" still opens a 10-minute *write* window. It gets its own card: un-arming it
has a real cost ("what's on my list? … ok, add milk", untagged, works today and would stop), and
with (1) fixed an open window is no longer a loaded gun.

**Residual risk, stated plainly.** This is a **prompt**, not a guarantee. It reduces the false
positive rate; it cannot make it zero. The escalation, **only** if one recurs, is confirm-first on
untagged creates — rejected for now because the secretary replies *in the chat the message came
from*, so a confirmation would interrupt the owner's conversation with a third party to ask about
his private to-do list. A louder failure is not a safer one.

**Still open.**
- **Deploying to the droplet.** The acceptance run
  (`ANTHROPIC_API_KEY=… RUNS=3 node scripts/tasks-addressed-selftest.mjs`) was the **precondition
  for deploying** this fix — it is the only thing that proves the posture actually works on the
  model, in both directions — and it has now been met: **48/48 live, 3 runs out of 3**, both
  directions proved (the two logged sentences that wrote the phantom task produce the empty plan;
  the genuine untagged follow-ups — edit, delete, complete, create, list — still act). The fix is
  committed but **not yet on the droplet**; the deploy is the owner's call.
- **The phantom task itself** is still on the owner's real Google Tasks list. Operational, not
  code — the owner deletes it.
