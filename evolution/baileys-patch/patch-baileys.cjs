// Patch Baileys 7.0.0-rc.9 inside the Evolution image. Three fixes, all for the same symptom:
// the instance says "open", sending works, and incoming messages only arrive in a burst after
// each reconnect (about every 50 minutes), further and further behind.
//
// 1. messages-recv.js: ack top-level <status> stanzas (evolution-api #2778, Baileys #2883).
//    WhatsApp sends contacts' status updates this way since late 2026-09. rc.9 never acks them,
//    so the server holds inbound messages and ends the stream ~50 min later with
//    <stream:error><ack class="status"/>. A plain ack, no error code.
// 2. messages-recv.js: keep the offline queue alive when one node throws. rc.9 awaits each
//    offline node with no try/catch, so one throw ends the loop with isProcessing stuck at true.
// 3. socket.js: keep asking for offline batches until <ib><offline> arrives (waha #2349,
//    Baileys #2810). rc.9 asks for one batch of 100 and never again, and the server holds live
//    messages behind an unfinished offline queue. Ask again every 5 s; if the queue still has
//    not finished after the cap, end the connection so the reconnect resumes it.
//
// Refuses to run on any other Baileys version, or if an anchor is missing or repeated.
'use strict';
const fs = require('fs');
const path = require('path');

const root = process.argv[2] || '/evolution/node_modules/baileys';
const version = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')).version;
if (version !== '7.0.0-rc.9') {
  console.error(`patch-baileys: Baileys ${version}, expected 7.0.0-rc.9; refusing`);
  process.exit(1);
}

function patchFile(rel, marker, edits) {
  const file = path.join(root, rel);
  let src = fs.readFileSync(file, 'utf8');
  if (src.includes(marker)) {
    console.log(`patch-baileys: ${rel} already patched`);
    return;
  }
  for (const [anchor, replacement, label] of edits) {
    const n = src.split(anchor).length - 1;
    if (n !== 1) {
      console.error(`patch-baileys: ${rel}: anchor "${label}" found ${n} times, expected 1; refusing`);
      process.exit(1);
    }
    src = src.replace(anchor, () => replacement);
  }
  fs.writeFileSync(file, src);
  console.log(`patch-baileys: patched ${rel}`);
}

patchFile('lib/Socket/messages-recv.js', "ws.on('CB:status'", [
  [
    "    ws.on('CB:notification', async (node) => {\n",
    "    // patch-baileys: ack top-level <status> stanzas (evolution-api #2778, Baileys #2883)\n" +
    "    ws.on('CB:status', (node) => {\n" +
    "        sendMessageAck(node).catch(error => onUnexpectedError(error, 'acking status stanza'));\n" +
    "    });\n" +
    "    ws.on('CB:notification', async (node) => {\n",
    'CB:notification',
  ],
  [
    "                    await nodeProcessor(node);\n",
    "                    // patch-baileys: one failing offline node must not stop the queue\n" +
    "                    try {\n" +
    "                        await nodeProcessor(node);\n" +
    "                    }\n" +
    "                    catch (error) {\n" +
    "                        onUnexpectedError(error, `processing offline ${type}`);\n" +
    "                    }\n",
    'offline nodeProcessor',
  ],
]);

patchFile('lib/Socket/socket.js', 'patch-baileys: offline batches', [
  [
    "    ws.on('CB:ib,,offline_preview', async (node) => {\n" +
    "        logger.info('offline preview received', JSON.stringify(node));\n" +
    "        await sendNode({\n" +
    "            tag: 'ib',\n" +
    "            attrs: {},\n" +
    "            content: [{ tag: 'offline_batch', attrs: { count: '100' } }]\n" +
    "        });\n" +
    "    });\n",
    "    // patch-baileys: offline batches. Keep asking until <ib><offline> (waha #2349, Baileys #2810)\n" +
    "    let offlineBatchTimer;\n" +
    "    let offlineQueueDone = false;\n" +
    "    ws.on('CB:ib,,offline_preview', async (node) => {\n" +
    "        const preview = getBinaryNodeChild(node, 'offline_preview');\n" +
    "        logger.info({ preview: preview?.attrs }, 'offline preview received');\n" +
    "        const previewCount = +(preview?.attrs?.count || 0);\n" +
    "        const maxRequests = Math.max(200, Math.ceil(previewCount / 50) + 20);\n" +
    "        let requests = 0;\n" +
    "        let inFlight = false;\n" +
    "        clearInterval(offlineBatchTimer);\n" +
    "        const tick = async () => {\n" +
    "            if (offlineQueueDone || !ws.isOpen) {\n" +
    "                clearInterval(offlineBatchTimer);\n" +
    "                return;\n" +
    "            }\n" +
    "            if (requests >= maxRequests) {\n" +
    "                clearInterval(offlineBatchTimer);\n" +
    "                logger.warn({ requests, previewCount }, 'offline queue did not finish, reconnecting');\n" +
    "                end(new Boom('Offline queue did not finish', { statusCode: DisconnectReason.connectionClosed }));\n" +
    "                return;\n" +
    "            }\n" +
    "            if (inFlight) {\n" +
    "                return;\n" +
    "            }\n" +
    "            inFlight = true;\n" +
    "            requests++;\n" +
    "            try {\n" +
    "                await sendNode({ tag: 'ib', attrs: {}, content: [{ tag: 'offline_batch', attrs: { count: '100' } }] });\n" +
    "            }\n" +
    "            catch (error) {\n" +
    "                logger.warn({ error }, 'offline batch request failed');\n" +
    "            }\n" +
    "            finally {\n" +
    "                inFlight = false;\n" +
    "            }\n" +
    "        };\n" +
    "        await tick();\n" +
    "        offlineBatchTimer = setInterval(() => { tick(); }, 5000);\n" +
    "    });\n",
    'CB:ib,,offline_preview',
  ],
  [
    "    ws.on('CB:ib,,offline', (node) => {\n",
    "    ws.on('CB:ib,,offline', (node) => {\n" +
    "        offlineQueueDone = true; // patch-baileys: stop asking for offline batches\n" +
    "        clearInterval(offlineBatchTimer);\n",
    'CB:ib,,offline',
  ],
]);
