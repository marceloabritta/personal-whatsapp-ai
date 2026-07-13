# Incident: the manager died mid-run, and the board could not recover

**Date:** 2026-07-12, ~17:17 local (20:17 UTC)
**Card affected:** `f6ea4100` — *Flight search via chat*, in Scoping
**Symptom the human saw:** the board hung. The card sat at "working" forever and never came back.
**Status:** root cause confirmed. **No fix has been implemented.** This document is the handoff.

---

## 1. What actually happened

The `plan__scoping` worker was dispatched on the flight card at 20:12:58 UTC. It ran for four
minutes, read the repo, and at **20:16:05** its last words in the transcript were:

> *"I have what I need. Writing the scope."*

Sixty seconds later, **mid-`Write`**, its session recorded:

> `[Request interrupted by user]`

`SCOPE.md` was roughly one second from hitting disk. It was never written. The manager's own
session transcript stops at the same instant and the process is gone.

**The cause: the human closed the VS Code window whose integrated terminal was running the
server.** That "user" in `[Request interrupted by user]` is not a human clicking anything — it is
the Agent SDK's `claude` CLI subprocess being torn down as its parent process died.

`run.sh` ends with:

```bash
exec "$HERE/.venv/bin/python" -m manager
```

and `manager/__main__.py` calls `uvicorn.run(...)` in the **foreground**. So the server is a
child of whatever terminal launched it. Closing the VS Code window tears down that terminal's
process group; the server takes a SIGHUP/SIGKILL and dies instantly. Nothing supervises it, so
nothing brings it back.

### The alternatives, and why each is ruled out

This matters, because the obvious guesses are all wrong and lead to the wrong fix.

| Hypothesis | Ruled out by |
|---|---|
| A bug / unhandled exception in the manager | An exception is caught at `manager/manager.py:94` and posted to the card thread as `⚠️ manager error`. **The card has no such message.** |
| A clean Ctrl-C (SIGINT) | SIGINT unwinds the `finally` at `manager/manager.py:97`, which clears `busy`. **`busy` stayed `true` on disk — no cleanup ran at all.** That is the signature of a hard kill. |
| Out of memory / Jetsam | The only Jetsam event on 2026-07-12 is at 16:15, an hour earlier, and python is not among its victims. No crash report exists in `DiagnosticReports`. |
| The browser tab / board UI was closed | Not a factor. `server.py:286` dispatches via `asyncio.create_task`, so a run is **not** tied to the WebSocket handler. Closing the board UI is safe. Good design — leave it alone. |

Corroborating detail: the human's `zsh` history has no entries from that window either, which is
what you see when a shell is killed rather than exited.

---

## 2. Why it could not recover — the real defect

Killing the process is the trigger. The **defect** is that the system has no memory of work in
flight, and no way back.

**A run exists only in RAM.** When `handle_card_message` dispatches, nothing durable records that
a run started, what card it was for, or what it was doing. So when the process dies:

1. The worker's output is lost outright. Four minutes of scoping work, one second from disk, gone.
2. On restart, the manager has **no idea a run was ever dispatched**. It cannot resume, retry, or
   even report the failure.
3. The card is left with `busy: true` persisted in `board.json`. `Board._load` (`board.py:66`)
   reads that straight back and `Board._bootstrap` (`board.py:81`) never resets it — so restarting
   just brings the card back spinning "working" forever, with no error and no way to message it.

**There are also no logs.** `uvicorn` logs to a stdout that dies with the terminal. There is no log
file anywhere. This incident had to be reconstructed from raw Claude session transcripts under
`~/.claude/projects/`. Whoever debugs the next one deserves better.

### ⚠️ The trap: do NOT "fix" this by clearing `busy` on boot

The tempting one-line fix is to reset `busy = False` in `_bootstrap`, on the reasoning that a
process which just started cannot have a run in flight. **Do not do this.** It is worse than the
bug:

- It only hides the spinner. The interrupted work is still lost.
- **`busy: true` on a freshly-loaded board is the single most valuable signal there is** — it is
  the only durable evidence that a run was cut off. Clearing it destroys the very fact that the
  recovery path needs to detect. You would be deleting the fire alarm to stop the noise.

The flag should be *read* at boot and *acted on*, not erased.

---

## 3. The asset already in place

The recovery path is cheap, because the hard part is already built: **the card's SDK session is
persisted and resumable.**

```python
# manager/manager.py:393, in _real_card
options = ClaudeAgentOptions(
    ...
    resume=card.session_id,   # ← already there
)
```

`card.session_id` survives in `board.json`. Re-entering a dead run's session — with its full
context of what the manager had already decided and delegated — is a matter of calling `query()`
with `resume=` set and a fresh prompt. That is the hook the whole wake-up hangs off.

---

## 4. Suggested fix — three layers

### Layer 1: Survive (detach + supervise)

Stop the server from being a child of a terminal, and let the OS restart it.

A `launchd` LaunchAgent, mirroring the one already in this repo at
`scripts/com.marcelo.secretary-triage.plist`:

```xml
<key>Label</key>          <string>com.marcelo.coding-kanban</string>
<key>ProgramArguments</key>
<array>
  <string>/bin/bash</string>
  <string>/Users/marceloabritta/Desktop/Coding/Personal Whatsapp AI/AI Coding-kanban/run.sh</string>
</array>
<key>KeepAlive</key>      <true/>   <!-- restart on ANY exit, including a kill -->
<key>RunAtLoad</key>      <true/>   <!-- up at login -->
<key>StandardOutPath</key><string>/Users/marceloabritta/Library/Logs/coding-kanban.log</string>
<key>StandardErrorPath</key><string>/Users/marceloabritta/Library/Logs/coding-kanban.log</string>
```

Result: `kill -9` → back in ~5s. Close VS Code → completely unaffected. And **a real log file**,
so the next incident does not require transcript archaeology.

*(The human should confirm they want `RunAtLoad=true` — i.e. the board always running at
`localhost:4173` from login — versus starting it explicitly and only having launchd resurrect it
on crash. Both are one boolean apart.)*

### Layer 2: Remember (a durable in-flight journal)

Before a run starts, write it down. Clear it when it finishes. Anything left over at boot was
interrupted.

```python
# in Board — persisted to data/inflight.json (atomic, same tmp+os.replace as _save)
{
  "card_id":    "f6ea4100",
  "kind":       "card",          # or "board"
  "text":       "<the message that triggered the run>",
  "session_id": "41d91aea-...",  # as known at dispatch
  "column":     "Scoping — user flow + out-of-scope line",
  "started_at": 1783886578.2
}
```

Written in `handle_card_message` immediately before the `try:` (`manager.py:89`), deleted in the
`finally` (`manager.py:97`). A crash between those two points leaves the entry on disk. That entry
*is* the recovery ticket.

This subsumes the `busy` flag: `busy` becomes a derived view of "has a live journal entry",
which also fixes the stuck-spinner symptom **as a consequence of** fixing the real problem, rather
than by papering over it.

### Layer 3: Resume (wake itself up)

On startup (a FastAPI lifespan hook in `server.py`), read the journal. For each leftover entry,
re-enter the card's session and tell it the truth about what happened:

```python
async def resume_interrupted_runs():
    for entry in board.inflight.all():
        await board.append_message(entry.card_id, "system",
            "⚠️ The previous run was cut off — the manager process died mid-flight "
            "(this is now recovered automatically). Resuming.")
        # resume=card.session_id gives the manager back everything it knew.
        await manager.handle_card_message(entry.card_id, RESUME_PROMPT)
```

with a prompt along the lines of:

> Your previous run on this card was interrupted mid-flight: the process was killed. You may have
> been part-way through a delegation, and a worker may have died before writing its output.
> **Do not assume any of it landed.** Inspect the card folder on disk, compare it against the
> column's exit criteria, and pick up exactly where the evidence says you left off — re-delegating
> the worker if its artifact is missing.

That last instruction is what makes it correct rather than merely clever: in *this* incident the
worker died one second before `Write`, so the manager must be told to verify disk state, not to
trust its own memory of having delegated.

### Layer 4 (small, but do it): handle signals

Trap `SIGTERM`/`SIGINT` and shut down gracefully — flush the journal, post a note to any affected
card thread. A clean stop should never leave the state that a kill leaves. This is what makes the
difference between "the operator restarted it" and "we lost work" legible in the board itself.

---

## 5. Acceptance test

The fix is real when this passes:

1. Start the server; send a card a message that triggers a worker delegation.
2. While the worker is running, `kill -9` the server process.
3. **Do nothing.**
4. Within seconds, launchd restarts it. The card thread shows the interruption note, the manager
   re-enters its session, inspects the card folder, sees the artifact is missing, and re-delegates.
5. The card reaches its column's exit criteria without a human touching anything.

---

## 6. Current state of the board (read before you start)

- The two cards that were stuck (`f6ea4100` *Flight search via chat*, `0631e2a5` *Fix schedulling
  task malfunction*) had their `busy` flags **cleared by hand on disk** during this investigation,
  so the board is usable right now and will not spin on restart.
- **The code is untouched.** The bug is fully present. Nothing in this document has been implemented.
- `f6ea4100` sits in Scoping with `IDEA.md` present and **no `SCOPE.md`** — the scoping run is the
  one that was lost. Its two blocking questions were already answered by the human and are recorded
  in the card's own thread (data source is open, as long as it is programmatically fetchable and not
  partner-gated; a booking link is sent as a follow-up turn on request, not on every option). The
  scoper can simply be re-run.
