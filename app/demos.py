"""Demonstration endpoints' plumbing: Package Guard and SecureProxy egress.

Neither of these is something an agent framework gives you. They exist so a
customer evaluating AgentBox can trigger the two controls that previously had
no path at all from inside a running application -- package policy and egress
policy -- and see the platform's own verdict, in the platform's own words.

Both are deliberately thin: they run the real thing and report what came back.
Neither interprets policy, and neither has a "pretend" mode.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from base64 import b64encode
from pathlib import Path
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Package Guard
# ---------------------------------------------------------------------------

#: Package managers this app can be asked to drive, as an ALLOWLIST. `pip` is
#: the spelling to use: on most Python images the various pip aliases are one
#: binary, and this endpoint accepts the name Package Guard installs its shim
#: for rather than every alias of it. Wrapping more than one of them in a
#: single pass was measured to leave a dangling symlink that crashed every
#: install (2026-08-20), which is why there is one governed name and not
#: several.
_MANAGERS = ("pip", "npm")

#: A package name, an optional npm scope, and an optional version specifier.
#: The install runs WITHOUT a shell (no `sh -c`), so this is defence in depth
#: rather than the only thing between a name and a command -- but a name that
#: cannot be a package should be refused before Package Guard is asked about it.
#:
#: Both optional halves are real, not hypothetical: a first version of this
#: accepted neither, which refused `@scope/name` and `docopt==0.6.2` -- an
#: ordinary scoped npm package and an ordinary pinned pip install. A scope must
#: begin with an alphanumeric so `@../evil/x` cannot pass as one.
_PACKAGE_RE = re.compile(
    r"^(?:@[A-Za-z0-9][A-Za-z0-9._-]{0,63}/)?"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
    r"(?:[@=<>!~^][A-Za-z0-9._*+!=<>~^-]{0,63})?$"
)

#: npm needs a project root it can write. /app is an image layer under a
#: read-only rootfs, and npm walks UP to the nearest package.json, so without
#: one here it targets /app and fails with a misleading ENOENT about a
#: platform-specific package. /home/agent/scratch is declared in filesystem_writable.
_NPM_ROOT = Path(os.environ.get("APP_INSTALL_ROOT", "/home/agent/scratch"))

#: Substrings meaning Package Guard FAILED rather than DECIDED. They matter
#: because they are invisible in the verdict: the daemon wraps every exception
#: as "Blocked by Package Guard: {exc}", so an infrastructure failure reads
#: word for word like a policy block. Every one of these was a real failure
#: seen live on 2026-08-29, and each would pass a naive keyword check.
_INFRA_FAILURES = (
    "execv",
    "staticx",
    "daemon unavailable",
    "daemon returned no final response",
    "resolution failed",
    "error loading shared library",
    "unsupported resolver tool",
    "connection refused",
)


def _verdict(output: str) -> str:
    """Which decision Package Guard rendered.

    Keys off the exact prefixes policy.py emits rather than loose keywords:
    "approval" appears both in a genuine HOLD and in "Blocked by ...: approval
    request was rejected", so keyword matching cannot tell the two apart.
    """
    lowered = output.lower()
    if "blocked by package guard" in lowered:
        return "block"
    if "held by package guard" in lowered:
        return "hold"
    if "allowed by package guard" in lowered:
        return "allow"
    return "none"


def _ensure_npm_root() -> None:
    _NPM_ROOT.mkdir(parents=True, exist_ok=True)
    manifest = _NPM_ROOT / "package.json"
    if not manifest.is_file():
        manifest.write_text('{"name":"agent-scratch","private":true}', encoding="utf-8")


#: Where Package Guard writes its OWN audit events, in the order it looks
#: (`package_guard.audit.AuditSink._candidate_audit_paths`). This is the only
#: place an ALLOW is recorded at all: the guard prints a full banner for a
#: refusal and **prints nothing for an allow**, so stdout cannot tell "allowed"
#: from "ungoverned". Reading the sink is what makes the two distinguishable.
#:
#: Container-local and on a tmpfs: the app has no audit peer (§0.40.27), and
#: the controller collects these out on its reconciliation tick. So this is
#: the app reading its own record, not a privileged lookup.
#: Read at CALL time, not import time. A module-level tuple would freeze
#: whatever `AGENTBOX_AUDIT_LOG` held when this module was first imported --
#: the same shape as the local-MCP staging root that froze to prod's path
#: under `agentbox --env dev`. It also made the behaviour untestable: a test
#: that sets the variable after import would be silently ignored, and would
#: then read whatever real sink the developer's machine happens to have.
def _sink_candidates() -> tuple[str, ...]:
    return (
        os.environ.get("AGENTBOX_AUDIT_LOG", "").strip(),
        "/tmp/agentbox-audit/events.jsonl",
    )


#: How long to wait for the guard to record a HOLD before giving up on seeing
#: one. Measured on a real install 2026-09-21: `approval_required` was written
#: at t+11s and the request appeared in the admin queue at t+17s.
_HOLD_DETECT_SECONDS = 45.0


def _guard_sink() -> Path | None:
    """Where the guard's record WILL be, whether or not it exists yet.

    Existence is deliberately not required. The sink is created by the guard's
    first write, so on a freshly recreated container it is absent until the
    first governed install -- and requiring it here made that first install
    the one case that could not be read. Measured on a real stack 2026-09-21:
    `tabulate` was allowed, the sink recorded `evaluated_request | allow |
    score 15`, and the endpoint still reported `none` because the path had
    been resolved to None a second earlier.

    The readers below treat an absent file as "nothing recorded", which is the
    same answer they give for an app with no guard at all -- correct in both
    cases, and it costs one `is_file()` check rather than a whole branch.
    """
    for raw in _sink_candidates():
        if raw:
            return Path(raw).expanduser()
    return None


def _sink_events_after(path: Path | None, offset: int) -> list[dict]:
    """The guard's events written after *offset* bytes, oldest first.

    Correlated by POSITION rather than by package name, because an event
    carries neither: its `subject` is a request digest and the name appears
    only inside a human-readable message. A byte offset taken immediately
    before the install is exact, needs no parsing, and cannot match a
    different request that happens to name the same package.
    """
    if path is None:
        return []
    try:
        # BINARY, then decode. `offset` is a byte count from `stat().st_size`,
        # and `TextIOWrapper.seek` takes an opaque cookie from its own
        # `tell()` -- not a byte offset. Passing one happens to work for plain
        # ASCII and is undefined in general, which is the kind of thing that
        # holds until a log line contains a non-ASCII package name.
        with path.open("rb") as handle:
            handle.seek(offset)
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    events: list[dict] = []
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _sink_size(path: Path | None) -> int:
    try:
        return path.stat().st_size if path else 0
    except OSError:
        return 0


def _verdict_from_sink(events: list[dict]) -> tuple[str, dict]:
    """The guard's own verdict, and the metadata it recorded with it.

    Preferred over the stdout heuristic wherever a sink exists, and returns
    `("", {})` when it has nothing to say so the caller can fall back rather
    than treat silence as a verdict.
    """
    verdict = ""
    metadata: dict = {}
    for event in events:
        action = str(event.get("action") or "")
        outcome = str(event.get("outcome") or "")
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        if action == "approval_required":
            # Terminal for our purposes: a human now owns this decision.
            meta = details.get("metadata")
            return "hold", {
                "score": (meta or {}).get("score"),
                "approval_ttl_seconds": (meta or {}).get("approval_ttl_seconds"),
                "request_digest": details.get("request_digest"),
                "reason": str(event.get("message") or ""),
            }
        if action == "evaluated_request":
            verdict = {"allow": "allow", "block": "block"}.get(outcome, verdict)
            metadata = {
                "score": (details.get("metadata") or {}).get("score"),
                "reason": str(event.get("message") or ""),
            }
    return verdict, metadata


def _reap_later(process: subprocess.Popen, ttl: float | None) -> None:
    """Wait out a held install in the background, so nothing is orphaned.

    The child is deliberately left alive -- see the hold branch below -- but it
    must not outlive the decision it is waiting for. The guard expires the
    request after `approval_ttl_seconds`, at which point the command fails on
    its own; this only guarantees the process is collected if it does not.

    A daemon thread, so it never holds the app open on shutdown.
    """
    import threading

    limit = float(ttl or 300) + 30.0

    def _wait() -> None:
        try:
            process.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.communicate(timeout=15)
            except Exception:  # noqa: BLE001 - nothing left to report it to
                pass
        except Exception:  # noqa: BLE001 - the process is gone either way
            pass

    threading.Thread(
        target=_wait, name="package-guard-hold-reaper", daemon=True
    ).start()


def _already_installed(package: str, manager: str) -> bool:
    """Whether *package* is already there, so there is nothing to install.

    Asked BEFORE installing, because Package Guard judges every install request
    on its own. Measured on the VM 2026-09-24: an operator approved a held
    install, it completed, and the agent's next "install it" raised a NEW hold
    -- so the agent told the user the package was not installed yet, while it
    was. Skipping an install that would change nothing bypasses no control.

    `pip show` answers for a directory package and a single-module one alike;
    npm is checked on disk, never with `npm ls`, which fails for unrelated
    problems in the tree.
    """
    if manager == "npm":
        return (_NPM_ROOT / "node_modules" / package).is_dir()
    try:
        shown = subprocess.run(  # noqa: S603 - bare argv, validated name, no shell
            [manager, "show", package],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return shown.returncode == 0


def install_package(package: str, manager: str) -> dict:
    """Run a real, Package Guard-governed install and report the verdict.

    Invoked as a bare argv (`["pip", "install", pkg]`), NOT through a shell and
    NOT as `python -m pip`. Both alternatives miss the point: `python -m pip`
    reaches pip's module entry point directly and never touches the PATH shim
    that IS the enforcement, and a login shell (`sh -lc`) re-initialises PATH
    from the image profile and drops the shim directory entirely -- confirmed
    live 2026-08-29, where a login shell saw no npm at all. This process
    inherits the container's ENV PATH, which has the shim directory on it, so
    a bare name resolves to the wrapper exactly as an agent's own call would.
    """
    package = (package or "").strip()
    manager = (manager or "").strip().lower()
    if manager not in _MANAGERS:
        raise ValueError(f"manager must be one of {', '.join(_MANAGERS)}")
    if not _PACKAGE_RE.match(package):
        raise ValueError("package is not a valid package name")

    if _already_installed(package, manager):
        return {
            "package": package,
            "manager": manager,
            "verdict": "installed",
            "exit_code": 0,
            "output": f"{package} is already installed; there is nothing to add.",
            "already_installed": True,
        }

    cwd = None
    if manager == "npm":
        _ensure_npm_root()
        cwd = str(_NPM_ROOT)

    # Where the guard's own record starts for THIS install, in bytes.
    sink = _guard_sink()
    offset = _sink_size(sink)

    try:
        # Popen, not run(): a HOLD blocks the child until an operator decides,
        # and the point is to report the hold rather than sit through it.
        process = subprocess.Popen(  # noqa: S603 - bare argv, validated name, no shell
            [manager, "install", package],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{manager} is not installed in this image, so Package Guard's shim has "
            f"nothing to wrap: {exc}"
        ) from exc

    # Watch the guard's record while the child runs. A HOLD is written within
    # seconds (measured: `approval_required` at t+11s, visible in the admin
    # queue at t+17s), while the child itself would block for the full
    # `approval_ttl_seconds` -- 300, the same number the old `timeout=300`
    # used, so the install gave up at the exact moment the request expired and
    # the operator was never given a window at all. Reporting the hold early
    # hands them nearly all of it.
    held: dict | None = None
    deadline = time.monotonic() + _HOLD_DETECT_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        verdict_now, metadata_now = _verdict_from_sink(_sink_events_after(sink, offset))
        if verdict_now == "hold":
            held = metadata_now
            break
        time.sleep(1.0)

    if held is not None:
        # Report the hold, and LEAVE THE CHILD RUNNING.
        #
        # Terminating it was the obvious thing and it was wrong: the pending
        # request belongs to the blocked command, so killing the command
        # withdraws it. Measured on a real stack 2026-09-21 -- with the child
        # still blocked the admin queue showed `pending: 1`; with it
        # terminated a fresh hold showed `pending: 0`, twice, and the guard
        # recorded `approval_command_terminated: approval command was
        # terminated before a decision`. Reporting a hold while cancelling the
        # thing an operator is being told to decide is worse than the 300s
        # timeout this replaced, because it reads as actionable and is not.
        #
        # So the endpoint returns now and the install keeps waiting. Approving
        # it in the console lets the real install proceed; running this again
        # afterwards reads the verdict back out of the sink.
        _reap_later(process, held.get("approval_ttl_seconds"))
        ttl = held.get("approval_ttl_seconds")
        return {
            "package": package,
            "manager": manager,
            "verdict": "hold",
            # Still running on purpose, so there is no exit code yet. `None`
            # rather than a placeholder: a number here would be a claim about
            # an outcome that has not happened.
            "exit_code": None,
            "output": (
                f"{held.get('reason') or 'Held by Package Guard'}\n\n"
                f"Score {held.get('score')}. Request "
                f"{str(held.get('request_digest') or '')[:16]} is waiting in "
                f"Security -> Package approvals"
                + (f" for {ttl}s" if ttl else "")
                + ". The install is still blocked on that decision rather than "
                "cancelled -- approve it there and it proceeds, then call this "
                "again to read the verdict back."
            ),
            "infra_failure": None,
        }

    try:
        stdout, stderr = process.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError(
            f"{manager} install of {package!r} did not finish within 300s, and "
            "Package Guard recorded no approval request -- so this is a slow "
            "install rather than a held one"
        ) from None

    output = f"{stdout}\n{stderr}".strip()
    lowered = output.lower()
    infra = [needle for needle in _INFRA_FAILURES if needle in lowered]

    # The guard's OWN record first, stdout only as a fallback.
    #
    # An ALLOW prints nothing, so the stdout heuristic returns "none" for a
    # perfectly governed install -- and the console then renders "No control
    # here" on a protected app, which under-reports the product in the one
    # direction that matters. Measured live 2026-09-21: `six` installed
    # through the shim, `pip show` confirmed it, and the endpoint reported
    # `verdict: none` while the sink held `evaluated_request`.
    sink_verdict, sink_metadata = _verdict_from_sink(_sink_events_after(sink, offset))
    verdict = sink_verdict or _verdict(output)
    if sink_verdict:
        reason = sink_metadata.get("reason") or ""
        score = sink_metadata.get("score")
        note = f"Package Guard: {verdict}"
        if score is not None:
            note += f" (score {score})"
        if reason:
            note += f" -- {reason}"
        output = f"{note}\n\n{output}" if output else note

    return {
        "package": package,
        "manager": manager,
        "verdict": verdict,
        "exit_code": process.returncode,
        # Bounded: an ALLOWed install prints a full resolution log, and this
        # is rendered in an admin's browser.
        "output": output[-4000:],
        # A verdict reached by a broken Package Guard fails CLOSED, which is the
        # safe direction but also the deceptive one -- it looks exactly like a
        # policy block. Name it rather than let it read as enforcement.
        "infra_failure": infra or None,
    }


# ---------------------------------------------------------------------------
# SecureProxy egress
# ---------------------------------------------------------------------------

_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


def probe_egress(host: str, port: int = 443) -> dict:
    """Ask SecureProxy's forward proxy to open a tunnel, and report its answer.

    A RAW CONNECT, reading the proxy's own status line, rather than an ordinary
    HTTPS request. Two reasons, both learned the hard way:

    * A denied CONNECT reaches urllib as a generic OSError, not an HTTPError --
      the proxy's status is lost, so a probe built on urlopen reports the same
      opaque failure whether the destination was refused by policy or the proxy
      itself was broken. Reading the status line keeps "403 Forbidden" distinct
      from "cannot reach the proxy at all", which is the entire question here.
    * The credential has to be spelled `Proxy-Authorization` exactly. urllib's
      ProxyHandler adds it via `add_header`, which capitalises it to
      `Proxy-authorization`, and only the exact spelling is forwarded into the
      tunnel -- so every request comes back 407. That silently disabled
      Package Guard's own intel fetches until it was found (2026-08-29).
    """
    host = (host or "").strip()
    if not _HOST_RE.match(host):
        raise ValueError("host must be a bare hostname, without scheme or path")
    port = int(port)
    if not (1 <= port <= 65535):
        raise ValueError("port must be between 1 and 65535")

    raw = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "").strip()
    if not raw:
        return {
            "host": host,
            "port": port,
            "proxied": False,
            "status": 0,
            "reason": "no HTTPS_PROXY is set, so this app has no egress route at all",
        }

    proxy = urlsplit(raw)
    if not proxy.hostname or not proxy.port:
        return {
            "host": host,
            "port": port,
            "proxied": False,
            "status": 0,
            "reason": f"unparseable proxy url: {raw!r}",
        }

    lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
    if proxy.username is not None:
        token = b64encode(
            f"{proxy.username or ''}:{proxy.password or ''}".encode()
        ).decode()
        lines.append(f"Proxy-Authorization: Basic {token}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode()

    try:
        sock = socket.create_connection((proxy.hostname, proxy.port), timeout=25)
    except OSError as exc:
        return {
            "host": host,
            "port": port,
            "proxied": False,
            "status": 0,
            "reason": f"cannot reach the proxy at {proxy.hostname}:{proxy.port} -- {exc}",
        }

    try:
        sock.sendall(request)
        sock.settimeout(25)
        buffer = b""
        while b"\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
    except OSError as exc:
        return {
            "host": host,
            "port": port,
            "proxied": True,
            "status": 0,
            "reason": f"proxy connection failed mid-request -- {exc}",
        }
    finally:
        sock.close()

    if not buffer:
        return {
            "host": host,
            "port": port,
            "proxied": True,
            "status": 0,
            "reason": "the proxy closed the connection without a status line",
        }

    status_line = buffer.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    bits = status_line.split(None, 2)
    if len(bits) < 2 or not bits[1].isdigit():
        return {
            "host": host,
            "port": port,
            "proxied": True,
            "status": 0,
            "reason": f"unparseable proxy status line: {status_line!r}",
        }

    status = int(bits[1])
    reason = bits[2] if len(bits) > 2 else ""
    return {
        "host": host,
        "port": port,
        "proxied": True,
        "status": status,
        "reason": reason,
        # 200 means the tunnel opened: this destination is allowed by the
        # app's current egress policy. 403 is the policy refusing it. 407
        # means the credential did not survive, which is a wiring fault, not
        # a policy decision.
        "allowed": status == 200,
    }
