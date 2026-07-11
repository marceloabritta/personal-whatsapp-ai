# Skill: `transcribe_audio`

> **For humans — quick read.**
>
> Turns a WhatsApp voice message into text.
>
> **It handles one task:** transcribe a voice note you **reply to**.
>
> **How you call it:** press-and-hold the voice message → **Reply** → type
> `@brain transcribe`. It downloads that audio, transcribes it, and sends you the text.
> Takes roughly up to a minute for short notes.
>
> If you didn't reply to an audio, it tells you how to do it.

---

## For AI / maintainers — detailed

Source: `skill.js` (logic) + `prompt.js` (fixed reply strings — **no LLM prompts**;
this skill makes **no Claude call**). Contract: `export const manifest` +
`export async function run(ctx)`; auto-discovered at boot.

### How it's invoked
The router classifies an `@brain` order as `transcribe_audio` — disambiguated by
`ctx.hasQuotedAudio` (the orchestrator tells the router when the replied-to message is a
voice note). The orchestrator then calls `run(ctx)`. **Single-shot; never stateful.**

### What it receives (`ctx`)
Uses: `number` (reply target), `quoted` (`{id,hasAudio,mediaType,text,calendarLink}` —
here `quoted.id` = the voice message id, `quoted.hasAudio` = true for a PTT/audio),
`env` (`ASSEMBLYAI_API_KEY`, `ASSEMBLYAI_LANGUAGE`), `evolution` (media download),
`send(number,text)` (reply), `tag` (for the "how to call me" hint).

### Control flow — `run(ctx)`
1. **Guard:** if `!quoted || !quoted.hasAudio` → send `MSG.noAudio(tag)` and stop.
2. **Key check:** if `env.ASSEMBLYAI_API_KEY` is missing → `MSG.transcriptionFailed`, stop.
3. **Download (Evolution):** `evolution.getMediaBase64(quoted.id)` → `{ base64, mimetype }`.
   On error → `MSG.downloadFailed`, stop. *(Requires Evolution
   `DATABASE_SAVE_DATA_NEW_MESSAGE=true`; otherwise old audios 404.)*
4. Send `MSG.processing` ("transcribing… ~1 min").
5. **Transcribe (AssemblyAI)**, wrapped in try/catch:
   - `Buffer.from(base64, "base64")` → raw bytes.
   - **`aaiUpload(apiKey, buffer)`** → `POST /v2/upload` (raw bytes) → `upload_url`.
   - **`aaiTranscribe(apiKey, uploadUrl, env.ASSEMBLYAI_LANGUAGE)`**:
     `POST /v2/transcript` `{ audio_url, language_code }` → `{ id }`, then **poll**
     `GET /v2/transcript/{id}` **up to 40 times, every 3 s (~2 min max)** until
     `status === "completed"` (returns `text`); `status === "error"` throws; exhausting
     the loop throws `"AAI timeout"`.
   - `clean = text.trim()`; send `formatTranscript(clean)` — or `MSG.empty` if blank.
   - Any throw (upload/create/poll/timeout) → caught → `MSG.transcriptionFailed`.

### External APIs
- **Evolution:** `getMediaBase64` → `POST /chat/getBase64FromMediaMessage/{instance}`
  `{message:{key:{id}}}` → decrypted `{ base64, mimetype }`.
- **AssemblyAI:** `POST /v2/upload`, `POST /v2/transcript`, `GET /v2/transcript/{id}`
  (poll). Language from `ASSEMBLYAI_LANGUAGE` (default `"en"`; set `"pt"` for Portuguese).
- **No Anthropic/LLM call.** **No WhatsApp send other than via `ctx.send`.**
- Note: audio bytes leave the droplet for AssemblyAI (a US service) — the one point where
  the self-hosted privacy model is broken (a self-hosted Whisper is the alternative).

### Stateful behavior, timeouts, completion
- **Stateful:** never — it opens no session and returns after one pass.
- **Timeout:** `aaiTranscribe` polls ~2 min (40 × 3 s); if the transcript isn't ready it
  throws and the owner gets `MSG.transcriptionFailed`. (WhatsApp voice notes are short,
  so this is ample.)
- **Completes when:** the transcript text (or `MSG.empty`) is sent — or an error/guard
  message is sent. Every failure path ends in exactly one `ctx.send`.
