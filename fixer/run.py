#!/usr/bin/env python3
"""The fix loop's worker: runs on his Mac once a day, in Claude Code, on his Claude subscription.

    1. asks the droplet what is in the queue        (ssh → app.fixdesk.cli pull)
    2. for each plan he approved with "fix it":       makes the fix in a worktree, tests it, has the
                                                      diff read a second time, runs the live checks,
                                                      pushes the branch, reports
    3. for each new case the judge opened:            investigates it read-only and reports a plan

The droplet cannot call a laptop, so the Mac asks. What is said on WhatsApp is not decided here:
this reports to the queue, and the fix desk inside Lisa (`Lisa flow/app/fixdesk/`) sends.

BILLING. The model work here must go to the Claude subscription, never to an API key:
  - every `ANTHROPIC_*` variable is removed from Claude Code's environment (a key there wins over
    the account sign-in, silently);
  - `--bare` is never used (it skips the sign-in);
  - a run stops before its first model call unless `claude auth status` shows the account signed
    in, and stops mid-start if Claude Code reports any key as its credential.

SAFETY. The investigation can read and nothing else. The fix run can edit files in its own
worktree and run the offline tests, nothing else: no git writes, no ssh, no network tools. The
commit, the checks, the push and the report are made by this script, after it has checked that
the change stays inside Lisa and the dispatcher and carries no contact data.

Standard library only. Installed by `fixer/install.sh` into ~/.lisa-fixer/app and started by
launchd; `python3 ~/.lisa-fixer/app/run.py` runs it by hand. `--dry` pulls and prints, and does
nothing else."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("FIXER_STATE", "~/.lisa-fixer")).expanduser()


def _load_config() -> dict:
    """Settings: the defaults, then ~/.lisa-fixer/config.env, then the environment."""
    cfg = {
        "FIXER_REPO": "~/Code/personal-whatsapp-ai",
        "FIXER_SSH_HOST": "secretaria-droplet",
        "FIXER_CONTAINER": "lisa_brain",
        "FIXER_IMAGE": "evolution-lisa",
        "FIXER_CLAUDE": "",            # path to the claude command; found on PATH when empty
        "FIXER_MODEL": "",             # empty = the account's default model
        "FIXER_MAX_NEW": "5",          # investigations per run
        "FIXER_MAX_FIX": "3",          # fixes per run
        "FIXER_INVESTIGATE_TIMEOUT": "1800",
        "FIXER_FIX_TIMEOUT": "3600",
        "FIXER_LIVE_CHECKS": "1",      # "0" = offline tests only (no API spend, not end to end)
        "FIXER_DEPLOY": "0",           # the first fixes stop at a tested branch
        "CLAUDE_CODE_OAUTH_TOKEN": "",  # only if launchd cannot see the sign-in (claude setup-token)
    }
    f = STATE / "config.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(cfg):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


CFG = _load_config()
REPO = Path(CFG["FIXER_REPO"]).expanduser()
VENV_PY = REPO / "Lisa flow" / ".venv" / "bin" / "python"
OFFLINE_TESTS = STATE / "bin" / "offline-tests"

# What a change may touch. Anything else fails the run before a test is spent on it.
ALLOWED_PATHS = ("Lisa flow/app/", "Lisa flow/tests/", "dispatcher/app.py", "dispatcher/tests/")

READ_TOOLS = [
    "Read", "Grep", "Glob",
    "Bash(git log *)", "Bash(git show *)", "Bash(git diff *)", "Bash(git grep *)",
    "Bash(git status*)", "Bash(git blame *)",
    "Bash(.claude/scripts/lisa-log-sql *)",
]
FIX_TOOLS = READ_TOOLS + ["Edit", "Write", f"Bash({OFFLINE_TESTS} *)", f"Bash({OFFLINE_TESTS})"]

PLAN_KINDS = ("bug", "judge", "decision", "duplicate", "already_fixed", "no_action")
PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": list(PLAN_KINDS)},
        "judge_right": {"type": "boolean"},
        "issue": {"type": "string"},
        "diagnosis": {"type": "string"},
        "fix": {"type": "string"},
        "duplicate_of": {"type": "integer"},
        "fixed_by": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "test": {"type": "string"},
        "gates": {"type": "array", "items": {"type": "string"}},
        "prompt_changes": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "notes": {"type": "string"},
    },
    "required": ["kind", "judge_right", "issue", "diagnosis", "fix", "confidence"],
}
FIX_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "done": {"type": "boolean"},
        "summary": {"type": "string"},
        "test_added": {"type": "string"},
        "prompt_changed": {"type": "boolean"},
        "why_not": {"type": "string"},
    },
    "required": ["done", "summary"],
}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"ok": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["ok", "reason"],
}


class Stop(Exception):
    """This step failed in a way worth telling him: (step, detail)."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step, self.detail = step, detail


class BillingError(Exception):
    """Claude Code would not run on the subscription. Nothing may be spent."""


# ── logging ─────────────────────────────────────────────────────────────────
def log(msg: str, **kw: Any) -> None:
    line = json.dumps({"t": datetime.now().isoformat(timespec="seconds"), "msg": msg, **kw}, default=str)
    print(line, flush=True)
    try:
        d = STATE / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{datetime.now():%Y-%m}.log", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def sh(cmd: list[str], *, cwd: Optional[Path] = None, stdin: Optional[str] = None,
       timeout: float = 300, check: bool = True, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=timeout, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}… exited {p.returncode}: {(p.stderr or p.stdout)[-400:]}")
    return p


# ── the queue, over SSH ───────────────────────────────────────────────────────
def queue(*args: str, stdin: Optional[dict] = None, timeout: float = 120) -> dict:
    """One call to `app.fixdesk.cli` inside the Lisa container. The answer is the last line."""
    remote = "docker exec -i {c} python -m app.fixdesk.cli {a}".format(
        c=shlex.quote(CFG["FIXER_CONTAINER"]), a=" ".join(shlex.quote(a) for a in args))
    base = shlex.split(os.environ.get("FIXER_SSH_CMD", "")) or [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", CFG["FIXER_SSH_HOST"]]
    p = sh([*base, remote], stdin=json.dumps(stdin if stdin is not None else {}, default=str),
           timeout=timeout, check=False)
    lines = [ln for ln in p.stdout.strip().splitlines() if ln.startswith("{")]
    if not lines:
        raise RuntimeError(f"queue {args[0]}: no answer (exit {p.returncode}): {p.stderr[-300:]}")
    return json.loads(lines[-1])


# ── Claude Code ──────────────────────────────────────────────────────────────
def find_claude() -> Optional[str]:
    if CFG["FIXER_CLAUDE"]:
        return CFG["FIXER_CLAUDE"] if Path(CFG["FIXER_CLAUDE"]).expanduser().exists() else None
    for cand in (shutil.which("claude"), "~/.local/bin/claude", "/opt/homebrew/bin/claude",
                 "/usr/local/bin/claude", "~/.claude/local/claude"):
        if cand and Path(cand).expanduser().exists():
            return str(Path(cand).expanduser())
    return None


def claude_env() -> dict:
    """Claude Code's environment: nothing that could put a run on an API key, and nothing a
    parent Claude session left behind. The one-year subscription token is the only exception."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANTHROPIC_", "CLAUDE_", "CLAUDECODE", "AWS_BEARER_TOKEN_BEDROCK"))}
    if CFG["CLAUDE_CODE_OAUTH_TOKEN"]:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = CFG["CLAUDE_CODE_OAUTH_TOKEN"]
    return env


def auth_status(cli: str) -> dict:
    """Who Claude Code would run as. `ok` only for an account sign-in, never a key."""
    try:
        p = sh([cli, "auth", "status", "--json"], env=claude_env(), timeout=60, check=False,
               cwd=Path.home())
        st = json.loads(p.stdout[p.stdout.index("{"):])
    except Exception as exc:
        return {"ok": False, "why": f"could not read the sign-in: {exc}"[:200]}
    method = str(st.get("authMethod") or "none")
    out = {"logged_in": bool(st.get("loggedIn")), "auth_method": method,
           "login_expires": st.get("loginExpiresAt"), "provider": st.get("apiProvider")}
    if not out["logged_in"]:
        out.update(ok=False, why="Claude Code is not signed in on this Mac")
    elif st.get("apiProvider") not in (None, "firstParty") or re.search(r"key|console|helper", method, re.I):
        out.update(ok=False, why=f"Claude Code is signed in with '{method}', which bills an API key")
    else:
        out["ok"] = True
    return out


def run_claude(cli: str, prompt: str, *, cwd: Path, mode: str, tools: list[str], schema: dict,
               add_dirs: list[Path], timeout: float, record: Path,
               session_id: Optional[str] = None, resume: Optional[str] = None) -> dict:
    """One headless Claude Code run. Returns its final result message.

    The prompt goes in on stdin. The whole stream is kept in `record` for reading afterwards."""
    cmd = [cli, "-p", "--output-format", "stream-json", "--verbose",
           "--permission-mode", mode, "--permission-prompts", "none",
           "--allowedTools", ",".join(tools),
           "--json-schema", json.dumps(schema)]
    for d in add_dirs:
        cmd += ["--add-dir", str(d)]
    if resume:
        cmd += ["--resume", resume]
    elif session_id:
        cmd += ["--session-id", session_id]
    if CFG["FIXER_MODEL"]:
        cmd += ["--model", CFG["FIXER_MODEL"]]

    proc = subprocess.Popen(cmd, cwd=cwd, env=claude_env(), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    killed: dict = {}

    def kill(why: str) -> None:
        killed["why"] = why
        proc.kill()

    timer = threading.Timer(timeout, kill, args=(f"no answer in {int(timeout)} s",))
    timer.start()
    result: Optional[dict] = None
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        with open(record, "a") as rec:
            for line in proc.stdout:
                rec.write(line)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("type") == "system" and msg.get("subtype") == "init":
                    src = msg.get("apiKeySource")
                    if src not in (None, "none"):
                        kill(f"billing:{src}")
                elif msg.get("type") == "result":
                    result = msg
        proc.wait(timeout=30)
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
    if str(killed.get("why", "")).startswith("billing:"):
        raise BillingError(f"Claude Code started with credential '{killed['why'][8:]}', not the account")
    if killed:
        raise RuntimeError(killed["why"])
    if result is None:
        raise RuntimeError(f"Claude Code ended with no result (exit {proc.returncode}): "
                           f"{(proc.stderr.read() or '')[-300:]}")
    if result.get("is_error"):
        raise RuntimeError(f"Claude Code reported an error: {str(result.get('result'))[:300]}")
    return result


def structured(result: dict) -> dict:
    out = result.get("structured_output")
    if isinstance(out, dict):
        return out
    text = str(result.get("result") or "")
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    raise RuntimeError("Claude Code's answer was not in the asked-for shape")


# ── git and the worktree ─────────────────────────────────────────────────────
def git(*args: str, cwd: Optional[Path] = None, check: bool = True, timeout: float = 180) -> str:
    return sh(["git", *args], cwd=cwd or REPO, check=check, timeout=timeout).stdout.strip()


def worktree(case_id: int) -> Path:
    return REPO / ".claude" / "worktrees" / f"fix-case-{case_id}"


def fresh_worktree(case_id: int) -> Path:
    """The case's own checkout of what is deployed (origin/main), with nothing else in it."""
    wt = worktree(case_id)
    if wt.exists():
        git("worktree", "remove", "--force", str(wt), check=False)
    git("worktree", "prune", check=False)
    git("worktree", "add", "--detach", str(wt), "origin/main")
    return wt


def remove_worktree(case_id: int) -> None:
    wt = worktree(case_id)
    if wt.exists():
        git("worktree", "remove", "--force", str(wt), check=False)


def changed_paths(wt: Path) -> list[str]:
    out = git("status", "--porcelain", "--untracked-files=all", cwd=wt)
    paths = []
    for line in out.splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip().strip('"'))
    return paths


def outside_scope(paths: list[str]) -> list[str]:
    return [p for p in paths if not p.startswith(ALLOWED_PATHS)]


# Contact data must never reach GitHub: a real number, a WhatsApp id, or a name from the chat.
_CONTACT_PATTERNS = [
    (re.compile(r"\b\d{10,}@(s\.whatsapp\.net|lid|g\.us)\b"), "a WhatsApp id"),
    (re.compile(r"\b\d{12,}-\d{6,}@g\.us\b"), "a group id"),
    (re.compile(r"(?<![\w.])\+?55\s?\(?\d{2}\)?\s?9?\d{4}[-\s]?\d{4}(?!\d)"), "a Brazilian phone number"),
]
# A test id is recognisably made up when it has four of one digit in a row (5511900000001,
# 5511999990000), which is how every id in the tests is written.
_MADE_UP = re.compile(r"(\d)\1{3}")


def contact_data(added_text: str, names: list[str]) -> list[str]:
    """What in these added lines looks like a real person's data. Made-up test ids pass."""
    hits = []
    for pat, what in _CONTACT_PATTERNS:
        for m in pat.finditer(added_text):
            digits = re.sub(r"\D", "", m.group(0).split("@")[0])
            if not _MADE_UP.search(digits):
                hits.append(f"{what}: {m.group(0)[:6]}…")
    for name in names:
        if len(name) >= 4 and re.search(rf"\b{re.escape(name)}\b", added_text):
            hits.append(f"a name from the chat: {name[:2]}…")
    return sorted(set(hits))


def chat_names(evidence: dict, owner: str = "Marcelo") -> list[str]:
    """First names of the other people in the session, to keep out of commits."""
    names = set()
    for line in evidence.get("transcript") or []:
        who = re.sub(r"\(.*", "", str(line.get("who") or "")).strip()
        if not who or who.startswith(("AI Assistant", owner)):
            continue
        names.update(w for w in who.split() if w[:1].isupper())
    return sorted(names)


# ── tests and checks ─────────────────────────────────────────────────────────
_SUMMARY = re.compile(r"(\d+)\s+passed,\s+(\d+)\s+failed")


def offline_tests(wt: Path) -> tuple[int, list[str]]:
    """Every offline test of Lisa and the dispatcher. Returns (files run, files that failed)."""
    failed, ran = [], 0
    for folder in ("Lisa flow", "dispatcher"):
        for test in sorted((wt / folder / "tests").glob("run_*.py")):
            ran += 1
            p = sh([str(VENV_PY), f"tests/{test.name}"], cwd=wt / folder, check=False, timeout=900)
            m = _SUMMARY.search(p.stdout[-400:])
            if p.returncode != 0 or (m and int(m.group(2)) > 0):
                failed.append(f"{folder}/tests/{test.name}")
    return ran, failed


def parse_gate(output: str) -> tuple[int, int]:
    passed = len(re.findall(r"^\s*\[PASS\]", output, re.M))
    failed = len(re.findall(r"^\s*\[FAIL\]", output, re.M))
    return passed, failed


def live_checks(case_id: int, wt: Path, gates: list[str]) -> str:
    """Run the named live checks on the droplet, against the real model, with this tree mounted
    over the running image. A check that fails is run once more: the model varies."""
    remote_dir = f"/tmp/fix-case-{case_id}"
    host = CFG["FIXER_SSH_HOST"]
    tar = subprocess.Popen(["tar", "-c", "--no-xattrs", "--exclude", "__pycache__",
                            "Lisa flow/app", "Lisa flow/tests"], cwd=wt, stdout=subprocess.PIPE,
                           env={**os.environ, "COPYFILE_DISABLE": "1"})
    up = subprocess.run(["ssh", "-o", "BatchMode=yes", host,
                         f"rm -rf {remote_dir} && mkdir -p {remote_dir} && tar -x -C {remote_dir}"],
                        stdin=tar.stdout, capture_output=True, text=True, timeout=300)
    tar.wait()
    if up.returncode != 0:
        raise Stop("the live checks", f"could not copy the change to the droplet: {up.stderr[-200:]}")
    script = (HERE / "remote" / "live-check.sh").read_text()
    notes = []
    try:
        for gate in gates:
            best = None
            for _ in range(2):
                p = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", host, "bash", "-s", "--", remote_dir,
                     CFG["FIXER_CONTAINER"], CFG["FIXER_IMAGE"], gate],
                    input=script, capture_output=True, text=True, timeout=2400)
                ok, bad = parse_gate(p.stdout)
                best = (ok, bad, p.returncode, p.stdout[-300:] + p.stderr[-200:])
                if bad == 0 and ok > 0 and p.returncode == 0:
                    break
            ok, bad, rc, tail = best
            name = gate.removesuffix(".py")
            if bad or not ok or rc != 0:
                why = f"{name} {ok}/{ok + bad}, twice" if ok + bad else f"{name} did not run: {tail.strip()[-120:]}"
                raise Stop("the live checks", why)
            notes.append(f"{name} {ok}/{ok}")
    finally:
        subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"rm -rf {remote_dir}"],
                       capture_output=True, timeout=120)
    return ", ".join(notes)


def pick_gates(plan: dict, wt: Path, paths: list[str]) -> list[str]:
    """The live checks this change needs: the ones the plan named that exist, and always the
    date check when Lisa's own code changed. None for a dispatcher-only change."""
    if not any(p.startswith("Lisa flow/app/") for p in paths):
        return []
    have = {t.name for t in (wt / "Lisa flow" / "tests").glob("live_*.py")} - {"live_key.py"}
    gates = [g if g.endswith(".py") else f"{g}.py" for g in (plan.get("gates") or [])]
    gates = [g for g in dict.fromkeys(gates) if g in have]
    if "live_dates.py" in have and "live_dates.py" not in gates:
        gates.append("live_dates.py")
    return gates[:4]


# ── the two jobs ─────────────────────────────────────────────────────────────
def prompt(name: str, **kw: Any) -> str:
    text = (HERE / "prompts" / f"{name}.md").read_text()
    for k, v in kw.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def case_dir(case_id: int) -> Path:
    d = STATE / "cases" / str(case_id)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE / "cases", 0o700)
    return d


def render_transcript(evidence: dict) -> str:
    lines = []
    for ln in evidence.get("transcript") or []:
        lines.append(f"[{ln.get('seq')}] {str(ln.get('ts'))[:19]} {ln.get('who')}: {ln.get('text')}")
    return "\n".join(lines) + "\n"


def check_plan(plan: dict, recent_ids: set[int]) -> Optional[str]:
    """Why this plan cannot be used, or None."""
    kind = plan.get("kind")
    if kind not in PLAN_KINDS:
        return f"unknown kind {kind!r}"
    if kind == "duplicate":
        if plan.get("duplicate_of") not in recent_ids:
            return "duplicate_of does not name a recent case"
        return None
    if kind in ("bug", "judge", "decision"):
        for field in ("issue", "diagnosis", "fix"):
            if len(str(plan.get(field) or "").strip()) < 20:
                return f"{field} is missing or too short"
    return None


def investigate(cli: str, case: dict, recent: list[dict]) -> str:
    cid = case["id"]
    if not queue("claim", str(cid), "investigating").get("ok"):
        return "not_claimed"
    try:
        d = case_dir(cid)
        evidence = case.get("evidence") or {}
        (d / "evidence.json").write_text(json.dumps({k: v for k, v in case.items()}, indent=1, default=str))
        (d / "transcript.txt").write_text(render_transcript(evidence))
        (d / "recent-cases.json").write_text(json.dumps(recent, indent=1, default=str))
        wt = fresh_worktree(cid)
        sid = str(uuid.uuid4())
        res = run_claude(
            cli, prompt("investigate", case_id=cid, case_dir=d, loop_id=case.get("loop_id"),
                        codes=", ".join(case.get("codes") or [])),
            cwd=wt, mode="dontAsk", tools=READ_TOOLS, schema=PLAN_SCHEMA, add_dirs=[d],
            timeout=float(CFG["FIXER_INVESTIGATE_TIMEOUT"]), record=d / "investigate.jsonl",
            session_id=sid)
        plan = structured(res)
        bad = check_plan(plan, {r["id"] for r in recent})
        if bad:
            raise RuntimeError(f"unusable plan: {bad}")
        (d / "plan.json").write_text(json.dumps(plan, indent=1))
        out = queue("plan", str(cid), stdin={"plan": plan, "session_id": sid})
        if not out.get("ok"):
            raise RuntimeError(f"the queue refused the plan: {out}")
        if out.get("status") != "planned":
            remove_worktree(cid)
        log("investigated", case=cid, kind=plan.get("kind"), status=out.get("status"),
            turns=res.get("num_turns"))
        return str(out.get("status"))
    except BillingError:
        queue("release", str(cid), stdin={"error": "billing check"})
        raise
    except Exception as exc:
        log("investigation failed", case=cid, error=str(exc)[:400])
        queue("release", str(cid), stdin={"error": str(exc)[:400]})
        return "released"


def fix(cli: str, case: dict) -> str:
    cid = case["id"]
    if not queue("claim", str(cid), "fixing").get("ok"):
        return "not_claimed"
    branch = f"fix/case-{cid}"
    try:
        result = _fix(cli, case, branch)
    except BillingError:
        queue("release", str(cid), stdin={"error": "billing check"})
        raise
    except Stop as stop:
        result = {"status": "failed", "failed_step": stop.step, "detail": stop.detail}
    except Exception as exc:
        log("fix run died", case=cid, error=str(exc)[:400])
        queue("release", str(cid), stdin={"error": str(exc)[:400]})
        return "released"
    out = queue("result", str(cid), stdin=result)
    log("fix reported", case=cid, status=result.get("status"), step=result.get("failed_step"),
        accepted=out.get("ok"))
    return str(result.get("status"))


def _fix(cli: str, case: dict, branch: str) -> dict:
    cid = case["id"]
    plan = case.get("plan") or {}
    d = case_dir(cid)
    evidence = {}
    if (d / "evidence.json").exists():
        evidence = (json.loads((d / "evidence.json").read_text()).get("evidence")) or {}
    (d / "plan.json").write_text(json.dumps(plan, indent=1))

    # The investigation's own checkout, moved to today's main, so the session can be resumed.
    wt = worktree(cid)
    resume = case.get("session_id") if wt.exists() else None
    if wt.exists():
        git("checkout", "--force", "--detach", "origin/main", cwd=wt)
        git("clean", "-fdq", cwd=wt)
    else:
        wt = fresh_worktree(cid)
    git("checkout", "-B", branch, "origin/main", cwd=wt)

    text = prompt("fix", case_id=cid, case_dir=d, plan=json.dumps(plan, indent=1),
                  note=case.get("approval_note") or "(none)", offline_tests=OFFLINE_TESTS)
    kw = dict(cwd=wt, mode="acceptEdits", tools=FIX_TOOLS, schema=FIX_SCHEMA, add_dirs=[d],
              timeout=float(CFG["FIXER_FIX_TIMEOUT"]), record=d / "fix.jsonl")
    try:
        res = run_claude(cli, text, resume=resume, **kw) if resume else \
            run_claude(cli, text, session_id=str(uuid.uuid4()), **kw)
    except BillingError:
        raise
    except RuntimeError as exc:
        if not resume:
            raise
        log("resume failed, starting fresh", case=cid, error=str(exc)[:200])
        git("checkout", "--force", "-B", branch, "origin/main", cwd=wt)
        git("clean", "-fdq", cwd=wt)
        res = run_claude(cli, text, session_id=str(uuid.uuid4()), **kw)
    answer = structured(res)
    if not answer.get("done"):
        raise Stop("the change", str(answer.get("why_not") or answer.get("summary") or "it was not made")[:200])

    paths = changed_paths(wt)
    if not paths:
        raise Stop("the change", "the run ended without changing any file")
    stray = outside_scope(paths)
    if stray:
        raise Stop("the scope check", f"it touched files outside Lisa and the dispatcher: {', '.join(stray[:3])}")
    git("add", "-A", cwd=wt)
    added = "\n".join(ln[1:] for ln in git("diff", "--cached", "-U0", cwd=wt).splitlines()
                      if ln.startswith("+") and not ln.startswith("+++"))
    leaks = contact_data(added, chat_names(evidence))
    if leaks:
        raise Stop("the contact-data check", f"the change contains {'; '.join(leaks[:3])}")

    ran, failed = offline_tests(wt)
    if failed:
        raise Stop("the offline tests", f"{len(failed)} of {ran} files fail: {', '.join(failed[:3])}")

    summary = " ".join(str(answer.get("summary") or "").split())[:200]
    git("add", "-A", cwd=wt)
    git("commit", "-q",
        "-m", f"fix(lisa): {summary[:100] or f'fix case F{cid}'}",
        "-m", f"Fix case F{cid} of the improvement loop.\n\n{plan.get('diagnosis', '')}\n\n"
              "Co-Authored-By: Claude <noreply@anthropic.com>", cwd=wt)
    commit = git("rev-parse", "HEAD", cwd=wt)
    stat = git("diff", "--shortstat", "origin/main...HEAD", cwd=wt)

    review = structured(run_claude(
        cli, prompt("review", plan=json.dumps(plan, indent=1), note=case.get("approval_note") or "(none)"),
        cwd=wt, mode="dontAsk", tools=READ_TOOLS, schema=REVIEW_SCHEMA, add_dirs=[],
        timeout=900, record=d / "review.jsonl", session_id=str(uuid.uuid4())))
    if not review.get("ok"):
        raise Stop("the second read of the diff", str(review.get("reason"))[:200])

    checks = f"Offline tests pass ({ran} files)"
    gates = pick_gates(plan, wt, [p for p in git("diff", "--name-only", "origin/main...HEAD", cwd=wt).splitlines()])
    if gates and CFG["FIXER_LIVE_CHECKS"] == "1":
        checks += "; live: " + live_checks(cid, wt, gates)
    elif gates:
        checks += "; live checks are switched off"
    checks += "."
    if answer.get("prompt_changed"):
        checks += " Her prompt changed: bump the prompt version when it ships."

    git("push", "--force", "-u", "origin", f"{branch}:{branch}", cwd=wt, timeout=300)
    files = re.search(r"(\d+) files? changed", stat)
    n = int(files.group(1)) if files else len(paths)
    return {"status": "ready", "branch": branch, "commit": commit,
            "summary": f"{summary} {n} file{'s' if n != 1 else ''} changed.".strip(),
            "checks": checks}


def fix_hours(now: datetime) -> bool:
    """May fixes run now? Only from 06:00 to 21:59. The live date check books "two hours from
    now", and late at night that reads as "3 in the morning?": on 2026-11-28 at 01:00 it scored
    1/5 on production's own code. A fix judged then would be reported as failed when it is not.
    Investigations spend no live check and run at any hour."""
    return 6 <= now.hour < 22


# ── one run ──────────────────────────────────────────────────────────────────
def main(argv: list[str]) -> int:
    dry = "--dry" in argv
    STATE.mkdir(parents=True, exist_ok=True)
    lock = open(STATE / "run.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another run is going; leaving")
        return 0

    cli = find_claude()
    info: dict = {"host": socket.gethostname(), "cli": cli or None}
    if cli:
        try:
            info["cli_version"] = sh([cli, "--version"], env=claude_env(), timeout=30, check=False).stdout.strip()
        except Exception:
            pass
        info.update(auth_status(cli))
    else:
        info.update(ok=False, why="the claude command is not installed on this Mac")

    try:
        pulled = queue("pull", info["host"], stdin=info)
    except Exception as exc:
        log("could not reach the queue", error=str(exc)[:300])
        return 1
    log("pulled", approved=len(pulled.get("approved") or []), new=len(pulled.get("new") or []),
        enabled=pulled.get("enabled"), signed_in=info.get("ok"), why=info.get("why"))
    if dry:
        print(json.dumps({"info": info, "approved": [c["id"] for c in pulled.get("approved") or []],
                          "new": [c["id"] for c in pulled.get("new") or []]}, indent=1))
        return 0
    if not pulled.get("enabled"):
        return 0
    if not info.get("ok"):
        log("stopping before any model work", why=info.get("why"))
        return 2
    if not VENV_PY.exists():
        log("no test environment", path=str(VENV_PY))
        return 2

    try:
        git("fetch", "--quiet", "origin")
        approved = (pulled.get("approved") or [])[: int(CFG["FIXER_MAX_FIX"])]
        if approved and not fix_hours(datetime.now()):
            log("fixes wait for daytime", waiting=len(approved))
            approved = []
        for case in approved:
            log("fixing", case=case["id"])
            fix(cli, case)
        recent = pulled.get("recent") or []
        for case in (pulled.get("new") or [])[: int(CFG["FIXER_MAX_NEW"])]:
            log("investigating", case=case["id"])
            investigate(cli, case, recent)
    except BillingError as exc:
        log("stopped: billing", error=str(exc))
        return 3
    log("run finished")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
