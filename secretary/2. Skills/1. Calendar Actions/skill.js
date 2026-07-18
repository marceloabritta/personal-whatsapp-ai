// ============================================================================
//  Skill "Calendar Actions" — LOGIC.
//  Interprets the order with Claude and acts on Google Calendar:
//    - create  : make a new event and fire the invite email.
//    - delete  : cancel an event the owner REPLIED to (its calendar link), with
//                a confirm-first step.
//    - edit    : change an event the owner REPLIED to (move/relength/rename/add or
//                remove an attendee); asks for clarification when ambiguous.
//  Run by the orchestrator when the router picks "calendar_action".
//
//  Skill contract (read by the orchestrator):
//    export const manifest = { id, description }
//    export async function run(ctx)
// ============================================================================
import { google } from "googleapis";
import {
  buildSystem,
  buildExtractionRules,
  buildUserPrompt,
  buildCreateReviewSystem,
  buildCreateReviewUser,
  buildResolveSystem,
  buildResolveUser,
  buildEditSystem,
  buildEditUser,
  buildEditReviewSystem,
  buildEditReviewUser,
  CAL_SCHEMA,
  REVIEW_SCHEMA,
  RESOLVE_SCHEMA,
  EDIT_SCHEMA,
  EDIT_REVIEW_SCHEMA,
  reply,
  localizeDate,
  localizeWhen,
  describeRecurrence,
} from "./prompt.js";
// Structured outputs (jsonFormat/readReply), the shared confirm-first classifier and
// Google OAuth all live in the orchestrator's lib — see those files.
import { jsonFormat, readReply } from "../../1. Orchestrator/lib/llm.js";
import { classifyConfirmation } from "../../1. Orchestrator/lib/confirm.js";
import { googleAuth } from "../../1. Orchestrator/lib/google.js";

// Capabilities exposed to OTHER skills through the orchestrator's registry
// (ctx.callSkill) — NOT seen by the router. `startCreate` runs the full
// confirm-first create flow (draft -> "yes" -> invite) on a caller-supplied `info`;
// the caller (e.g. task_action, for a "task" assigned to someone else) never
// re-implements it. ctx is injected by the orchestrator; the session/continuation
// lifecycle is owned by calendar_action (the session it opens is tagged with our id).
export const capabilities = {
  startCreate: (ctx, info) => handleCreate(ctx, info),
};

// `inputs` — THE DECLARED INPUT CONTRACT the orchestrator's merged router+extractor call asks
// the model to fill (lib/inputs.js). The router never reads a field name in here: it renders
// this as opaque prompt text and validates whatever comes back AGAINST it. What the skill gets
// back, through ctx.info, is a drop-in for interpret()'s output — which is why handleCreate,
// handleDelete, handleEdit and handleList need no changes at all.
//
// 🔴 `fields` MUST BE EXACTLY CAL_SCHEMA.required — the SAME TWELVE NAMES. That identity is
// what makes the payload a drop-in. It is also a NEW way to break this skill silently: add a
// field to CAL_SCHEMA and forget it here, and the merged prompt simply stops asking for it,
// draftFromInfo reads `undefined`, and the feature that field implements dies with no test
// going red. It has already happened once (6c76dab added all_day / all_day_end_iso).
// scripts/turn-latency-selftest.mjs T2.10 asserts the two sets are equal. If a future card
// makes it red, UPDATE THE DECLARATION — never loosen the lint.
//
// `requiredWhen` means "must be non-null before we can act". all_day and all_day_end_iso are
// NOT in it, on purpose: an ordinary timed create legitimately has all_day=false and
// all_day_end_iso=null, and requiring them would stop every normal create. They are DECLARED
// and NULLABLE — that is all. What makes the model fill them is the declaration plus the
// rulebook, not requiredWhen.
export const manifest = {
  id: "calendar_action",
  // This skill runs its OWN dialogue (it confirms create/edit/delete for itself). Default value;
  // written explicitly so the migration state is a grep and a new author's silence stays safe.
  conversation: "skill",
  description:
    "create, edit/reschedule, or delete/cancel a meeting or event in Google Calendar and notify the participants; also read/list what's on the calendar (answer questions like what's on tomorrow, anything Friday afternoon, or what's my next meeting)",
  inputs: {
    discriminator: "action",
    fields: {
      action: {
        type: "enum",
        enum: ["create", "delete", "edit", "list", "other"],
        desc: "what to do",
      },
      title: { type: "string", nullable: true, desc: "the event's short calendar heading" },
      participants: {
        type: "array",
        of: {
          name: { type: "string", nullable: true },
          email: { type: "email", nullable: true },
        },
        desc: "attendees besides the owner; email null if it is NOT in the conversation — NEVER invent one",
      },
      start_iso: { type: "iso", nullable: true, desc: "ISO-8601 with the -03:00 offset" },
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
      summary: { type: "string", desc: "a longer one-line agenda for the event body" },
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
        desc: 'the repeat rule for a RECURRING event, else null (one-off — the default). Object {freq: "daily"|"weekly"|"monthly", interval: number|null, byday: ["MO".."SU"]|null (weekly only), count: number|null, until: ISO-8601 -03:00|null}. count XOR until, never both.',
      },
      location: {
        type: "string",
        nullable: true,
        desc: "the VERBATIM physical address/venue of the meeting, exactly as written — NEVER invented, looked up, or reformatted. null when no place is given or when it is a video call.",
      },
      virtual: {
        type: "bool",
        nullable: true,
        desc: 'true when the meeting is a Google Meet VIDEO CALL ("chamada de vídeo", "por Meet", "online"). Physical XOR virtual: if both an address and a video call are asked for, virtual wins and location is null.',
      },
    },
    // A faithful transcription of missingOf()/isComplete() below. `participants[].email` means
    // "every attendee that EXISTS has an email" — an EMPTY list is COMPLETE, not missing (a
    // zero-guest create is an ordinary event; see missingOf's comment and commit 9eead61).
    requiredWhen: {
      create: ["start_iso", "participants[].email"],
      list: ["list_mode"],
      delete: [],
      edit: [],
      other: [],
    },
    // The skill's own plain-code sanity rules. Deliberately NOT here: an
    // `all_day_end_iso >= start_iso` rule. draftFromInfo() already clamps an inverted range to
    // a single day, in the one normalizer every path funnels through, and duplicating that
    // clamp is how the two silently drift apart. (A consistency failure does not withhold the
    // payload anyway — handover is gated on the VALIDITY tier alone.)
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
        name: "list_fields_only_on_list",
        test: (i) =>
          i.action === "list"
            ? !!i.list_mode
            : i.list_mode == null &&
              i.range_start_iso == null &&
              i.range_end_iso == null,
      },
      {
        name: "window_list_has_a_range",
        test: (i) =>
          !(i.action === "list" && i.list_mode === "window") || !!i.range_start_iso,
      },
      {
        // The LIST WINDOW, and nothing to do with all-day: a window cannot end before it starts.
        name: "end_after_start",
        test: (i) =>
          !(i.range_start_iso && i.range_end_iso) ||
          Date.parse(i.range_end_iso) >= Date.parse(i.range_start_iso),
      },
      {
        // Physical XOR virtual — documentary only. normalizeLocation() is the real enforcer
        // (virtual wins, then a non-empty address, else neither); a consistency failure never
        // withholds the payload, so this just makes the invariant grep-able alongside the code.
        name: "location_virtual_xor",
        test: (i) => !(i.virtual === true && i.location != null && String(i.location).trim() !== ""),
      },
    ],
    // Carried VERBATIM into the merged prompt. Same env + same default as server.js's
    // OWNER_NAME — the manifest is read at boot, before any ctx exists.
    rulebook: () => buildExtractionRules(process.env.OWNER_NAME || "User"),
  },
};

const CAL_TZ = "America/Sao_Paulo";

// The longest all-day span we will create from one order. A range is CLAMPED to it rather
// than refused: the owner sees "(31 dias)" in the confirm bubble and corrects it before
// anything is written. Confirm-first is the safety net; a new error string is not.
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

// The local day of an ISO instant, or null if it is missing/unparseable (localDayStr would
// throw on an Invalid Date, and a bad model value must never crash the flow).
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
// event still covers. Google's `end.date` is EXCLUSIVE. A single day on 2026-07-14 is
// start 2026-07-14 / end 2026-07-15; Mon 13 -> Wed 15 is start 2026-07-13 / end 2026-07-16
// (a THURSDAY). Off by one is a 2-day event, or a zero-day one Google rejects.
//
// It is a FUNCTION, and not a line inside createFromDraft, because there are now TWO
// writers — createFromDraft and applyEditDraft. There is still exactly ONE place days are
// converted, which is what keeps the sentence above true.
function allDayWireDates(draft) {
  // The DAY is derived from start_iso in CAL_TZ — which is why start_iso stays REQUIRED
  // (missingOf().noTime guards the null-start -> 1970 write on create; on edit the draft is
  // seeded from the event itself, see allDayFromEvent).
  const start_date = localDayStr(new Date(draft.start_iso).getTime());
  const last_date = draft.all_day_end_iso
    ? localDayStr(new Date(draft.all_day_end_iso).getTime())
    : start_date;
  return { start_date, end_date: addDays(last_date, 1) }; // end_date EXCLUSIVE
}

// The two ALL-DAY sanity clamps, in ONE place. Returns the sanitized INCLUSIVE
// `all_day_end_iso`. Neither clamp has an error reply: the owner SEES the result in the
// confirm bubble ("Dia todo", "(3 dias)") and corrects it before anything is written.
//   - not all-day, or no end given -> null (a single day).
//   - the end day is BEFORE the start day (or unparseable) -> null (a single day), so a
//     MOVE that strands the old range end behind the new start self-heals instead of
//     writing a backwards event Google rejects.
//   - an absurdly long range -> clamp the SPAN. He sees "(31 dias)" and corrects it.
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
// The deterministic layer that turns the model's structured recurrence object into the
// exact RRULE string written to Google. Exported so scripts/calendar-recurrence-selftest.mjs
// can pin the output offline. `toRRule` is the SINGLE validator of a recurrence — an
// uncompilable / degenerate / past-until object falls back to null (a one-off), so a garbled
// recurrence degrades to a single event rather than erroring (confirm-first is the backstop).
//
// rec: the structured recurrence object (or null). opts: { allDay = false, startIso = null }.
// Returns an RRULE string ("RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=5") or null when there is no valid
// recurrence (one-off). Part order is fixed: FREQ ; INTERVAL ; BYDAY ; (COUNT | UNTIL).
export function toRRule(rec, { allDay = false, startIso = null } = {}) {
  if (!rec || typeof rec !== "object") return null;
  const FREQ = { daily: "DAILY", weekly: "WEEKLY", monthly: "MONTHLY" }[rec.freq];
  if (!FREQ) return null;                                   // unknown/absent freq -> one-off
  const parts = [`FREQ=${FREQ}`];

  const interval = Number(rec.interval);
  if (Number.isFinite(interval) && interval > 1) parts.push(`INTERVAL=${interval}`); // 0/1/missing -> default 1

  if (FREQ === "WEEKLY" && Array.isArray(rec.byday)) {      // BYDAY on WEEKLY only; never ordinal
    const ORDER = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
    const set = new Set(rec.byday.map((d) => String(d).toUpperCase()));
    const codes = ORDER.filter((d) => set.has(d));          // dedup + canonical order
    if (codes.length) parts.push(`BYDAY=${codes.join(",")}`);
  }

  const count = Number(rec.count);
  if (Number.isFinite(count) && count > 0) {
    parts.push(`COUNT=${count}`);                           // COUNT wins, UNTIL dropped — RRULE forbids both
  } else if (rec.until) {
    const startMs = Date.parse(startIso || "");
    const untilMs = Date.parse(rec.until);
    if (!Number.isFinite(untilMs)) return null;             // unparseable until -> one-off
    if (Number.isFinite(startMs) && untilMs <= startMs) return null; // past-until -> one-off
    const tok = toRRuleUntil(rec.until, allDay);
    if (!tok) return null;
    parts.push(`UNTIL=${tok}`);
  }
  return `RRULE:${parts.join(";")}`;
}

// Value-type-correct RRULE UNTIL token (RFC 5545: UNTIL must match DTSTART's value type).
// all-day series (DATE DTSTART) -> "YYYYMMDD". timed series -> UTC basic "YYYYMMDDTHHMMSSZ",
// pinned to the INCLUSIVE end of the until day in CAL_TZ. Returns null on an unparseable date.
export function toRRuleUntil(untilIso, allDay = false) {
  const ms = Date.parse(untilIso || "");
  if (!Number.isFinite(ms)) return null;
  const day = localDayStr(ms);                              // "YYYY-MM-DD" in CAL_TZ (existing helper)
  if (allDay) return day.replace(/-/g, "");                 // DATE form: YYYYMMDD
  const endMs = Date.parse(`${day}T23:59:59-03:00`);        // inclusive end of the local until-day
  return new Date(endMs).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z"); // YYYYMMDDTHHMMSSZ
}

// The READ direction: the all-day shape of a REAL Google event, in the house's own terms.
// The inverse of allDayWireDates — Google's `end.date` is EXCLUSIVE, so the INCLUSIVE last
// day is one day back, and it is null when that IS the start day (a single all-day day
// carries no range end — the create convention).
//
// `start_iso` is HERE, and not left to the callers, because an all-day event has NO
// `start.dateTime` at all: without it an edit draft has no day, and a RENAME of an all-day
// event would reach allDayWireDates with a null start and write `new Date(null)` — the
// event lands in 1970. It is the day at 00:00 -03:00, the same shape the model is asked to
// emit on a create. For a TIMED event this returns the event's own dateTime, unchanged.
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

// The Meet URL to surface in the confirm/done bubble: the event's hangoutLink first, else the
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
// Five branches, authoritative (PLAN §Interfaces):
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

// Do two attendee lists hold the same addresses (case-insensitive, order-independent)?
function sameEmailSet(a, b) {
  const norm = (list) => new Set((list || []).map((e) => String(e).toLowerCase()));
  const sa = norm(a);
  const sb = norm(b);
  if (sa.size !== sb.size) return false;
  for (const e of sa) if (!sb.has(e)) return false;
  return true;
}

// Nit A — the notify decision for an edit write. "all" if ANY substantive (non-location)
// field differs from the seed (`base` = editDraftFromEvent(ev)): the summary/agenda and every
// other field count. Otherwise "all" when the owner explicitly asked to notify (draft.notify),
// else "none" — a silent location-only edit does not spam the guests unless asked.
export function resolveSendUpdates(draft, base) {
  const substantiveChanged =
    (draft.title || "") !== (base.title || "") ||
    (draft.start_iso || null) !== (base.start_iso || null) ||
    (Number(draft.duration_min) || null) !== (Number(base.duration_min) || null) ||
    !!draft.all_day !== !!base.all_day ||
    (draft.all_day_end_iso || null) !== (base.all_day_end_iso || null) ||
    (draft.summary || "") !== (base.summary || "") ||
    !sameEmailSet(draft.emails, base.emails);
  if (substantiveChanged) return "all";
  return draft.notify ? "all" : "none";
}

function calendarClient(env) {
  return google.calendar({ version: "v3", auth: googleAuth(env) });
}

function calId(env) {
  return env.GOOGLE_CALENDAR_ID || "primary";
}

// The ONE events.insert in the repo. An ALL-DAY event is a different WIRE SHAPE, not a 24h
// timed block: `start:{date}/end:{date}` — the shape toListItem already recognises on the
// read side. `end_date` is EXCLUSIVE (Google's rule) and is computed by the caller.
async function createEvent(
  env,
  { title, emails, start_iso, end_iso, summary, all_day, start_date, end_date, recurrence, location, virtual }
) {
  const cal = calendarClient(env);
  // Idempotency: repeated "schedule this" (e.g. while testing) used to stack up
  // identical events, which then made "cancel this" leave siblings behind. If an
  // identical confirmed event already exists, reuse it instead of duplicating.
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
      // A RECURRING event carries an RRULE line; a one-off (recurrence null) omits the field
      // entirely — byte-identical to today's write.
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

// The ONE events.update in the repo: a FULL-RESOURCE REPLACE of an existing event, and the
// attendees are emailed about the change (the ordinary Google "updated invitation" — the
// same event, the same identity, no cancel-and-recreate).
//
// ⚠ UPDATE, NOT PATCH — deliberately. Converting a TIMED event into an ALL-DAY one means
// the old `start.dateTime` must not survive alongside the new `start.date`; a half-converted
// event is the corruption the edit path used to REFUSE the write rather than risk. Clearing
// a nested field through `events.patch` rests on Google's patch semantics, which no offline
// test can prove. A full replace makes the half-converted event STRUCTURALLY IMPOSSIBLE:
// `start` is a fresh literal, so nothing of the old one is left.
//
// Its one real cost: WHAT THE BODY DOES NOT CARRY, GOOGLE CLEARS. So the caller passes the
// FRESHLY FETCHED event and we spread it — reminders, colorId, recurrence, sequence and
// everything else we never touch ride along. resumeEditConfirm already re-fetches the event
// before writing, so this costs NO extra API call. (scripts/calendar-edit-selftest.mjs pins
// colorId as the tripwire for exactly this.)
async function updateEvent(env, eventId, ev, fields, sendUpdates = "all", conferenceVersion = false) {
  const cal = calendarClient(env);
  const r = await cal.events.update({
    calendarId: calId(env),
    eventId,
    sendUpdates, // "all" emails the attendees; "none" is a silent location-only edit (Nit A)
    // Only a conference-touching write carries the version — a plain edit never sends it, so
    // {...ev} re-supplies the live conferenceData and Google leaves an existing Meet untouched.
    ...(conferenceVersion ? { conferenceDataVersion: 1 } : {}),
    requestBody: { ...ev, ...fields },
  });
  return r.data;
}

// Find CONFIRMED events that are the same meeting: identical title and the exact
// same start instant. Used to (a) dedupe on create and (b) sweep every copy on
// delete, so cancelling a meeting doesn't leave duplicate rows behind.
// An ALL-DAY event carries `start.date`, never `start.dateTime` — so the timed branch below
// is BLIND to one, and without this dedupe-on-create would silently stop working for exactly
// the events card 0822a8e0 adds. The all-day case queries the START day's window (a range
// still STARTS inside it) and matches BOTH dates: matching only the start would dedupe a
// Mon–Wed order against a Monday-only event. The other caller (the delete sweep) passes no
// all-day flag → falsy → today's exact behaviour, byte for byte.
async function findConfirmedDuplicates(
  env,
  { title, startIso, excludeId, allDay, startDate, endDate }
) {
  if (!startIso) return [];
  if (allDay && !(startDate && endDate)) return [];
  const cal = calendarClient(env);
  const start = new Date(startIso).getTime();
  // Narrow window to bound the query: the start DAY for an all-day event, otherwise a
  // narrow window around the start instant.
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

// Identify which real calendar event(s) a request targets (cancel or edit), by
// MATCHING the details captured from the conversation against the calendar — not by
// trusting a decoded link alone. Signals, per candidate:
//   +100  the event id decoded from the replied-to link (strong, explicit)
//   + 40  same start instant as the captured date/time
//   + 30  an attendee email overlaps a captured participant email
// A candidate is a confident match at score >= 70, i.e. the decoded id, OR
// start+email together. A bare same-start coincidence (40) is NOT enough to act on
// — it could be a different meeting in the same slot. Returns confident matches
// (deduped by id), each with its event data.
async function matchEventTargets(env, { eidEventId, startIso, emails }) {
  const cal = calendarClient(env);
  const emailSet = new Set((emails || []).map((e) => String(e).toLowerCase()));
  const startMs = startIso ? new Date(startIso).getTime() : null;
  const candidates = new Map(); // id -> event

  // The link the owner replied to (may be absent or a non-decodable short link).
  if (eidEventId) {
    try {
      const ev = await getEvent(env, eidEventId);
      if (ev && ev.status === "confirmed") candidates.set(ev.id, ev);
    } catch {
      /* stale/undecodable — rely on the captured details below */
    }
  }

  // Everything sitting at the captured start instant.
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

// Cancel the matched event(s) AND, at delete time, re-inspect the calendar for
// any confirmed duplicate of the same meeting (same title + start) and remove
// those too. Returns how many distinct events were removed. A 410 (already
// deleted) on any single id is treated as success — the goal is "no copy survives".
async function cancelMeeting(env, { eventIds = [], title, startIso }) {
  const cal = calendarClient(env);
  const ids = new Set(eventIds.filter(Boolean));
  try {
    const dupes = await findConfirmedDuplicates(env, { title, startIso });
    for (const d of dupes) ids.add(d.id);
  } catch (e) {
    // If the duplicate lookup fails, still delete the matched targets below.
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

// A Google Calendar link carries an `eid` = base64url("<eventId> <calendarId>").
// Decode it back to the eventId we can act on.
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

async function interpret(ctx) {
  const { owner, anthropic, model, order, transcript, nowStr, contact, quoted } =
    ctx;
  const system = buildSystem(owner);
  const prompt = buildUserPrompt(owner, {
    order,
    transcript,
    nowStr,
    contact,
    quoted,
  });
  const msg = await anthropic.messages.create({
    model,
    max_tokens: 4096,
    system,
    output_config: jsonFormat(CAL_SCHEMA),
    messages: [{ role: "user", content: prompt }],
  });
  const info = readReply(msg, "calendar");
  console.log("CALENDAR RAW:", JSON.stringify(info));
  return info;
}

// ctx (from the orchestrator): { owner, tag, anthropic, model, order, transcript,
//   nowStr, contact, number, quoted, env, send }
export async function run(ctx) {
  const { number, send, session } = ctx;

  // CONTINUATION: resume a pending confirmation (e.g. "yes" to a cancellation).
  // Set by the orchestrator only when this message replies to the secretary's prompt.
  if (session?.intent === "delete" && session.stage === "await_confirmation") {
    return resumeDelete(ctx, session);
  }
  if (session?.intent === "create" && session.stage === "await_info") {
    return resumeInfo(ctx, session);
  }
  if (session?.intent === "create" && session.stage === "await_confirmation") {
    return resumeCreate(ctx, session);
  }
  if (session?.intent === "edit" && session.stage === "await_clarification") {
    return resumeEditClarify(ctx, session);
  }
  if (session?.intent === "edit" && session.stage === "await_confirmation") {
    return resumeEditConfirm(ctx, session);
  }

  // The orchestrator's merged router+extractor may ALREADY have extracted our declared inputs
  // (manifest.inputs), and a plain-code check has already said the SHAPE is good. Use it: that
  // is the whole round-trip this card removes. When it did not — a shape-invalid payload, or a
  // dual-intent turn where the payload belonged to another skill — fall back to our own
  // dedicated extraction call, exactly as before. No capability is lost either way; the worst
  // case is that the turn is as slow as it used to be.
  let info = ctx.info ?? null;
  if (!info) {
    try {
      info = await interpret(ctx);
    } catch (e) {
      console.error("Calendar/Claude error:", e);
      await ctx.sendFailure(number, reply(ctx.lang).thinkingError());
      return;
    }
  }

  if (info?.action === "delete") return handleDelete(ctx, info);
  if (info?.action === "create") return handleCreate(ctx, info);
  if (info?.action === "edit") return handleEdit(ctx, info);
  if (info?.action === "list") return handleList(ctx, info);

  await ctx.sendFailure(number, reply(ctx.lang).noAction({ summary: info?.summary }));
}

// ---- CREATE ----------------------------------------------------------------
// Create is fully STATEFUL and CONFIRM-FIRST. The flow always converges on a
// session: interpret (broad) -> if anything required is missing, a FOCUSED second
// LLM pass re-inspects the chat precisely for it -> still missing? open a gathering
// session and ASK, listening to ANY participant (awaitFrom:"any") until secure ->
// once complete, show the draft and wait for the owner's "yes" before writing to
// Google. Fallbacks (duration 45m, title from topic/names) never count as missing.
async function handleCreate(ctx, info) {
  const resolved = await resolveDraft(ctx, draftFromInfo(ctx, info));
  await advanceCreate(ctx, resolved);
}

// Required to create: a date/time, and an email for every named guest the owner has NOT
// told us he lacks one for. An event with ZERO outside guests is an ordinary, complete
// event. Everything else has a fallback and never blocks.
//
// The rule: a required field is legitimate only if a TRUTHFUL answer can satisfy it. The
// old ">= 1 attendee" invariant could not be satisfied by "nobody, it's just me", and the
// email requirement could not be satisfied by "I don't have hers" — so the owner could
// never leave the gathering loop. The email is still REQUIRED; it is now ANSWERABLE
// (`noEmail` = the owner said he hasn't got it), which is what was missing.
//
// ⚠ noTime STAYS. createFromDraft does `new Date(draft.start_iso)`: with a null start that
// is `new Date(null)` = the UNIX epoch, and the event lands in Google in 1970. This
// predicate is the ONLY thing guarding that write.
function missingOf(draft) {
  return {
    noTime: !draft.start_iso,
    emailNames: draft.participants
      .filter((p) => !p.email && !p.noEmail)
      .map((p) => p.name)
      .filter(Boolean),
  };
}

function isComplete(m) {
  return !m.noTime && m.emailNames.length === 0;
}

// The FOCUSED second pass: given what's missing, re-inspect the chat + latest
// message precisely for those fields and merge whatever it resolves. No LLM call
// when nothing is missing. Used both after the broad extraction and on each
// gathering message.
async function resolveDraft(ctx, draft) {
  const m = missingOf(draft);
  if (isComplete(m)) return draft;
  // gathering:false — on this immediate pass the "message" IS the order, so there is
  // nothing to classify; patch.decision is ignored here.
  const patch = await inspectMissing(ctx, draft, m, { gathering: false });
  return mergeDraft(ctx, draft, patch);
}

// Decide the next step from a draft: complete -> confirm; otherwise open (or
// refresh) the gathering session and ask precisely for what's still missing.
async function advanceCreate(ctx, draft) {
  const m = missingOf(draft);
  if (isComplete(m)) {
    await openCreateConfirm(ctx, draft);
    return;
  }
  await openInquiry(ctx, draft, m);
}

// Normalize an interpret()/review() result into the draft we store, render, and
// eventually insert. Applies the title fallback: inferred topic, else Owner/names.
export function draftFromInfo(ctx, info) {
  const { owner, contact } = ctx;
  // noEmail rides along: it is the owner's ANSWER ("I don't have hers"), and it must
  // survive every re-normalization or the email question comes back from the dead.
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

  // ALL-DAY. `all_day_end_iso` is the LAST day the event still COVERS — INCLUSIVE. The two
  // sanity clamps are normalizeAllDay's, and this is the one normalizer every create merge
  // path funnels through. applyPatchToDraft (edit) calls the same clamps, so a move that
  // strands the old range end behind the new start self-heals there too.
  const all_day = !!info.all_day;
  const all_day_end_iso = normalizeAllDay(info.start_iso, all_day, info.all_day_end_iso);

  // LOCATION. The single create-side normalizer every merge path funnels through (edge #11):
  // physical XOR virtual is decided here, once, by normalizeLocation — virtual wins, then a
  // non-empty verbatim address, else neither.
  const { location, virtual } = normalizeLocation(info.location, info.virtual);

  return {
    title,
    participants,
    start_iso: info.start_iso || null,
    duration_min,
    all_day,
    all_day_end_iso,
    summary: info.summary || "",
    // Kept RAW (no normalization here) — toRRule is the single validator, called at render
    // and write time. Plays no part in missingOf()/isComplete(): a null recurrence is an
    // ordinary one-off, never a missing field.
    recurrence: info.recurrence || null,
    location,
    virtual,
  };
}

function draftEmails(draft) {
  return (draft.participants || []).map((p) => p?.email).filter(Boolean);
}

// Named guests we will NOT be inviting: no email, and the owner has said he hasn't got
// one. They are named in the confirm draft and again in the confirmation — a person is
// never dropped silently.
function draftUninvited(draft) {
  return (draft.participants || []).filter((p) => !p.email && p.name).map((p) => p.name);
}

// The localized recurrence line for the confirm/done bubble, or null for a one-off. It MUST
// call toRRule with the SAME opts createFromDraft uses to WRITE the rule — `all_day` and
// `start_iso` transcribed, not re-derived — so the text line and the written RRULE can never
// diverge at a boundary. Gating the display on toRRule truthiness keeps wire and text in
// sync: an uncompilable or past-until recurrence (which toRRule drops to null) shows NO line
// and writes NO rule.
function recurrenceLineFor(draft, lang) {
  return toRRule(draft.recurrence, { allDay: !!draft.all_day, startIso: draft.start_iso })
    ? describeRecurrence(draft.recurrence, lang)
    : null;
}

// Open (or refresh) the confirmation session holding the draft and show it. The
// owner's next plain message resumes via resumeCreate. 10-min window to answer.
async function openCreateConfirm(ctx, draft) {
  const { number, send, sessions, remoteJid } = ctx;
  await sessions.set(
    remoteJid,
    {
      skill: "calendar_action",
      intent: "create",
      stage: "await_confirmation",
      awaitFrom: "owner", // only the owner approves their own event
      lang: ctx.lang, // reply to the continuation in the flow's language
      data: { draft },
    },
    600
  );
  await send(
    number,
    reply(ctx.lang).createConfirm({
      title: draft.title,
      emails: draftEmails(draft).join(", "),
      when: localizeWhen(ctx.lang, draft),
      // An all-day event has no duration to state — "(1440 min)" is the bug, not the event.
      duration: draft.all_day ? null : draft.duration_min,
      uninvited: draftUninvited(draft),
      recurrence: recurrenceLineFor(draft, ctx.lang),
      location: draft.location,
      virtual: draft.virtual,
    })
  );
}

// Actually write the confirmed draft to Google and report back. The INCLUSIVE -> EXCLUSIVE
// day conversion is allDayWireDates' — the one place it happens, shared with applyEditDraft.
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

  // Compile the RRULE with the SAME opts recurrenceLineFor uses to render the line — so the
  // written rule and the confirmed text agree. null (one-off) omits the field on the write.
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
      // The location line renders from the draft; the Meet link (may still be provisioning —
      // edge #8) is read from the created event, with the htmlLink above as the fallback.
      location: draft.location,
      virtual: draft.virtual,
      meetLink: meetLinkOf(ev),
    })
  );
}

// Resume a pending create. Runs for EVERY owner message while the session is open:
// classify + (if a change) re-draft in one call, then act. Silent on chatter.
async function resumeCreate(ctx, session) {
  const { number, send, sessions, remoteJid } = ctx;
  const draft = session.data?.draft;
  if (!draft) {
    await sessions.clear(remoteJid);
    return;
  }

  const review = await reviewCreate(ctx, draft);
  if (!review || review.decision === "unrelated") return; // not for us — ignore

  if (review.decision === "cancel") {
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).createCancelled({ title: draft.title }));
    return;
  }

  if (review.decision === "modify") {
    // Re-route the revised draft: re-show the confirm, or chase a newly-missing
    // email exactly like a fresh order (a change may drop an attendee's email).
    return advanceCreate(ctx, applyDraftUpdate(ctx, draft, review));
  }

  // decision === "confirm"
  try {
    await createFromDraft(ctx, draft);
    await sessions.clear(remoteJid);
  } catch (e) {
    console.error("Calendar error:", e?.response?.data || e?.message || e);
    await sessions.clear(remoteJid);
    await ctx.sendFailure(number, reply(ctx.lang).createGoogleError());
  }
}

// Carry what the owner has ALREADY ANSWERED onto a fresh guest list: a person matched by
// name keeps any known email and their noEmail flag. Without it, a later "modify" would
// resurrect an email question he has already answered. Shared with mergeDraft.
function carryNoEmail(prevList, nextList) {
  return (nextList || []).map((p) => {
    const was = (prevList || []).find(
      (q) => q?.name && p?.name && normName(q.name) === normName(p.name)
    );
    return {
      name: p?.name || null,
      email: p?.email || was?.email || null,
      noEmail: !!(p?.noEmail || was?.noEmail),
    };
  });
}

// Merge a "modify" review onto the current draft: prefer the review's fields, fall
// back to the previous draft for anything it didn't return, then re-normalize.
// Array.isArray, NOT `.length` — an EMPTIED guest list is an ANSWER ("don't invite
// anyone"), not an absence of information, and it must stick. Only a missing list
// (null/undefined) means "the review said nothing about the guests".
export function applyDraftUpdate(ctx, prev, review) {
  const participants = Array.isArray(review.participants)
    ? carryNoEmail(prev.participants, review.participants)
    : prev.participants;
  return draftFromInfo(ctx, {
    title: review.title ?? prev.title,
    participants,
    start_iso: review.start_iso ?? prev.start_iso,
    duration_min: review.duration_min ?? prev.duration_min,
    // `??`, so a modify that says nothing about them (a rename, an added guest) KEEPS them.
    // An explicit false still wins — that is the owner turning all-day off.
    all_day: review.all_day ?? prev.all_day,
    all_day_end_iso: review.all_day_end_iso ?? prev.all_day_end_iso,
    summary: review.summary ?? prev.summary,
    // DIRECT, not `?? prev`: for recurrence, null is the CLEAR value ("just once"), so
    // `?? prev` would make clearing impossible. The review copy makes the model echo the
    // current recurrence on every non-clearing modify and return null ONLY to clear, so a
    // direct read is correct. (Contrast all_day above: an explicit false is "turn off", so
    // it keeps `?? prev` to distinguish that from "not mentioned".)
    recurrence: review.recurrence,
    // DIRECT, same contract as recurrence: the model ECHOES the current location/virtual on
    // every non-location modify and returns location:null only to CLEAR, so a direct read is
    // correct. normalizeLocation (inside draftFromInfo) re-applies the XOR.
    location: review.location,
    virtual: review.virtual,
  });
}

// LLM: is the latest owner message a confirm / modify / cancel of the pending draft?
// Returns the parsed review (with a normalized decision) or null on doubt/error —
// null is treated by the caller as "ignore silently", the safe default.
async function reviewCreate(ctx, draft) {
  const { anthropic, model, owner, transcript, order, nowStr } = ctx;
  try {
    const msg = await anthropic.messages.create({
      model,
      max_tokens: 4096,
      system: buildCreateReviewSystem(owner),
      output_config: jsonFormat(REVIEW_SCHEMA),
      messages: [
        {
          role: "user",
          content: buildCreateReviewUser({
            draftJson: JSON.stringify(draft),
            transcript,
            latest: order,
            nowStr,
          }),
        },
      ],
    });
    const parsed = readReply(msg, "calendar");
    console.log("CREATE REVIEW RAW:", JSON.stringify(parsed));
    if (!parsed) return null;
    if (!["confirm", "modify", "cancel", "unrelated"].includes(parsed.decision)) {
      parsed.decision = "unrelated";
    }
    return parsed;
  } catch (e) {
    console.error("create review error:", e?.message || e);
    return null; // on error, do nothing (safe)
  }
}

// ---- CREATE: stateful gathering --------------------------------------------
// Ask precisely for what's missing and keep the session open, listening to ANY
// participant (awaitFrom:"any"), until every required field is secure. Each
// incoming message re-runs the focused resolver; progress → ask for the rest,
// complete → confirm, nothing new → stay silent (chatter).
async function openInquiry(ctx, draft, m) {
  const { number, send, sessions, remoteJid } = ctx;
  await sessions.set(
    remoteJid,
    {
      skill: "calendar_action",
      intent: "create",
      stage: "await_info",
      awaitFrom: "any", // the owner OR any attendee in the chat may answer
      lang: ctx.lang, // reply to the continuation in the flow's language
      data: { draft },
    },
    600
  );
  await send(number, reply(ctx.lang).inquiry(m));
}

const normName = (s) => String(s || "").trim().toLowerCase();

// Merge a resolver patch onto the draft: take start_iso if provided, and treat the
// resolver's guest list as AUTHORITATIVE — its own prompt promises the FULL list, so the
// list REPLACES the draft's rather than being appended to it. That is what makes an
// emptied list ("don't invite Laura") an answer, and it is what stops a substitution
// ("not Laura, Ana") from inviting BOTH — createEvent runs sendUpdates:"all", so an
// appended Laura is a real invite emailed to someone the owner removed.
// patch.participants === null still means "no information" → keep the previous list.
export function mergeDraft(ctx, prev, patch) {
  if (!patch) return prev;
  let participants = prev.participants.map((p) => ({ ...p }));

  if (Array.isArray(patch.participants)) {
    const clean = patch.participants.filter((p) => p && (p.name || p.email));
    const missing = participants.filter((p) => !p.email);
    const patchEmails = clean.filter(
      (p) => typeof p.email === "string" && p.email.includes("@")
    );
    // Bare-email fallback — FIRST, and unchanged. One attendee still missing an email +
    // one UN-named email in the patch → assign it directly. This is how a guest who
    // answers with nothing but her address is understood; it must NOT go through the
    // replace below, which would overwrite her name with the patch's null.
    if (missing.length === 1 && patchEmails.length === 1 && !patchEmails[0].name) {
      missing[0].email = patchEmails[0].email;
    } else {
      // Otherwise the patch's list wins, carrying over what we already know (a known
      // email, and the noEmail the owner already answered) for anyone matched by name.
      participants = carryNoEmail(participants, clean);
    }
  }

  // The NEGATIVE channel: the names the owner has ANSWERED that he has no email for.
  // They stay on the guest list (so he can be TOLD they are not invited) but no longer
  // block completion — the requirement is satisfied by the answer, not by an address.
  const noEmailFor = new Set(
    (patch.no_email_for || []).map((n) => normName(n)).filter(Boolean)
  );
  for (const p of participants) {
    if (p.name && noEmailFor.has(normName(p.name))) p.noEmail = true;
  }

  return draftFromInfo(ctx, {
    title: prev.title,
    participants,
    start_iso: patch.start_iso ?? prev.start_iso,
    duration_min: prev.duration_min,
    // Carried from prev, NOT from the patch: the resolver never returns these and never
    // needs to (gathering chases a time and emails, never a range). Without these two lines
    // the all-day flag is silently dropped on every gathering merge.
    all_day: prev.all_day,
    all_day_end_iso: prev.all_day_end_iso,
    summary: prev.summary,
    // Carried from prev, same as all_day above: the resolver chases a time and emails, never
    // the repeat rule, so without this line the recurrence is silently dropped on every
    // gathering merge.
    recurrence: prev.recurrence,
    // Carried from prev, same reason: the resolver never touches the place (edge #11), so
    // without these two lines location/virtual are silently dropped on every gathering merge —
    // exactly as all_day / recurrence are carried above.
    location: prev.location,
    virtual: prev.virtual,
  });
}

// Resume a gathering session. Runs for EVERY owner/contact message while open
// (awaitFrom:"any" — the session hears the whole chat), so it must first decide WHAT the
// message is: an answer, a cancellation, or chatter. It used to infer that from a field
// diff — "did the missing set shrink?" — which meant every truthful answer the code had
// no field for ("nobody", "I don't have her email", "forget it") was met with TOTAL
// SILENCE and the owner could not escape the loop. It now asks, with the same
// confirm|modify|cancel|unrelated channel the rest of the repo uses (6. Flight Search's
// resumeInfo is the template).
async function resumeInfo(ctx, session) {
  const { number, sessions, remoteJid } = ctx;
  const draft = session.data?.draft;
  if (!draft) {
    await sessions.clear(remoteJid);
    return;
  }

  const before = missingOf(draft);
  if (isComplete(before)) return advanceCreate(ctx, draft);

  const patch = await inspectMissing(ctx, draft, before, { gathering: true });

  // The model refused, or the API failed. This used to be silence too — indistinguishable
  // from "your message wasn't for me". Say something.
  if (!patch) {
    await ctx.sendFailure(number, reply(ctx.lang).thinkingError());
    return;
  }
  if (patch.decision === "unrelated") return; // chatter — the ONLY silent exit left
  if (patch.decision === "cancel") {
    await sessions.clear(remoteJid); // DISARM: an abandoned draft must not be resurrectable
    await ctx.send(number, reply(ctx.lang).createCancelled({ title: draft.title }));
    return;
  }

  // confirm | modify — an ANSWER. Merge it and move: ask for what is still missing, or
  // show the confirm draft.
  return advanceCreate(ctx, mergeDraft(ctx, draft, patch));
}

// LLM: the focused second pass. Told exactly what's missing, it resolves precisely
// those fields from the conversation + latest message, AND (while gathering) classifies
// what the message is. Returns the patch, or null on doubt/error — resolveDraft keeps the
// draft unchanged, resumeInfo now REPORTS the null instead of swallowing it.
async function inspectMissing(ctx, draft, m, { gathering = false } = {}) {
  const { anthropic, model, owner, transcript, order, nowStr } = ctx;
  try {
    const msg = await anthropic.messages.create({
      model,
      max_tokens: 2048,
      system: buildResolveSystem(owner),
      output_config: jsonFormat(RESOLVE_SCHEMA),
      messages: [
        {
          role: "user",
          content: buildResolveUser({
            draftJson: JSON.stringify(draft),
            needsTime: m.noTime,
            needEmailFor: m.emailNames,
            gathering,
            transcript,
            latest: order,
            nowStr,
          }),
        },
      ],
    });
    const patch = readReply(msg, "calendar");
    console.log("RESOLVE RAW:", JSON.stringify(patch));
    return patch;
  } catch (e) {
    console.error("resolve error:", e?.message || e);
    return null;
  }
}

// ---- EDIT / RESCHEDULE (Phase B) -------------------------------------------
// Change an EXISTING event the owner REPLIED to (its calendar link): move it,
// relength it, rename it, add/remove an attendee. Like create, edit is now
// CONFIRM-FIRST and stays open: the change is applied to a DRAFT of the event's
// target state, shown for confirmation, and only written to Google on the owner's
// "yes". While the confirm session is open the owner can keep refining the same
// event tagless ("actually 4:30", "also add bruno@x.com") — same review machinery
// as create (confirm | modify | cancel | unrelated). Nothing is written until "yes".

// Compact view of the real event handed to the focused edit pass. It must show the model
// WHAT IT IS EDITING: an all-day event has no `start.dateTime`, so reporting only that left
// the model blind to both the event's day and the fact that it is all-day at all.
function eventForLLM(ev) {
  const { all_day, start_iso, all_day_end_iso } = allDayFromEvent(ev);
  const end = all_day ? null : ev.end?.dateTime || null;
  const duration_min =
    start_iso && end ? Math.round((new Date(end) - new Date(start_iso)) / 60000) : null;
  // The current place, so the edit model sees what it is changing FROM.
  const { location, virtual } = locationFromEvent(ev);
  return {
    title: ev.summary || "",
    start_iso,
    end_iso: end,
    duration_min,
    all_day,
    all_day_end_iso,
    attendees: (ev.attendees || []).map((a) => a.email).filter(Boolean),
    location,
    virtual,
  };
}

// The editable DRAFT = the event's target state. Seeded from the current event, then each
// requested change is folded in; the confirm writes it to Google. Its ALL-DAY fields are
// the create draft's, with the same meaning — `all_day_end_iso` is the LAST day the event
// still covers, INCLUSIVE — so the same renderer (localizeWhen) and the same wire
// conversion (allDayWireDates) serve both sides. There is no second all-day model.
//
// `duration_min` falls back to 45 for an all-day event. That is not a guess about the
// event: it is the default length used only IF the owner converts it to a timed one
// ("na verdade é às 10h") without stating a length — exactly the create default.
export function editDraftFromEvent(ev) {
  const { all_day, start_iso, all_day_end_iso } = allDayFromEvent(ev);
  const end = all_day ? null : ev.end?.dateTime || null;
  const duration_min =
    start_iso && end ? Math.round((new Date(end) - new Date(start_iso)) / 60000) : 45;
  // Seed the current place from the event, and notify:false — the per-write notify signal is
  // set true only when the model reports an explicit "let the guests know" (applyPatchToDraft).
  const { location, virtual } = locationFromEvent(ev);
  return {
    title: ev.summary || "",
    start_iso,
    duration_min,
    all_day,
    all_day_end_iso,
    summary: ev.description || "",
    emails: (ev.attendees || []).map((a) => a.email).filter(Boolean),
    location,
    virtual,
    notify: false,
  };
}

// Does the patch actually change anything? (An all-null/empty patch means the model
// couldn't extract a change — distinct from a `clarify` question.)
//
// `new_all_day === true` is a change ON ITS OWN — "na verdade é o dia todo" says nothing
// else, and without this it was answered with "não consegui entender o que mudar". So is a
// new range end. `new_all_day === false` is NOT: see THE RULE below.
export function hasEditChange(p) {
  return !!(
    p.new_start_iso ||
    Number(p.new_duration_min) > 0 ||
    p.new_title ||
    (typeof p.new_summary === "string" && p.new_summary.trim()) ||
    p.new_all_day === true ||
    p.new_all_day_end_iso ||
    (Array.isArray(p.add_emails) && p.add_emails.length) ||
    (Array.isArray(p.remove_emails) && p.remove_emails.length) ||
    // A location change counts: a new address, switching to a Meet, or clearing the place.
    // `notify_guests` alone is NOT a change — it only steers who is emailed about a real one.
    (typeof p.new_location === "string" && p.new_location.trim()) ||
    p.new_virtual === true ||
    p.remove_location === true
  );
}

// Fold a change patch onto the draft (immutably): overwrite the touched fields, merge
// attendees (case-insensitive remove, then dedup add). Untouched fields carry over.
//
// ⚠ THE RULE — `new_all_day === false` IS HONOURED ONLY ALONGSIDE A `new_start_iso`.
// EDIT_SCHEMA REQUIRES the field, so a model answering an ordinary RENAME can emit `false`
// rather than `null` — and a naive fold would then SILENTLY CONVERT the owner's all-day
// event into a 45-minute block. That is the exact harm the old guard existed to prevent,
// re-entering through the front door. Turning all-day OFF means GIVING the event a time
// ("na verdade é às 10h") — always. So a bare `false` is IGNORED. `true` and a new range
// end are honoured on their own. Enforced HERE, in code, not in prompt hope.
export function applyPatchToDraft(draft, patch) {
  const d = { ...draft, emails: [...draft.emails] };
  if (patch.new_title) d.title = patch.new_title;
  if (typeof patch.new_summary === "string" && patch.new_summary.trim())
    d.summary = patch.new_summary;
  if (patch.new_start_iso) d.start_iso = patch.new_start_iso;
  if (Number(patch.new_duration_min) > 0) d.duration_min = Number(patch.new_duration_min);

  if (patch.new_all_day === true) d.all_day = true;
  else if (patch.new_all_day === false && patch.new_start_iso) d.all_day = false;
  if (patch.new_all_day_end_iso) d.all_day_end_iso = patch.new_all_day_end_iso;
  // The same two clamps draftFromInfo applies, so a move that strands the old range end
  // behind the new start self-heals into a single day instead of writing a backwards
  // event, and turning all-day off drops the range end with it.
  d.all_day_end_iso = normalizeAllDay(d.start_iso, d.all_day, d.all_day_end_iso);

  const remove = new Set(
    (patch.remove_emails || []).map((e) => String(e || "").trim().toLowerCase())
  );
  d.emails = d.emails.filter((e) => !remove.has(String(e).toLowerCase()));
  const have = new Set(d.emails.map((e) => String(e).toLowerCase()));
  for (const e of (patch.add_emails || []).map((x) => String(x || "").trim()).filter(Boolean)) {
    if (!have.has(e.toLowerCase())) {
      d.emails.push(e);
      have.add(e.toLowerCase());
    }
  }

  // LOCATION — XOR + THE-RULE discipline (mirrors new_all_day). new_virtual:true makes it a
  // Meet, dropping any address; else a non-empty new_location makes it physical, dropping any
  // Meet; else remove_location clears BOTH. A BARE new_virtual:false is IGNORED — turning video
  // off means GIVING the event an address (via new_location), exactly as new_all_day:false
  // needs a time. normalizeLocation then re-applies the XOR, once.
  if (patch.new_virtual === true) {
    d.virtual = true;
    d.location = null;
  } else if (typeof patch.new_location === "string" && patch.new_location.trim()) {
    d.location = patch.new_location;
    d.virtual = false;
  } else if (patch.remove_location === true) {
    d.location = null;
    d.virtual = false;
  }
  const loc = normalizeLocation(d.location, d.virtual);
  d.location = loc.location;
  d.virtual = loc.virtual;

  // notify is STICKY across the edit's refinements: once the owner asks to let the guests know,
  // it stays on. Set true ONLY on an explicit request (resolveSendUpdates reads it).
  if (patch.notify_guests === true) d.notify = true;

  return d;
}

// The draft rendered as an "event" for the review LLM (so it judges against the
// currently-proposed target, not the original). An ALL-DAY target has no end instant —
// printing start+duration for one describes an event that does not exist.
function draftAsEventJson(d) {
  const end_iso =
    d.start_iso && !d.all_day
      ? new Date(new Date(d.start_iso).getTime() + d.duration_min * 60000).toISOString()
      : null;
  return JSON.stringify({
    title: d.title,
    start_iso: d.start_iso,
    end_iso,
    duration_min: d.duration_min,
    all_day: !!d.all_day,
    all_day_end_iso: d.all_day_end_iso || null,
    attendees: d.emails,
    // The proposed place, so the edit-review model judges refinements against the target.
    location: d.location,
    virtual: !!d.virtual,
  });
}

// The focused EDIT extraction (first pass): given the real event + the change request,
// resolve only the changed fields (or a clarify question). Throws on API error (callers
// handle it); returns null on a refusal/unparseable reply.
async function interpretEdit(ctx, ev) {
  const { anthropic, model, owner, transcript, order, nowStr } = ctx;
  const msg = await anthropic.messages.create({
    model,
    max_tokens: 2048,
    system: buildEditSystem(owner),
    output_config: jsonFormat(EDIT_SCHEMA),
    messages: [
      {
        role: "user",
        content: buildEditUser({
          eventJson: JSON.stringify(eventForLLM(ev)),
          transcript,
          latest: order,
          nowStr,
        }),
      },
    ],
  });
  const patch = readReply(msg, "calendar");
  console.log("EDIT RAW:", JSON.stringify(patch));
  return patch;
}

// The confirm-step review (runs for every owner message while confirming): one call
// that BOTH classifies (confirm | modify | cancel | unrelated) AND, for a modify,
// returns the further change to fold in. Null on doubt/error → caller ignores silently.
async function reviewEdit(ctx, draft) {
  const { anthropic, model, owner, transcript, order, nowStr } = ctx;
  try {
    const msg = await anthropic.messages.create({
      model,
      max_tokens: 2048,
      system: buildEditReviewSystem(owner),
      output_config: jsonFormat(EDIT_REVIEW_SCHEMA),
      messages: [
        {
          role: "user",
          content: buildEditReviewUser({
            eventJson: draftAsEventJson(draft),
            transcript,
            latest: order,
            nowStr,
          }),
        },
      ],
    });
    const parsed = readReply(msg, "calendar");
    console.log("EDIT REVIEW RAW:", JSON.stringify(parsed));
    if (!parsed) return null;
    if (!["confirm", "modify", "cancel", "unrelated"].includes(parsed.decision)) {
      parsed.decision = "unrelated";
    }
    return parsed;
  } catch (e) {
    console.error("edit review error:", e?.message || e);
    return null; // on error, do nothing (safe)
  }
}

// Open (or refresh) the confirm session holding the draft, and show the target state.
// The owner's next plain message resumes via resumeEditConfirm. 10-min window.
async function openEditConfirm(ctx, eventId, draft, base) {
  const { number, send, sessions, remoteJid } = ctx;
  // `base` (the event's CURRENT state, editDraftFromEvent(ev)) rides the session so the notify
  // decision survives the confirm round-trip — resumeEditConfirm reads it back and re-passes it.
  await sessions.set(
    remoteJid,
    {
      skill: "calendar_action",
      intent: "edit",
      stage: "await_confirmation",
      awaitFrom: "owner", // only the owner approves changes to their event
      lang: ctx.lang,
      data: { eventId, draft, base },
    },
    600
  );
  // Preview whether the guests will be emailed (Nit A) so the bubble tells the truth: a silent
  // location-only edit says "I won't notify"; a substantive one keeps "notify everyone".
  const willNotify = resolveSendUpdates(draft, base) === "all";
  await send(
    number,
    reply(ctx.lang).editConfirm({
      title: draft.title,
      emails: draft.emails.join(", "),
      // localizeWhen, not localizeDate: an all-day target has no clock time to print, and
      // an all-day event has no duration to state. Identical to openCreateConfirm.
      when: localizeWhen(ctx.lang, draft),
      duration: draft.all_day ? null : draft.duration_min,
      location: draft.location,
      virtual: draft.virtual,
      notifyGuests: willNotify,
    })
  );
}

// Write the confirmed draft to Google (a full-resource update of the existing event) and
// report back. `ev` is the FRESHLY FETCHED event resource — see updateEvent for why the
// caller owns the fetch and why the body is spread from it.
//
// The old guard here REFUSED to write a start/end for an all-day event, because the only
// shape it knew how to write was a `dateTime` one, and that would have converted the event
// into a timed block. Its intent is honoured, not deleted: an all-day draft is now written
// in the ALL-DAY WIRE SHAPE (`start:{date}` / `end:{date}`, the end EXCLUSIVE — the same
// conversion createFromDraft makes), so there is nothing left to refuse.
async function applyEditDraft(ctx, eventId, draft, ev) {
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

  // The location/conference fields, computed against the event's CURRENT place (base). This is
  // where the conditional conferenceDataVersion (Nit C) and Meet-clear (Nit D) come from. The
  // Meet requestId seed is the eventId — deterministic and idempotent per Google.
  const base = editDraftFromEvent(ev);
  const { fields: locFields, conferenceVersion } = locationUpdateFields(draft, base, eventId);
  Object.assign(fields, locFields);

  // Nit A: notify the guests only when something substantive changed, or the owner asked. A
  // silent location-only edit passes sendUpdates:"none".
  const sendUpdates = resolveSendUpdates(draft, base);
  const updated = await updateEvent(env, eventId, ev, fields, sendUpdates, conferenceVersion);

  // Rendered from the DRAFT, not by reading `updated.start.dateTime` back — which is null
  // for an all-day event, and is where "(sem horário)" came from.
  await send(
    number,
    reply(ctx.lang).editDone({
      title: draft.title || "(untitled)",
      emails: draft.emails.join(", "),
      when: localizeWhen(ctx.lang, draft),
      duration: all_day ? null : draft.duration_min,
      link: updated.htmlLink || ev.htmlLink || "",
      // The location line renders from the draft; the Meet link is read back from the updated
      // event (may still be provisioning — edge #8), with the htmlLink above as the fallback.
      location: draft.location,
      virtual: draft.virtual,
      meetLink: meetLinkOf(updated),
      notified: sendUpdates === "all",
    })
  );
}

async function handleEdit(ctx, info) {
  const { number, env, send, tag, quoted, sessions, remoteJid } = ctx;

  // Resolve the event to change the SAME way delete does: MATCH the event's identity
  // against the calendar, not just a decoded link. `info.start_iso` here is the event's
  // CURRENT start (the locator the extraction reads from the replied-to invite/summary or
  // the conversation — NOT the new time being requested; that change is extracted later by
  // interpretEdit). This works whether the owner replied to the invite (link), the
  // summary/confirm bubble (current start + email, no link), or a tagless request that
  // names who + when.
  const participants = Array.isArray(info?.participants) ? info.participants : [];
  const emails = participants.map((p) => p?.email).filter(Boolean);
  const startIso = info?.start_iso || null; // the event's CURRENT start, used to find it
  const eidEventId = resolveEventId(quoted?.calendarLink); // may be null

  // Same guard as delete: need the link, or start+email together.
  if (!eidEventId && !(startIso && emails.length)) {
    await send(number, reply(ctx.lang).editNeedSignal({ tag }));
    return;
  }

  let matches;
  try {
    matches = await matchEventTargets(env, { eidEventId, startIso, emails });
  } catch (e) {
    console.error("Calendar edit match error:", e?.response?.data || e?.message || e);
    await ctx.sendFailure(number, reply(ctx.lang).editCheckError());
    return;
  }
  if (!matches.length) {
    await ctx.sendFailure(number, reply(ctx.lang).editNoMatch());
    return;
  }

  // Matcher returns full, confirmed-only event resources; patch the primary (same
  // "primary" pick delete makes for display). The confirm-first step below shows the
  // target, so a wrong pick among same-slot dupes is catchable before any write.
  const ev = matches[0];
  const eventId = ev.id;

  let patch;
  try {
    patch = await interpretEdit(ctx, ev);
  } catch (e) {
    console.error("edit interpret error:", e?.message || e);
    await ctx.sendFailure(number, reply(ctx.lang).editCheckError());
    return;
  }
  if (!patch) {
    await ctx.sendFailure(number, reply(ctx.lang).editCheckError());
    return;
  }

  // Ambiguous / missing detail → ask, keep the event id, resume on the answer (which
  // then rolls into the confirm below).
  if (patch.clarify && !hasEditChange(patch)) {
    await sessions.set(
      remoteJid,
      {
        skill: "calendar_action",
        intent: "edit",
        stage: "await_clarification",
        awaitFrom: "owner", // only the owner edits their own event
        lang: ctx.lang,
        data: { eventId },
      },
      600 // 10 min window to answer
    );
    await send(number, reply(ctx.lang).editClarify(patch.clarify));
    return;
  }

  if (!hasEditChange(patch)) {
    await send(number, reply(ctx.lang).editNoChange());
    return;
  }

  // Confirm-first: fold the change into a draft and ask before writing anything. `base` is the
  // event's current state — the notify decision (Nit A) is computed against it.
  const base = editDraftFromEvent(ev);
  const draft = applyPatchToDraft(base, patch);
  await openEditConfirm(ctx, eventId, draft, base);
}

// Resume a pending edit CLARIFICATION (the first request was ambiguous). Re-inspect the
// fresh event against the answer; once it resolves to a concrete change, roll into the
// confirm; else stay silent (chatter / still ambiguous) until answered or the TTL.
async function resumeEditClarify(ctx, session) {
  const { env, sessions, remoteJid } = ctx;
  const eventId = session.data?.eventId;
  if (!eventId) {
    await sessions.clear(remoteJid);
    return;
  }

  let ev;
  try {
    ev = await getEvent(env, eventId);
  } catch {
    await sessions.clear(remoteJid); // event vanished — drop the stale session
    return;
  }
  if (!ev || ev.status !== "confirmed") {
    await sessions.clear(remoteJid);
    return;
  }

  let patch;
  try {
    patch = await interpretEdit(ctx, ev);
  } catch (e) {
    console.error("edit clarify interpret error:", e?.message || e);
    return; // transient — keep the session, let them try again
  }
  if (!patch || !hasEditChange(patch)) return; // still ambiguous / chatter — wait

  const base = editDraftFromEvent(ev);
  const draft = applyPatchToDraft(base, patch);
  await openEditConfirm(ctx, eventId, draft, base);
}

// Resume a pending edit CONFIRMATION. Runs for every owner message while open: one
// review call classifies + (for a change) re-drafts, then acts. Stays open across
// multiple refinements; silent on chatter; writes to Google only on "yes".
async function resumeEditConfirm(ctx, session) {
  const { number, env, send, sessions, remoteJid } = ctx;
  const { eventId, draft, base } = session.data || {};
  if (!eventId || !draft) {
    await sessions.clear(remoteJid);
    return;
  }

  const review = await reviewEdit(ctx, draft);
  if (!review || review.decision === "unrelated") return; // not for us — ignore

  if (review.decision === "cancel") {
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).editCancelled({ title: draft.title }));
    return;
  }

  if (review.decision === "modify") {
    // Ambiguous further change → ask and keep the session (draft unchanged). `base` (the
    // event's current state) rides through so resolveSendUpdates still has its seed.
    if (!hasEditChange(review) && review.clarify) {
      await openEditConfirm(ctx, eventId, draft, base); // refresh TTL, keep draft
      await send(number, reply(ctx.lang).editClarify(review.clarify));
      return;
    }
    if (!hasEditChange(review)) return; // nothing new resolved — stay silent
    const updated = applyPatchToDraft(draft, review);
    await openEditConfirm(ctx, eventId, updated, base); // re-show the revised draft, keep open
    return;
  }

  // decision === "confirm" — write it now.
  try {
    // Re-check the event still exists before writing (it may have been deleted). The
    // fetched resource is then HANDED to applyEditDraft: events.update replaces the whole
    // event, so the body must carry everything we do not touch. Same fetch, no extra call.
    const ev = await getEvent(env, eventId);
    if (!ev || ev.status !== "confirmed") {
      await sessions.clear(remoteJid);
      await ctx.sendFailure(number, reply(ctx.lang).editNoMatch());
      return;
    }
    await applyEditDraft(ctx, eventId, draft, ev);
    await sessions.clear(remoteJid);
  } catch (e) {
    console.error("Calendar edit update error:", e?.response?.data || e?.message || e);
    await sessions.clear(remoteJid);
    await ctx.sendFailure(number, reply(ctx.lang).editGoogleError());
  }
}

// ---- DELETE ----------------------------------------------------------------
// Don't trust the link alone: gather what the conversation says about the event
// (start time, participant emails) PLUS the id decoded from any replied-to link,
// then MATCH that against the real calendar. Only open the confirmation SESSION
// when a confident match is found. The "yes" arrives as a continuation (handled
// by resumeDelete) — no @secretary tag needed.
async function handleDelete(ctx, info) {
  const { number, env, send, tag, quoted, sessions, remoteJid } = ctx;

  // Identity captured from the request/conversation, plus the link as one signal.
  const participants = Array.isArray(info?.participants) ? info.participants : [];
  const emails = participants.map((p) => p?.email).filter(Boolean);
  const startIso = info?.start_iso || null;
  const eidEventId = resolveEventId(quoted?.calendarLink); // may be null

  // Need at least one usable signal beyond a bare start time to be sure.
  if (!eidEventId && !(startIso && emails.length)) {
    await send(number, reply(ctx.lang).deleteNeedSignal({ tag }));
    return;
  }

  let matches;
  try {
    matches = await matchEventTargets(env, { eidEventId, startIso, emails });
  } catch (e) {
    console.error("Calendar match error:", e?.response?.data || e?.message || e);
    await ctx.sendFailure(number, reply(ctx.lang).deleteCheckError());
    return;
  }

  if (!matches.length) {
    await ctx.sendFailure(number, reply(ctx.lang).deleteNoMatch());
    return;
  }

  // Confident matches of the same meeting (dupes included). Describe them from the
  // first match; the confirm-time sweep re-checks the calendar and removes any copy.
  const primary = matches[0];
  const title = primary.summary || "(untitled)";
  const start = primary.start?.dateTime || startIso || null;
  const when = localizeDate(ctx.lang, primary.start?.dateTime || startIso);
  const ids = matches.map((e) => e.id);

  // Confirm-first: remember the matched ids + identity and ask. The owner can just
  // type "yes"/"no" (no reply, no tag); the secretary watches and ignores chatter.
  await sessions.set(
    remoteJid,
    {
      skill: "calendar_action",
      intent: "delete",
      stage: "await_confirmation",
      awaitFrom: "owner", // only the owner confirms their own cancellation
      lang: ctx.lang, // reply to the "yes"/"no" in the flow's language
      data: { ids, title, when, start },
    },
    600 // 10 min window to confirm
  );
  await send(
    number,
    reply(ctx.lang).deleteConfirm({ title, when, count: ids.length })
  );
}

// Resume a pending cancellation. Called for EVERY owner message while the session
// is open — so we ask the LLM whether this message actually confirms/declines, and
// stay SILENT on normal chatter (no nagging, no accidental deletes).
async function resumeDelete(ctx, session) {
  const { number, env, send, sessions, remoteJid } = ctx;
  const { ids, title, when, start } = session.data || {};

  const decision = await classifyConfirmation(ctx, {
    action: `cancel the event "${title}"${when ? ` at ${when}` : ""}`,
    who: "calendar",
  });

  if (decision === "unrelated") return; // not a response to us — ignore silently

  if (decision === "decline") {
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).deleteKeep({ title }));
    return;
  }

  // decision === "confirm"
  try {
    const n = await cancelMeeting(env, {
      eventIds: ids || [],
      title,
      startIso: start,
    });
    await sessions.clear(remoteJid);
    await send(number, reply(ctx.lang).deleteCancelled({ title, removed: n }));
  } catch (e) {
    console.error("Calendar delete error:", e?.response?.data || e?.message || e);
    await sessions.clear(remoteJid);
    await ctx.sendFailure(number, reply(ctx.lang).deleteGoogleError());
  }
}

// ---- LIST (read-only) ------------------------------------------------------
// Answer a read-only question about the schedule. The simplest action: no session,
// no confirm, no write — resolve the window (or a forward scan for "next"), fetch,
// and reply. The window comes from interpret() (list_mode + range_start_iso/
// range_end_iso); an unresolved window defaults to the rest of today.
async function handleList(ctx, info) {
  const { env, number, send, lang } = ctx;
  const now = Date.now();
  const cal = calendarClient(env);

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
      return ctx.sendFailure(number, reply(lang).listError());
    }
    const next = items.find((e) => e.start?.dateTime || e.start?.date);
    return send(number, reply(lang).listNext({ event: next ? toListItem(next) : null }));
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
    return ctx.sendFailure(number, reply(lang).listError());
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
}

// End-of-day (23:59:59.999) for the calendar TZ, in ms. São Paulo is a fixed -03:00
// offset (Brazil has no DST), matching the -03:00 used throughout this skill.
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
// All-day events carry a date (no dateTime); timed events carry start/end instants.
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
  // External attendees only: drop the owner's own entry (self) and room resources.
  const emails = (e.attendees || [])
    .filter((a) => a.email && !a.self && !a.resource)
    .map((a) => a.email);
  return { allDay, startIso, dayMs, title: e.summary || "", emails, durationMin };
}
