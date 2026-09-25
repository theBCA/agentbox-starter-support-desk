"""The MCP Bridge, as this application reaches it.

The agent never talks to a company system directly: this app has no route to
an MCP server, only to AgentBox's MCP Bridge, which checks that THIS
application holds a grant for THAT server and tool, holds sensitive actions
for an operator, and records every call. This module is the one place the app
speaks to it, and it turns the bridge's answers into one shape so nothing
downstream has to know the wire format:

  ok            the tool ran; ``result`` is the bridge's body
  held          the bridge created an approval request and did NOT run the
                tool; ``approval_id`` names it. A held call never resumes by
                itself -- after a manager approves, the same call has to be
                made again
  denied        the bridge refused: the server or tool is not granted to this
                application, or its identity was not accepted
  failed        no decision was reached -- the bridge could not be reached,
                answered something unparseable, the approval service behind
                it was down, or the tool itself errored
  unconfigured  this container was given no bridge at all (the unprotected
                case), reported rather than raised so a caller can say so

Why the split matters: a 403 from the bridge is an ANSWER, not an outage, and
the bridge says which answer -- ``error_type: approval_required`` with an
``approval_id`` is "waiting for a person", a bare 403 is "not allowed".
Flattening them into one "bridge error" was measured (2026-09-20) to send an
operator off to re-approve and rebuild an application that only needed a
click in the approvals queue.

``urllib`` on purpose, reached as ``urllib.request.urlopen`` rather than an
imported name: the bridge is a plain HTTP peer on this application's own
network (no proxy, no TLS), and the unit tests patch ``urllib.request.urlopen``
to assert the app token is sent.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, NamedTuple

BRIDGE_URL_VAR = "MANAGED_MCP_BRIDGE_URL"
APP_TOKEN_VAR = "AGENTBOX_CUSTOM_APP_MCP_TOKEN"

#: Bounded: a tool result is rendered into the model's context and into a
#: browser, and a runaway server should not be able to fill either.
MAX_RESULT_CHARS = 6_000

#: What a 403 with no reason most likely means. Kept as a FALLBACK, never as
#: the answer: the bridge normally says exactly why, and that text wins.
_REASONLESS_REFUSAL = (
    "MCP Bridge refused this application's identity (HTTP 403) and gave no "
    "reason. The most likely cause is that the target MCP server is not "
    "approved and bound to this application yet - approve its tools and assign "
    "it to the application in the admin console, then rebuild."
)


class BridgeUnconfigured(RuntimeError):
    """This container was not given a bridge URL and an app token."""


class BridgeOutcome(NamedTuple):
    status: str  # ok | held | denied | failed | unconfigured
    result: Any = None
    error: str = ""
    approval_id: str = ""
    http_status: int = 0


def configuration() -> tuple[str, str]:
    """The bridge URL and this application's token, or a refusal naming the
    variable that is missing -- the wording the `/mcp/*` routes always used."""
    url = os.environ.get(BRIDGE_URL_VAR, "").strip()
    if not url:
        raise BridgeUnconfigured(f"{BRIDGE_URL_VAR} is not set")
    token = os.environ.get(APP_TOKEN_VAR, "").strip()
    if not token:
        raise BridgeUnconfigured(f"{APP_TOKEN_VAR} is not set")
    return url, token


def is_configured() -> bool:
    try:
        configuration()
    except BridgeUnconfigured:
        return False
    return True


def _request(
    method: str, path: str, body: dict | None = None, *, timeout: float
) -> tuple[int, dict]:
    url, token = configuration()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{url.rstrip('/')}{path}",
        data=data,
        headers={"content-type": "application/json", "X-AgentBox-App-Token": token},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - AgentBox local bridge URL
            status = int(getattr(response, "status", 200) or 200)
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            parsed = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            parsed = {}
        return int(exc.code), parsed if isinstance(parsed, dict) else {}
    if not isinstance(parsed, dict):
        parsed = {"ok": False, "error": "MCP Bridge answered with a non-object body"}
    return status, parsed


def call_tool(
    server: str, tool: str, arguments: dict[str, Any], *, timeout: float = 30.0
) -> BridgeOutcome:
    """Run one tool through the bridge and say what happened, in one of the
    five statuses above. Never raises for anything the bridge or the network
    did -- those are outcomes, and the caller decides what each one means."""
    try:
        status, body = _request(
            "POST",
            "/call",
            {"server": server, "tool": tool, "arguments": arguments},
            timeout=timeout,
        )
    except BridgeUnconfigured as exc:
        return BridgeOutcome("unconfigured", error=str(exc))
    except (OSError, ValueError) as exc:
        return BridgeOutcome("failed", error=f"MCP Bridge call failed: {exc}")

    if status == 200 and body.get("ok"):
        return BridgeOutcome("ok", result=body, http_status=status)

    error = str(body.get("error") or "").strip()
    error_type = str(body.get("error_type") or "")
    approval_id = str(body.get("approval_id") or "")
    if status == 403 and error_type == "approval_required":
        return BridgeOutcome(
            "held", error=error, approval_id=approval_id, http_status=status
        )
    if status == 403 and error_type == "approval_unavailable":
        # Fail-closed, not a decision: the approver could not be reached and
        # the action stayed blocked. An outage on the platform's side, and it
        # must never be painted as a policy verdict.
        return BridgeOutcome("failed", error=error, http_status=status)
    if status in (401, 403):
        return BridgeOutcome(
            "denied", error=error or _REASONLESS_REFUSAL, http_status=status
        )
    if not error:
        error = (
            json.dumps(body, default=str)
            if body
            else f"MCP Bridge answered HTTP {status}"
        )
    return BridgeOutcome("failed", error=error, http_status=status)


def list_tools(*, timeout: float = 20.0) -> tuple[list[dict], list[dict]]:
    """The tools the bridge grants this application, each named
    ``<server>__<tool>``, and the servers it could not ask.

    An unconfigured bridge lists nothing -- the unprotected case, where there
    are no company systems to offer -- and an unreachable one is reported
    through the second list rather than raised: an agent with no tools is
    still an agent, and the console says why the list is short.
    """
    try:
        status, body = _request("GET", "/tools", timeout=timeout)
    except BridgeUnconfigured:
        return [], []
    except (OSError, ValueError) as exc:
        return [], [{"server": "*", "reason": f"MCP Bridge unreachable: {exc}"}]
    if status != 200 or not body.get("ok"):
        reason = str(body.get("error") or f"MCP Bridge answered HTTP {status}")
        return [], [{"server": "*", "reason": reason}]
    tools = [item for item in body.get("tools") or [] if isinstance(item, dict)]
    unavailable = [
        item for item in body.get("unavailable") or [] if isinstance(item, dict)
    ]
    return tools, unavailable


def split_tool_name(qualified: str) -> tuple[str, str]:
    """``<server>__<tool>`` back into its halves.

    From the RIGHT: a bundled server is itself named ``<app>__<name>``, so the
    first ``__`` is inside the server's name, not between server and tool.
    """
    server, sep, tool = qualified.rpartition("__")
    return (server, tool) if sep else ("", qualified)


def result_text(result: Any) -> str:
    """What a successful call returned, as text for the model.

    The bridge answers with MCP content blocks. Text blocks are joined;
    anything else is serialised; both are bounded.
    """
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            texts = [
                block["text"]
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            if texts:
                return "\n".join(texts)[:MAX_RESULT_CHARS]
        trimmed = {k: v for k, v in result.items() if k not in ("ok", "server", "tool")}
        return json.dumps(trimmed, default=str)[:MAX_RESULT_CHARS]
    return str(result)[:MAX_RESULT_CHARS]
