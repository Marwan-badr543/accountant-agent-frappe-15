# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""
Controller Layer — Agent Write Gateway
---------------------------------------
Whitelisted endpoints for agent writes. Parses input, delegates to the service
layer, maps domain exceptions to HTTP status codes.

Prohibitions (per project_rules.md section 1):
  ❌ NO database or ORM calls
  ❌ NO business validation or calculations
  ❌ NO transaction management

AUTHENTICATION
  Every endpoint here is @frappe.whitelist() with allow_guest ABSENT, i.e.
  False. Callers authenticate with the agent user's own key pair:

      Authorization: token <api_key>:<api_secret>

  Frappe's own validate_auth() runs in the top-level WSGI handler before path
  dispatch (frappe/app.py) and calls frappe.set_user() itself, so everything
  below executes as the agent user with the customer's role permissions, User
  Permissions and permission query conditions fully in force.

  This is deliberately a SEPARATE gateway from agent_api_router.py, which still
  uses the legacy allow_guest=True key scheme for read-only endpoints. The two
  are not mixed: no write may ever inherit the legacy path's assumptions.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import frappe
from frappe import _

from accountant_agent.agent_api.services.agent_write_service import (
    AgentWriteError,
    DocTypeNotAllowedError,
    DryRunOnlyError,
    MissingParameterError,
    NotAgentSessionError,
    PolicyCapExceededError,
    ResourceNotFoundError,
    WritePolicyDisabledError,
    WriteRejectedError,
    amend_existing_document,
    assert_session_is_agent_user,
    build_document_spec,
    cancel_existing_document,
    create_document,
    create_documents_batch,
    get_write_log,
    list_agent_documents,
    load_write_policy,
    preflight_document,
    search_candidates_bulk,
    submit_existing_document,
)

# ─── Exception → HTTP mapping ────────────────────────────────────────────────

_STATUS_BY_EXCEPTION: tuple[tuple[type[Exception], int], ...] = (
    (NotAgentSessionError, 403),
    (WritePolicyDisabledError, 403),
    (DocTypeNotAllowedError, 403),
    (PolicyCapExceededError, 422),
    (DryRunOnlyError, 422),
    (MissingParameterError, 400),
    (ResourceNotFoundError, 404),
    (WriteRejectedError, 422),
)


def _error(exc: AgentWriteError) -> dict:
    """Map a domain exception to a status code and a structured error body.

    Only the domain exception's own message crosses this boundary. Tracebacks,
    table names and Python types never do (project_rules.md section 5).
    """
    status = 500
    for klass, code in _STATUS_BY_EXCEPTION:
        if isinstance(exc, klass):
            status = code
            break
    frappe.local.response.http_status_code = status
    return {"error": exc.message, "error_code": exc.code}


def _unexpected(context: str) -> dict:
    """Log the detail, return nothing revealing."""
    frappe.log_error(title=f"Agent write gateway: {context}", message=frappe.get_traceback())
    frappe.local.response.http_status_code = 500
    return {
        "error": _("The request could not be completed."),
        "error_code": "INTERNAL_ERROR",
    }


def _parse_json_param(value: Any, name: str) -> Any:
    """Accept either a JSON string (form-encoded) or an already-parsed object."""
    if value is None:
        raise MissingParameterError(_("Missing {0} parameter.").format(name))
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        raise MissingParameterError(_("{0} is not valid JSON.").format(name))


# ─── Read-only endpoints ─────────────────────────────────────────────────────


@frappe.whitelist()
def get_document_spec(doctype: Optional[str] = None) -> dict:
    """Field specification for a DocType, so the agent never guesses a field name."""
    try:
        assert_session_is_agent_user()
        return {"spec": build_document_spec(doctype or frappe.form_dict.get("doctype"))}
    except AgentWriteError as exc:
        return _error(exc)
    except Exception:
        return _unexpected("get_document_spec")


@frappe.whitelist()
def search_candidates_bulk_endpoint(
    refs: Optional[str] = None, limit: Optional[int] = None
) -> dict:
    """Permission-filtered candidates for many references in one round trip.

    Read-only, so it runs only the agent-session assertion. It is gated by
    frappe.get_list permissions as the agent user, NOT by the write-policy
    allowlist: a customer may legitimately let the agent look up an account in a
    DocType it may not write.
    """
    try:
        assert_session_is_agent_user()
        parsed = _parse_json_param(refs if refs is not None else frappe.form_dict.get("refs"), "refs")
        return {"results": search_candidates_bulk(parsed, limit=int(limit or 12))}
    except AgentWriteError as exc:
        return _error(exc)
    except Exception:
        return _unexpected("search_candidates_bulk")


@frappe.whitelist()
def get_write_policy() -> dict:
    """The customer's current write policy, so the agent can fail fast and honestly."""
    try:
        assert_session_is_agent_user()
        return {"policy": load_write_policy().as_dict()}
    except AgentWriteError as exc:
        return _error(exc)
    except Exception:
        return _unexpected("get_write_policy")


@frappe.whitelist()
def get_write_log_endpoint(idempotency_key: Optional[str] = None) -> dict:
    """Resolve the outcome of a request whose response was never received.

    The ONLY recovery path for an unknown outcome. A timed-out caller asks what
    happened to its key; it never retries the write and hopes.
    """
    try:
        assert_session_is_agent_user()
        key = idempotency_key or frappe.form_dict.get("idempotency_key")
        return {"log": get_write_log(key)}
    except AgentWriteError as exc:
        return _error(exc)
    except Exception:
        return _unexpected("get_write_log")


@frappe.whitelist()
def list_documents(limit: Optional[int] = None, doctype: Optional[str] = None) -> dict:
    """Documents this agent created, so it can act on its own work only."""
    try:
        assert_session_is_agent_user()
        return list_agent_documents(
            limit=int(limit or 20),
            target_doctype=doctype or frappe.form_dict.get("doctype"),
        )
    except AgentWriteError as exc:
        return _error(exc)
    except Exception:
        return _unexpected("list_documents")


@frappe.whitelist()
def preflight(payload: Optional[str] = None, run_dry_run: Optional[int] = None) -> dict:
    """Validate a document without writing it. Never mutates."""
    try:
        assert_session_is_agent_user()
        parsed = _parse_json_param(
            payload if payload is not None else frappe.form_dict.get("payload"), "payload"
        )
        dry = True if run_dry_run is None else bool(int(run_dry_run))
        return {"preflight": preflight_document(parsed, run_dry_run=dry)}
    except AgentWriteError as exc:
        return _error(exc)
    except Exception:
        return _unexpected("preflight")


# ─── Write endpoints ─────────────────────────────────────────────────────────


@frappe.whitelist()
def create(
    payload: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    approved_by: Optional[str] = None,
) -> dict:
    """Create one document, idempotently."""
    try:
        assert_session_is_agent_user()
        parsed = _parse_json_param(
            payload if payload is not None else frappe.form_dict.get("payload"), "payload"
        )
        return {
            "receipt": create_document(
                payload=parsed,
                idempotency_key=idempotency_key or frappe.form_dict.get("idempotency_key"),
                run_id=run_id or frappe.form_dict.get("run_id"),
                session_id=session_id or frappe.form_dict.get("session_id"),
                approved_by=approved_by or frappe.form_dict.get("approved_by"),
            )
        }
    except AgentWriteError as exc:
        return _error(exc)
    except Exception:
        return _unexpected("create")


@frappe.whitelist()
def submit(
    doctype: Optional[str] = None,
    docname: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    approved_by: Optional[str] = None,
) -> dict:
    """Post a draft to the ledger. Separately authorised from creation."""
    try:
        assert_session_is_agent_user()
        return {
            "receipt": submit_existing_document(
                doctype=doctype or frappe.form_dict.get("doctype"),
                docname=docname or frappe.form_dict.get("docname"),
                idempotency_key=idempotency_key or frappe.form_dict.get("idempotency_key"),
                run_id=run_id or frappe.form_dict.get("run_id"),
                session_id=session_id or frappe.form_dict.get("session_id"),
                approved_by=approved_by or frappe.form_dict.get("approved_by"),
            )
        }
    except AgentWriteError as exc:
        return _error(exc)
    except Exception:
        return _unexpected("submit")


@frappe.whitelist()
def cancel(
    doctype: Optional[str] = None,
    docname: Optional[str] = None,
    reason: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    approved_by: Optional[str] = None,
) -> dict:
    """Reverse a submitted document."""
    try:
        assert_session_is_agent_user()
        return {
            "receipt": cancel_existing_document(
                doctype=doctype or frappe.form_dict.get("doctype"),
                docname=docname or frappe.form_dict.get("docname"),
                reason=reason or frappe.form_dict.get("reason"),
                idempotency_key=idempotency_key or frappe.form_dict.get("idempotency_key"),
                run_id=run_id or frappe.form_dict.get("run_id"),
                session_id=session_id or frappe.form_dict.get("session_id"),
                approved_by=approved_by or frappe.form_dict.get("approved_by"),
            )
        }
    except AgentWriteError as exc:
        return _error(exc)
    except Exception:
        return _unexpected("cancel")


@frappe.whitelist()
def amend(
    doctype: Optional[str] = None,
    docname: Optional[str] = None,
    payload: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    approved_by: Optional[str] = None,
) -> dict:
    """Create a corrected successor to a cancelled document."""
    try:
        assert_session_is_agent_user()
        parsed = _parse_json_param(
            payload if payload is not None else frappe.form_dict.get("payload"), "payload"
        )
        return {
            "receipt": amend_existing_document(
                doctype=doctype or frappe.form_dict.get("doctype"),
                docname=docname or frappe.form_dict.get("docname"),
                payload=parsed,
                idempotency_key=idempotency_key or frappe.form_dict.get("idempotency_key"),
                run_id=run_id or frappe.form_dict.get("run_id"),
                session_id=session_id or frappe.form_dict.get("session_id"),
                approved_by=approved_by or frappe.form_dict.get("approved_by"),
            )
        }
    except AgentWriteError as exc:
        return _error(exc)
    except Exception:
        return _unexpected("amend")


@frappe.whitelist()
def create_batch(
    documents: Optional[str] = None,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    approved_by: Optional[str] = None,
) -> dict:
    """Create many documents in one transaction, one savepoint each.

    All-or-nothing within a document; continue-on-error across documents.
    """
    try:
        assert_session_is_agent_user()
        parsed = _parse_json_param(
            documents if documents is not None else frappe.form_dict.get("documents"), "documents"
        )
        return {
            "batch": create_documents_batch(
                documents=parsed,
                run_id=run_id or frappe.form_dict.get("run_id"),
                session_id=session_id or frappe.form_dict.get("session_id"),
                approved_by=approved_by or frappe.form_dict.get("approved_by"),
            )
        }
    except AgentWriteError as exc:
        return _error(exc)
    except Exception:
        return _unexpected("create_batch")
