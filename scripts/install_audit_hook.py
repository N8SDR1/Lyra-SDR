"""Install the v0.1 Phase 0 capability-audit gate as a pre-commit hook.

Per ``docs/architecture/v0.1_rx2_consensus_plan.md`` §3.1.x item 9.

Run once per fresh clone:

    python scripts/install_audit_hook.py

This drops a ``.git/hooks/pre-commit`` that calls
``scripts/audit_no_isinstance_hl2.py`` on every commit attempt and
rejects the commit if any UI file contains an ``isinstance(*, HL2*)``
check.  Pre-existing local hooks (if any) are preserved as
``.git/hooks/pre-commit.local`` and still invoked first; their exit
codes propagate.

Idempotent: re-running prints "already installed" if the hook is
present, "upgraded" if it's an older version, "installed" on fresh
write.  Safe to wire into a setup-developer-environment workflow.

Why not the ``pre-commit`` framework?  Lyra is a single-developer
project; adding a pip dependency for one one-line audit gate isn't
worth the install complexity.  Plain ``.git/hooks/pre-commit`` is
zero-deps and well-understood.

If GitHub Actions CI ever lands for Lyra, add::

    - name: Capability audit
      run: python scripts/audit_no_isinstance_hl2.py

to the workflow.  Same exit-code semantics work; the local hook
catches violations BEFORE push, CI catches them as a backstop.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


HOOK_VERSION = "v0.1-phase0-2026-05-11"
HOOK_MARKER = f"# lyra-audit-hook-version: {HOOK_VERSION}"

HOOK_TEMPLATE = f"""#!/usr/bin/env sh
{HOOK_MARKER}
# Auto-installed by scripts/install_audit_hook.py (Lyra v0.1 Phase 0).
# Runs the capability-driven UI audit gate before every commit.
# Re-run scripts/install_audit_hook.py to upgrade after edits here.

set -e

# Run any pre-existing local hook first so we don't clobber operator
# customizations.  If they exit non-zero we abort before our audit.
if [ -x "$(dirname "$0")/pre-commit.local" ]; then
    "$(dirname "$0")/pre-commit.local"
fi

# Lyra capability-driven UI audit (consensus-plan §3.1.x item 9).
# Reject commits that introduce `isinstance(radio.protocol, HL2*)`
# checks in lyra/ui/ -- those should be `radio.capabilities.*` reads
# instead.  See scripts/audit_no_isinstance_hl2.py for details.
python scripts/audit_no_isinstance_hl2.py
"""


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    hooks_dir = repo_root / ".git" / "hooks"
    hook_path = hooks_dir / "pre-commit"

    if not hooks_dir.is_dir():
        print(
            f"ERROR: {hooks_dir} not found.  Is this a git checkout?",
            file=sys.stderr,
        )
        return 1

    status = "installed"
    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8", errors="replace")
        if HOOK_MARKER in existing:
            print(f"OK: hook already installed (version {HOOK_VERSION}).")
            return 0
        if "lyra-audit-hook-version:" in existing:
            status = "upgraded"
        else:
            # Foreign hook -- preserve it as pre-commit.local so our
            # installed wrapper invokes it first.
            local_path = hooks_dir / "pre-commit.local"
            if not local_path.exists():
                local_path.write_text(existing, encoding="utf-8")
                # Preserve executable bit if it was set.
                if os.name != "nt":
                    local_path.chmod(local_path.stat().st_mode | stat.S_IEXEC)
                print(
                    f"Preserved existing hook as "
                    f".git/hooks/pre-commit.local "
                    f"(our installed hook will invoke it first)."
                )
            status = "wrapped existing hook"

    hook_path.write_text(HOOK_TEMPLATE, encoding="utf-8", newline="\n")
    # chmod +x for POSIX -- on Windows git emulates this via the
    # filemode config and the actual flag is ignored at the FS level.
    if os.name != "nt":
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)

    print(f"OK: pre-commit hook {status} at {hook_path}")
    print("    Next commit will block if `isinstance(*, HL2*)` is")
    print("    introduced anywhere under `lyra/ui/`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
