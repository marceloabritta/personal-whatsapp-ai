"""Live checks and evals bill to the tests key, never to production's.

Every live_*.py calls require_tests_key() before it touches the model. It passes only when the
container was started with the droplet's tests env file AFTER the live service's env, which
replaces ANTHROPIC_API_KEY and REVIEW_ANTHROPIC_API_KEY with the tests key and sets
LISA_KEY_ROLE=tests:

    docker run --rm --env-file <(docker exec lisa_brain env) --env-file /opt/evolution/tests.env ...

Docker reads the env files in order and the later one wins. Without that file the run stops here,
so a test can never spend on the production key by accident."""
from __future__ import annotations

import os
import sys

TESTS_ENV = "/opt/evolution/tests.env"


def require_tests_key() -> None:
    if os.environ.get("LISA_KEY_ROLE") == "tests" and os.environ.get("ANTHROPIC_API_KEY"):
        return
    print(f"Refusing to run: this check bills to the tests key. Add --env-file {TESTS_ENV} after "
          "the live service's env (see tests/live_key.py).")
    sys.exit(2)
