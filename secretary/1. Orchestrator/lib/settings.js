// ============================================================================
//  lib/settings.js  —  DURABLE SETTINGS the owner can change by asking.
//  A session (lib/sessions.js) is a short-lived pending action; a SETTING outlives
//  the process and has no TTL. Same Redis, same graceful degradation, different key
//  space (secretary:settings:) and no expiry.
//
//  Today it holds one thing: the tag list the owner summons her with, changed via the
//  `assistant_settings` skill. SECRETARY_TAG is the SEED; a stored value wins.
//
//  Backed by Redis (the product already runs it with --appendonly yes on a named
//  volume, so it survives restart AND redeploy). Falls back to an in-memory Map when
//  REDIS_URL is unset/unreachable or the `redis` package isn't installed — the change
//  then holds for the process lifetime only, boot falls back to the SECRETARY_TAG seed,
//  and saveTags() SAYS SO by returning false. Which is also why a bad tag can never
//  become permanent by accident.
//
//  TWO THINGS HERE ARE LOAD-BEARING, and both are about telling the truth:
//
//  `ready` — sessions.js fires its connect WITHOUT awaiting it, so live() is false for
//  the first moments after boot. That is harmless for a session (nothing is pending at
//  boot) and fatal for a setting: server.js reads the stored tags AT BOOT, and without
//  something to await, that read races the connection, misses the stored value and
//  silently falls back to the env seed. The feature would look like it works and then
//  forget the tag on every restart. `ready` resolves — never rejects — once Redis is
//  connected or has given up, and boot awaits it.
//
//  `saveTags` returns true ONLY when the store actually acknowledged the write, false
//  when it only reached the memory fallback. The skill's success message hangs off that
//  boolean: it is what prevents her from ever reporting a change she did not persist.
// ============================================================================

const PREFIX = "secretary:settings:";
const TAGS_KEY = "tags";

// createSettings({ url }) -> { ready, loadTags, saveTags }
export function createSettings({ url } = {}) {
  const mem = new Map(); // key -> value (fallback; no TTL — this is a setting)
  let redis = null;

  // Resolves once Redis is connected OR has given up. NEVER rejects: a settings store
  // that cannot reach Redis is a degraded store, not a crashed boot.
  const ready = !url
    ? Promise.resolve(false)
    : import("redis")
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
            console.error("settings Redis error:", e.message)
          );
          redis.on("ready", () => console.log("settings: Redis connected"));
          return redis.connect().then(() => true);
        })
        .catch((e) => {
          console.error(
            "settings: Redis unavailable, using memory:",
            e.message
          );
          return false;
        });

  const live = () => redis && redis.isReady;

  return {
    ready,

    // The stored tag list, or null if nothing has ever been stored (-> use the seed).
    async loadTags() {
      if (live()) {
        try {
          const s = await redis.get(PREFIX + TAGS_KEY);
          if (s) {
            const parsed = JSON.parse(s);
            if (Array.isArray(parsed) && parsed.length) return parsed;
          }
          return null;
        } catch (e) {
          console.error("settings loadTags fell back to memory:", e.message);
        }
      }
      return mem.get(TAGS_KEY) || null;
    },

    // TRUE only if the store really took it. The memory fallback still HOLDS the value
    // (so the change is live for this process) but reports false — she must never claim
    // a persistence she did not get.
    async saveTags(list) {
      mem.set(TAGS_KEY, list);
      if (live()) {
        try {
          await redis.set(PREFIX + TAGS_KEY, JSON.stringify(list));
          return true;
        } catch (e) {
          console.error("settings saveTags fell back to memory:", e.message);
        }
      }
      return false;
    },
  };
}
