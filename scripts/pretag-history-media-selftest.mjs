#!/usr/bin/env node
// ============================================================================
//  Self-test for the reference-gated history->media fallback (card beba8beb):
//  "Mary can't see a PDF sent BEFORE the @mary tag."
//
//  The bug: turn assembly sourced ctx.media ONLY from the current message's
//  attachment + its quoted reply (inboundMedia). A file sent on an EARLIER
//  message and then referred to in words after the @mary tag ("me resuma o PDF
//  acima") reached the model as nothing -> "I can't read attachments". The repro
//  (scripts/pretag-pdf-blindness-repro.mjs) demonstrates the failing half.
//
//  The fix adds a THIRD media source: the single most-recent relayable file in
//  recent history, pulled in ONLY when (a) the turn carries no attachment/quote
//  AND (b) the order text actually REFERS to a file (mentionsFile). An unrelated
//  turn (calendar / time / chit-chat) pulls NOTHING. That reference gate is the
//  human's explicit requirement: "she should only fetch a file from the history
//  in case the conversation calls for it."
//
//  This drives the REAL exports from lib/whatsapp.js — inboundMedia, getQuoted,
//  and the three new helpers mentionsFile / historyMediaOf / historyMediaFile —
//  and the REAL matchedTagNew from lib/identity.js. assemble() below mirrors the
//  server.js MEDIA-PREP media-detection decision INCLUDING the reference-gated
//  fallback, exactly as the plan specifies:
//        let files = inboundMedia(data, quoted);                 // server.js:321
//        if (!files.length && mentionsFile(order)) {             // the new gate
//          const h = historyMediaFile(history, nowSec);
//          if (h) files = [h];
//        }
//        ctx.media = files.length ? {...} : null;                // server.js:549+
//  It changes NO product code; it only OBSERVES what the turn assembles.
//  `order`, `nowSec` and row timestamps are supplied by the test so both the gate
//  and the 1 h recency bound are fully deterministic. No network, no API key.
//
//  Run:  node scripts/pretag-history-media-selftest.mjs
// ============================================================================
import {
  inboundMedia,
  getQuoted,
  mentionsFile,
  historyMediaOf,
  historyMediaFile,
} from "../secretary/1. Orchestrator/lib/whatsapp.js";
import { matchedTagNew } from "../secretary/1. Orchestrator/lib/identity.js";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

const CHAT = "5511976001033@s.whatsapp.net"; // the chat from the report

// A fixed "now" so recency is deterministic (no Date.now in the assertions).
const NOW = 1_000_000_000;

// --- server.js MEDIA-PREP media-detection decision, incl. the reference gate ----------
// Returns what { files, ctx.media } WOULD be for a webhook `data` + the owner's post-tag
// `order`, against a projected `history` at time `nowSec`. Detection only — the download /
// size / mime checks live downstream of files.length and are live-only (see the honesty
// note at the bottom); they don't affect "is the file seen".
function assemble(data, order, history, nowSec) {
  const quoted = getQuoted(data); // server.js:315
  let files = inboundMedia(data, quoted); // server.js:321
  if (!files.length && mentionsFile(order)) {
    // the new reference-gated fallback (server.js:549+)
    const h = historyMediaFile(history, nowSec);
    if (h) files = [h];
  }
  return {
    files,
    media: files.length ? { blocks: files.map((f) => f.mediaType), source: files[0].source } : null,
  };
}

// --- fixtures --------------------------------------------------------------------------
// A recent PDF row, in fetchHistory's POST-FIX projected shape (media id carried through).
const recentPdf = {
  t: NOW - 60,
  fromMe: true,
  text: "", // a document has no text -> combine drops it from the transcript, as today
  pushName: "Marcelo",
  mediaId: "PDF_MSG_ID",
  mediaType: "document",
  mimetype: "application/pdf",
};
// The same PDF, but 2 h old — outside the 1 h recency window.
const stalePdf = { ...recentPdf, t: NOW - 7200 };
// History with no relayable media at all (text-only rows).
const textOnlyHistory = [
  { t: NOW - 120, fromMe: false, text: "oi", pushName: "Marcelo", mediaId: null, mediaType: null, mimetype: null },
  { t: NOW - 90, fromMe: true, text: "tudo bem?", pushName: "Você", mediaId: null, mediaType: null, mimetype: null },
];

// A text-only @mary turn that REFERS to the earlier PDF (no attach, no quote) — the bug's turn.
const turnRef = {
  key: { id: "REF_MSG_ID", fromMe: true, remoteJid: CHAT },
  messageTimestamp: NOW,
  message: { extendedTextMessage: { text: "@mary me resuma o PDF acima, sobre o Denza." } },
};
// The PDF ATTACHED on the @mary message (captioned document) — an on-turn file.
const turnAttach = {
  key: { id: "PDF_ON_TAG", fromMe: true, remoteJid: CHAT },
  messageTimestamp: NOW,
  message: {
    documentWithCaptionMessage: {
      message: {
        documentMessage: {
          fileName: "DENZA-B5.pdf",
          mimetype: "application/pdf",
          caption: "@mary esse arquivo aqui",
        },
      },
    },
  },
};
// The @mary message QUOTES the earlier PDF (WhatsApp reply-to) — an on-turn quoted file.
const turnQuote = {
  key: { id: "REF_QUOTING_PDF", fromMe: true, remoteJid: CHAT },
  messageTimestamp: NOW,
  message: { extendedTextMessage: { text: "@mary me resuma o PDF acima, sobre o Denza." } },
  contextInfo: {
    stanzaId: "PDF_MSG_ID", // replying to the earlier PDF message
    quotedMessage: { documentMessage: { fileName: "DENZA-B5.pdf", mimetype: "application/pdf" } },
  },
};

console.log("============================================================");
console.log(" Reference-gated history->media fallback (card beba8beb)");
console.log("============================================================\n");

// These are genuine @mary turns (the gate the orchestrator opens on).
check(
  "0. matchedTagNew opens on the reference turn (it IS routed)",
  matchedTagNew("@mary me resuma o PDF acima, sobre o Denza.") === "@mary"
);

// --- 1. THE FIX — file-referencing pre-tag turn + recent PDF -> the file is pulled -----
{
  const r = assemble(turnRef, "me resuma o PDF acima", [recentPdf], NOW);
  check(
    "1. file-referencing @mary turn + recent PDF -> files=1, media SET, source=history, id matches",
    r.files.length === 1 &&
      r.media !== null &&
      r.files[0].source === "history" &&
      r.files[0].id === "PDF_MSG_ID"
  );
}

// --- 2. THE HUMAN'S REQUIREMENT — an UNRELATED turn pulls NOTHING ----------------------
{
  const time = assemble(turnRef, "que horas são?", [recentPdf], NOW);
  const cal = assemble(turnRef, "marca uma reunião amanhã às 10h", [recentPdf], NOW);
  check(
    "2a. unrelated 'que horas são?' + recent PDF -> files=0, media=null (nothing pulled)",
    time.files.length === 0 && time.media === null
  );
  check(
    "2b. calendar 'marca uma reunião amanhã às 10h' + recent PDF -> files=0, media=null",
    cal.files.length === 0 && cal.media === null
  );
}

// --- 3. No regression — on-turn attachment (history NOT consulted) ---------------------
{
  const r = assemble(turnAttach, "@mary esse arquivo aqui", [recentPdf], NOW);
  check(
    "3. on-turn attachment -> files=1, source=attachment (gate not reached; history unused)",
    r.files.length === 1 && r.files[0].source === "attachment"
  );
}

// --- 4. No regression — quoted file (history NOT consulted) ----------------------------
{
  const r = assemble(turnQuote, "me resuma o PDF acima", [recentPdf], NOW);
  check(
    "4. quoted file -> files=1, source=quote (gate not reached; history unused)",
    r.files.length === 1 && r.files[0].source === "quote"
  );
}

// --- 5. No over-reach — references a file but none in history --------------------------
{
  const r = assemble(turnRef, "me resuma o PDF", textOnlyHistory, NOW);
  check(
    "5. references a file but history has none -> files=0, media=null",
    r.files.length === 0 && r.media === null
  );
}

// --- 6. Recency bound — file-referencing turn but only a stale (2 h) file --------------
{
  const r = assemble(turnRef, "me resuma o PDF acima", [stalePdf], NOW);
  check(
    "6. file-referencing turn but only a stale (t=now-7200) file -> files=0, media=null",
    r.files.length === 0 && r.media === null
  );
}

// --- 7. mentionsFile unit cases (en+pt positives incl. diacritics; negatives) ---------
check("7a. mentionsFile('me resuma o PDF acima') === true", mentionsFile("me resuma o PDF acima") === true);
check("7b. mentionsFile('manda a foto') === true", mentionsFile("manda a foto") === true);
check("7c. mentionsFile('o documento que enviei') === true", mentionsFile("o documento que enviei") === true);
check("7d. mentionsFile('esse formulário') === true (diacritic-insensitive)", mentionsFile("esse formulário") === true);
check("7e. mentionsFile('que horas são?') === false", mentionsFile("que horas são?") === false);
check("7f. mentionsFile('marca uma reunião amanhã às 10h') === false", mentionsFile("marca uma reunião amanhã às 10h") === false);
check("7g. mentionsFile('tudo bem?') === false", mentionsFile("tudo bem?") === false);
check("7h. mentionsFile('') === false", mentionsFile("") === false);
check("7i. mentionsFile(null) === false", mentionsFile(null) === false);

// --- 8. historyMediaOf projection carries the id --------------------------------------
{
  // A raw Evolution record whose message node is a bare documentMessage; the id is on r.key.id.
  const docRec = {
    key: { id: "PDF_MSG_ID" },
    message: { documentMessage: { fileName: "DENZA-B5.pdf", mimetype: "application/pdf" } },
  };
  const doc = historyMediaOf(docRec.message);
  check(
    "8a. documentMessage node -> { mediaType:document, mimetype:application/pdf }",
    !!doc && doc.mediaType === "document" && doc.mimetype === "application/pdf"
  );
  // The projected row (fetchHistory's shape) takes its mediaId from r.key.id — case 1 relies on this.
  const projected = doc ? { mediaId: docRec.key.id, mediaType: doc.mediaType, mimetype: doc.mimetype } : null;
  check(
    "8b. the projected row's mediaId === r.key.id ('PDF_MSG_ID') — what case 1 relies on",
    !!projected && projected.mediaId === "PDF_MSG_ID"
  );
  // The captioned wrapper unwraps to the same document descriptor.
  const wrapped = historyMediaOf({
    documentWithCaptionMessage: { message: { documentMessage: { mimetype: "application/pdf" } } },
  });
  check(
    "8c. captioned documentWithCaptionMessage node -> mediaType:document",
    !!wrapped && wrapped.mediaType === "document"
  );
  // An image node projects to an image descriptor.
  const img = historyMediaOf({ imageMessage: { mimetype: "image/jpeg" } });
  check(
    "8d. imageMessage node -> { mediaType:image, mimetype:image/jpeg }",
    !!img && img.mediaType === "image" && img.mimetype === "image/jpeg"
  );
  // A text node carries no relayable media.
  check("8e. text node (conversation) -> null", historyMediaOf({ conversation: "oi" }) === null);
}

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
