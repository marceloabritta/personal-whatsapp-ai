// ============================================================================
//  Skill "Calendar Actions" — LOGIC.  CONVERTED (pure task, read-then-act).
//  In the NEW (@mary) flow the ORCHESTRATOR runs the conversation and hands a validated
//  payload in ctx.info. run() is a pure dispatch on ctx.info.action:
//    - find   (READ): gather candidate events matching query / start / participants; send
//                     NOTHING; return { candidates:[{event_id,title,start,end,attendees,link}],
//                     count } — the model reads them back and proposes.
//    - list   (READ): send the rendered schedule AND return the same items structured (each
//                     carrying event_id) so a follow-up edit/delete can target one.
//    - create (ACT):  build the event from info, write it to Google, send createDone, return
//                     { ok, link, eventId }.
//    - edit   (ACT):  fetch info.event_id, overlay the change fields, write it, send editDone,
//                     return { ok, eventId }.
//    - delete (ACT):  cancel info.event_id (+ its confirmed duplicates), send deleteCancelled,
//                     return { ok, cancelled }.
//  There is NO in-skill propose/confirm/classify, NO session, NO capabilities export
//  (startCreate is gone — the model chains calendar_action itself), and NO classifyConfirmation
//  import. The deterministic Google/RRULE/all-day helpers are kept verbatim.
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description, conversation, inputs }
//    export async function run(ctx) -> a JSON-serializable value (the read-back)
// ============================================================================
import { google } from "googleapis";
import {
  buildExtractionRules,
  reply,
  localizeWhen,
  describeRecurrence,
} from "./prompt.js";
import { googleAuth } from "../../1. Orchestrator/lib/google.js";

// `inputs` — THE DECLARED INPUT CONTRACT the orchestrator's merged router+extractor call fills
// (lib/inputs.js). `action` is the discriminator; a READ value (find / list) carries the
// completeness it needs and no more, an ACT value (create / edit / delete) carries what it must
// have to act. Every non-discriminator field is NULLABLE, so a READ payload — every field but
// `action` nulled — is shape-valid and passes the dispatch gate.
export const manifest = {
  id: "calendar_action",
  // CONVERTED (pure task): the model runs the dialogue and proposes before an ACT; run() acts.
  conversation: "orchestrator",
  description:
    "create, edit/reschedule, or delete/cancel a meeting or event in Google Calendar and notify " +
    "the participants; also read/list what's on the calendar, or FIND a specific event to act on " +
    "(answer questions like what's on tomorrow, anything Friday afternoon, or what's my next " +
    "meeting)",
  inputs: {
    discriminator: "action",
    fields: {
      action: {
        type: "enum",
        enum: ["find", "list", "create", "edit", "delete", "other"],
        desc: "what to do — find/list READ the calendar; create/edit/delete WRITE to it",
      },
      query: {
        type: "string",
        nullable: true,
        desc: 'free-text description of the event to FIND (action="find") — its topic/who/when',
      },
      event_id: {
        type: "string",
        nullable: true,
        desc: 'the target event id, read back from a find/list (required for action="edit"/"delete")',
      },
      title: { type: "string", nullable: true, desc: "the event's short calendar heading" },
      participants: {
        type: "array",
        nullable: true,
        of: {
          name: { type: "string", nullable: true },
          email: { type: "email", nullable: true },
        },
        desc: "attendees besides the owner; email null if it is NOT in the conversation — NEVER invent one",
      },
      start_iso: {
        type: "iso",
        nullable: true,
        desc: "ISO-8601 with the -03:00 offset (create: the first occurrence; find: the start to match)",
      },
      duration_min: { type: "number", nullable: true, desc: "minutes; null -> 45 default" },
      all_day: {
        type: "bool",
        nullable: true,
        desc: 'true ONLY when the order says the event takes the WHOLE DAY ("o dia inteiro", "o dia todo", "all day") instead of starting at a time. STILL fill start_iso — the FIRST day, 00:00, -03:00. duration_min is then ignored. If a TIME is given ("amanhã 10h"), all_day = false.',
      },
      all_day_end_iso: {
        type: "iso",
        nullable: true,
        desc: 'ONLY for an all-day RANGE spanning several days ("de segunda a quarta", "a semana toda"). The LAST day the event STILL COVERS — INCLUSIVE: for "segunda a quarta" it is WEDNESDAY, not Thursday. Do NOT add a day. 00:00 with the -03:00 offset. null for a single all-day day, and null whenever all_day is false.',
      },
      summary: { type: "string", nullable: true, desc: "a longer one-line agenda for the event body" },
      list_mode: {
        type: "enum",
        enum: ["window", "next"],
        nullable: true,
        desc: 'action="list" ONLY, else null',
      },
      range_start_iso: { type: "iso", nullable: true, desc: 'action="list" ONLY, else null' },
      range_end_iso: { type: "iso", nullable: true, desc: 'action="list" ONLY, else null' },
      recurrence: {
        type: "object",
        nullable: true,
        desc: 'the repeat rule for a RECURRING create, else null (one-off — the default). Object {freq: "daily"|"weekly"|"monthly", interval: number|null, byday: ["MO".."SU"]|null (weekly only), count: number|null, until: ISO-8601 -03:00|null}. count XOR until, never both.',
      },
      location: {
        type: "string",
        nullable: true,
        desc: "WHERE — a VERBATIM physical address/venue/room, copied word-for-word, never looked up; null if none",
      },
      virtual: {
        type: "bool",
        nullable: true,
        desc: "true iff a Google Meet video call; XOR with location — video wins",
      },
    },
    // A READ (find/list) is completeness-valid as soon as its discriminator is set (list also
    // needs a list_mode to choose window/next). An ACT names its target: create needs a date +
    // an email for every named guest; edit/delete need the event_id read back from a find/list.
    requiredWhen: {
      find: [],
      list: ["list_mode"],
      create: ["start_iso", "participants[].email"],
      edit: ["event_id"],
      delete: ["event_id"],
      other: [],
    },
    consistency: [
      {
        name: "attendee_count_matches_email_count",
        test: (i) =>
          i.action !== "create" ||
          !Array.isArray(i.participants) ||
          i.participants.every(
            (p) => p && p.email != null && String(p.email).trim() !== ""
          ),
      },
      {
        name: "create_always_has_a_date",
        test: (i) => i.action !== "create" || !!i.start_iso,
      },
      {
        // Physical XOR virtual — a place is one or the other, never both. (normalizeLocation
        // enforces "video wins" at merge time; this rejects an already-contradictory payload.)
        name: "location_virtual_xor",
        test: (i) => !(i.location && String(i.location).trim() && i.virtual === true),
      },
      {
        // The LIST WINDOW: a window cannot end before it starts.
        name: "end_after_start",
        test: (i) =>
          !(i.range_start_iso && i.range_end_iso) ||
          Date.parse(i.range_end_iso) >= Date.parse(i.range_start_iso),
      },
      {
        // An edit must actually CHANGE something — else there is nothing to write.
        name: "edit_has_a_change",
        test: (i) =>
          i.action !== "edit" ||
          !!(
            (i.title && String(i.title).trim()) ||
            i.start_iso ||
            Number(i.duration_min) > 0 ||
            (typeof i.summary === "string" && i.summary.trim()) ||
            i.all_day === true ||
            i.all_day_end_iso ||
            i.recurrence ||
            (Array.isArray(i.participants) && i.participants.length) ||
            (i.location && String(i.location).trim()) ||
            i.virtual === true
          ),
      },
      {
        name: "window_list_has_a_range",
        test: (i) =>
          !(i.action === "list" && i.list_mode === "window") || !!i.range_start_iso,
      },
    ],
    // Carried VERBATIM into the merged prompt. Same env + default as server.js's OWNER_NAME.
    rulebook: () => buildExtractionRules(process.env.OWNER_NAME || "User"),
  },
};

const CAL_TZ = "America/Sao_Paulo";

// The longest all-day span we will create from one order. A range is CLAMPED to it.
const MAX_ALL_DAY_DAYS = 31;

// ---- all-day day arithmetic (the ONE place dates are computed) ----------------
// `YYYY-MM-DD` in CAL_TZ — the same Intl.DateTimeFormat("en-CA") trick endOfLocalDay uses.
function localDayStr(ms) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: CAL_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(ms));
}

// The local day of an ISO instant, or null if it is missing/unparseable.
function dayOfIso(iso) {
  const ms = Date.parse(iso || "");
  return Number.isFinite(ms) ? localDayStr(ms) : null;
}

// Shift a `YYYY-MM-DD` by n days. NOON, not midnight — cheap insurance against offset
// arithmetic landing on the wrong side of a day boundary.
function addDays(day, n) {
  return localDayStr(Date.parse(`${day}T12:00:00-03:00`) + n * 86400000);
}

// THE INCLUSIVE -> EXCLUSIVE CONVERSION HAPPENS HERE, AND ONLY HERE. The draft (and the
// model, and the confirm bubble) speak INCLUSIVE days: all_day_end_iso is the last day the
// event still covers. Google's `end.date` is EXCLUSIVE.
function allDayWireDates(draft) {
  const start_date = localDayStr(new Date(draft.start_iso).getTime());
  const last_date = draft.all_day_end_iso
    ? localDayStr(new Date(draft.all_day_end_iso).getTime())
    : start_date;
  return { start_date, end_date: addDays(last_date, 1) }; // end_date EXCLUSIVE
}

// The two ALL-DAY sanity clamps, in ONE place. Returns the sanitized INCLUSIVE `all_day_end_iso`.
function normalizeAllDay(start_iso, all_day, all_day_end_iso) {
  let end = all_day ? all_day_end_iso || null : null;
  if (!end) return null;
  const startDay = dayOfIso(start_iso);
  const endDay = dayOfIso(end);
  if (!startDay || !endDay || endDay <= startDay) return null;
  const span =
    Math.round(
      (Date.parse(`${endDay}T12:00:00-03:00`) - Date.parse(`${startDay}T12:00:00-03:00`)) /
        86400000
    ) + 1;
  if (span > MAX_ALL_DAY_DAYS) {
    end = `${addDays(startDay, MAX_ALL_DAY_DAYS - 1)}T00:00:00-03:00`;
  }
  return end;
}

// ---- RRULE compiler (recurring create) --------------------------------------
// The deterministic layer that turns the model's structured recurrence object into the exact
// RRULE string written to Google. `toRRule` is the SINGLE validator of a recurrence — an
// uncompilable / degenerate / past-until object falls back to null (a one-off). Kept per the
// recurring-events work (PREFLIGHT §2).
export function toRRule(rec, { allDay = false, startIso = null } = {}) {
  if (!rec || typeof rec !== "object") return null;
  const FREQ = { daily: "DAILY", weekly: "WEEKLY", monthly: "MONTHLY" }[rec.freq];
  if (!FREQ) return null; // unknown/absent freq -> one-off
  const parts = [`FREQ=${FREQ}`];

  const interval = Number(rec.interval);
  if (Number.isFinite(interval) && interval > 1) parts.push(`INTERVAL=${interval}`); // 0/1/missing -> default 1

  if (FREQ === "WEEKLY" && Array.isArray(rec.byday)) { // BYDAY on WEEKLY only; never ordinal
    const ORDER = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
    const set = new Set(rec.byday.map((d) => String(d).toUpperCase()));
    const codes = ORDER.filter((d) => set.has(d)); // dedup + canonical order
    if (codes.length) parts.push(`BYDAY=${codes.join(",")}`);
  }

  const count = Number(rec.count);
  if (Number.isFinite(count) && count > 0) {
    parts.push(`COUNT=${count}`); // COUNT wins, UNTIL dropped — RRULE forbids both
  } else if (rec.until) {
    const startMs = Date.parse(startIso || "");
    const untilMs = Date.parse(rec.until);
    if (!Number.isFinite(untilMs)) return null; // unparseable until -> one-off
    if (Number.isFinite(startMs) && untilMs <= startMs) return null; // past-until -> one-off
    const tok = toRRuleUntil(rec.until, allDay);
    if (!tok) return null;
    parts.push(`UNTIL=${tok}`);
  }
  return `RRULE:${parts.join(";")}`;
}

// Value-type-correct RRULE UNTIL token (RFC 5545: UNTIL must match DTSTART's value type).
export function toRRuleUntil(untilIso, allDay = false) {
  const ms = Date.parse(untilIso || "");
  if (!Number.isFinite(ms)) return null;
  const day = localDayStr(ms); // "YYYY-MM-DD" in CAL_TZ (existing helper)
  if (allDay) return day.replace(/-/g, ""); // DATE form: YYYYMMDD
  const endMs = Date.parse(`${day}T23:59:59-03:00`); // inclusive end of the local until-day
  return new Date(endMs).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z"); // YYYYMMDDTHHMMSSZ
}

// The READ direction: the all-day shape of a REAL Google event, in the house's own terms.
function allDayFromEvent(ev) {
  const all_day = !ev?.start?.dateTime && !!ev?.start?.date;
  if (!all_day) {
    return { all_day: false, start_iso: ev?.start?.dateTime || null, all_day_end_iso: null };
  }
  const startDay = ev.start.date;
  const lastDay = ev.end?.date ? addDays(ev.end.date, -1) : startDay; // EXCLUSIVE -> INCLUSIVE
  return {
    all_day: true,
    start_iso: `${startDay}T00:00:00-03:00`,
    all_day_end_iso: lastDay > startDay ? `${lastDay}T00:00:00-03:00` : null,
  };
}

// ---- LOCATION (physical XOR virtual) ----------------------------------------
// Location rides the draft as TWO coupled fields, treated exactly like all_day / recurrence:
//   location: string|null  — the VERBATIM physical address (outer-trimmed, never looked up)
//   virtual:  boolean      — true iff the event is a Google Meet video call
// normalizeLocation is the SOLE enforcer of the XOR: virtual wins, then a non-empty address
// means physical, everything else is "no location". Every create merge path funnels through
// it (draftFromInfo), so the invariant is decided in exactly one place — the same "one place
// does the arithmetic" discipline normalizeAllDay uses. Exported for the offline selftest.
// Ported verbatim from the @assistant flow (secretary/2. Skills/1. Calendar Actions/skill.js).
export function normalizeLocation(location, virtual) {
  if (virtual === true) return { location: null, virtual: true }; // virtual wins the XOR
  const addr = typeof location === "string" ? location.trim() : "";
  return addr ? { location: addr, virtual: false } : { location: null, virtual: false };
}

// The READ direction: the {location, virtual} of a REAL Google event resource. A Meet is
// signalled by conferenceData (or a hangoutLink); otherwise the event's own `location` string
// is the physical address. Funnels through normalizeLocation so virtual still wins.
export function locationFromEvent(ev) {
  const virtual = !!(ev?.hangoutLink || ev?.conferenceData);
  return normalizeLocation(ev?.location, virtual);
}

// The Meet URL to surface in the done bubble: the event's hangoutLink first, else the
// `video` entryPoint uri, else null. Edge #8: Google may still be provisioning the Meet on the
// immediate insert/update response, so this can be null — the event htmlLink (already shown) is
// the always-works fallback the caller relies on.
export function meetLinkOf(ev) {
  if (ev?.hangoutLink) return ev.hangoutLink;
  const entries = ev?.conferenceData?.entryPoints;
  if (Array.isArray(entries)) {
    const video = entries.find((e) => e?.entryPointType === "video" && e?.uri);
    if (video) return video.uri;
  }
  return null;
}

// The location/conference fragment of an events.insert body, plus whether
// conferenceDataVersion:1 is needed. A virtual create provisions a Meet with a DETERMINISTIC
// requestId (seed = start_iso||start_date||title — no Date.now/Math.random, and reusing the id
// is idempotent per Google). A physical create sets `location`. NEITHER field -> `{}` (no key
// added, byte-identical to today's write). The draft is already normalized (XOR holds).
export function locationInsertBody({ location, virtual, seed }) {
  if (virtual) {
    return {
      body: { conferenceData: { createRequest: { requestId: `meet-${seed}` } } },
      conferenceVersion: true,
    };
  }
  if (location) return { body: { location }, conferenceVersion: false };
  return { body: {}, conferenceVersion: false };
}

// The location/conference fields for the full-resource events.update, plus whether
// conferenceDataVersion:1 is needed. This is where Nit C (conditional version), Nit D
// (Meet-clear on virtual->physical), edge #2 (idempotent) and edge #3 (XOR switch) all live.
// Five branches, authoritative:
//   virtual  -> virtual   : {} / false — {...ev} re-supplies the live Meet and, with NO
//                           conferenceDataVersion, Google leaves it untouched (idempotent).
//   physical -> virtual   : provision a Meet (createRequest, deterministic requestId), drop
//                           the address; version:true.
//   virtual  -> physical  : Nit D — clear the stale Meet (conferenceData:null) + write the
//                           address; version:true.
//   physical -> physical, SAME address : {} / false — a non-location edit disturbs NOTHING
//                           (Nit C: never sends a conference version, never touches a Meet).
//   physical -> physical, CHANGED      : set/clear the address only; no conference; version:false.
export function locationUpdateFields(draft, base, seed) {
  const dv = !!draft.virtual;
  const bv = !!base.virtual;
  if (dv && bv) return { fields: {}, conferenceVersion: false };
  if (dv && !bv) {
    return {
      fields: { location: "", conferenceData: { createRequest: { requestId: `meet-${seed}` } } },
      conferenceVersion: true,
    };
  }
  if (!dv && bv) {
    return {
      fields: { location: draft.location || "", conferenceData: null },
      conferenceVersion: true,
    };
  }
  if ((draft.location || null) === (base.location || null)) {
    return { fields: {}, conferenceVersion: false };
  }
  return { fields: { location: draft.location || "" }, conferenceVersion: false };
}

function calendarClient(env) {
  return google.calendar({ version: "v3", auth: googleAuth(env) });
}

function calId(env) {
  return env.GOOGLE_CALENDAR_ID || "primary";
}

// The ONE events.insert in the repo. An ALL-DAY event is a different WIRE SHAPE.
async function createEvent(
  env,
  { title, emails, start_iso, end_iso, summary, all_day, start_date, end_date, recurrence, location, virtual }
) {
  const cal = calendarClient(env);
  // Idempotency: reuse an identical confirmed event rather than stacking duplicates.
  const existing = await findConfirmedDuplicates(env, {
    title,
    startIso: start_iso,
    allDay: all_day,
    startDate: start_date,
    endDate: end_date,
  });
  if (existing.length) return { ...existing[0], reused: true };
  // The location/conference fragment. A physical event adds `location`; a virtual one adds a
  // conferenceData.createRequest and needs conferenceDataVersion:1; NEITHER adds no key at all
  // (byte-identical to today's write). Seed the deterministic Meet requestId from the event's
  // own start/title — no Date.now/Math.random.
  const { body: locBody, conferenceVersion } = locationInsertBody({
    location,
    virtual,
    seed: start_iso || start_date || title,
  });
  const r = await cal.events.insert({
    calendarId: calId(env),
    sendUpdates: "all", // fires the invite email to the participants
    // Only a conference-touching write carries the version — a plain create never sends it.
    ...(conferenceVersion ? { conferenceDataVersion: 1 } : {}),
    requestBody: {
      summary: title,
      description: summary || "",
      ...(all_day
        ? { start: { date: start_date }, end: { date: end_date } }
        : {
            start: { dateTime: start_iso, timeZone: CAL_TZ },
            end: { dateTime: end_iso, timeZone: CAL_TZ },
          }),
      ...(recurrence ? { recurrence: [recurrence] } : {}),
      ...locBody,
      attendees: emails.map((email) => ({ email })),
    },
  });
  return r.data;
}

async function getEvent(env, eventId) {
  const cal = calendarClient(env);
  const r = await cal.events.get({ calendarId: calId(env), eventId });
  return r.data;
}

// The ONE events.update in the repo: a FULL-RESOURCE REPLACE. The caller passes the FRESHLY
// FETCHED event and we spread it, so everything we never touch rides along.
async function updateEvent(env, eventId, ev, fields, conferenceVersion = false) {
  const cal = calendarClient(env);
  const r = await cal.events.update({
    calendarId: calId(env),
    eventId,
    sendUpdates: "all", // email the attendees about the change
    // Only a conference-touching write carries the version — a plain edit never sends it, so
    // {...ev} re-supplies the live conferenceData and Google leaves an existing Meet untouched.
    ...(conferenceVersion ? { conferenceDataVersion: 1 } : {}),
    requestBody: { ...ev, ...fields },
  });
  return r.data;
}

// Find CONFIRMED events that are the same meeting: identical title and start instant.
async function findConfirmedDuplicates(
  env,
  { title, startIso, excludeId, allDay, startDate, endDate }
) {
  if (!startIso) return [];
  if (allDay && !(startDate && endDate)) return [];
  const cal = calendarClient(env);
  const start = new Date(startIso).getTime();
  const dayMs = allDay ? Date.parse(`${startDate}T12:00:00-03:00`) : null;
  const timeMin = allDay
    ? new Date(`${startDate}T00:00:00-03:00`).toISOString()
    : new Date(start - 60000).toISOString();
  const timeMax = allDay
    ? new Date(endOfLocalDay(dayMs)).toISOString()
    : new Date(start + 60000).toISOString();
  const r = await cal.events.list({
    calendarId: calId(env),
    timeMin,
    timeMax,
    singleEvents: true,
    showDeleted: false,
    maxResults: 50,
  });
  return (r.data.items || []).filter(
    (e) =>
      e.status === "confirmed" &&
      e.summary === title &&
      e.id !== excludeId &&
      (allDay
        ? e.start?.date === startDate && e.end?.date === endDate
        : e.start?.dateTime && new Date(e.start.dateTime).getTime() === start)
  );
}

// Identify which real calendar event(s) a request targets, by MATCHING the details captured
// from the conversation against the calendar. Kept per PLAN §B1; used by the FIND precise path.
//   +100  the event id decoded from the replied-to link (strong, explicit)
//   + 40  same start instant as the captured date/time
//   + 30  an attendee email overlaps a captured participant email
// A confident match is score >= 70.
async function matchEventTargets(env, { eidEventId, startIso, emails }) {
  const cal = calendarClient(env);
  const emailSet = new Set((emails || []).map((e) => String(e).toLowerCase()));
  const startMs = startIso ? new Date(startIso).getTime() : null;
  const candidates = new Map(); // id -> event

  if (eidEventId) {
    try {
      const ev = await getEvent(env, eidEventId);
      if (ev && ev.status === "confirmed") candidates.set(ev.id, ev);
    } catch {
      /* stale/undecodable — rely on the captured details below */
    }
  }

  if (startMs != null) {
    const r = await cal.events.list({
      calendarId: calId(env),
      timeMin: new Date(startMs - 60000).toISOString(),
      timeMax: new Date(startMs + 60000).toISOString(),
      singleEvents: true,
      showDeleted: false,
      maxResults: 50,
    });
    for (const e of r.data.items || []) {
      if (e.status === "confirmed" && e.start?.dateTime) candidates.set(e.id, e);
    }
  }

  const confident = [];
  for (const e of candidates.values()) {
    let score = 0;
    if (eidEventId && e.id === eidEventId) score += 100;
    if (startMs != null && e.start?.dateTime && new Date(e.start.dateTime).getTime() === startMs)
      score += 40;
    const attendees = (e.attendees || []).map((a) => String(a.email || "").toLowerCase());
    if (emailSet.size && attendees.some((a) => emailSet.has(a))) score += 30;
    if (score >= 70) confident.push(e);
  }
  return confident;
}

// Cancel the matched event(s) AND sweep any confirmed duplicate of the same meeting. Returns
// how many distinct events were removed. A 410 (already deleted) is treated as success.
async function cancelMeeting(env, { eventIds = [], title, startIso }) {
  const cal = calendarClient(env);
  const ids = new Set(eventIds.filter(Boolean));
  try {
    const dupes = await findConfirmedDuplicates(env, { title, startIso });
    for (const d of dupes) ids.add(d.id);
  } catch (e) {
    console.error("Calendar dup lookup error:", e?.response?.data || e?.message || e);
  }

  let deleted = 0;
  for (const id of ids) {
    try {
      await cal.events.delete({
        calendarId: calId(env),
        eventId: id,
        sendUpdates: "all", // notify attendees of the cancellation
      });
      deleted++;
    } catch (e) {
      const code = e?.code || e?.response?.status;
      if (code === 410) {
        deleted++; // already gone — that's the outcome we wanted
        continue;
      }
      throw e;
    }
  }
  return deleted;
}

// A Google Calendar link carries an `eid` = base64url("<eventId> <calendarId>"). Kept per
// PLAN §B1 (a deterministic helper the model may lean on when a link is quoted).
function resolveEventId(link) {
  const m = String(link || "").match(/[?&]eid=([^&\s]+)/);
  if (!m) return null;
  try {
    const b64 = m[1].replace(/-/g, "+").replace(/_/g, "/");
    const decoded = Buffer.from(b64, "base64").toString("utf8");
    const eventId = decoded.split(/\s+/)[0];
    return eventId || null;
  } catch {
    return null;
  }
}

// ---- Entry point -------------------------------------------------------------
// ctx (from the orchestrator): { owner, contact, number, env, lang, send, sendFailure, info }.
export async function run(ctx) {
  const { number } = ctx;
  const info = ctx.info || {};

  switch (info.action) {
    case "find":
      return handleFind(ctx, info);
    case "list":
      return handleList(ctx, info);
    case "create":
      return handleCreate(ctx, info);
    case "edit":
      return handleEdit(ctx, info);
    case "delete":
      return handleDelete(ctx, info);
    default:
      await ctx.sendFailure(number, reply(ctx.lang).noAction({ summary: info?.summary }));
      return { ok: false, reason: "noAction" };
  }
}

// ---- FIND (READ) -----------------------------------------------------------
// Gather candidate events the model can echo back into an edit/delete. SENDS NOTHING — the
// orchestrator serializes the return value and drives the read-back turn. The candidate shape
// is lean (id/title/start/end/attendees/link) to stay under READBACK_CAP.
async function handleFind(ctx, info) {
  const { env } = ctx;
  const emails = (Array.isArray(info.participants) ? info.participants : [])
    .map((p) => p?.email)
    .filter(Boolean);
  const q = String(info.query || "").trim().toLowerCase();

  let matches = [];
  // Precise path: a start instant + an attendee email → a confident id match (matchEventTargets).
  if (info.start_iso && emails.length) {
    try {
      matches = await matchEventTargets(env, {
        eidEventId: null,
        startIso: info.start_iso,
        emails,
      });
    } catch (e) {
      console.error("Calendar find(match) error:", e?.response?.data || e?.message || e);
    }
  }

  // Broad path: list a window and filter by title text / attendee email.
  if (!matches.length) {
    const cal = calendarClient(env);
    const startMs = Date.parse(info.start_iso || "");
    let timeMin, timeMax;
    if (Number.isFinite(startMs)) {
      timeMin = new Date(startMs - 6 * 3600000).toISOString();
      timeMax = new Date(endOfLocalDay(startMs)).toISOString();
    } else {
      const now = Date.now();
      timeMin = new Date(now).toISOString();
      timeMax = new Date(now + 30 * 86400000).toISOString();
    }
    let items = [];
    try {
      const r = await cal.events.list({
        calendarId: calId(env),
        timeMin,
        timeMax,
        singleEvents: true,
        orderBy: "startTime",
        showDeleted: false,
        maxResults: 50,
      });
      items = (r.data.items || []).filter((e) => e.status === "confirmed");
    } catch (e) {
      console.error("Calendar find(list) error:", e?.response?.data || e?.message || e);
      return { candidates: [], count: 0, error: "lookup_failed" };
    }
    const emailSet = new Set(emails.map((e) => String(e).toLowerCase()));
    matches = items.filter((e) => {
      let ok = true;
      if (q) ok = ok && String(e.summary || "").toLowerCase().includes(q);
      if (emailSet.size) {
        const at = (e.attendees || []).map((a) => String(a.email || "").toLowerCase());
        ok = ok && at.some((a) => emailSet.has(a));
      }
      return ok;
    });
  }

  const candidates = matches.slice(0, 20).map((e) => ({
    event_id: e.id,
    title: e.summary || "",
    start: e.start?.dateTime || e.start?.date || null,
    end: e.end?.dateTime || e.end?.date || null,
    attendees: (e.attendees || [])
      .filter((a) => a.email && !a.self && !a.resource)
      .map((a) => a.email),
    link: e.htmlLink || "",
  }));
  return { candidates, count: candidates.length };
}

// ---- CREATE (ACT) ----------------------------------------------------------
async function handleCreate(ctx, info) {
  const { number } = ctx;
  const draft = draftFromInfo(ctx, info);
  if (!draft.start_iso) {
    // requiredWhen guarantees this, but createFromDraft does `new Date(start_iso)` — a null
    // start would land the event in 1970. Defensive.
    await ctx.sendFailure(number, reply(ctx.lang).noAction({ summary: info?.summary }));
    return { ok: false, reason: "noDate" };
  }
  try {
    const res = await createFromDraft(ctx, draft); // sends createDone
    return { ok: true, link: res.link, eventId: res.eventId, reused: res.reused };
  } catch (e) {
    console.error("Calendar create error:", e?.response?.data || e?.message || e);
    await ctx.sendFailure(number, reply(ctx.lang).createGoogleError());
    return { ok: false, reason: "googleError" };
  }
}

// Normalize the payload into the draft we insert. Applies the title fallback and defaults.
// Exported for the offline location selftest (scripts/mary-calendar-location-selftest.mjs).
export function draftFromInfo(ctx, info) {
  const { owner, contact } = ctx;
  const participants = (Array.isArray(info.participants) ? info.participants : [])
    .map((p) => ({
      name: p?.name || null,
      email: p?.email || null,
      noEmail: !!p?.noEmail,
    }))
    .filter((p) => p.name || p.email);
  const names = participants.map((p) => p.name).filter(Boolean);
  const title =
    String(info.title || "").trim() ||
    `${owner}/${names.join("/") || contact || "Guest"}`;
  const duration_min = Number(info.duration_min) > 0 ? Number(info.duration_min) : 45;

  const all_day = !!info.all_day;
  const all_day_end_iso = normalizeAllDay(info.start_iso, all_day, info.all_day_end_iso);

  return {
    title,
    participants,
    start_iso: info.start_iso || null,
    duration_min,
    all_day,
    all_day_end_iso,
    summary: info.summary || "",
    recurrence: info.recurrence || null,
    // Location rides the draft like all_day / recurrence; normalizeLocation is the sole XOR
    // enforcer (virtual wins), so the invariant is decided in exactly one place.
    ...normalizeLocation(info.location, info.virtual),
  };
}

function draftEmails(draft) {
  return (draft.participants || []).map((p) => p?.email).filter(Boolean);
}

// Named guests we will NOT be inviting: no email, and the owner has said he hasn't got one.
function draftUninvited(draft) {
  return (draft.participants || []).filter((p) => !p.email && p.name).map((p) => p.name);
}

// The localized recurrence line for the done bubble, or null for a one-off. It MUST call
// toRRule with the SAME opts createFromDraft uses to WRITE the rule, so the text line and the
// written RRULE can never diverge.
function recurrenceLineFor(draft, lang) {
  return toRRule(draft.recurrence, { allDay: !!draft.all_day, startIso: draft.start_iso })
    ? describeRecurrence(draft.recurrence, lang)
    : null;
}

// Write the draft to Google and report back. Returns { link, eventId, reused } for the read-back.
async function createFromDraft(ctx, draft) {
  const { env, number, send } = ctx;
  const emails = draftEmails(draft);
  const all_day = !!draft.all_day;

  let start_date = null;
  let end_date = null;
  let end_iso = null;
  if (all_day) {
    ({ start_date, end_date } = allDayWireDates(draft));
  } else {
    end_iso = new Date(
      new Date(draft.start_iso).getTime() + draft.duration_min * 60000
    ).toISOString();
  }

  const rrule = toRRule(draft.recurrence, { allDay: all_day, startIso: draft.start_iso });

  const ev = await createEvent(env, {
    title: draft.title,
    emails,
    start_iso: draft.start_iso,
    end_iso,
    summary: draft.summary,
    all_day,
    start_date,
    end_date,
    recurrence: rrule,
    location: draft.location,
    virtual: draft.virtual,
  });
  await send(
    number,
    reply(ctx.lang).createDone({
      reused: !!ev.reused,
      title: draft.title,
      emails: emails.join(", "),
      when: localizeWhen(ctx.lang, draft),
      duration: all_day ? null : draft.duration_min,
      link: ev.htmlLink || "",
      uninvited: draftUninvited(draft),
      recurrence: recurrenceLineFor(draft, ctx.lang),
      // The location line renders from the draft; meetLink is read back from the created event
      // (may still be provisioning — edge #8), with the htmlLink above as the fallback.
      location: draft.location,
      virtual: draft.virtual,
      meetLink: meetLinkOf(ev),
    })
  );
  return { link: ev.htmlLink || "", eventId: ev.id, reused: !!ev.reused };
}

// ---- EDIT (ACT) ------------------------------------------------------------
// The edited state = the event's current state, with the non-null change fields from info
// overlaid. events.update replaces the whole event, so the freshly fetched resource is spread
// (updateEvent) and everything untouched — reminders, colorId, recurrence — rides along.
async function handleEdit(ctx, info) {
  const { env, number } = ctx;
  const eventId = info.event_id;
  if (!eventId) {
    await ctx.sendFailure(number, reply(ctx.lang).editNoMatch());
    return { ok: false, reason: "noEventId" };
  }

  let ev;
  try {
    ev = await getEvent(env, eventId);
  } catch (e) {
    console.error("Calendar edit fetch error:", e?.response?.data || e?.message || e);
    await ctx.sendFailure(number, reply(ctx.lang).editNoMatch());
    return { ok: false, reason: "noMatch" };
  }
  if (!ev || ev.status !== "confirmed") {
    await ctx.sendFailure(number, reply(ctx.lang).editNoMatch());
    return { ok: false, reason: "noMatch" };
  }

  const base = editDraftFromEvent(ev);
  const draft = {
    title: (info.title && String(info.title).trim()) || base.title,
    start_iso: info.start_iso ?? base.start_iso,
    duration_min: Number(info.duration_min) > 0 ? Number(info.duration_min) : base.duration_min,
    // all_day: honour an explicit true; a bare false only alongside a new start (turning
    // all-day off ALWAYS means giving the event a time) — otherwise keep the event's state.
    all_day:
      info.all_day === true
        ? true
        : info.all_day === false && info.start_iso
          ? false
          : base.all_day,
    all_day_end_iso: info.all_day_end_iso ?? base.all_day_end_iso,
    summary:
      typeof info.summary === "string" && info.summary.trim() ? info.summary : base.summary,
    emails:
      Array.isArray(info.participants) && info.participants.length
        ? info.participants.map((p) => p?.email).filter(Boolean)
        : base.emails,
  };
  // The place, "absent = keep base": virtual:true -> Meet (drop address); else a non-empty
  // location -> physical; else keep the event's current place (base). The model never sees the
  // event JSON on @mary's edit path, so "absent = keep" is what carries an unchanged place
  // through a time/title edit. normalizeLocation then re-applies the XOR (video wins).
  const place =
    info.virtual === true
      ? { location: null, virtual: true }
      : info.location && String(info.location).trim()
        ? { location: info.location, virtual: false }
        : { location: base.location, virtual: base.virtual };
  Object.assign(draft, normalizeLocation(place.location, place.virtual));
  draft.all_day_end_iso = normalizeAllDay(draft.start_iso, draft.all_day, draft.all_day_end_iso);

  try {
    await applyEditDraft(ctx, eventId, draft, ev, base); // sends editDone
    return { ok: true, eventId };
  } catch (e) {
    console.error("Calendar edit update error:", e?.response?.data || e?.message || e);
    await ctx.sendFailure(number, reply(ctx.lang).editGoogleError());
    return { ok: false, reason: "googleError" };
  }
}

// The editable DRAFT seed = the event's current state, in the create draft's own terms.
// Exported for the offline location selftest (scripts/mary-calendar-location-selftest.mjs).
export function editDraftFromEvent(ev) {
  const { all_day, start_iso, all_day_end_iso } = allDayFromEvent(ev);
  const end = all_day ? null : ev.end?.dateTime || null;
  const duration_min =
    start_iso && end ? Math.round((new Date(end) - new Date(start_iso)) / 60000) : 45;
  return {
    title: ev.summary || "",
    start_iso,
    duration_min,
    all_day,
    all_day_end_iso,
    summary: ev.description || "",
    emails: (ev.attendees || []).map((a) => a.email).filter(Boolean),
    // Seed the event's CURRENT place so a time/title edit that says nothing about location
    // carries it through unchanged (the "absent = keep base" overlay in handleEdit).
    ...locationFromEvent(ev),
  };
}

// Write the confirmed edit draft to Google (a full-resource update) and report back.
async function applyEditDraft(ctx, eventId, draft, ev, base) {
  const { env, number, send } = ctx;
  const all_day = !!draft.all_day;

  const fields = {
    summary: draft.title,
    description: draft.summary || "",
    attendees: draft.emails.map((email) => ({ email })),
  };
  if (all_day && draft.start_iso) {
    const { start_date, end_date } = allDayWireDates(draft);
    fields.start = { date: start_date };
    fields.end = { date: end_date };
  } else if (draft.start_iso) {
    const endIso = new Date(
      new Date(draft.start_iso).getTime() + draft.duration_min * 60000
    ).toISOString();
    fields.start = { dateTime: draft.start_iso, timeZone: CAL_TZ };
    fields.end = { dateTime: endIso, timeZone: CAL_TZ };
  }

  // The location/conference fields, computed against the event's CURRENT place (base, seeded by
  // editDraftFromEvent in handleEdit). This is where the conditional conferenceDataVersion
  // (Nit C) and the Meet-clear (Nit D) come from. The Meet requestId seed is the eventId —
  // deterministic and idempotent per Google.
  const { fields: locFields, conferenceVersion } = locationUpdateFields(draft, base, eventId);
  Object.assign(fields, locFields);

  const updated = await updateEvent(env, eventId, ev, fields, conferenceVersion);

  await send(
    number,
    reply(ctx.lang).editDone({
      title: draft.title || "(untitled)",
      emails: draft.emails.join(", "),
      when: localizeWhen(ctx.lang, draft),
      duration: all_day ? null : draft.duration_min,
      link: updated.htmlLink || ev.htmlLink || "",
      // The location line renders from the draft; meetLink is read back from the updated event
      // (may still be provisioning — edge #8), with the htmlLink above as the fallback.
      location: draft.location,
      virtual: draft.virtual,
      meetLink: meetLinkOf(updated),
    })
  );
}

// ---- DELETE (ACT) ----------------------------------------------------------
async function handleDelete(ctx, info) {
  const { env, number } = ctx;
  const eventId = info.event_id;
  if (!eventId) {
    await ctx.sendFailure(number, reply(ctx.lang).deleteNoMatch());
    return { ok: false, reason: "noEventId" };
  }

  // Fetch for the title/start the dedupe sweep + the confirmation copy use.
  let title = null;
  let startIso = info.start_iso || null;
  try {
    const ev = await getEvent(env, eventId);
    if (ev) {
      title = ev.summary || null;
      startIso = ev.start?.dateTime || startIso;
    }
  } catch {
    /* proceed with what we have — cancelMeeting still deletes the id */
  }

  try {
    const n = await cancelMeeting(env, { eventIds: [eventId], title, startIso });
    await ctx.send(
      number,
      reply(ctx.lang).deleteCancelled({ title: title || "(untitled)", removed: n })
    );
    return { ok: true, cancelled: n };
  } catch (e) {
    console.error("Calendar delete error:", e?.response?.data || e?.message || e);
    await ctx.sendFailure(number, reply(ctx.lang).deleteGoogleError());
    return { ok: false, reason: "googleError" };
  }
}

// ---- LIST (READ) -----------------------------------------------------------
// Send the rendered schedule AND return the same items structured (each with event_id), so a
// follow-up edit/delete can target one by id.
async function handleList(ctx, info) {
  const { env, number, send, lang } = ctx;
  const now = Date.now();
  const cal = calendarClient(env);

  const asCandidates = (items) =>
    items.map((e) => ({
      event_id: e.id,
      title: e.summary || "",
      start: e.start?.dateTime || e.start?.date || null,
    }));

  // "next meeting" → scan forward and show the first upcoming event.
  if (info?.list_mode === "next") {
    let items;
    try {
      const r = await cal.events.list({
        calendarId: calId(env),
        timeMin: new Date(now).toISOString(),
        timeMax: new Date(now + 14 * 86400000).toISOString(),
        singleEvents: true,
        orderBy: "startTime",
        showDeleted: false,
        maxResults: 10,
      });
      items = (r.data.items || []).filter((e) => e.status === "confirmed");
    } catch (e) {
      console.error("Calendar list(next) error:", e?.response?.data || e?.message || e);
      await ctx.sendFailure(number, reply(lang).listError());
      return { ok: false, reason: "listError" };
    }
    const next = items.find((e) => e.start?.dateTime || e.start?.date);
    await send(number, reply(lang).listNext({ event: next ? toListItem(next) : null }));
    return { events: next ? asCandidates([next]) : [] };
  }

  // window mode: an explicit range if the LLM resolved one, else now → end of today.
  const parsedStart = info?.range_start_iso ? new Date(info.range_start_iso).getTime() : NaN;
  const startMs = Number.isFinite(parsedStart) ? parsedStart : now;
  const parsedEnd = info?.range_end_iso ? new Date(info.range_end_iso).getTime() : NaN;
  let endMs = Number.isFinite(parsedEnd) ? parsedEnd : endOfLocalDay(startMs);
  if (endMs <= startMs) endMs = endOfLocalDay(startMs); // guard an empty/backwards range

  let items;
  try {
    const r = await cal.events.list({
      calendarId: calId(env),
      timeMin: new Date(startMs).toISOString(),
      timeMax: new Date(endMs).toISOString(),
      singleEvents: true,
      orderBy: "startTime",
      showDeleted: false,
      maxResults: 50,
    });
    items = (r.data.items || []).filter((e) => e.status === "confirmed");
  } catch (e) {
    console.error("Calendar list error:", e?.response?.data || e?.message || e);
    await ctx.sendFailure(number, reply(lang).listError());
    return { ok: false, reason: "listError" };
  }

  await send(
    number,
    reply(lang).listEvents({
      startMs,
      endMs,
      events: items.map(toListItem),
      capped: items.length >= 50,
    })
  );
  return { events: asCandidates(items) };
}

// End-of-day (23:59:59.999) for the calendar TZ, in ms. São Paulo is a fixed -03:00 offset.
function endOfLocalDay(ms) {
  const [y, m, d] = new Intl.DateTimeFormat("en-CA", {
    timeZone: CAL_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  })
    .format(new Date(ms))
    .split("-");
  return new Date(`${y}-${m}-${d}T23:59:59.999-03:00`).getTime();
}

// Flatten a Google event into the locale-neutral shape the reply renderers need.
function toListItem(e) {
  const startIso = e.start?.dateTime || null;
  const endIso = e.end?.dateTime || null;
  const allDay = !startIso && !!e.start?.date;
  const dayMs = allDay
    ? new Date(`${e.start.date}T00:00:00-03:00`).getTime()
    : startIso
    ? new Date(startIso).getTime()
    : null;
  const durationMin =
    startIso && endIso ? Math.round((new Date(endIso) - new Date(startIso)) / 60000) : null;
  const emails = (e.attendees || [])
    .filter((a) => a.email && !a.self && !a.resource)
    .map((a) => a.email);
  return { allDay, startIso, dayMs, title: e.summary || "", emails, durationMin };
}
