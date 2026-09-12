"""The gap vocabulary — the thing that makes findings STACK.

Free-text criticism never aggregates: a hundred turns produce a hundred unique complaints.
A closed vocabulary, pinned into the judge's enforced-JSON enum, makes counting a GROUP BY.

Two families, one table:
  JUDGE_CODES  — assigned by reading the transcript (the model's call);
  TIMING_CODES — assigned by arithmetic over measured seconds (code's call, see timing.py).

From the fix side there is no difference — both are things Lisa got wrong — so they share one
ranking. `proposed_gap` is the escape hatch for a real failure no code covers; promoting one to
a real code is a deliberate edit HERE plus a judge_version bump, never automatic. A vocabulary
that drifts on its own cannot be counted over time."""
from __future__ import annotations

# --- what the judge may assign, by reading ---------------------------------
# Descriptions are shown to the judge verbatim, so they are written AT it: short, concrete,
# and phrased as the condition that must hold for the code to apply.
JUDGE_CODES: dict[str, str] = {
    # whether to speak at all
    "spoke_unprompted": "Replied to a message that was between other people and not for her.",
    "missed_turn": "Stayed silent when she was directly addressed and an answer was expected.",
    "closed_too_early": "Stepped out of the conversation while a task was still unfinished.",
    # doing the thing
    "repeat_confirmation": "Asked for a confirmation that had already been given earlier in the chat.",
    "ignored_approval": "The go-ahead was given and she asked again or stalled instead of acting. "
                        "Use this for a failure to ACT on a yes; use claimed_undone only when she "
                        "said the thing WAS done.",
    "acted_without_confirm": "Created, changed or deleted something without being given approval.",
    "wrong_action": "Right intent, but the wrong operation or the wrong item.",
    "wrong_details": "A value the owner GAVE was changed, dropped or invented — a wrong date, a "
                     "missing end time, a made-up hour, the wrong weekday. Not for a reply that "
                     "merely leaves information out; that is incomplete_message.",
    "claimed_undone": "Stated that something WAS done when the chat gives no sign it was. Only "
                      "when a completion was actually claimed.",
    # understanding
    "false_refusal": "Said she cannot do something she can in fact do — read an image or a PDF, "
                     "search the web, transcribe a voice note, work the calendar. Check the "
                     "capability list before using this, and use it instead of wrong_domain or "
                     "ignored_context when the fault is a denied capability.",
    "ignored_context": "Missed or contradicted something plainly stated earlier in the chat.",
    "misread_request": "Answered a different question from the one that was asked.",
    "redid_handled": "Re-did a request that an earlier reply had already closed out.",
    "wrong_domain": "Treated the ask as the wrong kind of task entirely — not merely refusing it.",
    # how it reads
    "wrong_language": "Wrote in a different language from the one the conversation is in.",
    "too_verbose": "Over-explained, or volunteered extras nobody asked for.",
    "incomplete_message": "Left out something the reader needed: a detail that was asked for, WHAT "
                          "a confirmation is actually changing, a link that was the point of the "
                          "request. The message is clear but missing a piece.",
    "bad_format": "Formatting or structure that reads badly as a WhatsApp message.",
    "unclear_reply": "Genuinely ambiguous or confusing to read. Not for a message that is clear "
                     "but incomplete; that is incomplete_message.",
}

# --- what the timing scorer assigns, by arithmetic --------------------------
TIMING_CODES: dict[str, str] = {
    "slow_reply": "The reply took longer than its task class allows.",
    "slow_silence": "Took too long to decide to say nothing, holding the chat lock.",
    "window_expired": "The turn outran the 60s listening window.",
    "no_answer_error": "A model or tool error ended the turn with nothing sent.",
    "delivery_failed": "A reply was composed but the send never landed.",
    "dropped_after_timeout": "A follow-up arrived after the window lapsed and was never handled.",
}

OTHER = "other"  # carries proposed_gap text; never assigned directly by the scorer

ALL_CODES: dict[str, str] = {**JUDGE_CODES, **TIMING_CODES, OTHER: "Nothing in the vocabulary fits."}

VERDICTS = ("good", "acceptable", "bad")
SEVERITIES = ("minor", "major")

# --- task classes ----------------------------------------------------------
# The ONE field that crosses between the two assessors. Deliberately scoped: the judge is asked
# what kind of work the turn required, never how long that should take. Classifying an ask is a
# reading task; calibrating seconds is not, which is why the budget lives in timing.py.
TASK_CLASSES: dict[str, str] = {
    "ack": "A short reply, a greeting, a one-line answer from what is already in the chat, "
           "or a decision that nothing needed saying.",
    "calendar_read": "Looking something up in the calendar — agenda, availability, finding an event.",
    "calendar_write": "Creating, changing or deleting a calendar event, including confirming it first.",
    "transcription": "Turning a voice note into text.",
    "web_lookup": "One fact that needs a web search to answer.",
    "web_research": "A question needing several sources pulled together.",
}
DEFAULT_TASK_CLASS = "ack"


def judge_schema() -> dict:
    """The judge's enforced-JSON contract.

    Kept small on purpose: the Anthropic output_config rejects schemas with more than 16
    anyOf/array parameters, and optionals must be real unions rather than the OpenAI-style
    required-and-nullable. One array (`gaps`), everything else scalar."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rationale": {
                "type": "string",
                "description": "One or two sentences, in English, on why this turn was or was "
                               "not the right thing to say. Write it FIRST, then decide.",
            },
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "task_class": {
                "type": "string",
                "enum": list(TASK_CLASSES),
                "description": "What kind of work this turn required — NOT how long it should take.",
            },
            "gaps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "code": {"type": "string", "enum": list(JUDGE_CODES)},
                        "severity": {"type": "string", "enum": list(SEVERITIES)},
                        "evidence": {
                            "type": "string",
                            "description": "The quote from the transcript that proves it.",
                        },
                    },
                    "required": ["code", "severity", "evidence"],
                },
            },
            "proposed_gap": {
                "type": "string",
                "description": "Empty string unless there is a real failure no code above covers; "
                               "then a short phrase naming it.",
            },
        },
        "required": ["rationale", "verdict", "confidence", "task_class", "gaps", "proposed_gap"],
    }


def normalise(raw: dict) -> dict:
    """Coerce a judge payload into something safe to store.

    The enum does most of the work, but a model can still hand back `verdict:"good"` alongside a
    list of gaps. Rather than drop either half — the gaps are the valuable part — we keep the gaps
    and downgrade the verdict. A judge that hedges is more useful than one silently inconsistent."""
    verdict = raw.get("verdict") if raw.get("verdict") in VERDICTS else "acceptable"
    gaps = []
    for g in raw.get("gaps") or []:
        code = (g or {}).get("code")
        if code not in JUDGE_CODES:
            continue
        sev = g.get("severity") if g.get("severity") in SEVERITIES else "minor"
        gaps.append({"code": code, "severity": sev, "evidence": (g.get("evidence") or "")[:500]})

    proposed = (raw.get("proposed_gap") or "").strip()
    if proposed and not gaps:  # a real miss the vocabulary could not absorb
        gaps.append({"code": OTHER, "severity": "minor", "evidence": proposed[:500]})

    if verdict == "good" and gaps:
        verdict = "acceptable"

    task_class = raw.get("task_class")
    if task_class not in TASK_CLASSES:
        task_class = DEFAULT_TASK_CLASS

    return {
        "verdict": verdict,
        "confidence": raw.get("confidence") if raw.get("confidence") in ("low", "medium", "high") else "low",
        "rationale": (raw.get("rationale") or "")[:2000],
        "task_class": task_class,
        "gaps": gaps,
        "proposed_gap": proposed[:500],
    }
