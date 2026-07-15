#!/usr/bin/env node
// ============================================================================
//  board-ingest.mjs — turn spooled feature specs and triaged bugfix plans into
//  cards on the kanban BACKLOG, exactly once, with nothing silently dropped.
//
//  Three deterministic sub-commands (no model call, no dependency):
//    seed     — account for everything already on disk BEFORE the first enqueue, so
//               nothing that predates this feature ever becomes a card. Writes
//               `Board Inbox/ledger.tsv` (tracked in git). Refuses to run twice.
//    enqueue  — scan the two funnels, and write one `Board Inbox/queue/<basename>.json`
//               per NEW file (skipping anything already in the ledger). Hard interlock:
//               refuses to run at all if the ledger is missing (that mistake opens a card
//               for every file already on disk).
//    drain    — under a single-flight lock, POST each queued entry to the board's
//               HTTP API as a typed backlog card, copy the source into the card's
//               folder, and archive the entry to `delivered/`. Idempotent and
//               crash-safe: the board is consumed over its existing endpoints only.
//
//  The board (AI Coding-kanban/) is a VENDORED system folder that ./update.sh replaces
//  wholesale — so this ingest is coded against its HTTP API by endpoint, never by
//  internals, and the drain verifies at runtime that the card it created landed
//  unrouted and typed (the "wrong-board tripwire", step b2). If the API drifts, the
//  drain fails loudly and RETAINS the queue; it never silently drops.
//
//  CLI:  node scripts/board-ingest.mjs <seed|enqueue|drain>
//  Every function is also exported so scripts/board-ingest-selftest.mjs can drive it
//  in-process against a temp repo and a stub board.
// ============================================================================
import { mkdir, writeFile, readFile, readdir, rename, unlink, copyFile, stat, appendFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const BOARD_TIMEOUT_MS = 30_000; // per board request — global fetch has NO default timeout
const DRAIN_RUN_MS = 30 * 60_000; // run-wide wall-clock ceiling, well under the 60-min stale-lock backstop

// ---- small helpers -----------------------------------------------------------
const exists = async (p) => !!(await stat(p).catch(() => null));
const listDir = async (dir) => (await readdir(dir).catch(() => []));

function defaultRepoDir() {
  return path.resolve(__dirname, "..");
}
function defaultBoardUrl() {
  const host = process.env.MANAGER_HOST || "127.0.0.1";
  const port = process.env.MANAGER_PORT || "4173";
  return `http://${host}:${port}`;
}

function inboxPaths(repoDir) {
  const inbox = path.join(repoDir, "Board Inbox");
  return {
    inbox,
    ledger: path.join(inbox, "ledger.tsv"),
    queueDir: path.join(inbox, "queue"),
    deliveredDir: path.join(inbox, "delivered"),
    lockPath: path.join(inbox, ".drain.lock"),
  };
}

// ---- the header the two producers write (D2: YAML-shaped frontmatter) --------
// One line-based parser, no YAML dependency. `collapse` guards against a multi-line
// title breaking the line-based parse (the producers already collapse newlines).
export function parseHeader(text) {
  const result = { title: "", one_liner: "", reports: [], had: false };
  const lines = String(text || "").split(/\r?\n/);
  if (lines[0]?.trim() !== "---") return result;
  result.had = true;
  let inReports = false;
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim() === "---") break;
    const listMatch = line.match(/^\s*-\s+(.*\S)\s*$/);
    if (inReports && listMatch) {
      result.reports.push(listMatch[1].trim());
      continue;
    }
    const kv = line.match(/^([A-Za-z_]+):\s*(.*)$/);
    if (!kv) continue;
    const key = kv[1].toLowerCase();
    const val = kv[2].trim();
    if (key === "reports") {
      inReports = true;
      if (val) result.reports.push(val);
      continue;
    }
    inReports = false;
    if (key === "title") result.title = val;
    else if (key === "one_liner") result.one_liner = val;
  }
  return result;
}

function firstHeading(text) {
  const m = String(text || "").match(/^#\s+(.*\S)\s*$/m);
  return m ? m[1].trim() : "";
}

// The generator writes `| Trigger | reported |` and, for an owner report,
// `| Source | **OWNER-REPORTED** (human-verified) |` (selflearning.js render). Either
// row, tolerant of column padding, means owner-reported. NEVER grep `Source: OWNER-REPORTED`
// — that string exists in no report (it is prose in triage-failures.md).
function isOwnerReported(text) {
  return (
    /^\|\s*Trigger\s*\|\s*reported\s*\|/mi.test(text) ||
    /^\|\s*Source\s*\|.*OWNER-REPORTED/mi.test(text)
  );
}

// ---- the ledger (tracked in git; membership is tested on the PATH column) ----
async function readLedger(ledgerPath) {
  const map = new Map(); // repo-relative path -> why
  const raw = await readFile(ledgerPath, "utf8").catch(() => null);
  if (raw == null) return { exists: false, map };
  for (const line of raw.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const cols = line.split("\t");
    if (cols.length >= 3) map.set(cols[1], cols[2]);
  }
  return { exists: true, map };
}
async function appendLedger(ledgerPath, relPath, why) {
  await appendFile(ledgerPath, `${new Date().toISOString()}\t${relPath}\t${why}\n`, "utf8");
}

// ============================================================================
//  seed — account for everything already on disk. Idempotent (refuses if the
//  ledger already exists). Run ONCE, by hand, before the first enqueue.
// ============================================================================
export async function seed(opts = {}) {
  const repoDir = opts.repoDir || defaultRepoDir();
  const { inbox, ledger } = inboxPaths(repoDir);
  await mkdir(inbox, { recursive: true });

  if (await exists(ledger)) {
    console.log("board-ingest: ledger.tsv already exists — seed refuses to run (idempotent). Nothing changed.");
    return { seeded: 0, skipped: true };
  }

  const files = [];
  // Deliberately WIDER than the enqueue glob: every *.md under New Features Plans/ is
  // accounted for, so plan docs / notes can never later be reconsidered as candidates.
  for (const f of await listDir(path.join(repoDir, "New Features Plans")))
    if (f.endsWith(".md")) files.push(path.join("New Features Plans", f));
  for (const f of await listDir(path.join(repoDir, "Bugs and Malfunctions")))
    if (/^bugfix-.*\.md$/.test(f)) files.push(path.join("Bugs and Malfunctions", f));
  for (const f of await listDir(path.join(repoDir, "Bugs and Malfunctions", "_reports")))
    if (f.endsWith(".md")) files.push(path.join("Bugs and Malfunctions", "_reports", f));

  for (const rel of files) await appendLedger(ledger, rel, "seed");
  console.log(`board-ingest: seeded ${files.length} file(s) already on disk into the ledger.`);
  return { seeded: files.length };
}

// ============================================================================
//  enqueue — write one queue entry per NEW file. Interlock: throws if unseeded.
// ============================================================================
export async function enqueue(opts = {}) {
  const repoDir = opts.repoDir || defaultRepoDir();
  const { inbox, ledger, queueDir, deliveredDir } = inboxPaths(repoDir);

  const { exists: seeded, map: ledgerMap } = await readLedger(ledger);
  if (!seeded) {
    // An absent ledger must NEVER be read as an empty one — that opens a card for every
    // file already on disk (the six-unwanted-cards bug). Refuse loudly.
    throw new Error("ledger missing — run 'node scripts/board-ingest.mjs seed' first");
  }

  await mkdir(queueDir, { recursive: true });
  const queued = [];

  // A file ledgered `enqueued` but with neither a queue entry nor a delivered record was
  // lost to a crash mid-enqueue (ledger appended, queue write never happened). Say so —
  // it is one `+ New card` away, and the design prefers a missed card to a double one.
  for (const [rel, why] of ledgerMap) {
    if (why !== "enqueued") continue;
    const base = path.basename(rel);
    if (!(await exists(path.join(queueDir, `${base}.json`))) && !(await exists(path.join(deliveredDir, `${base}.json`))))
      console.error(`board-ingest: ${rel} was ledgered but has no queue entry — a crash may have lost the card; re-add it by hand if needed`);
  }

  const enqueueOne = async (rel, entry) => {
    await appendLedger(ledger, rel, "enqueued"); // ledger BEFORE the queue write (crash -> miss, never dupe)
    await writeFile(path.join(queueDir, `${entry.basename}.json`), JSON.stringify(entry, null, 2), "utf8");
    queued.push(entry.basename);
  };

  // ---- 1. feature specs: New Features Plans/feature-*.md -> kind:feature -----
  for (const f of await listDir(path.join(repoDir, "New Features Plans"))) {
    if (!/^feature-.*\.md$/.test(f)) continue;
    const rel = path.join("New Features Plans", f);
    if (ledgerMap.has(rel)) continue;
    const abs = path.join(repoDir, rel);
    const text = await readFile(abs, "utf8").catch(() => "");
    const h = parseHeader(text);
    let title = h.title || h.one_liner;
    if (!title) {
      const st = await stat(abs).catch(() => null);
      const day = (st ? new Date(st.mtimeMs) : new Date()).toISOString().slice(0, 10);
      title = `Feature request (${day})`; // never null, never "feature"
    }
    await enqueueOne(rel, {
      source: rel,
      basename: f,
      title,
      one_liner: h.one_liner || "",
      kind: "feature",
      cardId: null,
      queuedAt: new Date().toISOString(),
    });
  }

  // ---- 2. bugfix plans: Bugs and Malfunctions/bugfix-*.md -> kind:maintenance -
  for (const f of await listDir(path.join(repoDir, "Bugs and Malfunctions"))) {
    if (!/^bugfix-.*\.md$/.test(f)) continue;
    const rel = path.join("Bugs and Malfunctions", f);
    if (ledgerMap.has(rel)) continue;
    const text = await readFile(path.join(repoDir, rel), "utf8").catch(() => "");
    const h = parseHeader(text);
    let title = h.title;
    if (!title) {
      title = firstHeading(text) || `Bugfix (${f.replace(/\.md$/, "")})`;
      console.error(`board-ingest: ${f} has no header — falling back to # H1`);
    }
    await enqueueOne(rel, {
      source: rel,
      basename: f,
      title,
      one_liner: h.one_liner || "",
      kind: "maintenance",
      cardId: null,
      queuedAt: new Date().toISOString(),
    });
  }

  // ---- 3. owner-reported failures no plan claims: _reports/*.md -> maintenance -
  // The set of reports any plan already names (by basename), parsed fresh each run so a
  // report a plan claims can never earn a card of its own (edge 10).
  const planned = new Set();
  for (const f of await listDir(path.join(repoDir, "Bugs and Malfunctions"))) {
    if (!/^bugfix-.*\.md$/.test(f)) continue;
    const text = await readFile(path.join(repoDir, "Bugs and Malfunctions", f), "utf8").catch(() => "");
    for (const r of parseHeader(text).reports) planned.add(path.basename(r));
  }
  for (const f of await listDir(path.join(repoDir, "Bugs and Malfunctions", "_reports"))) {
    if (!f.endsWith(".md")) continue;
    const rel = path.join("Bugs and Malfunctions", "_reports", f);
    if (ledgerMap.has(rel)) continue;
    if (planned.has(f)) {
      // Named by a plan -> accounted for, never reconsidered (keeps edge 11 stable).
      await appendLedger(ledger, rel, "planned");
      continue;
    }
    const text = await readFile(path.join(repoDir, rel), "utf8").catch(() => "");
    if (!isOwnerReported(text)) continue; // a machine report no plan names stays a no-op forever (edge 12)
    const olMatch = text.match(/\*\*What the owner says went wrong:\*\*\s*(.*\S)/);
    await enqueueOne(rel, {
      source: rel,
      basename: f,
      title: firstHeading(text) || `Owner report (${f.replace(/\.md$/, "")})`,
      one_liner: olMatch ? olMatch[1].trim() : "",
      kind: "maintenance",
      cardId: null,
      queuedAt: new Date().toISOString(),
    });
  }

  return { queued };
}

// ============================================================================
//  drain — deliver every queued entry to the board, exactly once, under a lock.
// ============================================================================
function boardFetch(fetchFn, url, init = {}) {
  return fetchFn(url, { ...init, signal: AbortSignal.timeout(BOARD_TIMEOUT_MS) });
}

function descHasSource(description, basename) {
  return String(description || "").split(/\r?\n/).includes(`source: ${basename}`);
}

// Exclusive-create lockfile carrying the holder's PID (D4). A lock whose PID is dead
// (or whose mtime is > 60 min old) is broken LOUDLY and re-taken once — a drain that
// died holding the lock must not un-drain the queue forever.
async function acquireLock(lockPath) {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      await writeFile(lockPath, JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() }), { flag: "wx" });
      return true;
    } catch (e) {
      if (e?.code !== "EEXIST") throw e;
      let stale = false;
      let holderPid = null;
      try {
        const raw = JSON.parse(await readFile(lockPath, "utf8"));
        holderPid = raw.pid;
        let alive = false;
        try {
          process.kill(holderPid, 0); // no signal sent — just a liveness probe
          alive = true;
        } catch (ke) {
          alive = ke?.code === "EPERM"; // exists but owned by another user
        }
        const st = await stat(lockPath).catch(() => null);
        const ageMs = st ? Date.now() - st.mtimeMs : Infinity;
        if (!alive || ageMs > 60 * 60_000) stale = true;
      } catch {
        stale = true; // an unreadable lock is a broken lock
      }
      if (stale && attempt === 0) {
        console.error(`board-ingest: stale lock from pid ${holderPid} — breaking it`);
        await unlink(lockPath).catch(() => {});
        continue; // retry once
      }
      return false; // a live holder — a held lock is a success, not an error
    }
  }
  return false;
}

export async function drain(opts = {}) {
  const repoDir = opts.repoDir || defaultRepoDir();
  const boardUrl = opts.boardUrl || defaultBoardUrl();
  const fetchFn = opts.fetch || globalThis.fetch;
  const { inbox, queueDir, deliveredDir, lockPath } = inboxPaths(repoDir);

  const result = { delivered: [], waiting: 0, boardDown: false, lockHeld: false, wrongBoard: false, errored: false };
  await mkdir(inbox, { recursive: true });

  if (!(await acquireLock(lockPath))) {
    console.log("board-ingest: another drain holds the lock — nothing to do");
    result.lockHeld = true;
    return result;
  }

  try {
    // 1. read the queue
    const files = (await listDir(queueDir)).filter((f) => f.endsWith(".json")).sort();
    if (files.length === 0) return result; // silent no-op
    const entries = [];
    for (const f of files) {
      try {
        entries.push({ file: path.join(queueDir, f), data: JSON.parse(await readFile(path.join(queueDir, f), "utf8")) });
      } catch (e) {
        console.error(`board-ingest: could not read queue entry ${f}: ${e?.message || e}`);
      }
    }

    // 2. snapshot the board (used for reconcile). A down / refused / slow board is not an error.
    let cards = [];
    try {
      const res = await boardFetch(fetchFn, `${boardUrl}/api/board`);
      const body = await res.json();
      cards = (body && body.cards) || [];
    } catch (e) {
      console.log(`board-ingest: board not running — ${entries.length} card(s) waiting`);
      result.boardDown = true;
      result.waiting = entries.length;
      return result;
    }

    const persist = (e) => writeFile(e.file, JSON.stringify(e.data, null, 2), "utf8");
    const archive = async (e) => {
      await mkdir(deliveredDir, { recursive: true });
      await rename(e.file, path.join(deliveredDir, path.basename(e.file)));
    };

    // deliver one entry: reconcile -> create -> copy -> confirm -> archive.
    const deliverOne = async (e) => {
      const entry = e.data;
      const basename = entry.basename;
      const srcAbs = path.join(repoDir, entry.source);
      let wrongBoard = false;
      let created = null;

      // a. reconcile a lost ack against the board's own record (edge 4).
      if (entry.cardId == null) {
        const match = cards.find((c) => descHasSource(c.description, basename));
        if (match) {
          entry.cardId = match.id;
          await persist(e);
        }
      }

      // b. create — no id yet. The id is written to disk the instant POST returns (edge 5).
      if (entry.cardId == null) {
        const description = `${entry.one_liner || ""}\n\nsource: ${basename}`;
        const res = await boardFetch(fetchFn, `${boardUrl}/api/card`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ title: entry.title, description, kind: entry.kind }),
        });
        if (!res.ok) throw new Error(`POST /api/card -> ${res.status}`);
        const body = await res.json();
        entry.cardId = body.id;
        await persist(e);
        created = body.id;
        // b2. the wrong-board tripwire — verify the board honoured the kind we sent.
        if (body.kind == null || body.kind !== entry.kind) {
          wrongBoard = true;
          console.error(
            `board-ingest: this board did not accept the 'kind' we sent — it is not the board this feature was built against (card ${body.id} may be mis-typed and mis-filed)`
          );
        }
      }

      // c. copy the source into the card's folder.
      const cardRes = await boardFetch(fetchFn, `${boardUrl}/api/card/${entry.cardId}`);
      if (cardRes.status === 404) {
        // the card was deleted mid-drain -> archive, never re-POST (edge 19).
        await archive(e);
        return { delivered: true, wrongBoard, created };
      }
      if (!cardRes.ok) throw new Error(`GET /api/card/${entry.cardId} -> ${cardRes.status}`);
      const absDir = (await cardRes.json()).abs_dir;
      // Do NOT mkdir absDir: a missing folder is a failed copy that must retry, not be papered over.
      await copyFile(srcAbs, path.join(absDir, basename));

      // d. confirm (the card folder may have moved) then archive.
      let absDir2 = absDir;
      const conf = await boardFetch(fetchFn, `${boardUrl}/api/card/${entry.cardId}`);
      if (conf.ok) absDir2 = (await conf.json()).abs_dir;
      let present = await exists(path.join(absDir2, basename));
      if (!present) {
        await copyFile(srcAbs, path.join(absDir2, basename)).catch(() => {});
        present = await exists(path.join(absDir2, basename));
      }
      if (present) {
        await archive(e);
        return { delivered: true, wrongBoard, created };
      }
      return { delivered: false, wrongBoard, created }; // leave queued; the retry reuses cardId
    };

    // 3. per entry, bounded by the run-wide ceiling.
    const start = Date.now();
    const createdIds = [];
    for (let i = 0; i < entries.length; i++) {
      if (Date.now() - start >= DRAIN_RUN_MS) {
        console.log(`board-ingest: run budget reached — ${entries.length - i} entrie(s) left for next tick`);
        break;
      }
      try {
        const r = await deliverOne(entries[i]);
        if (r.delivered) result.delivered.push(entries[i].data.basename);
        if (r.wrongBoard) result.wrongBoard = true;
        if (r.created) createdIds.push(r.created);
      } catch (err) {
        result.errored = true;
        console.error(`board-ingest: error draining ${entries[i].data.basename}: ${err?.message || err} — left queued for the next tick`);
      }
    }

    // b2 (continued) — re-check that the cards we created landed in the backlog. Best-effort.
    if (createdIds.length) {
      try {
        const res = await boardFetch(fetchFn, `${boardUrl}/api/board`);
        const body = await res.json();
        for (const c of (body && body.cards) || []) {
          if (createdIds.includes(c.id) && c.pipeline && c.pipeline !== "backlog") {
            result.wrongBoard = true;
            console.error(
              `board-ingest: card ${c.id} did not land in the backlog (pipeline=${c.pipeline}) — the board is not the version this feature was built against`
            );
          }
        }
      } catch {
        /* best-effort — the delivery already happened */
      }
    }

    result.waiting = (await listDir(queueDir)).filter((f) => f.endsWith(".json")).length;
    return result;
  } finally {
    await unlink(lockPath).catch(() => {});
  }
}

// ---- CLI ---------------------------------------------------------------------
const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const cmd = process.argv[2];
  const repoDir = defaultRepoDir();
  if (cmd === "seed") {
    await seed({ repoDir });
    process.exit(0);
  } else if (cmd === "enqueue") {
    try {
      const { queued } = await enqueue({ repoDir });
      console.log(`board-ingest: enqueued ${queued.length} new card(s).`);
      process.exit(0);
    } catch (e) {
      console.error(`board-ingest: ${e?.message || e}`);
      process.exit(1);
    }
  } else if (cmd === "drain") {
    const r = await drain({ repoDir });
    const bad = !r.boardDown && !r.lockHeld && r.errored;
    console.log(`board-ingest: delivered ${r.delivered.length}, ${r.waiting} waiting${r.boardDown ? " (board down)" : ""}${r.lockHeld ? " (locked)" : ""}.`);
    process.exit(bad ? 1 : 0);
  } else {
    console.error("usage: node scripts/board-ingest.mjs <seed|enqueue|drain>");
    process.exit(2);
  }
}
