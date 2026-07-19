#!/usr/bin/env node
// ============================================================================
//  Self-test for the GENERALIZED INBOUND-MEDIA RELAY (card cf60f344):
//  "File & Image Interpretation in Stateful Conversations".
//
//  Written BEFORE the code, from PLAN.md §Tests (A–F) + the "Amendment —
//  mid-session captioned-file order" sub-assertion. Offline: no network, no API
//  key, no Redis, no Google credentials, no framework, no new dependency. FREE.
//
//  THE FEATURE, in one line: relay any inbound media on a @mary turn to the turn
//  call as Anthropic multimodal content — detected on every entry point, carrying
//  ALL supported files as N content blocks, behind one minimal extension point.
//
//  WHAT THIS SUITE ASSERTS (the DETERMINISTIC layer only, per CONVENTIONS §5):
//    A. inboundMedia() returns the turn's media LIST, both webhook shapes, id from key.id
//    B. inboundMedia() multi-file (attachment + quoted) — attachment first, quote second
//    C. the captioned-document gate opens (new flow), legacy stays shut; plus the
//       mid-session (untagged) continuation order derivation (the Amendment)
//    D. extractText() BYTE-IDENTITY regression tripwire (its three callers)
//    E. mediaBlockFor() — the extension point: native image/PDF vs defer-to-null
//    F. route() — N-block array when media present, byte-identical string when absent,
//       empty-text guard, model pinned where route() reads it, read-back vs repair
//
//  WHAT IT CANNOT CATCH (stated, CONVENTIONS §5): whether a live vision model reads
//  the right number off a real receipt — that is model accuracy, not orchestration,
//  and it is not assertable offline.
//
//  ⚠ WHY IT IS RED TODAY, AND WHY THAT IS THE POINT.
//  lib/whatsapp.js exports NEITHER inboundMedia NOR mediaBlockFor yet, and route()
//  does not read ctx.media. So:
//    - A/B/C/E are RED because the two new exports are `undefined` (the detector /
//      extension point do not exist). The safe wrappers below turn a missing export
//      into a FAILING check, never a thrown script and never a false green.
//    - F.1 / F.4b are RED because route() builds a STRING and reads ctx.model where the
//      feature requires an N-block ARRAY and the pinned media.model.
//    - D, F.2, F.4a are GREEN today and MUST STAY green — they are the byte-identity /
//      legacy-untouched guards, not regressions.
//  A missing export is read via a DYNAMIC import (property access -> `undefined`), never
//  a static `import { inboundMedia }`, which would be a link-time SyntaxError — i.e. the
//  script would fail for the WRONG reason.
//
//  Run:  node scripts/file-relay-selftest.mjs
// ============================================================================

// Deterministic tags, set BEFORE any import evaluates identity.js's env-seeded lists.
process.env.SECRETARY_TAG = "@assistente,@assistant"; // legacy list
process.env.SECRETARY_TAG_NEW = "@mary"; // new-flow list

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// ---- real modules, loaded by DYNAMIC import (missing exports -> undefined) ----
const WA = await import(
  new URL("../secretary/1. Orchestrator/lib/whatsapp.js", import.meta.url).href
);
const ID = await import(
  new URL("../secretary/1. Orchestrator/lib/identity.js", import.meta.url).href
);
const RT = await import(
  new URL("../secretary/1. Orchestrator/router/router.js", import.meta.url).href
);
const PR = await import(
  new URL("../secretary/1. Orchestrator/router/prompt.js", import.meta.url).href
);

const { extractText, getQuoted } = WA;
const inboundMedia = WA.inboundMedia; // undefined today
const mediaBlockFor = WA.mediaBlockFor; // undefined today
const { matchedTag, matchedTagNew } = ID;
const { route } = RT;
const { buildRouterUser } = PR;

const HAS_DETECTOR = typeof inboundMedia === "function";
const HAS_BLOCK = typeof mediaBlockFor === "function";

if (!HAS_DETECTOR || !HAS_BLOCK) {
  console.log(
    "\n  ..    lib/whatsapp.js exports:" +
      `  inboundMedia=${HAS_DETECTOR ? "present" : "MISSING"}` +
      `  mediaBlockFor=${HAS_BLOCK ? "present" : "MISSING"}\n` +
      "        The missing export(s) are why sections A/B/C/E below are RED — and for\n" +
      "        no other reason. Each is a unit assertion on a function that does not\n" +
      "        exist yet. The feature is absent, the script is not broken.\n"
  );
}

// A sentinel that is NEITHER an array NOR null — so an ABSENT function makes the
// "expect a list" AND the "expect null" checks BOTH fail, never a false green.
const ABSENT = Symbol("absent");
let detectorErr = "";
let blockErr = "";

function im(data, quoted) {
  if (!HAS_DETECTOR) return ABSENT;
  try {
    return inboundMedia(data, quoted);
  } catch (e) {
    detectorErr = e?.message || String(e);
    return ABSENT;
  }
}
function mb(arg) {
  if (!HAS_BLOCK) return ABSENT;
  try {
    return mediaBlockFor(arg);
  } catch (e) {
    blockErr = e?.message || String(e);
    return ABSENT;
  }
}
const len = (x) => (Array.isArray(x) ? x.length : -1);
const at = (x, i) => (Array.isArray(x) ? x[i] : undefined);
const attachCaption = (list) =>
  Array.isArray(list) ? list.find((m) => m.source === "attachment")?.caption || "" : ABSENT;

// ============================================================================
//  A. Detector returns a LIST — both webhook shapes, id from data.key.id.
// ============================================================================
console.log("\n=== A. inboundMedia — the media LIST, both webhook shapes ===\n");

// A bare documentMessage. The id lives on data.key.id (server.js:356), NOT in
// data.message — the node below carries NO id, so a pass proves key.id was used.
const bareDoc = {
  key: { id: "A" },
  message: { documentMessage: { caption: "@mary total?", mimetype: "application/pdf" } },
};
const aBare = im(bareDoc, null);
check(
  'A.1  bare documentMessage -> [{attachment, document, id:"A", caption:"@mary total?", application/pdf}]',
  len(aBare) === 1 &&
    at(aBare, 0)?.source === "attachment" &&
    at(aBare, 0)?.mediaType === "document" &&
    at(aBare, 0)?.id === "A" &&
    at(aBare, 0)?.caption === "@mary total?" &&
    at(aBare, 0)?.mimetype === "application/pdf"
);

// The captioned-wrapper shape (assumed for a captioned PDF): the SAME fields nested
// one level deeper. Must yield the SAME single-entry list — the pinned-shape guard.
const wrapDoc = {
  key: { id: "A" },
  message: {
    documentWithCaptionMessage: {
      message: { documentMessage: { caption: "@mary total?", mimetype: "application/pdf" } },
    },
  },
};
const aWrap = im(wrapDoc, null);
check(
  "A.2  documentWithCaptionMessage wrapper -> the SAME single-entry list (pinned-shape guard)",
  len(aWrap) === 1 &&
    at(aWrap, 0)?.source === "attachment" &&
    at(aWrap, 0)?.mediaType === "document" &&
    at(aWrap, 0)?.id === "A" &&
    at(aWrap, 0)?.caption === "@mary total?" &&
    at(aWrap, 0)?.mimetype === "application/pdf"
);

// An imageMessage with a caption.
const imgMsg = {
  key: { id: "I" },
  message: { imageMessage: { caption: "look at this", mimetype: "image/png" } },
};
const aImg = im(imgMsg, null);
check(
  'A.3  imageMessage w/ caption -> one {attachment, image, id:"I", caption, image/png} entry',
  len(aImg) === 1 &&
    at(aImg, 0)?.source === "attachment" &&
    at(aImg, 0)?.mediaType === "image" &&
    at(aImg, 0)?.id === "I" &&
    at(aImg, 0)?.caption === "look at this" &&
    at(aImg, 0)?.mimetype === "image/png"
);

// A plain conversation text -> no media.
const textMsg = { key: { id: "T" }, message: { conversation: "hello there" } };
check("A.4  plain conversation text, no quote -> [] (empty list)", len(im(textMsg, null)) === 0);

// id from key.id specifically: a doc whose message node has NO id anywhere.
const bareDoc2 = {
  key: { id: "KEYID" },
  message: { documentMessage: { caption: "x", mimetype: "application/pdf" } },
};
check("A.5  id is read from data.key.id, never from data.message", at(im(bareDoc2, null), 0)?.id === "KEYID");

// ============================================================================
//  B. Detector — MULTI-FILE turn (attachment + quoted), attachment first.
// ============================================================================
console.log("\n=== B. inboundMedia — multi-file (attachment + quoted) ===\n");

// A document attachment (id "A") in a webhook whose contextInfo.quotedMessage is an
// image (its id arrives as stanzaId "Q"). getQuoted reads the sibling contextInfo.
const multi = {
  key: { id: "A" },
  message: { documentMessage: { caption: "see attached", mimetype: "application/pdf" } },
  contextInfo: { stanzaId: "Q", quotedMessage: { imageMessage: { mimetype: "image/jpeg" } } },
};
const q = getQuoted(multi);
const bList = im(multi, q);
check(
  "B.1  attachment + quoted -> TWO entries, attachment first (source:attachment, id:A), " +
    "quote second (source:quote, image, id:Q)  — the no-silent-primary-only guard",
  len(bList) === 2 &&
    at(bList, 0)?.source === "attachment" &&
    at(bList, 0)?.id === "A" &&
    at(bList, 1)?.source === "quote" &&
    at(bList, 1)?.mediaType === "image" &&
    at(bList, 1)?.id === "Q"
);

// ============================================================================
//  B2. AUDIO is EXCLUDED from detection (card cf60f344 regression fix).
//  Audio is NOT a relay concern: the AI can't take audio natively, so it is
//  always handled by transcribe_audio via NORMAL routing (ctx.hasQuotedAudio),
//  never intercepted by the media relay. A quoted voice-note is transcribe_audio's
//  ONLY trigger; if inboundMedia surfaced it, server.js media-prep would mark it
//  fileUnsupported and close the turn BEFORE the router runs — killing the skill.
//  So inboundMedia must yield NO audio entry, for a quoted audio AND a direct one.
// ============================================================================
console.log("\n=== B2. inboundMedia — AUDIO is excluded (transcribe_audio not intercepted) ===\n");

// Quoted voice-note: reply to an audio (getQuoted -> mediaType "audio", hasAudio true).
// This is transcribe_audio's trigger; the relay must NOT surface it.
const quotedAudioTurn = {
  key: { id: "K" },
  message: { conversation: "@mary transcribe" },
  contextInfo: { stanzaId: "VN", quotedMessage: { audioMessage: { mimetype: "audio/ogg" } } },
};
const qAudio = getQuoted(quotedAudioTurn);
const bAudioList = im(quotedAudioTurn, qAudio);
check(
  "B2.1  getQuoted surfaces the quoted audio (mediaType audio, hasAudio true) — routing still sees it",
  qAudio?.mediaType === "audio" && qAudio?.hasAudio === true
);
check(
  "B2.2  quoted audio -> inboundMedia returns NO audio entry (transcribe_audio's turn is not intercepted)",
  len(bAudioList) === 0 && Array.isArray(bAudioList) && !bAudioList.some((m) => m.mediaType === "audio")
);

// Direct audio attachment (a bare voice note sent as an attachment): also excluded.
const audioAttach = {
  key: { id: "AU" },
  message: { audioMessage: { mimetype: "audio/ogg" } },
};
const aAudioList = im(audioAttach, null);
check(
  "B2.3  direct audioMessage attachment -> NO audio entry (audio reaches transcribe_audio via routing)",
  len(aAudioList) === 0 && Array.isArray(aAudioList) && !aAudioList.some((m) => m.mediaType === "audio")
);

// ============================================================================
//  C. Gate opens for a captioned document (critical path) + the Amendment.
// ============================================================================
console.log("\n=== C. captioned-document gate open + mid-session order (Amendment) ===\n");

// First message, tagged caption. extractText of a document is "" today; the caption
// carries the tag+order. The NEW matcher sees (text || caption); the LEGACY stays shut.
const cText = extractText(bareDoc.message).trim(); // "" for a document
const cCap = attachCaption(im(bareDoc, null)); // "@mary total?"
const gateText = typeof cCap === "string" ? cText || cCap : ABSENT;
check(
  'C.1  NEW matcher opens on (text||caption): matchedTagNew("@mary total?") === "@mary"',
  gateText !== ABSENT && matchedTagNew(gateText) === "@mary"
);
check(
  'C.2  new-flow order slice (text||caption).slice(tag).trim() === "total?"',
  gateText !== ABSENT && gateText.slice("@mary".length).trim() === "total?"
);
check(
  "C.3  LEGACY matcher stays shut for the same document: matchedTag(text) === null",
  matchedTag(cText) === null
);

// Legacy image path unchanged: a captioned image's text ALREADY carries the caption,
// so gateText (text || caption) === text and the caption is never consulted.
const iText = extractText(imgMsg.message).trim(); // "look at this"
const iCap = attachCaption(im(imgMsg, null));
const iGate = typeof iCap === "string" ? iText || iCap : ABSENT;
check("C.4  captioned image: gateText === text (legacy image path unchanged)", iGate !== ABSENT && iGate === iText);

// Amendment — mid-session (untagged) captioned DOCUMENT continuation: text is "",
// so the derived new-flow order falls back to the attachment caption. This is the gap
// the Amendment closes (line 420's continuation branch is `text.trim()`, "" today).
const midDoc = {
  key: { id: "A" },
  message: { documentMessage: { caption: "which line is biggest?", mimetype: "application/pdf" } },
};
const midText = extractText(midDoc.message).trim(); // ""
const midCap = attachCaption(im(midDoc, null)); // "which line is biggest?"
const midOrder = typeof midCap === "string" ? midText.trim() || midCap.trim() : ABSENT;
check(
  'C.5  mid-session captioned document (untagged): derived order === "which line is biggest?"',
  midOrder !== ABSENT && midOrder === "which line is biggest?"
);

// Amendment — a QUOTE entry (caption "", source "quote") must NOT override a real
// continuation text: with a text order and only a quoted file, order stays the text.
const quoteOnly = {
  key: { id: "K" },
  message: { conversation: "keep this" },
  contextInfo: { stanzaId: "Q", quotedMessage: { imageMessage: { mimetype: "image/jpeg" } } },
};
const qoText = extractText(quoteOnly.message).trim(); // "keep this"
const qoCap = attachCaption(im(quoteOnly, getQuoted(quoteOnly))); // "" — no attachment, only a quote
const qoOrder = typeof qoCap === "string" ? qoText.trim() || qoCap.trim() : ABSENT;
check(
  'C.6  a quote entry does NOT override a real continuation text: order === "keep this"',
  qoOrder !== ABSENT && qoOrder === "keep this"
);

// ============================================================================
//  D. extractText BYTE-IDENTITY — the regression tripwire on its three callers.
//  GREEN today; goes RED if anyone adds a documentMessage branch to extractText
//  (which would silently change the legacy flow + both transcript builders).
// ============================================================================
console.log("\n=== D. extractText byte-identity regression tripwire ===\n");

check('D.1  extractText(documentMessage) === "" (no document branch)', extractText({ documentMessage: { caption: "@mary x" } }) === "");
check(
  'D.2  extractText(documentWithCaptionMessage wrapper) === ""',
  extractText({ documentWithCaptionMessage: { message: { documentMessage: { caption: "@mary x" } } } }) === ""
);
check('D.3  extractText(conversation) still returns the text', extractText({ conversation: "hi" }) === "hi");
check('D.4  extractText(extendedTextMessage.text) still returns the text', extractText({ extendedTextMessage: { text: "yo" } }) === "yo");
check('D.5  extractText(imageMessage.caption) still returns the caption', extractText({ imageMessage: { caption: "cap" } }) === "cap");
check('D.6  extractText(videoMessage.caption) still returns the caption', extractText({ videoMessage: { caption: "vid" } }) === "vid");

// ============================================================================
//  E. Extension point mediaBlockFor — native block vs defer-to-null.
//  media_type comes from the REAL mimetype, NEVER a hard-coded default — so an
//  audio/ogg mime leaking onto an image is REJECTED, not trusted (the MUST-RESOLVE).
// ============================================================================
console.log("\n=== E. mediaBlockFor — the extension point (native vs defer) ===\n");

const eImg = mb({ mediaType: "image", mimetype: "image/png", base64: "AAA" });
check(
  "E.1  image + image/png -> image block (base64, media_type image/png)",
  eImg !== ABSENT &&
    eImg?.type === "image" &&
    eImg?.source?.type === "base64" &&
    eImg?.source?.media_type === "image/png" &&
    eImg?.source?.data === "AAA"
);
const ePdf = mb({ mediaType: "document", mimetype: "application/pdf", base64: "BBB" });
check(
  "E.2  document + application/pdf -> document block (base64, media_type application/pdf)",
  ePdf !== ABSENT &&
    ePdf?.type === "document" &&
    ePdf?.source?.type === "base64" &&
    ePdf?.source?.media_type === "application/pdf" &&
    ePdf?.source?.data === "BBB"
);
check(
  "E.3  document + docx mime -> null (defer to unsupported)",
  mb({
    mediaType: "document",
    mimetype: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    base64: "C",
  }) === null
);
check("E.4  audio + audio/ogg -> null (deferred sibling)", mb({ mediaType: "audio", mimetype: "audio/ogg", base64: "D" }) === null);
check("E.5  video + video/mp4 -> null (deferred sibling)", mb({ mediaType: "video", mimetype: "video/mp4", base64: "E" }) === null);
check(
  "E.6  image + audio/ogg -> null (the getMediaBase64 fallback is NEVER trusted; no malformed block)",
  mb({ mediaType: "image", mimetype: "audio/ogg", base64: "F" }) === null
);

// ============================================================================
//  F. route() — N-block array with media, byte-identical string without, model pin.
//  A FakeSDK records messages.create params so `content` and `model` are assertable.
// ============================================================================
console.log("\n=== F. route() — content array vs string, model pin, read-back vs repair ===\n");

class FakeSDK {
  constructor() {
    this.seen = [];
    this.messages = {
      create: async (params) => {
        this.seen.push(params);
        // A valid, parseable reply so route() completes cleanly — the return value is
        // irrelevant; only the RECORDED params matter here.
        return { stop_reason: "end_turn", content: [{ type: "text", text: '{"next":"done"}' }] };
      },
    };
  }
}

const imgBlock = { type: "image", source: { type: "base64", media_type: "image/png", data: "AAA" } };
const pdfBlock = { type: "document", source: { type: "base64", media_type: "application/pdf", data: "BBB" } };

function baseCtx(over = {}) {
  const sdk = new FakeSDK();
  const ctx = {
    owner: "Marcelo",
    anthropic: sdk,
    model: "ctx-model", // deliberately NOT the vision model — proves the pin overrides it
    order: "read this",
    transcript: "OWNER: hi",
    nowStr: "2026-07-18 10:00",
    contact: "Laura",
    hasQuotedAudio: false,
    quoted: null,
    catalog: [],
    tags: ["@mary"],
    ...over,
  };
  return { ctx, sdk };
}
const sentContent = (sdk) => sdk.seen[0]?.messages?.[0]?.content;
const sentModel = (sdk) => sdk.seen[0]?.model;

// F.1 — media present, initial turn: content is [img, pdf, {text}] and model is pinned.
{
  const { ctx, sdk } = baseCtx({ media: { blocks: [imgBlock, pdfBlock], model: "claude-sonnet-5" } });
  await route(ctx, {});
  const content = sentContent(sdk);
  check(
    "F.1a media present -> content is [imgBlock, pdfBlock, {type:text}] (media-before-text, N=2)",
    Array.isArray(content) &&
      content.length === 3 &&
      content[0] === imgBlock &&
      content[1] === pdfBlock &&
      content[2]?.type === "text"
  );
  check(
    'F.1b model pinned to media.model ("claude-sonnet-5") regardless of ctx.model',
    sentModel(sdk) === "claude-sonnet-5"
  );
}

// F.2 — media absent: content is the EXACT buildRouterUser string and model is ctx.model
// (byte-identical — Decision 8). GREEN today; the guard that keeps text-only turns unchanged.
{
  const { ctx, sdk } = baseCtx({ media: undefined });
  await route(ctx, {});
  const expected = buildRouterUser("Marcelo", {
    order: "read this",
    transcript: "OWNER: hi",
    hasQuotedAudio: false,
    hasQuotedCalendarLink: false,
    nowStr: "2026-07-18 10:00",
    contact: "Laura",
    quotedText: null,
  });
  check("F.2a media absent -> content === the exact buildRouterUser string (byte-identical)", sentContent(sdk) === expected);
  check("F.2b media absent -> model === ctx.model (byte-identical)", sentModel(sdk) === "ctx-model");
}

// F.3 — empty-text guard (nit a). GUARD-THE-GUARD: buildRouterUser NEVER renders empty
// (it always carries the date/contact preamble), so an empty `user` cannot be produced
// through the real route()+buildRouterUser path. Per PLAN.md §Tests F ("In production
// buildRouterUser is never empty; the assertion guards the guard") and the fallback the
// card grants, this asserts the DOCUMENTED guard expression from router.js — that media +
// an empty user yields a media-ONLY array with NO trailing text block. It is the one
// assertion here that does not drive route() (see TESTS.md).
{
  const blocks = [imgBlock, pdfBlock];
  const user = "";
  const content = user && user.trim() ? [...blocks, { type: "text", text: user }] : [...blocks];
  check(
    "F.3  empty user + media -> media-only array, NO trailing text block (guard-the-guard; see TESTS.md)",
    Array.isArray(content) && content.length === 2 && !content.some((b) => b.type === "text")
  );
}

// F.4 — a READ-BACK carries no file (content is a string); a REPAIR keeps it (array).
{
  const { ctx, sdk } = baseCtx({ media: { blocks: [imgBlock, pdfBlock], model: "claude-sonnet-5" } });
  await route(ctx, { readback: { result: "done", said: null } });
  check("F.4a media set + turn.readback -> content is a STRING (no media on a read-back)", typeof sentContent(sdk) === "string");
}
{
  const { ctx, sdk } = baseCtx({ media: { blocks: [imgBlock, pdfBlock], model: "claude-sonnet-5" } });
  await route(ctx, { repair: "Your last payload was missing a field." });
  const content = sentContent(sdk);
  check(
    "F.4b media set + turn.repair (no readback) -> content is an ARRAY (repair keeps the media)",
    Array.isArray(content) && content[0] === imgBlock
  );
}

// ---- verdict ----------------------------------------------------------------
if (detectorErr) console.log(`\n  note: inboundMedia threw: ${detectorErr}`);
if (blockErr) console.log(`  note: mediaBlockFor threw: ${blockErr}`);
console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
