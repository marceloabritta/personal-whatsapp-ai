#!/usr/bin/env node
// ============================================================================
//  Self-test for the board ingest — the exactly-once, nothing-dropped core of
//  "specs and bugfix plans land on the backlog by themselves".
//
//  This drives the exported functions of scripts/board-ingest.mjs (seed / enqueue /
//  drain / parseHeader) against a throwaway temp repo (mkdtemp) and an in-memory STUB
//  BOARD — an object exposing fetch(url, init) that mimics the kanban's real HTTP API
//  (POST /api/card -> {id, kind}; GET /api/board -> {cards:[…]}; GET /api/card/{id} ->
//  {abs_dir}). Every failure mode is produced by telling the stub to misbehave. No
//  network, no keys, no board, no model call.
//
//  The invariants, grouped by the promise they protect:
//
//  NOTHING IS CREATED TWICE
//    1. the lost ack (edge 4): the board made the card but the ack was lost -> the next
//       drain reconciles by the `source:` footer and POSTs ZERO times. One card.
//    2. failed copy then retry (edge 5): the copy fails -> entry NOT archived, cardId IS
//       recorded, the retry copies without a second POST. One card.
//    3. two drains + the lock (edge 21 / D4): a live lock -> {lockHeld:true}, no POST; a
//       DEAD lock is broken and the drain proceeds. The queue can never deadlock.
//    4. enqueue is idempotent (edge 14): twice over the same tree -> the same entries.
//    5. delivered never resurrects (edges 15, 19): an archived source is not re-queued
//       when edited; a GET /api/card/{id} 404 mid-drain archives, never re-POSTs.
//
//  NOTHING IS SILENTLY DROPPED
//    6. the ledger seed (edge 16): seed the 8 files on disk, enqueue -> ZERO cards; then
//       a fresh feature-*.md and bugfix-*.md -> exactly two, kinds feature + maintenance.
//    7. the interlock: enqueue() with no ledger.tsv THROWS and queues nothing.
//    8. the owner-report predicate (edges 10/11/12): the fixture report is written by the
//       REAL captureFailure, so the predicate is tested against what the generator writes.
//    9. board down (edge 1): the stub refuses the connection -> entries stay queued,
//       drain() returns boardDown:true, exit code 0.
//   10. titles (edges 9, 13): a title-less spec falls back, never null/"feature"; a
//       header-less plan falls back to its # H1 and LOGS.
//   11. the footer: the created card's description ends with `source: <basename>` — the
//       exact line test 1 reconciles on. Reformat it and both tests go red together.
//   12. the wrong-board tripwire (drain step b2): a board that IGNORES `kind` still gets
//       exactly one card AND the mismatch is reported. Guards ./update.sh swapping the
//       vendored board out from under us.
//
//  Run:  node scripts/board-ingest-selftest.mjs
// ============================================================================
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// ---- import the (not-yet-built) ingest ---------------------------------------
// Until scripts/board-ingest.mjs exists this fails HERE, loudly and for the right
// reason: the feature is missing. Everything below is what the next column must make
// green — it must not need editing to do so.
let mod;
try {
  mod = await import("./board-ingest.mjs");
} catch (e) {
  console.error("\ncould not import scripts/board-ingest.mjs — the ingest does not exist yet:");
  console.error(`  ${e?.message || e}\n`);
  check("scripts/board-ingest.mjs exists and exports seed/enqueue/drain/parseHeader", false);
  console.log(`\nFAIL (${failures}) — the ingest is unbuilt; nothing below could run.\n`);
  process.exit(1);
}
const { seed, enqueue, drain, parseHeader } = mod;

// ---- fixtures ----------------------------------------------------------------
async function makeRepo() {
  const repo = await mkdtemp(path.join(tmpdir(), "board-ingest-"));
  await mkdir(path.join(repo, "New Features Plans"), { recursive: true });
  await mkdir(path.join(repo, "Bugs and Malfunctions", "_reports"), { recursive: true });
  await mkdir(path.join(repo, "Board Inbox"), { recursive: true });
  await mkdir(path.join(repo, ".cards"), { recursive: true });
  return repo;
}
const write = (repo, rel, body) => writeFile(path.join(repo, rel), body, "utf8");
const exists = async (p) => !!(await stat(p).catch(() => null));
const jsonRes = (status, obj) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => obj,
  text: async () => JSON.stringify(obj),
});

// A stub board. Its behaviour is bent entirely by `opts`:
//   refuse       — every fetch throws (a down / refused board).
//   ignoreKind   — POST returns kind:"" and files into a pipeline, not the backlog.
//   lostAck      — POST records the card, then THROWS instead of returning (edge 4).
//   badAbsDir    — GET /api/card/{id} points abs_dir at a dir that does not exist.
//   card404      — GET /api/card/{id} returns 404 (card deleted mid-drain, edge 19).
function makeBoard(repo, opts = {}) {
  const board = { cards: [], posts: 0, n: 0, base: path.join(repo, ".cards"), opts };
  board.fetch = async (url, init = {}) => {
    if (opts.refuse) throw new TypeError("fetch failed: ECONNREFUSED 127.0.0.1:4173");
    const u = new URL(url);
    const method = (init.method || "GET").toUpperCase();
    if (method === "POST" && u.pathname === "/api/card") {
      board.posts++;
      const body = JSON.parse(init.body || "{}");
      const id = `card-${++board.n}`;
      const kind = opts.ignoreKind ? "" : body.kind || "";
      const pipeline = opts.ignoreKind ? "plan" : "backlog";
      const dir = path.join(board.base, id);
      await mkdir(dir, { recursive: true });
      board.cards.push({ id, title: body.title, description: body.description, kind, pipeline, dir });
      if (opts.lostAck) throw new TypeError("fetch failed: socket hang up (ack lost)");
      return jsonRes(200, { id, kind });
    }
    if (method === "GET" && u.pathname === "/api/board") {
      return jsonRes(200, {
        cards: board.cards.map((c) => ({
          id: c.id, title: c.title, description: c.description, kind: c.kind, pipeline: c.pipeline,
        })),
      });
    }
    const m = u.pathname.match(/^\/api\/card\/([^/]+)$/);
    if (method === "GET" && m) {
      const card = board.cards.find((c) => c.id === m[1]);
      if (!card || opts.card404) return jsonRes(404, { error: "not found" });
      const absDir = opts.badAbsDir ? path.join(board.base, "gone", card.id) : card.dir;
      return jsonRes(200, { abs_dir: absDir });
    }
    return jsonRes(404, { error: `unhandled ${method} ${u.pathname}` });
  };
  return board;
}

const BOARD_URL = "http://127.0.0.1:4173";
const runDrain = (repo, board, over = {}) =>
  drain({ repoDir: repo, boardUrl: BOARD_URL, fetch: board.fetch, ...over });
const runEnqueue = (repo) => enqueue({ repoDir: repo });
const runSeed = (repo) => seed({ repoDir: repo });
const queueFiles = async (repo) =>
  (await readdir(path.join(repo, "Board Inbox", "queue")).catch(() => [])).filter((f) => f.endsWith(".json"));
const deliveredFiles = async (repo) =>
  (await readdir(path.join(repo, "Board Inbox", "delivered")).catch(() => [])).filter((f) => f.endsWith(".json"));
const readQueue = async (repo) => {
  const out = [];
  for (const f of await queueFiles(repo))
    out.push(JSON.parse(await readFile(path.join(repo, "Board Inbox", "queue", f), "utf8")));
  return out;
};

// Capture console.log + console.error while running fn — several assertions are on
// what the ingest LOGS (the "loud" degradations the plan promises).
async function capturing(fn) {
  const out = [];
  const log = console.log, err = console.error;
  console.log = (...a) => out.push(a.join(" "));
  console.error = (...a) => out.push(a.join(" "));
  try {
    const result = await fn();
    return { result, out: out.join("\n") };
  } finally {
    console.log = log;
    console.error = err;
  }
}

// The 8 files a seed must account for, mirroring the real repo layout.
const SPEC = (title, one) => `---\ntitle: ${title}\none_liner: ${one}\nwhen: 2026-07-14 09:12:03 (America/Sao_Paulo)\n---\n\n# ${title}\n\n${one}\n`;
const PLAN = (title, one, reports = []) =>
  `---\ntitle: ${title}\none_liner: ${one}\nreports:\n${reports.map((r) => `  - _reports/${r}`).join("\n")}\n---\n\n# ${title}\n\n${one}\n`;
async function seedTree(repo) {
  await write(repo, "New Features Plans/feature-calendar-conflict-check.md", SPEC("Calendar conflict check", "warn on overlaps"));
  await write(repo, "New Features Plans/feature-calendar-recurring-events.md", SPEC("Recurring events", "repeat events"));
  await write(repo, "New Features Plans/reminders-followups.md", "# reminders\nnot a feature-*.md file\n");
  await write(repo, "New Features Plans/Self-Leaning-Final-Steps.md", "# notes\nnot a feature-*.md file\n");
  await write(repo, "Bugs and Malfunctions/bugfix-lid-history-blindness.md", PLAN("LID history blindness", "fetch both JIDs"));
  await write(repo, "Bugs and Malfunctions/bugfix-task-false-positive.md", PLAN("Task false positive", "stop over-matching"));
  await write(repo, "Bugs and Malfunctions/_reports/2026-07-12T12-19-23-reported-calendar-action.md", "# seeded report a\n");
  await write(repo, "Bugs and Malfunctions/_reports/2026-07-13T09-43-54-reported-calendar-action.md", "# seeded report b\n");
}

console.log(`\nboard-ingest self-test\n`);

// ============================================================================
//  NOTHING IS CREATED TWICE
// ============================================================================

// ---- 1: the lost ack (edge 4) ------------------------------------------------
console.log("1    the lost ack: reconcile against the board, never re-POST");
{
  const repo = await makeRepo();
  await seedTree(repo);
  await runSeed(repo);
  await write(repo, "New Features Plans/feature-lost-ack-2026-07-14T09-12-03.md", SPEC("Lost ack spec", "the ack goes missing"));
  await runEnqueue(repo);

  const board = makeBoard(repo, { lostAck: true }); // board makes the card, then the ack is lost
  await runDrain(repo, board).catch(() => {}); // the lost ack surfaces as a rejected POST
  const afterFirst = (await readQueue(repo))[0];
  check("after the lost ack the entry is still queued with no cardId", afterFirst && afterFirst.cardId == null);
  check("the board did make the card once", board.cards.length === 1);

  board.opts.lostAck = false; // the network is back; nothing else changed
  const second = await runDrain(repo, board);
  check("the second drain adopts the existing card and POSTs zero more", board.posts === 1 && board.cards.length === 1);
  check("the reconciled entry is delivered", (await deliveredFiles(repo)).length === 1 && (await queueFiles(repo)).length === 0);
  check("drain reports it delivered", Array.isArray(second.delivered) && second.delivered.length === 1);
  await rm(repo, { recursive: true, force: true });
}

// ---- 2: failed copy, then retry (edge 5) -------------------------------------
console.log("\n2    a failed copy is retried — with no second POST");
{
  const repo = await makeRepo();
  await seedTree(repo);
  await runSeed(repo);
  await write(repo, "New Features Plans/feature-copy-fail-2026-07-14T09-13-00.md", SPEC("Copy fail spec", "the copy fails first"));
  await runEnqueue(repo);

  const board = makeBoard(repo, { badAbsDir: true }); // POST works, the copy target is missing
  await runDrain(repo, board);
  const stuck = (await readQueue(repo))[0];
  check("the entry is NOT archived while the copy is failing", (await queueFiles(repo)).length === 1);
  check("but the cardId IS recorded, so the retry reuses it", stuck && stuck.cardId != null);
  check("exactly one POST so far", board.posts === 1);

  board.opts.badAbsDir = false; // the abs_dir resolves now
  await runDrain(repo, board);
  check("the retry copies and archives, with no second POST", board.posts === 1 && (await deliveredFiles(repo)).length === 1);
  await rm(repo, { recursive: true, force: true });
}

// ---- 3: two drains + the lock (edge 21, D4) ----------------------------------
console.log("\n3    the drain lock: a live lock blocks, a dead lock is broken");
{
  const repo = await makeRepo();
  await seedTree(repo);
  await runSeed(repo);
  await write(repo, "New Features Plans/feature-locked-2026-07-14T09-14-00.md", SPEC("Locked spec", "held under a live lock"));
  await runEnqueue(repo);
  const lockPath = path.join(repo, "Board Inbox", ".drain.lock");

  // a live lock (our own PID) -> the drain does nothing and says so.
  await write(repo, "Board Inbox/.drain.lock", JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() }));
  const board = makeBoard(repo);
  const held = await runDrain(repo, board);
  check("a live lock -> {lockHeld:true} and zero POSTs", held.lockHeld === true && board.posts === 0);
  check("the queue is untouched under a held lock", (await queueFiles(repo)).length === 1);

  // a dead lock (a PID that cannot exist) -> broken, and the drain proceeds.
  await write(repo, "Board Inbox/.drain.lock", JSON.stringify({ pid: 2147483646, startedAt: new Date().toISOString() }));
  const board2 = makeBoard(repo);
  await runDrain(repo, board2);
  check("a dead lock is broken and the drain proceeds", board2.posts === 1 && (await deliveredFiles(repo)).length === 1);
  check("the lock is released afterwards", !(await exists(lockPath)));
  await rm(repo, { recursive: true, force: true });
}

// ---- 4: enqueue is idempotent (edge 14) --------------------------------------
console.log("\n4    enqueue is idempotent");
{
  const repo = await makeRepo();
  await seedTree(repo);
  await runSeed(repo);
  await write(repo, "New Features Plans/feature-idem-2026-07-14T09-15-00.md", SPEC("Idempotent spec", "queued exactly once"));
  await runEnqueue(repo);
  const first = await queueFiles(repo);
  await runEnqueue(repo); // same tree, again
  const second = await queueFiles(repo);
  check("a second enqueue over the same tree adds nothing", first.length === 1 && second.length === 1);
  await rm(repo, { recursive: true, force: true });
}

// ---- 5: delivered never resurrects (edges 15, 19) ----------------------------
console.log("\n5    a delivered card never comes back");
{
  const repo = await makeRepo();
  await seedTree(repo);
  await runSeed(repo);
  const src = "New Features Plans/feature-once-2026-07-14T09-16-00.md";
  await write(repo, src, SPEC("Once spec", "delivered exactly once"));
  await runEnqueue(repo);
  const board = makeBoard(repo);
  await runDrain(repo, board);
  check("it delivered once", (await deliveredFiles(repo)).length === 1);

  await write(repo, src, SPEC("Once spec EDITED", "edited after delivery")); // touch the source
  await runEnqueue(repo);
  check("editing a delivered source does not re-queue it", (await queueFiles(repo)).length === 0);

  // edge 19: a 404 mid-drain archives, it does not re-POST.
  await write(repo, "New Features Plans/feature-deleted-2026-07-14T09-17-00.md", SPEC("Deleted-mid-drain spec", "card vanishes"));
  await runEnqueue(repo);
  const board2 = makeBoard(repo, { card404: true });
  await runDrain(repo, board2);
  check("a 404 mid-drain archives the entry rather than re-POSTing", board2.posts === 1 && (await queueFiles(repo)).length === 0);
  await rm(repo, { recursive: true, force: true });
}

// ============================================================================
//  NOTHING IS SILENTLY DROPPED
// ============================================================================

// ---- 6: the ledger seed (edge 16) --------------------------------------------
console.log("\n6    the seed: an unseeded first run must not open cards for the 8 files on disk");
{
  const repo = await makeRepo();
  await seedTree(repo);
  const seeded = await runSeed(repo);
  check("seed accounts for all 8 files already on disk", seeded && seeded.seeded === 8);
  await runEnqueue(repo);
  check("enqueue over a freshly seeded tree opens ZERO cards", (await queueFiles(repo)).length === 0);

  await write(repo, "New Features Plans/feature-new-2026-07-14T09-18-00.md", SPEC("Brand new feature", "a real new spec"));
  await write(repo, "Bugs and Malfunctions/bugfix-new-thing.md", PLAN("Brand new bugfix", "a real new plan"));
  await runEnqueue(repo);
  const q = await readQueue(repo);
  check("exactly the two NEW files are queued", q.length === 2);
  check("their kinds are feature and maintenance", q.some((e) => e.kind === "feature") && q.some((e) => e.kind === "maintenance"));
  check(
    "reminders-followups.md / Self-Leaning-Final-Steps.md never match the enqueue glob",
    !q.some((e) => /reminders-followups|Self-Leaning-Final-Steps/.test(e.source))
  );
  await rm(repo, { recursive: true, force: true });
}

// ---- 7: the interlock --------------------------------------------------------
console.log("\n7    the interlock: enqueue with no ledger throws and queues nothing");
{
  const repo = await makeRepo();
  await seedTree(repo); // files on disk, but NO seed -> no ledger
  let threw = false;
  await runEnqueue(repo).catch(() => { threw = true; });
  check("enqueue refuses to run without a ledger (throws)", threw);
  check("and it queued nothing", (await queueFiles(repo)).length === 0);
  await rm(repo, { recursive: true, force: true });
}

// ---- 8: the owner-report predicate, tested against the REAL generator --------
console.log("\n8    the owner-report predicate: fixtures written by the real captureFailure");
{
  // Point the real capture layer at a temp _reports/ and drive it with no ctx.anthropic,
  // exactly as scripts/selflearning-selftest.mjs does — so there is no network call and the
  // predicate is checked against BYTES THE GENERATOR ACTUALLY WROTE, not a hand-typed string.
  const repo = await makeRepo();
  await seedTree(repo);
  await runSeed(repo);
  process.env.SELF_LEARNING_DIR = path.join(repo, "Bugs and Malfunctions", "_reports");
  const { captureFailure } = await import(
    `../secretary/1. Orchestrator/lib/selflearning.js?bust=${Date.now()}`
  );
  const ctx = { owner: "Marcelo", remoteJid: "5511@s.whatsapp.net", contact: "Marcelo", _turn: {} };

  // (a) an OWNER report that no plan names -> one maintenance card carrying it.
  const ownerPath = await captureFailure(ctx, {
    phase: "reported",
    taskId: "calendar-action",
    report: { note: "you scheduled it at 6pm not 5pm", whatWentWrong: "wrong time", expected: "5pm" },
  });
  check("captureFailure wrote the owner report", !!ownerPath);
  await runEnqueue(repo);
  let q = await readQueue(repo);
  const ownerBase = path.basename(ownerPath);
  check("an unclaimed owner report earns exactly one maintenance card", q.filter((e) => e.source.includes(ownerBase)).length === 1);
  check("that card is kind:maintenance", q.find((e) => e.source.includes(ownerBase))?.kind === "maintenance");

  // (b) the same report, once a plan's reports: header names it -> no card (edge 10).
  const repo2 = await makeRepo();
  await seedTree(repo2);
  await runSeed(repo2);
  process.env.SELF_LEARNING_DIR = path.join(repo2, "Bugs and Malfunctions", "_reports");
  const { captureFailure: cf2 } = await import(`../secretary/1. Orchestrator/lib/selflearning.js?bust=${Date.now()}b`);
  ctx._turn = {}; // each sub-case is an independent turn; reset the rails' per-turn capture guard
  const claimed = await cf2(ctx, {
    phase: "reported", taskId: "calendar-action",
    report: { note: "same complaint", whatWentWrong: "wrong time", expected: "5pm" },
  });
  await write(repo2, "Bugs and Malfunctions/bugfix-claims-it.md", PLAN("Claims the report", "already planned", [path.basename(claimed)]));
  await runEnqueue(repo2);
  q = await readQueue(repo2);
  check("a report a plan already names earns NO card of its own", !q.some((e) => e.source.includes(path.basename(claimed))));

  // (c) a MACHINE report (phase soft) no plan names -> no card (edge 12: the noise filter).
  const repo3 = await makeRepo();
  await seedTree(repo3);
  await runSeed(repo3);
  process.env.SELF_LEARNING_DIR = path.join(repo3, "Bugs and Malfunctions", "_reports");
  const { captureFailure: cf3 } = await import(`../secretary/1. Orchestrator/lib/selflearning.js?bust=${Date.now()}c`);
  ctx._turn = {}; // each sub-case is an independent turn; reset the rails' per-turn capture guard
  const machine = await cf3(ctx, { phase: "soft", taskId: "calendar-action", softMessage: "I couldn't do it" });
  await runEnqueue(repo3);
  q = await readQueue(repo3);
  check("a machine report no plan names earns NO card", !machine || !q.some((e) => e.source.includes(path.basename(machine))));

  delete process.env.SELF_LEARNING_DIR;
  await rm(repo, { recursive: true, force: true });
  await rm(repo2, { recursive: true, force: true });
  await rm(repo3, { recursive: true, force: true });
}

// ---- 9: board down (edge 1) --------------------------------------------------
console.log("\n9    a down board is not an error");
{
  const repo = await makeRepo();
  await seedTree(repo);
  await runSeed(repo);
  await write(repo, "New Features Plans/feature-down-2026-07-14T09-19-00.md", SPEC("Board down spec", "the board is off"));
  await runEnqueue(repo);
  const board = makeBoard(repo, { refuse: true });
  const res = await runDrain(repo, board);
  check("a refused board -> boardDown:true", res.boardDown === true);
  check("the entry stays queued", (await queueFiles(repo)).length === 1);
  check("no card was created", board.posts === 0);
  await rm(repo, { recursive: true, force: true });
}

// ---- 10: titles (edges 9, 13) ------------------------------------------------
console.log("\n10   titles: a spec never lands as null/'feature'; a header-less plan logs");
{
  const repo = await makeRepo();
  await seedTree(repo);
  await runSeed(repo);
  // a spec with no title and no one_liner at all.
  await write(repo, "New Features Plans/feature-untitled-2026-07-14T09-20-00.md", "no header here, just prose\n");
  // a plan with no frontmatter header — only a # H1.
  await write(repo, "Bugs and Malfunctions/bugfix-headerless.md", "# The H1 title\n\nsome plan body\n");
  const { out } = await capturing(() => runEnqueue(repo));
  const q = await readQueue(repo);
  const spec = q.find((e) => e.source.includes("feature-untitled"));
  check("a title-less spec falls back to 'Feature request (...)' — never null", spec && spec.title && /^Feature request \(/.test(spec.title));
  check("a title-less spec is never titled 'feature'", spec && spec.title !== "feature");
  const plan = q.find((e) => e.source.includes("bugfix-headerless"));
  check("a header-less plan falls back to its # H1", plan && plan.title === "The H1 title");
  check("...and the missing header is logged loudly", /no header/i.test(out));
  await rm(repo, { recursive: true, force: true });
}

// ---- 11: the footer ----------------------------------------------------------
console.log("\n11   the created card's description ends with the exact source: footer");
{
  const repo = await makeRepo();
  await seedTree(repo);
  await runSeed(repo);
  const base = "feature-footer-2026-07-14T09-21-00.md";
  await write(repo, `New Features Plans/${base}`, SPEC("Footer spec", "check the footer"));
  await runEnqueue(repo);
  const board = makeBoard(repo);
  await runDrain(repo, board);
  const desc = board.cards[0]?.description || "";
  check("description's last line is `source: <basename>`", desc.split("\n").pop() === `source: ${base}`);
  await rm(repo, { recursive: true, force: true });
}

// ---- 12: the wrong-board tripwire (drain step b2) ----------------------------
console.log("\n12   the wrong-board tripwire: a board that ignores kind is caught, not trusted");
{
  const repo = await makeRepo();
  await seedTree(repo);
  await runSeed(repo);
  await write(repo, "New Features Plans/feature-tripwire-2026-07-14T09-22-00.md", SPEC("Tripwire spec", "wrong board version"));
  await runEnqueue(repo);
  const board = makeBoard(repo, { ignoreKind: true }); // files into a pipeline, drops kind
  const { result, out } = await capturing(() => runDrain(repo, board));
  check("it STILL delivers exactly one card", board.posts === 1 && (await deliveredFiles(repo)).length === 1);
  check(
    "and it reports the mismatch loudly (return flag or a logged warning)",
    result?.wrongBoard === true || /did not accept the 'kind'|not the board this feature|mis-typed/i.test(out)
  );
  await rm(repo, { recursive: true, force: true });
}

// ---- also exercise parseHeader directly --------------------------------------
console.log("\n+    parseHeader reads the frontmatter the producers write");
{
  const h = parseHeader(PLAN("A title", "a one liner", ["r1.md", "r2.md"]));
  check("parseHeader extracts title/one_liner", h && h.title === "A title" && h.one_liner === "a one liner");
  check("parseHeader extracts the reports list", h && Array.isArray(h.reports) && h.reports.length === 2);
  check("parseHeader.had is true when a header is present", h && h.had === true);
  const none = parseHeader("# just an H1\nno frontmatter\n");
  check("parseHeader.had is false with no frontmatter", none && none.had === false);
}

console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures})`}\n`);
process.exit(failures === 0 ? 0 : 1);
