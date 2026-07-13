#!/usr/bin/env node
// ============================================================================
//  Self-test for the Tasks planner's ADDRESSED bit.
//
//  The bug this exists to prevent (2026-07-11, "task false positive" —
//  Bugs and Malfunctions/bugfix-task-false-positive.md):
//  the engaged window keeps listening for 10 minutes after a task exchange, and a
//  continuation is NEVER tagged (server.js:255). So the planner read
//  "amanha vou tentar implementar o tenente dentro do VsCode" — the owner talking to
//  TONY about TONY's project — as an order, and silently wrote a phantom task to the
//  owner's real Google Tasks list. Thirteen seconds later "e mandar ele ter workers"
//  was read as an EDIT to the phantom. The planner had no way to know it had not been
//  addressed: ctx.tag falls back to TAGS[0] and is always truthy.
//
//  The fix threads ctx.isTagged into planTaskOps and gives the untagged case a
//  stricter posture. This fixture proves BOTH directions, because the dangerous
//  regression is the SILENT one — a posture that leans too hard on "do nothing"
//  swallows genuine in-window follow-ups with no reply and no failure report
//  (skill.js:368-371). Over-correction is a FAILURE here, not a pass.
//
//  TWO HALVES, and both matter:
//
//  Half A — LIVE model calls (costs a few cents). Proves the PROMPT:
//    1-4    the overheard chatter (incl. the two real logged strings) -> EMPTY plan
//    5-10   genuine untagged in-window follow-ups -> the RIGHT ops. The window still works.
//    11-12  the tagged path is unchanged
//    13-15  the quoted path: a reaction/musing over a quote creates nothing; an explicit
//           "transforma isso em tarefa" still does
//    16     an untagged create whose phrasing appears NOWHERE in the prompt's examples —
//           it can only pass if the RULE is being reasoned from, not the few-shots.
//
//  Half B — OFFLINE source scan. Proves the WIRING, which half A is blind to:
//    planTaskOps is private, and driving run/resumeEngaged/resumeConfirm end-to-end
//    would need real Google Tasks OAuth + a session store. So the only way to see the
//    three call sites is to read the source — the precedent is selflearning-selftest.mjs's
//    lint. It asserts the VALUE (ctx.isTagged), not merely that some argument was passed:
//    a hardcoded { addressed: true } at skill.js:591 would satisfy a presence check, sail
//    through half A untouched, and silently restore the bug.
//
//  Run:  ANTHROPIC_API_KEY=sk-ant-… node scripts/tasks-addressed-selftest.mjs   (both halves)
//        TASKS_SELFTEST_OFFLINE=1  node scripts/tasks-addressed-selftest.mjs    (half B only, free)
//        ANTHROPIC_API_KEY=… RUNS=3 node scripts/tasks-addressed-selftest.mjs   (the acceptance run —
//              the fix is probabilistic, so every live case runs 3x and ANY failure is a failure)
// ============================================================================
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";

import {
  buildPlanSystem,
  buildPlanUser,
  PLAN_SCHEMA,
} from "../secretary/2. Skills/3. Tasks/prompt.js";
import { jsonFormat, readReply } from "../secretary/1. Orchestrator/lib/llm.js";

const OFFLINE = process.env.TASKS_SELFTEST_OFFLINE === "1";
const RUNS = Math.max(1, Number(process.env.RUNS || 1));
const MODEL = process.env.CLAUDE_MODEL || "claude-sonnet-5"; // mirrors server.js:50
const OWNER = "Marcelo";
const CONTACT = "Tony Lampada";

// Fixed, so "tomorrow" is deterministically 2026-07-12.
const NOW = "Saturday, 07/11/2026, 11:18 PM";

let failures = 0;
function check(name, cond) {
  console.log(`${cond ? "  ok  " : "  FAIL"}  ${name}`);
  if (!cond) failures++;
}

// ---- Fixtures ----------------------------------------------------------------
// The real logged exchange (bugfix-task-false-positive.md). Every ME: line here is the
// owner talking TO TONY about TONY's project — none of it is addressed to the secretary.
// The window is open only because of the tagged list request near the top.
const TRANSCRIPT_OVERHEARD = `ME: shipei aqui que todas as msgs da secretária devem ser em itálico
ME: @secretaria que tarefas eu tenho pra amanhã?
ME: *[Secretaria IA do Marcelo]:*

_Aqui estão suas tarefas em aberto:_
_12/jul - brincar com o sistema do Tony_
OTHER: Fazendo um bugfix pro bridge…
ME: esse projeto seu n parece ser mto grande. é?
ME: pq vc chama esse liutenant de litenant, mas ele é um worker rssss
ME: tira o nome e o rosto, é só mais um robo uai rsss ainda n capturei a diferença para além da interface gráfica
OTHER: Grande em termos de volume de código?
ME: de complexidade da coisa toda
OTHER: Vc não entendeu ainda rs
OTHER: O tenente cria novas sessões do Claude code e conversa com elas.
OTHER: O tenente não faz as coisas. Só delega
ME: mas vc que delegou ai uai rsss
OTHER: E eu não tô falando do subagente. Eh outro terminal mesmo que ele pilota. Por isso tem o tmux.
OTHER: Ah sim. Mas ele monitora pra mim

Quem decide as coisas sou eu ainda né rs
OTHER: O fato do tenente não implementar diretamente permite que ele funcione num nível de abstração mais alto`;

// Case 2's world: the phantom has ALREADY been created 13s earlier, so its referent IS
// now on the list. The continuation must still be read as speech to Tony.
const TRANSCRIPT_PHANTOM_CREATED = `${TRANSCRIPT_OVERHEARD}
ME: amanha vou tentar implementar o tenente dentro do VsCode`;

// A task exchange the secretary is genuinely in the middle of: the owner tagged her, she
// created the task and confirmed. Whatever he types next is a plausible follow-up TO HER.
const TRANSCRIPT_EXCHANGE = `ME: shipei aqui que todas as msgs da secretária devem ser em itálico
OTHER: Fazendo um bugfix pro bridge…
ME: @secretaria cria uma tarefa pra amanhã: brincar com o sistema do Tony
ME: *[Secretaria IA do Marcelo]:*

_Adicionei à sua lista:_
_12/jul - brincar com o sistema do Tony_

_Me diga se precisa mudar algo, senão está tudo certo._`;

const LIST_OVERHEARD = `1. brincar com o sistema do Tony (due 12/jul)`;
const LIST_PHANTOM = `1. brincar com o sistema do Tony (due 12/jul)
2. implementar o tenente dentro do VsCode (due 12/jul)`;
const LIST_EXCHANGE = `1. brincar com o sistema do Tony (due 12/jul)
2. comprar leite`;
const LIST_EMPTY = "";

// Deliberately task-shaped: a model that treats a QUOTE as an order will bite on it.
const QUOTED_TONY = {
  text: "acabei de subir o dashboard novo de métricas do secretário",
};

// ---- Half A: the live cases --------------------------------------------------
const ops = (p) => (Array.isArray(p?.ops) ? p.ops : []);
const one = (p) => (ops(p).length === 1 ? ops(p)[0] : null);
const has = (s, sub) => String(s || "").toLowerCase().includes(sub.toLowerCase());

const CASES = [
  {
    n: 1,
    addressed: false,
    order: "amanha vou tentar implementar o tenente dentro do VsCode",
    transcript: TRANSCRIPT_OVERHEARD,
    listText: LIST_OVERHEARD,
    why: "THE BUG — a statement of intent, said to Tony. Not a task.",
    expected: "ops: [], list_requested: false",
    assert: (p) => ops(p).length === 0 && p.list_requested === false,
  },
  {
    n: 2,
    addressed: false,
    order: "e mandar ele ter workers",
    transcript: TRANSCRIPT_PHANTOM_CREATED,
    listText: LIST_PHANTOM,
    why: "THE COMPOUNDING EDIT — continues the sentence above, even though the phantom is now ON the list.",
    expected: "ops: []",
    assert: (p) => ops(p).length === 0,
  },
  {
    n: 3,
    addressed: false,
    order: "quantas tarefas você tem pra amanhã?",
    transcript: TRANSCRIPT_OVERHEARD,
    listText: LIST_OVERHEARD,
    why: "THE LIST LEAK — 'você' is Tony. Printing the list here shows it to him.",
    expected: "list_requested: false, ops: []",
    assert: (p) => p.list_requested === false && ops(p).length === 0,
  },
  {
    n: 4,
    addressed: false,
    order: "cancela a reunião com o cliente",
    transcript: TRANSCRIPT_OVERHEARD,
    listText: LIST_OVERHEARD,
    why: "an imperative — but its referent is a meeting, not a task on his list. Aimed at Tony.",
    expected: "ops: []",
    assert: (p) => ops(p).length === 0,
  },
  {
    n: 5,
    addressed: false,
    order: "na verdade muda essa pra sexta",
    transcript: TRANSCRIPT_EXCHANGE,
    listText: LIST_EXCHANGE,
    why: "OVER-CORRECTION GUARD — the window must still work.",
    expected: "1 op, kind=edit, target_index=1",
    assert: (p) => one(p)?.kind === "edit" && one(p)?.target_index === 1,
  },
  {
    n: 6,
    addressed: false,
    order: "cancela essa",
    transcript: TRANSCRIPT_EXCHANGE,
    listText: LIST_EXCHANGE,
    why: "OVER-CORRECTION GUARD — refers to the task just touched.",
    expected: "1 op, kind=delete, target_index=1",
    assert: (p) => one(p)?.kind === "delete" && one(p)?.target_index === 1,
  },
  {
    n: 7,
    addressed: false,
    order: "pode marcar a de comprar leite como feita",
    transcript: TRANSCRIPT_EXCHANGE,
    listText: LIST_EXCHANGE,
    why: "OVER-CORRECTION GUARD — refers to a task on his open list.",
    expected: "1 op, kind=complete, target_index=2",
    assert: (p) => one(p)?.kind === "complete" && one(p)?.target_index === 2,
  },
  {
    n: 8,
    addressed: false,
    order: "adiciona também: comprar pão",
    transcript: TRANSCRIPT_EXCHANGE,
    listText: LIST_EXCHANGE,
    why: "OVER-CORRECTION GUARD — an untagged CREATE is still a create. A create is never on the list; that proves nothing.",
    expected: "1 op, kind=create, title contains 'pão'",
    assert: (p) => one(p)?.kind === "create" && has(one(p)?.title, "pão"),
  },
  {
    n: 9,
    addressed: false,
    order: "e o que mais eu tenho?",
    transcript: TRANSCRIPT_EXCHANGE,
    listText: LIST_EXCHANGE,
    why: "OVER-CORRECTION GUARD — a first-person question TO the secretary. The uniform bar most risks killing this.",
    expected: "list_requested: true, ops: []",
    assert: (p) => p.list_requested === true && ops(p).length === 0,
  },
  {
    n: 10,
    addressed: false,
    order: "pronto, é isso",
    transcript: TRANSCRIPT_EXCHANGE,
    listText: LIST_EXCHANGE,
    why: "owner_done is the one op the overheard posture may still emit — a window that can only die by TTL is a regression.",
    expected: "owner_done: true, ops: []",
    assert: (p) => p.owner_done === true && ops(p).length === 0,
  },
  {
    n: 11,
    addressed: true,
    order: "crie tarefa pra mim amanhã: brincar com o sistema do Tony",
    transcript: TRANSCRIPT_OVERHEARD,
    listText: LIST_EMPTY,
    why: "the TAGGED path is unchanged (tag already stripped, as server.js:271 does).",
    expected: "1 op, kind=create, due_iso starts 2026-07-12",
    assert: (p) =>
      one(p)?.kind === "create" && String(one(p)?.due_iso || "").startsWith("2026-07-12"),
  },
  {
    n: 12,
    addressed: true,
    order: "que tarefas eu tenho pra amanhã?",
    transcript: TRANSCRIPT_OVERHEARD,
    listText: LIST_OVERHEARD,
    why: "the TAGGED read is unchanged.",
    expected: "list_requested: true",
    assert: (p) => p.list_requested === true,
  },
  {
    n: 13,
    addressed: false,
    order: "kkk isso é muito bom",
    quoted: QUOTED_TONY,
    transcript: TRANSCRIPT_EXCHANGE,
    listText: LIST_EXCHANGE,
    why: "a reaction to a quote. The quote is context, never an order.",
    expected: "ops: []",
    assert: (p) => ops(p).length === 0,
  },
  {
    n: 14,
    addressed: false,
    order: "temos que fazer isso um dia",
    quoted: QUOTED_TONY,
    transcript: TRANSCRIPT_EXCHANGE,
    listText: LIST_EXCHANGE,
    why: "the hard one — a task-shaped musing over a quote, on the create-from-quote path the core prompt actively invites.",
    expected: "ops: []",
    assert: (p) => ops(p).length === 0,
  },
  {
    n: 15,
    addressed: false,
    order: "transforma isso em tarefa",
    quoted: QUOTED_TONY,
    transcript: TRANSCRIPT_EXCHANGE,
    listText: LIST_EXCHANGE,
    why: "OVER-CORRECTION GUARD on the quote clause — an explicit imperative to record the quote still ACTS, titled from the QUOTED text.",
    expected: "1 op, kind=create, title contains 'dashboard'",
    assert: (p) => one(p)?.kind === "create" && has(one(p)?.title, "dashboard"),
  },
  {
    n: 16,
    addressed: false,
    order: "coloca na lista: pagar o IPVA",
    transcript: TRANSCRIPT_EXCHANGE,
    listText: LIST_EXCHANGE,
    why: "THE CANARY — this phrasing appears NOWHERE in the posture's examples, by design. Cases 8 and 15 use wordings the few-shots teach, so a model could pass them by pattern-matching the example list while the RULE is still broken. This one can only pass if rule (b) — form-of-address, referent test NOT applied to creates — is actually being reasoned from. If the referent rule ever leaks back onto creates, this goes red first. If a future edit adds 'coloca na lista' to the examples, change THIS CASE, not the prompt.",
    expected: "1 op, kind=create, title contains 'IPVA'",
    assert: (p) => one(p)?.kind === "create" && has(one(p)?.title, "IPVA"),
  },
];

// The call MUST mirror production exactly (skill.js:127-145): same max_tokens, same
// output_config, and NO temperature. A fixture that is easier than production proves nothing.
// `thinking` too: this script builds its OWN raw Anthropic client, so it does not inherit
// server.js's withThinkingDefault() wrapper — production sends thinking:{type:"disabled"} on
// every call, and a fixture that let the model think would no longer be mirroring it.
async function planFor(anthropic, c) {
  const msg = await anthropic.messages.create({
    model: MODEL,
    max_tokens: 1500,
    thinking: { type: "disabled" },
    system: buildPlanSystem(OWNER, { addressed: c.addressed }),
    output_config: jsonFormat(PLAN_SCHEMA),
    messages: [
      {
        role: "user",
        content: buildPlanUser(OWNER, {
          order: c.order,
          transcript: c.transcript,
          nowStr: NOW,
          contact: CONTACT,
          quoted: c.quoted || null,
          listText: c.listText,
          addressed: c.addressed,
        }),
      },
    ],
  });
  return readReply(msg, "tasks");
}

if (!OFFLINE) {
  if (!process.env.ANTHROPIC_API_KEY) {
    console.error(
      "ANTHROPIC_API_KEY is not set — half A calls the live planner.\n" +
        "Run the offline wiring lint alone with:  TASKS_SELFTEST_OFFLINE=1 node scripts/tasks-addressed-selftest.mjs"
    );
    process.exit(2);
  }

  // There is NO root package.json / node_modules — the SDK lives in secretary/node_modules,
  // so a bare `import Anthropic from "@anthropic-ai/sdk"` throws ERR_MODULE_NOT_FOUND from
  // scripts/ (which is exactly why router-selftest.mjs cannot run today). Resolve it from
  // the secretary package instead.
  const require = createRequire(new URL("../secretary/package.json", import.meta.url));
  const AnthropicMod = require("@anthropic-ai/sdk");
  const Anthropic = AnthropicMod.default ?? AnthropicMod;
  const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

  console.log(`\nhalf A — LIVE planner calls  (model ${MODEL}, RUNS=${RUNS})\n`);
  for (const c of CASES) {
    for (let r = 1; r <= RUNS; r++) {
      let plan = null;
      let err = null;
      try {
        plan = await planFor(anthropic, c);
      } catch (e) {
        err = e;
      }
      const ok = !err && !!plan && !!c.assert(plan);
      const tag = c.addressed ? "TAGGED  " : "untagged";
      check(
        `A${String(c.n).padStart(2, "0")}${RUNS > 1 ? `.${r}` : ""}  [${tag}]${c.quoted ? " [quoted]" : ""} "${c.order}"`,
        ok
      );
      if (!ok) {
        console.log(`          why      ${c.why}`);
        console.log(`          expected ${c.expected}`);
        console.log(
          `          got      ${err ? `ERROR ${err?.message || err}` : JSON.stringify(plan)}`
        );
      }
    }
  }
}

// ---- Half B: the offline source scan (always runs) ---------------------------
console.log(`\nhalf B — offline source scan: the wiring\n`);

const SKILL_SRC = await readFile(
  new URL("../secretary/2. Skills/3. Tasks/skill.js", import.meta.url),
  "utf8"
);
const SERVER_SRC = await readFile(
  new URL("../secretary/1. Orchestrator/server.js", import.meta.url),
  "utf8"
);

// The balanced { … } that follows `needle`, so the scan reads the ctx literal itself and
// not the whole file.
function objectLiteral(src, needle) {
  const at = src.indexOf(needle);
  if (at < 0) return "";
  const open = src.indexOf("{", at);
  if (open < 0) return "";
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(open, i + 1);
  }
  return "";
}

const totalMentions = (SKILL_SRC.match(/planTaskOps\(/g) || []).length;
const declarations = (SKILL_SRC.match(/async function planTaskOps\(/g) || []).length;
const callSites = totalMentions - declarations;
check(
  `B01  exactly THREE planTaskOps() call sites — run, resumeConfirm, resumeEngaged (found ${callSites})`,
  callSites === 3
);

const wired = (
  SKILL_SRC.match(
    /planTaskOps\(\s*ctx\s*,\s*open\s*,\s*\{\s*addressed:\s*ctx\.isTagged\s*\}\s*\)/g
  ) || []
).length;
check(
  `B02  every call site passes the flag WITH THE VALUE ctx.isTagged (${wired}/3)`,
  callSites === 3 && wired === 3
);

// Presence-checking is NOT enough: `{ addressed: true }` at skill.js:591 would pass a
// "was a third argument given?" lint, pass half A untouched (half A never sees the wiring),
// and silently restore the bug. Read the VALUE; fail on the literal.
check(
  "B03  NO call site hardcodes a literal (addressed: true / addressed: false)",
  !/addressed:\s*(true|false)\b/.test(SKILL_SRC)
);

check(
  "B04  `addressed` is REQUIRED — declared as planTaskOps(ctx, open, { addressed }), never defaulted",
  /async function planTaskOps\(ctx, open, \{ addressed \}\)/.test(SKILL_SRC) &&
    !/\{\s*addressed\s*\}\s*=\s*\{/.test(SKILL_SRC) &&
    !/\baddressed\s*=\s*(true|false)/.test(SKILL_SRC)
);

// A rails field with zero readers, or a reader with no rails field: both are red.
check(
  "B05  the rails field exists — server.js's ctx literal carries `isTagged`",
  /^\s*isTagged,\s*$/m.test(objectLiteral(SERVER_SRC, "const ctx = {"))
);

// Never let a throwing builder crash the scan — a FAIL is a result, a stack trace is not.
const posture = (addressed) => {
  try {
    return buildPlanSystem(OWNER, { addressed });
  } catch {
    return "";
  }
};
const TAGGED = posture(true);
const UNTAGGED = posture(false);

check(
  "B06  the two postures DIFFER (buildPlanSystem addressed:true !== addressed:false)",
  !!TAGGED && !!UNTAGGED && TAGGED !== UNTAGGED
);

check(
  "B07  the addressed posture is TODAY'S text — no untagged block, and it keeps the empty-plan guardrail",
  !/THIS MESSAGE CARRIES NO TAG/.test(TAGGED) &&
    TAGGED.includes("When unsure, PREFER the empty plan")
);

// Every in-window follow-up is untagged (server.js:255), so this branch is entered by
// "na verdade muda essa pra sexta" too. A posture that ASSERTS "you were not addressed"
// would silently swallow real follow-ups. It must ASK.
check(
  "B08  the untagged posture ASKS whether it was addressed — it never ASSERTS that it wasn't",
  /Was this message aimed at YOU/.test(UNTAGGED) &&
    !/was NOT addressed to you/i.test(UNTAGGED) &&
    !/you are overhearing\b/i.test(UNTAGGED)
);

check(
  "B09  the untagged posture carries the uniform bar (ops + list_requested, owner_done exempt), the two logged strings, and the quote clause",
  UNTAGGED.includes("list_requested") &&
    UNTAGGED.includes("owner_done") &&
    UNTAGGED.includes("amanha vou tentar implementar o tenente dentro do VsCode") &&
    UNTAGGED.includes("e mandar ele ter workers") &&
    UNTAGGED.includes("A QUOTED MESSAGE IS CONTEXT, NEVER AN ORDER")
);

check(
  "B10  omitting the flag is IMPOSSIBLE — buildPlanSystem(owner) throws",
  (() => {
    try {
      buildPlanSystem(OWNER);
      return false;
    } catch {
      return true;
    }
  })()
);

// A referent rule applied to CREATES forbids every untagged create (a create is by
// definition not on his list). That fails silently, and the few-shots can mask it in this
// very fixture. Keep the carve-out in the file even if someone "tidies" the posture later.
check(
  "B11  the create carve-out survives — 'NEVER apply the referent test above to a create'",
  UNTAGGED.includes("NEVER apply the referent test above to a create")
);

// ---- done --------------------------------------------------------------------
console.log(
  `\n${failures === 0 ? "PASS" : `FAIL (${failures})`}` +
    (OFFLINE ? "   [half B only — TASKS_SELFTEST_OFFLINE=1, no live calls made]" : "") +
    "\n"
);
process.exit(failures === 0 ? 0 : 1);
