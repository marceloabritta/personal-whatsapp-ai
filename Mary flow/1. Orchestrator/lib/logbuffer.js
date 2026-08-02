// ============================================================================
//  logbuffer.js  —  an in-memory RING BUFFER of the secretary's own logs.
//
//  There is no log FILE: everything is console.log/console.error to stdout, read
//  with `docker logs`. So nothing could read its own logs in-process — and a
//  failure report without "what the code was doing just before" is nearly useless.
//
//  installLogBuffer() wraps console so every line (a) still prints to stdout
//  unchanged (docker logs keeps working) and (b) is pushed into a fixed-size ring
//  that captureFailure() can read back (lib/selflearning.js).
//
//  Entries are REDACTED and TRUNCATED on the way IN, not on the way out: whatever
//  sits in this buffer can end up in a Markdown file inside a git repo, so a secret
//  must never enter it in the first place.
// ============================================================================

let RING = [];
let CAPACITY = 500;
let MAX_LINE_CHARS = 2000;
let installed = false;

// ---- Redaction ---------------------------------------------------------------
// Defence in depth, NOT a guarantee: the repo is private and the owner reviews every
// report before acting on it. Order matters — the generic high-entropy sweep runs LAST
// so the specific, well-labelled patterns get a chance to name what they redacted.
const SECRET_PATTERNS = [
  [/sk-ant-[A-Za-z0-9_-]{10,}/g, "«redacted:anthropic-key»"],
  [/AIza[0-9A-Za-z_-]{30,}/g, "«redacted:google-key»"],
  [/1\/\/[A-Za-z0-9_-]{20,}/g, "«redacted:google-refresh-token»"],
  [/Bearer\s+\S+/gi, "Bearer «redacted»"],
  [
    /((?:api[-_]?key|apikey|authorization|token|secret|password)\s*[:=]\s*)\S+/gi,
    "$1«redacted»",
  ],
  // Long high-entropy blobs (base64, JWTs, ids). Deliberately last, and deliberately
  // blunt: an over-redacted log line is a cost we happily pay for a leaked key we don't.
  [/\b[A-Za-z0-9_-]{60,}\b/g, "«redacted:blob»"],
];

export function redact(text) {
  let s = String(text ?? "");
  for (const [re, replacement] of SECRET_PATTERNS) s = s.replace(re, replacement);
  return s;
}

// ---- The ring ----------------------------------------------------------------
function stringifyArg(a) {
  if (typeof a === "string") return a;
  if (a instanceof Error) return `${a.message}\n${a.stack || ""}`;
  try {
    return JSON.stringify(a);
  } catch {
    return String(a); // circular / exotic — String() never throws here
  }
}

function push(level, args) {
  let text = args.map(stringifyArg).join(" ");
  if (text.length > MAX_LINE_CHARS) {
    // server.js logs the ENTIRE transcript on every webhook (TRANSCRIPT>>>…). Untruncated,
    // that one line would crowd every other log entry out of a report.
    text = `${text.slice(0, MAX_LINE_CHARS)}… «truncated ${text.length - MAX_LINE_CHARS} chars»`;
  }
  RING.push({ t: new Date().toISOString(), level, text: redact(text) });
  if (RING.length > CAPACITY) RING.shift();
}

// Call ONCE, at the top of server.js's body — before anything else logs. Safe to call
// after the imports: no imported module logs at module scope, they only log inside
// functions. Idempotent (a second call is a no-op, so a re-import can't double-wrap).
export function installLogBuffer({ capacity = 500, maxLineChars = 2000 } = {}) {
  if (installed) return;
  installed = true;
  CAPACITY = capacity;
  MAX_LINE_CHARS = maxLineChars;

  for (const level of ["log", "warn", "error"]) {
    const original = console[level].bind(console);
    console[level] = (...args) => {
      original(...args); // stdout FIRST and unconditionally — docker logs is the source of truth
      try {
        push(level, args);
      } catch {
        /* a broken buffer must never break logging, let alone the request */
      }
    };
  }
}

// The last `n` entries, oldest first, as a plain string. `n` is larger for owner-reported
// failures: the mistake happened in an EARLIER webhook turn, so the evidence sits further
// back in the ring, under the current turn's own logging (see selflearning.js).
export function getRecentLogs(n = 80) {
  return RING.slice(-n)
    .map((e) => `${e.t} [${e.level}] ${e.text}`)
    .join("\n");
}

// Test seam (scripts/selflearning-selftest.mjs).
export function __resetLogBuffer() {
  RING = [];
}
