"""Aggregation — turning stored verdicts into the ranked list that drives the next fix.

Ranking is by MAJOR-severity count first, then raw frequency. Without that, a swarm of minor
`too_verbose` notes buries a handful of `acted_without_confirm` incidents. Timing gaps rank in the
same table as quality gaps: from the fix side there is no difference, and a shared budget deserves
a shared leaderboard.

Every read is parameterised by judge_version, so two rubrics can sit side by side in one database
and be compared before the new one becomes the one the sweeper uses."""
from __future__ import annotations

from typing import Any, Optional

from .taxonomy import ALL_CODES, JUDGE_CODES, TIMING_CODES


async def build_report(
    store: Any, *, judge_version: str, scope_version: Optional[str] = None,
    examples_per_code: int = 3,
) -> dict:
    """Everything the page needs, in one pass over the review tables."""
    from psycopg.rows import dict_row

    s = store.schema
    where = "r.judge_version = %(jv)s"
    params: dict = {"jv": judge_version}
    if scope_version:
        where += " AND r.prompt_version = %(scope)s"
        params["scope"] = scope_version

    async with store._pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:

            async def q(sql: str, extra: dict | None = None) -> list[dict]:
                await cur.execute(sql, {**params, **(extra or {})})
                return list(await cur.fetchall())

            headline = (await q(f"""
                SELECT count(*) turns,
                       count(DISTINCT r.loop_id) sessions,
                       count(DISTINCT r.chat_id) chats,
                       count(*) FILTER (WHERE r.turn_kind = 'reply')   replies,
                       count(*) FILTER (WHERE r.turn_kind = 'silence') silences,
                       count(*) FILTER (WHERE r.verdict = 'good')       good,
                       count(*) FILTER (WHERE r.verdict = 'acceptable') acceptable,
                       count(*) FILTER (WHERE r.verdict = 'bad')        bad,
                       count(*) FILTER (WHERE r.verdict = 'error')      errored,
                       count(*) FILTER (WHERE r.overtaken)              overtaken,
                       min(r.ts) first_seen, max(r.ts) last_seen
                  FROM {s}.reviews r WHERE {where}"""))[0]

            by_kind = await q(f"""
                SELECT r.turn_kind,
                       count(*) turns,
                       count(*) FILTER (WHERE r.verdict = 'good') good,
                       count(*) FILTER (WHERE r.verdict = 'bad')  bad,
                       percentile_disc(0.5) WITHIN GROUP (ORDER BY r.wait_seconds) p50,
                       percentile_disc(0.9) WITHIN GROUP (ORDER BY r.wait_seconds) p90,
                       max(r.wait_seconds) worst
                  FROM {s}.reviews r WHERE {where} GROUP BY 1 ORDER BY 1""")

            gaps = await q(f"""
                SELECT f.code, f.source,
                       count(*) hits,
                       count(*) FILTER (WHERE f.severity = 'major') major,
                       count(DISTINCT f.loop_id) sessions,
                       max(f.ts) last_seen
                  FROM {s}.findings f JOIN {s}.reviews r ON r.id = f.review_id
                 WHERE {where}
                 GROUP BY 1, 2 ORDER BY major DESC, hits DESC""")

            examples = await q(f"""
                SELECT code, evidence, loop_id, reply_text, rationale, turn_kind FROM (
                  SELECT f.code, f.evidence, f.loop_id, r.reply_text, r.rationale, r.turn_kind,
                         row_number() OVER (PARTITION BY f.code
                                            ORDER BY (f.severity = 'major') DESC, f.ts DESC) rn
                    FROM {s}.findings f JOIN {s}.reviews r ON r.id = f.review_id
                   WHERE {where}) x
                 WHERE rn <= %(n)s ORDER BY code, rn""", {"n": examples_per_code})

            by_task = await q(f"""
                SELECT coalesce(r.task_class, 'unknown') task_class, count(*) turns,
                       percentile_disc(0.5) WITHIN GROUP (ORDER BY r.wait_seconds) p50,
                       percentile_disc(0.9) WITHIN GROUP (ORDER BY r.wait_seconds) p90,
                       max(r.wait_seconds) worst,
                       count(*) FILTER (WHERE r.timing_band IN ('slow','breach')) over_budget,
                       percentile_disc(0.5) WITHIN GROUP (ORDER BY r.model_ms) model_p50
                  FROM {s}.reviews r WHERE {where}
                 GROUP BY 1 ORDER BY turns DESC""")

            by_domain = await q(f"""
                SELECT coalesce(r.domain,'unknown') domain, count(DISTINCT r.id) turns,
                       count(f.id) hits,
                       count(f.id) FILTER (WHERE f.severity = 'major') major
                  FROM {s}.reviews r LEFT JOIN {s}.findings f ON f.review_id = r.id
                 WHERE {where} GROUP BY 1 ORDER BY major DESC, hits DESC""")

            # The cross-tab that matters most: waited, and got nothing.
            slow_silent = await q(f"""
                SELECT r.loop_id, r.chat_id, r.ts, r.wait_seconds, r.turn_kind,
                       r.delivery, r.turn_error, r.rationale
                  FROM {s}.reviews r
                 WHERE {where} AND r.wait_seconds > 30
                   AND (r.reply_text IS NULL OR r.delivery <> 'ok')
                 ORDER BY r.wait_seconds DESC LIMIT 20""")

            worst = await q(f"""
                SELECT r.loop_id, max(r.chat_id) chat_id, max(r.ts) ts,
                       count(DISTINCT r.id) turns,
                       count(f.id) FILTER (WHERE f.severity = 'major') major,
                       count(f.id) hits
                  FROM {s}.reviews r LEFT JOIN {s}.findings f ON f.review_id = r.id
                 WHERE {where} GROUP BY r.loop_id
                HAVING count(f.id) > 0
                 ORDER BY major DESC, hits DESC LIMIT 15""")

            proposals = await q(f"""
                SELECT r.proposed_gap, count(*) hits, max(r.loop_id) loop_id
                  FROM {s}.reviews r
                 WHERE {where} AND coalesce(r.proposed_gap,'') <> ''
                 GROUP BY 1 ORDER BY hits DESC LIMIT 25""")

            trend = await q(f"""
                SELECT date_trunc('week', r.ts)::date week, count(DISTINCT r.id) turns,
                       count(f.id) FILTER (WHERE f.severity = 'major') major,
                       count(*) FILTER (WHERE r.verdict = 'bad') bad
                  FROM {s}.reviews r LEFT JOIN {s}.findings f ON f.review_id = r.id
                 WHERE {where} GROUP BY 1 ORDER BY 1""")

    ex: dict[str, list] = {}
    for e in examples:
        ex.setdefault(e["code"], []).append(e)

    return {
        "judge_version": judge_version,
        "scope_version": scope_version,
        "headline": headline,
        "by_kind": by_kind,
        "gaps": [dict(g, description=ALL_CODES.get(g["code"], "")) for g in gaps],
        "examples": ex,
        "by_task": by_task,
        "by_domain": by_domain,
        "slow_silent": slow_silent,
        "worst": worst,
        "proposals": proposals,
        "trend": trend,
        "code_families": {"judge": list(JUDGE_CODES), "timing": list(TIMING_CODES)},
    }


def format_text(rep: dict) -> str:
    """The terminal view — the same ranking, for a quick look without opening a browser."""
    h = rep["headline"]
    out = [
        f"Lisa session review — judge {rep['judge_version']}"
        + (f", scope {rep['scope_version']}" if rep["scope_version"] else ""),
        f"  {h['sessions']} sessions · {h['chats']} chats · {h['turns']} turns "
        f"({h['replies']} replies, {h['silences']} silences)",
        f"  good {h['good']} · acceptable {h['acceptable']} · bad {h['bad']}"
        + (f" · unjudged {h['errored']}" if h["errored"] else ""),
        "",
        f"  {'CODE':<24} {'SRC':<7} {'MAJOR':>6} {'HITS':>6} {'SESSIONS':>9}",
        f"  {'-'*24} {'-'*7} {'-'*6} {'-'*6} {'-'*9}",
    ]
    for g in rep["gaps"]:
        out.append(f"  {g['code']:<24} {g['source']:<7} {g['major']:>6} {g['hits']:>6} {g['sessions']:>9}")
    if not rep["gaps"]:
        out.append("  (no findings)")
    out += ["", "  latency by task class (seconds)",
            f"  {'TASK':<18} {'TURNS':>6} {'p50':>7} {'p90':>7} {'WORST':>7} {'OVER':>6}"]
    for t in rep["by_task"]:
        out.append(
            f"  {t['task_class']:<18} {t['turns']:>6} {_s(t['p50']):>7} {_s(t['p90']):>7} "
            f"{_s(t['worst']):>7} {t['over_budget']:>6}"
        )
    if rep["slow_silent"]:
        out += ["", f"  waited and got nothing: {len(rep['slow_silent'])} turns over 30s"]
    return "\n".join(out)


def _s(v) -> str:
    return "-" if v is None else f"{float(v):.1f}"
