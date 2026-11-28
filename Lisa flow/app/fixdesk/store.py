"""The fix loop's queue: one case per session the judge found a mistake in.

Three tables in the log schema, written only here. Like the review layer, the desk READS the
log and the reviews and never writes to them.

A case moves one way through its states, and every move is a compare-and-set on the state it is
leaving, so the desk in the Lisa process and the worker on the Mac (which reaches the same
tables through `cli.py`) cannot both act on one case:

    new → investigating → planned → proposed → approved → fixing → ready | shipped | failed
                        ↘ duplicate | already_fixed | no_action        (the investigator's call)
                                    ↘ dismissed (he said skip) | expired (no answer)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger("mary.fixdesk.store")

# Timing codes that open a case: the ones where nothing reached the chat. A slow reply is rarely
# one bug, so it stays in the review report only.
TIMING_CODES = ("no_answer_error", "delivery_failed", "window_expired")

OPEN_KINDS = ("bug", "judge", "decision")                    # a plan he is shown
CLOSED_KINDS = ("duplicate", "already_fixed", "no_action")   # nothing to show him
RESULT_STATES = ("ready", "shipped", "failed")
MAX_ATTEMPTS = 3   # runs that may die on one case before it is set aside


class FixStore:
    def __init__(self, dsn: str, *, schema: str = "mary_log") -> None:
        self.dsn = dsn
        self.schema = schema
        self._pool: Any = None

    async def open(self) -> None:
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(self.dsn, min_size=1, max_size=2, open=False)
        await self._pool.open()
        async with self._pool.connection() as conn:
            await conn.execute(self._ddl())

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # -- small helpers -------------------------------------------------------
    async def _rows(self, q: str, params: Any = None) -> list[dict]:
        from psycopg.rows import dict_row

        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(q, params)
                return list(await cur.fetchall()) if cur.description else []

    async def _one(self, q: str, params: Any = None) -> Optional[dict]:
        rows = await self._rows(q, params)
        return rows[0] if rows else None

    async def event(self, case_id: Optional[int], kind: str, detail: Any = None) -> None:
        try:
            await self._rows(
                f"INSERT INTO {self.schema}.fix_events (case_id, kind, detail) VALUES (%s, %s, %s)",
                (case_id, kind, json.dumps(detail, default=str) if detail is not None else None),
            )
        except Exception as exc:  # the trail is a nicety; a move must not fail for it
            log.warning("fix event not written: %s", exc)

    async def _move(self, case_id: int, frm: tuple[str, ...], to: str, sets: str = "",
                    params: tuple = ()) -> Optional[dict]:
        """Compare-and-set one case's state. Returns the row when it moved, None when the case
        was no longer in a state this move starts from."""
        s = self.schema
        row = await self._one(
            f"""UPDATE {s}.fix_cases SET status = %s, updated_at = now(){', ' + sets if sets else ''}
                 WHERE id = %s AND status = ANY(%s) RETURNING *""",
            (to, *params, case_id, list(frm)),
        )
        if row:
            await self.event(case_id, f"{to}", None)
        return row

    # -- the trigger ----------------------------------------------------------
    async def qualifying_codes(self, loop_id: str, judge_version: str) -> list[str]:
        """The codes on this session that open a case: anything the judge filed, and the timing
        failures where nothing arrived."""
        s = self.schema
        rows = await self._rows(
            f"""SELECT DISTINCT f.code
                  FROM {s}.findings f JOIN {s}.reviews r ON r.id = f.review_id
                 WHERE r.loop_id = %s AND r.judge_version = %s
                   AND (f.source = 'judge' OR f.code = ANY(%s))
                 ORDER BY 1""",
            (loop_id, judge_version, list(TIMING_CODES)),
        )
        return [r["code"] for r in rows]

    async def open_case(self, loop_id: str, judge_version: str, codes: list[str]) -> Optional[int]:
        """One case per session, ever: a re-grade under a new rubric does not open a second."""
        s = self.schema
        row = await self._one(
            f"""INSERT INTO {s}.fix_cases (loop_id, judge_version, chat_id, codes)
                SELECT %s, %s, (SELECT chat_id FROM {s}.loops WHERE loop_id = %s), %s
                ON CONFLICT (loop_id) DO NOTHING RETURNING id""",
            (loop_id, judge_version, loop_id, codes),
        )
        if row:
            await self.event(row["id"], "new", {"codes": codes})
            return row["id"]
        return None

    # -- the desk: what to send ------------------------------------------------
    async def due_proposals(self, limit: int) -> list[dict]:
        if limit <= 0:
            return []
        return await self._rows(
            f"SELECT * FROM {self.schema}.fix_cases WHERE status = 'planned' ORDER BY id LIMIT %s",
            (limit,),
        )

    async def proposed_today(self, tz_name: str) -> int:
        row = await self._one(
            f"""SELECT count(*) AS n FROM {self.schema}.fix_cases
                 WHERE proposed_at IS NOT NULL
                   AND (proposed_at AT TIME ZONE %s)::date = (now() AT TIME ZONE %s)::date""",
            (tz_name, tz_name),
        )
        return int((row or {}).get("n") or 0)

    async def mark_proposed(self, case_id: int, wa_id: str) -> None:
        await self._move(case_id, ("planned",), "proposed",
                         "proposal_wa_id = %s, proposed_at = now()", (wa_id,))

    async def due_results(self) -> list[dict]:
        return await self._rows(
            f"""SELECT * FROM {self.schema}.fix_cases
                 WHERE status = ANY(%s) AND result_sent_at IS NULL AND approved_at IS NOT NULL
                 ORDER BY id""",
            (list(RESULT_STATES),),
        )

    async def mark_result_sent(self, case_id: int, wa_id: str) -> None:
        await self._rows(
            f"""UPDATE {self.schema}.fix_cases
                   SET result_sent_at = now(), result_wa_id = %s, updated_at = now() WHERE id = %s""",
            (wa_id, case_id),
        )
        await self.event(case_id, "result_sent", None)

    async def expire(self, days: int) -> int:
        rows = await self._rows(
            f"""UPDATE {self.schema}.fix_cases SET status = 'expired', updated_at = now()
                 WHERE status = 'proposed' AND proposed_at < now() - make_interval(days => %s)
                 RETURNING id""",
            (days,),
        )
        return len(rows)

    # -- the desk: his answer ---------------------------------------------------
    async def find_for_reply(self, quoted_id: Optional[str], case_id: Optional[int]) -> Optional[dict]:
        """Which case an answer is about: the message he replied to, else the case he named,
        else the newest plan still waiting for an answer."""
        s = self.schema
        if quoted_id:
            row = await self._one(
                f"""SELECT * FROM {s}.fix_cases
                     WHERE proposal_wa_id = %s OR result_wa_id = %s ORDER BY id DESC LIMIT 1""",
                (quoted_id, quoted_id),
            )
            if row:
                return row
        if case_id is not None:
            return await self._one(f"SELECT * FROM {s}.fix_cases WHERE id = %s", (case_id,))
        return await self._one(
            f"SELECT * FROM {s}.fix_cases WHERE status = 'proposed' ORDER BY proposed_at DESC LIMIT 1"
        )

    async def approve(self, case_id: int, note: str, wa_id: Optional[str]) -> bool:
        row = await self._move(case_id, ("proposed",), "approved",
                               "approved_at = now(), approval_note = %s, approval_wa_id = %s",
                               (note or None, wa_id))
        return row is not None

    async def dismiss(self, case_id: int) -> bool:
        return (await self._move(case_id, ("proposed",), "dismissed")) is not None

    async def worker_seen(self) -> Optional[dict]:
        return await self._one(
            f"SELECT host, seen_at, info FROM {self.schema}.fix_worker ORDER BY seen_at DESC LIMIT 1"
        )

    # -- the worker on the Mac (through cli.py) ------------------------------------
    async def heartbeat(self, host: str, info: dict) -> None:
        await self._rows(
            f"""INSERT INTO {self.schema}.fix_worker (host, seen_at, info) VALUES (%s, now(), %s)
                ON CONFLICT (host) DO UPDATE SET seen_at = now(), info = EXCLUDED.info""",
            (host, json.dumps(info, default=str)),
        )

    async def pull(self, *, new_limit: int = 10) -> dict:
        """Everything one daily run needs: the cases he approved, the new ones with their
        evidence, and the recent cases to recognise a repeat against."""
        s = self.schema
        approved = await self._rows(
            f"SELECT * FROM {s}.fix_cases WHERE status = 'approved' ORDER BY approved_at")
        new = await self._rows(
            f"SELECT * FROM {s}.fix_cases WHERE status = 'new' ORDER BY id LIMIT %s", (new_limit,))
        for case in new:
            case["evidence"] = await self.evidence(case["loop_id"], case["judge_version"])
        recent = await self._rows(
            f"""SELECT id, status, codes, created_at, seen, duplicate_of,
                       plan->>'kind' AS kind, plan->>'issue' AS issue,
                       plan->>'diagnosis' AS diagnosis, plan->>'fix' AS fix,
                       result->>'commit' AS commit, result->>'branch' AS branch
                  FROM {s}.fix_cases
                 WHERE plan IS NOT NULL AND created_at > now() - interval '45 days'
                   AND status NOT IN ('duplicate')
                 ORDER BY id DESC LIMIT 60""")
        return {"approved": approved, "new": new, "recent": recent}

    async def evidence(self, loop_id: str, judge_version: str) -> dict:
        s = self.schema
        loop = await self._one(
            f"""SELECT loop_id, chat_id, started_at, updated_at, prompt_version, model, lang, trigger
                  FROM {s}.loops WHERE loop_id = %s""", (loop_id,))
        reviews = await self._rows(
            f"""SELECT id, seq, ts, turn_kind, domain, prompt_version, verdict, confidence,
                       rationale, proposed_gap, reply_text, task_class, wait_seconds, timing_band,
                       delivery, turn_error
                  FROM {s}.reviews WHERE loop_id = %s AND judge_version = %s ORDER BY seq""",
            (loop_id, judge_version))
        findings = await self._rows(
            f"""SELECT r.seq, f.code, f.severity, f.evidence, f.harm, f.source
                  FROM {s}.findings f JOIN {s}.reviews r ON r.id = f.review_id
                 WHERE r.loop_id = %s AND r.judge_version = %s ORDER BY r.seq, f.id""",
            (loop_id, judge_version))
        transcript = await self._rows(
            f"""SELECT seq, ts, who, text FROM {s}.events
                 WHERE loop_id = %s AND stream = 'transcript' ORDER BY seq""", (loop_id,))
        return {"loop": loop, "reviews": reviews, "findings": findings, "transcript": transcript}

    async def claim(self, case_id: int, to: str) -> Optional[dict]:
        """The worker takes a case: new → investigating, or approved → fixing."""
        frm = {"investigating": ("new",), "fixing": ("approved",)}.get(to)
        if not frm:
            return None
        return await self._move(case_id, frm, to, "attempts = attempts + 1")

    async def release(self, case_id: int, error: str) -> Optional[str]:
        """A run died on this case. Put it back for the next run, or set it aside after
        MAX_ATTEMPTS: an approved one then fails out loud, a new one goes quiet as `stuck`."""
        s = self.schema
        row = await self._one(f"SELECT status, attempts FROM {s}.fix_cases WHERE id = %s", (case_id,))
        if not row or row["status"] not in ("investigating", "fixing"):
            return None
        fixing = row["status"] == "fixing"
        await self.event(case_id, "run_died", {"error": error[:500]})
        if row["attempts"] >= MAX_ATTEMPTS:
            if fixing:
                result = {"status": "failed", "failed_step": "the run itself",
                          "detail": f"It stopped {row['attempts']} times: {error[:160]}"}
                await self._move(case_id, ("fixing",), "failed", "result = %s", (json.dumps(result),))
                return "failed"
            await self._move(case_id, ("investigating",), "stuck")
            return "stuck"
        back = "approved" if fixing else "new"
        await self._move(case_id, (row["status"],), back)
        return back

    async def save_plan(self, case_id: int, plan: dict, session_id: Optional[str]) -> Optional[str]:
        """The investigator's answer. A plan he should see becomes `planned`; a repeat adds to
        the case it repeats and says nothing; the other closed kinds just close."""
        kind = plan.get("kind")
        if kind in OPEN_KINDS:
            to = "planned"
        elif kind in CLOSED_KINDS:
            to = kind
        else:
            return None
        dup = plan.get("duplicate_of") if kind == "duplicate" else None
        row = await self._move(
            case_id, ("investigating",), to,
            # The fix run gets its own count of attempts, apart from the investigation's.
            "plan = %s, session_id = %s, duplicate_of = %s, attempts = 0",
            (json.dumps(plan, default=str), session_id, dup),
        )
        if not row:
            return None
        if dup:
            await self._rows(
                f"UPDATE {self.schema}.fix_cases SET seen = seen + 1, updated_at = now() WHERE id = %s",
                (dup,))
        return to

    async def save_result(self, case_id: int, result: dict) -> Optional[str]:
        to = result.get("status")
        if to not in RESULT_STATES:
            return None
        row = await self._move(case_id, ("fixing",), to, "result = %s",
                               (json.dumps(result, default=str),))
        return to if row else None

    # -- schema ------------------------------------------------------------------
    def _ddl(self) -> str:
        s = self.schema
        return f"""
        CREATE TABLE IF NOT EXISTS {s}.fix_cases (
            id             bigserial PRIMARY KEY,
            loop_id        text NOT NULL UNIQUE,
            judge_version  text NOT NULL,
            chat_id        text,
            codes          text[] NOT NULL DEFAULT '{{}}',
            status         text NOT NULL DEFAULT 'new',
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            attempts       int NOT NULL DEFAULT 0,
            seen           int NOT NULL DEFAULT 1,      -- sessions with this same cause
            duplicate_of   bigint,
            plan           jsonb,                       -- the investigator's answer
            session_id     text,                        -- its Claude Code session, resumed to fix
            proposal_wa_id text,
            proposed_at    timestamptz,
            approved_at    timestamptz,
            approval_note  text,
            approval_wa_id text,
            result         jsonb,                       -- what the fix run reported
            result_wa_id   text,
            result_sent_at timestamptz
        );
        CREATE INDEX IF NOT EXISTS fix_cases_status ON {s}.fix_cases (status, id);

        CREATE TABLE IF NOT EXISTS {s}.fix_events (
            id       bigserial PRIMARY KEY,
            case_id  bigint,
            ts       timestamptz NOT NULL DEFAULT now(),
            kind     text NOT NULL,
            detail   jsonb
        );
        CREATE INDEX IF NOT EXISTS fix_events_case ON {s}.fix_events (case_id, id);

        CREATE TABLE IF NOT EXISTS {s}.fix_worker (
            host     text PRIMARY KEY,
            seen_at  timestamptz NOT NULL,
            info     jsonb
        );
        """
