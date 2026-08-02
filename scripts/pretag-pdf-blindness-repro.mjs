#!/usr/bin/env node
// ============================================================================
//  REPRODUCTION (throwaway, card beba8beb):
//  "Mary can't see a PDF sent BEFORE the @mary tag."
//
//  Symptom (owner-reported, verified): a PDF that arrived on an EARLIER message,
//  then referred to by a later @mary message ("me resuma o PDF acima"), is NOT
//  seen — Mary says she can't read attachments. The SAME PDF is read fine when it
//  is present on the very message that tags her (or when that message quotes it).
//
//  This harness drives the REAL turn-assembly path — inboundMedia()/getQuoted()
//  from lib/whatsapp.js — and mirrors server.js's exact media-prep gate:
//        const files = inboundMedia(data, quoted);   // server.js:319
//        ctx.media = null;                            // server.js:528
//        if (files.length) { ... ctx.media = {...} }  // server.js:529-583
//  It does NOT change any product code. It only OBSERVES what the turn assembles.
//
//  The claim under test: turn assembly sources media ONLY from the CURRENT message
//  (its *Message attachment) + its quoted reply. A file that lives only in earlier
//  history therefore yields files == [] -> ctx.media == null -> the model gets no
//  document block -> "I can't read attachments." That is the bug, at the assembly layer.
//
//  Run:  node scripts/pretag-pdf-blindness-repro.mjs
// ============================================================================

process.env.SECRETARY_TAG_NEW = "@mary"; // deterministic tag, before identity.js loads

const WA = await import(
  new URL("../secretary/1. Orchestrator/lib/whatsapp.js", import.meta.url).href
);
const ID = await import(
  new URL("../secretary/1. Orchestrator/lib/identity.js", import.meta.url).href
);
const { inboundMedia, getQuoted, extractText, combine, buildLabeledTranscript, remember } = WA;
const { matchedTagNew } = ID;

// --- server.js media-prep gate, transcribed verbatim in its decision shape --------------
// Returns what ctx.media WOULD be for a given webhook `data` (media detection only; the
// download/size/mime checks are downstream of files.length and irrelevant to "is it seen").
function ctxMediaFor(data) {
  const quoted = getQuoted(data); // server.js:315
  const files = inboundMedia(data, quoted); // server.js:319
  let media = null; // server.js:528
  if (files.length) media = { blocks: files.map((f) => f.mediaType) }; // server.js:529/583 (shape only)
  return { files, media };
}

const CHAT = "5511976001033@s.whatsapp.net"; // the chat from the report

console.log("============================================================");
console.log(" REPRO: Mary blind to a PDF sent before the @mary tag");
console.log("============================================================\n");

// --------------------------------------------------------------------------------------
// TURN 1 — the owner drops a PDF as a plain attachment. NO @mary tag on it.
// (documentMessage; the media id lives on data.key.id — Evolution's shape.)
// --------------------------------------------------------------------------------------
const turn1_pdf = {
  key: { id: "PDF_MSG_ID", fromMe: true, remoteJid: CHAT },
  messageTimestamp: 1000,
  message: {
    documentWithCaptionMessage: {
      message: {
        documentMessage: { fileName: "DENZA-B5.pdf", mimetype: "application/pdf", caption: "" },
      },
    },
  },
};
// server.js:313 buffers every message's TEXT for history. A document's text is "".
const t1text = extractText(turn1_pdf.message).trim();
if (t1text) remember(CHAT, { t: 1000, fromMe: true, text: t1text, pushName: "Marcelo" });
const r1 = ctxMediaFor(turn1_pdf);
console.log("TURN 1  — PDF attachment, no tag (not routed; here only to seed history)");
console.log(`         extractText -> ${JSON.stringify(t1text)}  (nothing buffered for history)`);
console.log(`         inboundMedia files: ${r1.files.length}\n`);

// --------------------------------------------------------------------------------------
// TURN 2 — the FAILING turn. Owner tags @mary and refers to "the PDF above".
// Plain text extendedTextMessage. It does NOT carry the PDF and does NOT quote it.
// --------------------------------------------------------------------------------------
const turn2_ref = {
  key: { id: "REF_MSG_ID", fromMe: true, remoteJid: CHAT },
  messageTimestamp: 1010,
  message: { extendedTextMessage: { text: "@mary me resuma o PDF acima, sobre o Denza." } },
};
remember(CHAT, { t: 1010, fromMe: true, text: "@mary me resuma o PDF acima, sobre o Denza.", pushName: "Marcelo" });
const gate2 = matchedTagNew(extractText(turn2_ref.message).trim());
const r2 = ctxMediaFor(turn2_ref);
console.log("TURN 2  — '@mary me resuma o PDF acima' (references the earlier PDF, no attach/quote)");
console.log(`         gate opens? matchedTagNew -> ${JSON.stringify(gate2)}  (turn IS routed)`);
console.log(`         inboundMedia files: ${r2.files.length}`);
console.log(`         ctx.media:          ${JSON.stringify(r2.media)}   <-- what the model receives`);

// The PDF plainly exists in history, yet is nowhere in the turn the model sees.
const conv = combine(CHAT, []); // [] = no Evolution durable history; buffer only
const transcript = buildLabeledTranscript(conv);
console.log("\n         labeled transcript the model DOES get (text-only history):");
console.log(transcript.split("\n").map((l) => "           " + l).join("\n"));
console.log("         -> the PDF from TURN 1 is not in the transcript (docs have no text) and");
console.log("            not in ctx.media (files empty). The model is blind to it.\n");

const BUG = r2.files.length === 0 && r2.media === null && gate2 === "@mary";
console.log(`  ${BUG ? "REPRODUCED" : "not reproduced"}: routed @mary turn, but ctx.media === null.`);
console.log(`             The model is told nothing about the earlier PDF -> "não consigo ler anexos".\n`);

// ======================================================================================
//  CONTRAST — the two shapes that SUCCEED in the report, proving the difference is
//  solely "is the file on THIS message?", not anything about the PDF itself.
// ======================================================================================
console.log("------------------------------------------------------------");
console.log(" CONTRAST — the same PDF, but present on the tagging turn:");
console.log("------------------------------------------------------------\n");

// SUCCESS A — the PDF is ATTACHED to the @mary message (captioned document).
const okAttach = {
  key: { id: "PDF_ON_TAG", fromMe: true, remoteJid: CHAT },
  messageTimestamp: 1020,
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
const rA = ctxMediaFor(okAttach);
console.log("SUCCESS A — PDF attached ON the @mary message ('@mary esse arquivo aqui')");
console.log(`         inboundMedia files: ${rA.files.length}  (${rA.files.map((f) => f.mediaType).join(", ")})`);
console.log(`         ctx.media:          ${JSON.stringify(rA.media)}   <-- document block reaches the model\n`);

// SUCCESS B — the @mary message QUOTES/replies to the earlier PDF message.
const okQuote = {
  key: { id: "REF_QUOTING_PDF", fromMe: true, remoteJid: CHAT },
  messageTimestamp: 1030,
  message: { extendedTextMessage: { text: "@mary me resuma o PDF acima, sobre o Denza." } },
  contextInfo: {
    stanzaId: "PDF_MSG_ID", // replying to TURN 1's message
    quotedMessage: { documentMessage: { fileName: "DENZA-B5.pdf", mimetype: "application/pdf" } },
  },
};
const rB = ctxMediaFor(okQuote);
console.log("SUCCESS B — @mary message QUOTES the earlier PDF (WhatsApp reply-to)");
console.log(`         inboundMedia files: ${rB.files.length}  (${rB.files.map((f) => f.source + ":" + f.mediaType).join(", ")})`);
console.log(`         ctx.media:          ${JSON.stringify(rB.media)}   <-- quoted document block reaches the model\n`);

console.log("============================================================");
console.log(" SUMMARY");
console.log("============================================================");
console.log(` pre-tag reference (TURN 2):   files=${r2.files.length}  ctx.media=${r2.media ? "SET" : "null"}   -> BLIND  (the bug)`);
console.log(` file on the tag  (SUCCESS A): files=${rA.files.length}  ctx.media=${rA.media ? "SET" : "null"}   -> reads it`);
console.log(` quotes the file  (SUCCESS B): files=${rB.files.length}  ctx.media=${rB.media ? "SET" : "null"}   -> reads it`);
console.log(`\n Turn assembly pulls media ONLY from the current message + its quoted reply,`);
console.log(` never from earlier history. A PDF that lives only in history is invisible.`);
process.exit(BUG ? 0 : 1);
