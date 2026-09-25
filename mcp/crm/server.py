"""crm -- the customer system a support agent works in, as a small local MCP
server bundled with the Support Desk starter. AgentBox scans it, builds it into
its own container and reaches it only through MCP Bridge, under an app-scoped
name such as `<app>__crm`.

Three operations, chosen because the bridge treats each differently:

    find_customer(query)               reads; declared read-only, never held
    save_interaction(customer_id, ...) writes a dated note; an ordinary call
    purge_inactive_customers(years)    classified DESTRUCTIVE by the bridge's
                                       own classifier, so it is held for an
                                       operator's approval before it ever runs

That last one is the point of having a third operation: a queue with nothing
in it demonstrates nothing, and "delete old customer records" is a request a
support agent will genuinely receive.

Records live in a file, seeded on first start from the bundled
`customers.json` (forty invented people; nobody here is real). The path is
this container's own writable layer, which survives a restart but not a
`docker rm` -- run it again and the sample data is back, which is what a demo
wants.
"""

from __future__ import annotations

import json
import re
import os
import shutil
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# host must be 0.0.0.0, not FastMCP's 127.0.0.1 default -- this server runs in
# its own container, reachable from managed-mcp-bridge over the docker
# network, not from a process sharing its own loopback.
mcp = FastMCP("crm", host="0.0.0.0")

_STORE = Path(os.environ.get("CRM_STORE_PATH", "/data/crm.json"))
_SEED = Path(__file__).resolve().parent / "customers.json"
# FastMCP serves requests from a thread pool, so two concurrent writes can
# interleave a read-modify-write and lose one. A lock is cheaper than
# reasoning about whether that can happen in practice.
_LOCK = threading.Lock()


def _read() -> list[dict]:
    if not _STORE.exists() and _SEED.is_file():
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_SEED, _STORE)
    try:
        raw = _STORE.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # A truncated file (killed mid-write) should not make every later
        # call fail. Start over from the seed rather than raise.
        return json.loads(_SEED.read_text(encoding="utf-8")) if _SEED.is_file() else []
    return data if isinstance(data, list) else []


def _write(customers: list[dict]) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: a crash mid-write leaves the previous good file in
    # place instead of a half-written one.
    tmp = _STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(customers, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_STORE)


def _public(customer: dict) -> dict:
    return {
        "id": customer.get("id"),
        "name": customer.get("name"),
        "city": customer.get("city"),
        "customer_since": customer.get("since"),
        "last_contact": customer.get("last_contact"),
        "orders": customer.get("orders"),
        "status": customer.get("status"),
        "notes": list(customer.get("notes") or []),
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_customer(query: str) -> dict:
    """Customer records whose name or customer number matches the query."""
    needle = (query or "").strip().lower()
    if not needle:
        return {"matches": [], "count": 0, "error": "query must not be empty"}
    with _LOCK:
        customers = _read()
    matches = [
        _public(c)
        for c in customers
        if needle in str(c.get("name", "")).lower() or needle == str(c.get("id", "")).lower()
    ]
    return {"matches": matches[:10], "count": len(matches)}


@mcp.tool()
def save_interaction(customer_id: str, summary: str) -> dict:
    """A dated note added to one customer's record; the record's note count comes back."""
    customer_id = (customer_id or "").strip()
    summary = (summary or "").strip()
    if not customer_id or not summary:
        return {"saved": False, "error": "customer_id and summary are both required"}
    with _LOCK:
        customers = _read()
        for customer in customers:
            if str(customer.get("id", "")).lower() == customer_id.lower():
                note = {
                    "date": date.today().isoformat(),
                    "summary": summary[:1000],
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                }
                customer.setdefault("notes", []).append(note)
                customer["last_contact"] = note["date"]
                _write(customers)
                return {"saved": True, "customer_id": customer["id"], "notes": len(customer["notes"])}
    return {"saved": False, "error": f"no customer with id {customer_id}"}


#: A vault placeholder, `[IBAN DE89…3000 · tok_…]` or a bare `tok_…`. This tool
#: gets the REAL number only when an administrator allowed it to receive
#: IBANs; otherwise AgentBox hands it the placeholder unchanged.
_VAULT_TOKEN = re.compile(r"tok_[0-9a-f]{24}")
_IBAN = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}")


@mcp.tool()
def issue_refund(order_id: str, iban: str) -> dict:
    """Refunds one order to the customer's bank account (IBAN); the account's last digits come back.

    The demo of tokenization. AgentBox gives the AI a placeholder instead of
    the IBAN, and swaps the real number in only for a tool an administrator
    allowed to receive IBANs. The answer carries the last four digits, never
    the number, so it reads back cleanly.
    """
    order_id = (order_id or "").strip()
    if _VAULT_TOKEN.search(iban or ""):
        raise ValueError(
            "This tool received a placeholder, not an account number. In AgentBox, "
            "allow crm · issue_refund to receive IBANs (Security > Services > "
            "gateway > Sensitive-data rules)."
        )
    account = re.sub(r"\s+", "", iban or "").upper()
    if not order_id or not _IBAN.fullmatch(account):
        raise ValueError("issue_refund needs an order id and an IBAN")
    return {
        "refunded": True,
        "order_id": order_id,
        "account_ending": account[-4:],
        "reference": f"RF-{order_id}",
    }


@mcp.tool()
def purge_inactive_customers(years: int = 5) -> dict:
    """Deletes every customer record with no contact for more than the given number of years.

    Destructive on purpose, and named so the bridge can tell: this is the
    starter's example of an action AgentBox holds for a manager rather than
    running on request. Approved in the admin console, the same call is made
    again and the records go.
    """
    try:
        years = int(years)
    except (TypeError, ValueError):
        return {"deleted": 0, "error": "years must be a whole number"}
    cutoff = date.today().year - years
    with _LOCK:
        customers = _read()
        keep, gone = [], []
        for customer in customers:
            last = str(customer.get("last_contact") or "")
            year = int(last[:4]) if last[:4].isdigit() else cutoff + 1
            (gone if year < cutoff else keep).append(customer)
        _write(keep)
    return {"deleted": len(gone), "remaining": len(keep), "deleted_ids": [c.get("id") for c in gone]}


if __name__ == "__main__":
    # FastMCP.run()'s default transport is stdio, meant for a client that
    # spawns this process and talks over its stdin/stdout -- confirmed live
    # 2026-08-24: deployed as its own detached container (no attached stdin),
    # stdio mode reads immediate EOF and exits right after startup,
    # crash-looping forever under Docker's restart policy. The bridge reaches
    # this server over HTTP at <container>:8000/sse, which needs sse.
    #
    # The retry loop absorbs a second, separately confirmed issue: freshly
    # joining the already-busy bridge network sometimes is not ready the
    # instant this process starts, and uvicorn's SSE app exits cleanly (code
    # 0, no traceback) if its startup fails.
    for attempt in range(10):
        try:
            mcp.run(transport="sse")
            break
        except BaseException as exc:
            print(f"crm: startup attempt {attempt} failed: {exc!r}", flush=True)
            time.sleep(1)
