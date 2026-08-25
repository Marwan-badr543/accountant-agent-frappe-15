# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""
Repository Layer — Agent Write Gateway
---------------------------------------
All database and Document API operations for agent writes.

Prohibitions (per project_rules.md section 1):
  ❌ NO business authorization or validation rules
  ❌ NO direct handling of API request schemas

TWO ABSOLUTE RULES FOR THIS MODULE
  1. Writes go through the Document API - frappe.get_doc(...).insert()/.submit()/
     .cancel(). Never raw SQL. insert() is what runs check_permission("create"),
     _validate_links(), _validate_mandatory() and every customer server script;
     SQL runs none of them.
  2. ignore_permissions=True appears nowhere in this file. The moment it does,
     the customer's permission configuration becomes advisory. api/tests/
     test_create_structural.py asserts both rules by static inspection.

Candidate lookups are NOT here. Recognising a name - turning "Laptop" into
SKU002 - is a read, it needs no permission grant, and it lives in
agent_api_repository beside the SQL endpoint that has always read the site
without one. See the long note there for why that changed.

What stays here is every write, and rule 2 above is untouched by that move:
insert(), submit(), cancel() and amend() all execute as the agent's own ERP
user under the customer's permissions, and ignore_permissions still appears
nowhere in this file.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import frappe

# Savepoint identifiers are interpolated into SQL by frappe.db.savepoint, so
# they must never derive from caller input. A fixed prefix plus a monotonic
# counter is the whole vocabulary.
_SAVEPOINT_PREFIX = "agent_write_"


def next_savepoint_name(ordinal: int) -> str:
    """A safe, unique savepoint identifier for one document in a batch."""
    return f"{_SAVEPOINT_PREFIX}{int(ordinal)}"


# ─── Write Log ───────────────────────────────────────────────────────────────


def find_write_log_by_key(idempotency_key: str) -> Optional[dict]:
    """Return the existing log row for a key, or None.

    This is the timeout-recovery lookup: a caller that did not receive a
    response resolves the outcome by asking what happened to its key, never by
    retrying the write.
    """
    name = frappe.db.exists("Agent Write Log", {"idempotency_key": idempotency_key})
    if not name:
        return None

    return frappe.db.get_value(
        "Agent Write Log",
        name,
        [
            "name",
            "idempotency_key",
            "action",
            "status",
            "target_doctype",
            "target_docname",
            "docstatus_written",
            "error_code",
            "error_message",
        ],
        as_dict=True,
    )


def reserve_write_log(
    idempotency_key: str,
    action: str,
    target_doctype: str,
    request_digest: str,
    run_id: Optional[str],
    session_id: Optional[str],
    approved_by: Optional[str],
) -> Any:
    """Insert the IN_FLIGHT reservation for a write.

    This runs BEFORE the document insert on purpose: the UNIQUE index on
    idempotency_key becomes the concurrency gate, so two simultaneous replays
    cannot both proceed to create a document. The loser raises
    UniqueValidationError and reads back the winner's result.

    agent_user is taken from the authenticated session, never from the payload.
    """
    log = frappe.get_doc(
        {
            "doctype": "Agent Write Log",
            "idempotency_key": idempotency_key,
            "action": action,
            "status": "IN_FLIGHT",
            "target_doctype": target_doctype,
            "agent_user": frappe.session.user,
            "approved_by": approved_by,
            "request_digest": request_digest,
            "run_id": run_id,
            "session_id": session_id,
        }
    )
    log.insert()
    return log


def commit_write_log(
    log: Any,
    target_docname: str,
    docstatus_written: int,
    amount_written: Optional[float],
    response_snapshot: Optional[dict],
) -> None:
    """Mark a reservation COMMITTED, in the same transaction as the document.

    db_set defaults to commit=False in both v14 and v15 (verified), so this
    stays inside the caller's savepoint. If the document write rolls back, so
    does this - either both exist or neither does, with no window in between.
    """
    written = {
        "status": "COMMITTED",
        "target_docname": target_docname,
        "docstatus_written": docstatus_written,
        "response_snapshot": json.dumps(response_snapshot or {}, default=str)[:100000],
    }

    # A DOCUMENT WITH NO AMOUNT IS NOT A DOCUMENT WITH A NULL AMOUNT.
    #
    # `amount_written` is a Currency field, and Frappe renders those as
    # `decimal(21,9) NOT NULL DEFAULT 0`. Writing an explicit None is therefore
    # not "leave it blank" - it is a NULL into a NOT NULL column, and MariaDB
    # answers (1048, "Column 'amount_written' cannot be null").
    #
    # `_document_amount` returns None for anything without a grand total or a
    # total debit, which is every Item, Customer, Supplier, Cost Center and
    # Warehouse the agent can now be asked to set up. So this one line refused
    # every non-monetary document the agent created - AFTER the customer had
    # approved it, and with the row already reserved.
    #
    # Omitted rather than coerced: the reservation inserted moments ago already
    # left the column at its zero default, and not writing a number we do not
    # have is the honest version of the same result.
    if amount_written is not None:
        written["amount_written"] = amount_written

    log.db_set(written)


def record_failed_attempt(
    idempotency_key: str,
    action: str,
    target_doctype: str,
    request_digest: str,
    run_id: Optional[str],
    session_id: Optional[str],
    error_code: str,
    error_message: str,
) -> None:
    """Persist a refused or failed write in a SEPARATE transaction.

    The document savepoint has already been rolled back by the time this runs,
    which also erased the IN_FLIGHT reservation. Without this, a policy block, a
    permission denial or a probe by a compromised agent would leave no trace in
    the customer's ERP at all - precisely the events a security administrator
    needs to see.

    Uses its own key namespace so a failed attempt never occupies the
    idempotency key of a write that may still succeed on retry.

    Not privileged: frappe.set_user was applied at request start and a savepoint
    rollback does not touch session state, so this insert is still permission
    checked against the Accountant Agent role. If the customer revoked that
    role, this is refused exactly as the document write was - fail-closed in
    both directions.

    Never raises. A logging failure must not replace the error the caller
    actually needs to see.
    """
    try:
        attempt = 0
        base = f"{idempotency_key}:fail"
        while frappe.db.exists("Agent Write Log", {"idempotency_key": f"{base}:{attempt}"}):
            attempt += 1
            if attempt > 50:
                return

        frappe.get_doc(
            {
                "doctype": "Agent Write Log",
                "idempotency_key": f"{base}:{attempt}",
                "action": action,
                "status": "FAILED",
                "target_doctype": target_doctype,
                "agent_user": frappe.session.user,
                "request_digest": request_digest,
                "run_id": run_id,
                "session_id": session_id,
                "error_code": error_code,
                "error_message": (error_message or "")[:2000],
            }
        ).insert()
        frappe.db.commit()
    except Exception as exc:
        frappe.log_error(
            title="Agent write: could not record failed attempt",
            message=f"key={idempotency_key} action={action}: {exc}",
        )


def find_stranded_in_flight(older_than_minutes: int = 60) -> list[dict]:
    """Rows still IN_FLIGHT past a grace period.

    This should always be empty: the reservation and its COMMITTED update live
    in one transaction, so a process kill rolls back both. A surviving IN_FLIGHT
    row is therefore an invariant violation and is alerted on, never swept away
    - a quietly deleted invariant violation is a bug nobody ever finds.
    """
    return frappe.get_all(
        "Agent Write Log",
        filters={
            "status": "IN_FLIGHT",
            "creation": ["<", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-older_than_minutes)],
        },
        fields=["name", "idempotency_key", "action", "target_doctype", "creation"],
        limit=100,
    )


def run_totals_so_far(run_id: str) -> tuple[int, float]:
    """How much this run has already written: document count and money.

    The run's own history, read in ONE aggregate query. A per-run ceiling that
    only ever sees the request in front of it is not a ceiling - a run writing
    one document per call would never reach any limit - so the committed rows
    for this run_id are what the caps are measured against.

    get_list, not get_all: this reads the customer's own audit table and their
    permission configuration applies to it exactly as it does to everything
    else in this module.
    """
    if not run_id:
        return 0, 0.0

    rows = frappe.get_list(
        "Agent Write Log",
        filters={
            "status": "COMMITTED",
            "agent_user": frappe.session.user,
            "run_id": run_id,
        },
        fields=["count(name) as documents", "sum(amount_written) as spent"],
    )
    if not rows:
        return 0, 0.0

    row = rows[0]
    return int(row.get("documents") or 0), float(row.get("spent") or 0.0)


# ─── Document search ─────────────────────────────────────────────────────────


def read_permitted_documents(
    doctype: str,
    filters: list,
    or_filters: Optional[list],
    fields: list[str],
    limit: int,
    order_by: str,
) -> list[dict]:
    """Documents of one DocType that the AGENT USER is allowed to see.

    frappe.get_list, and the choice is load-bearing rather than stylistic.
    read_link_candidates next door uses get_all on purpose: recognising that
    "Laptop" means SKU002 is a name lookup on master data, and gating it would
    make the agent unable to read the customer's own item list.

    This is not that. These are the customer's transactions, and they are read
    so the agent can offer to POST or REVERSE one of them. get_list applies the
    agent user's roles, their User Permissions and every permission query
    condition on the DocType, so a robot restricted to one company is offered
    one company's invoices — and a DocType it may not read raises
    frappe.PermissionError here rather than leaking a row.

    No ignore_permissions, no raw SQL, and no path that mutates anything.
    """
    return frappe.get_list(
        doctype,
        filters=filters,
        or_filters=or_filters,
        fields=fields,
        limit_page_length=limit,
        order_by=order_by,
    )


# ─── Document API writes ─────────────────────────────────────────────────────


def insert_document(payload: dict) -> Any:
    """Create a document through the Document API as the session user.

    No ignore_permissions. insert() runs check_permission("create"),
    _validate_mandatory(), _validate_links(), validate_workflow() and the
    customer's own server scripts. That chain is the product.
    """
    doc = frappe.get_doc(payload)
    doc.insert()
    return doc


def submit_document(doctype: str, docname: str) -> Any:
    """Submit an existing document. Runs check_permission("submit")."""
    doc = frappe.get_doc(doctype, docname)
    doc.submit()
    return doc


def cancel_document(doctype: str, docname: str, reason: Optional[str]) -> Any:
    """Cancel a submitted document. Runs check_permission("cancel")."""
    doc = frappe.get_doc(doctype, docname)
    if reason and doc.meta.has_field("remarks"):
        doc.db_set("remarks", f"{doc.get('remarks') or ''}\n{reason}".strip())
    doc.cancel()
    return doc


def amend_document(doctype: str, docname: str, payload: dict) -> Any:
    """Amend a cancelled document into a new one carrying amended_from."""
    source = frappe.get_doc(doctype, docname)
    amended = frappe.copy_doc(source)
    amended.amended_from = docname
    amended.update(payload or {})
    amended.insert()
    return amended


# ─── Read helpers ────────────────────────────────────────────────────────────


def doctype_exists(doctype: str) -> bool:
    return bool(frappe.db.exists("DocType", doctype))


def get_doctype_meta(doctype: str) -> Any:
    return frappe.get_meta(doctype)


def has_server_script(doctype: str) -> bool:
    """Whether the customer has attached a Server Script to this DocType.

    If they have, the transactional dry-run is disabled for it: we do not
    execute code we have not read inside a savepoint whose rollback cannot undo
    an outbound HTTP call or an email.
    """
    if not frappe.db.exists("DocType", "Server Script"):
        return False
    return bool(
        frappe.db.exists(
            "Server Script", {"reference_doctype": doctype, "disabled": 0}
        )
    )


def list_written_documents(limit: int = 20, target_doctype: Optional[str] = None) -> list[dict]:
    """Documents this agent actually created, newest first.

    The authority for "did the agent write this?", and still the FIRST place a
    lifecycle request is resolved: a document the agent wrote itself, in a
    state where the requested action is possible, is acted on without a
    question, because there is nothing to be wrong about.

    It is no longer the ONLY place. read_permitted_documents searches the
    customer's own documents too, so "post the Zuckerman invoice" works on an
    invoice a human typed. What kept a misunderstood instruction off a human's
    journal entry was this list; what keeps it off now is that a document found
    anywhere else is never acted on until a person has picked it out by name.

    Scoped to the session user, which is always the agent (the router asserts
    it), so one customer's log can never surface in another's.
    """
    filters = {"status": "COMMITTED", "agent_user": frappe.session.user}
    if target_doctype:
        filters["target_doctype"] = target_doctype

    # get_list, not get_all: the customer's permission configuration must apply
    # even to this app's own DocType. get_all would force ignore_permissions and
    # quietly return rows a revoked role should no longer see.
    return frappe.get_list(
        "Agent Write Log",
        filters=filters,
        fields=["target_doctype", "target_docname", "action", "docstatus_written",
                "amount_written", "creation", "run_id", "approved_by"],
        order_by="creation desc",
        limit_page_length=max(1, min(int(limit or 20), 50)),
    )


def get_document_state(doctype: str, docname: str) -> Optional[dict]:
    """Current docstatus and headline amount, read as the agent user."""
    if not frappe.db.exists(doctype, docname):
        return None
    doc = frappe.get_doc(doctype, docname)
    doc.check_permission("read")
    return {
        "doctype": doctype,
        "docname": docname,
        "docstatus": int(doc.docstatus or 0),
        "amount": _headline_amount(doc),
        "posting_date": str(doc.get("posting_date") or ""),
        "company": doc.get("company"),
    }


def _headline_amount(doc: Any) -> Optional[float]:
    for fieldname in ("base_grand_total", "grand_total", "total_debit", "paid_amount"):
        value = doc.get(fieldname)
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def get_write_policy_doc() -> Any:
    """The customer's Agent Write Policy singleton."""
    return frappe.get_single("Agent Write Policy")


def get_session_user() -> str:
    return frappe.session.user
