# Secretary Messages — Bold Header + Italic Body (Implementation Plan)

The owner and the secretary share one WhatsApp account, so every secretary reply lands in the
same thread as the owner's own typing, in the same plain black text. Today the only visual
separator is the header line (`[Marcelo's AI Secretary]:`). This plan makes the secretary's
voice typographically distinct: **bold header, italic body**.

Touch points: [`server.js`](../secretary/1.%20Orchestrator/server.js) (`send()`, the single funnel),
[`identity.js`](../secretary/1.%20Orchestrator/lib/identity.js) (header + self-detection), and one
bypass in [`Feature Requests/skill.js`](../secretary/2.%20Skills/4.%20Feature%20Requests/skill.js#L288)
(document caption). No skill's reply strings change.

## Goal

Every message the secretary sends renders as:

> **[Marcelo's AI Secretary]:**
>
> _Confirm this event:_
> _- Q3 budget review_
> _- ana@example.com_
> _- Jul 12, 2026, 3:00 PM (45 min)_
>
> _Reply "yes" to confirm and I'll send the invites._

WhatsApp markup: `*bold*` for the header, `_italic_` for the body.

Non-goal: restyling *what* the secretary says. Every localized reply string in every skill stays
byte-identical — this is a presentation layer applied at the send boundary, nothing else.

## The two constraints that shape the design

**1. WhatsApp italics do not span newlines.** `_line one\nline two_` renders as *literal
underscores*, not italics. Nearly every reply in the app is multiline (calendar confirms, task
lists, day-grouped agendas). So the formatter must wrap **each line individually**, not the body
as a whole.

**2. `isOwnMessage()` is the only thing keeping the bot from reading its own replies.** Because
replies arrive with `fromMe=true` (same account as the owner), self-detection is a pure header
`startsWith` check — [identity.js:42-43](../secretary/1.%20Orchestrator/lib/identity.js#L42-L43).
Bolding the header prepends a `*`, which breaks that match, and a broken match means the bot
treats its own reply as an owner continuation. **This is the one change that can loop the bot.**
It must be handled in the same commit.

## Implementation

### 1. New module: `lib/format.js`

One exported function, applied at the send boundary. Conservative by design — when a line looks
risky, it ships plain rather than shipping broken markers.

```js
// WhatsApp italics do not span newlines, so we wrap line by line. A line is left
// PLAIN (not wrapped) when wrapping it would corrupt something:
//   - blank lines: nothing to italicize
//   - lines carrying a URL: findCalendarLink() matches `eid=\S+`, and a trailing `_`
//     is a valid base64url char — it would be swallowed into the eid and silently
//     corrupt the reply-to-invite edit/delete flow (whatsapp.js:60-66)
//   - lines already containing _ * or ~ : emails (bruno_x@…), verbatim task titles,
//     transcripts. An unbalanced marker inside breaks the italics unpredictably.
// Leading indentation and a leading bullet stay OUTSIDE the markers so `- ` still
// renders as a bullet: `- _Buy milk_`.
const URL_RE = /https?:\/\//i;
const MARKER_RE = /[_*~]/;

export function italicizeBody(body) {
  if (!body) return body;
  return body
    .split("\n")
    .map((line) => {
      const trimmed = line.trimEnd();
      if (!trimmed.trim()) return line;                 // blank
      if (URL_RE.test(trimmed) || MARKER_RE.test(trimmed)) return line;
      const m = trimmed.match(/^(\s*(?:[-*•]\s+)?)(.*)$/); // indent + bullet, then content
      const [, prefix, content] = m;
      return content ? `${prefix}_${content}_` : line;
    })
    .join("\n");
}

// The secretary's full outgoing message: bold header, blank line, italic body.
export function frame(header, body, { italic = true } = {}) {
  return `*${header}*\n\n${italic ? italicizeBody(body) : body}`;
}
```

### 2. `server.js` — `send()` ([server.js:147-151](../secretary/1.%20Orchestrator/server.js#L147-L151))

Format **after** `localizeBody()`. The translation fallback runs the body through Haiku and its
prompt promises to preserve URLs, numbers and line breaks — it says nothing about `_`/`*`
([server.js:129](../secretary/1.%20Orchestrator/server.js#L129)). Never let the model see the
markers; add them last.

```js
async function send(number, text, lang = "en", opts = {}) {
  const body = await localizeBody(text, lang);          // markers added AFTER translation
  return evolution.sendText(number, frame(headerFor(lang), body, opts));
}
```

And thread the opts through the skill-facing binding
([server.js:296](../secretary/1.%20Orchestrator/server.js#L296)):

```js
ctx.send = (number, text, opts) => send(number, text, ctx.lang, opts);
```

`opts.italic === false` is the escape hatch — see the audio transcript below.

### 3. `identity.js` — keep self-detection alive

`HEADERS` stays raw (unbolded); the bold is applied by `frame()`. Only the matcher changes: strip
leading formatting markers before comparing, so it recognizes **both** the new bold header and
every plain-header message already sitting in the fetched history and the in-memory buffer.

```js
export function isOwnMessage(text) {
  const t = (text || "").replace(/^[*_~\s]+/, "");   // tolerate a bolded/italicized header
  return ALL_HEADERS.some((h) => t.startsWith(h));
}
```

Backward compatibility matters here: `LEGACY_HEADERS` already exists for exactly this reason, and
the buffer/history will contain unbolded headers for as long as the current chat history is read.

### 4. Feature Requests — the one bypass

[`skill.js:288-300`](../secretary/2.%20Skills/4.%20Feature%20Requests/skill.js#L288-L300) builds its
own header and calls `evolution.sendMedia` directly, so it never passes through `send()`. Point it
at the shared helper so the document caption gets the same treatment:

```js
const caption = frame(headerFor(ctx.lang), reply(ctx.lang).docCaption({ title: draft.title || slug }));
```
(Its copy ends in a `📄` emoji — emoji sit fine inside `_…_`.)

### 5. Audio transcript — the deliberate exception

[`Audio/prompt.js:25`](../secretary/2.%20Skills/2.%20Audio%20transcriptions/prompt.js#L25) sends
`Audio transcript:\n\n${text}` — and `text` is *the owner's own words*, not the secretary's. The
whole point of this feature is to distinguish the two, so italicizing a transcript works against
it. Send that one message with `ctx.send(number, …, { italic: false })`.

Trade-off, stated plainly: this leaves the `Audio transcript:` label plain too. That's the cheap
version. If it reads badly, the fix is a per-message split (italic label, plain transcript), which
is a second call or a sentinel — not worth it before seeing it in the thread.

## Edge cases

- **Multiline** — handled by per-line wrapping. This is the whole reason the naive
  `_${body}_` one-liner does not work.
- **Calendar invite URLs** (`createDone` / `editDone`,
  [prompt.js:507](../secretary/2.%20Skills/1.%20Calendar%20Actions/prompt.js#L507)) — the URL is on
  its own line, so the URL carve-out leaves that line plain and the `eid` stays clean. **This is
  the highest-consequence edge case**: a corrupted `eid` silently breaks reply-to-invite delete/edit.
- **Emails with underscores** (`bruno_x@gmail.com`) and **verbatim task titles** — caught by the
  `MARKER_RE` carve-out; those lines ship plain. Slightly inconsistent, never broken.
- **Bullet lines** (`- ${title}` throughout calendar and tasks) — bullet stays outside the markers.
- **LLM-authored bodies** sent verbatim (Feature Requests' `out.reply`, calendar's `editClarify`)
   — they're plain prose, so per-line wrapping is safe; if the model emits a `*` the carve-out
   drops that line to plain.
- **Long-tail languages** — markers are applied after `localizeBody()`, so Haiku never sees them.
- **Router context** — the bot's own messages are fed back to the router LLM as history with the
  markers in them. Harmless noise; no prompt reads the body structurally.

## Testing

1. `@secretary what's on my calendar` → header bold, every event line italic, day headers italic.
2. `@secretary schedule dinner tomorrow 8pm` → confirm bubble italic; `yes` → done message italic
   **and the Google Calendar link line plain and clickable**.
3. **Reply to that invite message** with "cancel it" → the eid still resolves and the event is
   deleted. (Regression guard for the `_`-in-eid corruption.)
4. Multi-turn: after any bot reply, send a bare continuation (`yes`) → the bot must **not** treat
   its own bolded reply as an owner message. Watch the log for a routing loop. (Regression guard
   for `isOwnMessage`.)
5. Task list with a title containing `_` → that line ships plain, the rest italic, nothing broken.
6. Audio note → transcript body plain (not italic), header still bold.
7. Feature request → document caption has the bold header.
8. A non-en/pt message (e.g. Spanish) → translation still clean, markers intact and correct.

## Deploy & done-when

- Deploy = git pull + restart on the droplet (see the project deploy workflow); production write —
  **get an explicit go-ahead before shipping**.
- **Done when:** every secretary message in the thread reads as a bold header over an italic body;
  calendar links stay clickable and reply-to-invite still resolves; the bot never re-consumes its
  own message. Then update `ARCHITECTURE.md` (the header contract at ~L160) and archive this plan
  to `Shipped Features/`.
