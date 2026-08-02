# Chat Math Assistant

## Summary
On request, the assistant scans recent chat messages (including images like bills or PDFs) to find a relevant value and performs a quick calculation, replying inline with the result.

## Problem / motivation
Marcelo often needs to do quick math — like splitting or adding a bill amount — based on values shared in chat, sometimes as images (receipts, PDFs). Doing this manually is tedious, so he wants the assistant to handle it inline instead.

## User flow (from the user's point of view)
1. Marcelo or someone else shares a value in chat, possibly as an image (e.g. a bill or PDF).
2. Marcelo calls the assistant with a math instruction, referencing that value (e.g. "secretary, split that by 2" or "add X to it").
3. The assistant scans the last 10 messages in the chat to find the relevant number, including reading values from images if needed.
4. If the relevant value is unambiguous, the assistant performs the calculation and replies inline with the result.
5. If it's unclear which value or image to use, the assistant asks Marcelo to clarify instead of guessing.
6. No tracking or logging happens afterward — it's treated as a one-off reply.

## Actors
- Marcelo
- Assistant

## Data & services touched
- Chat message history (last 10 messages), including both text and images (e.g. bill/PDF images)
- Image/text extraction capability to read numeric values from images

## Edge cases & open questions
- Multiple candidate numbers or images found in the last 10 messages — assistant must ask for clarification rather than guess.
- Value is embedded in an image (e.g. a PDF of a purchase bill) — assistant must correctly read/extract the number from it.
- No relevant number found in the last 10 messages.
- **Open:** Should the assistant support more complex/multi-step math beyond simple split/add, or is this scoped to simple arithmetic only?

---
*Drafted by @assistant on WhatsApp. Save to the repo and refine.*