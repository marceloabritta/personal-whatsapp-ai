#!/usr/bin/env node
// ============================================================================
//  THROWAWAY REPRODUCTION — card 689e2395, NEW incident (list-intent delete).
//
//  Drives the DEPLOYED two-phase flow (router.route() then router.extract(),
//  commit 1b33831 == origin/main == the droplet's /app) on the incident
//  transcript, whose FINAL owner message is a plain LIST intent:
//      "@mary o que esta na minha agenda hoje?"
//  ...but whose recent history is dominated by an unresolved cancel of the
//  "Churrasco" event (create-with-link -> "@mary cancelar" -> confirm -> "sim"
//  -> deleteNoMatch).
//
//  It imports route()+extract() from a worktree checked out at 1b33831 (the
//  DEPLOYED revision), NOT the stale local `main` (aca9c96). It observes:
//    phase 1 route()   -> { keepListening, execute }         (which skills)
//    phase 2 extract() -> { action, event_id, ... }          (the payload)
//  If extract() emits action:"delete" with a NON-NULL event_id on this
//  list-intent turn, that is the mis-dispatch + resolvable-target that deleted
//  the event. Google is stubbed OFFLINE (loader hook) purely so the calendar
//  skill MODULE (which does `import "googleapis"` at top) can load and expose
//  its manifest.inputs — NO real calendar call is made; the skill is NOT run.
//
//  Needs ANTHROPIC_API_KEY. Run:
//    ANTHROPIC_API_KEY=$PROJECT_ANTHROPIC_API_KEY NATIVE_TOOLS=on \
//      node scripts/calendar-listintent-delete-repro.mjs
// ============================================================================
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { mkdtemp, writeFile, readdir } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";

const THIS_FILE = fileURLToPath(import.meta.url);
const WT = process.env.DEPLOYED_WT || "/tmp/deployed-1b33831";

if (!process.env.LISTINTENT_CHILD) {
  // ---- PARENT: write a googleapis stub + loader hook, re-exec as child ----
  if (!process.env.ANTHROPIC_API_KEY) {
    console.error("ANTHROPIC_API_KEY unset — this repro needs a live model call.");
    process.exit(2);
  }
  const tmp = await mkdtemp(path.join(os.tmpdir(), "listintent-repro-"));
  await writeFile(
    path.join(tmp, "gstub.mjs"),
    `const calendar = () => ({ events: {} });
class OAuth2 { constructor(...a){this._a=a;} setCredentials(c){this._c=c;} }
export const google = { calendar, tasks: () => ({ tasks:{}, tasklists:{} }), auth: { OAuth2 } };
export default { google };
`
  );
  await writeFile(
    path.join(tmp, "hooks.mjs"),
    `const STUB = ${JSON.stringify(pathToFileURL(path.join(tmp, "gstub.mjs")).href)};
export async function resolve(spec, ctx, next) {
  if (spec === "googleapis") return { url: STUB, format: "module", shortCircuit: true };
  return next(spec, ctx);
}
`
  );
  await writeFile(
    path.join(tmp, "register.mjs"),
    `import { register } from "node:module";
register("./hooks.mjs", import.meta.url);
`
  );
  const child = spawn(
    process.execPath,
    ["--import", pathToFileURL(path.join(tmp, "register.mjs")).href, THIS_FILE],
    { stdio: "inherit", env: { ...process.env, LISTINTENT_CHILD: "1" } }
  );
  child.on("exit", (code) => process.exit(code ?? 1));
} else {
  // ---- CHILD: googleapis is stubbed. Import the DEPLOYED router + skills. ----
  const Anthropic = (await import("@anthropic-ai/sdk")).default;
  const { route, extract } = await import(
    pathToFileURL(path.join(WT, "secretary/1. Orchestrator/router/router.js")).href
  );
  const { renderStateBlock } = await import(
    pathToFileURL(path.join(WT, "secretary/1. Orchestrator/router/prompt.js")).href
  );

  const SKILLS_DIR = path.join(WT, "secretary", "3. Mary Skills");
  const OWNER = process.env.OWNER_NAME || "Marcelo";
  const MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-5";
  const N = Number(process.env.REPRO_RUNS) || 5;

  async function buildCatalog() {
    const catalog = [];
    for (const e of await readdir(SKILLS_DIR, { withFileTypes: true })) {
      if (!e.isDirectory()) continue;
      const file = path.join(SKILLS_DIR, e.name, "skill.js");
      try {
        const mod = await import(pathToFileURL(file).href);
        const id = mod.manifest?.id;
        if (!id || typeof mod.run !== "function") continue;
        catalog.push({
          id,
          description: mod.manifest.description || "",
          inputs: mod.manifest.inputs || null,
          conversation: mod.manifest.conversation === "orchestrator" ? "orchestrator" : "skill",
        });
      } catch (err) {
        console.error(`skip skill "${e.name}":`, err.message);
      }
    }
    return catalog;
  }

  const CHURRASCO_LINK =
    "https://www.google.com/calendar/event?eid=YzRhYWhlMWY4bWwxYm1zZWVya2tobHVvaTggbWFyY2Vsb2Ficml0dGFAbQ";
  const CHURRASCO_ID = "c4aahe1f8ml1bmseerkkhluoi8";

  const labeledTranscript = [
    `OWNER: @mary agendar o churrasco amanhã 12h na Rua Copaíba, 44, Urbanova, São José dos Campos`,
    `SECRETARY: Pronto! Convite criado e enviado:\n\n- Churrasco\n- (ninguém convidado)\n- 2 de ago. de 2026, 12:00\n- Rua Copaíba, 44, Urbanova, São José dos Campos\n\nAqui está o link do evento:\n${CHURRASCO_LINK}`,
    `OWNER: @mary cancelar`,
    `SECRETARY: Confirma que quer cancelar o evento "Churrasco" (2 de ago. de 2026, 12:00, Rua Copaíba, 44, Urbanova, São José dos Campos)?`,
    `OWNER: sim`,
    `SECRETARY: Não encontrei um evento correspondente — pode já ter sido cancelado, ou não tenho certeza de qual você quer dizer.`,
    `OWNER: @mary o que esta na minha agenda hoje?`,
  ].join("\n");

  const ORDER = "o que esta na minha agenda hoje?";
  const nowStr = "8/1/2026, 8:20:00 AM";
  const freshState = { goal: ORDER.slice(0, 240), pendingNeed: null, payload: null, didWork: false, log: [] };

  const baseCtx = (anthropic, catalog) => ({
    owner: OWNER, anthropic, model: MODEL, env: process.env,
    order: ORDER, transcript: labeledTranscript, nowStr, contact: "Avelino",
    hasQuotedAudio: false, quoted: null, catalog, tags: ["@mary"], media: null, audioTranscript: null,
  });

  async function runOnce(anthropic, catalog, calInputs, i) {
    const ctx = baseCtx(anthropic, catalog);
    const stateBlock = renderStateBlock(freshState);
    const r = await route(ctx, { labeledTranscript, stateBlock });
    const execute = r.execute || [];
    console.log(`\n===== RUN ${i + 1}/${N} =====`);
    console.log("route -> keepListening:", r.keepListening, " execute:", JSON.stringify(execute), " hasSay:", !!r.say, r.say ? `("${String(r.say).slice(0,60)}")` : "");

    let action = null, eventId = null, info = null;
    if (execute.includes("calendar_action")) {
      info = await extract(ctx, { labeledTranscript, primary: "calendar_action", spec: calInputs, stateBlock, problems: null });
      action = info?.action ?? null;
      eventId = info?.event_id ?? null;
      console.log("extract -> action:", JSON.stringify(action), " event_id:", JSON.stringify(eventId));
      console.log("extract full info:", JSON.stringify(info));
    } else {
      console.log("(route did NOT put calendar_action in execute this run)");
    }
    const misDispatch = execute.includes("calendar_action") && action === "delete";
    const resolvable = misDispatch && !!eventId && String(eventId).includes(CHURRASCO_ID.slice(0, 10));
    console.log(
      misDispatch
        ? resolvable
          ? ">>> MIS-DISPATCH + RESOLVABLE TARGET (delete would EXECUTE) — reproduces the new incident"
          : `>>> mis-dispatch delete, event_id=${JSON.stringify(eventId)} (would ${eventId ? "resolve via id" : "bail noMatch"})`
        : ">>> no delete dispatched this run"
    );
    return { execute, action, eventId, misDispatch, resolvable };
  }

  const catalog = await buildCatalog();
  const calEntry = catalog.find((c) => c.id === "calendar_action");
  console.log("deployed worktree:", WT);
  console.log("catalog ids:", catalog.map((c) => c.id).join(", "));
  console.log("model:", MODEL, " NATIVE_TOOLS:", process.env.NATIVE_TOOLS || "(unset)");
  if (!calEntry) { console.error("calendar_action not in catalog — abort"); process.exit(3); }
  const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

  const results = [];
  for (let i = 0; i < N; i++) {
    try { results.push(await runOnce(anthropic, catalog, calEntry.inputs, i)); }
    catch (e) { console.error(`RUN ${i + 1} threw:`, e?.message || e); results.push({ error: e?.message || String(e) }); }
  }
  const dispatchedCal = results.filter((r) => r.execute?.includes("calendar_action")).length;
  const deletes = results.filter((r) => r.action === "delete").length;
  const lists = results.filter((r) => r.action === "list" || r.action === "find").length;
  const resolvable = results.filter((r) => r.resolvable).length;
  console.log(`\n===== SUMMARY over ${N} runs =====`);
  console.log(`route dispatched calendar_action : ${dispatchedCal}/${N}`);
  console.log(`extract action == delete          : ${deletes}/${N}`);
  console.log(`extract action == list/find       : ${lists}/${N}`);
  console.log(`delete WITH resolvable event_id   : ${resolvable}/${N}  <- would actually delete`);
}
