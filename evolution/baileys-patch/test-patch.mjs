// Checks of the patched Baileys, run while the image is built (see Dockerfile). The socket's
// transport is swapped for a fake that records frames, so nothing reaches WhatsApp:
//  A. a top-level <status> stanza is answered with a plain <ack class="status" ...>;
//  B. after <ib><offline_preview>, <offline_batch> is asked for again every 5 s, and the
//     asking stops once <ib><offline> arrives.
// Against the unpatched rc.9, A sends nothing and B asks once. Exits non-zero on a failure.
import * as B from 'baileys';

const logger = {
  level: 'silent', child() { return logger; },
  trace() {}, debug() {}, info() {}, warn() {}, error() {}, fatal() {},
};
const sent = [];
const sock = B.makeWASocket({
  auth: { creds: B.initAuthCreds(), keys: { get: async () => ({}), set: async () => {} } },
  logger, printQRInTerminal: false, waWebSocketUrl: 'ws://127.0.0.1:9',
});
const fake = { readyState: 1, send: (d, cb) => { sent.push(Buffer.from(d)); cb && cb(); }, close() {} };
sock.ws.socket = fake;

const sleep = ms => new Promise(r => setTimeout(r, ms));
async function decoded() {
  const out = [];
  for (const frame of sent) {
    for (let i = 0; i < 12 && i + 3 <= frame.length; i++) {
      const len = (frame[i] << 16) | (frame[i + 1] << 8) | frame[i + 2];
      if (len === frame.length - i - 3) { out.push(await B.decodeBinaryNode(frame.subarray(i + 3))); break; }
    }
  }
  return out;
}
const batches = async () => (await decoded()).filter(n => n.tag === 'ib' && n.content?.[0]?.tag === 'offline_batch').length;
let failures = 0;
const check = (ok, what) => { console.log(`${ok ? 'PASS' : 'FAIL'}: ${what}`); if (!ok) failures++; };

// A. status ack
const status = { tag: 'status', attrs: { id: '2A94A8D1A75E4EDFB960F0BF4CE0A0C0', from: '5511999999999@s.whatsapp.net', type: 'media', offline: '1' }, content: [] };
sock.ws.emit('CB:status', status);
await sleep(300);
const ack = (await decoded()).find(n => n.tag === 'ack');
console.log('ack sent:', JSON.stringify(ack?.attrs ?? null));
check(!!ack && ack.attrs.class === 'status' && ack.attrs.id === status.attrs.id && ack.attrs.to === status.attrs.from &&
      ack.attrs.type === 'media' && !('error' in ack.attrs), 'plain status ack');

// B. offline batches (the refused real connection has closed the transport by now: put the fake back)
await sleep(500);
sock.ws.socket = fake;
console.log('transport open again:', sock.ws.isOpen);
sock.ws.emit('CB:ib,,offline_preview', { tag: 'ib', attrs: {}, content: [{ tag: 'offline_preview', attrs: { count: '5521', message: '1018', status: '186' } }] });
await sleep(300);
check(await batches() === 1, `first offline_batch at once (got ${await batches()})`);
await sleep(10500);
const afterTen = await batches();
check(afterTen === 3, `asked again every 5 s (3 after ~10.8 s, got ${afterTen})`);
sock.ws.emit('CB:ib,,offline', { tag: 'ib', attrs: {}, content: [{ tag: 'offline', attrs: { count: '5521' } }] });
await sleep(6000);
check(await batches() === afterTen, `stops once <ib><offline> arrives (got ${await batches()})`);

process.exit(failures ? 1 : 0);
