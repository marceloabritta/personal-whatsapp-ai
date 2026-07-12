# Skill: `transcribe_audio`

> **For humans — quick read.**
>
> Turns a WhatsApp voice message into text.
>
> **It handles one task:** transcribe a voice note you **reply to**.
>
> **How you call it:** press-and-hold the voice message → **Reply** → type
> `@secretary transcribe`. It downloads that audio, transcribes it, and sends you the text.
> Takes roughly up to a minute for short notes.
>
> A **short audio (≤ 2 min)** comes back as text in the chat; a **long one (> 2 min)** comes
> back as a `.txt` file you can open and save, so the thread isn't flooded.
>
> If you didn't reply to an audio, it tells you how to do it.

## What you'll see (the full conversation)

Every secretary message is prefixed with the language-aware header — `[Marcelo's AI Secretary]:`
in English, `[Secretaria IA do Marcelo]:` in Portuguese (from `headerFor(lang)`) — and a blank line. This skill is
one-shot — it sends **exactly one message**: the result. There is **no "transcribing…"
acknowledgment**, and it does **not** wait for or pick up any follow-up answer.

**Normal run — short audio (≤ 2 min):**
1. You reply to a voice note with `@secretary transcribe`.
2. The secretary sends the transcript inline:
   > Here is the transcribed audio:
   >
   > &lt;the transcribed text&gt;
   - If the audio was silent/too short: *"I transcribed it, but no text came out (silent or very short audio)."*

**Normal run — long audio (> 2 min):** you get a `.txt` document
(`audio-transcript-<timestamp>.txt`) carrying the full transcript, captioned:
> The audio is long so I transcribed it onto a file. Here it is.

**Instead of the above, you may see one of these and nothing more:**
- Not a reply to an audio: *"To transcribe, reply to the audio you want and call @secretary
  again. E.g.: press and hold the audio, tap Reply and type \"@secretary transcribe\"."*
- Couldn't download it (too old / not saved): *"I couldn't download that audio from
  WhatsApp. It may be too old or was not saved. Try a more recent audio."*
- Transcription failed (or API key missing): *"I downloaded the audio, but the
  transcription failed. Error in the log. Try again?"*

---

## For AI / maintainers — detailed

Source: `skill.js` (logic) + `prompt.js` (reply strings — **no LLM prompts**;
this skill makes **no Claude call**). Contract: `export const manifest` +
`export async function run(ctx)`; auto-discovered at boot.

**Localization:** `prompt.js` holds `MSG` as a per-language map (`{ en, pt }`) plus the
`transcript` label; `skill.js` selects the set with `const M = msg(ctx.lang)` (fallback
`en`) and sends `M.noAudio(tag)` / `M.transcript(text)` / `M.longAudio` (the file caption) /
etc. The "what you'll see" strings above are the **en** copy — replies mirror the chat
language, and any language without a map is translated from `en` by the orchestrator's
`send()` fallback (the transcript text itself is the audio's own words and isn't localized).

### How it's invoked
The router classifies an `@secretary` order as `transcribe_audio` — disambiguated by
`ctx.hasQuotedAudio` (the orchestrator tells the router when the replied-to message is a
voice note). The orchestrator then calls `run(ctx)`. **Single-shot; never stateful.**

### What it receives (`ctx`)
Uses: `number` (reply target), `quoted` (`{id,hasAudio,mediaType,text,calendarLink}` —
here `quoted.id` = the voice message id, `quoted.hasAudio` = true for a PTT/audio),
`env` (`ASSEMBLYAI_API_KEY`, `ASSEMBLYAI_LANGUAGE`), `evolution` (media download **and**
`sendMedia` for the long-audio file), `send(number,text)` (reply), `lang`, `tag` (for the
"how to call me" hint).

### Control flow — `run(ctx)`
1. **Guard:** if `!quoted || !quoted.hasAudio` → send `MSG.noAudio(tag)` and stop.
2. **Key check:** if `env.ASSEMBLYAI_API_KEY` is missing → `MSG.transcriptionFailed`, stop.
3. **Download (Evolution):** `evolution.getMediaBase64(quoted.id)` → `{ base64, mimetype }`.
   On error → `MSG.downloadFailed`, stop. *(Requires Evolution
   `DATABASE_SAVE_DATA_NEW_MESSAGE=true`; otherwise old audios 404.)*
4. **Transcribe (AssemblyAI)**, wrapped in try/catch — **no ack message is sent first**; the
   result is the skill's only message:
   - `Buffer.from(base64, "base64")` → raw bytes.
   - **`aaiUpload(apiKey, buffer)`** → `POST /v2/upload` (raw bytes) → `upload_url`.
   - **`aaiTranscribe(apiKey, uploadUrl, lang || env.ASSEMBLYAI_LANGUAGE || "en")`**:
     the transcription language now follows the detected `ctx.lang` first (a PT chat
     transcribes in PT), then the static env, then English.
     `POST /v2/transcript` `{ audio_url, language_code }` → `{ id }`, then **poll**
     `GET /v2/transcript/{id}` **up to 40 times, every 3 s (~2 min max)** until
     `status === "completed"` → returns **`{ text, durationSec }`** (`durationSec` =
     AssemblyAI's `audio_duration`, the length of the audio itself — not the poll time);
     `status === "error"` throws; exhausting the loop throws `"AAI timeout"`.
   - Any throw (upload/create/poll/timeout) → caught → `M.transcriptionFailed`.
5. **Deliver** — `clean = text.trim()`; blank → `M.empty` and stop. Otherwise the delivery
   format is chosen by **audio length**, `LONG_AUDIO_SEC = 120` (a `skill.js` const):
   - **`durationSec > 120` → `.txt` document.** `sendTranscriptFile()` base64s the transcript
     and calls `evolution.sendMedia` with `mediatype:"document"`, `mimetype:"text/plain"`,
     `fileName: audio-transcript-<UTC yyyy-mm-dd-hh-mm>.txt`, and a caption of
     `frame(headerFor(lang), M.longAudio)` — `sendMedia` bypasses `ctx.send`, so the skill
     frames the caption itself (same rule as `feature_request`).
   - **Otherwise → inline** `M.transcript(clean)` through `ctx.send`, italic body like every
     other reply.
   - **`durationSec` null** (AssemblyAI didn't report one) reads as short → inline.
   - **The file send failing falls back to the inline transcript** (`sendTranscriptFile`
     returns false): a wall of text beats losing the transcript the owner asked for.

### External APIs
- **Evolution:** `getMediaBase64` → `POST /chat/getBase64FromMediaMessage/{instance}`
  `{message:{key:{id}}}` → decrypted `{ base64, mimetype }`; `sendMedia` →
  `POST /message/sendMedia/{instance}` for the long-audio `.txt`.
- **AssemblyAI:** `POST /v2/upload`, `POST /v2/transcript`, `GET /v2/transcript/{id}`
  (poll). Language from `ASSEMBLYAI_LANGUAGE` (default `"en"`; set `"pt"` for Portuguese).
- **No Anthropic/LLM call.** Every WhatsApp send goes through `ctx.send`, **except** the
  long-audio document, which must use `evolution.sendMedia` directly.
- Note: audio bytes leave the droplet for AssemblyAI (a US service) — the one point where
  the self-hosted privacy model is broken (a self-hosted Whisper is the alternative).

### Stateful behavior, timeouts, completion
- **Stateful:** never — it opens no session and returns after one pass.
- **Timeout:** `aaiTranscribe` polls ~2 min (40 × 3 s); if the transcript isn't ready it
  throws and the owner gets `MSG.transcriptionFailed`. (WhatsApp voice notes are short,
  so this is ample.)
- **Completes when:** the transcript is delivered (inline or as the `.txt`), or `MSG.empty` /
  an error/guard message is sent. Every path ends in exactly one message to the owner.
