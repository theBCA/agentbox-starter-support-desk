"""What the agent can do, and what each attempt is reported as.

The agent reaches the world through exactly three kinds of tool, built here
into one toolkit per request:

* **the company systems** AgentBox grants this application -- read from the
  bridge's catalogue (``GET /tools``) at request time and called through
  ``POST /call``. Nothing in this file knows which systems those are, which is
  what lets one codebase carry several concepts;
* **post_to_site** and **read_web_page** -- an outbound POST and GET. Inside
  AgentBox the only route out is SecureProxy's forward proxy, which decides
  each destination against this application's own list; outside, the request
  simply goes;
* **install_package** -- ``pip install``. Inside AgentBox that reaches
  Package Guard's PATH shim, which allows, blocks or holds before anything is
  downloaded; outside, it reaches pip;
* **keep_note** -- a file in the agent's own notes directory. Inside AgentBox
  that directory is a volume the file guard watches from outside the
  container, and a poisoned note is quarantined within seconds; outside, the
  note simply stays;
* **list_files** and **read_file** -- a READ-ONLY look at the agent's own
  HOME. Generic on purpose: this application knows nothing about where the
  platform keeps anything. Its standing instructions say where to look (the
  reviewed skills, for one), and these are how the agent follows them -- the
  SDKs this kit uses have no file tool of their own.

Every attempt ends in a :class:`ToolOutcome` carrying a *signal* from a fixed
vocabulary. The signal is derived from what came back -- the bridge's body,
the proxy's status line, the guard's verdict -- and never from what was asked,
so a step that happened to go differently is reported as it went. The console
turns a signal into one plain sentence; the model gets the text.

Three things that text tells the model on purpose: a held action has NOT run
and will not run by itself (so it must not claim success or retry in a loop);
a refusal is a decision to relay, not an error to work around; and a failure
is neither -- the platform's own words are passed on and nothing is invented.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app import bridge, demos, egress

#: The whole vocabulary. Anything the console renders comes from here.
SIGNALS = frozenset(
    {
        "tool_ok",
        "tool_held",
        "tool_denied",
        "egress_ok",
        "egress_blocked",
        "pkg_allow",
        "pkg_block",
        "pkg_hold",
        "note_quarantined",
        "note_kept",
        "failed",
    }
)

POST_TO_SITE = "post_to_site"
READ_WEB_PAGE = "read_web_page"
INSTALL_PACKAGE = "install_package"
KEEP_NOTE = "keep_note"
LIST_FILES = "list_files"
READ_FILE = "read_file"

#: Bounds on one file-tool answer, so a directory tree or a large file cannot
#: crowd the conversation out of the model's context.
_MAX_LISTED_FILES = 200
_MAX_READ_BYTES = 64 * 1024

#: How long to give the file guard to act on a note before reporting it kept.
#: A poisoned write is quarantined within a few seconds; a clean one is never
#: touched, which is the other honest answer.
_NOTE_WATCH_SECONDS = 8.0

#: What the system prompt says about the tools. Appended by every backend so
#: the three SDKs are told the same thing in the same words.
TOOL_GUIDANCE = (
    "You have tools. The ones named after the company's systems read or change "
    "real records; post_to_site sends text to a website and "
    "read_web_page fetches one; install_package adds software to this "
    "application; keep_note saves a note in your own files; list_files and "
    "read_file show you files under your home directory, read-only -- use them "
    "to follow your standing instructions, for instance to read the skills they "
    "point you to. Use a tool when the "
    "request needs one and not otherwise, and say in one short sentence what "
    "you are about to do before you call it. Read every tool result before "
    "answering. If it says the action is waiting for a manager's approval, "
    "tell the user it is waiting and stop: do not try again and do not say it "
    "happened. If it says the action was refused, or a site is not on the "
    "list, or a package was refused, relay that plainly. If it says something "
    "failed, say so in the words you were given. Never describe a result a "
    "tool did not return."
)

#: A model-facing tool name: letters, digits, underscore, dash, 64 max.
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_TOOL_NAME = 64
#: The bridge prefixes each description with ``[<server>]``; the model gets the
#: description without it (the server is not a word a user would recognise).
_SERVER_PREFIX_RE = re.compile(r"^\[[^\]]*\]\s*")
_MAX_DETAIL_CHARS = 2_000


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    kind: str  # bridge | egress | package
    server: str = ""
    tool: str = ""


@dataclass(frozen=True)
class ToolOutcome:
    name: str
    signal: str
    ok: bool
    detail: str
    approval_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def as_event(self) -> dict[str, Any]:
        """The fields the console reads, bounded for a browser."""
        return {
            "name": self.name,
            "signal": self.signal,
            "ok": self.ok,
            "detail": self.detail[:_MAX_DETAIL_CHARS],
            "approval_id": self.approval_id,
            "data": dict(self.data),
        }


_FIXED_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name=POST_TO_SITE,
        description=(
            "Send text to a web address outside the company, as an HTTP POST. "
            "Use it only when asked to post, publish or send something to a "
            "website. The result says whether the site could be reached and "
            "what it answered."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full address, starting with https:// or http://",
                },
                "text": {"type": "string", "description": "What to send"},
            },
            "required": ["url", "text"],
            "additionalProperties": False,
        },
        kind="egress",
    ),
    ToolSpec(
        name=READ_WEB_PAGE,
        description=(
            "Fetch a web page or a data file from an address outside the company, "
            "as an HTTP GET. Use it only when asked for something that lives on a "
            "website, such as today's prices. The result is the page's text, or "
            "the reason it could not be reached."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full address, starting with https:// or http://",
                }
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        kind="egress",
    ),
    ToolSpec(
        name=KEEP_NOTE,
        description=(
            "Save a short note in your own notes, for the next time you look at "
            "this matter. Use it only when asked to note or remember something. "
            "The result says whether the note is there."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "A short file-safe title"},
                "text": {"type": "string", "description": "The note"},
            },
            "required": ["title", "text"],
            "additionalProperties": False,
        },
        kind="note",
    ),
    ToolSpec(
        name=INSTALL_PACKAGE,
        description=(
            "Add a Python package to this application with pip. Use it only "
            "when asked to add software. The result says whether it was "
            "installed, refused, or is waiting for an operator's decision."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The package name, optionally pinned, e.g. name==1.2",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        kind="package",
    ),
)


_FILE_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name=LIST_FILES,
        description=(
            "List the files under a directory in your home directory, however "
            "deep. Read-only. Use it to see what is there before reading a file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "An absolute path, or one relative to your home",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        kind="files",
    ),
    ToolSpec(
        name=READ_FILE,
        description=(
            "Read one text file under your home directory. Read-only. The "
            "result is the file's text, or the reason it could not be read."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "An absolute path, or one relative to your home",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        kind="files",
    ),
)
_FIXED_SPECS = _FIXED_SPECS + _FILE_SPECS


class Toolkit:
    """The tools of one request, and the record of what each call came to."""

    def __init__(self, specs: list[ToolSpec], *, unavailable: list[dict] | None = None):
        self.specs = list(specs)
        #: Bridge servers the catalogue could not include, with the reason.
        self.unavailable = list(unavailable or [])
        self._by_name = {spec.name: spec for spec in self.specs}
        self._outcomes: dict[str, deque[ToolOutcome]] = defaultdict(deque)

    def spec(self, name: str) -> ToolSpec | None:
        return self._by_name.get(name)

    async def run(
        self, name: str, arguments: dict[str, Any] | None
    ) -> tuple[str, ToolOutcome]:
        """Run one tool. Returns the text for the model and the outcome for
        the console, and keeps the outcome so the stream can attach it to the
        SDK's result block later (`take_outcome`)."""
        spec = self._by_name.get(name)
        args = dict(arguments or {})
        if spec is None:
            outcome = ToolOutcome(
                name,
                "failed",
                False,
                f"{name!r} is not one of this application's tools",
            )
        elif spec.kind == "bridge":
            outcome = await asyncio.to_thread(_run_bridge_tool, spec, args)
        elif spec.kind == "egress":
            outcome = await _run_outbound(spec, args)
        elif spec.kind == "note":
            outcome = await asyncio.to_thread(_run_keep_note, spec, args)
        elif spec.kind == "files":
            outcome = await asyncio.to_thread(_run_file_tool, spec, args)
        else:
            outcome = await asyncio.to_thread(_run_install, spec, args)
        self._outcomes[name].append(outcome)
        return model_text(outcome), outcome

    def take_outcome(self, name: str) -> ToolOutcome | None:
        """The oldest outcome of *name* not yet handed out.

        SIMPLIFIED: matched by name in call order, which is exact for the
        sequential calls an agent makes; two parallel calls of the same tool
        are paired in dispatch order. Key on the SDK's tool_use_id if that
        ever proves wrong.
        """
        queue = self._outcomes.get(name)
        return queue.popleft() if queue else None


def build_toolkit() -> Toolkit:
    """One toolkit for one request.

    Rebuilt every time on purpose: the grant can change between two requests
    (a server assigned, a tool approved) and a cached catalogue would keep
    offering yesterday's tools. SIMPLIFIED: one ``GET /tools`` per request;
    add a short TTL if that round trip ever shows up in latency.
    """
    specs = list(_FIXED_SPECS)
    taken = {spec.name for spec in specs}
    catalogue, unavailable = bridge.list_tools()
    for entry in catalogue:
        server, tool = bridge.split_tool_name(str(entry.get("name") or ""))
        if not server or not tool:
            continue
        name = _model_name(tool, server, taken)
        taken.add(name)
        schema = entry.get("inputSchema")
        specs.append(
            ToolSpec(
                name=name,
                description=_plain_description(entry.get("description")),
                input_schema=(
                    schema
                    if isinstance(schema, dict) and schema
                    else {"type": "object", "properties": {}}
                ),
                kind="bridge",
                server=server,
                tool=tool,
            )
        )
    return Toolkit(specs, unavailable=unavailable)


def _model_name(tool: str, server: str, taken: set[str]) -> str:
    """The bare tool name when it is free; prefixed with the server's short
    name when two servers offer the same tool (or one shadows a fixed tool)."""
    base = _TOOL_NAME_RE.sub("_", tool)[:_MAX_TOOL_NAME] or "tool"
    if base not in taken:
        return base
    short = server.rpartition("__")[2] or server  # <app>__<name> -> <name>
    prefixed = _TOOL_NAME_RE.sub("_", f"{short}__{tool}")[:_MAX_TOOL_NAME]
    candidate, n = prefixed, 2
    while candidate in taken:
        candidate = f"{prefixed[: _MAX_TOOL_NAME - 3]}_{n}"
        n += 1
    return candidate


def _plain_description(raw: Any) -> str:
    text = _SERVER_PREFIX_RE.sub("", str(raw or "")).strip()
    return text or "A tool of one of the company's systems."


def _run_bridge_tool(spec: ToolSpec, arguments: dict[str, Any]) -> ToolOutcome:
    answer = bridge.call_tool(spec.server, spec.tool, arguments)
    data = {"server": spec.server, "tool": spec.tool, "http_status": answer.http_status}
    if answer.status == "ok":
        return ToolOutcome(
            spec.name, "tool_ok", True, bridge.result_text(answer.result), data=data
        )
    if answer.status == "held":
        return ToolOutcome(
            spec.name,
            "tool_held",
            False,
            answer.error,
            approval_id=answer.approval_id,
            data=data,
        )
    if answer.status == "denied":
        return ToolOutcome(spec.name, "tool_denied", False, answer.error, data=data)
    return ToolOutcome(spec.name, "failed", False, answer.error, data=data)


def _https_proxy_set() -> bool:
    configured = egress.proxy_configuration()
    return bool(configured.get("HTTPS_PROXY") or configured.get("https_proxy"))


async def _run_outbound(spec: ToolSpec, arguments: dict[str, Any]) -> ToolOutcome:
    """post_to_site and read_web_page: one path out, two verbs."""
    url = str(arguments.get("url") or "").strip()
    text = str(arguments.get("text") or "")
    reading = spec.name == READ_WEB_PAGE
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ToolOutcome(
            spec.name, "failed", False, "url must be a full http:// or https:// address"
        )
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    proxied = _https_proxy_set()
    data: dict[str, Any] = {"host": host, "port": port, "proxied": proxied}

    if proxied:
        # Ask the proxy first, with the raw CONNECT that keeps its own status
        # line (`demos.probe_egress`): a refused tunnel is the decision this
        # tool exists to report, and it has to stay distinct from "the site
        # is down", which a client library folds into one connection error.
        probe = await asyncio.to_thread(demos.probe_egress, host, port)
        data["proxy_status"] = probe.get("status")
        if probe.get("status") == 403:
            return ToolOutcome(
                spec.name,
                "egress_blocked",
                False,
                f"{host} is not on this application's list of allowed sites; "
                "nothing was sent or read",
                data=data,
            )
        if probe.get("status") != 200:
            return ToolOutcome(
                spec.name,
                "failed",
                False,
                f"the outbound proxy did not open a tunnel to {host}: "
                f"{probe.get('reason') or probe.get('status')}",
                data=data,
            )

    try:
        sent = await (egress.fetch(url) if reading else egress.post(url, text))
    except Exception as exc:  # noqa: BLE001 - reported to the model, never raised into the agent loop
        verb = "read" if reading else "send to"
        return ToolOutcome(
            spec.name, "failed", False, f"could not {verb} {host}: {exc}", data=data
        )
    data["status"] = sent.get("status")
    data["verb"] = "read" if reading else "sent"
    if reading:
        detail = (
            f"read {host} (HTTP {sent.get('status')}):\n{sent.get('body_prefix') or ''}"
        )
    else:
        detail = f"sent to {host}; it answered HTTP {sent.get('status')}"
    return ToolOutcome(spec.name, "egress_ok", True, detail, data=data)


def _run_keep_note(spec: ToolSpec, arguments: dict[str, Any]) -> ToolOutcome:
    """Write the note where the agent's own files live, then watch it.

    Inside AgentBox that directory is a volume the file guard reads from
    OUTSIDE the container; content that trips one of its rules is moved to
    quarantine within seconds, and a clean note is never touched. So the
    verdict is read off the filesystem: gone means quarantined, still there
    means kept. Outside AgentBox nothing watches, and "kept" is the honest
    answer whatever the note said.
    """
    raw_title = str(arguments.get("title") or "")
    title = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_title).strip("-.")[:60] or "note"
    text = str(arguments.get("text") or "")
    configured = os.environ.get("AGENTBOX_AGENTIC_FILES_PATH", "").strip()
    watched = bool(configured)
    base = Path(configured) if configured else Path.home() / "notes"
    target = base / "notes" / f"{title}.md"
    data: dict[str, Any] = {"path": str(target), "watched": watched}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        return ToolOutcome(
            spec.name, "failed", False, f"could not write the note: {exc}", data=data
        )
    if watched:
        deadline = time.monotonic() + _NOTE_WATCH_SECONDS
        while time.monotonic() < deadline:
            if not target.exists():
                return ToolOutcome(
                    spec.name,
                    "note_quarantined",
                    False,
                    "the note was moved to quarantine seconds after it was written",
                    data=data,
                )
            time.sleep(0.5)
    return ToolOutcome(
        spec.name, "note_kept", True, f"the note is kept at {target.name}", data=data
    )


def _inside_home(raw: str) -> Path:
    """*raw* resolved against HOME, refused if it lands outside it.

    Resolved BEFORE the check, so `..` and a symlink pointing out of HOME are
    judged by where they lead, not by how they are spelled.
    """
    home = Path.home().resolve()
    candidate = Path(raw).expanduser() if raw else home
    if not candidate.is_absolute():
        candidate = home / candidate
    resolved = candidate.resolve()
    if resolved != home and home not in resolved.parents:
        raise ValueError(f"{raw!r} is outside your home directory")
    return resolved


def _run_file_tool(spec: ToolSpec, arguments: dict[str, Any]) -> ToolOutcome:
    """`list_files` / `read_file`: a read-only look at the agent's own HOME."""
    raw = str(arguments.get("path") or "").strip()
    data: dict[str, Any] = {"path": raw}
    try:
        target = _inside_home(raw)
    except (ValueError, OSError) as exc:
        return ToolOutcome(spec.name, "failed", False, str(exc), data=data)
    if spec.name == LIST_FILES:
        if not target.is_dir():
            return ToolOutcome(
                spec.name, "failed", False, f"{raw!r} is not a directory", data=data
            )
        files: list[str] = []
        more = False
        for item in target.rglob("*"):
            if item.is_file():
                if len(files) == _MAX_LISTED_FILES:
                    more = True  # stop walking: a large tree is not read whole
                    break
                files.append(str(item))
        shown = sorted(files)
        text = "\n".join(shown) if shown else "(no files)"
        if more:
            text += f"\n... more than {_MAX_LISTED_FILES} files; list a subdirectory"
        return ToolOutcome(spec.name, "tool_ok", True, text, data=data)
    if not target.is_file():
        return ToolOutcome(
            spec.name, "failed", False, f"{raw!r} is not a file", data=data
        )
    try:
        with target.open("rb") as handle:
            raw_bytes = handle.read(_MAX_READ_BYTES + 1)
    except OSError as exc:
        return ToolOutcome(spec.name, "failed", False, f"could not read it: {exc}", data=data)
    text = raw_bytes[:_MAX_READ_BYTES].decode("utf-8", errors="replace")
    if len(raw_bytes) > _MAX_READ_BYTES:
        text += "\n[truncated]"
    return ToolOutcome(spec.name, "tool_ok", True, text, data=data)


def _run_install(spec: ToolSpec, arguments: dict[str, Any]) -> ToolOutcome:
    name = str(arguments.get("name") or "").strip()
    try:
        result = demos.install_package(name, "pip")
    except (ValueError, RuntimeError) as exc:
        return ToolOutcome(spec.name, "failed", False, str(exc), data={"package": name})

    verdict = str(result.get("verdict") or "none")
    output = str(result.get("output") or "")
    data: dict[str, Any] = {
        "package": name,
        "verdict": verdict,
        "exit_code": result.get("exit_code"),
        # Package Guard spoke, or nothing did. An install that nothing
        # evaluated is the unprotected case, and the console says so.
        "governed": verdict != "none",
    }
    if result.get("already_installed"):
        return ToolOutcome(
            spec.name, "pkg_allow", True, f"{name} is already installed; nothing was added", data=data
        )
    infra = result.get("infra_failure")
    if infra:
        return ToolOutcome(
            spec.name,
            "failed",
            False,
            f"Package Guard could not evaluate {name} ({', '.join(infra)}); "
            "this is a fault, not a refusal",
            data=data,
        )
    if verdict == "block":
        return ToolOutcome(
            spec.name,
            "pkg_block",
            False,
            _first_line(output) or f"Package Guard refused {name}",
            data=data,
        )
    if verdict == "hold":
        return ToolOutcome(
            spec.name,
            "pkg_hold",
            False,
            _first_line(output)
            or f"Package Guard is holding {name} for an operator's decision",
            data=data,
        )
    if verdict == "allow" or result.get("exit_code") == 0:
        detail = (
            f"{name} installed; Package Guard allowed it"
            if verdict == "allow"
            else f"{name} installed; nothing checked it first"
        )
        return ToolOutcome(spec.name, "pkg_allow", True, detail, data=data)
    return ToolOutcome(
        spec.name,
        "failed",
        False,
        output[-600:] or f"pip exited {result.get('exit_code')}",
        data=data,
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def model_text(outcome: ToolOutcome) -> str:
    """What the model reads. Says what happened, and what NOT to do next."""
    signal = outcome.signal
    data = outcome.data
    if signal == "tool_ok":
        return outcome.detail or "Done."
    if signal == "tool_held":
        ref = f" (approval id: {outcome.approval_id})" if outcome.approval_id else ""
        return (
            "AgentBox has PAUSED this action and it has NOT run. A manager must "
            f"approve it in the admin console first{ref}; after that the same "
            "request has to be made again. Tell the user it is waiting for "
            "approval, and stop."
        )
    if signal == "tool_denied":
        return (
            f"AgentBox refused this action, so it did not run: {outcome.detail} "
            "Tell the user it was not allowed."
        )
    if signal == "egress_blocked":
        return (
            f"AgentBox did not allow a connection to {data.get('host')}: it is not "
            "on this application's list of allowed sites, so nothing was sent or "
            "read. Tell the user the site is not on the list."
        )
    if signal == "egress_ok":
        note = (
            ""
            if data.get("proxied")
            else " This application has no outbound gate, so the request went straight out."
        )
        if data.get("verb") == "read":
            return f"{outcome.detail}{note}"
        return (
            f"Sent to {data.get('host')}; it answered HTTP {data.get('status')}.{note}"
        )
    if signal == "note_quarantined":
        return (
            "AgentBox removed the note seconds after you wrote it: its content "
            "tripped a rule, so it is in quarantine and you cannot read it back. "
            "Tell the user the note was quarantined; do not write it again."
        )
    if signal == "note_kept":
        watched = (
            " AgentBox watches your notes and left this one alone."
            if data.get("watched")
            else " Nothing watches your notes here."
        )
        return f"The note is kept.{watched}"
    if signal == "pkg_block":
        return (
            f"Package Guard refused to install {data.get('package')} and nothing was "
            f"downloaded: {outcome.detail} Tell the user the package was refused."
        )
    if signal == "pkg_hold":
        return (
            f"Package Guard is holding the install of {data.get('package')} until an "
            "operator decides; it has not been installed. Tell the user it is waiting."
        )
    if signal == "pkg_allow":
        return f"{outcome.detail}."
    return (
        f"This could not be completed: {outcome.detail} This is a failure, not a "
        "policy decision; say so plainly and do not guess at a result."
    )
