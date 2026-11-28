#!/usr/bin/env python3
"""The fix loop's tool on his Mac: what the daily session calls to take work and to hand it in.

Once a day a scheduled task in the Claude desktop app starts an ordinary Claude Code session
(`fixer/task-prompt.md` is its prompt). The session does the thinking: it investigates each new
case and makes each fix he approved. This script is everything around that, and every step that
must not depend on the session's judgment:

    run.py pull                    what is in the queue (and: this Mac is here)
    run.py investigate <id>        take a new case: its own checkout, the evidence laid out
    run.py plan <id>               hand in the plan written to <checkout>/.fixer/plan.json
    run.py fix <id>                take an approved case: its checkout on branch fix/case-<id>
    run.py ship <id> --summary .. --review ..
                                   check the change and put it live: scope, contact data, every
                                   offline test, the commit, the live checks, the merge to main,
                                   the deploy (with a rollback tag), the report
    run.py undo <id>               take a fix that went live back out: revert it on main, deploy
    run.py fail <id> --step .. --detail ..     give an approved case up, and say why
    run.py release <id> --error ..             put a case back for the next run

Each prints one JSON object. Exit 0 means done; anything else means not done, and the object
says why.

The droplet cannot call a laptop, so the Mac asks: the queue is reached over ssh, inside Lisa's
container (`Lisa flow/app/fixdesk/cli.py`). What is said on WhatsApp is not decided here: this
reports to the queue, and the fix desk inside Lisa sends.

WHAT IS ENFORCED HERE, whatever the session does: a change reaches GitHub and production only
through `ship`, and `ship` refuses one that touches anything outside Lisa and the dispatcher,
that carries what looks like a real person's data, or that fails an offline test or a live
check. The session never commits, pushes or deploys; this does. The deploy keeps the running
image under a rollback tag, tries the new image before swapping, and puts the old one back if
the new one does not boot; main is then reverted too, so main and production never disagree.

Standard library only. `fixer/install.sh` copies it to ~/.lisa-fixer/app, outside the repo."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
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
        "FIXER_MAX_NEW": "5",          # investigations offered per run
        "FIXER_MAX_FIX": "3",          # fixes offered per run
        "FIXER_LIVE_CHECKS": "1",      # "0" = offline tests only (no API spend, not end to end)
        "FIXER_DEPLOY": "1",           # "0" = stop at a tested, pushed branch
        "FIXER_STALE_HOURS": "6",      # a case taken this long ago and never handed in is put back
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
LOG_SQL = REPO / ".claude" / "scripts" / "lisa-log-sql"
EVIDENCE_DIR = ".fixer"   # inside the case's checkout, and ignored by git

# What a change may touch. Anything else is refused before a test is spent on it.
ALLOWED_PATHS = ("Lisa flow/app/", "Lisa flow/tests/", "dispatcher/app.py", "dispatcher/tests/")

PLAN_KINDS = ("bug", "judge", "decision", "duplicate", "already_fixed", "no_action")
SENT_KINDS = ("bug", "judge", "decision")   # a plan he is shown
PARA_MIN, PARA_MAX = 20, 300                # one paragraph on his phone, in characters


class Stop(Exception):
    """This step said no: (step, detail). The change can be corrected and handed in again."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step, self.detail = step, detail


class Final(Stop):
    """It failed after reaching main. Main has been put back; the case is over, not retried:
    a branch whose merge was reverted cannot simply be merged again."""


# Changing one of these changes the judge's rubric, so its version has to move.
JUDGE_FILES = ("Lisa flow/app/review/prompt.py", "Lisa flow/app/review/taxonomy.py",
               "Lisa flow/app/review/timing.py")


# ── logging ─────────────────────────────────────────────────────────────────
def log(msg: str, **kw: Any) -> None:
    line = json.dumps({"t": datetime.now().isoformat(timespec="seconds"), "msg": msg, **kw}, default=str)
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
def _ssh() -> list[str]:
    return shlex.split(os.environ.get("FIXER_SSH_CMD", "")) or [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30",
        CFG["FIXER_SSH_HOST"]]


def remote_script(name: str, args: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run one of fixer/remote/*.sh on the droplet, fed over ssh."""
    script = (HERE / "remote" / name).read_text()
    return sh([*_ssh(), "bash", "-s", "--", *args], stdin=script, timeout=timeout, check=False)


def queue(*args: str, stdin: Optional[dict] = None, timeout: float = 120) -> dict:
    """One call to `app.fixdesk.cli` inside the Lisa container. The answer is the last line."""
    remote = "docker exec -i {c} python -m app.fixdesk.cli {a}".format(
        c=shlex.quote(CFG["FIXER_CONTAINER"]), a=" ".join(shlex.quote(a) for a in args))
    p = sh([*_ssh(), remote], stdin=json.dumps(stdin if stdin is not None else {}, default=str),
           timeout=timeout, check=False)
    lines = [ln for ln in p.stdout.strip().splitlines() if ln.startswith("{")]
    if not lines:
        raise RuntimeError(f"queue {args[0]}: no answer (exit {p.returncode}): {p.stderr[-300:]}")
    return json.loads(lines[-1])


def pull_queue() -> dict:
    info = {"host": socket.gethostname(), "ok": True, "runner": "desktop"}
    return queue("pull", info["host"], stdin=info)


# ── cases this Mac has taken and not yet handed in ───────────────────────────────
def _claims() -> dict:
    try:
        return json.loads((STATE / "claims.json").read_text())
    except (OSError, ValueError):
        return {}


def _set_claim(case_id: int, phase: Optional[str]) -> None:
    claims = _claims()
    if phase is None:
        claims.pop(str(case_id), None)
    else:
        claims[str(case_id)] = {"phase": phase, "at": time.time()}
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "claims.json").write_text(json.dumps(claims))


def release_stale(now: Optional[float] = None) -> list[int]:
    """A session that died mid-case leaves the case taken for good: nobody else may touch it.
    Whatever this Mac took more than FIXER_STALE_HOURS ago and never handed in goes back to the
    queue, where three such deaths set it aside (and an approved one is reported as failed)."""
    now = time.time() if now is None else now
    limit = float(CFG["FIXER_STALE_HOURS"]) * 3600
    back = []
    for cid, c in list(_claims().items()):
        if now - float(c.get("at") or 0) > limit:
            try:
                queue("release", cid, stdin={"error": f"the session that took it ({c.get('phase')}) never handed it in"})
            except Exception:
                continue
            _set_claim(int(cid), None)
            back.append(int(cid))
    return back


# ── git and the case's checkout ────────────────────────────────────────────────
def git(*args: str, cwd: Optional[Path] = None, check: bool = True, timeout: float = 180) -> str:
    return sh(["git", *args], cwd=cwd or REPO, check=check, timeout=timeout).stdout.strip()


def worktree(case_id: int) -> Path:
    return REPO / ".claude" / "worktrees" / f"fix-case-{case_id}"


def _ignore_evidence() -> None:
    """The evidence folder holds real chat text. It sits in the case's checkout so the session can
    read it, and git is told, for every checkout of this repository, never to see it."""
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir"))
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    have = exclude.read_text() if exclude.exists() else ""
    if f"/{EVIDENCE_DIR}/" not in have.splitlines():
        exclude.write_text(have + ("" if have.endswith("\n") or not have else "\n") + f"/{EVIDENCE_DIR}/\n")


def fresh_worktree(case_id: int) -> Path:
    """The case's own checkout of what is deployed (origin/main), with nothing else in it."""
    wt = worktree(case_id)
    if wt.exists():
        git("worktree", "remove", "--force", str(wt), check=False)
    git("worktree", "prune", check=False)
    git("fetch", "--quiet", "origin", check=False)
    git("worktree", "add", "--detach", str(wt), "origin/main")
    _ignore_evidence()
    (wt / EVIDENCE_DIR).mkdir(exist_ok=True)
    return wt


def remove_worktree(case_id: int) -> None:
    wt = worktree(case_id)
    if wt.exists():
        git("worktree", "remove", "--force", str(wt), check=False)


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


# ── the evidence ───────────────────────────────────────────────────────────────
def render_transcript(evidence: dict) -> str:
    lines = []
    for ln in evidence.get("transcript") or []:
        lines.append(f"[{ln.get('seq')}] {str(ln.get('ts'))[:19]} {ln.get('who')}: {ln.get('text')}")
    return "\n".join(lines) + "\n"


def fetch_evidence(loop_id: str, judge_version: str) -> dict:
    """A case's evidence, read from the production log through the read-only path. Used when this
    Mac holds no copy of it: the queue sends evidence with a new case only."""
    if not re.fullmatch(r"[\w.-]+", loop_id) or not re.fullmatch(r"[\w.-]+", judge_version):
        return {}
    q = f"""select json_build_object(
      'loop', (select row_to_json(l) from (select loop_id, chat_id, started_at, prompt_version, model, lang
               from lisa_log.loops where loop_id = '{loop_id}') l),
      'reviews', (select coalesce(json_agg(r order by r.seq), '[]') from (select seq, ts, turn_kind, domain,
               prompt_version, verdict, confidence, rationale, reply_text from lisa_log.reviews
               where loop_id = '{loop_id}' and judge_version = '{judge_version}') r),
      'findings', (select coalesce(json_agg(f), '[]') from (select r.seq, f.code, f.severity, f.evidence, f.harm,
               f.source from lisa_log.findings f join lisa_log.reviews r on r.id = f.review_id
               where r.loop_id = '{loop_id}' and r.judge_version = '{judge_version}' order by r.seq, f.id) f),
      'transcript', (select coalesce(json_agg(t order by t.seq), '[]') from (select seq, ts, who, text
               from lisa_log.events where loop_id = '{loop_id}' and stream = 'transcript') t))"""
    try:
        p = sh([str(LOG_SQL), "-A", "-t", q], timeout=120, check=False)
        # json_agg breaks its output over several lines: the answer is the whole of stdout.
        return json.loads(p.stdout.strip()) if p.returncode == 0 else {}
    except Exception:
        return {}


def lay_out(case: dict, wt: Path, recent: Optional[list] = None) -> dict:
    """Write the case's evidence into its checkout (and keep a copy outside the repo). Returns
    the paths the session reads."""
    cid = case["id"]
    keep = STATE / "cases" / str(cid)
    keep.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE / "cases", 0o700)
    evidence = case.get("evidence")
    if not evidence and (keep / "evidence.json").exists():
        evidence = json.loads((keep / "evidence.json").read_text())
    if not evidence:
        evidence = fetch_evidence(str(case.get("loop_id") or ""), str(case.get("judge_version") or ""))
    evidence = evidence or {}
    d = wt / EVIDENCE_DIR
    d.mkdir(exist_ok=True)
    files = {"evidence.json": json.dumps(evidence, indent=1, default=str),
             "transcript.txt": render_transcript(evidence)}
    if recent is not None:
        files["recent-cases.json"] = json.dumps(recent, indent=1, default=str)
    if case.get("plan"):
        files["approved-plan.json"] = json.dumps(case["plan"], indent=1, default=str)
    for name, text in files.items():
        (d / name).write_text(text)
        (keep / name).write_text(text)
    return {name: str(d / name) for name in files}


def load_evidence(case_id: int) -> dict:
    for f in (worktree(case_id) / EVIDENCE_DIR / "evidence.json", STATE / "cases" / str(case_id) / "evidence.json"):
        if f.exists():
            try:
                return json.loads(f.read_text())
            except ValueError:
                pass
    return {}


# ── checks ─────────────────────────────────────────────────────────────────────
def check_plan(plan: dict, recent_ids: set[int]) -> Optional[str]:
    """Why this plan cannot be handed in, or None."""
    kind = plan.get("kind")
    if kind not in PLAN_KINDS:
        return f"kind must be one of {', '.join(PLAN_KINDS)}; got {kind!r}"
    if kind == "duplicate":
        if plan.get("duplicate_of") not in recent_ids:
            return "duplicate_of must be the id of a case in recent-cases.json"
        return None
    if kind == "already_fixed" and not str(plan.get("fixed_by") or "").strip():
        return "already_fixed needs fixed_by: the short hash of the commit that fixed it"
    if kind in SENT_KINDS:
        for field in ("issue", "diagnosis", "fix"):
            n = len(" ".join(str(plan.get(field) or "").split()))
            if n < PARA_MIN:
                return f"{field} is missing or too short"
            if n > PARA_MAX:
                return f"{field} is {n} characters; the limit is {PARA_MAX}. It is read on a phone: shorten it"
    return None


def fix_hours(now: datetime) -> bool:
    """May fixes run now? Only from 06:00 to 21:59. The live date check books "two hours from
    now", and late at night that reads as "3 in the morning?": on 2026-11-28 at 01:00 it scored
    1/5 on production's own code. A fix judged then would be reported as failed when it is not.
    Investigations spend no live check and run at any hour."""
    return 6 <= now.hour < 22


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
                failing = "; ".join(ln.strip()[7:90] for ln in p.stdout.splitlines() if ln.strip().startswith("[FAIL]"))
                best = (ok, bad, p.returncode, failing or (p.stdout[-200:] + p.stderr[-200:]).strip())
                if bad == 0 and ok > 0 and p.returncode == 0:
                    break
            ok, bad, rc, tail = best
            name = gate.removesuffix(".py")
            if bad or not ok or rc != 0:
                why = f"{name} {ok}/{ok + bad}, twice ({tail[:160]})" if ok + bad else f"{name} did not run: {tail[-160:]}"
                raise Stop("the live checks", why)
            notes.append(f"{name} {ok}/{ok}")
    finally:
        subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"rm -rf {remote_dir}"],
                       capture_output=True, timeout=120)
    return ", ".join(notes)


def pick_gates(plan: dict, wt: Path, paths: list[str]) -> list[str]:
    """The live checks this change needs: the ones the plan named that exist, and always the
    date check when Lisa's own code changed. None for a change that leaves her code alone."""
    if not any(p.startswith("Lisa flow/app/") for p in paths):
        return []
    have = {t.name for t in (wt / "Lisa flow" / "tests").glob("live_*.py")} - {"live_key.py"}
    gates = [g if g.endswith(".py") else f"{g}.py" for g in (plan.get("gates") or [])]
    gates = [g for g in dict.fromkeys(gates) if g in have]
    # The judge's own files reach no live check: they are not on the reply path.
    reply_path = [p for p in paths if p.startswith("Lisa flow/app/") and not p.startswith("Lisa flow/app/review/")]
    if reply_path and "live_dates.py" in have and "live_dates.py" not in gates:
        gates.append("live_dates.py")
    return gates[:4]


# ── putting a change live ─────────────────────────────────────────────────────────
def _compose_get(text: str, key: str) -> Optional[str]:
    m = re.search(rf'^\s+{key}:\s*"?([^"\n]+?)"?\s*$', text, re.M)
    return m.group(1) if m else None


def _compose_set(text: str, key: str, value: str) -> str:
    new, n = re.subn(rf'^(\s+{key}:\s*).*$', lambda m: f'{m.group(1)}"{value}"', text, flags=re.M)
    if n != 1:
        raise Stop("the versions", f"evolution/docker-compose.yml has no single {key} line")
    return new


def version_bumps(wt: Path, paths: list[str], prompt_changed: bool, label: str) -> dict:
    """The version lines this change moves, written into the repo's compose file (the live
    override gets the same values at deploy). Her prompt version names the code that answered a
    session, and the judge only grades sessions of the current one, so a change to what she is
    told starts a new version. The judge's own version moves when its rubric does."""
    f = wt / "evolution" / "docker-compose.yml"
    text = f.read_text()
    out: dict = {"prompt": None, "judge": None}
    if prompt_changed or any(p.endswith("/prompt.py") and "/review/" not in p for p in paths):
        out["prompt"] = f"{datetime.now():%Y-%m-%d}-{label}"
        text = _compose_set(_compose_set(text, "PROMPT_VERSION", out["prompt"]),
                            "REVIEW_SCOPE_VERSION", out["prompt"])
    if any(p in JUDGE_FILES for p in paths):
        cur = _compose_get(text, "REVIEW_JUDGE_VERSION") or "v0"
        m = re.fullmatch(r"v(\d+)", cur)
        out["judge"] = f"v{int(m.group(1)) + 1}" if m else f"{cur}-{label}"
        text = _compose_set(text, "REVIEW_JUDGE_VERSION", out["judge"])
    if out["prompt"] or out["judge"]:
        f.write_text(text)
    return out


def services_for(paths: list[str]) -> list[str]:
    """Which containers a change has to restart. Tests alone restart nothing."""
    out = []
    if any(p.startswith("Lisa flow/app/") for p in paths):
        out.append("lisa")
    if "dispatcher/app.py" in paths:
        out.append("dispatcher")
    return out


def remote_deploy(sha: str, tag: str, services: list[str], bumps: dict, drill: bool = False) -> dict:
    """Put main at `sha` into production (fixer/remote/deploy.sh). `ok` is true only when the
    new containers are up; otherwise `detail` says why and `state` says what production is on."""
    args = [sha, tag, ",".join(services) or "-", bumps.get("prompt") or "-", bumps.get("judge") or "-"]
    if drill:
        args.append("drill-rollback")
    try:
        p = remote_script("deploy.sh", args, timeout=2400)
        lines = [ln for ln in p.stdout.strip().splitlines() if ln.startswith("{")]
        out = json.loads(lines[-1]) if lines else {"detail": f"no answer from the server (exit {p.returncode})"}
    except Exception as exc:
        out = {"detail": f"the connection to the server failed: {exc}"[:200]}
    out["ok"] = out.get("ok") in (True, "true")
    if out["ok"]:
        return out
    if out.get("rolled_back") == "true":
        out["state"] = "Production was put back on the previous image."
    elif out.get("rolled_back") == "false" and not out.get("swap"):
        out["state"] = "Production was not touched."
    else:
        out["state"] = "Production may be down or half-swapped: it needs a look now."
    return out


def revert_main(wt: Path, commits: list[tuple[str, bool]]) -> bool:
    """Take what could not be deployed back out of main, so main says what production runs.
    `commits` are (sha, is_merge), newest first. True when main was put back and the server's
    checkout followed."""
    try:
        git("fetch", "--quiet", "origin", cwd=wt)
        git("checkout", "-q", "--detach", "origin/main", cwd=wt)
        for sha, is_merge in commits:
            git("revert", *(["-m", "1"] if is_merge else []), "--no-edit", sha, cwd=wt)
        git("push", "-q", "origin", "HEAD:main", cwd=wt, timeout=300)
        back = git("rev-parse", "HEAD", cwd=wt)
        remote_script("deploy.sh", [back, "pull", "-", "-", "-"], timeout=300)
        return True
    except Exception as exc:
        log("main could not be put back", commits=[c[0][:7] for c in commits], error=str(exc)[:300])
        git("revert", "--abort", cwd=wt, check=False)
        return False


def land(wt: Path, branch: str, message: str, paths: list[str], prompt_changed: bool,
         label: str, tag: str) -> dict:
    """Merge the checked branch into main, push, and deploy. Before main is pushed a refusal is
    a Stop (correct it, hand it in again); after, it is Final (main is put back, the case ends)."""
    git("fetch", "--quiet", "origin", cwd=wt)
    tested_on = git("merge-base", "origin/main", branch, cwd=wt)
    moved = git("rev-parse", "origin/main", cwd=wt) != tested_on
    git("checkout", "-q", "--detach", "origin/main", cwd=wt)
    try:
        try:
            git("merge", "--no-ff", "--no-commit", branch, cwd=wt)
        except RuntimeError:
            git("merge", "--abort", cwd=wt, check=False)
            raise Stop("the merge", "main changed under this fix and the two no longer fit together")
        git("commit", "-q", "-m", message, cwd=wt)
        merge = git("rev-parse", "HEAD", cwd=wt)
        # The version lines go in a commit of their own. They sit next to each other in one file,
        # so inside the merge they would make every later undo of it a conflict.
        bumps = version_bumps(wt, paths, prompt_changed, label)
        landed = [(merge, True)]
        if bumps["prompt"] or bumps["judge"]:
            git("add", "-A", cwd=wt)
            git("commit", "-q", "-m", f"chore(lisa): versions for {label}", cwd=wt)
            landed.insert(0, (git("rev-parse", "HEAD", cwd=wt), False))
        if moved:   # the tests ran on yesterday's main: run them on what will actually ship
            ran, failed = offline_tests(wt)
            if failed:
                raise Stop("the offline tests", f"merged with today's main, {len(failed)} of {ran} files fail: "
                                                f"{', '.join(failed[:3])}")
        sha = git("rev-parse", "HEAD", cwd=wt)
        try:
            git("push", "-q", "origin", "HEAD:main", cwd=wt, timeout=300)
        except RuntimeError:
            raise Stop("the merge", "main moved while this was being checked; hand it in again")
    except Stop:
        git("checkout", "-q", "--force", branch, cwd=wt, check=False)
        raise

    services = services_for(paths)
    out = remote_deploy(sha, tag, services, bumps)
    if not out["ok"]:
        back = revert_main(wt, landed)
        git("checkout", "-q", "--force", branch, cwd=wt, check=False)
        main_state = "Main was put back too." if back else "MAIN STILL HAS THE CHANGE: revert it by hand."
        raise Final(str(out.get("step") or "the deploy"),
                    f"{str(out.get('detail') or 'it did not go live')[:120]}. {out['state']} {main_state}")
    git("checkout", "-q", "--force", branch, cwd=wt, check=False)
    return {"commit": merge, "deployed": sha, "services": services, "bumped": bumps,
            "rollback_tag": tag if services else None, "swap": out.get("swap")}


# ── the commands ───────────────────────────────────────────────────────────────
def cmd_pull(args: argparse.Namespace) -> dict:
    released = release_stale()
    pulled = pull_queue()
    if not pulled.get("enabled"):
        return {"ok": True, "enabled": False, "undo": [], "approved": [], "new": [],
                "note": "The fix loop is switched off on the droplet. Nothing to do."}
    daytime = fix_hours(datetime.now())
    approved = [{"id": c["id"], "issue": (c.get("plan") or {}).get("issue"), "note": c.get("approval_note")}
                for c in (pulled.get("approved") or [])[: int(CFG["FIXER_MAX_FIX"])]]
    new = [{"id": c["id"], "codes": c.get("codes")} for c in (pulled.get("new") or [])[: int(CFG["FIXER_MAX_NEW"])]]
    undo = [{"id": c["id"], "issue": (c.get("plan") or {}).get("issue")} for c in pulled.get("undo") or []]
    out = {"ok": True, "enabled": True, "undo": undo, "approved": approved if daytime else [], "new": new}
    if approved and not daytime:
        out["note"] = (f"{len(approved)} approved fix(es) wait for daytime: fixes run only from 06:00 to 21:59, "
                       "when the live date check can be trusted.")
    if released:
        out["put_back"] = released
    log("pulled", undo=len(undo), approved=len(out["approved"]), new=len(new), put_back=released)
    return out


def cmd_investigate(args: argparse.Namespace) -> dict:
    cid = args.id
    pulled = pull_queue()
    case = next((c for c in pulled.get("new") or [] if c["id"] == cid), None)
    if case is None:
        return {"ok": False, "error": f"case {cid} is not a new case in the queue"}
    if not queue("claim", str(cid), "investigating").get("ok"):
        return {"ok": False, "error": f"case {cid} could not be taken: something else holds it"}
    _set_claim(cid, "investigating")
    try:
        wt = fresh_worktree(cid)
        recent = [r for r in pulled.get("recent") or [] if r["id"] != cid]
        files = lay_out(case, wt, recent)
    except Exception as exc:
        queue("release", str(cid), stdin={"error": f"setting up: {exc}"[:300]})
        _set_claim(cid, None)
        raise
    log("investigating", case=cid)
    return {"ok": True, "case": cid, "loop_id": case.get("loop_id"), "codes": case.get("codes"),
            "checkout": str(wt), "evidence": files,
            "write_plan_to": str(wt / EVIDENCE_DIR / "plan.json"),
            "log_query": f"{LOG_SQL} \"select seq, stream, label, left(coalesce(text, payload::text), 600) "
                         f"from lisa_log.events where loop_id = '{case.get('loop_id')}' order by seq\""}


def cmd_plan(args: argparse.Namespace) -> dict:
    cid = args.id
    d = worktree(cid) / EVIDENCE_DIR
    try:
        plan = json.loads((d / "plan.json").read_text())
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"cannot read {d / 'plan.json'}: {exc}"}
    try:
        recent_ids = {r["id"] for r in json.loads((d / "recent-cases.json").read_text())}
    except (OSError, ValueError):
        recent_ids = set()
    bad = check_plan(plan, recent_ids)
    if bad:
        return {"ok": False, "error": bad, "fix_the_file_and_run_again": True}
    out = queue("plan", str(cid), stdin={"plan": plan, "session_id": None})
    if not out.get("ok"):
        return {"ok": False, "error": f"the queue refused the plan (is case {cid} still being investigated?)"}
    _set_claim(cid, None)
    (STATE / "cases" / str(cid)).mkdir(parents=True, exist_ok=True)
    (STATE / "cases" / str(cid) / "plan.json").write_text(json.dumps(plan, indent=1))
    remove_worktree(cid)   # a fix starts from a fresh checkout of whatever is deployed by then
    log("planned", case=cid, kind=plan.get("kind"), status=out.get("status"))
    sent = out.get("status") == "planned"
    return {"ok": True, "status": out.get("status"),
            "note": "Lisa will send him this plan." if sent else "Closed; nothing is sent to him."}


def cmd_fix(args: argparse.Namespace) -> dict:
    cid = args.id
    if not fix_hours(datetime.now()):
        return {"ok": False, "wait": True, "error": "Fixes run only from 06:00 to 21:59. Leave it for the next run."}
    pulled = pull_queue()
    case = next((c for c in pulled.get("approved") or [] if c["id"] == cid), None)
    if case is None:
        return {"ok": False, "error": f"case {cid} is not an approved case in the queue"}
    if not VENV_PY.exists():
        return {"ok": False, "error": f"no test environment at {VENV_PY}"}
    if not queue("claim", str(cid), "fixing").get("ok"):
        return {"ok": False, "error": f"case {cid} could not be taken: something else holds it"}
    _set_claim(cid, "fixing")
    branch = f"fix/case-{cid}"
    try:
        wt = fresh_worktree(cid)
        git("checkout", "-q", "-B", branch, "origin/main", cwd=wt)
        files = lay_out(case, wt)
    except Exception as exc:
        queue("release", str(cid), stdin={"error": f"setting up: {exc}"[:300]})
        _set_claim(cid, None)
        raise
    log("fixing", case=cid)
    return {"ok": True, "case": cid, "checkout": str(wt), "branch": branch,
            "plan": case.get("plan"), "his_note": case.get("approval_note"),
            "evidence": files, "offline_tests": str(OFFLINE_TESTS),
            "may_change": list(ALLOWED_PATHS)}


def _ship(cid: int, summary: str, prompt_changed: bool) -> dict:
    wt = worktree(cid)
    branch = f"fix/case-{cid}"
    if not wt.exists() or git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt) != branch:
        raise Stop("the checkout", f"{wt} is not on {branch}; take the case with `fix {cid}` first")
    plan = {}
    f = wt / EVIDENCE_DIR / "approved-plan.json"
    if f.exists():
        plan = json.loads(f.read_text())

    git("add", "-A", cwd=wt)
    paths = [p for p in git("diff", "--cached", "--name-only", "origin/main", cwd=wt).splitlines() if p]
    if not paths:
        raise Stop("the change", "no file is changed against production's code")
    stray = outside_scope(paths)
    if stray:
        raise Stop("the scope check", f"it touches files outside Lisa and the dispatcher: {', '.join(stray[:3])}")
    added = "\n".join(ln[1:] for ln in git("diff", "--cached", "-U0", "origin/main", cwd=wt).splitlines()
                      if ln.startswith("+") and not ln.startswith("+++"))
    leaks = contact_data(added, chat_names(load_evidence(cid)))
    if leaks:
        raise Stop("the contact-data check", f"the change contains {'; '.join(leaks[:3])}")
    if not any("/tests/" in p for p in paths):
        raise Stop("the test check", "no test file is changed: the fix must add a check that reproduces the mistake")

    ran, failed = offline_tests(wt)
    if failed:
        raise Stop("the offline tests", f"{len(failed)} of {ran} files fail: {', '.join(failed[:3])}")

    git("add", "-A", cwd=wt)
    if git("diff", "--cached", "--name-only", cwd=wt):
        git("commit", "-q", "-m", f"fix(lisa): {summary[:100]}",
            "-m", f"Fix case F{cid} of the improvement loop.\n\n{plan.get('diagnosis', '')}\n\n"
                  "Co-Authored-By: Claude <noreply@anthropic.com>", cwd=wt)
    commit = git("rev-parse", "HEAD", cwd=wt)

    checks = f"Offline tests pass ({ran} files)"
    gates = pick_gates(plan, wt, paths)
    if gates and CFG["FIXER_LIVE_CHECKS"] == "1":
        checks += "; live: " + live_checks(cid, wt, gates)
    elif gates:
        checks += "; live checks are switched off"
    checks += "."
    if prompt_changed and CFG["FIXER_DEPLOY"] != "1":
        checks += " Her prompt changed: bump the prompt version when it ships."

    git("push", "--force", "-u", "origin", f"{branch}:{branch}", cwd=wt, timeout=300)
    n = len(paths)
    said = f"{summary} {n} file{'s' if n != 1 else ''} changed.".strip()
    if CFG["FIXER_DEPLOY"] != "1":
        return {"status": "ready", "branch": branch, "commit": commit, "summary": said, "checks": checks}

    landed = land(wt, branch, f"Merge {branch}: {summary[:120]}", paths, prompt_changed,
                  label=f"fix-case-{cid}", tag=f"pre-fix-case-{cid}")
    if not landed["services"]:
        checks += " Tests only: nothing had to restart."
    checks += f" To take it back, reply undo F{cid}."
    return {"status": "shipped", "branch": branch, "summary": said, "checks": checks, **landed}


def cmd_ship(args: argparse.Namespace) -> dict:
    cid = args.id
    summary = " ".join((args.summary or "").split())
    if len(summary) < 10 or len(summary) > 160:
        return {"ok": False, "error": "--summary is one sentence of 10 to 160 characters: what she now does differently"}
    if len(" ".join((args.review or "").split())) < 10:
        return {"ok": False, "error": "--review is the second reader's one-sentence verdict; get it before shipping"}
    try:
        result = _ship(cid, summary, args.prompt_changed)
    except Final as stop:   # it reached main and could not go live: over, and he is told
        log("deploy failed", case=cid, step=stop.step, detail=stop.detail)
        queue("result", str(cid), stdin={"status": "failed", "failed_step": stop.step, "detail": stop.detail[:240]})
        _set_claim(cid, None)
        return {"ok": False, "step": stop.step, "detail": stop.detail, "final": True,
                "next": "The case is closed as failed and Lisa will tell him. Do not hand it in again."}
    except Stop as stop:
        log("ship refused", case=cid, step=stop.step, detail=stop.detail)
        return {"ok": False, "step": stop.step, "detail": stop.detail,
                "next": f"Correct it and run ship again, or give the case up with: fail {cid} --step ... --detail ..."}
    out = queue("result", str(cid), stdin=result)
    if not out.get("ok"):
        return {"ok": False, "error": f"{result['status']} on {result['branch']}, but the queue refused the report", **result}
    _set_claim(cid, None)
    log(result["status"], case=cid, branch=result["branch"], commit=result["commit"][:7])
    live = result["status"] == "shipped"
    return {"ok": True, **result,
            "note": "It is live. Lisa will tell him." if live else "Lisa will tell him. Nothing is deployed."}


def cmd_undo(args: argparse.Namespace) -> dict:
    """He asked for a fix that went live to be taken back: revert its merge on main, check the
    result still passes, deploy it. Needs no judgment, so the session only has to run it."""
    cid = args.id
    pulled = pull_queue()
    case = next((c for c in pulled.get("undo") or [] if c["id"] == cid), None)
    if case is None:
        return {"ok": False, "error": f"case {cid} is not waiting to be taken back"}
    result = case.get("result") or {}
    sha = str(result.get("commit") or "")

    def done(ok: bool, detail: str = "", **kw: Any) -> dict:
        out = queue("undone", str(cid), stdin={"ok": ok, "detail": detail[:240], **kw})
        log("undone" if ok else "undo failed", case=cid, detail=detail[:200])
        return {"ok": ok and bool(out.get("ok")), "detail": detail,
                "note": "Lisa will tell him it is out of production." if ok else "Lisa will tell him it is still live, and why."}

    if result.get("status") != "shipped" or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return done(False, "This fix was not deployed by the loop, so there is no recorded merge to take back.")
    wt = fresh_worktree(cid)
    if git("log", "origin/main", "--format=%H", f"--grep=This reverts commit {sha}", cwd=wt):
        remove_worktree(cid)
        return done(True, "It had already been reverted on main.")
    try:
        git("revert", "-m", "1", "--no-edit", sha, cwd=wt)
    except RuntimeError:
        git("revert", "--abort", cwd=wt, check=False)
        remove_worktree(cid)
        return done(False, "Later changes touch the same lines, so it has to be taken back by hand.")
    paths = [p for p in git("diff", "--name-only", "HEAD~1", "HEAD", cwd=wt).splitlines() if p]
    try:
        # A version that has been live is never reused: the undo gets versions of its own.
        bumps = version_bumps(wt, paths, bool((result.get("bumped") or {}).get("prompt")), f"undo-case-{cid}")
    except Stop as stop:
        remove_worktree(cid)
        return done(False, f"{stop.detail}.")
    landed = [(git("rev-parse", "HEAD", cwd=wt), False)]
    if bumps["prompt"] or bumps["judge"]:
        git("add", "-A", cwd=wt)
        git("commit", "-q", "-m", f"chore(lisa): versions for undo-case-{cid}", cwd=wt)
        landed.insert(0, (git("rev-parse", "HEAD", cwd=wt), False))
    ran, failed = offline_tests(wt)
    if failed:
        remove_worktree(cid)
        return done(False, f"With the change taken out, {len(failed)} of {ran} test files fail "
                           f"({', '.join(failed[:2])}). Nothing was changed.")
    back = git("rev-parse", "HEAD", cwd=wt)
    try:
        git("push", "-q", "origin", "HEAD:main", cwd=wt, timeout=300)
    except RuntimeError:
        remove_worktree(cid)
        return done(False, "Main moved while the undo was being checked. Nothing was changed; it is tried again next run.")
    out = remote_deploy(back, f"pre-undo-case-{cid}", services_for(paths), bumps)
    if not out["ok"]:
        main_back = revert_main(wt, landed)
        remove_worktree(cid)
        return done(False, f"The undo did not deploy: {str(out.get('detail'))[:100]}. {out['state']} "
                           f"{'Main was put back to match.' if main_back else 'MAIN AND PRODUCTION DISAGREE: it needs a look.'}")
    remove_worktree(cid)
    return done(True, commit=back, swap=out.get("swap"))


def cmd_fail(args: argparse.Namespace) -> dict:
    cid = args.id
    result = {"status": "failed", "failed_step": " ".join(args.step.split())[:80],
              "detail": " ".join(args.detail.split())[:240]}
    out = queue("result", str(cid), stdin=result)
    if not out.get("ok"):
        return {"ok": False, "error": f"the queue refused (is case {cid} being fixed?)"}
    _set_claim(cid, None)
    log("failed", case=cid, step=result["failed_step"])
    return {"ok": True, "note": "Lisa will tell him it was not fixed, and why."}


def cmd_release(args: argparse.Namespace) -> dict:
    cid = args.id
    out = queue("release", str(cid), stdin={"error": args.error[:300]})
    _set_claim(cid, None)
    log("released", case=cid, status=out.get("status"))
    return {"ok": bool(out.get("ok")), "status": out.get("status")}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="run.py", description="The fix loop's tool on the Mac.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull").set_defaults(fn=cmd_pull)
    for name, fn in (("investigate", cmd_investigate), ("plan", cmd_plan), ("fix", cmd_fix)):
        p = sub.add_parser(name)
        p.add_argument("id", type=int)
        p.set_defaults(fn=fn)
    p = sub.add_parser("undo")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_undo)
    p = sub.add_parser("ship")
    p.add_argument("id", type=int)
    p.add_argument("--summary", required=True)
    p.add_argument("--review", required=True)
    p.add_argument("--prompt-changed", action="store_true")
    p.set_defaults(fn=cmd_ship)
    p = sub.add_parser("fail")
    p.add_argument("id", type=int)
    p.add_argument("--step", required=True)
    p.add_argument("--detail", required=True)
    p.set_defaults(fn=cmd_fail)
    p = sub.add_parser("release")
    p.add_argument("id", type=int)
    p.add_argument("--error", required=True)
    p.set_defaults(fn=cmd_release)
    args = ap.parse_args(argv)
    try:
        out = args.fn(args)
    except Exception as exc:
        log("command failed", cmd=args.cmd, error=str(exc)[:400])
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
    print(json.dumps(out, indent=1, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
