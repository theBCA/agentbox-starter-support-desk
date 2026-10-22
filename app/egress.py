"""Outbound HTTPS from inside an AgentBox application.

Every byte an application sends to the internet goes through SecureProxy's
forward proxy, and SecureProxy terminates TLS there using a per-install CA
(plan 0.11.3). Two things follow that a plain `urlopen` gets wrong, and both
are the reason this module exists rather than a one-line helper.

**1. Use a client that authenticates the CONNECT tunnel.** `urllib`'s
`ProxyHandler` adds the proxy credential with `add_header`, which capitalises
the key to `Proxy-authorization` -- while `AbstractHTTPHandler.do_open` only
forwards a header spelled exactly `Proxy-Authorization` into the tunnel. The
credential is therefore silently dropped and the proxy answers `407`. Measured
live: plain urlopen → 407, the same request with the header → 200. `httpx` and
`requests` spell it correctly; plain `urllib` does not. This app uses `httpx`.

**2. Do not pass `verify=`.** AgentBox injects its CA and sets
`REQUESTS_CA_BUNDLE` and `SSL_CERT_FILE` in the image, and `httpx` reads them
when `trust_env` is on -- which is its default. Passing an explicit `verify`
argument overrides exactly the thing the platform configured: `verify=False`
turns an inspected channel into an unauthenticated one, and even
`verify="/some/path"` replaces the platform's choice with the app's. The
correct amount of TLS configuration in an AgentBox application is none.

The refusal below is the app's half of §0.11.3's fail-closed posture: the proxy
denies a connection it cannot intercept, and the app declines to make one it
cannot verify. A request that succeeded without verification would prove
nothing and look identical to one that worked.
"""

from __future__ import annotations

import os
from typing import Any

from app.tls_trust import assert_verification_enabled, describe_trust

#: SecureProxy's forward proxy, as the image's environment names it. Reported
#: rather than parsed: an app that rewrote this would be routing around the
#: only path out of its network, which has no route out to find.
PROXY_ENV_VARS: tuple[str, ...] = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
)

DEFAULT_TIMEOUT_SECONDS = 20.0


def proxy_configuration() -> dict[str, bool]:
    """Which proxy variables are set. Values are NOT returned -- the forward
    proxy URL carries this app's virtual key as its credential."""
    return {name: bool(os.environ.get(name, "").strip()) for name in PROXY_ENV_VARS}


async def fetch(url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """GET *url* through SecureProxy, verifying TLS the way the image says to.

    Returns the status, a short body prefix and the trust configuration that
    was in force, so a caller can tell "the CA is wired up and the destination
    is allowed" from "the CA is wired up and policy said no" -- two different
    answers that a bare status code collapses into one.
    """
    assert_verification_enabled()

    import httpx

    # No `verify=`, and `trust_env` left at its default so the proxy variables
    # and the CA variables are both read from the environment AgentBox built.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.get(url)

    return {
        "url": url,
        "status": response.status_code,
        "body_prefix": response.text[:200],
        "proxy": proxy_configuration(),
        "trust": describe_trust(),
    }


async def post(
    url: str, text: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """POST *text* to *url* through SecureProxy, under the same two rules.

    This is the agent's "send it to a website" action. Whether a tunnel opens
    at all is SecureProxy's decision, taken before this runs
    (`agent_tools._run_post_to_site` asks with a raw CONNECT first, so the
    proxy's own status line survives); this only carries the body once it
    has, and reports what the destination itself answered.
    """
    assert_verification_enabled()

    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.post(
            url,
            content=text.encode("utf-8"),
            headers={"content-type": "text/plain; charset=utf-8"},
        )

    return {
        "url": url,
        "status": response.status_code,
        "body_prefix": response.text[:200],
        "proxy": proxy_configuration(),
        "trust": describe_trust(),
    }
