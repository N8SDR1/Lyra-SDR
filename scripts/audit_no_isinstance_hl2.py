"""Capability-driven UI audit gate (v0.1 Phase 0 item 9).

Per ``docs/architecture/v0.1_rx2_consensus_plan.md`` §3.1.x item 9
(R3-9 Round 3 2026-05-11):

  > Zero ``isinstance(radio.protocol, HL2)`` checks in ``lyra/ui/``
  > (M-3).  Audit gate: pre-commit hook one-liner
  > ``! rg "isinstance.*HL2" lyra/ui/`` (R3-9 picks CI grep over
  > human-review).  Fails the commit if any match.

Why this exists
===============

v0.4 will add ANAN family support per operator decision 2026-05-03.
v0.1 / v0.2 / v0.3 stay HL2-only by scope, but the
**hardware-abstraction discipline** (CLAUDE.md §6.7) says: write
capability-driven UI now so the v0.4 retrofit is one new module per
family, not a months-long refactor of every UI call site.

The discipline:

* UI code MUST use ``radio.capabilities.*`` field reads (see
  ``lyra/protocol/capabilities.py``) instead of branching on the
  radio's protocol class identity.
* The pre-commit hook (`.git/hooks/pre-commit`) runs this script
  to catch violations BEFORE they land in the tree.

Usage
=====

From the repo root::

    python scripts/audit_no_isinstance_hl2.py

Exit codes:
* ``0`` -- no violations; clean.
* ``1`` -- one or more violations; commit should be rejected.

The script uses Python's stdlib only (no ripgrep dependency) so it
runs on a fresh Windows install without additional tooling.

Installation as a pre-commit hook
=================================

From the repo root, run::

    python scripts/install_audit_hook.py

That copies an executor into ``.git/hooks/pre-commit`` which calls
this script on every commit attempt.  Pre-existing hooks (if any)
are preserved as ``.git/hooks/pre-commit.local`` and still invoked.

CI integration
==============

If/when GitHub Actions is wired for Lyra, add a step::

    - name: Capability audit
      run: python scripts/audit_no_isinstance_hl2.py

to the workflow.  Same exit-code semantics work for CI gating.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


# Pattern matches the smell: ``isinstance(...some_expr..., HL2...)`` where
# "HL2" is any token starting with that prefix.  Wider than the plan's
# literal ``isinstance.*HL2`` so common phrasings like
# ``isinstance(radio.protocol, HL2_Stream)`` or ``isinstance(x, HL2Plus)``
# are also flagged.  Compiled once at module load.
PATTERN = re.compile(r"isinstance\s*\([^)]*\bHL2\w*")


# Patterns that are SAFE despite matching the literal text -- e.g. a
# string literal inside a docstring or comment explaining what NOT to
# write.  Add to this list sparingly; the right answer is usually to
# rephrase the comment, not add an exception.
SAFE_CONTEXTS = (
    # Module docstrings explaining the audit gate may quote the
    # forbidden pattern.  None today; placeholder for future
    # legitimate exceptions.
)


def find_violations(ui_root: Path) -> list[tuple[Path, int, str]]:
    """Walk ``ui_root`` recursively, return list of (path, line_no, line)
    tuples for each match of the forbidden pattern.

    Only ``.py`` files are scanned.  ``__pycache__`` directories are
    skipped.
    """
    violations: list[tuple[Path, int, str]] = []
    for path in sorted(ui_root.rglob("*.py")):
        # Skip caches.
        if "__pycache__" in path.parts:
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, start=1):
                    if not PATTERN.search(line):
                        continue
                    if line.strip() in SAFE_CONTEXTS:
                        continue
                    violations.append((path, lineno, line.rstrip()))
        except (OSError, UnicodeDecodeError) as e:
            print(f"WARN: could not scan {path}: {e}", file=sys.stderr)
    return violations


def main() -> int:
    # Resolve repo root: this script lives in scripts/; the ui tree
    # is at lyra/ui/ relative to repo root.
    repo_root = Path(__file__).resolve().parent.parent
    ui_root = repo_root / "lyra" / "ui"

    if not ui_root.is_dir():
        print(f"ERROR: {ui_root} does not exist", file=sys.stderr)
        return 1

    violations = find_violations(ui_root)

    if not violations:
        print(f"OK: no `isinstance(*, HL2*)` checks found in {ui_root}")
        print("     (capability-driven UI discipline holds; see "
              "lyra/protocol/capabilities.py).")
        return 0

    print(
        f"FAIL: {len(violations)} capability-discipline violation(s) "
        f"in lyra/ui/:",
        file=sys.stderr,
    )
    for path, lineno, line in violations:
        # Display as repo-relative for readability.
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            rel = path
        print(f"  {rel}:{lineno}:  {line}", file=sys.stderr)
    print(
        "\nFix: replace each `isinstance(radio.protocol, HL2...)` check\n"
        "with a `radio.capabilities.<field>` read.  See\n"
        "`lyra/protocol/capabilities.py` for the RadioCapabilities\n"
        "fields available.  Capability struct exists exactly to keep\n"
        "UI code hardware-agnostic for v0.4 multi-radio (ANAN family)\n"
        "expansion -- see CLAUDE.md §6.7 discipline #6.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
