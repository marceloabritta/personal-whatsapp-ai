#!/usr/bin/env node
// ============================================================================
//  Self-test for conversation history retrieval (lib/evolution.js + lib/whatsapp.js).
//
//  The bug this exists to prevent (2026-07-12, "LID history blindness"):
//  WhatsApp LID addressing stores a 1:1 chat's INBOUND messages under the contact's
//  `…@lid` JID, while the JID the webhook hands us — and that we send to — is the phone
//  `…@s.whatsapp.net`. `fetchHistory` queried only the latter, so the durable history
//  came back containing NOTHING BUT THE SECRETARY'S OWN OUTBOUND MESSAGES. Every skill
//  in every 1:1 chat was running on the volatile in-memory buffer alone, and every
//  container restart wiped the secretary's memory. It cost a real scheduling failure.
//
//  So the load-bearing invariant is #2: a 1:1 transcript MUST contain inbound messages.
//  If someone "simplifies" fetchHistory back to a single query, that test goes red.
//
//    1. fetchHistory issues BOTH queries: key.remoteJid AND key.remoteJidAlt
//    2. a 1:1 transcript CONTAINS INBOUND (OTHER:) messages          <-- the regression
//    3. overlapping rows from the two queries are deduped by combine
//    4. one failing query does not take down the other (partial > empty)
//    5. group chats (@g.us) still work and gain no duplicates
//    6. ordering is chronological and the window caps at `limit`
//
//  No network, no keys — `fetch` is stubbed.
//
//  Run:  node scripts/history-selftest.mjs
// ============================================================================
import { createEvolution } from "../secretary/1. Orchestrator/lib/evolution.js";
import { combine, buildTranscript } from "../secretary/1. Orchestrator/lib/whatsapp.js";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

const PHONE = "553171746333@s.whatsapp.net"; // what the webhook gives us
const LID = "250104602693736@lid"; // where inbound actually lands
const GROUP = "5512981548521-1390317844@g.us";

const msg = (t, fromMe, text, remoteJid) => ({
  key: { id: `${t}`, fromMe, remoteJid, remoteJidAlt: remoteJid === LID ? PHONE : undefined },
  messageTimestamp: t,
  pushName: fromMe ? "Você" : "Savinho Carissimo",
  message: { conversation: text },
});

// The real production shape: outbound-via-API rows sit under the phone JID, the actual
// conversation (both sides, incl. messages the owner typed on his phone) under the LID.
const BY_PHONE = [msg(100, true, "_Antes de agendar, preciso da data e horário._", PHONE)];
const BY_LID = [
  msg(90, true, "@secretaria marque uma reuniao com o savio amanha", LID), // the lost order
  msg(95, false, "Top", LID),
  msg(100, true, "_Antes de agendar, preciso da data e horário._", LID), // overlaps BY_PHONE
  msg(105, false, "domingos.carissimo@gmail.com", LID),
];

// --- stub fetch -------------------------------------------------------------
let queries = [];
let failNextRemoteJid = false;

function stubFetch(rows) {
  globalThis.fetch = async (_url, opts) => {
    const { where } = JSON.parse(opts.body);
    queries.push(where);
    if (where.key?.remoteJid && failNextRemoteJid) {
      return { ok: false, status: 500, text: async () => "boom" };
    }
    const jid = where.key?.remoteJid ?? where.key?.remoteJidAlt;
    const byAlt = !!where.key?.remoteJidAlt;
    const recs = rows.filter((r) =>
      byAlt ? r.key.remoteJidAlt === jid : r.key.remoteJid === jid
    );
    return { ok: true, status: 200, json: async () => ({ messages: { records: recs } }) };
  };
}

const evo = createEvolution({ url: "http://stub", apikey: "x", instance: "i" });

// --- 1 + 2 + 3 + 6: the 1:1 chat -------------------------------------------
queries = [];
stubFetch([...BY_PHONE, ...BY_LID]);

const hist = await evo.fetchHistory(PHONE);
const conv = combine(PHONE, hist); // empty buffer == a freshly restarted container
const transcript = buildTranscript(conv);

check(
  "1. queries BOTH key.remoteJid and key.remoteJidAlt",
  queries.length === 2 &&
    queries.some((q) => q.key?.remoteJid === PHONE) &&
    queries.some((q) => q.key?.remoteJidAlt === PHONE)
);

const inbound = conv.filter((m) => !m.fromMe);
check(
  "2. a 1:1 transcript contains INBOUND messages  <-- the LID regression",
  inbound.length === 2 && transcript.includes("OTHER: Top")
);

check(
  "   2b. the order the owner actually gave is present",
  transcript.includes("@secretaria marque uma reuniao com o savio amanha")
);

// t=100 is returned by BOTH queries with identical text -> must appear once.
const dupes = conv.filter((m) => m.t === 100);
check("3. rows returned by both queries are deduped by combine", dupes.length === 1);

check(
  "6. chronological order, capped at the window",
  conv.map((m) => m.t).join(",") === "90,95,100,105" && combine(PHONE, hist, 2).length === 2
);

// --- 4: a failing query must not take down the other ------------------------
queries = [];
failNextRemoteJid = true;
stubFetch([...BY_PHONE, ...BY_LID]);

const partial = await evo.fetchHistory(PHONE);
check(
  "4. one query failing still returns the other's rows (partial > empty)",
  partial.length === BY_LID.length &&
    partial.some((m) => m.text.includes("marque uma reuniao"))
);
failNextRemoteJid = false;

// --- 5: groups keep working, and gain no duplicates -------------------------
queries = [];
const GROUP_ROWS = [
  msg(200, false, "bom dia", GROUP),
  msg(210, true, "bom dia!", GROUP),
];
stubFetch(GROUP_ROWS);

const gconv = combine(GROUP, await evo.fetchHistory(GROUP));
check(
  "5. group chats still work and gain no duplicates",
  gconv.length === 2 && gconv.filter((m) => !m.fromMe).length === 1
);

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
