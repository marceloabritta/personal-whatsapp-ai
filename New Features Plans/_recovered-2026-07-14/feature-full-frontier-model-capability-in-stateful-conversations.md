# Full Frontier-Model Capability in Stateful Conversations

## Summary
When a conversation with the assistant is mid-flow (stateful), it should draw on the full native capability of the underlying frontier model — handling audio, files, images, math, and other contextual requests — instead of being limited to narrow, single-purpose skills.

## Problem / motivation
Audio understanding, file/image interpretation, and math have so far been treated as separate, narrowly scoped features, each with its own logic. But these are all things the frontier model can already do natively — they're facets of one underlying capability, not distinct skills to be built and maintained individually. Scoping them narrowly undersells what the assistant can actually do mid-conversation and creates duplicated logic across skills. This capability should instead live at the orchestrator level, so any skill (calendar, math, or others) can benefit from it automatically.

## User flow (from the user's point of view)
1. Marcelo is mid-flow in a stateful conversation with the assistant, possibly inside a specific skill (e.g. calendar).
2. He sends any input the frontier model can natively handle — audio, an image, a file, a math question, a contextual question, etc. — as part of that ongoing flow.
3. The orchestrator-level "full model capacity" skill interprets the input using the model's full native ability, taking into account the conversation context and whatever specific skill/intent is currently active.
4. The assistant checks whether there's a desired outcome tied to the active skill (e.g. completing a calendar action) and responds accordingly — or, if there's no skill-specific outcome (e.g. a standalone math question), it simply answers directly.
5. If something is missing or ambiguous, the assistant asks Marcelo for clarification, just as it would with a plain text instruction.
6. The assistant replies in-line within the same conversation.
7. Note: starting a brand-new request or call still requires text (or the existing `@assistant transcribe` command for standalone audio) — this broader capability only applies to replies within an existing stateful flow.

## Actors
- Marcelo (user)
- Assistant (secretary)
- Orchestrator / core "full model capacity" skill

## Data & services touched
- Chat history and context of the ongoing stateful conversation
- The active skill/flow context (e.g. calendar)
- Any audio, images, or files shared within the conversation
- Underlying frontier model's native capabilities (audio, vision/file understanding, math, etc.)

## Edge cases & open questions
- Videos/films are explicitly excluded from this capability.
- Starting a brand-new request or call by voice or file is not covered — only replies within an existing stateful flow.
- The manual `@assistant transcribe` command remains available for standalone audio outside stateful flows.
- This capability must sit at the orchestrator level, usable across all skills (calendar, math, etc.), not hardcoded into just a couple of them.
- When a specific skill's flow is active, the assistant should factor in that skill's intended outcome when responding, rather than answering in a vacuum.
- **Open:** Beyond audio, files, and math, should other frontier-model capabilities (e.g. web search, code execution, image generation) be explicitly included or excluded?
- **Open:** Should there be guardrails on what the model can do autonomously mid-flow, or is it fully open to its native capacity?
- **Open:** When answering a contextual question within an active skill (e.g. a calendar-related question), should the assistant also proactively continue/advance that skill's flow, or just answer and wait for the user's next explicit instruction?

---
*Drafted by @assistant on WhatsApp. Save to the repo and refine.*