# Audio Input Support in Stateful Conversations

## Summary
Marcelo can reply to the assistant with a voice message during an ongoing conversation, and it will be understood just like typed text.

## Problem / motivation
Right now, once Marcelo is mid-conversation with the assistant (e.g. answering a clarifying question), he can only reply with text — even though sending a quick voice message would often be faster and more natural. He wants audio replies to work seamlessly in these situations, without extra steps.

## User flow (from the user's point of view)
1. Marcelo is in a stateful exchange with the assistant (e.g. mid feature-request clarification, mid chat-math request, etc.).
2. Instead of typing a reply, he sends a voice message.
3. The assistant automatically transcribes and interprets the audio — no need for Marcelo to explicitly call `@assistant transcribe`.
4. The assistant treats the interpreted content exactly as if it had been typed, continuing the flow normally.
5. If the transcribed content is missing details needed to proceed, the assistant asks Marcelo for the specifics — the same way it would for an incomplete text instruction.

## Actors
- Marcelo
- Assistant

## Data & services touched
- Voice messages sent by Marcelo during active, stateful assistant conversations
- Existing audio transcription capability (already used by the manual `@assistant transcribe` command)

## Edge cases & open questions
- Starting a brand-new request via audio (i.e., not as a reply within a stateful flow) is explicitly out of scope.
- Unclear or ambiguous audio is handled the same as incomplete text: the assistant asks for the missing specifics rather than guessing or asking Marcelo to repeat the whole message.
- The manual `@assistant transcribe` command remains available as-is for other cases, such as transcribing audio outside a stateful flow.

---
*Drafted by @assistant on WhatsApp. Save to the repo and refine.*