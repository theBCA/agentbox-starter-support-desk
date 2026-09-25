"""Turning the Claude Agent SDK's message stream into the console's events.

Kept apart from `claude.py` so it has no SDK import and can be tested with
plain stand-ins: it works on ATTRIBUTES -- `name`/`input`/`id` for a tool
call, `tool_use_id`/`content`/`is_error` for its result, `text` for prose --
which is what the SDK's dataclasses expose. Those dataclasses carry no `type`
field, which is why an earlier version that keyed on one never emitted a tool
event for a real run.

Two rules that are easy to get wrong:

* prose is a `token` only when the ASSISTANT said it. A user-role message
  carries tool results, and their text is the tool's answer to the model, not
  the model's answer to the user;
* the outcome attached to a `tool_result` comes from the toolkit's own record
  of the call, never from the block's text, so the console derives its word
  from the signal and not from prose.
"""

from __future__ import annotations

from typing import Any

from app.agent_tools import Toolkit, ToolOutcome


def _field(block: Any, name: str) -> Any:
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _blocks(message: Any) -> list:
    content = _field(message, "content")
    return list(content) if isinstance(content, (list, tuple)) else []


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(_field(block, "text"))
            for block in content
            if isinstance(_field(block, "text"), str)
        )
    return ""


def plain_tool_name(name: str, prefix: str) -> str:
    """`mcp__<server>__find_customer` -> `find_customer`."""
    return name[len(prefix) :] if name.startswith(prefix) else name


def translate(
    message: Any,
    *,
    from_assistant: bool,
    prefix: str,
    pending: dict[str, str],
    toolkit: Toolkit,
) -> list[dict[str, Any]]:
    """The console events one SDK message amounts to, in order.

    `pending` maps a tool call's id to its plain name across messages: the
    call arrives in an assistant message and its result in the user message
    that follows, and only the id links the two.
    """
    events: list[dict[str, Any]] = []
    for block in _blocks(message):
        tool_use_id = _field(block, "tool_use_id")
        if tool_use_id is not None:
            name = pending.pop(str(tool_use_id), None) or "tool"
            outcome = toolkit.take_outcome(name)
            if outcome is None:
                # A result the toolkit did not produce (a tool the SDK ran on
                # its own). Reported from the block, and never as a success
                # the platform did not confirm.
                is_error = bool(_field(block, "is_error"))
                outcome = ToolOutcome(
                    name,
                    "failed" if is_error else "tool_ok",
                    not is_error,
                    _content_text(_field(block, "content")),
                )
            events.append(
                {"type": "tool_result", "id": str(tool_use_id), **outcome.as_event()}
            )
            continue

        name = _field(block, "name")
        tool_input = _field(block, "input")
        if name is not None and tool_input is not None:
            plain = plain_tool_name(str(name), prefix)
            block_id = str(_field(block, "id") or "")
            if block_id:
                pending[block_id] = plain
            events.append(
                {
                    "type": "tool",
                    "id": block_id,
                    "name": plain,
                    "input": tool_input or {},
                }
            )
            continue

        text = _field(block, "text")
        if from_assistant and isinstance(text, str) and text.strip():
            events.append({"type": "token", "text": text})
    return events
