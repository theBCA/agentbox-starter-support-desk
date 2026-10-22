"""Claude Agent SDK backend.

Uses the real `claude-agent-sdk` package -- the agentic runtime, not the raw
`anthropic` API client. That distinction is the whole point: AgentBox exists
to secure *agentic* applications, so a starter built on a bare HTTP client
would not exercise what AgentBox protects (a tool loop, multi-turn
orchestration, and the subprocess the SDK drives).

Packaging consequence, verified on PyPI 2026-08-30: `claude-agent-sdk`
publishes wheels only for manylinux/macos/win -- there is NO musllinux
build. On `python:3.12-alpine` pip therefore falls back to the 0.3MB sdist,
which does not bundle the Claude Code CLI this SDK spawns
(`_internal/transport/subprocess_cli.py`); it installs cleanly and fails at
runtime. This app is built on `python:3.12-slim` for that reason -- see the
note in the Dockerfile.

SecureProxy: `configure_secureproxy` binds ANTHROPIC_API_KEY and
ANTHROPIC_BASE_URL in `os.environ` before the SDK starts, so the CLI
subprocess inherits them and every model call is brokered by SecureProxy.
Direct provider fallback is refused, not silently allowed.

Tools: the agent's toolkit (`app/agent_tools.py`) is handed to the SDK as an
in-process MCP server, built once per request, and Claude Code's own built-in
tools are switched OFF (`tools=[]`). The agent therefore reaches the company's
systems, the internet and pip only through tools that go through the
platform's controls and report the verdict. Every tool is pre-allowed by its
full name, so the non-interactive CLI never has to ask -- and never silently
denies, which is what an un-allowed tool comes to when nobody can answer.
"""

from __future__ import annotations

import asyncio
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from app.agent_tools import TOOL_GUIDANCE, Toolkit, build_toolkit
from app.backends._secureproxy import configure_secureproxy
from app.backends._shared import (
    build_system_instructions,
    build_user_text,
    get_current_date_iso,
)
from app.backends._stream import translate

#: The in-process server's name, and the prefix the SDK gives its tools
#: (`mcp__<server>__<tool>`). The console gets the bare name back.
_SDK_SERVER = "agentbox"
SDK_TOOL_PREFIX = f"mcp__{_SDK_SERVER}__"

#: Room for a few tool calls and an answer. A loop stops here rather than
#: running until the budget does.
_MAX_TURNS = 10

#: A run that hit `max_turns` is a stopped run, not a failed one: whatever the
#: agent said up to that point is still its answer.
_NON_FATAL_RESULT_SUBTYPES = frozenset({"error_max_turns"})


def _sdk_server(toolkit: Toolkit):
    sdk_tools = []
    for spec in toolkit.specs:

        async def _run(args, _name=spec.name):
            text, outcome = await toolkit.run(
                _name, args if isinstance(args, dict) else {}
            )
            return {
                "content": [{"type": "text", "text": text}],
                "is_error": not outcome.ok,
            }

        sdk_tools.append(tool(spec.name, spec.description, spec.input_schema)(_run))
    return create_sdk_mcp_server(name=_SDK_SERVER, tools=sdk_tools)


async def _prepare() -> tuple[Toolkit, ClaudeAgentOptions]:
    configure_secureproxy("anthropic")
    # The catalogue is one HTTP round trip to the bridge; off the event loop.
    toolkit = await asyncio.to_thread(build_toolkit)

    # The date goes in through the system prompt rather than as a tool: the
    # value is fixed for the life of the request, so handing it over up front
    # costs one line and removes a whole tool round trip from every call.
    # The concept's prompt and any standing instructions come from the
    # shared builder; then what only this process knows -- the tools it
    # offers, and today's date.
    instructions = (
        f"{await build_system_instructions()}\n\n"
        f"{TOOL_GUIDANCE}\n\n"
        f"Today's date in ISO 8601 format is {get_current_date_iso()}."
    )
    options = ClaudeAgentOptions(
        system_prompt=instructions,
        max_turns=_MAX_TURNS,
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"),
        tools=[],
        mcp_servers={_SDK_SERVER: _sdk_server(toolkit)},
        # Both spellings, on purpose: Claude Code's permission rules name an
        # MCP tool `mcp__<server>__<tool>` (or a whole server as
        # `mcp__<server>`), while the SDK's own examples allow the bare name.
        # Listing all of them costs nothing; missing the one the CLI checks
        # turns every call into a silent denial.
        allowed_tools=[
            f"mcp__{_SDK_SERVER}",
            *(SDK_TOOL_PREFIX + spec.name for spec in toolkit.specs),
            *(spec.name for spec in toolkit.specs),
        ],
    )
    return toolkit, options


def _assistant_text(message: AssistantMessage) -> str:
    return "\n".join(
        block.text
        for block in message.content
        if isinstance(getattr(block, "text", None), str) and block.text.strip()
    )


def _raise_if_failed(message: ResultMessage, last_error_text: str) -> None:
    """A failed run arrives as DATA, not as an exception.

    The starter's whole refusal story depends on the gateway's 403 reaching
    `main._secureproxy_block_detail`, and that only ever sees exceptions. So
    a result that reports an error is raised with the text the run produced
    -- which, for a SecureProxy block, carries the gateway's own detail.
    """
    if not message.is_error or message.subtype in _NON_FATAL_RESULT_SUBTYPES:
        return
    parts = [
        str(part)
        for part in (message.result, last_error_text, *(message.errors or ()))
        if part
    ]
    raise RuntimeError("\n".join(parts) or f"the agent run failed ({message.subtype})")


async def process(user_input: str, question: str | None) -> dict:
    """One answer, plus what the agent did on the way.

    Built on `stream` rather than beside it, so the two cannot drift: the
    answer is the stream's final `result`, and `tool_calls` are its
    `tool_result` events -- the same signals the console renders.
    """
    final = ""
    said: list[str] = []
    tool_calls: list[dict] = []
    async for event in stream(user_input, question):
        kind = event.get("type")
        if kind == "result":
            final = str(event.get("text") or "")
        elif kind == "token":
            said.append(str(event.get("text") or ""))
        elif kind == "tool_result":
            tool_calls.append(
                {
                    key: event.get(key)
                    for key in ("name", "signal", "ok", "detail", "approval_id")
                }
            )

    answer = final or "\n".join(said)
    if not answer.strip():
        raise RuntimeError(
            "claude-agent-sdk returned no assistant text; the bundled CLI may "
            "be unavailable in this image"
        )
    return {"answer": answer, "tool_calls": tool_calls}


async def stream(user_input: str, question: str | None):
    """Yield the agent's turns as they happen, for POST /process/stream.

    Events: `token` (assistant prose), `tool` (a call, with the plain tool
    name and its input), `tool_result` (the toolkit's outcome for that call,
    with its signal), and one final `result` with the complete answer. The
    final answer is emitted ONCE, from the ResultMessage; incremental text
    comes only from assistant content blocks, so nothing is replayed.
    """
    toolkit, options = await _prepare()
    pending: dict[str, str] = {}
    last_error_text = ""

    async for message in query(
        prompt=build_user_text(user_input, question), options=options
    ):
        if isinstance(message, ResultMessage):
            _raise_if_failed(message, last_error_text)
            if isinstance(message.result, str) and message.result.strip():
                yield {"type": "result", "text": message.result}
            continue

        from_assistant = isinstance(message, AssistantMessage)
        if from_assistant and message.error:
            last_error_text = f"{message.error}: {_assistant_text(message)}"
            continue
        for event in translate(
            message,
            from_assistant=from_assistant,
            prefix=SDK_TOOL_PREFIX,
            pending=pending,
            toolkit=toolkit,
        ):
            yield event
