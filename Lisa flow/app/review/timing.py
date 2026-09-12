"""The timing half of the review — pure arithmetic, no model call, no I/O.

The question is about the machine, not the answer: given the conversation as it stood, how long
did the system take to say something, and was that adequate for the work?

WHAT IS MEASURED: from the LAST HUMAN MESSAGE before the reply, to the reply going out. Not
end-to-end from the start of a task (that blurs the moment a task spans several loops), and not
from the message that happened to trigger the activation — those differ on ~16% of turns, almost
all of them silent turns in busy group chats where the activation-based number is just counting
other people's chatter.

Replies and silences are scored against DIFFERENT budgets. Nobody waits on a non-answer, so a
slow silence looks free — and is not: main.py holds a per-thread lock for the whole turn, so those
seconds are charged to whoever speaks next in that chat. Producing nothing should never be slow,
whatever the topic, so silence gets one flat budget instead of the task's.

Everything here is a pure function of already-recorded numbers, so the budgets can be re-tuned and
the whole history re-scored in milliseconds — no judge re-run, no token spend. Quality verdicts are
the expensive half; timing verdicts are free."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .taxonomy import DEFAULT_TASK_CLASS

# The listening window (config.loop_ttl_seconds). act_node only refreshes it AFTER the work
# finishes, so a turn that runs longer leaves the session expired at the moment it answers: the
# reply lands, but the follow-up it invites falls outside the window and is dropped. Past this
# it is `window_expired` at major, whatever the task class.
WINDOW_SECONDS = 60.0

# Past this, a turn that delivered nothing is major rather than minor — someone waited, and got
# no answer at all.
SLOW_SILENT_SECONDS = 30.0


@dataclass(frozen=True)
class Budget:
    """`good` up to `good_s`; `slow` up to `slow_s`; `breach` beyond.

    `per_audio_second` scales the budget with the length of a voice note — the only task whose
    honest cost depends on its input size."""
    good_s: float
    slow_s: float
    per_audio_second: float = 0.0

    def band(self, secs: float, audio_sec: Optional[float] = None) -> str:
        extra = (audio_sec or 0.0) * self.per_audio_second
        if secs <= self.good_s + extra:
            return "good"
        if secs <= self.slow_s + extra * 2:
            return "slow"
        return "breach"


# Deliberately set NEAR today's medians rather than at an aspiration: a budget half the traffic
# breaches ranks nothing. Tighten once the first report shows where the real tail is.
# Baselines when these were chosen (120 turns, prompt_version 2026-08-05-programmatic-calendar):
# replies p50 7.3s / p90 25.4s; silences p50 12.7s / p90 39.5s.
BUDGETS: dict[str, Budget] = {
    "ack":             Budget(good_s=8,  slow_s=20),
    "calendar_read":   Budget(good_s=12, slow_s=25),
    "calendar_write":  Budget(good_s=20, slow_s=40),
    "transcription":   Budget(good_s=15, slow_s=30, per_audio_second=0.3),
    "web_lookup":      Budget(good_s=20, slow_s=40),
    "web_research":    Budget(good_s=35, slow_s=60),
}

# Silence is scored against this regardless of what the conversation was about.
SILENCE_BUDGET = Budget(good_s=8, slow_s=20)


@dataclass(frozen=True)
class Turn:
    """The measured facts about one activation. Everything here already exists in lisa_log."""
    silent: bool
    reply_ts: Optional[float] = None        # when the record was written (reply on the wire)
    last_human_ts: Optional[float] = None   # WhatsApp messageTimestamp of the newest human line
    error: bool = False                     # record.error_category != "none"
    delivery: Optional[str] = None          # "ok" | "failed" | "silent"
    audio_sec: Optional[float] = None       # voice-note length, when this turn transcribed one


@dataclass(frozen=True)
class TimingScore:
    band: str                       # good | slow | breach | unknown
    wait_seconds: Optional[float]
    gaps: list                      # [(code, severity), ...]


def score_timing(turn: Turn, task_class: str = DEFAULT_TASK_CLASS) -> TimingScore:
    """Band one turn's wait and file whatever timing gaps it earns.

    Returns band "unknown" with no gaps when the wait cannot be established — the activation
    message carries no WhatsApp timestamp. Filing nothing beats guessing: an unmeasured turn must
    not be able to inflate a gap count."""
    secs = _wait(turn)

    gaps: list = []
    # A turn that errored or failed to deliver is scored even when the clock is unknown — the
    # no-answer facts stand on their own.
    sev = "major" if (secs is not None and secs > SLOW_SILENT_SECONDS) else "minor"
    if turn.error:
        gaps.append(("no_answer_error", sev))
    if turn.delivery == "failed":
        gaps.append(("delivery_failed", sev))

    if secs is None:
        return TimingScore("unknown", None, gaps)

    budget = SILENCE_BUDGET if turn.silent else BUDGETS.get(task_class, BUDGETS[DEFAULT_TASK_CLASS])
    band = budget.band(secs, turn.audio_sec)
    code = "slow_silence" if turn.silent else "slow_reply"

    if secs > WINDOW_SECONDS:
        # Outranning the window is its own failure and supersedes the budget band — the session
        # was already expired by the time this turn spoke.
        gaps.append(("window_expired", "major"))
    elif band == "breach":
        gaps.append((code, "major"))
    elif band == "slow":
        gaps.append((code, "minor"))

    return TimingScore(band, round(secs, 3), gaps)


def _wait(turn: Turn) -> Optional[float]:
    if turn.reply_ts is None or turn.last_human_ts is None:
        return None
    secs = float(turn.reply_ts) - float(turn.last_human_ts)
    # WhatsApp timestamps are whole seconds from the SENDER's clock, so a small negative skew is
    # normal and means "immediate", not "before it was asked". A large negative is a broken
    # pairing we refuse to score.
    if secs < -5:
        return None
    return max(secs, 0.0)
