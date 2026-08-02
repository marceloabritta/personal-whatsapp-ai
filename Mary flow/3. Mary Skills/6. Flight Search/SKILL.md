# Skill: `flight_search`

> **@mary tree — CONVERTED (pure task).** This is the `secretary/3. Mary Skills/` copy. The
> **orchestrator** runs the slot-chase and the confirm-before-search dialogue and hands a validated
> payload in `ctx.info`; there is **no** in-skill session and **no** LLM pass. But the **options
> sidecar** stays — it is a data cache keyed off a separate redis key (`${remoteJid}|flights`), not
> a conversation session. `manifest.conversation:"orchestrator"` with declared `inputs`
> (discriminator `intent ∈ search|link|book|other`; `link` requires `option_number`). `run(ctx)`
> follows READ-then-ACT: `search` queries Kiwi, selects the options, sends them, STASHES them, and
> **returns** `{options:[{n,summary,price,bookingUrl}],count}`; `link` reads the sidecar and sends
> the booking link, returning `{ok,option}`; `book`/`other` decline. Invariant S (a new search
> tombstones the old options at flow start) and the filter→sort→take-3 order below are unchanged.

> **For humans — quick read.**
>
> Ask for a flight in a sentence; get the three cheapest options **a person would actually
> pick**; ask for the booking link of one of them. It never buys anything.
>
> **How it works:**
> 1. `@secretary find me a flight from São Paulo to Lisbon on the 14th, back on the 22nd`.
> 2. Anything missing (origin, destination, departure date) is **asked for, one field at a
>    time**. Nothing is guessed — there is no assumed origin.
> 3. **It confirms before it searches**: origin → destination, the dates, passengers, cabin.
>    Say `yes`, or correct it (*"2 people, business"*, *"make it the 15th"*) as often as you
>    like. Say `no` and it drops the whole thing.
> 4. It searches, **throws away the junk** (see *The result filter* — this is the part that
>    matters), and shows the **3 cheapest survivors**, numbered, with the airport per leg
>    (São Paulo is three airports), the stop count and the times.
> 5. `link for option 2` — tagged or not — and it sends that option's booking link. Then the
>    conversation is over: the session and the options are cleared.
>
> **Three things worth knowing:**
> - **It shows fewer than three options sometimes, and occasionally none.** That is the
>   filter working, not a bug. It says so plainly when it happens.
> - **It cannot buy.** Say *"book it"* and it sends the link **and** tells you it can't
>   purchase. The purchase is yours to make on Kiwi's page.
> - **It never claims a search "expired".** If the options are gone it says what actually
>   happened — either *"I dropped those when the new search started"* or *"I don't have any
>   options on hand"*. It only says what it knows.
>
> **Setup:** no API key. Kiwi's endpoint is keyless. One optional env var,
> `FLIGHT_CURRENCY` (default `BRL`).

---

## The flow

```
@secretary find me a flight ...      (tagged: the router picks flight_search)
        │
        ├─ interpret (C1)  ── intent: search | link | book | other
        │                     │
        │  intent "search" ───┴──▶ WRITE THE TOMBSTONE (Invariant S — see below)
        │                          │
        │                          ├─ a required field is missing ─▶ ask for it   (await_info)
        │                          └─ complete ───────────────────▶ confirm       (await_confirmation)
        │                                                              │ "yes"
        │                                                              ▼
        │                                                        Kiwi search
        │                                                              │
        │                                          filter (both legs) → sort → take 3
        │                                                              │
        │                                          show 3 options + stash them (await_link)
        │
        └─ intent "link"/"book" ──▶ answerLink()  (NO tombstone — a link request is not a search)
```

Three LLM passes, three different questions (`prompt.js`):

| Pass | Schema | Question |
|---|---|---|
| C1 | `FLIGHT_SCHEMA` | A fresh message: is this a **search**, a **link** request for options already shown, **book**, or **other**? Plus the trip's fields. |
| B | `FLIGHT_REVIEW_SCHEMA` | A draft is pending: **confirm / modify / cancel / unrelated** — and a `modify` re-drafts in the same call. |
| C2 | `LINK_REVIEW_SCHEMA` | The options are on the table: **link / book / done / unrelated**. |

**Confirm-first, on a read-only action.** This deliberately deviates from the repo's convention
(`calendar_action`'s `list` is read-only, no confirm). It earns its keep for a different reason:
it catches a wrong reading — the origin, the date, 1 passenger, economy, a one-way he meant as a
return — **before** he reads three useless options. Do not "fix" it.

---

## The result filter — the one behaviour you will mistake for a bug

**Kiwi is a virtual-interlining OTA.** It sells itineraries that chain **unrelated carriers on
separate tickets**, where a missed connection is the passenger's problem. Its `search-flight`
tool accepts exactly `flyFrom, flyTo, departureDate, departureDateFlexDays, returnDate,
returnDateFlexDays, adults, children, infants, cabinClass, currency, locale` — **there is no
max-stops parameter and no self-transfer exclusion.** So the filter cannot live at the API. It
lives in `skill.js`, and it runs **before** the 3-cheapest selection.

**The rule, in order (`selectOptions`):**

1. **Stops.** Drop any itinerary with **more than 1 stop on any leg**.
2. **Carrier chaining.** Drop any itinerary with a **leg whose segments are not all the same
   carrier** — the self-transfer discriminator.
3. **Then** the owner's own explicit filters (`direct`, `overnight`), on top — never instead.
4. **Then** sort by price ascending and take 3.

**Why the order is load-bearing, measured not asserted.** On a real SAO→LIS capture
(2026-07-12, 15 results), **the four cheapest results were all self-transfer carrier chains**:

```
DROP #0  5392  CGH>REC>LIS stops=1 carr=[LA,TP]  | LIS>REC>CGH carr=[TP,G3]   <carrier-chain>
DROP #1  5449  CGH>REC>LIS stops=1 carr=[AD,TP]  | LIS>REC>CGH carr=[TP,G3]   <carrier-chain>
DROP #2  5484  CGH>REC>LIS stops=1 carr=[LA,TP]  | LIS>REC>CGH carr=[TP,LA]   <carrier-chain>
DROP #3  5640  CGH>REC>LIS stops=1 carr=[AD,TP]  | LIS>REC>CGH carr=[TP,LA]   <carrier-chain>
KEEP #4  5768  VCP>LIS      stops=0 carr=[AD]    | LIS>VCP     carr=[AD]
```

A "3 cheapest, then filter" build shows the owner exactly that junk — or nothing at all. The
filter runs **first**, over **both legs** (a clean outbound with a chained inbound is still a
self-transfer itinerary), and `selectOptions()` is the single place that owns the order.
`scripts/flights-selftest.mjs` test #4 fails loudly on any other order.

**Kiwi exposes NO self-transfer marker.** The union of every key at all three levels
(itinerary / leg / segment) was taken across a full result set: nothing matching
`self|transfer|virtual|interlin|separate|ticket|protect|guarantee`. The carrier-chain fallback
is the only discriminator available, and it is **deliberately over-strict** — it also drops a
legitimate single-ticket alliance connection. **When in doubt, drop.** One fewer option is a
small cost; a self-transfer chain presented as an ordinary connection is only discovered at the
airport.

> **⚠ KIWI'S RESULTS ARE VOLATILE, AND THIS WILL LOOK LIKE A BUG TOO.** The identical query,
> run four times while this was built, returned **four disjoint result sets**. On one, 11 of 15
> itineraries survived the filter; on another, **15 of 15** survived and the filter did nothing
> at all. So *"the filter dropped everything today and nothing yesterday"* is **expected**, not
> a malfunction. It also means the selftest's fixture is **frozen and hand-built** — a fixture
> regenerated from a live call would very likely stop discriminating and would silently gut the
> test suite. **Never refresh it** (test `#4a` exists to catch you).

**What the owner sees when the filter bites** — and these are four *different* facts, kept apart
on purpose:

| Situation | Reply | Sender |
|---|---|---|
| 3+ survive | the ordinary 3-cheapest list (`results`) | `ctx.send` |
| 1–2 survive | show them **and say why there are fewer than three** (`thinnedResults`). Never pad back to 3, never disable the filter. | `ctx.send` |
| Kiwi returned results, the filter judged them, **none survived** | `emptyAfterFilter` — *"only multi-stop / split-ticket itineraries were on offer"*. The rejected chain is never shown. | `ctx.send` |
| Kiwi returned **0 results** | `emptyResults` | `ctx.send` |
| **Nothing could be JUDGED** (the `segments`/`carrier` data the filter needs was absent) | **`searchFailed`** — this is provider shape-drift, not an empty result, and it belongs on the Bugs board. | `ctx.sendFailure` |

---

## The stash, and its three states (Invariant S)

The options do **not** live in the chat's session. They live in a **sidecar key**,
`` `${remoteJid}|flights` ``, in the same session store.

**Why.** A *tagged* follow-up — `@secretary link for option 2` — makes the orchestrator clear the
chat's session (`server.js:402`) **before** the router has decided which skill the order belongs
to (`server.js:407`). The bare-jid session dies there. The sidecar does not: `sessions.clear()`
is an exact-key delete, with no wildcard and no key scan, so it cannot reach `…|flights`.

**Three states, three different facts:**

| Stash | Means | Reply to "link for option 2" |
|---|---|---|
| `{ options: [...] }` | the options are live | the booking link (`linkSent`) |
| `{ discarded: true }` | **a new search destroyed them** (the tombstone) | `resultsDiscarded` — *"I dropped those when the new search started"* |
| `null` | nothing here at all | `noResultsToLink` — *"I don't have any flight options on hand"* |

**INVARIANT S — the tombstone is written at FLOW START.** `writeTombstone()` is called in
`run()`, the moment `interpret()` returns `intent: "search"`: **before** the slot chase, **before**
the confirmation, **before** Kiwi is ever called. Not inside `runSearch()`.

Get that wrong and this comes back:

> search A stashes 3 options → search B returns **0 results** → *"I found no flights"* →
> owner: *"link for option 2"* → **the skill sends search A's stale booking URL.**

A link turn (`intent: "link"`/`"book"`) must **not** tombstone — it would destroy the very options
it is about to read. Both mistakes have a test: `#14a`/`#14b` catch a late tombstone, `#14c` catches
an unconditional one.

**No reply in this skill ever claims a search EXPIRED.** The skill cannot know that: an absent
stash means "expired" *or* "already sent" *or* "never searched", and picking one is a lie. It says
only what it knows. (`scripts/flights-selftest.mjs` #10 asserts that no reply string, `en` or `pt`,
matches `/expir/i`.)

---

## The 23 reply keys and their senders

All 23 ship `en` **and** `pt` in `prompt.js`. The rule is the **outcome**, not the tone: *did the
owner ask for something the system should have been able to give him, and not get it?*

**`ctx.send` (19)** — successes, questions, and truthful empty results:
`askOrigin`, `askDestination`, `askDate`, `cityAmbiguous`, `cityUnknown`, `badDate`,
`returnBeforeDepart`, `declined`, `results`, `thinnedResults`, `emptyResults`, `emptyAfterFilter`,
`explicitFilterEmpty`, `linkSent`, `whichOption`, `optionOutOfRange`, `resultsDiscarded`,
`noResultsToLink`, `cannotBook`.

**`ctx.sendFailure` (4)** — he asked, and he did not get it (these reach the Bugs board):
`searchFailed`, `thinkingError`, `notAFlight`, `linkMissing`.

Two of these are worth defending, because they look like near-misses:

- **`optionOutOfRange`** ("option 5" of 3) is a **question**, not a failure. Nothing malfunctioned.
- **`cannotBook`** goes out on **`ctx.send`**. Booking is a **decided product boundary**, not a
  missing capability: the flow completed as designed and he *does* get the link. (A decision by
  the owner, made when this skill was planned. Do not flip it without asking him.)

---

## The Kiwi wire contract (in brief — the full one is in `PROJECT_LOG.md` §8)

```
POST https://mcp.kiwi.com
Content-Type: application/json
Accept: application/json, text/event-stream        <-- BOTH. json alone -> HTTP 406.

{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
  "name":"search-flight",
  "arguments":{"flyFrom":"SAO","flyTo":"LIS","departureDate":"14/08/2026",
               "returnDate":"22/08/2026","adults":1,"cabinClass":"M",
               "currency":"BRL","locale":"pt"}}}
```

The five things that will otherwise bite you:

1. **Keyless.** No API key, **no `initialize` handshake, no `Mcp-Session-Id`.** A cold
   `tools/call` works. Do not build session plumbing.
2. **The response is always SSE-framed, and the frame is CRLF** —
   `event: message\r\ndata: {…}\r\n\r\n`. The parser splits on `/\r?\n/`, takes **every** `data: `
   line, concatenates and parses once. Read `result.structuredContent`.
3. **`isError: true` arrives on an HTTP 200**, with **no `structuredContent`** and
   `content[0].text` as a **plain, non-JSON string**. A naive `JSON.parse(content[0].text)` throws.
   **Check `isError` first.** It lands as `searchFailed`.
4. **Dates are `dd/mm/yyyy`, not ISO.** An ISO date returns `isError: true`. The skill keeps ISO
   internally and converts at the wire (`toKiwiDate`).
5. **`cabinClass` is the enum `M|W|C|F`** (economy / premium economy / business / first).

Also true, and load-bearing for the renderer: **on a one-way, `inbound` is present and `null`**
(not absent) — so `"inbound" in it` would render an empty return block. And a past date, or a city
Kiwi cannot resolve, comes back as a cheerful `resultsCount: 0` — which is why `badDate` is
computed **in our code**, before the call.

**Kiwi is a self-described prototype with no SLA.** Its shape can change or the endpoint can
vanish. Every unreadable answer (`isError`, a shape we can't parse, nothing judgeable) lands on
`ctx.sendFailure` → the owner's Bugs board, rather than masquerading as "no flights today".
`locale` is fixed at `pt` deliberately: it drives Kiwi's own booking page, not our reply.
`ctx.lang` controls only what *we* say.

---

## Config

| Env var | Default | Note |
|---|---|---|
| `FLIGHT_CURRENCY` | `BRL` | The currency asked of Kiwi. **There is no provider API key.** If Kiwi returns a different currency, it is displayed as returned and never converted. |

## Files

**`manifest.inputs`** — this skill **declares** its required inputs (`intent`, `origin`,
`destination`, `depart_date`, `return_date`, `adults`, `cabin`, `summary`) so the orchestrator's
merged router call can pre-extract them (`1. Orchestrator/lib/inputs.js`). It does **not yet
consume `ctx.info`**: it still makes its own extraction call. Adopting the pre-extracted payload
needs its own live accuracy check and is a follow-up card — only this skill's *routing* has been
measured under the merged prompt, never its payload accuracy.

| File | What's in it |
|---|---|
| `skill.js` | `manifest` + `run`, the C1/C2 recognition, the slot chase, confirm/modify, the Kiwi client, **`selectOptions` (the filter → sort → take-3 order)**, the stash lifecycle, `answerLink`. |
| `prompt.js` | The 3 schemas, their prompt builders, the 23-key `{ en, pt }` reply map, and the renderers (`renderOptions`, `renderConfirm`, `localizeFlightDate`, `fmtDuration`). |

## Tests

`node scripts/flights-selftest.mjs` — offline, no keys, no network (`fetch` and `ctx.anthropic`
are stubbed; `createSessions()` runs on its in-memory Map). 45 assertions. The two it exists for
are **#4** (filter before sort) and **#14** (the tombstone at flow start).

A change to this skill's `manifest.description` requires the **live** router check —
`ANTHROPIC_API_KEY=… node scripts/router-selftest.mjs` — because every skill's description is fed
to the router's catalog, so rewording ours can misroute a skill nobody touched. It costs money.
