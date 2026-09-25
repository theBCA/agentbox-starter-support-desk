"""What this application can observe about its own confinement.

`GET /runtime-info` answers one question -- *is this app running inside
AgentBox, and which controls are actually wired* -- and the UI at `/` renders
the answer as its banner. That makes the banner DERIVED rather than
configured, which is the only reason it is worth showing: a demo page that
said "protected" because a build flag said so would be a claim, not evidence.

Three rules this module follows, each earned elsewhere in the product.

**1. Report the evidence, never the value.** Every proxy variable an app
receives carries the app's own virtual key as the credential
(`http://<virtual-key>:@kobil-secureproxy:8101`), so this reports THAT a
variable is set and never what it contains. `egress.proxy_configuration()`
already returns booleans for exactly this reason.

**2. A control this app cannot see is `null`, not `false`.** Some controls
leave no trace inside the container: gVisor, the Hardening Check, the audit
trail and the skill scanner all act on the app from outside it. Saying `false`
about them would be a fabricated finding -- the same mistake as rendering a
security service's silence as *Not configured*. They are listed with
`wired: None` and a detail saying where to look instead.

**3. The managed test is the SAME one the model path uses.** `managed` below
is `configure_secureproxy`'s own rule, so the banner and the fail-closed
refusal cannot disagree. If they were derived separately, an app could show a
green banner and refuse every model call, or the reverse.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Where the wrap puts Package Guard inside an application image
#: (`custom_app_package_guard.CONTAINER_PACKAGE_GUARD_DIR`). The shim
#: directory is what goes on `PATH`; the runtime binary is what a shim calls.
_PACKAGE_GUARD_DIR = Path("/app/package-guard")
_PACKAGE_GUARD_SHIM_DIR = _PACKAGE_GUARD_DIR / "shims"
_PACKAGE_GUARD_RUNTIME = _PACKAGE_GUARD_DIR / "package-guard-runtime"


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def managed() -> bool:
    """Whether AgentBox provisioned this container.

    Deliberately identical to `_secureproxy.configure_secureproxy`'s test, so
    the two cannot disagree. Neither signal is forgeable from inside the app in
    any way that matters: both are written by the platform's own generator.
    """
    return bool(_env("AGENTBOX_APP_ID")) or _env("KOBIL_SECUREPROXY_REQUIRED") == "1"


def _package_guard_state() -> tuple[bool | None, str]:
    """Is the governed `pip` the one a bare `pip install` would reach?

    Both halves are checked, because either alone is misleading. The runtime
    binary present with no shim on `PATH` means an ungoverned install; a shim
    on `PATH` with no runtime behind it is the shape that fails closed and
    reads word for word like a policy block (`demos._INFRA_FAILURES` exists
    because of it).
    """
    resolved = shutil.which("pip")
    shimmed = bool(resolved) and Path(resolved).parent == _PACKAGE_GUARD_SHIM_DIR
    runtime_present = _PACKAGE_GUARD_RUNTIME.exists()
    if shimmed and runtime_present:
        return True, f"`pip` on PATH resolves to {resolved}, and the runtime is present"
    if shimmed and not runtime_present:
        return (
            False,
            f"a shim is on PATH ({resolved}) but {_PACKAGE_GUARD_RUNTIME} is "
            "missing -- installs will fail closed, which looks like a policy "
            "block and is not one",
        )
    if resolved:
        return False, f"`pip` on PATH resolves to {resolved}, which is not a shim"
    return None, "no `pip` on PATH in this image, so there is nothing to govern"


#: The env var each agent SDK reads its credential from. Keyed on the SDK, not
#: on the provider, because an SDK reads a fixed name and will not read
#: another -- a claude app routed to OpenRouter still receives
#: `ANTHROPIC_API_KEY` and never learns which provider it is on.
_SDK_KEY_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY")


def raw_provider_key_vars() -> list[str]:
    """Credential variables holding something OTHER than this app's virtual key.

    The distinction is the product's central claim and it is worth measuring
    rather than asserting. A managed app's SDK variable IS set --
    `configure_secureproxy` binds it to the SecureProxy virtual key before the
    SDK starts -- so "is ANTHROPIC_API_KEY set" answers the wrong question. A
    value equal to `KOBIL_SECUREPROXY_API_KEY` is a proxy credential scoped to
    this app, revocable, and worth nothing at the vendor. Anything else is a
    real provider key the application can read, print, or hand to a model.

    Returns NAMES, never values.
    """
    virtual = _env("KOBIL_SECUREPROXY_API_KEY")
    held: list[str] = []
    for name in _SDK_KEY_VARS:
        value = _env(name)
        if value and (not virtual or value != virtual):
            held.append(name)
    return held


def self_facts() -> str:
    """What the agent may truthfully say about its own setup, as prose.

    Goes into the system prompt so the answer to "which AI key are you using
    and what happens if it leaks" is grounded in the same facts the banner is,
    rather than in whatever the model assumes about apps in general. Names
    of variables, never values -- the whole point is that a managed app never
    holds a real key to repeat.
    """
    raw = raw_provider_key_vars()
    proxied = bool(_env("KOBIL_SECUREPROXY_URL") and _env("KOBIL_SECUREPROXY_API_KEY"))
    bridged = bool(
        _env("MANAGED_MCP_BRIDGE_URL") and _env("AGENTBOX_CUSTOM_APP_MCP_TOKEN")
    )
    guard_wired, _evidence = _package_guard_state()

    lines = [
        "Facts about your own setup, for when you are asked. Say only these; "
        "do not guess beyond them."
    ]
    if raw:
        lines.append(
            "You hold a real AI key in your environment "
            f"({', '.join(raw)}). Nothing stands between you and the AI "
            "company. If that key leaked, whoever has it could spend the "
            "company's money and read its traffic until someone noticed and "
            "replaced it."
        )
    elif proxied:
        lines.append(
            "Your calls to the AI go through AgentBox. You were given a pass "
            "that belongs to this application alone; you do not hold, and "
            "cannot read, the company's real AI key. If your pass leaked it "
            "could be switched off on its own, and it is worth nothing at "
            "the AI company."
        )
    else:
        lines.append(
            "You hold no AI key in your environment and no AgentBox pass either."
        )
    lines.append(
        "The company's systems are reached through AgentBox, which checks "
        "each call and keeps a record of it."
        if bridged
        else "You have no connection to the company's systems through AgentBox."
    )
    if guard_wired is True:
        lines.append("Software you add is checked by AgentBox before it is installed.")
    elif guard_wired is False:
        lines.append("Nothing checks software you add before it is installed.")
    return "\n".join(lines)


def controls() -> list[dict]:
    """One row per control, in the order the UI shows them.

    `wired` is tri-state on purpose: True, False, or None for *this app cannot
    see it from in here*.
    """
    from app.egress import proxy_configuration
    from app.tls_trust import (
        misconfigured_sources,
        trust_sources,
        verification_disabled_by,
    )

    proxy_vars = proxy_configuration()
    proxied = any(proxy_vars.values())

    sources = trust_sources()
    disabled_by = verification_disabled_by()
    misconfigured = misconfigured_sources()

    # The two evidence strings below are built here rather than inline: both
    # are several clauses long, and an inline conditional that spans them is
    # the shape that ends up saying something other than what it reads as.
    if sources:
        tls_clauses = [
            ", ".join(
                f"{s.variable} -> {'present' if s.exists else 'MISSING'}"
                for s in sources
            )
        ]
    else:
        tls_clauses = ["no CA variables set"]
    if disabled_by:
        tls_clauses.append(f"verification disabled by {', '.join(disabled_by)}")
    if misconfigured:
        tls_clauses.append(f"misconfigured: {', '.join(misconfigured)}")
    tls_evidence = "; ".join(tls_clauses)

    agentic_files = _env("AGENTBOX_AGENTIC_FILES_PATH")
    skills_root = _env("AGENTBOX_SKILLS_ROOT")
    bridge_url = _env("MANAGED_MCP_BRIDGE_URL")
    bridge_token = _env("AGENTBOX_CUSTOM_APP_MCP_TOKEN")
    guard_wired, guard_evidence = _package_guard_state()

    raw_keys = raw_provider_key_vars()
    if _env("KOBIL_SECUREPROXY_URL") and _env("KOBIL_SECUREPROXY_API_KEY"):
        secureproxy_evidence = (
            "KOBIL_SECUREPROXY_URL and KOBIL_SECUREPROXY_API_KEY are both set"
        )
    else:
        secureproxy_evidence = "no SecureProxy URL and virtual key in this environment"
    # Reported in BOTH directions, because each is the interesting half of the
    # other: a raw key beside a wired proxy is a finding, and a raw key with no
    # proxy is the whole point of the unmanaged case.
    secureproxy_evidence += (
        f"; this app can read a real provider key ({', '.join(raw_keys)})"
        if raw_keys
        else "; this app holds no raw provider key"
    )

    if skills_root:
        declared = _env("AGENTBOX_ALLOWED_SKILLS") or "none"
        skills_evidence = f"AGENTBOX_SKILLS_ROOT={skills_root}; declared: {declared}"
    else:
        skills_evidence = "AGENTBOX_SKILLS_ROOT is unset"

    rows: list[dict] = [
        {
            "id": "secureproxy",
            "name": "SecureProxy LLM gateway",
            "wired": bool(
                _env("KOBIL_SECUREPROXY_URL") and _env("KOBIL_SECUREPROXY_API_KEY")
            ),
            "detail": (
                "Model calls are brokered. This app holds no provider key -- it "
                "was handed a per-app virtual key and a proxy base URL, and the "
                "SDK was pointed at them."
            ),
            "absent_detail": (
                "The SDK talks to the vendor directly, with a real provider key "
                "this app can read. Nothing inspects the prompt or the answer."
            ),
            "evidence": secureproxy_evidence,
        },
        {
            "id": "egress",
            "name": "Egress policy (forward proxy)",
            "wired": proxied,
            "detail": (
                "The only route out of this app's private network is SecureProxy's "
                "forward proxy, which decides each destination against this "
                f"application's own policy (mode: {_env('AGENTBOX_EGRESS_MODE') or 'unknown'})."
            ),
            "absent_detail": (
                "No proxy is configured, so outbound requests go straight out. "
                "Nothing decides which destinations this app may reach."
            ),
            "evidence": (
                ", ".join(name for name, present in proxy_vars.items() if present)
                + " set (values withheld -- each carries this app's virtual key)"
                if proxied
                else "no proxy variables set in this environment"
            ),
        },
        {
            "id": "tls",
            "name": "TLS trust",
            # TRI-STATE, and the `None` arm is the ordinary case rather than a
            # gap. Measured on a real install 2026-09-21: the app container
            # has no CA variables and no added CA, SecureProxy holds no
            # interception CA, its runtime carries no mitm module, and
            # `forward_proxy.py` SPLICES the CONNECT tunnel -- six references
            # to splicing, none to interception. There is no inspected channel
            # to trust, so an "absent control" verdict here was a fabricated
            # finding: it reported the product as missing something it does not
            # do, and dragged the wired count down for it.
            #
            # Still worth flagging, and still `False`: an app that has DEFEATED
            # its own verification, or that names a CA file which is not there.
            "wired": (
                False if (disabled_by or misconfigured) else (True if sources else None)
            ),
            "detail": (
                "A platform CA is configured and the files it names are present, "
                "so an inspected channel would verify. The correct amount of TLS "
                "configuration in an AgentBox app is none."
            ),
            "absent_detail": (
                "This app has weakened its own TLS verification, or names a CA "
                "file that is not there. Either way an outbound request is not "
                "verifying what it claims to."
            ),
            "evidence": (
                tls_evidence
                if (sources or disabled_by or misconfigured)
                else (
                    "no CA variables set, and none is needed: SecureProxy tunnels "
                    "TLS rather than intercepting it, so there is no substituted "
                    "certificate to trust. Verification runs against the public "
                    "trust store, unchanged."
                )
            ),
        },
        {
            "id": "mcp_bridge",
            "name": "MCP Bridge",
            "wired": bool(bridge_url and bridge_token),
            "detail": (
                "This app has no direct route to any MCP server. Every tool call "
                "goes through the bridge, which checks that THIS application has a "
                "grant for THAT server, and holds destructive tools for approval."
            ),
            "absent_detail": (
                "No bridge. An app in this state either reaches its MCP servers "
                "directly or not at all -- either way nothing checks the grant."
            ),
            "evidence": (
                "MANAGED_MCP_BRIDGE_URL and an app token are both set"
                if bridge_url and bridge_token
                else f"bridge URL {'set' if bridge_url else 'absent'}, "
                f"app token {'set' if bridge_token else 'absent'}"
            ),
        },
        {
            "id": "package_guard",
            "name": "Package Guard",
            "wired": guard_wired,
            "detail": (
                "`pip install` reaches a policy decision point before anything is "
                "downloaded: allow, block, or hold for an operator."
            ),
            "absent_detail": (
                "`pip install` reaches pip. Whatever the agent asks for, it gets -- "
                "including a typosquat."
            ),
            "evidence": guard_evidence,
        },
        {
            "id": "file_guard",
            "name": "agentic-file-guard",
            "wired": bool(agentic_files) and Path(agentic_files).is_dir(),
            "detail": (
                f"The agent's own files live at {agentic_files or '(unset)'}, on a "
                "volume the guard watches from outside this container. It scans "
                "CONTENT, so a clean write produces nothing and a poisoned one is "
                "quarantined."
            ),
            "absent_detail": (
                "The agent's files are ordinary container files. Nothing scans "
                "what the agent writes to itself."
            ),
            "evidence": (
                f"AGENTBOX_AGENTIC_FILES_PATH={agentic_files} exists"
                if agentic_files and Path(agentic_files).is_dir()
                else f"AGENTBOX_AGENTIC_FILES_PATH={agentic_files or '(unset)'}"
            ),
        },
        {
            "id": "skills",
            "name": "Scanned skills",
            "wired": bool(skills_root) and Path(skills_root).is_dir(),
            "detail": (
                "Skills are mounted read-only, one per approved skill, after the "
                "Skill Scanner cleared them. This app folds them into its system "
                "instructions and cannot write to them."
            ),
            "absent_detail": (
                "No scanned-skill mount. Any instructions the agent picks up came "
                "from somewhere nothing checked."
            ),
            "evidence": skills_evidence,
        },
    ]

    # Rule 2. These act on the application from outside it and leave nothing in
    # here to read, so `wired` is None rather than a guess in either direction.
    rows.extend(
        {
            "id": control_id,
            "name": name,
            "wired": None,
            "detail": detail,
            "absent_detail": detail,
            "evidence": "not observable from inside the container -- ask the platform",
        }
        for control_id, name, detail in (
            (
                "sandbox",
                "gVisor sandbox",
                "Whether this container's syscalls are intercepted is a property of "
                "the runtime Docker started it with. `agentbox status` and the app's "
                "own card in the admin console answer it.",
            ),
            (
                "audit",
                "Audit trail",
                "Events are written by the platform's own components, not by this "
                "app, and an app that could read the trail could also shape it. "
                "Security -> Audit Log is the surface.",
            ),
            (
                "hardening_check",
                "Hardening Check",
                "A pre-flight scan of this container's own posture, run on the host "
                "before the app is allowed to serve. Its findings are on the app's "
                "card.",
            ),
        )
    )
    return rows


def describe() -> dict:
    """The whole answer, in the shape `GET /runtime-info` returns."""
    from app.tls_trust import describe_trust

    rows = controls()
    return {
        "managed": managed(),
        "app_id": _env("AGENTBOX_APP_ID"),
        "agent_type": _env("AGENTBOX_APP_TYPE") or "claude",
        "provider": _env("AGENTBOX_MODEL_PROVIDER"),
        "egress_mode": _env("AGENTBOX_EGRESS_MODE"),
        "skills": [s for s in _env("AGENTBOX_ALLOWED_SKILLS").split(",") if s.strip()],
        # Names of credential variables holding something OTHER than this
        # app's own virtual key -- a real provider key the app can read.
        # Empty is the claim the product makes; the page renders "where is
        # the AI key" from this field and never from the model's answer.
        "raw_provider_keys": raw_provider_key_vars(),
        "controls": rows,
        # Counted here rather than in the browser so every surface that asks
        # this question gets the same number.
        "wired_count": sum(1 for row in rows if row["wired"] is True),
        "observable_count": sum(1 for row in rows if row["wired"] is not None),
        "tls_trust": describe_trust(),
    }
