// ============================================================================
//  lib/evolution.js  —  Evolution API client (WhatsApp gateway).
//  Send text, fetch history and download decrypted media (base64).
//  Shared by every skill.
// ============================================================================
import { extractText } from "./whatsapp.js";

export function createEvolution({ url, apikey, instance }) {
  const base = `${url}`;
  const headers = { "Content-Type": "application/json", apikey };

  // Sends RAW text (no header). The secretary's header framing
  // is the orchestrator's job (server.js), not this client's.
  async function sendText(number, text) {
    const res = await fetch(`${base}/message/sendText/${instance}`, {
      method: "POST",
      headers,
      body: JSON.stringify({ number, text }),
    });
    if (!res.ok) console.error("sendText failed", res.status, await res.text());
    return res.ok;
  }

  // Sends a MEDIA message (document / image / …) as base64. Used by feature_request
  // to deliver the generated spec as a real, saveable `.md` document
  // (mediatype:"document", mimetype:"text/markdown"). Like sendText, the secretary's
  // header framing is the CALLER's job — pass it inside `caption`. Returns res.ok.
  async function sendMedia(number, { mediatype, mimetype, media, fileName, caption }) {
    const res = await fetch(`${base}/message/sendMedia/${instance}`, {
      method: "POST",
      headers,
      body: JSON.stringify({ number, mediatype, mimetype, media, fileName, caption }),
    });
    if (!res.ok) console.error("sendMedia failed", res.status, await res.text());
    return res.ok;
  }

  // One findMessages page. Returns [] on any failure, so one bad query can never
  // take down the other in fetchHistory.
  async function findMessages(where) {
    try {
      const res = await fetch(`${base}/chat/findMessages/${instance}`, {
        method: "POST",
        headers,
        body: JSON.stringify({ where }),
      });
      if (!res.ok) return [];
      const data = await res.json();
      return Array.isArray(data)
        ? data
        : data?.messages?.records || data?.records || [];
    } catch {
      return [];
    }
  }

  // Fetches a conversation's history and normalizes to { t, fromMe, text, pushName }.
  //
  // WhatsApp LID addressing: in a 1:1 chat, inbound messages are persisted under the
  // contact's `…@lid` JID, while the JID the webhook hands us (and that we send to) is
  // the phone `…@s.whatsapp.net`. Querying `remoteJid` alone therefore returns nothing
  // but our OWN outbound messages — the secretary reading its own voice back. Evolution
  // records the phone JID on those LID rows as `key.remoteJidAlt`, so we ask both ways
  // and merge. Group chats (@g.us) match on the first query and no-op on the second.
  // `combine` (whatsapp.js) dedupes the overlap by timestamp+text.
  async function fetchHistory(remoteJid) {
    const pages = await Promise.all([
      findMessages({ key: { remoteJid } }),
      findMessages({ key: { remoteJidAlt: remoteJid } }),
    ]);
    return pages.flat().map((r) => ({
      t: Number(r.messageTimestamp) || 0,
      fromMe: r.key?.fromMe,
      text: extractText(r.message).trim(),
      pushName: r.pushName,
    }));
  }

  // Downloads a message's decrypted media (by id) and returns { base64, mimetype }.
  // Requires Evolution to be persisting messages (DATABASE_SAVE_DATA_NEW_MESSAGE=true,
  // the default); otherwise the message is not found and the endpoint returns 400.
  async function getMediaBase64(messageId, { convertToMp4 = false } = {}) {
    const res = await fetch(
      `${base}/chat/getBase64FromMediaMessage/${instance}`,
      {
        method: "POST",
        headers,
        body: JSON.stringify({
          message: { key: { id: messageId } },
          convertToMp4,
        }),
      }
    );
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      throw new Error(`getBase64FromMediaMessage ${res.status}: ${body}`);
    }
    const data = await res.json();
    if (!data?.base64) throw new Error("Response has no base64 field");
    return { base64: data.base64, mimetype: data.mimetype || "audio/ogg" };
  }

  return { sendText, sendMedia, fetchHistory, getMediaBase64 };
}
