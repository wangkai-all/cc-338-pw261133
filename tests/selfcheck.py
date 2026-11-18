"""End-to-end self-test using in-process servers and a temporary database.

Run:  python3 run.py --check
"""

from __future__ import annotations

import traceback

CHECKS: list[tuple[str, object]] = []


def check(name: str):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn

    return deco


def run_selfcheck(cfg) -> int:
    from tests.harness import Harness
    from tests import selfcheck_cases  # noqa: F401  (registers checks)
    from tests import policy_units  # noqa: F401  (registers unit checks)

    failures = 0
    h = Harness()
    try:
        for name, fn in CHECKS:
            try:
                h.reset()
                fn(h)
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
            except Exception:
                failures += 1
                print(f"  ERROR {name}")
                traceback.print_exc()
    finally:
        h.close()
    total = len(CHECKS)
    print(f"\n{total - failures}/{total} checks passed")
    return 0 if failures == 0 else 1
