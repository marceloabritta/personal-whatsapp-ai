"""The fix loop's tool on the Mac, command by command, with nothing real behind it.

`fixer/run.py` is what the daily session calls to take a case and to hand its work in. It talks
to the queue on the droplet (over ssh) and to git. Here the queue is a small script that answers
from a file and writes down what it was asked, and git is a throwaway repository with a bare
"origin" beside it. What is checked is everything that must hold whatever the session does: a
plan too long for a phone is refused before it reaches the queue, and a change is pushed and
reported ready only after the scope check, the contact-data check, the test check and every
offline test, and is refused, with the step, when any of them says no.

    python3 fixer/tests/run_fixer.py
Exits non-zero on any failed check. Every name and number here is made up.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


TMP = Path(tempfile.mkdtemp(prefix="fixer-test-")).resolve()
STATE, REPO, ORIGIN = TMP / "state", TMP / "repo", TMP / "origin.git"
FAKE_SSH, FAKE_SQL = TMP / "ssh", TMP / "lisa-log-sql"
QUEUE, QCALLS = TMP / "queue.json", TMP / "queue-calls.jsonl"
DCALLS = TMP / "deploy-calls.jsonl"

FAKE_SSH.write_text(f'''#!{sys.executable}
import json, sys
q = json.load(open({str(QUEUE)!r}))
if "bash" in sys.argv and "--" in sys.argv:          # one of fixer/remote/*.sh
    args = sys.argv[sys.argv.index("--") + 1:]
    sys.stdin.read()
    with open({str(DCALLS)!r}, "a") as f:
        f.write(json.dumps(args) + "\\n")
    if args[2] == "-":
        print(json.dumps({{"ok": "true", "pulled": args[0][:7]}}))
    else:
        print(json.dumps(q.get("deploy", {{"ok": "true", "rollback_tag": args[1], "swap": "10:00:00-10:00:20 UTC"}})))
    sys.exit(0)
remote = sys.argv[-1]
args = remote.split("app.fixdesk.cli", 1)[1].split()
body = sys.stdin.read()
with open({str(QCALLS)!r}, "a") as f:
    f.write(json.dumps({{"args": args, "body": json.loads(body or "{{}}")}}) + "\\n")
print("a line of noise that is not the answer")
print(json.dumps(q.get(args[0], {{"ok": True}})))
''')
FAKE_SQL.write_text(f'''#!{sys.executable}
import json
print(json.dumps({{"transcript": [{{"seq": 1, "ts": "2026-11-24T14:00:00", "who": "Olindina Peçanha", "text": "from the log"}}],
                  "reviews": [], "findings": [], "loop": {{"loop_id": "loop-7"}}}}))
''')
for f in (FAKE_SSH, FAKE_SQL):
    f.chmod(f.stat().st_mode | stat.S_IEXEC)

os.environ.update(FIXER_STATE=str(STATE), FIXER_REPO=str(REPO), FIXER_SSH_CMD=str(FAKE_SSH),
                  FIXER_LIVE_CHECKS="0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run as fx  # noqa: E402

fx.VENV_PY = Path(sys.executable)
fx.LOG_SQL = FAKE_SQL
DAY, NIGHT = True, False


def g(*args: str, cwd: Path = REPO) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo() -> None:
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(ORIGIN)], check=True)
    REPO.mkdir()
    g("init", "-q", "-b", "main")
    g("config", "user.email", "test@example.invalid")
    g("config", "user.name", "Test")
    for path, text in {
        "Lisa flow/app/thing.py": "VALUE = 1\n",
        "Lisa flow/app/review/prompt.py": "JUDGE = 1\n",
        "Lisa flow/tests/run_thing.py":
            "import sys\nsys.path.insert(0, '.')\nfrom app.thing import VALUE\n"
            "ok = VALUE in (1, 2)\nprint('1 passed, 0 failed' if ok else '0 passed, 1 failed')\n"
            "sys.exit(0 if ok else 1)\n",
        "Lisa flow/tests/live_dates.py": "print('[PASS] x')\n",
        "Lisa flow/tests/live_busy.py": "print('[PASS] x')\n",
        "dispatcher/app.py": "X = 1\n",
        "dispatcher/tests/run_d.py": "print('1 passed, 0 failed')\n",
        "evolution/docker-compose.yml":
            "services:\n  lisa:\n    environment:\n      PROMPT_VERSION: \"2026-11-27-timezone-card\"\n"
            "      REVIEW_SCOPE_VERSION: \"2026-11-27-timezone-card\"\n      REVIEW_JUDGE_VERSION: \"v6\"\n",
        "README.md": "repo\n",
    }.items():
        (REPO / path).parent.mkdir(parents=True, exist_ok=True)
        (REPO / path).write_text(text)
    g("add", "-A")
    g("commit", "-q", "-m", "start")
    g("remote", "add", "origin", str(ORIGIN))
    g("push", "-q", "origin", "main")
    g("fetch", "-q", "origin")


def queue_says(**answers: dict) -> None:
    QUEUE.write_text(json.dumps(answers))
    QCALLS.write_text("")
    DCALLS.write_text("")


def deploys() -> list[list[str]]:
    return [json.loads(ln) for ln in DCALLS.read_text().splitlines() if ln.strip()]


def main_file(path: str) -> str:
    return g("show", f"main:{path}", cwd=ORIGIN)


def asked() -> list[dict]:
    return [json.loads(ln) for ln in QCALLS.read_text().splitlines() if ln.strip()]


def verbs() -> list[str]:
    return [c["args"][0] for c in asked()]


def run(*argv: str, daytime: bool = DAY) -> tuple[int, dict]:
    """One command, as the session would run it: exit code and the JSON it printed."""
    fx.fix_hours = lambda now: daytime
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fx.main(list(argv))
    return rc, json.loads(buf.getvalue())


PLAN = {"kind": "bug", "judge_right": True, "confidence": "high",
        "issue": "Tuesday, in a chat with a supplier, the card showed one hour instead of ninety minutes.",
        "diagnosis": "The end time was in a voice note and the booking step only reads typed text.",
        "fix": "Read the end time from every message of the request before using the default.",
        "gates": ["live_busy"]}
EVIDENCE = {"transcript": [
    {"seq": 1, "ts": "2026-11-24T14:00:00", "who": "Marcelo", "text": "@lisa marca quinta 15h"},
    {"seq": 2, "ts": "2026-11-24T14:00:05", "who": "Zuleide Quaresma (voice message — transcribed)", "text": "até 16h30"},
    {"seq": 3, "ts": "2026-11-24T14:00:09", "who": "AI Assistant", "text": "Marquei 15h–16h."}],
    "reviews": [], "findings": [], "loop": {"loop_id": "loop-7"}}
NEW = {"id": 7, "loop_id": "loop-7", "codes": ["wrong_details"], "judge_version": "v5", "evidence": EVIDENCE}
RECENT = [{"id": 3, "status": "proposed", "issue": "an older one"}, {"id": 7, "status": "new"}]


def approved(case_id: int, **kw) -> dict:
    return {"id": case_id, "loop_id": f"loop-{case_id}", "judge_version": "v5", "plan": PLAN,
            "approval_note": "only in groups", **kw}


def unit_checks() -> None:
    print("plans")
    ok = fx.check_plan
    check("a full bug plan passes", ok(PLAN, set()) is None)
    check("a short paragraph is refused", ok({**PLAN, "fix": "do it"}, set()) == "fix is missing or too short")
    long = ok({**PLAN, "diagnosis": "word " * 80}, set())
    check("a paragraph too long for a phone is refused, with its length", long and "399 characters; the limit is 300" in long, str(long))
    check("an unknown kind is refused", "kind must be one of" in ok({**PLAN, "kind": "poem"}, set()))
    check("a duplicate must name a recent case", ok({"kind": "duplicate", "duplicate_of": 3}, {1, 2}) is not None
          and ok({"kind": "duplicate", "duplicate_of": 2}, {1, 2}) is None)
    check("already_fixed must name the commit", ok({"kind": "already_fixed"}, set()) is not None
          and ok({"kind": "already_fixed", "fixed_by": "a3f58e0"}, set()) is None)
    check("no_action needs no paragraphs", ok({"kind": "no_action"}, set()) is None)

    print("\nwhat a change may touch")
    check("Lisa's code, her tests, the dispatcher",
          fx.outside_scope(["Lisa flow/app/x.py", "Lisa flow/tests/run_x.py", "dispatcher/app.py",
                            "dispatcher/tests/run_y.py"]) == [])
    stray = ["evolution/docker-compose.yml", "fixer/run.py", ".claude/scripts/lisa-log-sql", "Lisa flow/Dockerfile",
             ".github/workflows/x.yml", "dispatcher/Dockerfile", ".fixer/evidence.json"]
    check("not the compose file, the tool itself, the scripts, a Dockerfile, CI or the evidence",
          fx.outside_scope(stray) == stray)

    print("\ncontact data")
    cd = fx.contact_data
    check("a real-looking WhatsApp id is caught", cd('JID = "5531987654321@s.whatsapp.net"', []) != [])
    check("a group id is caught", cd("120363298765432101@g.us", []) != [])
    check("a formatted Brazilian number is caught", cd("call +55 31 98765-4321 now", []) != [])
    check("the made-up test ids pass", cd('A = "5511900000001@s.whatsapp.net"; B = "5511999990000@s.whatsapp.net"', []) == [])
    check("a name from the chat is caught", cd('who = "Zuleide"', ["Zuleide", "Quaresma"]) != [])
    check("ordinary code passes", cd("def end_time(messages):\n    return messages[-1]", ["Zuleide"]) == [])
    check("names come from the other speakers only", fx.chat_names(EVIDENCE) == ["Quaresma", "Zuleide"],
          str(fx.chat_names(EVIDENCE)))

    print("\nlive checks")
    check("verdict lines are counted", fx.parse_gate("[PASS] a\n  [PASS] b\n[FAIL] c\nnoise [PASS]") == (2, 1))
    pick = fx.pick_gates
    check("a change to her reply path gets the plan's checks, and the date check always",
          pick({"gates": ["live_nope", "live_busy"]}, REPO, ["Lisa flow/app/thing.py"]) == ["live_busy.py", "live_dates.py"])
    check("a change to the judge alone gets no date check: the judge is not on the reply path",
          pick({"gates": []}, REPO, ["Lisa flow/app/review/prompt.py", "Lisa flow/tests/run_review.py"]) == [])
    check("a dispatcher-only change gets none", pick(PLAN, REPO, ["dispatcher/app.py"]) == [])
    at = lambda h: datetime(2026, 11, 28, h, 30)  # noqa: E731
    check("fixes run in the daytime only: the live date check misreads the small hours",
          [fx.fix_hours(at(h)) for h in (1, 5, 6, 12, 21, 22, 23)] == [False, False, True, True, True, False, False])

    print("\nthe queue")
    queue_says(pull={"ok": True, "enabled": True, "new": [], "approved": [], "recent": []})
    out = fx.pull_queue()
    check("the answer is the last JSON line, past any noise", out["enabled"] is True)
    c = asked()[0]
    check("every pull says this Mac is here, running in the desktop app",
          c["args"][0] == "pull" and c["body"]["ok"] is True and c["body"]["runner"] == "desktop", str(c))


def pull_checks() -> None:
    print("\npull")
    queue_says(pull={"ok": True, "enabled": False, "new": [NEW], "approved": [approved(4)], "recent": []})
    rc, out = run("pull")
    check("switched off on the droplet: nothing is offered", rc == 0 and out["approved"] == [] and out["new"] == []
          and "switched off" in out["note"], str(out))
    many = [dict(NEW, id=i) for i in range(10, 20)]
    queue_says(pull={"ok": True, "enabled": True, "new": many, "recent": [],
                     "approved": [approved(i) for i in range(1, 6)]})
    rc, out = run("pull")
    check("at most 3 fixes and 5 investigations a run",
          [c["id"] for c in out["approved"]] == [1, 2, 3] and [c["id"] for c in out["new"]] == [10, 11, 12, 13, 14], str(out))
    check("an approved case comes with its issue and his note",
          out["approved"][0] == {"id": 1, "issue": PLAN["issue"], "note": "only in groups"}, str(out["approved"][0]))
    rc, out = run("pull", daytime=NIGHT)
    check("at night the fixes are held back, with the reason, and investigations are not",
          out["approved"] == [] and len(out["new"]) == 5 and "wait for daytime" in out["note"], str(out))

    queue_says(pull={"ok": True, "enabled": True, "new": [], "approved": [], "recent": []},
               release={"ok": True, "status": "new"})
    fx._set_claim(41, "investigating")
    fx._set_claim(42, "fixing")
    claims = fx._claims()
    claims["41"]["at"] = time.time() - 7 * 3600
    (STATE / "claims.json").write_text(json.dumps(claims))
    rc, out = run("pull")
    rel = [c for c in asked() if c["args"][0] == "release"]
    check("a case taken seven hours ago and never handed in is put back; a fresh one is left",
          out.get("put_back") == [41] and [c["args"][1] for c in rel] == ["41"] and list(fx._claims()) == ["42"], str(out))
    fx._set_claim(42, None)


def investigate_checks() -> None:
    print("\ninvestigate, then plan")
    queue_says(pull={"ok": True, "enabled": True, "new": [NEW], "approved": [], "recent": RECENT},
               claim={"ok": True}, plan={"ok": True, "status": "planned"})
    rc, out = run("investigate", "7")
    wt = Path(out["checkout"])
    check("the case is taken", rc == 0 and verbs() == ["pull", "claim"] and asked()[1]["args"] == ["claim", "7", "investigating"])
    check("it gets a checkout of its own, at production's code",
          wt == fx.worktree(7) and (wt / "Lisa flow/app/thing.py").read_text() == "VALUE = 1\n")
    check("the evidence is laid out inside it",
          "Zuleide" in (wt / ".fixer/transcript.txt").read_text() and (wt / ".fixer/evidence.json").exists())
    recent = json.loads((wt / ".fixer/recent-cases.json").read_text())
    check("... with the recent cases, without itself", [r["id"] for r in recent] == [3])
    check("git does not see the evidence", g("status", "--porcelain", "--untracked-files=all", cwd=wt) == "")
    check("a copy is kept outside the repository", (STATE / "cases/7/transcript.txt").exists())
    check("it is told where to write the plan and how to read the log",
          out["write_plan_to"] == str(wt / ".fixer/plan.json") and "loop-7" in out["log_query"])
    check("the case is remembered as taken", fx._claims()["7"]["phase"] == "investigating")

    rc, out = run("plan", "7")
    check("no plan file: refused, and the queue is not asked", rc == 1 and "cannot read" in out["error"] and "plan" not in verbs()[2:])
    (wt / ".fixer/plan.json").write_text(json.dumps({**PLAN, "issue": "word " * 90}))
    QCALLS.write_text("")
    rc, out = run("plan", "7")
    check("a paragraph too long for his phone is refused before it reaches the queue",
          rc == 1 and "limit is 300" in out["error"] and verbs() == [], str(out))
    (wt / ".fixer/plan.json").write_text(json.dumps({**PLAN, "kind": "duplicate", "duplicate_of": 99}))
    rc, out = run("plan", "7")
    check("a duplicate of a case that is not in the list is refused", rc == 1 and "duplicate_of" in out["error"])
    (wt / ".fixer/plan.json").write_text(json.dumps(PLAN))
    rc, out = run("plan", "7")
    body = asked()[-1]
    check("a good plan is handed in", rc == 0 and out["status"] == "planned" and body["args"] == ["plan", "7"]
          and body["body"]["plan"] == PLAN, str(out))
    check("... and he will be sent it", out["note"] == "Lisa will send him this plan.")
    check("the checkout is removed and the case no longer counts as taken", not wt.exists() and "7" not in fx._claims())
    check("the plan is kept on the Mac", json.loads((STATE / "cases/7/plan.json").read_text()) == PLAN)

    queue_says(pull={"ok": True, "enabled": True, "new": [dict(NEW, id=8)], "approved": [], "recent": RECENT},
               claim={"ok": True}, plan={"ok": True, "status": "duplicate"})
    run("investigate", "8")
    (fx.worktree(8) / ".fixer/plan.json").write_text(json.dumps({"kind": "duplicate", "duplicate_of": 3}))
    rc, out = run("plan", "8")
    check("a repeat closes without a message", rc == 0 and out["note"] == "Closed; nothing is sent to him.", str(out))

    queue_says(pull={"ok": True, "enabled": True, "new": [], "approved": [], "recent": []})
    rc, out = run("investigate", "9")
    check("a case that is not in the queue is refused", rc == 1 and "not a new case" in out["error"] and verbs() == ["pull"])
    queue_says(pull={"ok": True, "enabled": True, "new": [dict(NEW, id=9)], "approved": [], "recent": []},
               claim={"ok": False})
    rc, out = run("investigate", "9")
    check("a case something else holds is left alone", rc == 1 and not fx.worktree(9).exists() and "9" not in fx._claims())


def fix_checks() -> None:
    print("\nfix, then ship")

    def take(case_id: int, **more: dict) -> Path:
        queue_says(pull={"ok": True, "enabled": True, "new": [], "approved": [approved(case_id)], "recent": []},
                   claim={"ok": True}, result={"ok": True}, release={"ok": True, "status": "approved"}, **more)
        rc, out = run("fix", str(case_id))
        assert rc == 0, out
        QCALLS.write_text("")
        return Path(out["checkout"])

    def good_change(wt: Path, value: int = 2, test: str = "run_new.py") -> None:
        (wt / "Lisa flow/app/thing.py").write_text(f"VALUE = {value}\n")
        (wt / "Lisa flow/tests" / test).write_text('print("1 passed, 0 failed")\n')

    def ship(case_id: int, *extra: str) -> tuple[int, dict]:
        return run("ship", str(case_id), "--summary", "She reads the end time from voice notes too.",
                   "--review", "It does what the plan says and nothing else.", *extra)

    queue_says(pull={"ok": True, "enabled": True, "new": [], "approved": [approved(7)], "recent": []}, claim={"ok": True})
    rc, out = run("fix", "7", daytime=NIGHT)
    check("at night a fix is not started, and the case is not taken", rc == 1 and out.get("wait") is True and verbs() == [])
    rc, out = run("fix", "7")
    wt = Path(out["checkout"])
    check("the case is taken to be fixed", rc == 0 and asked()[1]["args"] == ["claim", "7", "fixing"])
    check("on a branch of its own, at production's code",
          out["branch"] == "fix/case-7" and g("rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == "fix/case-7")
    check("with the plan he approved, his note, and what it may change",
          out["plan"] == PLAN and out["his_note"] == "only in groups" and "Lisa flow/app/" in out["may_change"])
    check("the evidence kept from the investigation is laid out again",
          "Zuleide" in (wt / ".fixer/transcript.txt").read_text() and (wt / ".fixer/approved-plan.json").exists())
    QCALLS.write_text("")

    rc, out = ship(7)
    check("nothing changed: refused", rc == 1 and out["step"] == "the change", str(out))
    (wt / "evolution/extra-compose.yml").write_text("x\n")
    (wt / "Lisa flow/app/thing.py").write_text("VALUE = 2\n")
    rc, out = ship(7)
    check("a file outside Lisa and the dispatcher: refused at the scope check",
          rc == 1 and out["step"] == "the scope check" and "extra-compose" in out["detail"], str(out))
    (wt / "evolution/extra-compose.yml").unlink()
    rc, out = ship(7)
    check("no test changed: refused at the test check", rc == 1 and out["step"] == "the test check", str(out))
    (wt / "Lisa flow/tests/run_new.py").write_text('JID = "5531987654321@s.whatsapp.net"\nprint("1 passed, 0 failed")\n')
    rc, out = ship(7)
    check("a real-looking number: refused at the contact-data check", rc == 1 and out["step"] == "the contact-data check", str(out))
    (wt / "Lisa flow/tests/run_new.py").write_text('WHO = "Zuleide"\nprint("1 passed, 0 failed")\n')
    rc, out = ship(7)
    check("a name from the chat: refused there too", rc == 1 and out["step"] == "the contact-data check", str(out))
    (wt / "Lisa flow/tests/run_new.py").write_text('print("1 passed, 0 failed")\n')
    (wt / "Lisa flow/app/thing.py").write_text("VALUE = 3\n")
    rc, out = ship(7)
    check("a broken test: refused at the offline tests, naming the file",
          rc == 1 and out["step"] == "the offline tests" and "run_thing.py" in out["detail"], str(out))
    check("every refusal says what to do next", "run ship again" in out["next"])
    check("none of the refusals reached the queue, GitHub or the server",
          verbs() == [] and g("branch", "--list", "fix/*", cwd=ORIGIN) == "" and deploys() == [])
    check("... and the case still counts as taken", fx._claims()["7"]["phase"] == "fixing")

    rc, out = run("ship", "7", "--summary", "short", "--review", "It does what the plan says.")
    check("a summary that is not a sentence is refused", rc == 1 and "--summary" in out["error"])
    rc, out = run("ship", "7", "--summary", "She reads the end time from voice notes too.", "--review", "ok")
    check("no second reader's verdict: refused", rc == 1 and "--review" in out["error"])

    (wt / "Lisa flow/app/thing.py").write_text("VALUE = 2\n")
    rc, out = ship(7, "--prompt-changed")
    res = asked()[-1]
    body = res["body"]
    today = datetime.now().strftime("%Y-%m-%d")
    check("corrected, it goes live", rc == 0 and out["status"] == "shipped" and res["args"] == ["result", "7"], str(out))
    check("the branch is on origin with the change", g("show", "fix/case-7:Lisa flow/app/thing.py", cwd=ORIGIN) == "VALUE = 2")
    merge7 = g("log", "--format=%H", "--grep=Merge fix/case-7: She reads the end time", "main", cwd=ORIGIN)
    check("main has it, as a merge that names the branch",
          main_file("Lisa flow/app/thing.py") == "VALUE = 2" and len(g("log", "-1", "--format=%P", merge7, cwd=ORIGIN).split()) == 2)
    check("the report carries that merge, which is what an undo reverts", body["commit"] == merge7)
    check("the version lines went in a commit of their own, on top",
          g("log", "-1", "--format=%s", "main", cwd=ORIGIN) == "chore(lisa): versions for fix-case-7"
          and g("diff", "--name-only", "main~1", "main", cwd=ORIGIN) == "evolution/docker-compose.yml"
          and "docker-compose" not in g("diff", "--name-only", f"{merge7}~1", merge7, cwd=ORIGIN))
    d = deploys()
    check("the server was asked to deploy main's head, Lisa only, under a rollback tag",
          len(d) == 1 and d[0][:3] == [g("rev-parse", "main", cwd=ORIGIN), "pre-fix-case-7", "lisa"]
          and body["deployed"] == d[0][0], str(d))
    check("her prompt changed, so her prompt version moves, and the judge's does not",
          d[0][3:5] == [f"{today}-fix-case-7", "-"] and body["bumped"] == {"prompt": f"{today}-fix-case-7", "judge": None}, str(d))
    compose = main_file("evolution/docker-compose.yml")
    check("... and the repo's compose file says the same",
          f'PROMPT_VERSION: "{today}-fix-case-7"' in compose and f'REVIEW_SCOPE_VERSION: "{today}-fix-case-7"' in compose
          and 'REVIEW_JUDGE_VERSION: "v6"' in compose, compose)
    check("what changed, in words",
          body["summary"] == "She reads the end time from voice notes too. 2 files changed.", body["summary"])
    check("what was checked, and how to take it back",
          body["checks"] == "Offline tests pass (3 files); live checks are switched off. To take it back, reply undo F7.",
          body["checks"])
    check("the rollback tag is kept in the report", body["rollback_tag"] == "pre-fix-case-7" and body["services"] == ["lisa"])
    msg = g("log", "-1", "--format=%B", "fix/case-7", cwd=ORIGIN)
    check("the fix's own commit says what and which case", msg.startswith("fix(lisa): She reads the end time") and "F7" in msg, msg)
    check("the evidence did not go with it", ".fixer" not in g("ls-tree", "-r", "--name-only", "main", cwd=ORIGIN))
    check("the case no longer counts as taken", "7" not in fx._claims())

    print("\nwhat a deploy restarts, and which versions move")
    wt = take(21)
    (wt / "Lisa flow/app/review/prompt.py").write_text("JUDGE = 2\n")
    (wt / "Lisa flow/tests/run_j.py").write_text('print("1 passed, 0 failed")\n')
    rc, out = ship(21)
    d = deploys()
    check("a change to the judge's rubric moves the judge's version, v6 to v7, and not her prompt's",
          rc == 0 and d[0][2:5] == ["lisa", "-", "v7"] and 'REVIEW_JUDGE_VERSION: "v7"' in main_file("evolution/docker-compose.yml"), str(d))
    wt = take(22)
    (wt / "dispatcher/app.py").write_text("X = 2\n")
    (wt / "dispatcher/tests/run_d2.py").write_text('print("1 passed, 0 failed")\n')
    rc, out = ship(22)
    check("a dispatcher change restarts the dispatcher only, and moves no version",
          rc == 0 and deploys()[0][2:5] == ["dispatcher", "-", "-"], str(deploys()))
    wt = take(23)
    (wt / "Lisa flow/tests/run_only.py").write_text('print("1 passed, 0 failed")\n')
    rc, out = ship(23)
    body = asked()[-1]["body"]
    check("a tests-only change restarts nothing: the server only pulls",
          rc == 0 and deploys()[0][2] == "-" and body["rollback_tag"] is None and "nothing had to restart" in body["checks"],
          str(deploys()))

    print("\nwhen the deploy fails")
    before = g("rev-parse", "main", cwd=ORIGIN)
    wt = take(24, deploy={"ok": "false", "step": "the deploy", "detail": "lisa did not come up on the new image",
                          "rolled_back": "true", "swap": "10:00:00-10:01:10 UTC"})
    good_change(wt, value=1, test="run_f.py")
    (wt / "Lisa flow/app/extra.py").write_text("Y = 1\n")
    rc, out = ship(24)
    res = asked()[-1]
    check("it is reported as failed, at the deploy, saying production and main were put back",
          rc == 1 and out.get("final") is True and res["args"] == ["result", "24"] and res["body"]["status"] == "failed"
          and res["body"]["failed_step"] == "the deploy"
          and "Production was put back on the previous image. Main was put back too." in res["body"]["detail"], str(res))
    check("main no longer has the change, through a revert on top",
          "extra.py" not in g("ls-tree", "-r", "--name-only", "main", cwd=ORIGIN)
          and g("log", "-1", "--format=%s", "main", cwd=ORIGIN).startswith("Revert")
          and g("rev-parse", "main~2", cwd=ORIGIN) == before)
    d = deploys()
    check("the server was told to follow main back", len(d) == 2 and d[1][0] == g("rev-parse", "main", cwd=ORIGIN) and d[1][2] == "-", str(d))
    check("the session is told not to hand it in again", "Do not hand it in again" in out["next"] and "24" not in fx._claims())

    wt = take(25, deploy={"ok": "false", "step": "the image check", "detail": "run_step3.py fails inside Lisa's new image",
                          "rolled_back": "false"})
    good_change(wt, value=1, test="run_g.py")
    (wt / "Lisa flow/app/extra2.py").write_text("Y = 1\n")
    rc, out = ship(25)
    check("stopped before the swap: it says production was not touched",
          "Production was not touched. Main was put back too." in asked()[-1]["body"]["detail"], str(asked()[-1]))
    wt = take(26, deploy={"ok": "false", "step": "the deploy", "detail": "lisa did not start", "rolled_back": "false",
                          "swap": "10:00:00-10:02:00 UTC"})
    good_change(wt, value=1, test="run_h.py")
    (wt / "Lisa flow/app/extra3.py").write_text("Y = 1\n")
    rc, out = ship(26)
    check("a rollback that also failed is said in so many words",
          "Production may be down or half-swapped: it needs a look now." in asked()[-1]["body"]["detail"], str(asked()[-1]))

    print("\nwhen main moved under the fix")
    wt = take(27)
    (wt / "Lisa flow/app/thing.py").write_text("VALUE = 2  # the fix\n")
    (wt / "Lisa flow/tests/run_i.py").write_text('print("1 passed, 0 failed")\n')
    other = TMP / "other"
    subprocess.run(["git", "clone", "-q", str(ORIGIN), str(other)], check=True)
    g("config", "user.email", "t@example.invalid", cwd=other); g("config", "user.name", "T", cwd=other)
    (other / "Lisa flow/app/thing.py").write_text("VALUE = 2  # someone else\n")
    g("commit", "-qam", "someone else", cwd=other); g("push", "-q", "origin", "main", cwd=other)
    head = g("rev-parse", "main", cwd=ORIGIN)
    rc, out = ship(27)
    check("a fix that no longer fits today's main is refused at the merge, and nothing moves",
          rc == 1 and out["step"] == "the merge" and not out.get("final") and g("rev-parse", "main", cwd=ORIGIN) == head
          and deploys() == [] and verbs() == [], str(out))
    check("the checkout is back on the fix's branch", g("rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == "fix/case-27")

    print("\nwith deploying switched off")
    fx.CFG["FIXER_DEPLOY"] = "0"
    head = g("rev-parse", "main", cwd=ORIGIN)
    wt = take(28)
    (wt / "Lisa flow/app/more.py").write_text("Z = 1\n")
    (wt / "Lisa flow/tests/run_k.py").write_text('print("1 passed, 0 failed")\n')
    rc, out = ship(28, "--prompt-changed")
    body = asked()[-1]["body"]
    check("it stops at a tested, pushed branch: main and the server are untouched",
          rc == 0 and body["status"] == "ready" and g("rev-parse", "main", cwd=ORIGIN) == head and deploys() == []
          and g("show", "fix/case-28:Lisa flow/app/more.py", cwd=ORIGIN) == "Z = 1", str(body))
    check("... and says the prompt version still has to move", "bump the prompt version when it ships" in body["checks"])
    fx.CFG["FIXER_DEPLOY"] = "1"

    print("\nundo")
    shipped = lambda cid, commit, **kw: {"id": cid, "status": "undo_requested", "plan": PLAN,  # noqa: E731
                                         "result": {"status": "shipped", "commit": commit, "services": ["lisa"], **kw}}
    merge = lambda cid: g("log", "--format=%H", f"--grep=Merge fix/case-{cid}:", "main", cwd=ORIGIN)  # noqa: E731
    queue_says(pull={"ok": True, "enabled": True, "new": [], "approved": [], "recent": [], "undo": [
        shipped(21, merge(21))]}, undone={"ok": True, "status": "undone"})
    rc, out = run("pull")
    check("pull lists what is waiting to be taken back", out["undo"] == [{"id": 21, "issue": PLAN["issue"]}], str(out))
    QCALLS.write_text("")
    m21 = merge(21)
    rc, out = run("undo", "21")
    res = asked()[-1]
    check("the fix's merge is reverted on main: the judge's file is as it was",
          rc == 0 and main_file("Lisa flow/app/review/prompt.py") == "JUDGE = 1", str(out))
    check("... by a revert that names the merge, and other fixes shipped since are untouched",
          m21 in g("log", "-3", "--format=%B", "main", cwd=ORIGIN) and main_file("dispatcher/app.py") == "X = 2")
    d = deploys()
    check("the revert is deployed under its own rollback tag, and the judge's version moves on, v7 to v8",
          len(d) == 1 and d[0] == [g("rev-parse", "main", cwd=ORIGIN), "pre-undo-case-21", "lisa", "-", "v8"], str(d))
    check("... which the repo's compose file carries too",
          'REVIEW_JUDGE_VERSION: "v8"' in main_file("evolution/docker-compose.yml"))
    check("the queue is told it is out of production", res["args"] == ["undone", "21"] and res["body"]["ok"] is True, str(res))
    check("the checkout is cleaned up", not fx.worktree(21).exists())
    QCALLS.write_text(""); DCALLS.write_text("")
    rc, out = run("undo", "21")
    check("asked twice, it sees the revert is already on main and deploys nothing more",
          rc == 0 and asked()[-1]["body"]["ok"] is True and deploys() == [], str(out))

    queue_says(pull={"ok": True, "enabled": True, "new": [], "approved": [], "recent": [], "undo": [
        shipped(7, merge7, bumped={"prompt": "x", "judge": None})]}, undone={"ok": True, "status": "shipped"})
    head = g("rev-parse", "main", cwd=ORIGIN)
    rc, out = run("undo", "7")
    body = asked()[-1]["body"]
    check("a fix whose lines were changed again since cannot be reverted cleanly: it says so and touches nothing",
          rc == 1 and body["ok"] is False and "Later changes touch the same lines" in body["detail"]
          and g("rev-parse", "main", cwd=ORIGIN) == head and deploys() == [], str(body))

    queue_says(pull={"ok": True, "enabled": True, "new": [], "approved": [], "recent": [], "undo": [
        dict(shipped(22, merge(22).splitlines()[-1]), result={"status": "shipped", "commit": merge(22).splitlines()[-1], "services": ["dispatcher"]})]},
               undone={"ok": True, "status": "shipped"},
               deploy={"ok": "false", "step": "the deploy", "detail": "dispatcher did not come up", "rolled_back": "true", "swap": "x"})
    rc, out = run("undo", "22")
    body = asked()[-1]["body"]
    check("an undo that does not deploy is reported as still live, and main is put back to match",
          rc == 1 and body["ok"] is False and "The undo did not deploy" in body["detail"] and "Main was put back to match." in body["detail"]
          and main_file("dispatcher/app.py") == "X = 2", str(body))
    check("... and the server was told to follow main back", deploys()[-1][2] == "-" and deploys()[-1][0] == g("rev-parse", "main", cwd=ORIGIN))

    queue_says(pull={"ok": True, "enabled": True, "new": [], "approved": [], "recent": [], "undo": [
        {"id": 2, "status": "undo_requested", "plan": PLAN, "result": {"status": "ready", "commit": "abc"}}]},
        undone={"ok": True, "status": "shipped"})
    rc, out = run("undo", "2")
    check("a fix the loop never deployed cannot be taken back by it, and says so",
          rc == 1 and "not deployed by the loop" in asked()[-1]["body"]["detail"] and deploys() == [], str(out))
    rc, out = run("undo", "99")
    check("a case that is not waiting to be taken back is refused", rc == 1 and "not waiting" in out["error"])

    print("\nwithout a copy of the evidence on the Mac")
    wt = take(12)
    check("it is read from the log", "from the log" in (wt / ".fixer/transcript.txt").read_text())
    (wt / "Lisa flow/tests/run_new.py").write_text('WHO = "Olindina"\nprint("1 passed, 0 failed")\n')
    rc, out = ship(12)
    check("... and its names are still kept out of commits", rc == 1 and out["step"] == "the contact-data check", str(out))

    print("\ngiving a case up, putting one back")
    rc, out = run("fail", "12", "--step", "the change", "--detail", "The cause is elsewhere: the card is right.")
    res = asked()[-1]
    check("fail reports it, with the step and the reason",
          rc == 0 and res["args"] == ["result", "12"] and res["body"] == {
              "status": "failed", "failed_step": "the change", "detail": "The cause is elsewhere: the card is right."}, str(res))
    check("... and the case no longer counts as taken", "12" not in fx._claims())
    take(13)
    rc, out = run("release", "13", "--error", "the server could not be reached")
    check("release puts it back for the next run", rc == 0 and out["status"] == "approved"
          and asked()[-1]["args"] == ["release", "13"] and "13" not in fx._claims())
    rc, out = ship(14)
    check("shipping a case that was never taken is refused", rc == 1 and out["step"] == "the checkout", str(out))
    queue_says(pull={"ok": True, "enabled": True, "new": [], "approved": [], "recent": []})
    rc, out = run("fix", "15")
    check("a case that is not approved cannot be fixed", rc == 1 and "not an approved case" in out["error"])


def main() -> int:
    make_repo()
    unit_checks()
    pull_checks()
    investigate_checks()
    fix_checks()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
