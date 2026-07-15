# Full Model Capacity as Core Orchestrator Skill

## Summary
The assistant gains a core, orchestrator-level skill that unlocks the frontier model's full native capabilities — audio understanding, file/image reading, math, and contextual reasoning — during any active conversation, aware of what the user is currently trying to do.

## Problem / motivation
Right now, capabilities like understanding audio, reading files, or doing math are treated as separate, narrowly-scoped features. But they're really all facets of the same thing: the frontier model's full native capacity. Splitting them up skill-by-skill wastes capability and creates redundant, duplicated work. Bringing them together as one orchestrator-level skill lets the assistant respond naturally and fully, no matter what the user throws at it mid-conversation.

## User flow (from the user's point of view)
1. Marcelo is in the middle of a conversation, possibly inside a specific skill (e.g. calendar, chat math).
2. Marcelo sends a message that may include audio, a file/image, a math request, or a contextual question.
3. The orchestrator's "full model capacity" skill handles the input using the model's native abilities, factoring in what the active skill is trying to accomplish.
4. If something needed is missing or unclear, the assistant asks Marcelo for specifics — just as it would if the input had been plain text.
5. The assistant responds appropriately, keeping in mind whether there's a desired outcome tied to the active skill, or whether it's simply answering a standalone question.

## Actors
- Marcelo (user)
- Secretary/Assistant (orchestrator + active skill)

## Data & services touched
- Chat history (recent messages, used for context)
- Audio messages sent in chat
- Files/images sent in chat
- Active skill/flow state

## Edge cases & open questions
- Audio/file input is only used to support replies mid-flow, not to kick off brand-new requests.
- The manual `@assistant transcribe` command remains available outside of stateful flows.
- Video files are excluded from file interpretation.
- For chat math specifically: the assistant looks at the last 10 messages for relevant numbers, asks rather than guesses when unclear, and does not track or log calculations.
- Ambiguous or unclear input should always prompt the assistant to ask Marcelo, rather than guess.
- **Open:** When the model answers a contextual question mid-skill (e.g. a calendar weekday calculation), should it also nudge the flow forward automatically, or just answer and wait for Marcelo's next move?

---
*Drafted by @assistant on WhatsApp. Save to the repo and refine.*