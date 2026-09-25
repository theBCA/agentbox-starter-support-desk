"""Shared prompt assembly for this application's backends.

The job the agent does is not written here. It comes from `concept/prompt.md`
beside `app/` -- the concept is data, the code is the kit -- so the same
backend can carry a different product by swapping that directory.

`build_system_instructions` puts the concept's prompt first and appends the
standing instructions the environment supplies, if it supplies any.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

#: The concept lives beside `app/`, in the source tree, not in a mount: it is
#: part of what this application IS, and it ships in the image.
CONCEPT_DIR = Path(__file__).resolve().parents[2] / "concept"

#: What the agent is told when the concept ships no prompt. Deliberately
#: dull: an app whose `concept/prompt.md` went missing should still answer,
#: and should not pretend to be anything in particular.
_FALLBACK_PROMPT = (
    "You are this application's assistant. Answer the user's message directly "
    "and briefly, use the tools you have when the request needs them, and "
    "relay every tool result truthfully."
)

#: The one thing this application is told about the environment it runs in:
#: the absolute path of a file holding standing instructions for the agent.
#:
#: The whole contract is *read that file and append it to the system prompt*.
#: It is optional in both directions -- an unset variable, a missing file and
#: an empty file all mean "there are none", and the application then behaves
#: exactly as one that was never given any. Nothing is parsed out of the file
#: and nothing else is inferred from it.
SYSTEM_INSTRUCTIONS_VAR = "AGENTBOX_SYSTEM_INSTRUCTIONS"

#: A bound, so a file that grew unexpectedly cannot crowd out the concept's
#: own prompt or the conversation. Truncation is ANNOUNCED rather than silent
#: (see below): standing instructions the agent is expected to follow are the
#: worst thing to drop the tail of without saying so.
_MAX_SYSTEM_INSTRUCTION_CHARS = 20_000

_TRUNCATION_NOTICE = (
    "\n\n[The instructions above were truncated because they exceeded "
    f"{_MAX_SYSTEM_INSTRUCTION_CHARS} characters. Say so if you are asked to "
    "follow something that appears to be cut off.]"
)


def load_concept_prompt() -> str:
    """`concept/prompt.md`, or the fallback.

    Read once at import: the concept is source, not a cache, and does not
    change under a running application.
    """
    try:
        text = (CONCEPT_DIR / "prompt.md").read_text(encoding="utf-8").strip()
    except OSError:
        return _FALLBACK_PROMPT
    return text or _FALLBACK_PROMPT


BASE_INSTRUCTIONS = load_concept_prompt()


def load_system_instructions() -> str:
    """The standing instructions this environment supplies, or ``""``.

    Re-read on every request rather than cached at startup: the file may be
    written, rewritten or removed while this application is running, and a
    copy taken at import would be a copy of whichever happened first.

    Every failure answers ``""`` and says nothing -- no variable set, a path
    that is not a readable file, an empty one. An application given none must
    behave exactly as an application that was never offered any.
    """
    configured = os.environ.get(SYSTEM_INSTRUCTIONS_VAR, "").strip()
    if not configured:
        return ""
    try:
        text = Path(configured).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if len(text) <= _MAX_SYSTEM_INSTRUCTION_CHARS:
        return text
    return text[:_MAX_SYSTEM_INSTRUCTION_CHARS] + _TRUNCATION_NOTICE


async def build_system_instructions() -> str:
    """The concept's prompt, then the standing instructions if there are any."""
    parts = [BASE_INSTRUCTIONS]
    standing = load_system_instructions()
    if standing:
        parts.append(standing)
    return "\n\n".join(parts)


def build_user_text(user_input: str, question: str | None) -> str:
    """The user turn: the input as written, and an optional question after it."""
    text = user_input.strip()
    if question and question.strip():
        return f"{text}\n\nQuestion: {question.strip()}"
    return text


def get_current_date_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()
