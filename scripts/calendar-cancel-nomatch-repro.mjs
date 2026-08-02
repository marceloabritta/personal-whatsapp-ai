#!/usr/bin/env node
// ============================================================================
//  THROWAWAY REPRODUCTION — card 689e2395 "calendar event deletion error".
//
//  The incident (2026-08-01, chat with contact "Avelino"): the owner created a
//  "Churrasco" event, said "@mary cancelar", Mary asked to confirm, the owner said
//  "sim", and calendar_action returned the SOFT failure
//    "Não encontrei um evento correspondente — pode já ter sido cancelado, ..."
//  instead of deleting the event. No API/HTTP error, no stack trace.
//
//  This drives the REAL production classification turn (router.route(), the live
//  merged router+extractor call — the same one server.js runs) on the exact "sim"
//  confirm turn, using the real skill catalog and prompts. It then feeds the model's
//  emitted `info` payload into the REAL calendar_action run() with a capturing ctx and
//  prints the exact string the skill sends back.
//
//  NOTE: when the model emits a delete with event_id:null and there is no quoted
//  calendar link and no start+attendee-email locator, handleDelete bails at its
//  no-target guard and sends deleteNoMatch WITHOUT ever constructing a Google client —
//  so this repro needs NO Google credentials and makes NO calendar call on that path.
//
//  Needs ANTHROPIC_API_KEY (live model call, ~cheap). Google is never touched.
//  Run:  ANTHROPIC_API_KEY=$PROJECT_ANTHROPIC_API_KEY node scripts/calendar-cancel-nomatch-repro.mjs
// ============================================================================
import { readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import Anthropic from "@anthropic-ai/sdk";

import { route } from "../secretary/1. Orchestrator/router/router.js";
import * as calSkill from "../secretary/3. Mary Skills/1. Calendar Actions/skill.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.join(HERE, "..");
const SKILLS_DIR = path.join(REPO, "secretary", "3. Mary Skills");

const OWNER = "Marcelo";
const MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-5";
const N = Number(process.env.REPRO_RUNS) || 5;

if (!process.env.ANTHROPIC_API_KEY) {
  console.error("ANTHROPIC_API_KEY unset — this repro needs a live model call.");
  process.exit(2);
}

// --- Build the skill catalog exactly like server.js loadSkills() -------------
async function buildCatalog() {
  const catalog = [];
  const entries = await readdir(SKILLS_DIR, { withFileTypes: true });
  for (const e of entries) {
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
        conversation:
          mod.manifest.conversation === "orchestrator" ? "orchestrator" : "skill",
      });
    } catch (err) {
      console.error(`skip skill "${e.name}":`, err.message);
    }
  }
  return catalog;
}

// --- The incident conversation, in the model's own labelled view -------------
// Reconstructed from REPORT.md: an earlier "@mary agendar o churrasco" that SUCCEEDED
// (createDone with the event link), then "@mary cancelar" -> Mary's confirm bubble ->
// "sim". The create bubble carries the event link but the "sim" is a PLAIN message,
// not a reply quoting it — so ctx.quoted is null (no quoted calendar link).
const CHURRASCO_LINK =
  "https://www.google.com/calendar/event?eid=YzRhYWhlMWY4bWwxYm1zZWVya2tobHVvaTggbWFyY2Vsb2Ficml0dGFAbQ";

const labeledTranscript = [
  `OWNER: @mary agendar o churrasco amanhã 12h na Rua Copaíba, 44, Urbanova, São José dos Campos`,
  `SECRETARY: Pronto! Convite criado e enviado:\n\n- Churrasco\n- (ninguém convidado)\n- 2 de ago. de 2026, 12:00\n- Rua Copaíba, 44, Urbanova, São José dos Campos\n\nAqui está o link do evento:\n${CHURRASCO_LINK}`,
  `OWNER: @mary cancelar`,
  `SECRETARY: Confirma que quer cancelar o evento "Churrasco" (2 de ago. de 2026, 12:00, Rua Copaíba, 44, Urbanova, São José dos Campos)?`,
  `OWNER: sim`,
].join("\n");

const nowStr = "8/1/2026, 8:16:00 AM"; // ~incident time, America/Sao_Paulo

async function runOnce(anthropic, catalog, i) {
  const ctx = {
    owner: OWNER,
    anthropic,
    model: MODEL,
    order: "sim",
    transcript: labeledTranscript,
    nowStr,
    contact: "Avelino",
    hasQuotedAudio: false,
    quoted: null, // "sim" is a plain message, NOT a reply -> no quoted calendar link
    catalog,
    tags: ["@mary"],
    media: null,
  };
  const reply = await route(ctx, { labeledTranscript });
  const info = reply.info || {};
  console.log(`\n===== RUN ${i + 1}/${N} =====`);
  console.log("next   :", reply.next);
  console.log("skills :", JSON.stringify(reply.skills));
  console.log("info   :", JSON.stringify(info));
  console.log(
    `  -> action=${info.action}  event_id=${JSON.stringify(info.event_id)}  ` +
      `start_iso=${JSON.stringify(info.start_iso)}  participants=${JSON.stringify(info.participants)}`
  );

  // Does this payload reproduce the soft failure? Feed it into the REAL skill with a
  // capturing ctx (no Google needed on the no-target guard path).
  let reproduced = false;
  let sentText = null;
  if (reply.next === "execute" && reply.skills.includes("calendar_action") && info.action === "delete") {
    const captured = [];
    const skillCtx = {
      number: "5511981574800",
      env: process.env,
      lang: reply.lang || "pt",
      contact: "Avelino",
      remoteJid: "5511981574800@s.whatsapp.net",
      quoted: null, // no quoted calendar link — mirrors the incident
      info,
      send: async (_n, text) => captured.push({ via: "send", text }),
      sendFailure: async (_n, text) => captured.push({ via: "sendFailure", text }),
    };
    const ret = await calSkill.run(skillCtx);
    const soft = captured.find((c) => c.via === "sendFailure");
    sentText = soft?.text || (captured[0] && captured[0].text) || null;
    console.log("skill run() ->", JSON.stringify(ret));
    console.log("skill sent  ->", JSON.stringify(sentText));
    reproduced =
      ret?.ok === false &&
      ret?.reason === "noMatch" &&
      typeof sentText === "string" &&
      sentText.startsWith("Não encontrei um evento correspondente");
  } else {
    console.log("(model did NOT emit an execute of calendar_action delete on this run)");
  }
  console.log(reproduced ? ">>> REPRODUCED the soft failure on this run" : ">>> did NOT reproduce on this run");
  return { reply, info, reproduced, sentText };
}

async function main() {
  const catalog = await buildCatalog();
  console.log("catalog ids:", catalog.map((c) => c.id).join(", "));
  console.log("model:", MODEL);
  const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

  const results = [];
  for (let i = 0; i < N; i++) {
    try {
      results.push(await runOnce(anthropic, catalog, i));
    } catch (e) {
      console.error(`RUN ${i + 1} threw:`, e?.message || e);
      results.push({ reproduced: false, error: e?.message || String(e) });
    }
  }

  const repro = results.filter((r) => r.reproduced).length;
  const deletes = results.filter(
    (r) => r.info && r.info.action === "delete"
  ).length;
  const nullId = results.filter(
    (r) => r.info && r.info.action === "delete" && !r.info.event_id
  ).length;
  console.log(`\n===== SUMMARY over ${N} runs =====`);
  console.log(`delete dispatched : ${deletes}/${N}`);
  console.log(`  ...with event_id null : ${nullId}/${deletes || 0}`);
  console.log(`soft-failure REPRODUCED : ${repro}/${N}`);
}

main().catch((e) => {
  console.error("FATAL:", e);
  process.exit(1);
});
