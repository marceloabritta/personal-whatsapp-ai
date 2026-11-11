"""context — assemble the turn (§01/§02).

A fresh @mary tag starts a NEW loop: it wipes the checkpointed conversation memory
and re-seeds the last CONTEXT_WINDOW_MESSAGES from Evolution, so one loop never
leaks into the next on the same chat. A window continuation (untagged follow-up
while the loop is open) fetches only messages after the cursor and keeps the loop's
memory. Assistant-origin messages are filtered (they're already AIMessages in the
checkpoint). The new messages become one labeled user turn; the cursor advances.

A loop reads each message once (`read_ids`). An activation whose message an earlier one has
already read finds nothing new, and ends without a model call (`route_after_context`)."""
from __future__ import annotations

import asyncio

from langchain_core.messages import RemoveMessage

from ..carry import _find_card, carried_yes, kept_on_carry
from ..identity import is_own_message
from ..skills.setup import setup_matcher
from ..state import MessageState
from ..trace import Trace
from ..whatsapp import build_labeled_transcript, chat_key, label_for, when_label


# How many message ids a loop remembers having read. A loop seeds 30 and then reads what arrives,
# so this is far more than one holds; it only bounds the checkpoint.
READ_IDS_KEPT = 500
CALLED_FOR_MAX = 400


def called_for_text(text: str | None) -> str | None:
    """The tagged message as one line, short enough to quote in the prompt on every turn."""
    line = " ".join((text or "").split())
    if len(line) > CALLED_FOR_MAX:
        line = line[:CALLED_FOR_MAX].rstrip() + "…"
    return line or None


def _quoted_line(state: dict, records: list[dict], owner: str,
                 tz_name: str) -> tuple[str, str, str] | None:
    """The text message this one replies to → (transcript line, log speaker, log text), or None.

    A WhatsApp reply is how he points at the message he means: "@lisa marca" under a lunch
    proposed two days ago. Nothing else tells her which message that is, and it may be outside
    the window altogether. The quote payload carries neither the sender nor the send time, and the
    time is what its "amanhã" is read from, so the message is looked up by id in the fetched
    history. When the history no longer has it, the quote's own text is used, marked undated.
    Her own messages are skipped: she has them already, and a quoted card is many lines long."""
    qid = state.get("quoted_text_id")
    if not qid:
        return None
    rec = next((r for r in records if r.get("id") == qid), None)
    text = ((rec or {}).get("text") or state.get("quoted_text") or "").strip()
    if not text or is_own_message(text, owner):
        return None
    text = " / ".join(part.strip() for part in text.splitlines() if part.strip())
    replier = owner if state.get("from_me") else (state.get("push_name") or "Contact")
    if rec is None:
        source = f'an earlier message, sent at a time not known: "{text}"'
    else:
        when = when_label(rec.get("ts"), tz_name)
        sent = f", sent {when}" if when else ""
        source = f'this message from {label_for(rec, owner)}{sent}: "{text}"'
    return f"[{replier} is replying to {source}]", f"{replier} (replying to a message)", source


def _participant_phones(records: list[dict]) -> list[str]:
    """Every phone number that has spoken in this chat — the room's membership, as the history
    shows it. It is what lets "add Zen" resolve in a group: `state["phone"]` is None there
    (`remoteJid` is the group), so without this the address book has no identity to work from
    and a name that fits several contacts can never be narrowed to the one actually present.

    Only a real `@s.whatsapp.net` JID is a phone. An `@lid` is opaque and its tail looks enough
    like a local number to bind a stranger, which is the same rail `parse` holds.
    """
    out: list[str] = []
    seen: set[str] = set()
    for r in records:
        for jid in (r.get("participant"), r.get("participant_alt")):
            if not jid or not str(jid).endswith("@s.whatsapp.net"):
                continue
            k = chat_key(jid)
            if k and k not in seen:
                seen.add(k)
                out.append(k)
    return out


def _partner_name(records: list[dict]) -> str | None:
    """The WhatsApp name of the other person in a one-to-one chat, from the newest message they
    sent, or None while they have not written.

    It goes into the system prompt (the calendar's "This chat is one-to-one" line), and a push
    name is whatever its owner typed. So it is kept to one short line with no quotes: enough to
    name a person, not enough to pass for an instruction."""
    theirs = [r for r in records if not r.get("from_me") and (r.get("push_name") or "").strip()]
    if not theirs:
        return None
    newest = max(theirs, key=lambda r: r.get("ts") or 0)
    name = " ".join(str(newest["push_name"]).replace('"', "").split())[:40].strip()
    return name or None


def _log_transcript(trace: Trace, tid: str, loop_id: str | None, records: list[dict],
                    owner: str) -> None:
    """Record each chat message into the loop's transcript stream (deduped by id in the
    store). Both sides — owner, contact, and Mary — so the log is the real conversation.
    Provenance: an audio record is logged with source="audio" so the durable stream records
    which turns were spoken, not just what was said."""
    if not loop_id:
        return
    for r in records:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        trace.user(tid, label_for(r, owner), text, loop_id=loop_id,
                   wa_id=r.get("id"), ts=r.get("ts"), from_me=bool(r.get("from_me")),
                   source="audio" if r.get("is_audio") else "text")


async def _transcribe_audio(records: list[dict], transcription, settings, trace: Trace,
                            tid: str, loop_id: str | None) -> None:
    """Transcribe the voice notes in `records` IN PLACE — an is_audio record with no text
    gets its transcript (or an honest bracketed placeholder). Cached by wa id; capped at
    settings.max_context_transcriptions with the overflow logged (no silent truncation);
    concurrent under a bounded semaphore so a multi-clip window doesn't serialize."""
    audio = [r for r in records if r.get("is_audio") and not (r.get("text") or "").strip()]
    if not audio:
        return
    if not (transcription and settings.transcription_enabled):
        for r in audio:  # mark presence — better than a silently dropped, empty voice note
            r["text"] = "[voice message]"
        return

    todo, overflow = _newest(audio, settings.max_context_transcriptions)
    for r in overflow:
        r["text"] = "[voice message — not transcribed]"
    if overflow and loop_id:
        trace.code(tid, node="context", loop_id=loop_id, transcription_dropped=len(overflow))

    sem = asyncio.Semaphore(settings.transcription_concurrency)

    async def fill(r: dict) -> None:
        async with sem:
            res = await transcription.get(r["id"])
            txt = (res.get("text") or "").strip()
            if txt:
                r["text"] = txt
            elif res.get("error") == "empty":
                r["text"] = "[voice message — no speech]"
            else:
                r["text"] = "[voice message — could not transcribe]"

    await asyncio.gather(*(fill(r) for r in todo))


def _card_line(card: dict) -> str:
    """How a forwarded contact card appears in the transcript the model reads.

    An unambiguous card shows the number that will be used. An ambiguous one — two WhatsApp
    numbers, or none and several printed ones — shows them NUMBERED so the model can ask "1 or
    2?" and the owner can answer with a digit. Numbers that are not WhatsApp accounts are
    marked, because one of those cannot receive a voice note at all."""
    name = (card.get("name") or "").strip()
    head = f"contact card: {name}" if name else "contact card"
    if not card.get("ambiguous"):
        return f"[{head} · {card['number']}]"
    opts = "  ".join(
        f"{i}) {c['number']}" + (f" ({c['label']})" if c.get("label") else "")
        + ("" if c.get("wa") else " — not on WhatsApp")
        for i, c in enumerate(card.get("candidates") or [], 1)
    )
    return f"[{head} · several numbers, ask which: {opts}]"


def _collect_cards(records: list[dict], current: list[dict]) -> tuple[list[str], dict]:
    """Every contact card in this turn — from the fetched history and from the message being
    handled (the two overlap; the record wins). Returns (keys, views).

    Marks each card record IN PLACE so `build_labeled_transcript` emits a line for it: a card
    carries no text, so without this it would vanish from the transcript entirely."""
    keys: list[str] = []
    views: dict = {}
    seen: set[str] = set()

    def take(card: dict) -> None:
        # EVERY number on the card is a resolved id, not just the chosen one — otherwise
        # picking the other one would be refused by the write gate as unresolved.
        name = (card.get("name") or "").strip() or None
        for c in card.get("candidates") or [{"number": card.get("number")}]:
            num = c.get("number")
            if not num or num in seen:
                continue
            seen.add(num)
            keys.append(num)
            views[num] = {"chat_key": num, "chat_jid": f"{num}@s.whatsapp.net",
                          "label": name, "kind": "contact"}

    for r in records:
        cards = r.get("contact_cards") or []
        if cards and not (r.get("text") or "").strip():
            r["text"] = " ".join(_card_line(c) for c in cards)
        for c in cards:
            take(c)
    for c in current or []:
        take(c)
    return keys, views


def _newest(rows: list[dict], cap: int) -> tuple[list[dict], list[dict]]:
    """Split a chronological list into (the newest `cap`, the older rest), both in order.

    The cap used to keep the OLDEST rows. On 2026-11-10 he sent three screenshots and asked
    "@lisa what are the times on the image above?": six older PDFs and two stale images filled
    the 8 slots, the three screenshots were cut as "— omitted", and she told him she can't
    read images at all. What he is pointing at is almost always the latest thing he sent."""
    if cap <= 0:
        return [], list(rows)
    return rows[-cap:], rows[:-cap]


def _media_marker(r: dict, status: str = "ok") -> str:
    """Transcript marker for an image/PDF row: "[image]" / "[PDF: report.pdf]", or a status
    variant ("— unavailable" / "— too large" / "— omitted") when no block was attached."""
    kind = "PDF" if r.get("media_type") == "document" else "image"
    name = r.get("media_filename") if kind == "PDF" else None
    label = f"{kind}: {name}" if name else kind
    return f"[{label}]" if status == "ok" else f"[{label} — {status}]"


def _mark(r: dict, status: str) -> None:
    """Write the marker into the row's text so the labeled transcript emits a line — but never
    clobber a real caption (an image/PDF sent with text keeps its words)."""
    if not (r.get("text") or "").strip():
        r["text"] = _media_marker(r, status)


def _in_media_scope(r: dict) -> bool:
    """Images always; documents only when the declared mimetype is application/pdf."""
    mt = r.get("media_type")
    if mt == "image":
        return True
    if mt == "document":
        return (r.get("media_mimetype") or "").lower() == "application/pdf"
    return False


def _media_block(r: dict, res: dict, b64: str) -> dict:
    if r.get("media_type") == "document":
        blk = {"type": "document",
               "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
        if r.get("media_filename"):
            blk["title"] = r["media_filename"]
        return blk
    mimetype = res.get("mimetype") or r.get("media_mimetype") or "image/jpeg"
    return {"type": "image",
            "source": {"type": "base64", "media_type": mimetype, "data": b64}}


async def _attach_media(records: list[dict], evolution, settings, trace: Trace,
                        tid: str, loop_id: str | None) -> list[dict]:
    """Download the window's images/PDFs and return Anthropic content blocks — the twin of
    `_transcribe_audio`, but media can't fold into text so it travels as blocks. Each media row
    is marked IN PLACE so `build_labeled_transcript` emits a line even for a caption-less file.
    Bounded-concurrency gather; capped at settings.max_context_media; a failed/oversized download
    degrades to a marker with no block, and a per-turn byte budget stays under Anthropic's 32MB
    request ceiling — so a pathological file never breaks the turn."""
    if not settings.media_enabled:
        return []
    media = [r for r in records if _in_media_scope(r)]
    if not media:
        return []

    todo, overflow = _newest(media, settings.max_context_media)
    for r in overflow:
        _mark(r, "omitted")
    if overflow and loop_id:
        trace.code(tid, node="context", loop_id=loop_id, media_dropped=len(overflow))

    sem = asyncio.Semaphore(settings.transcription_concurrency)

    async def download(r: dict) -> tuple[dict, dict | None]:
        async with sem:
            return r, await evolution.get_media_base64(r["id"])

    fetched = await asyncio.gather(*(download(r) for r in todo))

    # The byte budget is spent newest first, for the same reason as the cap: the file he is
    # asking about is the one he just sent. Blocks still go out in chronological order.
    kept: dict[int, dict] = {}
    used = 0
    budget = settings.media_request_budget_bytes
    for i in range(len(fetched) - 1, -1, -1):
        r, res = fetched[i]
        b64 = (res or {}).get("base64")
        if not b64:
            _mark(r, "unavailable")
            continue
        approx = (len(b64) * 3) // 4  # decoded bytes ≈ 3/4 of the base64 length
        if approx > settings.media_max_item_bytes or used + approx > budget:
            _mark(r, "too large")
            continue
        used += approx
        kept[i] = _media_block(r, res, b64)
        _mark(r, "ok")
    return [kept[i] for i in sorted(kept)]


def _after_cursor(records: list[dict], cursor: str | None, window: int) -> list[dict]:
    if cursor:
        for i, r in enumerate(records):
            if r["id"] == cursor:
                return records[i + 1 :]
    # cursor unknown (fell out of the fetched range) — fall back to the window
    return records[-window:]


def route_after_context(state: MessageState) -> str:
    """A turn with nothing new to read ends in `act`, silent, with the window kept. Everything
    else goes on to the pending-write gate."""
    return "hold" if state.get("nothing_new") else "resolve"


async def context_node(
    state: MessageState, *, evolution, echoes, settings, trace: Trace, transcription=None,
    confirm_policies: dict | None = None,
) -> dict:
    tid = state["trace_id"]
    jid = state["remote_jid"]
    owner = settings.owner_name
    window = settings.context_window_messages

    # A fresh @mary tag opens a new loop → start from a clean context window.
    reset = state.get("trigger") == "tag"

    # ...except for SETUP, which starts from nothing at all. Configuring the assistant is not a
    # conversation with history: it is a form. Seeding the usual 30 messages replayed the
    # PREVIOUS setup attempt, and the model picked it up mid-flow — a fresh "@lisa setup" came
    # back already asking which direction for a group named in an earlier, abandoned session,
    # then proposed a write with an empty chat_key. So a setup tag seeds only the message that
    # opened it; everything the flow needs (a card, a group, the list) is surfaced within the
    # loop itself. Recognised programmatically, by the same matcher the router uses, because
    # `route` has not run yet at this point.
    setup_open = bool(
        reset and state.get("tag") and state.get("is_self_chat")
        and setup_matcher(state.get("text") or "") == "yes"
    )

    # A loop reads each message ONCE. An activation reads everything since the cursor, so while a
    # slow turn holds the chat's lock, the messages that pile up behind it are all read by the
    # first activation in the queue; the rest then wake with nothing left to read. The race guard
    # below used to take such a message for one Evolution had not stored yet, and put it back as
    # the newest line of the turn, which also moved the cursor back onto it. On 2026-11-10, called
    # in to work out a return, she read "Ta usando o que de LLM? / Modelo? / Opus 5.5?" (his
    # friend asking HIM about his product) in order and rightly let it pass. A turn later "Modelo?"
    # came round again, alone, right under her own answer, and she told the chat which model she
    # runs on. Six more turns then re-read the same lines.
    already = set() if reset else set(state.get("read_ids") or [])

    records = await evolution.fetch_history(jid)  # oldest → newest
    if setup_open:
        new = []  # the race guard below supplies the triggering message itself
    elif reset or not state.get("initialized"):
        new = records[-window:]
    else:
        new = [r for r in _after_cursor(records, state.get("last_whatsapp_message_id"), window)
               if r.get("id") not in already]

    # Race guard: the triggering message may not be in Evolution's DB yet. Not for one this loop
    # has read already, with one exception: a WhatsApp reply. What it points at is only known on
    # its own activation, so it is shown again, this time with the line that says so.
    cur_id = state.get("msg_id")
    points_back = bool(state.get("quoted_text_id") or state.get("quoted_audio_id"))
    out_of_order = None  # a message put back that the history holds at or before the cursor
    if cur_id and not any(r.get("id") == cur_id for r in new) \
            and (cur_id not in already or points_back):
        stored = next((r for r in records if r.get("id") == cur_id), None)
        if stored is not None:
            out_of_order = cur_id
        new = new + [stored or {
            "id": cur_id, "from_me": state.get("from_me"),
            "text": state.get("text") or "", "push_name": state.get("push_name"),
            "ts": state.get("ts", 0),
        }]

    raw = list(new)  # both sides, before the assistant-origin filter — for the log

    # Filter assistant-origin messages. On a window CONTINUATION her past replies already live
    # in the checkpoint as AIMessages, so re-ingesting them from history would duplicate them —
    # drop anything recognised by recorded id OR header stamp. On a RESET the checkpoint was just
    # wiped, so there is nothing to duplicate: KEEP her own replies as context, so she can see
    # what she already did and not redo an already-answered order. But keep only the ones we can
    # attribute — a header-stamped reply is labeled "AI Assistant" by build_labeled_transcript;
    # a reply recognised ONLY by recorded id (header missing/mangled) has no header for label_for
    # to key on and would be misattributed to the owner, so drop that one rather than mislabel it.
    if reset:
        new = [
            r for r in new
            if not (echoes.is_ours(jid, r.get("id"))
                    and not is_own_message(r.get("text") or "", owner))
        ]
    else:
        new = [
            r for r in new
            if not echoes.is_ours(jid, r.get("id"))
            and not is_own_message(r.get("text") or "", owner)
        ]

    loop_id = state.get("loop_id")

    # Hear voice notes: transcribe the window's audio IN PLACE before building the transcript,
    # so label_for annotates them "(voice message — transcribed)" and _log_transcript marks
    # the source. Mutates the shared record dicts, so both `raw` (reset log) and `new` (model)
    # see the transcripts. Language follows the locked session language when set, else auto.
    lang = state.get("session_lang")
    await _transcribe_audio(new, transcription, settings, trace, tid, loop_id)
    # See images & PDFs: download the window's media and build inline base64 blocks. Runs after
    # transcription so both passes have marked their rows before the transcript string is built.
    media_blocks = await _attach_media(new, evolution, settings, trace, tid, loop_id)

    tz_name = settings.calendar_timezone
    transcript = build_labeled_transcript(new, owner, tz_name)
    ids = [r["id"] for r in new if r.get("id")]
    # The cursor only moves forward. A message the race guard put back from further up the
    # history is read this turn, but the cursor stays where the history's own order has it.
    ahead = [i for i in ids if i != out_of_order]
    newest = ahead[-1] if ahead else state.get("last_whatsapp_message_id")
    if setup_open and records:
        # Seeding nothing must not leave the cursor behind the backlog: without this the next
        # turn would read everything after the old cursor and hand back the very history this
        # branch exists to skip.
        newest = records[-1]["id"]

    # Lines that sit after the transcript: what this message replies to. Collected rather than
    # appended on the spot, because a forwarded card re-renders the transcript below and used to
    # drop the voice-note line with it.
    reply_lines: list[str] = []

    # Compositional reply-to-audio: the owner referenced a voice note that isn't in the window.
    # Transcribe it and inject one explicit line — no speaker-label guessing about who spoke it.
    qid = state.get("quoted_audio_id")
    if qid and qid not in ids and transcription and settings.transcription_enabled:
        qres = await transcription.get(qid, language=lang)
        qtext = (qres.get("text") or "").strip()
        if qtext:
            reply_lines.append(f'[{owner} replied to a voice message — transcribed: "{qtext}"]')
            if loop_id:
                trace.user(tid, f"{owner} (replied-to voice message)", qtext,
                           loop_id=loop_id, wa_id=qid, source="audio")

    # Reply to a text message — added even when that message is in the window, because the
    # window does not say which message he is pointing at.
    quote = _quoted_line(state, records, owner, tz_name)
    if quote:
        reply_lines.append(quote[0])

    # Forwarded contact cards. Marked into the transcript, and their numbers remembered as
    # resolved ids for this loop — that is what lets `setup.enroll` name a contact at all.
    card_keys, card_views = _collect_cards(raw if reset else new, state.get("contact_cards"))

    # Durable transcript. On a fresh tag (loop open) log the whole seed — the ~30
    # messages before the tag, both sides — as the loop's opening context. On a window
    # continuation log only the new inbound; Mary's own replies are logged by `act`.
    if card_keys:  # a marked card changed the records, so re-render the labeled transcript
        transcript = build_labeled_transcript(raw if reset else new, owner, tz_name)
    if reply_lines:
        transcript = "\n".join(t for t in (transcript, *reply_lines) if t.strip())
    _log_transcript(trace, tid, loop_id, raw if reset else new, owner)
    if quote and loop_id:
        # After the seed, so the judge reads it where Lisa did: under the message that quotes it.
        # No wa_id: the log dedups transcript lines on (loop_id, wa_message_id), and the quoted
        # message's own line usually holds that id already, so this one would vanish.
        trace.user(tid, quote[1], quote[2], loop_id=loop_id, source="quote")

    # His messages that are new to this loop. An activation reads everything since the cursor, so
    # it can ingest messages that arrived after its own trigger — a contact's "Boa !" carried his
    # "sim" in with it, the resolver only read "Boa !", and the card went out a second time. The
    # resolver reads these instead; and when each one's own activation runs later, it is already
    # here, so it is not read (or allowed to approve anything) twice.
    if reset:
        # Everything already in the chat at a tag is history to the new loop, not an answer to
        # anything in it — and a cursor that falls out of the fetched range re-reads it later.
        prior_seen = [r["id"] for r in records if r.get("from_me") and r.get("id")]
    else:
        prior_seen = list(state.get("seen_owner_ids") or [])
    fresh = [r for r in new
             if r.get("from_me") and r.get("id") not in prior_seen
             and not is_own_message(r.get("text") or "", owner)]
    # A yes typed before the card existed is not an answer to it. One activation can now post a
    # result and then a new card, so a second "sim" that arrived in between looks fresh to the next
    # activation, and would approve a booking he had not seen. Read off the chat's own order.
    held_card = state.get("pending_card")
    answering = fresh
    if settings.contacts_crud_on and held_card and not reset:
        at = _find_card(records, held_card)
        if at is not None:
            later = {r.get("id") for r in records[at + 1:]}
            answering = [r for r in fresh if r.get("id") in later]
    owner_replies = [(r.get("text") or "").strip() for r in answering]
    seen_owner_ids = (prior_seen + [r["id"] for r in fresh if r.get("id")])[-200:]

    # A fresh tag under a card he already said yes to runs that write instead of dropping it
    # (carry.py). Decided before the reset below clears the held write.
    carried, carry_why = None, None
    if reset and not setup_open and state.get("pending_action"):
        domain = (state["pending_action"].get("task") or "").partition(".")[0]
        carried, carry_why = carried_yes(
            state, records, policy=(confirm_policies or {}).get(domain), echoes=echoes,
            settings=settings)

    # The turn is a plain string unless there are media blocks; then it's a content list with
    # the labeled transcript first, followed by the inline image/document blocks (chronological).
    content: object | None = None
    if transcript.strip() and media_blocks:
        content = [{"type": "text", "text": transcript}, *media_blocks]
    elif transcript.strip():
        content = transcript
    elif media_blocks:
        content = media_blocks
    # Nothing to put in front of her: an earlier activation read this message, or it carries
    # nothing she can read (a reaction, a sticker). The turn ends here, with no model call. Asked
    # again over the same thread she could only answer something a second time.
    nothing_new = content is None and not reset
    read_ids = ([] if reset else list(state.get("read_ids") or []))
    read_ids = (read_ids + [i for i in ids if i not in already])[-READ_IDS_KEPT:]

    trace.code(
        tid, node="context", loop_id=loop_id,
        initialized=bool(state.get("initialized")),
        reset=reset, setup_open=setup_open, ingested=len(new), context_message_ids=ids,
        carry=carry_why, carried_yes=carried, nothing_new=nothing_new or None,
    )

    update: dict = {
        "initialized": True,
        "nothing_new": nothing_new,
        "read_ids": read_ids,
        # The turn as the model will see it. `text` is only the triggering message and the
        # transcript was a local, so the address-book scan had nothing to read; publishing it here
        # is what lets a name mentioned three messages ago still resolve.
        "turn_text": transcript,
        # Who is in this room. Read from the WHOLE fetched history, not the window: membership is
        # a property of the chat, and someone who spoke yesterday is still here today.
        "participant_phones": _participant_phones(records),
        "last_whatsapp_message_id": newest,
        "context_message_ids": ids,
        # Per-activation tool-loop scratch — always fresh so a bound/log never carries over.
        "tool_hops": 0,
        "action_results": [],
        "last_ran": 0,
        "last_results": [],
        # The window decision is per-activation too. A path that never reaches `reason` (the
        # owner's yes runs the held write straight through) would otherwise act on the PREVIOUS
        # turn's state — the "awaiting_answer" of the question he just answered.
        "llm_state": "keep_listening",
        "card_out": None,
        "call": None,
        "next_call": None,
        "resume_caller": False,
        "skill_calls": 0,
        "carried_yes": carried,
        "owner_replies": owner_replies,
        "seen_owner_ids": seen_owner_ids,
    }
    # Who a one-to-one chat is with, by the name WhatsApp shows. Written only when the history has
    # a message of theirs: the chat is always the same person, so a turn that fetched none of
    # their messages keeps the name an earlier one found.
    if state.get("chat_kind") != "group":
        partner_name = _partner_name(records)
        if partner_name:
            update["partner_name"] = partner_name
    if nothing_new:
        # No model call this turn, so nothing would refresh what the last one left in the state:
        # `act` would send its reply again and the record would bill its tokens twice.
        update.update({"reply_body": None, "usage": None, "latency_ms": None,
                       "provider_request_id": None, "stop_reason": None, "tool_calls": [],
                       "error_category": "none"})
    if reset:
        # What he called her in for: the tagged message itself. Kept for the whole loop, because
        # it sets what she takes part in (prompt.py, "what you are here for"), and thirty turns
        # in it is a long way up the thread.
        update["called_for"] = called_for_text(state.get("text"))
        # New loop → drop tool memory too, so a stale goal or a resolved id from the previous
        # loop can never bleed into this one (the anti-delirium invariant). A fresh tag is a new
        # request, so a write awaiting confirmation is abandoned (cleared), not silently run —
        # unless he already said yes under its card, which `carried` (above) re-keeps at the end.
        update["workflow"] = None
        update["loop_domain"] = None
        update["seen_event_ids"] = []
        update["seen_events"] = {}
        update["seen_chat_keys"] = []
        update["seen_chats"] = {}
        update["listed_chats"] = {}
        update["pending_action"] = None
        update["pending_card"] = None
        update["last_confirm_sig"] = None
        # Contact memory is checkpointed, not per-turn scratch (the same reason seen_event_ids
        # needs clearing), so a stale address or a pending write cannot bleed into the next loop.
        update["seen_contacts"] = {}
        update["seen_people"] = {}
        update["side_effects"] = []
        update["pending_side_effects"] = []
        update["skill_stack"] = []
        update["pending_call"] = None

    if card_keys:
        prior = [] if reset else list(state.get("seen_chat_keys") or [])
        update["seen_chat_keys"] = prior + [k for k in card_keys if k not in prior]
        update["seen_chats"] = {**({} if reset else dict(state.get("seen_chats") or {})),
                                **card_views}

    # add_messages appends, so to truly start fresh we must first REMOVE every
    # message the checkpoint restored, then add this loop's seed turn.
    msgs: list = []
    if reset:
        for m in state.get("messages") or []:
            mid = getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
            if mid:
                msgs.append(RemoveMessage(id=mid))
        update["session_lang"] = None  # re-lock the language on this tag's turn
    if content is not None:
        msgs.append({"role": "user", "content": content})
    if msgs:
        update["messages"] = msgs
    if carried:
        # The new loop starts clean except for the approved write and what running it needs.
        update.update(kept_on_carry(state))
    return update
