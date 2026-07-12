# Bug report — the secretary is blind to durable history in every 1:1 chat (WhatsApp LID addressing)

| Field        | Value                                                                      |
|--------------|----------------------------------------------------------------------------|
| When         | 2026-07-12 12:19:23 (America/Sao_Paulo) — owner-reported                   |
| Chat         | 1:1 with Savinho Carissimo (`553171746333@s.whatsapp.net`)                 |
| Trigger      | `reported` (owner) → filed by the `feedback` skill                          |
| Source       | OWNER-REPORTED, root cause confirmed against Evolution's Postgres           |
| Skill        | Surfaced in `calendar_action`; the defect is in the **orchestrator**        |
| Severity     | **High** — affects every 1:1 conversation, every skill, silently            |
| Status       | **FIXED — verified against production. NOT yet deployed.**                  |
| Report       | `_reports/2026-07-12T12-19-23-reported-calendar-action.md`                  |

## Summary

The owner asked the secretary to schedule a meeting with Savio (`@secretaria marque uma
reuniao com o savio amanha`). The secretary asked for the date and time. He supplied context,
Savio supplied his e-mail, and on `@secretaria agendar` the secretary **asked for the date and
time again** — the date (`amanha`) was already in the chat. Told *"leia as msg, está tudo ai"*
("read the messages, it's all there"), it still could not.

It could not because **it genuinely could not see them.** `fetchHistory` reads Evolution's
Postgres by the chat's phone JID (`…@s.whatsapp.net`). But WhatsApp's **LID addressing** means
inbound messages in a 1:1 chat are persisted under a *different* JID (`…@lid`). The phone JID
therefore returns **only the secretary's own outbound messages** — it is talking to itself.

The only thing carrying real conversation is the **in-memory buffer**, which is wiped on every
container restart. The container had restarted at 14:41; the `amanha` message was sent before
that. It was gone.

This is not a `calendar_action` bug, not a prompt bug, and **not a deployment bug**. It is one
wrong lookup key in the orchestrator, and it has been silently degrading every skill in every
1:1 chat since LID addressing rolled out.

## What the user asked

> `@secretaria marque uma reuniao com o savio amanha`

and, later, after the secretary stalled:

> `@secretaria agendar`
> `leia as msg, está tudo ai`

His note, filed via the `feedback` skill: **`anote erro.`**

## Evidence — Evolution's Postgres

The chat with Savio, as the secretary sees it today (`where: {key: {remoteJid:
"553171746333@s.whatsapp.net"}}`) — **6 messages, all of them `fromMe`**, i.e. nothing but the
secretary's own sends:

```
14:00:25  ME  "Recebi o áudio, transcrevendo... ~1 min."
14:00:29  ME  "Transcrição do áudio: Fala aí, Savinho…"
14:12:35  ME  "_Antes de agendar, ainda preciso do seguinte: a data e o horário…_"
14:13:02  ME  "_Savio, estou sem o seu e-mail…_"
15:14:57  ME  "_Antes de agendar, ainda preciso do seguinte: a data e o horário…_"   ← the repeat
15:19:26  ME  "Anotado — registrei como um erro para investigar…"
```

The **same conversation**, stored under `250104602693736@lid` — the real thing, both sides,
34 messages, including every message the secretary claimed not to find:

```
15:19:56  THEM  domingos.carissimo@gmail.com
15:20:12  ME    ja tinha as infos aqui
15:20:34  THEM  Seu IA tem que responder então
```

This is systemic, not a Savio quirk. Across all **13,894** stored messages:

| JID domain       | `addressingMode` | `fromMe` | count |
|------------------|------------------|----------|-------|
| `g.us` (groups)  | lid              | false    | 6,632 |
| `lid`            | lid              | true     | 1,742 |
| `g.us` (groups)  | —                | false    | 1,713 |
| `lid`            | lid              | false    | 1,461 |
| `lid`            | —                | true/false | 1,258 |
| `s.whatsapp.net` | —                | **true** | **155** |
| `s.whatsapp.net` | —                | **false**| **0** |

**Not one inbound message has ever been stored under `…@s.whatsapp.net`.** Group chats
(`@g.us`) are *unaffected* — their inbound messages are stored under the same JID the webhook
delivers, so the lookup matches. **Only 1:1 chats are broken.**

### The link between the two JIDs

Evolution stores it on the message key itself:

```json
{ "id": "3AD04271D533765DEE1B",
  "fromMe": false,
  "remoteJid": "250104602693736@lid",
  "remoteJidAlt": "553171746333@s.whatsapp.net",
  "addressingMode": "lid" }
```

`remoteJidAlt` **is** the phone JID. No mapping table, no contact lookup, no extra round-trip is
needed — and `findMessages` accepts it as a filter (verified live: it returns 30 messages where
`remoteJid` returns 6).

## Call chain (what actually executed)

1. **`server.js:225`** — `const { fromMe, remoteJid, id } = data.key;` — the webhook delivers
   the **phone JID** (`553171746333@s.whatsapp.net`). This is correct for sending, and it is
   what everything downstream is keyed on.
2. **`server.js:230`** — `remember(remoteJid, …)` — the message goes into the in-memory buffer
   under the phone JID. **This buffer is the only place the conversation actually lives.**
3. **`server.js:284`** — `combine(remoteJid, await evolution.fetchHistory(remoteJid))`.
4. **`lib/evolution.js:39-45`** — `fetchHistory` posts
   `{ where: { key: { remoteJid } } }` to `/chat/findMessages`. **← the defect.** Inbound
   messages are stored under the LID, so this matches only the secretary's own outbound sends.
5. **`lib/whatsapp.js:84-90`** — `combine` merges history + buffer, dedupes, returns the last
   30. With history contributing nothing real, the result **is** the buffer.
6. **`lib/whatsapp.js:72-80`** — the buffer is a module-level `Map`, capped at 50 and **lost on
   every container restart**.

The container restarted at `14:41:25`. The `amanha` order predates it. By `15:14`, the
transcript handed to `calendar_action` was 8 lines and did not contain the order:

```
ME: Transcrição do áudio: Fala aí, Savinho...
ME: _Antes de agendar, ainda preciso do seguinte: a data e o horário e o e-mail de Savio._
ME: _Savio, estou sem o seu e-mail._
OTHER: Top
OTHER: domingos.carissimo@gmail.com
ME: passou muito tempo, ai ela desiste.
ME: @secretaria agendar          ← the `order`
```

The resolver ([`skill.js:619-648`](../secretary/2.%20Skills/1.%20Calendar%20Actions/skill.js)) is
handed exactly this and duly returned `{"start_iso":null,"participants":null}`. **The skill
behaved correctly on the input it was given.** The input was wrong.

## Root cause

**`fetchHistory` queries by the wrong key for LID-addressed chats.**

The comment at [`lib/whatsapp.js:70`](../secretary/1.%20Orchestrator/lib/whatsapp.js#L70) states
the design intent:

> *"In-memory buffer (short-term context). Cleared when the container is recreated; **the real
> history lives in Evolution's Postgres (via fetchHistory)**."*

That second clause is **false in production**. The durable read returns nothing usable, so the
architecture silently collapsed onto its own cache: the secretary's entire memory of any 1:1
chat is a volatile 50-message `Map` that dies with the process. Every restart is a lobotomy,
and no amount of prompt work in any skill can recover information that was never in the
transcript.

The reason this went unnoticed is that the buffer is *good enough* within a single uptime. The
failure only surfaces across a restart — or beyond 50 messages — which reads as flakiness
rather than as a bug.

## Ruled out

- **A deploy-pipeline / zero-downtime problem** — no. This is the tempting reading (restarts
  *do* wipe context), but it is the symptom. A zero-downtime pipeline is weeks of work, would
  *still* lose an in-process `Map` on any container replacement, and would leave the secretary
  permanently blind to anything older than its current uptime. Fix the lookup key and restarts
  stop mattering — exactly as the code always intended.
- **Evolution not persisting inbound messages** — no. `DATABASE_SAVE_DATA_NEW_MESSAGE=true`,
  and 13,894 messages are stored. They are simply under a JID we never ask for.
- **The 30-message window being too small** — no. The window never filled: `combine` had only
  the buffer to work with.
- **A `calendar_action` extraction or prompt bug** — no. Given a transcript without `amanha`,
  `start_iso: null` is the *correct* output. (See "Honest limitation" — there is a real, much
  smaller UX bug here too.)
- **The router** — no. It routed `calendar_action` correctly, and later routed the complaint to
  `feedback` correctly.

## Proposed fix

### 1. Query both JIDs in `fetchHistory` and merge

`lib/evolution.js:39-60`. Fire both filters, concatenate, and let the existing dedupe in
`combine` do the rest. Purely additive — chats that never used LID keep working unchanged,
because the `remoteJidAlt` query simply returns nothing for them.

```js
async function fetchHistory(remoteJid) {
  // WhatsApp LID addressing: in a 1:1 chat, inbound messages are persisted under the
  // contact's `…@lid` JID, while the JID the webhook hands us (and that we send to) is
  // the phone `…@s.whatsapp.net`. Evolution records the phone JID on the LID rows as
  // `key.remoteJidAlt`, so we ask both ways and merge. Querying only `remoteJid` returns
  // nothing but our OWN outbound messages. Group chats (@g.us) match on the first query.
  const pages = await Promise.all([
    findMessages({ key: { remoteJid } }),
    findMessages({ key: { remoteJidAlt: remoteJid } }),
  ]);
  return pages.flat().map((r) => ({
    t: Number(r.messageTimestamp) || 0,
    fromMe: r.key?.fromMe,
    text: extractText(r.message).trim(),
    pushName: r.pushName,
  }));
}
```

…with the existing body extracted into a `findMessages(where)` helper that returns
`data?.messages?.records || data?.records || []` and `[]` on error, so one failing query cannot
take down the other.

`combine` already dedupes on `` `${m.t}|${m.text}` `` and sorts by `t`, so overlapping rows are
harmless and ordering is preserved.

**Verified live against production before writing this plan:** the merged query returns **30**
messages where the current one returns **6**, and the recovered window contains, verbatim:

```
ME: @secretaria marque uma reuniao com o savio amanha
```

### 2. Fix the false comment

[`lib/whatsapp.js:70`](../secretary/1.%20Orchestrator/lib/whatsapp.js#L70) asserts the durable
history works. It is what made this invisible. Once the fix lands the claim becomes true —
state *why* it's true (both JIDs are queried) so the next person doesn't undo it.

### 3. Persist the buffer to Redis — *optional, defence in depth*

Redis is already connected (`sessions: Redis connected` in the boot logs) and already holds
sessions. Moving `buffers` from a module-level `Map` to Redis makes restarts free regardless of
how the JID work behaves, and removes the last piece of load-bearing volatile state.

Recommended, but **not required** — fix 1 makes the durable read work, which is the point.
Sequence it second so it never masks a regression in fix 1.

## Outcome — what was actually done (2026-07-12)

Fix 1 and fix 2 are implemented on `kanban-live-test`. Fix 3 (Redis) was **not** done — fix 1
makes the durable read work, which is the point, and leaving the buffer alone keeps this diff
honest and easy to judge.

**Verified against production Evolution, running the real patched module** (from a scratch dir
inside the container — production code was not touched):

| | before | after |
|---|---|---|
| history rows fetched for the Savio chat | 6 | **38** |
| transcript after `combine` (fresh, empty buffer) | 6 | **30** |
| **inbound (`OTHER:`) messages** | **0** | **12** |
| the lost order `@secretaria marque uma reuniao com o savio amanha` | absent | **recovered** |

Group chats re-checked: unchanged, **zero duplicate rows** from the merge.

`scripts/history-selftest.mjs` is new and is the regression guard. It is not vacuous — run
against the *original* `fetchHistory` it fails 5 of 7 checks, including the load-bearing one
("a 1:1 transcript contains inbound messages"); against the fix, all 7 pass. `node
scripts/selflearning-selftest.mjs` still passes.

### Discovered while verifying — worth its own ticket

`findMessages` paginates at **50 rows/page** and returns page 1 = the **newest** 50, descending.
That ordering is what makes this fix correct (we always merge *recent* history, never ancient).
But the 50 are *raw rows*, including non-text protocol noise — `messageContextInfo` device-list
records (`messageType: "unknown"`), reactions, stickers. In a busy chat a page can be mostly
noise: the group sampled here yielded **1 usable text message out of 50 rows**, even though the
group holds 3,574 text messages. So a busy chat's transcript can be far thinner than the
30-message window implies.

This is **pre-existing and unchanged by this fix** (the old code paginated identically) — but it
is the *next* thing standing between the secretary and a full transcript, and it is invisible
for exactly the same reason this bug was. Fix by requesting more pages, or by filtering
server-side to text message types.

## Deferred

- **The "date without a time" UX bug.** Even with a correct transcript, `amanha` gives a date
  but no time, so `start_iso` cannot be formed and the secretary must still ask. Today it asks
  for *"a data e o horário"* — re-requesting the date it already has, which is what made it feel
  like it wasn't listening. It should ask only for what is actually missing: *"Tenho amanhã —
  que horas?"*. Real, worth fixing, and **strictly separate from this bug.** File it on its own
  once the history fix is live and the behaviour can be re-observed on a correct transcript.
- **Raising the 30-message window.** With durable history restored, 30 may prove tight for a
  long scheduling thread. Measure before changing.

## Honest limitation

This fix restores the *durable* history. It does not make the buffer unnecessary for messages
that arrive between the webhook and the Postgres write, and it inherits whatever retention and
latency Evolution's persistence has — if Evolution drops a message, the secretary still never
sees it.

It also assumes `remoteJidAlt` is populated on LID rows. It is, on every row inspected here
(30 of the 34 LID rows in this chat carry it; the 4 without are non-text records). If a future
Evolution release changes that field, this breaks silently again — which is the argument for
fix 3, and for verification step 4 below.

## Verification

1. **The regression, from the log.** After deploying, restart the container (to guarantee an
   empty buffer), then in the Savio chat send `@secretaria agendar`. `TRANSCRIPT>>>` in
   `docker logs secretary` must now contain `@secretaria marque uma reuniao com o savio amanha`.
   This is the acceptance test — it is the exact condition that failed.
2. **The secretary must now resolve the date.** `CALENDAR RAW` should carry a non-null date for
   `amanha` (it may still, correctly, ask for the *time* — see Deferred).
3. **Groups must not regress.** In a `@g.us` chat, the transcript must be unchanged — the
   `remoteJidAlt` query returns nothing there, so the merge is a no-op. Confirm no duplicate
   lines appear.
4. **Assert the shape, don't trust it.** Add to `scripts/selflearning-selftest.mjs` (or a new
   fixture) a check that a merged `fetchHistory` for a known 1:1 JID returns **at least one
   `fromMe: false` message**. Today that assertion fails for every 1:1 chat in the system —
   which is precisely the bug, and precisely what would catch it coming back.
5. **Spot-check breadth.** Pick two other 1:1 chats and confirm their transcripts now contain
   `OTHER:` lines. Before the fix, none of them ever did.

## Files to touch

- `secretary/1. Orchestrator/lib/evolution.js` — `fetchHistory`: extract a `findMessages(where)`
  helper, query both `remoteJid` and `remoteJidAlt`, merge. **This is the fix** (~15 lines).
- `secretary/1. Orchestrator/lib/whatsapp.js` — correct the comment at line 70; optionally move
  `buffers` to Redis (fix 3).
- `secretary/1. Orchestrator/ORCHESTRATOR.md` — document LID addressing and why history is read
  under two keys. This is exactly the kind of invisible platform behaviour that will be
  rediscovered the hard way otherwise.
- `scripts/selflearning-selftest.mjs` — the inbound-messages-exist assertion (verification 4).
