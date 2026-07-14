"""Drive the real board page in a real browser. Click the chip, type, click Send.

This exists because of a bug that made the whole board look dead: both drawer-opening
functions set "which chat is open" and then immediately wiped it (`show()` calls
`closeAll()`, which nulls it), so the composer's `openMgr && send(...)` guard
short-circuited and **no websocket frame was ever sent**. Nothing threw. Nothing logged.
The message just vanished.

No unit test would have caught it and no amount of reading the diff did either. The only
thing that catches it is doing what a human does: open the drawer, type, hit send, and
check the frame actually reached the server.

    python tests/ui_test.py          (mock mode; needs Google Chrome; no API key)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.restart_test import Server, check, free_port, get, post, section  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

DRIVER = r"""
const CDP_URL = process.argv[2], PORT = process.argv[3], CARD_ID = process.argv[4], CARD_ID2 = process.argv[5];

const sock = new WebSocket(CDP_URL);
let id = 0;
const waiting = new Map();
const call = (method, params={}) => new Promise(res => {
  const msgId = ++id;
  waiting.set(msgId, res);
  sock.send(JSON.stringify({id: msgId, method, params}));
});
const evaluate = expr => call('Runtime.evaluate', {expression: expr, awaitPromise: true, returnByValue: true})
  .then(r => r.result?.result?.value);
const sleep = ms => new Promise(r => setTimeout(r, ms));

sock.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
};

sock.onopen = async () => {
  const out = {};
  await call('Runtime.enable');
  await call('Page.enable');
  await call('Page.navigate', {url: `http://127.0.0.1:${PORT}/`});
  await sleep(1500);

  // --- the board-level manager chat: click the 🧭 chip, type, click Send -------------
  await evaluate(`document.querySelector('#managers .mgr-chip').click()`);
  await sleep(400);
  out.mgr_drawer_open = await evaluate(`document.getElementById('d-mgr').classList.contains('open')`);
  out.openMgr_after_open = await evaluate(`String(openMgr)`);   // the bug: this was "null"

  await evaluate(`document.getElementById('m-input').value = 'hello from the browser'`);
  await evaluate(`document.getElementById('m-send').click()`);
  await sleep(600);
  out.mgr_echo = await evaluate(
    `[...document.querySelectorAll('#m-thread .msg.user')].some(e => e.textContent.includes('hello from the browser'))`);

  // --- a card chat: same bug lived in openCardDrawer ---------------------------------
  await evaluate(`openCardDrawer('${CARD_ID}')`);
  await sleep(400);
  out.card_drawer_open = await evaluate(`document.getElementById('d-card').classList.contains('open')`);
  out.openCard_after_open = await evaluate(`String(openCard)`);

  await evaluate(`document.getElementById('c-input').value = 'start'`);
  await evaluate(`document.getElementById('c-send').click()`);
  await sleep(600);
  out.card_echo = await evaluate(
    `[...document.querySelectorAll('#c-thread .msg.user')].some(e => e.textContent.includes('start'))`);
  out.no_stuck_pending = await evaluate(`document.querySelectorAll('#c-thread .msg.failed').length === 0`);

  // --- the open card is LIT on the board, and its drawer title matches it -------------
  // Static text cannot catch this: openCardDrawer() runs show() -> closeAll() FIRST, which
  // nulls the open card. Only a real DOM says who is actually lit, and what colour landed.
  out.lit_on_open = await evaluate(`[...document.querySelectorAll('.card.open-chat')].map(e=>e.dataset.id).join(',')`);
  out.dh_bg = await evaluate(`getComputedStyle(document.querySelector('#d-card .dh')).backgroundColor`);

  await evaluate(`openCardDrawer('${CARD_ID2}')`); await sleep(300);
  out.lit_after_switch = await evaluate(`[...document.querySelectorAll('.card.open-chat')].map(e=>e.dataset.id).join(',')`);

  await evaluate(`closeAll()`); await sleep(150);
  out.lit_after_close = await evaluate(`document.querySelectorAll('.card.open-chat').length`);

  // --- the card chat shows what the manager SAID, not what he RAN ---------------------
  await evaluate(`openCardDrawer('${CARD_ID}')`); await sleep(500);
  const visible = sel => `[...document.querySelectorAll('${sel}')].filter(e=>e.offsetParent!==null).length`;
  out.act_total = await evaluate(`document.querySelectorAll('#c-thread .msg.activity').length`);
  out.act_visible = await evaluate(visible('#c-thread .msg.activity'));
  out.worker_visible = await evaluate(visible('#c-thread .msg.worker'));
  out.mgr_visible = await evaluate(visible('#c-thread .msg.manager'));
  out.user_visible = await evaluate(visible('#c-thread .msg.user'));
  out.cmd_on_screen = await evaluate(
    `[...document.querySelectorAll('#c-thread .msg')].filter(e=>e.offsetParent!==null).some(e=>e.textContent.includes('delegating'))`);

  // 🔧 brings the working-out back
  await evaluate(`document.getElementById('work-btn').click()`); await sleep(200);
  out.act_visible_after = await evaluate(visible('#c-thread .msg.activity'));
  out.worker_visible_after = await evaluate(visible('#c-thread .msg.worker'));
  await evaluate(`document.getElementById('work-btn').click()`); await sleep(200);
  out.act_hidden_again = await evaluate(visible('#c-thread .msg.activity'));

  // a legacy thread — activity posted under the OLD "system" role — is re-classified
  out.legacy = await evaluate(`roleOf({role:'system', text:'⌘ ls -la /tmp'})`);
  out.legacy_delegate = await evaluate(`roleOf({role:'system', text:'→ delegating to **build__preflight**'})`);
  out.notice_kept = await evaluate(`roleOf({role:'system', text:'⚠️ manager error: boom'})`);
  out.recovery_kept = await evaluate(`roleOf({role:'system', text:'🛑 This run has now been interrupted 3 times.'})`);

  // --- creating a card asks IN THE APP, never with a browser prompt() -----------------
  // The browser's prompt()/confirm()/alert() cannot be styled, cannot show a type picker,
  // and block the tab. If any of them come back, these go red: a stubbed prompt() that is
  // never called is the only way to prove the app is not reaching for it.
  await evaluate(`window.__native = 0;
    window.prompt = () => { window.__native++; return null; };
    window.confirm = () => { window.__native++; return false; };
    window.alert  = () => { window.__native++; };`);
  await evaluate(`closeAll(); document.querySelector('.backlog .bl-new').click()`);
  await sleep(300);
  out.modal_open = await evaluate(`document.getElementById('modal').classList.contains('open')`);
  out.modal_title = await evaluate(`document.getElementById('modal-title').textContent`);
  out.modal_labels = await evaluate(
    `[...document.querySelectorAll('#modal-body label')].map(e=>e.textContent).join(' | ')`);
  out.modal_choices = await evaluate(`document.querySelectorAll('#modal-body .choice').length`);
  // the brief is a place to actually write, and the type picker is ONE line, not a wall
  out.brief_h = await evaluate(`Math.round(document.querySelector('#modal-body textarea').getBoundingClientRect().height)`);
  out.choice_rows = await evaluate(
    `new Set([...document.querySelectorAll('#modal-body .choice')].map(e=>Math.round(e.getBoundingClientRect().top))).size`);
  out.choices_h = await evaluate(
    `Math.round(document.querySelector('#modal-body .choices').getBoundingClientRect().height)`);
  // title is MANDATORY: the button is dead until it has one
  out.ok_disabled_empty = await evaluate(`document.getElementById('modal-ok').disabled`);
  await evaluate(`(i=>{i.value='Made in the app'; i.dispatchEvent(new Event('input'))})(document.querySelector('#modal-body input'))`);
  await sleep(100);
  out.ok_enabled_titled = await evaluate(`!document.getElementById('modal-ok').disabled`);
  // brief and type may be left blank — submit with only a title
  await evaluate(`document.getElementById('modal-ok').click()`); await sleep(900);
  out.modal_closed = await evaluate(`!document.getElementById('modal').classList.contains('open')`);
  out.native_used = await evaluate(`window.__native`);

  // --- the BACKLOG sits above every pipeline, and is where cards are created -----------
  out.backlog_present = await evaluate(`!!document.querySelector('.backlog')`);
  out.backlog_first = await evaluate(`document.getElementById('boards').firstElementChild.classList.contains('backlog')`);
  out.backlog_cards = await evaluate(`document.querySelectorAll('.backlog .card').length`);
  out.backlog_untyped = await evaluate(`document.querySelectorAll('.backlog .card[data-kind="unset"]').length`);
  out.backlog_kinds = await evaluate(
    `[...document.querySelectorAll('.backlog .card')].map(e=>e.dataset.kind).join(',')`);
  out.backlog_new_btn = await evaluate(`!!document.querySelector('.backlog .bl-new')`);
  out.no_add_in_pipelines = await evaluate(`document.querySelectorAll('.pipe .add-card').length`);
  out.pipelines_rendered = await evaluate(`[...document.querySelectorAll('.pipe .pipe-h .name')].map(e=>e.textContent).join(' | ')`);

  // --- a decision is FILED, not said: the chat only carries what he needs you for ------
  await evaluate(`openCardDrawer('${CARD_ID}')`); await sleep(400);
  // Distinct timestamps: appendMsg de-dupes on `ts <= seenTs`, so two messages stamped in the
  // same millisecond would see the second dropped. Real messages are microseconds apart.
  await evaluate(`appendMsg('c-thread',{role:'note',text:'PREFLIGHT: GO. Eleven fields verified.',ts:Date.now()/1000});
                  appendMsg('c-thread',{role:'manager',text:'Gate: your call.',ts:Date.now()/1000 + 1});`);
  await sleep(500);   // the board may repaint the drawer under us; let it settle
  const vis = sel => `[...document.querySelectorAll('${sel}')].filter(e=>e.offsetParent!==null).length`;
  out.note_hidden_in_chat = await evaluate(vis('#c-thread .msg.note'));
  out.msg_shown_in_chat = await evaluate(
    `[...document.querySelectorAll('#c-thread .msg.manager')].filter(e=>e.offsetParent!==null).some(e=>e.textContent.includes('your call'))`);
  await evaluate(`document.getElementById('tab-notes').click()`); await sleep(200);
  out.note_shown_in_tab = await evaluate(vis('#c-thread .msg.note'));
  out.chat_hidden_in_tab = await evaluate(vis('#c-thread .msg.manager'));
  out.composer_hidden = await evaluate(
    `getComputedStyle(document.querySelector('#d-card .composer')).display === 'none'`);
  await evaluate(`document.getElementById('tab-chat').click()`); await sleep(150);
  out.back_to_chat = await evaluate(vis('#c-thread .msg.note'));

  // --- the card shows the BRIEF I wrote, and lets me TRASH it -------------------------
  await evaluate(`openCardDrawer('${CARD_ID}')`); await sleep(500);
  out.brief_text = await evaluate(`document.getElementById('c-brief').textContent`);
  out.brief_visible = await evaluate(`document.getElementById('c-brief').offsetParent !== null`);
  out.trash_visible = await evaluate(`document.getElementById('c-trash').offsetParent !== null`);
  // trashing asks first, and a cancel leaves the card alone
  await evaluate(`document.getElementById('c-trash').click()`); await sleep(300);
  out.trash_asks = await evaluate(`document.getElementById('modal').classList.contains('open')`);
  out.trash_modal = await evaluate(`document.getElementById('modal-title').textContent`);
  await evaluate(`document.getElementById('modal-cancel').click()`); await sleep(250);
  out.trash_cancelled = await evaluate(`!document.getElementById('modal').classList.contains('open')`);

  // --- every message says WHEN it was sent -------------------------------------------
  out.stamped = await evaluate(
    `[...document.querySelectorAll('#c-thread .msg')].every(e => e.querySelector('.at'))`);
  out.stamp_sample = await evaluate(`document.querySelector('#c-thread .msg .at').textContent`);
  out.stamp_today = await evaluate(`stamp(Date.now()/1000)`);
  out.stamp_old = await evaluate(`stamp(Date.now()/1000 - 86400*5)`);

  // --- WHO is working: the manager, or a named worker --------------------------------
  out.who_manager = await evaluate(`whoIsWorking({busy:true, working:''})`);
  out.who_worker = await evaluate(`whoIsWorking({busy:true, working:'build__build_review'})`);
  out.who_idle = await evaluate(`whoIsWorking({busy:false, working:''})`);

  // --- reading history must NOT be yanked to the bottom by a new message --------------
  // Make the thread long enough to actually scroll, then scroll UP and push a message in.
  await evaluate(`openCardDrawer('${CARD_ID}')`); await sleep(400);
  await evaluate(`for(let i=0;i<40;i++) appendMsg('c-thread',{role:'manager',text:'filler '+i,ts:Date.now()/1000});`);
  await sleep(150);
  out.scrollable = await evaluate(`(t=>t.scrollHeight > t.clientHeight + 100)(document.getElementById('c-thread'))`);
  await evaluate(`document.getElementById('c-thread').scrollTop = 0`);  // read the top
  await sleep(150);
  out.top_before = await evaluate(`document.getElementById('c-thread').scrollTop`);

  await evaluate(`appendMsg('c-thread',{role:'manager',text:'a new message arrived',ts:Date.now()/1000})`);
  await sleep(200);
  out.top_after = await evaluate(`document.getElementById('c-thread').scrollTop`);
  out.pill_shown = await evaluate(`document.querySelector('#c-thread').parentNode.querySelector('.new-msgs').classList.contains('on')`);
  out.pill_text = await evaluate(`document.querySelector('#c-thread').parentNode.querySelector('.new-msgs').textContent`);

  // a SECOND one counts up, still without moving me
  await evaluate(`appendMsg('c-thread',{role:'manager',text:'and another',ts:Date.now()/1000})`);
  await sleep(150);
  out.pill_text2 = await evaluate(`document.querySelector('#c-thread').parentNode.querySelector('.new-msgs').textContent`);
  out.top_after2 = await evaluate(`document.getElementById('c-thread').scrollTop`);

  // clicking the pill takes me to the bottom, and it goes away
  await evaluate(`document.querySelector('#c-thread').parentNode.querySelector('.new-msgs').click()`);
  await sleep(250);
  out.pill_gone = await evaluate(`!document.querySelector('#c-thread').parentNode.querySelector('.new-msgs').classList.contains('on')`);
  out.at_bottom = await evaluate(`(t=>t.scrollHeight - t.scrollTop - t.clientHeight <= 60)(document.getElementById('c-thread'))`);

  // and once I AM at the bottom, new messages just follow, as before
  await evaluate(`appendMsg('c-thread',{role:'manager',text:'following along',ts:Date.now()/1000})`);
  await sleep(200);
  out.follows_when_down = await evaluate(`(t=>t.scrollHeight - t.scrollTop - t.clientHeight <= 60)(document.getElementById('c-thread'))`);
  out.no_pill_when_down = await evaluate(`!document.querySelector('#c-thread').parentNode.querySelector('.new-msgs').classList.contains('on')`);

  // --- the pipeline panel: 🧠 opens the worker, and EVERY drawer can be closed ----------
  // closeAll() used to iterate a hardcoded id list. A drawer missing from it could not be
  // closed by the ✕ or the scrim, and sat on top of whatever opened next — so the 🧠 button
  // looked dead and the panel was stuck until a page refresh. Drive the real sequence.
  await evaluate(`openPipe('maint')`); await sleep(250);
  out.pipe_open = await evaluate(`document.getElementById('d-pipe').classList.contains('open')`);
  out.pipe_rows = await evaluate(`document.querySelectorAll('#p-cols .crow').length`);

  // click the REAL 🧠 button of the first column, the way a human does
  await evaluate(`[...document.querySelectorAll('#p-cols .crow')][0].querySelector('.mini[title*="worker"]').click()`);
  await sleep(700);
  out.worker_open = await evaluate(`document.getElementById('d-worker').classList.contains('open')`);
  out.pipe_closed_behind = await evaluate(`!document.getElementById('d-pipe').classList.contains('open')`);
  out.worker_loaded = await evaluate(`document.getElementById('w-md').value.slice(0,200)`);


  // --- the worker chat: it collapses the file, and the ✕ goes BACK to the pipeline -------
  out.chat_title = await evaluate(`document.querySelector('#d-worker .wchat-h').textContent`);
  out.code_h_before = await evaluate(`Math.round(document.getElementById('w-md').getBoundingClientRect().height)`);
  await evaluate(`document.getElementById('w-input').value='make the exit criteria falsifiable'; document.getElementById('w-chat-send').click()`);
  await sleep(1200);
  out.chatting = await evaluate(`document.getElementById('d-worker').classList.contains('chatting')`);
  out.code_h_after = await evaluate(`Math.round(document.getElementById('w-md').getBoundingClientRect().height)`);
  out.chat_msgs = await evaluate(`document.querySelectorAll('#w-thread .msg').length`);
  out.chat_has_manager = await evaluate(`!!document.querySelector('#w-thread .msg.manager')`);
  // ...and it is NOT in the board chat: separate conversation, separate thread
  out.mgr_thread_untouched = await evaluate(
    `![...document.querySelectorAll('#m-thread .msg')].some(e=>e.textContent.includes('falsifiable'))`);

  // ✕ from a worker opened FROM the pipeline panel → back to the pipeline panel
  await evaluate(`document.getElementById('w-close').click()`); await sleep(300);
  out.x_returns_to_pipe = await evaluate(`document.getElementById('d-pipe').classList.contains('open')`);

  // ✕ from a worker opened from the BOARD (a column's 🧠) → back to the board, no drawer
  await evaluate(`closeAll()`); await sleep(120);
  await evaluate(`document.querySelectorAll('.col-h .tool')[0].click()`); await sleep(600);
  out.worker_from_board = await evaluate(`document.getElementById('d-worker').classList.contains('open')`);
  await evaluate(`document.getElementById('w-close').click()`); await sleep(250);
  out.x_returns_to_board = await evaluate(`document.querySelectorAll('.drawer.open').length`);

  // --- the manager's BRAIN: 🧠 on his chat → his standing orders, editable + discussable ---
  await evaluate(`closeAll()`); await sleep(120);
  await evaluate(`document.querySelector('#managers .mgr-chip').click()`); await sleep(350);
  await evaluate(`document.getElementById('m-brain').click()`); await sleep(800);
  out.brain_open = await evaluate(`document.getElementById('d-brain').classList.contains('open')`);
  out.brain_md = await evaluate(`document.getElementById('b-md').value.slice(0,400)`);
  out.brain_path = await evaluate(`document.getElementById('b-path').textContent`);

  await evaluate(`document.getElementById('b-input').value='you ask me too much'; document.getElementById('b-chat-send').click()`);
  await sleep(1200);
  out.brain_chatting = await evaluate(`document.getElementById('d-brain').classList.contains('chatting')`);
  out.brain_msgs = await evaluate(`document.querySelectorAll('#b-thread .msg').length`);
  // the brain chat must NOT land in the worker thread, nor in the board chat
  out.brain_isolated = await evaluate(
    `![...document.querySelectorAll('#w-thread .msg, #m-thread .msg')].some(e=>e.textContent.includes('you ask me too much'))`);

  await evaluate(`document.getElementById('b-close').click()`); await sleep(300);
  out.brain_x_returns_to_mgr = await evaluate(`document.getElementById('d-mgr').classList.contains('open')`);

  // the ✕ must actually close it — from any drawer
  await evaluate(`openPipe('maint')`); await sleep(200);
  await evaluate(`document.querySelector('#d-pipe [data-close]').click()`); await sleep(200);
  out.closed_by_x = await evaluate(`document.querySelectorAll('.drawer.open').length`);
  await evaluate(`openPipe('maint')`); await sleep(200);
  await evaluate(`document.getElementById('scrim').click()`); await sleep(200);
  out.closed_by_scrim = await evaluate(`document.querySelectorAll('.drawer.open').length`);

  // --- the board FITS. Only a real layout can answer this; a CSS grep cannot. ----------
  out.h_scroll = await evaluate(`document.getElementById('boards').scrollWidth - document.getElementById('boards').clientWidth`);
  // NOT documentElement.scrollWidth: the closed drawers sit at translateX(101%), just off the
  // right edge, so the document is "wider" than the viewport by design. body{overflow:hidden}
  // is what makes that unscrollable — so THAT is the thing worth asserting.
  out.body_overflow_x = await evaluate(`getComputedStyle(document.body).overflowX`);
  // every column, on every pipeline, the same width
  out.col_widths = await evaluate(
    `JSON.stringify([...new Set([...document.querySelectorAll('.col')].map(e=>Math.round(e.getBoundingClientRect().width)))])`);
  out.n_rows = await evaluate(`document.querySelectorAll('.row').length`);
  // a card in BUILD that came from maintenance keeps its own colour — not build's
  out.build_card_bgs = await evaluate(`JSON.stringify(
    [...document.querySelectorAll('.card')].map(e=>[e.dataset.kind, getComputedStyle(e).backgroundColor]))`);

  console.log(JSON.stringify(out));
  sock.close();
  process.exit(0);
};
"""


def main() -> int:
    if not os.path.exists(CHROME):
        print("Google Chrome not found — skipping the browser test.")
        return 0
    if not shutil.which("node"):
        print("node not found — skipping the browser test.")
        return 0

    ws_dir = tempfile.mkdtemp(prefix="km-ui-ws-")
    repo = tempfile.mkdtemp(prefix="km-ui-repo-")
    port, cdp_port = free_port(), free_port()

    srv = Server(ws_dir, repo, port)
    srv.start()
    cid = post(port, "/api/card", {"title": "Clicked in a real browser", "pipeline": "plan", "kind": "feature",
                                   "description": "The brief I typed when I made this card."})["id"]
    cid2 = post(port, "/api/card", {"title": "The other card", "pipeline": "plan", "kind": "feature"})["id"]
    # A real maintenance card, so "a fix is a different colour from a feature" cannot pass
    # vacuously by there being no fix on the board at all.
    post(port, "/api/card", {"title": "Something is broken", "pipeline": "maint", "kind": "maintenance"})
    # A card created the NORMAL way: no pipeline, no type. It must land in the backlog and be
    # rendered there, greyed, asking for a type.
    post(port, "/api/card", {"title": "Someone asked for a thing"})

    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={cdp_port}",
         "--no-first-run", "--user-data-dir=" + tempfile.mkdtemp(prefix="km-chrome-"), "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    driver = os.path.join(tempfile.mkdtemp(), "drive.mjs")
    with open(driver, "w") as fh:
        fh.write(DRIVER)

    try:
        target = None
        deadline = time.time() + 20
        while time.time() < deadline and not target:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=2) as r:
                    tabs = json.load(r)
                target = next((t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page"), None)
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        if not target:
            print("could not attach to Chrome — skipping.")
            return 0

        proc = subprocess.run(
            ["node", driver, target, str(port), cid, cid2],
            capture_output=True, text=True, timeout=90,
        )
        if proc.returncode != 0:
            print(proc.stdout, proc.stderr)
            return 1
        out = json.loads(proc.stdout.strip().splitlines()[-1])

        section("the board-level manager chat, driven by a real browser")
        check("clicking the chip opens the drawer", out["mgr_drawer_open"])
        check("the open manager is REMEMBERED (this is the bug)", out["openMgr_after_open"] != "null")
        check("what I typed appears in the thread", out["mgr_echo"])

        section("the card chat")
        check("the drawer opens", out["card_drawer_open"])
        check("the open card is REMEMBERED", out["openCard_after_open"] != "null")
        check("what I typed appears in the thread", out["card_echo"])
        check("nothing was left marked undelivered", out["no_stuck_pending"])

        section("the open-card highlight")
        check("exactly the open card is lit", out["lit_on_open"] == cid)
        check("the drawer title carries the pre-composited tint", out["dh_bg"] == "rgb(27, 37, 59)")
        check("opening another card moves the highlight — never two", out["lit_after_switch"] == cid2)
        # the `lit_after_switch` half is what stops this passing vacuously: "closing cleared it"
        # means nothing unless something was lit in the first place.
        check("closing clears it SYNCHRONOUSLY (closeAll does not re-render)",
              out["lit_after_switch"] != "" and out["lit_after_close"] == 0)

        section("creating a card is done IN THE APP, not by the browser")
        check("clicking + New card opens the app's own dialog", out["modal_open"])
        check(f"...titled properly ('{out['modal_title']}')", out["modal_title"] == "New card")
        check(f"...asking title / brief / type ('{out['modal_labels']}')",
              all(w in out["modal_labels"].lower() for w in ("title", "brief", "type")))
        check("...with a real type picker, not a letter to type", out["modal_choices"] == 3)
        check(f"the brief is big enough to write in ({out['brief_h']}px)", out["brief_h"] >= 180)
        check(f"the type picker is ONE line ({out['choice_rows']} row)", out["choice_rows"] == 1)
        check(
            f"...and is not the biggest thing in the dialog ({out['choices_h']}px vs brief {out['brief_h']}px)",
            out["choices_h"] < out["brief_h"],
        )
        check("TITLE IS MANDATORY: the button is dead while it is empty", out["ok_disabled_empty"])
        check("...and alive once it has one", out["ok_enabled_titled"])
        check("brief and type can be left blank — it submitted", out["modal_closed"])
        check("and the browser's prompt/confirm/alert were NEVER used", out["native_used"] == 0)
        check(
            "the card it made is on the board",
            any(c["title"] == "Made in the app" for c in get(port, "/api/board")["cards"]),
        )

        section("the backlog: above the pipelines, and the only place cards are made")
        check("it is rendered", out["backlog_present"])
        check("...above every pipeline", out["backlog_first"])
        check("the untyped card landed there", out["backlog_cards"] >= 1)
        # The card was created with NO type. By the time the page renders, the manager has
        # already been asked to classify it — so the invariant to assert is that nothing is
        # left untyped, not that something is.
        check(
            f"the manager typed it on arrival — nothing left needing one (kinds: {out['backlog_kinds']})",
            out["backlog_untyped"] == 0 and out["backlog_kinds"],
        )
        check(
            "and the server agrees: no card anywhere is untyped",
            all(c["kind"] in ("feature", "maintenance") for c in get(port, "/api/board")["cards"]),
        )
        check("cards are created there", out["backlog_new_btn"])
        check("...and NOWHERE else — no '+ card' inside a pipeline", out["no_add_in_pipelines"] == 0)
        check(
            f"all four pipelines render ({out['pipelines_rendered']})",
            "Expedited" in out["pipelines_rendered"] and "Build" in out["pipelines_rendered"],
        )

        section("the card chat shows what the manager SAID, not what he RAN")
        check("the manager's messages are there", out["mgr_visible"] >= 1)
        check("...and mine", out["user_visible"] >= 1)
        check("the run DID produce tool activity", out["act_total"] >= 1)
        check("...but none of it is on screen", out["act_visible"] == 0)
        check("...no '⌘'/'delegating' line is visible", not out["cmd_on_screen"])
        check("worker reports are hidden too", out["worker_visible"] == 0)

        check("🔧 brings the working-out back", out["act_visible_after"] >= 1)
        check("...including the worker reports", out["worker_visible_after"] >= 1)
        check("...and clicking it again hides them", out["act_hidden_again"] == 0)

        section("old threads are cleaned up, but notices are never swallowed")
        check("a legacy ⌘ line is re-classified as activity", out["legacy"] == "activity")
        check("...so is a legacy delegating line", out["legacy_delegate"] == "activity")
        check("an ERROR stays a visible notice", out["notice_kept"] == "system")
        check("...so does a recovery notice", out["recovery_kept"] == "system")

        section("reading history is never interrupted by a new message")
        check("the thread is long enough to scroll", out["scrollable"])
        check("I scrolled to the top", out["top_before"] == 0)
        check("a new message does NOT move me", out["top_after"] == 0)
        check("...a second one does not either", out["top_after2"] == 0)
        check(f"a discreet pill appears instead ('{out['pill_text']}')", out["pill_shown"])
        # It may already have counted a real message that landed while we were scrolled up,
        # so assert the INCREMENT — that is the actual behaviour — not an absolute number.
        n1 = int("".join(c for c in out["pill_text"] if c.isdigit()) or 0)
        n2 = int("".join(c for c in out["pill_text2"] if c.isdigit()) or 0)
        # NOT `n2 == n1 + 1`: real manager messages land in this thread while the test runs,
        # and they count too — correctly. The behaviour under test is that the count RISES and
        # the scroll never moves, not that nothing else is talking.
        check(f"...and it counts them up ({n1} → {n2})", n2 > n1 and "new message" in out["pill_text2"])
        check("clicking it takes me to the bottom", out["at_bottom"])
        check("...and the pill goes away", out["pill_gone"])
        check("once at the bottom, messages follow again", out["follows_when_down"])
        check("...with no pill in the way", out["no_pill_when_down"])

        section("the manager's reasoning is FILED, not shoved at me")
        check("a decision does not appear in the chat", out["note_hidden_in_chat"] == 0)
        check("...but what he needs me FOR does", out["msg_shown_in_chat"])
        check("the Decisions tab shows the reasoning", out["note_shown_in_tab"] >= 1)
        check("...and only that", out["chat_hidden_in_tab"] == 0)
        check("...with no composer — it is a record, not a conversation", out["composer_hidden"])
        check("back to Chat, and the decisions are out of the way again", out["back_to_chat"] == 0)

        section("the card shows its brief, and can be trashed")
        check(f"the brief I wrote is on the card ('{out['brief_text'][:40]}…')",
              "The brief I typed when I made this card." in out["brief_text"])
        check("...and it is actually visible", out["brief_visible"])
        check("there is a trash button on the card", out["trash_visible"])
        check("...which ASKS before trashing", out["trash_asks"])
        check(f"...in the app, not the browser ('{out['trash_modal']}')", "trash" in out["trash_modal"].lower())
        check("...and cancelling leaves the card alone", out["trash_cancelled"])
        check("the card is still on the board", any(c["id"] == cid for c in get(port, "/api/board")["cards"]))

        section("every message says when it was sent")
        check(f"every message in the thread is stamped (e.g. '{out['stamp_sample']}')", out["stamped"])
        # The clock format is the viewer's locale (12h here, 24h elsewhere) — assert the
        # DIFFERENCE, which is the actual rule: today = time only, older = date + time.
        check(f"today's messages show the clock ('{out['stamp_today']}')", ":" in out["stamp_today"])
        check(
            f"older ones carry the date too ('{out['stamp_old']}')",
            out["stamp_old"].endswith(out["stamp_today"].split()[0]) or len(out["stamp_old"]) > len(out["stamp_today"]),
        )

        section("the board says WHO is working, not just 'working'")
        check("the manager, when it is him", out["who_manager"] == "🧭 manager")
        check("...and the WORKER by name, when it is not", out["who_worker"] == "🔨 build review")
        check("nothing, when the card is idle", out["who_idle"] == "")

        section("the pipeline panel: 🧠 opens the worker, and drawers actually close")
        check("the pipeline panel opens", out["pipe_open"])
        check("it lists the columns", out["pipe_rows"] == 5)
        check("clicking 🧠 opens the worker drawer", out["worker_open"])
        check("...and the pipeline panel gets out of the way", out["pipe_closed_behind"])
        check(
            "...with the worker's REAL prompt loaded (not 'loading…')",
            "Entry criteria" in out["worker_loaded"] and "loading" not in out["worker_loaded"],
        )
        check("the ✕ closes it — no drawer left open", out["closed_by_x"] == 0)
        check("clicking outside closes it too", out["closed_by_scrim"] == 0)

        section("the worker chat")
        check("it is titled as asked", out["chat_title"] == "Talk to the manager about this worker")
        check("my message and the manager's reply are in the thread", out["chat_msgs"] >= 2)
        check("the manager actually answered", out["chat_has_manager"])
        check("starting the chat COLLAPSES the worker file", out["chatting"])
        check(
            f"...the file really shrank ({out['code_h_before']}px → {out['code_h_after']}px)",
            out["code_h_after"] < out["code_h_before"],
        )
        check(
            "it is its OWN conversation — it did not land in the board chat",
            out["mgr_thread_untouched"],
        )

        section("the manager's brain")
        check("🧠 on his chat opens his standing orders", out["brain_open"])
        check(
            "...loaded from MANAGER.md, with the real orders in it",
            "Decide. Do not ask." in out["brain_md"],
        )
        check("...and it says where the file lives", "MANAGER.md" in out["brain_path"])
        check("I can talk to him about rewriting them", out["brain_msgs"] >= 2)
        check("...which collapses the file, same as a worker", out["brain_chatting"])
        check(
            "...and it is its OWN conversation (not the board chat, not a worker's)",
            out["brain_isolated"],
        )
        check("the ✕ takes me back to his chat", out["brain_x_returns_to_mgr"])

        section("the ✕ goes back where you came from")
        check("from the pipeline panel → back to the pipeline panel", out["x_returns_to_pipe"])
        check("a column's 🧠 on the board opens the worker", out["worker_from_board"])
        check("...and from there the ✕ → back to the board", out["x_returns_to_board"] == 0)

        section("the board fits the screen (measured, not grepped)")
        check("the board does not scroll sideways", out["h_scroll"] <= 0)
        check("and the page cannot either", out["body_overflow_x"] == "hidden")
        check("all four pipelines rendered", out["n_rows"] == 4)
        widths = json.loads(out["col_widths"])
        check(
            f"every column is the same width across every pipeline (got {widths})",
            len(widths) == 1,
        )

        section("a card keeps its own colour, wherever it is parked")
        bgs = dict(json.loads(out["build_card_bgs"]))
        check("a feature card is painted", bool(bgs.get("feature")))
        check(
            "a maintenance card is a DIFFERENT colour from a feature card",
            "maintenance" not in bgs or bgs["maintenance"] != bgs["feature"],
        )

        section("and the frames actually reached the server")
        time.sleep(2.0)
        card = get(port, f"/api/card/{cid}")
        mgr_id = get(port, "/api/board")["managers"][0]["id"]
        mgr = get(port, f"/api/manager/{mgr_id}")
        check(
            "the manager_message frame landed (the manager replied)",
            any(m["role"] == "manager" for m in mgr["thread"]),
        )
        check(
            "the card message frame landed (a worker ran)",
            any(m["role"] == "worker" for m in card["thread"]),
        )
    finally:
        chrome.terminate()
        srv.kill_hard()

    from tests.restart_test import FAILED

    print(f"\n{'ALL PASSED' if not FAILED else 'FAILURES: ' + ', '.join(FAILED)}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
