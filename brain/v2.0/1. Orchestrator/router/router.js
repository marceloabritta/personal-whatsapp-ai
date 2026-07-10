// ============================================================================
//  router/router.js  —  ROUTER LOGIC.
//  Calls Claude with the classification prompt and returns the list of tasks,
//  validated against the catalog of skills discovered by the orchestrator.
// ============================================================================
import { buildRouterSystem, buildRouterUser } from "./prompt.js";

// ctx: { owner, anthropic, model, order, transcript, hasQuotedAudio, catalog }
// -> { tasks: string[], reason: string }
export async function route(ctx) {
  const { owner, anthropic, model, order, transcript, hasQuotedAudio, catalog } =
    ctx;
  const valid = new Set([...(catalog || []).map((c) => c.id), "other"]);

  const system = buildRouterSystem(owner, catalog || []);
  const user = buildRouterUser(owner, { order, transcript, hasQuotedAudio });

  const msg = await anthropic.messages.create({
    model,
    max_tokens: 200,
    system,
    messages: [{ role: "user", content: user }],
  });
  const out = msg.content
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("");
  console.log("ROUTER RAW:", out);

  const m = out.match(/\{[\s\S]*\}/);
  let parsed = null;
  try {
    parsed = m ? JSON.parse(m[0]) : null;
  } catch {
    parsed = null;
  }

  let tasks = Array.isArray(parsed?.tasks) ? parsed.tasks : [];
  tasks = tasks.filter((t) => valid.has(t));
  if (!tasks.length) tasks = ["other"];

  return { tasks, reason: parsed?.reason || "" };
}
