"""Installing a package at runtime, through package-guard.

§0.27.4 item 10 asks a starter to install a package at runtime *through*
package-guard, "so the governed path is demonstrated rather than described".
The description is the easy half and it was already there; this is the half
that runs.

Four things about the governed path that are easy to get wrong, all of them
load-bearing here:

**1. The governed entry point is the `pip` BINARY on `PATH`.**
package-guard installs a PATH shim per supported tool and moves the real
binary aside, so `pip install X` reaches policy. This module therefore spawns
`pip` by name and lets PATH resolve it: PATH is what makes the call governed,
so anything that resolves pip some other way is not the same call. (The shim generator does have a
`--include-python` mode that wraps `python`/`python3` to catch exactly that,
and the custom-app image build does not pass it.) So this module spawns
`pip`, deliberately, and never `-m pip`.

**2. `pip3` is never shimmed, on purpose.** `pip` and `pip3` are the same
binary on most Python images, and wrapping both in one pass was measured to
leave a dangling symlink that crashes every install -- so `pip3` is refused at
the application layer instead. This app only ever spells it `pip`.

**3. The install lands in the user site, not in `site-packages`.** A hardened
application's rootfs is read-only, so pip reports *"Defaulting to user
installation because normal site-packages is not writeable"* and installs
under `$HOME`. That is why a runtime install can succeed at all here, and it
is worth knowing before wondering why the package is not where you expected.
The Node starters have no such fallback -- npm needs a writable directory with
its own `package.json`, which is why they seed one under a declared writable
path.

**4. A refusal is an ANSWER.** package-guard writes
`Blocked by Package Guard: <reason>` to stderr and exits non-zero. That is a
policy decision about the package, not a failure of this endpoint, and the two
are reported apart -- collapsing them would make "the policy said no" and "pip
fell over" the same event.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

#: Conservative, and checked BEFORE anything is spawned. The command is built
#: as a list and never goes through a shell, so this is defence in depth
#: rather than the only guard -- but a name is agent-supplied input and an
#: option smuggled into it (`--index-url ...`) would be an argument to pip, not
#: a package.
_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"  # the package name
    r"(?:\[[A-Za-z0-9,._-]+\])?"  # optional extras
    r"(?:(?:==|>=|<=|~=|!=|>|<)[A-Za-z0-9._*+!-]+)?$"  # optional version pin
)

#: The only package manager this application will invoke. An allowlist rather
#: than a denylist: `go`, `cargo`, `gem` and the rest are refused by
#: package-guard's own refusing shims, but an app that offers to run an
#: arbitrary binary name has handed the choice to whoever calls it.
ECOSYSTEM = "pip"

BLOCKED_MARKER = "Blocked by Package Guard"

DEFAULT_TIMEOUT_SECONDS = 300.0


class InvalidRequirement(ValueError):
    """The requested package name is not one this endpoint will pass on."""


def install(requirement: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run `pip install <requirement>` and report what package-guard decided."""
    if not _REQUIREMENT_RE.match(requirement):
        raise InvalidRequirement(
            f"{requirement!r} is not a bare package name with an optional "
            "version pin; this endpoint will not pass options through to pip"
        )

    # `pip`, resolved through PATH, so the shim gets it. Never
    # `[sys.executable, "-m", "pip", ...]`, which would bypass policy, and
    # never `shell=True`.
    completed = subprocess.run(  # noqa: S603 - fixed argv, validated operand
        ["pip", "install", requirement],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )

    stderr = completed.stderr or ""
    blocked = BLOCKED_MARKER in stderr
    return {
        "ecosystem": ECOSYSTEM,
        "requirement": requirement,
        "command": ["pip", "install", requirement],
        "exit_code": completed.returncode,
        # Three outcomes, not two. "policy refused this package" and "the
        # install itself failed" send an operator to different places.
        "verdict": (
            "blocked" if blocked else "installed" if completed.returncode == 0 else "failed"
        ),
        "reason": _blocked_reason(stderr) if blocked else "",
        "stdout_tail": (completed.stdout or "")[-2000:],
        "stderr_tail": stderr[-2000:],
    }


def _blocked_reason(stderr: str) -> str:
    for line in stderr.splitlines():
        if BLOCKED_MARKER in line:
            return line.strip()
    return BLOCKED_MARKER
