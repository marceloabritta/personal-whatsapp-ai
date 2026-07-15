#!/usr/bin/env node
// ============================================================================
//  Self-test for the restructured droplet pull — scripts/self-learning-pull.sh.
//
//  It runs the REAL self-learning-pull.sh in a subprocess (a verbatim copy relocated into
//  a temp repo, so the script's own REPO/dest paths resolve INTO the fixture and never
//  touch the working tree — but the logic under test is the live script, byte for byte,
//  whatever version is on disk). ssh and rsync are STUBBED on PATH: tiny shell scripts
//  that read a fixture "droplet" from disk, log every invocation, and can be told to fail
//  or to drop a file into the spool mid-transfer. Offline by construction — there is no
//  droplet.
//
//  It exists to prove two things the current script gets wrong, and to lock in one rule:
//
//    1. THE BLIND ARCHIVE IS GONE (edge 18). A report written into the spool DURING the
//       pull must NOT be archived — it was never transferred. Today's `mv *.md _synced/`
//       (self-learning-pull.sh:38) sweeps it into _synced/ and it is lost, unreported.
//       -> against the LIVE script this test FAILS, which is the whole point.
//    2. NOTHING IS ARCHIVED WHEN THE TRANSFER FAILS. A failed rsync issues no archive mv.
//    3. THE FUNNELS ARE INDEPENDENT. An empty (or failing) report spool must not stop the
//       spec spool's pull, and vice-versa. Today's empty-spool `exit 0` (lines 27-31)
//       makes this FAIL.
//    4. `--remove-source-files` IS NEVER PASSED — it deletes the droplet's only copy. A
//       grep over every recorded rsync argv. The tripwire for a tempting "simplification".
//
//  Run:  node scripts/pull-archive-selftest.mjs
// ============================================================================
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm, cp, chmod } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const REAL_SCRIPT = fileURLToPath(new URL("./self-learning-pull.sh", import.meta.url));

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// The droplet spool paths the script hard-codes; the stubs remap /opt/secretary into the
// fixture. improvements/ is the reports funnel, specs/ is the (new) feature-spec funnel.
const REPORTS_REMOTE = "opt/secretary/improvements";
const SPECS_REMOTE = "opt/secretary/specs";

// ---- the stubs (written into a temp dir prepended to PATH) --------------------
const SSH_STUB = `#!/usr/bin/env bash
# stub ssh: log the remote command, then run it locally with /opt/secretary remapped into
# the fixture droplet. stdin is forwarded (the archive step pipes filenames to xargs).
set -uo pipefail
cmd="\${@: -1}"
printf 'SSH %s\\n' "$cmd" >> "$STUB_LOG"
remapped="$(printf '%s' "$cmd" | sed "s#/opt/secretary#\${STUB_DROPLET_ROOT}/opt/secretary#g")"
bash -c "$remapped"
`;

const RSYNC_STUB = `#!/usr/bin/env bash
# stub rsync: log argv, capture the transfer list BEFORE any mid-pull write, optionally
# fail, optionally drop a new file into the spool mid-transfer, then copy the captured
# list into the destination. No arrays — must run under macOS bash 3.2.
set -uo pipefail
printf 'RSYNC %s\\n' "$*" >> "$STUB_LOG"

dest="\${@: -1}"
usestdin=0
src=""
for a in "$@"; do
  [ "$a" = "--files-from=-" ] && usestdin=1
  case "$a" in *:*) src="$a" ;; esac
done
srcpath="\${src#*:}"
srcpath="$(printf '%s' "$srcpath" | sed "s#/opt/secretary#\${STUB_DROPLET_ROOT}/opt/secretary#g")"

# capture the list to transfer, BEFORE the mid-pull write happens
list=""
if [ "$usestdin" = "1" ]; then
  while IFS= read -r n; do [ -n "$n" ] && list="\${list}\${n}\\n"; done
else
  for f in $srcpath; do [ -e "$f" ] && list="\${list}\${f}\\n"; done
fi

# a report written into the spool mid-transfer (edge 18) — NOT in the captured list
if [ -n "\${STUB_RSYNC_MIDPULL_NAME:-}" ]; then
  case "$srcpath" in
    *"\${STUB_RSYNC_MIDPULL_MATCH:-improvements}"*)
      spooldir="$(printf '%s' "$srcpath" | sed 's#/\\*\\.md$##; s#/$##')"
      : > "\${spooldir}/\${STUB_RSYNC_MIDPULL_NAME}"
      ;;
  esac
fi

# optional stubbed failure (all, or only when srcpath contains the given token)
if [ -n "\${STUB_RSYNC_FAIL:-}" ]; then
  if [ "$STUB_RSYNC_FAIL" = "all" ]; then
    printf 'rsync: stubbed failure\\n' >&2; exit 1
  fi
  case "$srcpath" in *"$STUB_RSYNC_FAIL"*) printf 'rsync: stubbed failure\\n' >&2; exit 1 ;; esac
fi

mkdir -p "$dest"
base="\${srcpath%/}"
printf '%b' "$list" | while IFS= read -r item; do
  [ -z "$item" ] && continue
  if [ "$usestdin" = "1" ]; then cp "\${base}/\${item}" "$dest/" 2>/dev/null || true
  else cp "$item" "$dest/" 2>/dev/null || true
  fi
done
`;

// ---- fixture + runner --------------------------------------------------------
async function setup({ reports = [], specs = [] } = {}) {
  const root = await mkdtemp(path.join(tmpdir(), "pull-archive-"));
  const repo = path.join(root, "repo");
  const droplet = path.join(root, "droplet");
  const bin = path.join(root, "bin");
  const log = path.join(root, "log");

  await mkdir(path.join(repo, "scripts"), { recursive: true });
  await mkdir(path.join(repo, "Bugs and Malfunctions", "inbox"), { recursive: true });
  await mkdir(path.join(repo, "New Features Plans"), { recursive: true });
  await mkdir(path.join(droplet, REPORTS_REMOTE), { recursive: true });
  await mkdir(path.join(droplet, SPECS_REMOTE), { recursive: true });
  await mkdir(bin, { recursive: true });
  await writeFile(log, "", "utf8");

  // the LIVE script, verbatim, relocated so its BASH_SOURCE/.. resolves into the fixture
  await cp(REAL_SCRIPT, path.join(repo, "scripts", "self-learning-pull.sh"));

  for (const f of reports) await writeFile(path.join(droplet, REPORTS_REMOTE, f), `# ${f}\n`, "utf8");
  for (const f of specs) await writeFile(path.join(droplet, SPECS_REMOTE, f), `# ${f}\n`, "utf8");

  await writeFile(path.join(bin, "ssh"), SSH_STUB, "utf8");
  await writeFile(path.join(bin, "rsync"), RSYNC_STUB, "utf8");
  await chmod(path.join(bin, "ssh"), 0o755);
  await chmod(path.join(bin, "rsync"), 0o755);

  return { root, repo, droplet, bin, log };
}

function run(fx, env = {}) {
  const res = spawnSync("bash", [path.join(fx.repo, "scripts", "self-learning-pull.sh")], {
    encoding: "utf8",
    env: {
      ...process.env,
      PATH: `${fx.bin}:${process.env.PATH}`,
      STUB_DROPLET_ROOT: fx.droplet,
      STUB_LOG: fx.log,
      ...env,
    },
  });
  return res;
}
const logText = (fx) => readFile(fx.log, "utf8").catch(() => "");
const ls = (dir) => readdir(dir).catch(() => []);
const REPORTS = (fx) => path.join(fx.droplet, REPORTS_REMOTE);
const SPECS = (fx) => path.join(fx.droplet, SPECS_REMOTE);
const INBOX = (fx) => path.join(fx.repo, "Bugs and Malfunctions", "inbox");
const SPEC_DEST = (fx) => path.join(fx.repo, "New Features Plans");

const allRsyncLines = [];

console.log(`\npull-archive self-test  (real script: ${REAL_SCRIPT})\n`);

// ---- 1: the blind archive is gone (edge 18) ----------------------------------
console.log("1    a file written mid-pull is NOT archived (the silent-drop fix)");
{
  const fx = await setup({ reports: ["report-a.md", "report-b.md"] });
  run(fx, { STUB_RSYNC_MIDPULL_NAME: "report-midpull.md", STUB_RSYNC_MIDPULL_MATCH: "improvements" });
  const log = await logText(fx);
  allRsyncLines.push(...log.split("\n").filter((l) => l.startsWith("RSYNC")));

  const spoolNow = await ls(REPORTS(fx));
  const synced = await ls(path.join(REPORTS(fx), "_synced"));
  check(
    "the mid-pull file stays in the spool, unarchived (re-pulled next run)",
    spoolNow.includes("report-midpull.md") && !synced.includes("report-midpull.md")
  );
  check(
    "_synced/ holds exactly the two names captured before the transfer",
    synced.length === 2 && synced.includes("report-a.md") && synced.includes("report-b.md")
  );
  check(
    "no archive step ever runs a blind `mv *.md`",
    !/SSH .*mv[^\\n]*\*\.md/.test(log)
  );
  await rm(fx.root, { recursive: true, force: true });
}

// ---- 2: nothing archived when the transfer fails -----------------------------
console.log("\n2    a failed transfer archives nothing and exits non-zero");
{
  const fx = await setup({ reports: ["report-a.md", "report-b.md"] });
  const res = run(fx, { STUB_RSYNC_FAIL: "all" });
  const log = await logText(fx);
  allRsyncLines.push(...log.split("\n").filter((l) => l.startsWith("RSYNC")));
  const synced = await ls(path.join(REPORTS(fx), "_synced"));
  check("no archive mv was issued for the reports spool", !/SSH .*improvements.*mv/.test(log) && synced.length === 0);
  check("the run exits non-zero when a pull fails", res.status !== 0);
  await rm(fx.root, { recursive: true, force: true });
}

// ---- 3: the funnels are independent ------------------------------------------
console.log("\n3    an empty or failing spool never stops the other funnel");
{
  // 3a: reports spool EMPTY, specs spool NON-EMPTY -> specs must still be pulled.
  const fx1 = await setup({ reports: [], specs: ["feature-x-2026-07-14T09-12-03.md"] });
  run(fx1);
  allRsyncLines.push(...(await logText(fx1)).split("\n").filter((l) => l.startsWith("RSYNC")));
  check("an empty report spool does not stop the spec pull", (await ls(SPEC_DEST(fx1))).includes("feature-x-2026-07-14T09-12-03.md"));
  await rm(fx1.root, { recursive: true, force: true });

  // 3b: specs spool EMPTY, reports spool NON-EMPTY -> reports must still be pulled.
  const fx2 = await setup({ reports: ["report-c.md"], specs: [] });
  run(fx2);
  allRsyncLines.push(...(await logText(fx2)).split("\n").filter((l) => l.startsWith("RSYNC")));
  check("an empty spec spool does not stop the report pull", (await ls(INBOX(fx2))).includes("report-c.md"));
  await rm(fx2.root, { recursive: true, force: true });

  // 3c: a FAILING report pull must still let the spec pull run.
  const fx3 = await setup({ reports: ["report-d.md"], specs: ["feature-y-2026-07-14T10-00-00.md"] });
  run(fx3, { STUB_RSYNC_FAIL: "improvements" });
  allRsyncLines.push(...(await logText(fx3)).split("\n").filter((l) => l.startsWith("RSYNC")));
  check("a failing report pull still lets the spec pull run", (await ls(SPEC_DEST(fx3))).includes("feature-y-2026-07-14T10-00-00.md"));
  await rm(fx3.root, { recursive: true, force: true });
}

// ---- 4: --remove-source-files is never passed --------------------------------
console.log("\n4    rsync is never invoked with --remove-source-files");
{
  check(
    "no recorded rsync argv contains --remove-source-files",
    allRsyncLines.length > 0 && !allRsyncLines.some((l) => l.includes("--remove-source-files"))
  );
}

console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures})`}\n`);
process.exit(failures === 0 ? 0 : 1);
