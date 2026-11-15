# ============================================================================
#  Evolution API with its Baileys 7.0.0-rc.9 patched (see patch-baileys.cjs).
#
#  Without the patch the instance says "open" and sends fine, but incoming
#  messages only arrive in a burst after each reconnect, about every 50 minutes,
#  further and further behind (25 h on 2026-11-14): WhatsApp's top-level
#  <status> stanzas are never acked, and only one batch of 100 queued messages
#  is ever asked for. evolution-api #2778, waha #2349. Neither fix is released
#  upstream yet: drop this image once Evolution ships a Baileys that has both.
#
#  The base is pinned to the exact image that ran before the patch. The build
#  fails if the patch does not apply (other Baileys version, anchor moved) or if
#  the patched library does not ack a status stanza and keep asking for batches.
# ============================================================================
FROM evoapicloud/evolution-api@sha256:966625532d9076a2381e973a271307d107e6f070450de3abeeea8bd18be07252

COPY patch-baileys.cjs /patch/patch-baileys.cjs
COPY test-patch.mjs /evolution/test-patch.mjs
RUN node /patch/patch-baileys.cjs /evolution/node_modules/baileys \
 && node --check /evolution/node_modules/baileys/lib/Socket/messages-recv.js \
 && node --check /evolution/node_modules/baileys/lib/Socket/socket.js \
 && cd /evolution && timeout 60 node test-patch.mjs \
 && rm /evolution/test-patch.mjs
