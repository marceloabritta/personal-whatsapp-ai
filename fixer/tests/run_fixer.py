"""The daily worker, driven end to end with nothing real behind it.

`fixer/run.py` talks to three things: the queue on the droplet (over ssh), Claude Code, and git.
Here the first two are small scripts that answer from files and write down what they were asked,
and git is a throwaway repository with a bare "origin" beside it. So this checks what the worker
does around the model: that Claude Code never starts with an API key in reach, that a run on the
wrong credential is stopped, that an investigation's plan reaches the queue, and that a fix is
only reported ready after the scope check, the contact-data check, the tests, the second read
and the push, and is reported failed, with the step, when any of them says no.

    python3 fixer/tests/run_fixer.py
Exits non-zero on any failed check. Every name and number here is made up.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

_checks = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    _checks["pass" if cond else "fail"] += 1


TMP = Path(tempfile.mkdtemp(prefix="fixer-test-"))
STATE, REPO, ORIGIN = TMP / "state", TMP / "repo", TMP / "origin.git"
FAKE_CLAUDE, FAKE_SSH = TMP / "claude", TMP / "ssh"
SCRIPT, CALLS, QUEUE = TMP / "claude-script.json", TMP / "claude-calls.jsonl", TMP / "queue.json"
QCALLS = TMP / "queue-calls.jsonl"

FAKE_CLAUDE.write_text(f'''#!{sys.executable}
import json, os, sys, time
args = sys.argv[1:]
script = json.load(open({str(SCRIPT)!r}))
if args[:2] == ["auth", "status"]:
    print(json.dumps(script.get("auth", {{"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}})))
    sys.exit(0)
if "--version" in args:
    print("9.9.9 (stand-in)"); sys.exit(0)
prompt = sys.stdin.read()
step = script["runs"].pop(0)
json.dump(script, open({str(SCRIPT)!r}, "w"))
with open({str(CALLS)!r}, "a") as f:
    f.write(json.dumps({{"args": args, "cwd": os.getcwd(), "prompt": prompt[:200],
                        "env": sorted(k for k in os.environ if k.startswith(("ANTHROPIC", "CLAUDE")))}}) + "\\n")
print(json.dumps({{"type": "system", "subtype": "init", "apiKeySource": step.get("key_source", "none")}}), flush=True)
time.sleep(step.get("sleep", 0))
for path, content in (step.get("edits") or {{}}).items():
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    open(path, "w").write(content)
if step.get("no_result"):
    sys.exit(1)
print(json.dumps({{"type": "result", "is_error": bool(step.get("is_error")), "result": step.get("text", ""),
                  "structured_output": step.get("structured"), "num_turns": 3}}), flush=True)
''')
FAKE_SSH.write_text(f'''#!{sys.executable}
import json, sys
remote = sys.argv[-1]
args = remote.split("app.fixdesk.cli", 1)[1].split()
body = sys.stdin.read()
with open({str(QCALLS)!r}, "a") as f:
    f.write(json.dumps({{"args": args, "body": json.loads(body or "{{}}")}}) + "\\n")
q = json.load(open({str(QUEUE)!r}))
print("a line of noise that is not the answer")
print(json.dumps(q.get(args[0], {{"ok": True}})))
''')
for f in (FAKE_CLAUDE, FAKE_SSH):
    f.chmod(f.stat().st_mode | stat.S_IEXEC)

os.environ.update(FIXER_STATE=str(STATE), FIXER_REPO=str(REPO), FIXER_SSH_CMD=str(FAKE_SSH),
                  FIXER_CLAUDE=str(FAKE_CLAUDE), FIXER_LIVE_CHECKS="0", FIXER_MODEL="",
                  ANTHROPIC_API_KEY="sk-must-never-reach-claude", ANTHROPIC_BASE_URL="http://nope",
                  CLAUDE_CODE_SOMETHING="from a parent session")
os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run as fx  # noqa: E402


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
        "Lisa flow/tests/run_thing.py":
            "import sys\nsys.path.insert(0, '.')\nfrom app.thing import VALUE\n"
            "ok = VALUE in (1, 2)\nprint('1 passed, 0 failed' if ok else '0 passed, 1 failed')\n"
            "sys.exit(0 if ok else 1)\n",
        "Lisa flow/tests/live_dates.py": "print('[PASS] x')\n",
        "dispatcher/app.py": "X = 1\n",
        "dispatcher/tests/run_d.py": "print('1 passed, 0 failed')\n",
        "README.md": "repo\n",
    }.items():
        (REPO / path).parent.mkdir(parents=True, exist_ok=True)
        (REPO / path).write_text(text)
    g("add", "-A")
    g("commit", "-q", "-m", "start")
    g("remote", "add", "origin", str(ORIGIN))
    g("push", "-q", "origin", "main")
    g("fetch", "-q", "origin")


def script(runs: list[dict], auth: dict | None = None) -> None:
    body: dict = {"runs": runs}
    if auth is not None:
        body["auth"] = auth
    SCRIPT.write_text(json.dumps(body))
    CALLS.write_text("")
    QCALLS.write_text("")


def calls(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


PLAN = {"kind": "bug", "judge_right": True, "confidence": "high",
        "issue": "Tuesday, in a chat with a supplier, the card showed one hour instead of ninety minutes.",
        "diagnosis": "The end time was in a voice note and the booking step only reads typed text.",
        "fix": "Read the end time from every message of the request before using the default.",
        "gates": ["live_dates"]}
CASE = {"id": 7, "loop_id": "loop-7", "codes": ["wrong_details"], "judge_version": "v5",
        "evidence": {"transcript": [
            {"seq": 1, "ts": "2026-11-24T14:00:00", "who": "Marcelo", "text": "@lisa marca quinta 15h"},
            {"seq": 2, "ts": "2026-11-24T14:00:05", "who": "Zuleide Quaresma (voice message — transcribed)",
             "text": "até 16h30"},
            {"seq": 3, "ts": "2026-11-24T14:00:09", "who": "AI Assistant", "text": "Marquei 15h–16h."}]}}


def unit_checks() -> None:
    print("what Claude Code is started with")
    env = fx.claude_env()
    check("no ANTHROPIC_* variable survives", not any(k.startswith("ANTHROPIC") for k in env), str(sorted(env)[:5]))
    check("nor a parent session's CLAUDE_* variables", not any(k.startswith("CLAUDE") for k in env))
    check("PATH and HOME do", "PATH" in env and "HOME" in env)
    fx.CFG["CLAUDE_CODE_OAUTH_TOKEN"] = "tok"
    check("the subscription token from the settings file is the one exception",
          fx.claude_env().get("CLAUDE_CODE_OAUTH_TOKEN") == "tok")
    fx.CFG["CLAUDE_CODE_OAUTH_TOKEN"] = ""

    print("\nwho Claude Code would run as")
    script([], auth={"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                     "loginExpiresAt": "2027-01-01"})
    st = fx.auth_status(str(FAKE_CLAUDE))
    check("an account sign-in is accepted", st["ok"] is True and st["login_expires"] == "2027-01-01", str(st))
    script([], auth={"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"})
    st = fx.auth_status(str(FAKE_CLAUDE))
    check("not signed in is refused, and says so", st["ok"] is False and "not signed in" in st["why"], str(st))
    script([], auth={"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty"})
    check("signed in with a key is refused", fx.auth_status(str(FAKE_CLAUDE))["ok"] is False)
    script([], auth={"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"})
    check("another provider is refused", fx.auth_status(str(FAKE_CLAUDE))["ok"] is False)
    check("a missing command is refused", fx.auth_status(str(TMP / "nope"))["ok"] is False)

    print("\nplans")
    ok = fx.check_plan
    check("a full bug plan passes", ok(PLAN, set()) is None)
    check("a short paragraph is refused", ok({**PLAN, "fix": "do it"}, set()) == "fix is missing or too short")
    check("an unknown kind is refused", "unknown kind" in ok({**PLAN, "kind": "poem"}, set()))
    check("a duplicate must name a recent case", ok({"kind": "duplicate", "duplicate_of": 3}, {1, 2}) is not None
          and ok({"kind": "duplicate", "duplicate_of": 2}, {1, 2}) is None)
    check("no_action needs no paragraphs", ok({"kind": "no_action"}, set()) is None)

    print("\nwhat a change may touch")
    check("Lisa's code, her tests, the dispatcher",
          fx.outside_scope(["Lisa flow/app/x.py", "Lisa flow/tests/run_x.py", "dispatcher/app.py",
                            "dispatcher/tests/run_y.py"]) == [])
    stray = ["evolution/docker-compose.yml", "fixer/run.py", ".claude/scripts/lisa-log-sql", "Lisa flow/Dockerfile",
             ".github/workflows/x.yml", "dispatcher/Dockerfile"]
    check("not the compose file, the worker, the scripts, a Dockerfile or CI", fx.outside_scope(stray) == stray)

    print("\ncontact data")
    cd = fx.contact_data
    check("a real-looking WhatsApp id is caught", cd('JID = "5531987654321@s.whatsapp.net"', []) != [])
    check("a group id is caught", cd("120363298765432101@g.us", []) != [])
    check("a formatted Brazilian number is caught", cd("call +55 31 98765-4321 now", []) != [])
    check("the made-up test ids pass", cd('A = "5511900000001@s.whatsapp.net"; B = "5511999990000@s.whatsapp.net"', []) == [])
    check("a name from the chat is caught", cd('who = "Zuleide"', ["Zuleide", "Quaresma"]) != [])
    check("ordinary code passes", cd("def end_time(messages):\n    return messages[-1]", ["Zuleide"]) == [])
    check("names come from the other speakers only", fx.chat_names(CASE["evidence"]) == ["Quaresma", "Zuleide"],
          str(fx.chat_names(CASE["evidence"])))

    print("\nlive checks")
    check("verdict lines are counted", fx.parse_gate("[PASS] a\n  [PASS] b\n[FAIL] c\nnoise [PASS]") == (2, 1))
    wt = REPO
    check("a change to her code gets the plan's checks, the date check always",
          fx.pick_gates({"gates": ["live_nope", "live_dates"]}, wt, ["Lisa flow/app/thing.py"]) == ["live_dates.py"])
    check("a dispatcher-only change gets none", fx.pick_gates(PLAN, wt, ["dispatcher/app.py"]) == [])

    print("\nthe queue")
    QUEUE.write_text(json.dumps({"pull": {"ok": True, "enabled": True, "new": [], "approved": [], "recent": []}}))
    QCALLS.write_text("")
    out = fx.queue("pull", "mac", stdin={"cli": "x"})
    check("the answer is the last JSON line, past any noise", out["enabled"] is True)
    check("the body goes in on stdin", calls(QCALLS)[0] == {"args": ["pull", "mac"], "body": {"cli": "x"}})


def claude_checks() -> None:
    print("\none Claude Code run")
    rec = TMP / "rec.jsonl"
    common = dict(cwd=REPO, mode="dontAsk", tools=fx.READ_TOOLS, schema=fx.PLAN_SCHEMA, add_dirs=[TMP],
                  timeout=20, record=rec)
    script([{"structured": {"kind": "no_action"}}])
    res = fx.run_claude(str(FAKE_CLAUDE), "the prompt", session_id="11111111-1111-1111-1111-111111111111", **common)
    call = calls(CALLS)[0]
    check("the result comes back", fx.structured(res) == {"kind": "no_action"})
    check("the prompt went in on stdin", call["prompt"] == "the prompt")
    check("no key and no parent-session variable reached it", call["env"] == [], str(call["env"]))
    a = call["args"]
    check("headless, with a stream to check", a[:4] == ["-p", "--output-format", "stream-json", "--verbose"])
    check("nothing may prompt", a[a.index("--permission-prompts") + 1] == "none"
          and a[a.index("--permission-mode") + 1] == "dontAsk")
    tools = a[a.index("--allowedTools") + 1]
    check("an investigation gets no edit tool and no open shell",
          "Edit" not in tools and "Write" not in tools and "Bash(git log *)" in tools and "Bash(*)" not in tools, tools)
    check("never --bare", "--bare" not in a)
    check("the session id is passed", a[a.index("--session-id") + 1].startswith("1111"))
    check("the stream is kept", '"type": "result"' in rec.read_text())

    script([{"key_source": "ANTHROPIC_API_KEY", "sleep": 5, "structured": {}}])
    try:
        fx.run_claude(str(FAKE_CLAUDE), "p", **common)
        check("a run that starts on an API key is stopped", False)
    except fx.BillingError as exc:
        check("a run that starts on an API key is stopped", "ANTHROPIC_API_KEY" in str(exc))
    script([{"sleep": 30, "structured": {}}])
    try:
        fx.run_claude(str(FAKE_CLAUDE), "p", **{**common, "timeout": 1})
        check("a run that hangs is cut off", False)
    except RuntimeError as exc:
        check("a run that hangs is cut off", "no answer" in str(exc), str(exc))
    script([{"is_error": True, "text": "limit reached"}])
    try:
        fx.run_claude(str(FAKE_CLAUDE), "p", **common)
        check("an error result raises", False)
    except RuntimeError as exc:
        check("an error result raises", "limit reached" in str(exc))
    script([{"no_result": True}])
    try:
        fx.run_claude(str(FAKE_CLAUDE), "p", **common)
        check("a run with no result raises", False)
    except RuntimeError:
        check("a run with no result raises", True)
    script([{"structured": {"kind": "bug"}}])
    fx.run_claude(str(FAKE_CLAUDE), "p", resume="abc", **common)
    a = calls(CALLS)[0]["args"]
    check("a resumed run names the session and no new id", a[a.index("--resume") + 1] == "abc" and "--session-id" not in a)


def flow_checks() -> None:
    cli = str(FAKE_CLAUDE)
    QUEUE.write_text(json.dumps({"claim": {"ok": True}, "plan": {"ok": True, "status": "planned"},
                                 "result": {"ok": True, "status": "ready"}, "release": {"ok": True, "status": "new"}}))

    print("\nan investigation")
    script([{"structured": PLAN}])
    out = fx.investigate(cli, dict(CASE), recent=[])
    q = calls(QCALLS)
    check("claimed, then the plan reported", [c["args"][0] for c in q] == ["claim", "plan"] and out == "planned", str(q))
    check("the plan and the session to resume are sent", q[1]["body"]["plan"] == PLAN and len(q[1]["body"]["session_id"]) == 36)
    wt = fx.worktree(7)
    check("it ran in the case's own checkout", Path(calls(CALLS)[0]["cwd"]).resolve() == wt.resolve())
    d = fx.case_dir(7)
    check("the evidence was laid out for it", "Zuleide" in (d / "transcript.txt").read_text()
          and (d / "evidence.json").exists() and (d / "recent-cases.json").exists())
    check("the checkout is kept for the fix", wt.exists())

    script([{"structured": {**PLAN, "kind": "duplicate", "duplicate_of": 99}}])
    out = fx.investigate(cli, {**CASE, "id": 8}, recent=[{"id": 3}])
    check("an unusable plan releases the case", out == "released"
          and [c["args"][0] for c in calls(QCALLS)] == ["claim", "release"])
    QUEUE.write_text(json.dumps({"claim": {"ok": False}}))
    script([])
    check("a case someone else holds is left alone", fx.investigate(cli, {**CASE, "id": 9}, []) == "not_claimed"
          and calls(CALLS) == [])
    QUEUE.write_text(json.dumps({"claim": {"ok": True}, "plan": {"ok": True, "status": "duplicate"},
                                 "result": {"ok": True}, "release": {"ok": True}}))
    script([{"structured": {"kind": "duplicate", "duplicate_of": 3, "judge_right": True, "issue": "", "diagnosis": "",
                            "fix": "", "confidence": "high"}}])
    fx.investigate(cli, {**CASE, "id": 10}, recent=[{"id": 3}])
    check("a closed case's checkout is removed", not fx.worktree(10).exists())

    print("\na fix")
    QUEUE.write_text(json.dumps({"claim": {"ok": True}, "result": {"ok": True}, "release": {"ok": True}}))
    case = {**CASE, "plan": PLAN, "session_id": "sess-7", "approval_note": "only in groups"}
    good = {"Lisa flow/app/thing.py": "VALUE = 2\n", "Lisa flow/tests/run_thing2.py": "print('1 passed, 0 failed')\n"}
    script([{"edits": good, "structured": {"done": True, "summary": "She reads the end time from voice notes too."}},
            {"structured": {"ok": True, "reason": "does what the plan says"}}])
    out = fx.fix(cli, case)
    q = calls(QCALLS)
    res = q[-1]["body"]
    check("reported ready", out == "ready" and q[-1]["args"] == ["result", "7"], str(q[-1]))
    check("with the branch and the commit", res["branch"] == "fix/case-7" and len(res["commit"]) == 40)
    check("and what changed, in words",
          res["summary"] == "She reads the end time from voice notes too. 2 files changed.", res["summary"])
    check("and what was checked", res["checks"] == "Offline tests pass (3 files); live checks are switched off.", res["checks"])
    c = calls(CALLS)
    check("the fix run resumed the investigation, in its checkout",
          c[0]["args"][c[0]["args"].index("--resume") + 1] == "sess-7" and Path(c[0]["cwd"]).resolve() == wt.resolve())
    tools = c[0]["args"][c[0]["args"].index("--allowedTools") + 1]
    check("it may edit and run the offline tests, and has no git write, push or ssh",
          "Edit" in tools and "offline-tests" in tools and "git push" not in tools and "git commit" not in tools
          and "ssh" not in tools, tools)
    check("the second read is a fresh, read-only run",
          "--resume" not in c[1]["args"] and "Edit" not in c[1]["args"][c[1]["args"].index("--allowedTools") + 1])
    check("the branch is on origin with the change", g("show", "fix/case-7:Lisa flow/app/thing.py", cwd=ORIGIN) == "VALUE = 2")
    check("main is untouched", g("show", "main:Lisa flow/app/thing.py", cwd=ORIGIN) == "VALUE = 1")
    msg = g("log", "-1", "--format=%B", "fix/case-7", cwd=ORIGIN)
    check("the commit says what and which case", msg.startswith("fix(lisa): She reads the end time") and "F7" in msg, msg)

    def failed_fix(case_id: int, runs: list[dict]) -> dict:
        script(runs)
        fx.fix(cli, {**case, "id": case_id, "session_id": None})
        return calls(QCALLS)[-1]["body"]

    done = {"done": True, "summary": "Changed."}
    r = failed_fix(21, [{"edits": {"evolution/docker-compose.yml": "x\n"}, "structured": done}])
    check("a change outside Lisa and the dispatcher fails at the scope check",
          r["status"] == "failed" and r["failed_step"] == "the scope check" and "docker-compose" in r["detail"], str(r))
    r = failed_fix(22, [{"edits": {"Lisa flow/tests/run_new.py": 'JID = "5531987654321@s.whatsapp.net"\n'}, "structured": done}])
    check("a real-looking number fails at the contact-data check", r["failed_step"] == "the contact-data check", str(r))
    r = failed_fix(23, [{"edits": {"Lisa flow/app/thing.py": "VALUE = 3\n"}, "structured": done}])
    check("a broken test fails at the offline tests, naming the file",
          r["failed_step"] == "the offline tests" and "run_thing.py" in r["detail"], str(r))
    r = failed_fix(24, [{"edits": good, "structured": done}, {"structured": {"ok": False, "reason": "adds a network call"}}])
    check("a refusal from the second read fails there, with its reason",
          r["failed_step"] == "the second read of the diff" and r["detail"] == "adds a network call", str(r))
    r = failed_fix(25, [{"structured": {"done": False, "summary": "", "why_not": "the cause is elsewhere"}}])
    check("a run that declines says why", r["failed_step"] == "the change" and r["detail"] == "the cause is elsewhere", str(r))
    r = failed_fix(26, [{"structured": done}])
    check("a run that changed nothing fails", r["failed_step"] == "the change", str(r))
    branches = g("branch", "--list", "fix/*", cwd=ORIGIN)
    check("none of the failed ones was pushed", branches.split() == ["fix/case-7"], branches)

    script([{"key_source": "apiKeyHelper", "sleep": 5, "structured": done}])
    try:
        fx.fix(cli, {**case, "id": 27, "session_id": None})
        check("a fix on the wrong credential stops the whole run", False)
    except fx.BillingError:
        check("a fix on the wrong credential stops the whole run",
              [c["args"][0] for c in calls(QCALLS)] == ["claim", "release"])

    print("\none daily run")
    QUEUE.write_text(json.dumps({"pull": {"ok": True, "enabled": True, "approved": [], "recent": [],
                                          "new": [{**CASE, "id": 31}]},
                                 "claim": {"ok": True}, "plan": {"ok": True, "status": "planned"}}))
    script([{"structured": PLAN}], auth={"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"})
    rc = fx.main([])
    q = calls(QCALLS)
    check("not signed in: it checks in and stops before any model work",
          rc == 2 and [c["args"][0] for c in q] == ["pull"] and calls(CALLS) == [], f"{rc} {q}")
    check("... and the check-in says why", q[0]["body"]["ok"] is False and "not signed in" in q[0]["body"]["why"])
    script([{"structured": PLAN}])
    rc = fx.main(["--dry"])
    check("--dry asks the queue and does nothing else", rc == 0 and [c["args"][0] for c in calls(QCALLS)] == ["pull"])
    fx.VENV_PY = Path(sys.executable)
    script([{"structured": PLAN}])
    rc = fx.main([])
    check("signed in: the new case is investigated",
          rc == 0 and [c["args"][0] for c in calls(QCALLS)] == ["pull", "claim", "plan"])
    QUEUE.write_text(json.dumps({"pull": {"ok": True, "enabled": False, "approved": [], "recent": [], "new": [CASE]}}))
    script([])
    rc = fx.main([])
    check("switched off on the droplet: nothing runs", rc == 0 and calls(CALLS) == [])


def main() -> int:
    make_repo()
    fx.VENV_PY = Path(sys.executable)
    unit_checks()
    claude_checks()
    flow_checks()
    print(f"\n{_checks['pass']} passed, {_checks['fail']} failed")
    return 1 if _checks["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
