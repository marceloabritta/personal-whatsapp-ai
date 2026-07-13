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
    cid = post(port, "/api/card", {"title": "Clicked in a real browser"})["id"]
    cid2 = post(port, "/api/card", {"title": "The other card"})["id"]
    # A real maintenance card, so "a fix is a different colour from a feature" cannot pass
    # vacuously by there being no fix on the board at all.
    post(port, "/api/card", {"title": "Something is broken", "pipeline": "maint"})

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

        section("the ✕ goes back where you came from")
        check("from the pipeline panel → back to the pipeline panel", out["x_returns_to_pipe"])
        check("a column's 🧠 on the board opens the worker", out["worker_from_board"])
        check("...and from there the ✕ → back to the board", out["x_returns_to_board"] == 0)

        section("the board fits the screen (measured, not grepped)")
        check("the board does not scroll sideways", out["h_scroll"] <= 0)
        check("and the page cannot either", out["body_overflow_x"] == "hidden")
        check("all three pipelines rendered", out["n_rows"] == 3)
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
