"""Where this application's TLS trust comes from, and a refusal when it is off.

SecureProxy terminates TLS on the forward proxy using a per-install CA, and
AgentBox injects that CA into every application image (plan 0.11.3). Injecting
into the **system** trust store is not sufficient, because the major language
runtimes ignore it: Python's `requests`/`httpx` use certifi's bundled list and
Node uses its own compiled-in roots. So the image-rewrite step sets per-runtime
environment variables as well -- `REQUESTS_CA_BUNDLE` and `SSL_CERT_FILE` for
this runtime, `NODE_EXTRA_CA_CERTS` in the Node starters.

The application's half is two things, and this module is both:

1. **Do not defeat them.** An app that passes `verify=False`, builds an
   unverified `SSLContext` or sets `PYTHONHTTPSVERIFY=0` turns an inspected
   channel into an unauthenticated one, silently -- and nothing outside the
   container can tell. So the outbound path REFUSES rather than trusting the
   app to behave, which is the same fail-closed posture SecureProxy takes when
   interception cannot be established.

2. **Make the state visible.** §0.11.3 calls trust-store injection "exactly the
   class of gap that ships silently and surfaces as 'HTTPS randomly broken for
   some apps'". This module is this app's answer to that: which variables are
   set, whether the files they name exist, and whether verification is on.

Two states that look alike and are not, which is why they are reported
separately:

* **disabled** -- verification is off. Requests succeed and prove nothing.
* **misconfigured** -- a variable names a file that is not there. In THIS
  runtime that is loud: `ssl` and `requests` both raise rather than fall back.
  In Node it is the silent one -- a missing `NODE_EXTRA_CA_CERTS` file is a
  warning on stderr and the process continues without the CA. Same shape,
  opposite failure mode, so neither starter folds the two together.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

#: Every variable this runtime reads for its CA material, with what reads it.
#: `SSL_CERT_FILE`/`SSL_CERT_DIR` are the stdlib `ssl` module's;
#: `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` are requests'. httpx reads the stdlib
#: pair when `trust_env` is on, which is its default.
TRUST_ENV_VARS: tuple[str, ...] = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)

#: Debian's system bundle, which `update-ca-certificates` rewrites. Only the
#: system tools (`curl`, `apt`) read it; it is reported so an operator can see
#: that half landed even when the per-runtime variables did not.
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


class TlsVerificationDisabled(RuntimeError):
    """Raised instead of making an outbound request without verification."""


class TrustSource(NamedTuple):
    variable: str
    value: str
    exists: bool


def _path_exists(value: str) -> bool:
    try:
        return Path(value).exists()
    except OSError:
        return False


def trust_sources() -> list[TrustSource]:
    """Every CA variable that is set, in the order this runtime reads them."""
    found: list[TrustSource] = []
    for variable in TRUST_ENV_VARS:
        value = os.environ.get(variable, "").strip()
        if value:
            found.append(TrustSource(variable, value, _path_exists(value)))
    return found


def verification_disabled_by() -> list[str]:
    """Reasons verification is OFF, each naming the thing that turned it off.

    An empty list means nothing disabled it. This is deliberately not a
    boolean: "why" is what an operator needs, and a boolean would make two
    different misconfigurations indistinguishable.
    """
    reasons: list[str] = []
    if os.environ.get("PYTHONHTTPSVERIFY", "").strip() == "0":
        reasons.append("PYTHONHTTPSVERIFY=0")
    return reasons


def misconfigured_sources() -> list[str]:
    """CA variables naming a file or directory that is not there."""
    return [
        f"{source.variable}={source.value} (no such file)"
        for source in trust_sources()
        if not source.exists
    ]


def assert_verification_enabled() -> None:
    """Fail closed before any outbound request.

    The refusal names the specific cause, because "TLS verification is
    disabled" and "the CA file is missing" send an operator to two different
    places, and a single generic message would send them to neither.
    """
    reasons = verification_disabled_by()
    if reasons:
        raise TlsVerificationDisabled(
            "refusing to make an outbound request with TLS verification "
            f"disabled: {', '.join(reasons)}"
        )


def describe_trust() -> dict:
    """The whole answer, in the shape `GET /egress/trust` returns."""
    sources = trust_sources()
    disabled = verification_disabled_by()
    misconfigured = misconfigured_sources()
    return {
        "verification_enabled": not disabled,
        "disabled_by": disabled,
        "misconfigured": misconfigured,
        "sources": [
            {"variable": s.variable, "value": s.value, "exists": s.exists}
            for s in sources
        ],
        "system_ca_bundle": {
            "path": SYSTEM_CA_BUNDLE,
            "exists": _path_exists(SYSTEM_CA_BUNDLE),
        },
        "note": (
            "AgentBox injects its per-install CA into this image and sets "
            "REQUESTS_CA_BUNDLE and SSL_CERT_FILE. No source here means this "
            "app would reject SecureProxy's intercepted TLS -- the system "
            "store alone is not enough for this runtime."
        ),
    }
