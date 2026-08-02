// ============================================================================
//  format.js  —  how a secretary message LOOKS on WhatsApp.
//  The owner and the secretary share one WhatsApp account, so replies land in
//  the same thread as the owner's own typing. Bold header + italic body is what
//  separates the two voices visually. Applied once, at the send boundary
//  (server.js send()), so no skill has to think about markup.
// ============================================================================

// WhatsApp italics do NOT span newlines: `_line one\nline two_` renders as
// literal underscores. Nearly every reply here is multiline, so we wrap line by
// line, never the body as a whole.
//
// A line is left PLAIN when wrapping it would corrupt something:
//   - blank: nothing to italicize.
//   - carries a URL: findCalendarLink() matches `eid=\S+` and `_` is a valid
//     base64url char, so a trailing marker gets swallowed into the eid and
//     silently breaks the reply-to-invite edit/delete flow (whatsapp.js).
//   - already contains _ * or ~ : emails (bruno_x@...), verbatim task titles,
//     transcripts. An unbalanced marker inside the span breaks the italics.
// Plain-but-correct beats italic-but-broken.
const URL_RE = /https?:\/\//i;
const MARKER_RE = /[_*~]/;

// Leading indentation and a leading bullet stay OUTSIDE the markers, so `- ` is
// still rendered as a bullet: `- _Buy milk_`.
const PREFIX_RE = /^(\s*(?:[-*•]\s+)?)(.*)$/;

export function italicizeBody(body) {
  if (!body) return body;
  return body
    .split("\n")
    .map((line) => {
      const trimmed = line.trimEnd();
      if (!trimmed.trim()) return line;
      if (URL_RE.test(trimmed) || MARKER_RE.test(trimmed)) return line;
      const [, prefix, content] = trimmed.match(PREFIX_RE);
      return content ? `${prefix}_${content}_` : line;
    })
    .join("\n");
}

// A full outgoing message: bold header, blank line, italic body. `italic: false`
// opts the body out of the styling entirely — an escape hatch for a message that
// is not the secretary's own voice. No caller needs it today (the audio transcript
// used to, and is now italic like everything else).
export function frame(header, body, { italic = true } = {}) {
  return `*${header}*\n\n${italic ? italicizeBody(body) : body}`;
}
