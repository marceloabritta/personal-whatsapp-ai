---
name: lisa-review
description: Grade Lisa's past sessions and build the review report page. Use when asked to review/evaluate Lisa's conversations, find what she gets wrong, rank her mistakes, check response times, or produce the session-review page. Triggers on "review lisa", "how is lisa doing", "lisa mistakes", "lisa report", "evaluate lisa".
---

# Lisa session review

Grades every turn Lisa took — each reply AND each time she chose to stay silent — then stacks the
faults into a ranked list and renders a page to read it from.

Two independent assessors: a model that reads **only the chat transcript** (never a clock), and a
pure function that scores **only the clock** (never the text). Code lives in
`Lisa flow/app/review/`.

## Running it

Postgres is not published to the host, so this runs on the droplet. Use a throwaway container with
the staged source mounted — never rebuild `lisa_brain` just to run a report.

```bash
# 1. stage the current source
ssh secretaria-droplet 'mkdir -p /opt/review-staging'
cd "Lisa flow" && rsync -az --delete --exclude '__pycache__' app scripts tests \
    requirements.txt secretaria-droplet:/opt/review-staging/

# 2. env, lifted from the running container (stays on the droplet)
ssh secretaria-droplet 'docker exec lisa_brain env | \
    grep -E "^(DATABASE_URL|ANTHROPIC_API_KEY|LOG_SCHEMA|OWNER_NAME|MARY_TRIGGER_TAG)=" \
    > /opt/review-staging/.env.run && chmod 600 /opt/review-staging/.env.run'

# 3. grade anything not yet graded at this judge version
ssh secretaria-droplet 'docker run --rm --network evolution_evolution-net \
  --env-file /opt/review-staging/.env.run \
  -e REVIEW_JUDGE_VERSION=v1 -e REVIEW_MODEL=claude-opus-4-8 \
  -v /opt/review-staging:/app -w /app evolution-lisa \
  python -m scripts.review_report backfill --scope-version <PROMPT_VERSION>'

# 4. the report — terminal summary, plus an artifact-ready page
ssh secretaria-droplet 'docker run --rm --network evolution_evolution-net \
  --env-file /opt/review-staging/.env.run -e REVIEW_JUDGE_VERSION=v1 \
  -v /opt/review-staging:/app -w /app evolution-lisa \
  python -m scripts.review_report report --scope-version <PROMPT_VERSION> \
  --html /app/out.html --fragment'
scp secretaria-droplet:/opt/review-staging/out.html /tmp/lisa-review.html
```

Then publish `/tmp/lisa-review.html` with the Artifact tool. `--fragment` emits artifact-ready
content (no document wrapper); drop the flag for a file to open from disk.

The network is `evolution_evolution-net`. The image `evolution-lisa` already has every dependency.

## Scope

Default to the **currently deployed** `prompt_version` — grading turns from code that no longer
exists tells you nothing about what to fix. Find it with:

```bash
ssh secretaria-droplet "docker exec evolution_postgres psql -U evolution -d evolution -c \
  \"select prompt_version, count(*), min(started_at), max(started_at) \
    from lisa_log.loops group by 1 order by 3;\""
```

## Reading the result

Ranked by major-severity count first, then frequency. Quality and timing findings share one table.
Latency is measured **last human message → reply**, and replies and silences are banded separately:
a slow silence is not a user wait, it is lock time charged to whoever speaks next in that chat.

## Re-scoring after a rubric change

Edit `app/review/taxonomy.py` (codes) or `app/review/prompt.py` (how the judge reads), or
`app/review/timing.py` (budgets), then **bump `REVIEW_JUDGE_VERSION`** and re-run backfill. Old
verdicts stay; the two versions sit side by side and can be compared before the new one becomes
the one the live sweeper uses. Timing-only changes are free to re-score — the scorer is pure.

## Before trusting a run

Spot-check a handful of verdicts against the raw transcript. A judge nobody has checked is not
evidence. Pull a session with:

```bash
ssh secretaria-droplet "docker exec evolution_postgres psql -U evolution -d evolution -At -c \
  \"select seq||' ['||coalesce(who,label)||'] '||left(coalesce(text,payload->>'response',''),110) \
    from lisa_log.events where loop_id='<LOOP_ID>' and (stream='transcript' or label='record') \
    order by seq;\""
```

The known trap: `act_node` writes a reply's transcript line *before* its own record, so a naive
`seq < N` slice would show the judge the message it is judging. `store._context_for` drops the
assistant line sharing the turn's `trace_id`. If verdicts suddenly read "duplicate of the message
she just sent", that guard has regressed — `tests/run_review.py` pins it.

## Selftest

```bash
cd "Lisa flow" && .venv/bin/python tests/run_review.py
```

Layer B replays 120 recorded turns and asserts the scorer still reproduces the measured
distribution (replies p50 7.3s / p90 25.4s, silences p50 12.7s / p90 39.5s, 2 past the 60s window).
Any budget edit that moves those numbers is meant to fail this until the fixture expectations are
updated deliberately.
