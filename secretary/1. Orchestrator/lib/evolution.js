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

  // Fetches a conversation's history and normalizes to { t, fromMe, text, pushName }.
  async function fetchHistory(remoteJid) {
    try {
      const res = await fetch(`${base}/chat/findMessages/${instance}`, {
        method: "POST",
        headers,
        body: JSON.stringify({ where: { key: { remoteJid } } }),
      });
      if (!res.ok) return [];
      const data = await res.json();
      const recs = Array.isArray(data)
        ? data
        : data?.messages?.records || data?.records || [];
      return recs.map((r) => ({
        t: Number(r.messageTimestamp) || 0,
        fromMe: r.key?.fromMe,
        text: extractText(r.message).trim(),
        pushName: r.pushName,
      }));
    } catch {
      return [];
    }
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
