// ============================================================================
//  lib/sessions.js  —  per-chat CONVERSATION STATE (project-wide).
//  A "session" is a short-lived pending action keyed by remoteJid, so a follow-up
//  (confirmation, clarification, edit) can continue WITHOUT re-tagging @brain.
//
//  Generic on purpose: any skill can open/resume/clear a session. Shape is
//  skill-defined, but by convention:
//    { skill, intent, stage, data, expiresAt }
//
//  Backed by Redis (native TTL, survives brain restarts). Falls back to an
//  in-memory Map (with manual TTL) when REDIS_URL is unset/unreachable or the
//  `redis` package isn't installed — keeps local dev and no-Redis runs working.
//  Same interface either way. The `redis` dependency is imported dynamically so
//  the module loads fine without it.
// ============================================================================

const PREFIX = "brain:session:";
const DEFAULT_TTL = 15 * 60; // seconds

const nowSec = () => Math.floor(Date.now() / 1000);

// createSessions({ url, ttlSec }) -> { get, set, clear }
export function createSessions({ url, ttlSec = DEFAULT_TTL } = {}) {
  const mem = new Map(); // remoteJid -> { value, expiresAt } (fallback)
  let redis = null;

  if (url) {
    // Dynamic import: if `redis` isn't installed we just stay on memory.
    import("redis")
      .then(({ createClient }) => {
        redis = createClient({
          url,
          socket: {
            // give up after a few tries so local/no-Redis runs settle on memory
            reconnectStrategy: (retries) =>
              retries > 5 ? false : Math.min(retries * 200, 2000),
          },
        });
        redis.on("error", (e) =>
          console.error("sessions Redis error:", e.message)
        );
        redis.on("ready", () => console.log("sessions: Redis connected"));
        return redis.connect();
      })
      .catch((e) =>
        console.error("sessions: Redis unavailable, using memory:", e.message)
      );
  }

  const live = () => redis && redis.isReady;

  function memGet(jid) {
    const e = mem.get(jid);
    if (!e) return null;
    if (e.expiresAt <= nowSec()) {
      mem.delete(jid);
      return null;
    }
    return e.value;
  }

  return {
    async get(jid) {
      if (live()) {
        try {
          const s = await redis.get(PREFIX + jid);
          return s ? JSON.parse(s) : null;
        } catch (e) {
          console.error("sessions get fell back to memory:", e.message);
        }
      }
      return memGet(jid);
    },

    async set(jid, value, ttl = ttlSec) {
      const withMeta = { ...value, expiresAt: nowSec() + ttl };
      if (live()) {
        try {
          await redis.set(PREFIX + jid, JSON.stringify(withMeta), { EX: ttl });
          return;
        } catch (e) {
          console.error("sessions set fell back to memory:", e.message);
        }
      }
      mem.set(jid, { value: withMeta, expiresAt: nowSec() + ttl });
    },

    async clear(jid) {
      if (live()) {
        try {
          await redis.del(PREFIX + jid);
        } catch (e) {
          console.error("sessions clear (redis) failed:", e.message);
        }
      }
      mem.delete(jid);
    },
  };
}
