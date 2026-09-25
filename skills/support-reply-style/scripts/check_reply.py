"""Check a finished reply against the reply-style house rule.

Usage: python3 check_reply.py "<reply text>"
Prints `ok` and exits 0 when the reply follows the rule; otherwise prints one
line per broken rule and exits 1. Offline, no side effects, standard library
only: this file is shipped inside a house rule and runs wherever the agent
runs.
"""

from __future__ import annotations

import re
import sys

_CLOSING_RE = re.compile(r"(?:^|\s)Your case number (?:is [A-Za-z0-9#-]+|will follow by email)\.")
_MAX_WORDS_PER_SENTENCE = 30


def findings(reply: str) -> list[str]:
    text = (reply or "").strip()
    if not text:
        return ["reply is empty"]
    found: list[str] = []
    if not _CLOSING_RE.search(text):
        found.append("missing the closing line (SKILL.md, 'Reply style' part 3)")
    if "!" in text:
        found.append("exclamation mark (SKILL.md, 'Tone')")
    for sentence in re.split(r"(?<=[.?])\s+", text):
        words = sentence.split()
        if len(words) > _MAX_WORDS_PER_SENTENCE:
            found.append(f"sentence of {len(words)} words; keep it short (SKILL.md, 'Tone')")
    return found


def main(argv: list[str]) -> int:
    reply = " ".join(argv[1:]) if len(argv) > 1 else sys.stdin.read()
    problems = findings(reply)
    if not problems:
        print("ok")
        return 0
    for line in problems:
        print(line)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
