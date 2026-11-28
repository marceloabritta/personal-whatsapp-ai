"""The fix loop — a mistake the judge finds becomes a fix plan on his phone.

    judge files a finding          (app/review/, already there)
      → a case is opened           (desk.py, in this process)
      → his Mac investigates it    (fixer/ at the repo root: Claude Code, once a day)
      → she sends him the plan     (desk.py: issue, diagnosis, fix)
      → he answers "fix it"        (the dispatcher forwards it to POST /fix/reply)
      → his Mac makes the fix      (fixer/ again, on its next daily run)
      → she tells him how it went  (desk.py)

Nothing in this package calls a model. OFF unless FIX_LOOP_ENABLED."""
from .desk import FixDesk
from .store import FixStore

__all__ = ["FixDesk", "FixStore"]
