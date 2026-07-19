# File & Image Interpretation in Stateful Conversations

## Summary
The assistant can interpret files and images (receipts, screenshots, PDFs, documents) sent as replies within an ongoing conversation, not just text or audio.

## Problem / motivation
Today, when Marcelo is in the middle of a conversation with the assistant, he may want to reply with a photo, screenshot, PDF, or other document instead of typing — for example, sending a picture of a receipt or bill. The assistant may not fully understand these file types when they arrive mid-flow, which limits how Marcelo can provide input and forces him to fall back on text or audio.

## User flow (from the user's point of view)
1. Marcelo is in an ongoing stateful conversation with the assistant, in any function.
2. Instead of replying with text or audio, he sends a file — an image, PDF, or document.
3. The assistant interprets the content of the file as it relates to the current flow.
4. If something in the file is unclear or missing, the assistant asks Marcelo for the missing specifics, just as it would if he had replied with text.
5. The assistant continues the conversation using the information it extracted from the file.
6. This works hand-in-hand with the chat math feature — for example, the assistant can read a bill image and use its contents directly in a calculation.

## Actors
- Marcelo (user)
- Assistant

## Data & services touched
- Files sent in chat: photos, screenshots, PDFs, documents, bills, and similar (video files excluded)
- Chat math feature (for calculations based on file content)
- Stateful conversation/session context (to know what flow the file applies to)

## Edge cases & open questions
- File is unclear, low quality, unreadable, or in an unsupported format.
- File contains multiple relevant pieces of information, making it ambiguous which one applies.
- This capability only applies to replies within an existing flow, not to starting brand-new requests.
- Video/film files are not supported.

---
*Drafted by @assistant on WhatsApp. Save to the repo and refine.*