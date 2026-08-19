# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""
Service Layer — Agent Write Gateway
------------------------------------
Pure business logic for agent writes. Protocol-agnostic: it raises typed domain
exceptions and knows nothing about HTTP status codes.

Prohibitions (per project_rules.md section 1):
  ❌ NO HTTP framework imports
  ❌ NO direct HTTP exceptions

THE GUARD CHAIN
  Every write endpoint runs, in order:
      assert_session_is_agent_user()   - the request is genuinely the agent
      assert_write_policy_enabled()    - the customer switched writes on
      assert_doctype_allowed(action)   - this action on this DocType is permitted
      assert_within_policy_caps()      - counts, amounts, posting-date window

  Candidate search and spec lookup are READ-ONLY and run only the first
  assertion. They are gated by frappe.get_list permissions as the agent user,
  not by the write-policy allowlist: a customer may legitimately let the agent
  look up an account in a DocType it may not write. Making the write allowlist
  govern reads would build a second permission model competing with the ERP's
  own, which this design forbids.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import frappe
from frappe import _

from accountant_agent.agent_api.db.agent_write_repository import (
    amend_document,
    cancel_document,
    commit_write_log,
    count_link_candidates,
    doctype_exists,
    find_write_log_by_key,
    get_doctype_meta,
    get_document_state,
    list_written_documents,
    get_session_user,
    get_write_policy_doc,
    has_server_script,
    insert_document,
    next_savepoint_name,
    record_failed_attempt,
    reserve_write_log,
    search_link_candidates,
    submit_document,
)

# ─── Domain Exceptions (protocol-agnostic) ───────────────────────────────────


class AgentWriteError(Exception):
    """Base for every write-gateway domain failure."""

    code: str = "WRITE_ERROR"

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        self.message = message
        if code:
            self.code = code
        super().__init__(message)


class NotAgentSessionError(AgentWriteError):
    """The authenticated user is not the provisioned agent user."""

    code = "NOT_AGENT_SESSION"


class WritePolicyDisabledError(AgentWriteError):
    code = "WRITE_POLICY_DISABLED"


class DocTypeNotAllowedError(AgentWriteError):
    code = "DOCTYPE_NOT_ALLOWED"


class PolicyCapExceededError(AgentWriteError):
    code = "POLICY_BLOCKED"


class DryRunOnlyError(AgentWriteError):
    code = "DRY_RUN_ONLY"


class MissingParameterError(AgentWriteError):
    code = "MISSING_PARAMETER"


class ResourceNotFoundError(AgentWriteError):
    code = "NOT_FOUND"


class WriteRejectedError(AgentWriteError):
    """The ERP refused the write. Carries the mapped reason."""

    code = "WRITE_REJECTED"


# ─── Constants ───────────────────────────────────────────────────────────────

AGENT_USER: str = "accountant-agent@agent.local"

MAX_CANDIDATES_OFFERED: int = 12
MAX_CANDIDATE_SCAN: int = 50
MAX_BULK_REFS: int = 100
MAX_BATCH_SIZE: int = 50

VALID_ACTIONS: frozenset[str] = frozenset({"create", "submit", "cancel", "amend"})

#: DocTypes whose validate() has been read and confirmed free of non-transactional
#: side effects (no enqueue, no sendmail, no publish_realtime), so a savepoint
#: rollback genuinely undoes everything the dry run touched. Verified against
#: ERPNext v14 and v15 for this list; anything else gets static preflight only.
DRY_RUN_CERTIFIED: frozenset[str] = frozenset(
    {
        "Journal Entry",
        "Payment Entry",
        "Sales Invoice",
        "Purchase Invoice",
        "Customer",
        "Supplier",
    }
)


# ─── Typed snapshots ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DocTypePermission:
    document_type: str
    allow_create: bool
    allow_submit: bool
    allow_cancel: bool
    allow_amend: bool
    auto_submit_ceiling_amount: float


@dataclass(frozen=True, slots=True)
class WritePolicy:
    """An immutable read of the customer's Agent Write Policy."""

    enabled: bool
    dry_run_only: bool
    require_approval: bool
    max_documents_per_run: int
    max_total_amount_per_run: float
    posting_date_max_days_back: int
    posting_date_max_days_forward: int
    allowed_document_types: tuple[DocTypePermission, ...]
    allowed_companies: tuple[str, ...]
    blocked_accounts: tuple[str, ...]

    def permission_for(self, doctype: str) -> Optional[DocTypePermission]:
        for row in self.allowed_document_types:
            if row.document_type == doctype:
                return row
        return None

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "dry_run_only": self.dry_run_only,
            "require_approval": self.require_approval,
            "max_documents_per_run": self.max_documents_per_run,
            "max_total_amount_per_run": self.max_total_amount_per_run,
            "posting_date_max_days_back": self.posting_date_max_days_back,
            "posting_date_max_days_forward": self.posting_date_max_days_forward,
            "allowed_document_types": [
                {
                    "doctype": r.document_type,
                    "create": r.allow_create,
                    "submit": r.allow_submit,
                    "cancel": r.allow_cancel,
                    "amend": r.allow_amend,
                    "auto_submit_ceiling_amount": r.auto_submit_ceiling_amount,
                }
                for r in self.allowed_document_types
            ],
            "allowed_companies": list(self.allowed_companies),
            "blocked_accounts": list(self.blocked_accounts),
        }


@dataclass(frozen=True, slots=True)
class PreflightFinding:
    severity: str  # BLOCKING | ASK_USER | WARNING | INFO
    code: str
    field_path: str
    raw_value: Optional[str]
    human_message: str

    def as_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "field_path": self.field_path,
            "raw_value": self.raw_value,
            "human_message": self.human_message,
        }


# ─── Guards ──────────────────────────────────────────────────────────────────


def assert_session_is_agent_user() -> str:
    """Refuse any request not authenticated as the provisioned agent user.

    The anti-confused-deputy check. Frappe's token auth skips frappe.set_user
    when a session cookie already set a non-Guest user, so a request carrying
    both a human's cookie and the agent's token would otherwise execute as the
    human. This is the server-side assertion that closes it - a convention on
    the client would not.
    """
    session_user = get_session_user()
    if session_user in ("", "Guest", None):
        raise NotAgentSessionError(_("Authentication required."), code="NOT_AUTHENTICATED")
    if session_user != AGENT_USER:
        raise NotAgentSessionError(
            _("This endpoint may only be used by the Accountant Agent account.")
        )
    return session_user


def load_write_policy() -> WritePolicy:
    """Read the customer's policy fresh. Never cached across requests.

    The customer tightening their policy five minutes ago must be respected on
    the very next call; a cached policy is a security window.
    """
    if not doctype_exists("Agent Write Policy"):
        return WritePolicy(
            enabled=False, dry_run_only=False, require_approval=True,
            max_documents_per_run=0, max_total_amount_per_run=0.0,
            posting_date_max_days_back=0, posting_date_max_days_forward=0,
            allowed_document_types=(), allowed_companies=(), blocked_accounts=(),
        )

    doc = get_write_policy_doc()
    return WritePolicy(
        enabled=bool(doc.enabled),
        dry_run_only=bool(doc.dry_run_only),
        require_approval=bool(doc.require_approval),
        max_documents_per_run=int(doc.max_documents_per_run or 0),
        max_total_amount_per_run=float(doc.max_total_amount_per_run or 0),
        posting_date_max_days_back=int(doc.posting_date_max_days_back or 0),
        posting_date_max_days_forward=int(doc.posting_date_max_days_forward or 0),
        allowed_document_types=tuple(
            DocTypePermission(
                document_type=row.document_type,
                allow_create=bool(row.allow_create),
                allow_submit=bool(row.allow_submit),
                allow_cancel=bool(row.allow_cancel),
                allow_amend=bool(row.allow_amend),
                auto_submit_ceiling_amount=float(row.auto_submit_ceiling_amount or 0),
            )
            for row in (doc.allowed_document_types or [])
        ),
        allowed_companies=tuple(
            row.company for row in (doc.allowed_companies or []) if row.company
        ),
        blocked_accounts=tuple(
            row.account for row in (doc.blocked_accounts or []) if row.account
        ),
    )


def assert_write_policy_enabled(policy: WritePolicy) -> None:
    if not policy.enabled:
        raise WritePolicyDisabledError(
            _(
                "Agent writing is switched off in this system. A System Manager "
                "can enable it in Agent Write Policy."
            )
        )


def assert_doctype_allowed(policy: WritePolicy, doctype: str, action: str) -> DocTypePermission:
    """Both the policy AND the agent user's ERP permission must allow the action.

    This checks the policy half. The ERP half is enforced by the Document API
    itself at insert/submit/cancel time, as the agent user. Neither substitutes
    for the other.
    """
    if action not in VALID_ACTIONS:
        raise MissingParameterError(_("Unknown action '{0}'.").format(action))

    permission = policy.permission_for(doctype)
    if permission is None:
        raise DocTypeNotAllowedError(
            _(
                "{0} is not in the list of document types this agent may write. "
                "A System Manager can add it in Agent Write Policy."
            ).format(doctype)
        )

    allowed = {
        "create": permission.allow_create,
        "submit": permission.allow_submit,
        "cancel": permission.allow_cancel,
        "amend": permission.allow_amend,
    }[action]

    if not allowed:
        raise DocTypeNotAllowedError(
            _("The agent is not permitted to {0} {1} in this system.").format(action, doctype),
            code=f"{action.upper()}_NOT_PERMITTED",
        )
    return permission


def assert_within_policy_caps(policy: WritePolicy, payload: dict, doctype: str) -> None:
    """Posting-date window, company allow-list and blocked accounts."""
    _assert_posting_date_window(policy, payload)
    _assert_company_allowed(policy, payload)
    _assert_no_blocked_accounts(policy, payload, doctype)


def _assert_posting_date_window(policy: WritePolicy, payload: dict) -> None:
    posting_date = payload.get("posting_date") or payload.get("transaction_date")
    if not posting_date:
        return

    from frappe.utils import date_diff, getdate, nowdate

    delta = date_diff(getdate(posting_date), getdate(nowdate()))
    if delta < 0 and policy.posting_date_max_days_back:
        if abs(delta) > policy.posting_date_max_days_back:
            raise PolicyCapExceededError(
                _(
                    "The date {0} is more than {1} days in the past, which is "
                    "outside the window this system allows the agent to post in."
                ).format(posting_date, policy.posting_date_max_days_back)
            )
    if delta > policy.posting_date_max_days_forward:
        raise PolicyCapExceededError(
            _(
                "The date {0} is in the future. This system does not allow the "
                "agent to post future-dated entries."
            ).format(posting_date)
            if not policy.posting_date_max_days_forward
            else _(
                "The date {0} is more than {1} days ahead, which is outside the "
                "window this system allows."
            ).format(posting_date, policy.posting_date_max_days_forward)
        )


def _assert_company_allowed(policy: WritePolicy, payload: dict) -> None:
    if not policy.allowed_companies:
        return
    company = payload.get("company")
    if company and company not in policy.allowed_companies:
        raise PolicyCapExceededError(
            _("The agent is not permitted to post in {0}.").format(company)
        )


def _assert_no_blocked_accounts(policy: WritePolicy, payload: dict, doctype: str) -> None:
    if not policy.blocked_accounts:
        return

    blocked = set(policy.blocked_accounts)
    touched: set[str] = set()

    for key in ("account", "paid_from", "paid_to", "debit_to", "credit_to"):
        if payload.get(key):
            touched.add(payload[key])

    for value in payload.values():
        if isinstance(value, list):
            for row in value:
                if isinstance(row, dict) and row.get("account"):
                    touched.add(row["account"])

    offending = sorted(touched & blocked)
    if offending:
        raise PolicyCapExceededError(
            _("The agent is not permitted to post to {0} in this system.").format(
                ", ".join(offending)
            )
        )


def assert_run_caps(policy: WritePolicy, document_count: int, total_amount: float) -> None:
    """Blast-radius caps for a whole run."""
    if policy.max_documents_per_run and document_count > policy.max_documents_per_run:
        raise PolicyCapExceededError(
            _(
                "This run would create {0} documents, above the limit of {1} set "
                "in this system."
            ).format(document_count, policy.max_documents_per_run)
        )
    if policy.max_total_amount_per_run and abs(total_amount) > policy.max_total_amount_per_run:
        raise PolicyCapExceededError(
            _(
                "This run totals {0}, above the per-run limit of {1} set in this "
                "system."
            ).format(total_amount, policy.max_total_amount_per_run)
        )


def assert_not_dry_run(policy: WritePolicy) -> None:
    if policy.dry_run_only:
        raise DryRunOnlyError(
            _(
                "This system is in evaluation mode: the agent validates entries "
                "but does not record them. A System Manager can change this in "
                "Agent Write Policy."
            )
        )


# ─── Document spec ───────────────────────────────────────────────────────────


def build_document_spec(doctype: str) -> dict:
    """Field specification for a DocType, so the agent never guesses a field name."""
    if not doctype:
        raise MissingParameterError(_("Missing doctype parameter."))
    if not doctype_exists(doctype):
        raise ResourceNotFoundError(_("Document type '{0}' does not exist.").format(doctype))

    meta = get_doctype_meta(doctype)
    fields: list[dict] = []
    child_tables: list[dict] = []

    for df in meta.fields:
        if df.fieldtype in ("Section Break", "Column Break", "Tab Break", "HTML", "Button"):
            continue
        entry = {
            "fieldname": df.fieldname,
            "label": df.label,
            "fieldtype": df.fieldtype,
            "reqd": bool(df.reqd),
            "read_only": bool(df.read_only),
            "options": df.options,
            "default": df.default,
        }
        if df.fieldtype == "Table":
            child_tables.append(
                {
                    "fieldname": df.fieldname,
                    "label": df.label,
                    "child_doctype": df.options,
                    "fields": _child_field_spec(df.options),
                }
            )
        else:
            fields.append(entry)

    return {
        "doctype": doctype,
        "is_submittable": bool(meta.is_submittable),
        "is_single": bool(meta.issingle),
        "autoname": meta.autoname,
        "title_field": meta.title_field,
        "fields": fields,
        "child_tables": child_tables,
        "supports_dry_run_validate": _supports_dry_run(doctype),
        "savepoint_safe": doctype in DRY_RUN_CERTIFIED,
    }


def _child_field_spec(child_doctype: Optional[str]) -> list[dict]:
    if not child_doctype or not doctype_exists(child_doctype):
        return []
    meta = get_doctype_meta(child_doctype)
    return [
        {
            "fieldname": df.fieldname,
            "label": df.label,
            "fieldtype": df.fieldtype,
            "reqd": bool(df.reqd),
            "options": df.options,
            "default": df.default,
        }
        for df in meta.fields
        if df.fieldtype not in ("Section Break", "Column Break", "Tab Break", "HTML", "Button")
    ]


def _supports_dry_run(doctype: str) -> bool:
    """Whether the transactional dry run may run for this DocType.

    Off unless the DocType is on the certified list AND the customer has no
    Server Script attached to it. We do not execute code we have not read inside
    a savepoint, because a savepoint cannot roll back an outbound HTTP call.
    """
    if doctype not in DRY_RUN_CERTIFIED:
        return False
    return not has_server_script(doctype)


# ─── Candidate resolution ────────────────────────────────────────────────────

#: How a reference kind maps onto a DocType and the fields worth searching and
#: displaying. Everything here is derived programmatically - the agent's language
#: model never chooses a DocType or a filter.
_REF_KIND_SPEC: dict[str, dict] = {
    "ACCOUNT": {
        "doctype": "Account",
        "search_fields": ("name", "account_name", "account_number"),
        "display_fields": ("name", "account_name", "account_number", "root_type",
                           "account_type", "company", "account_currency"),
        "base_filters": {"disabled": 0, "is_group": 0},
    },
    "PARTY_CUSTOMER": {
        "doctype": "Customer",
        "search_fields": ("name", "customer_name"),
        "display_fields": ("name", "customer_name", "customer_group", "territory"),
        "base_filters": {"disabled": 0},
    },
    "PARTY_SUPPLIER": {
        "doctype": "Supplier",
        "search_fields": ("name", "supplier_name"),
        "display_fields": ("name", "supplier_name", "supplier_group"),
        "base_filters": {"disabled": 0},
    },
    "COST_CENTER": {
        "doctype": "Cost Center",
        "search_fields": ("name", "cost_center_name", "cost_center_number"),
        "display_fields": ("name", "cost_center_name", "company"),
        "base_filters": {"disabled": 0, "is_group": 0},
    },
    "ITEM": {
        "doctype": "Item",
        "search_fields": ("name", "item_name", "item_code"),
        "display_fields": ("name", "item_name", "item_group", "stock_uom"),
        "base_filters": {"disabled": 0},
    },
    "COMPANY": {
        "doctype": "Company",
        "search_fields": ("name", "company_name", "abbr"),
        "display_fields": ("name", "company_name", "abbr", "default_currency"),
        "base_filters": {},
    },
}


def search_candidates_bulk(refs: Sequence[dict], limit: int = MAX_CANDIDATES_OFFERED) -> dict:
    """Permission-filtered candidates for MANY references in one round trip.

    Deliberately plural. A per-reference call would be an N+1 network pattern:
    a batch with 40 distinct unresolved parties would cost 40 sequential HTTPS
    round trips to the customer's ERP.

    Every result carries total_matched and truncated, so a cut-off candidate set
    is never silent - the caller either offers "show more" or asks a narrowing
    question instead of quietly hiding the right answer.
    """
    if not refs:
        return {}
    if len(refs) > MAX_BULK_REFS:
        raise MissingParameterError(
            _("At most {0} references may be resolved in one request.").format(MAX_BULK_REFS)
        )

    limit = max(1, min(int(limit or MAX_CANDIDATES_OFFERED), MAX_CANDIDATES_OFFERED))
    results: dict[str, dict] = {}

    for ref in refs:
        ref_id = str(ref.get("ref_id") or "")
        if not ref_id:
            continue
        try:
            results[ref_id] = _search_one_reference(ref, limit)
        except frappe.PermissionError:
            # The agent cannot read this DocType at all. That is a legitimate
            # customer configuration, not an error - report an empty set with a
            # reason rather than failing the whole batch.
            results[ref_id] = {
                "ref_id": ref_id,
                "candidates": [],
                "total_matched": 0,
                "truncated": False,
                "error_code": "READ_NOT_PERMITTED",
            }
    return results


def _search_one_reference(ref: dict, limit: int) -> dict:
    ref_id = str(ref.get("ref_id"))
    kind = str(ref.get("kind") or "").upper()
    raw_value = (ref.get("raw_value") or "").strip()
    context = ref.get("context") or {}

    spec = _REF_KIND_SPEC.get(kind)
    target_doctype = ref.get("target_doctype") or (spec or {}).get("doctype")
    if not target_doctype or not doctype_exists(target_doctype):
        return {
            "ref_id": ref_id, "candidates": [], "total_matched": 0,
            "truncated": False, "error_code": "UNKNOWN_REFERENCE_KIND",
        }

    meta = get_doctype_meta(target_doctype)
    available = {df.fieldname for df in meta.fields} | {"name"}

    search_fields = [
        f for f in ((spec or {}).get("search_fields") or ("name",)) if f in available
    ] or ["name"]
    display_fields = [
        f for f in ((spec or {}).get("display_fields") or ("name",)) if f in available
    ] or ["name"]

    filters = {
        key: value
        for key, value in ((spec or {}).get("base_filters") or {}).items()
        if key in available
    }
    filters.update(_context_filters(context, available))

    or_filters = (
        [[field, "like", f"%{raw_value}%"] for field in search_fields]
        if raw_value
        else None
    )

    total = count_link_candidates(target_doctype, filters, or_filters)
    rows = search_link_candidates(
        doctype=target_doctype,
        filters=filters,
        or_filters=or_filters,
        fields=display_fields,
        limit=min(limit, MAX_CANDIDATE_SCAN),
        offset=int(ref.get("offset") or 0),
        order_by="name asc",
    )

    return {
        "ref_id": ref_id,
        "target_doctype": target_doctype,
        "candidates": [
            {"value": row.get("name"), "display": _display_label(row, display_fields), "fields": row}
            for row in rows
        ],
        "total_matched": total,
        "truncated": total > len(rows),
        "next_offset": (int(ref.get("offset") or 0) + len(rows)) if total > len(rows) else None,
    }


def _context_filters(context: dict, available: set[str]) -> dict:
    """Narrow the candidate pool by accounting context, computed not guessed."""
    filters: dict[str, Any] = {}
    for key in ("company", "root_type", "account_type", "party_type"):
        value = context.get(key)
        if value and key in available:
            filters[key] = value
    return filters


def _display_label(row: dict, display_fields: Sequence[str]) -> str:
    """A label an accountant recognises, built from whichever fields exist."""
    name = row.get("name") or ""
    extras = [
        str(row.get(f))
        for f in display_fields
        if f not in ("name",) and row.get(f) and str(row.get(f)) not in name
    ]
    return f"{name} ({', '.join(extras[:3])})" if extras else name


# ─── Preflight ───────────────────────────────────────────────────────────────


def preflight_document(payload: dict, run_dry_run: bool = True) -> dict:
    """Validate a document without writing it. Never mutates.

    Two tiers:
      A - static: permission, mandatory fields, link integrity. Always runs, fires
          no after_insert/on_submit hooks, so no email or background job escapes.
      B - transactional: the DocType's real validate() inside a savepoint that is
          always rolled back. Full fidelity, opt-in per DocType.

    The point of preflight is not to duplicate the ERP's judgement - it is to
    turn "the ERP would refuse this" into a specific, answerable question before
    the user is asked to approve anything.
    """
    doctype = (payload or {}).get("doctype")
    if not doctype:
        raise MissingParameterError(_("Missing doctype in payload."))
    if not doctype_exists(doctype):
        raise ResourceNotFoundError(_("Document type '{0}' does not exist.").format(doctype))

    findings: list[PreflightFinding] = []
    doc = frappe.get_doc(payload)
    _prepare_like_insert(doc)

    findings.extend(_preflight_permission(doc, doctype))
    findings.extend(_preflight_links(doc))
    findings.extend(_preflight_mandatory(doc))
    findings.extend(_preflight_frozen_period(payload))

    dry_run_ran = False
    if run_dry_run and _supports_dry_run(doctype):
        extra, dry_run_ran = _preflight_transactional(doc)
        findings.extend(extra)

    blocking = [f for f in findings if f.severity == "BLOCKING"]
    ask_user = [f for f in findings if f.severity == "ASK_USER"]

    return {
        "doctype": doctype,
        "ok": not blocking and not ask_user,
        "findings": [f.as_dict() for f in findings],
        "dry_run_performed": dry_run_ran,
        "dry_run_available": _supports_dry_run(doctype),
    }


#: Fields the framework populates for itself during insert(). A user must never
#: be asked "which naming series?" or "what parent?" - those are not accounting
#: questions, and surfacing them would make the agent look like a form validator
#: rather than an accountant.
_FRAMEWORK_FIELDS: frozenset[str] = frozenset(
    {
        "naming_series", "name", "parent", "parenttype", "parentfield", "idx",
        "docstatus", "owner", "creation", "modified", "modified_by", "amended_from",
    }
)


def _prepare_like_insert(doc: Any) -> None:
    """Apply exactly the preparation insert() does, stopping short of naming.

    insert() runs, in order (identical in v14 and v15):

        _set_defaults() -> set_user_and_timestamp() -> set_docstatus()
        -> check_permission("create") -> check_if_latest() -> _validate_links()
        -> before_insert -> set_new_name() -> set_parent_in_children()
        -> run_before_save_methods() [validate] -> _validate() [mandatory]

    Preflight replays that prefix but deliberately NEVER calls set_new_name(),
    which is the only step that touches tabSeries. That is what makes a dry run
    consume no voucher number and leave no gap in the customer's numbering.

    Without this preparation, preflight reports defaults and parent links as
    missing and asks the user nonsense questions about them.
    """
    for step in ("_set_defaults", "set_user_and_timestamp", "set_docstatus", "set_parent_in_children"):
        method = getattr(doc, step, None)
        if method is None:
            continue
        try:
            method()
        except Exception:
            # Preparation is best-effort: its only purpose is to reduce noise in
            # the findings. A failure here must not mask the real validation
            # result, which the subsequent checks produce anyway.
            pass


def _preflight_permission(doc: Any, doctype: str) -> list[PreflightFinding]:
    try:
        doc.check_permission("create")
    except frappe.PermissionError:
        return [
            PreflightFinding(
                severity="BLOCKING",
                code="PERMISSION_DENIED",
                field_path="",
                raw_value=None,
                human_message=_(
                    "The agent is not authorised to create {0} records in this "
                    "system. A System Manager can grant that on the Accountant "
                    "Agent user."
                ).format(doctype),
            )
        ]
    return []


def _preflight_links(doc: Any) -> list[PreflightFinding]:
    """Structured link failures - the input to the 'did you mean' loop.

    Calls get_invalid_links() directly rather than _validate_links(), because
    the former returns (fieldname, failed_value, label) tuples while the latter
    throws them away into a joined message string. Those tuples are exactly what
    the resolution engine needs to generate candidates.
    """
    findings: list[PreflightFinding] = []
    try:
        invalid, cancelled = doc.get_invalid_links()
    except Exception:
        return findings

    for fieldname, value, label in invalid:
        findings.append(
            PreflightFinding(
                severity="ASK_USER",
                code="LINK_NOT_FOUND",
                field_path=fieldname,
                raw_value=str(value) if value is not None else None,
                human_message=str(label),
            )
        )

    for child in doc.get_all_children():
        try:
            child_invalid, child_cancelled = child.get_invalid_links(
                is_submittable=doc.meta.is_submittable
            )
        except Exception:
            continue
        for fieldname, value, label in child_invalid:
            findings.append(
                PreflightFinding(
                    severity="ASK_USER",
                    code="LINK_NOT_FOUND",
                    field_path=f"{child.parentfield}[{child.idx}].{fieldname}",
                    raw_value=str(value) if value is not None else None,
                    human_message=str(label),
                )
            )
        for fieldname, value, label in child_cancelled:
            findings.append(
                PreflightFinding(
                    severity="BLOCKING",
                    code="LINK_CANCELLED",
                    field_path=f"{child.parentfield}[{child.idx}].{fieldname}",
                    raw_value=str(value) if value is not None else None,
                    human_message=str(label),
                )
            )

    for fieldname, value, label in cancelled:
        findings.append(
            PreflightFinding(
                severity="BLOCKING",
                code="LINK_CANCELLED",
                field_path=fieldname,
                raw_value=str(value) if value is not None else None,
                human_message=str(label),
            )
        )
    return findings


def _preflight_mandatory(doc: Any) -> list[PreflightFinding]:
    """Missing required fields, one finding per field, framework noise removed.

    Uses _get_missing_mandatory_fields() rather than _validate_mandatory():
    the former returns (fieldname, message) tuples, the latter joins them into
    one string and raises. One finding per field is what lets the agent ask one
    precise question per gap instead of pasting an error at the user.
    """
    findings: list[PreflightFinding] = []

    def collect(target: Any, path_prefix: str = "") -> None:
        getter = getattr(target, "_get_missing_mandatory_fields", None)
        if getter is None:
            return
        try:
            missing = getter()
        except Exception:
            return
        for fieldname, message in missing:
            if fieldname in _FRAMEWORK_FIELDS:
                continue
            findings.append(
                PreflightFinding(
                    severity="ASK_USER",
                    code="MANDATORY_MISSING",
                    field_path=f"{path_prefix}{fieldname}",
                    raw_value=None,
                    human_message=_clean_message(str(message)),
                )
            )

    collect(doc)
    try:
        for child in doc.get_all_children():
            collect(child, f"{child.parentfield}[{child.idx}].")
    except Exception:
        pass
    return findings


def _preflight_frozen_period(payload: dict) -> list[PreflightFinding]:
    """Detect a closed accounting period before attempting to post into it.

    ERPNext enforces this in general_ledger.py, but raises a bare
    ValidationError whose only distinguishing feature is its English message.
    Depending on that string would break under a translated site, so the check
    is done directly against Accounts Settings here and message matching is kept
    only as a backstop in _classify_exception.

    Note the role test mirrors ERPNext's own: it is evaluated for the SESSION
    user, which is the agent - so this correctly reflects whether the customer
    granted the agent the frozen-accounts modifier role.
    """
    posting_date = (payload or {}).get("posting_date") or (payload or {}).get("transaction_date")
    if not posting_date:
        return []
    if not doctype_exists("Accounts Settings"):
        return []

    frozen_upto = frappe.db.get_single_value("Accounts Settings", "acc_frozen_upto")
    if not frozen_upto:
        return []

    from frappe.utils import format_date, getdate

    if getdate(posting_date) > getdate(frozen_upto):
        return []

    modifier_role = frappe.db.get_single_value("Accounts Settings", "frozen_accounts_modifier")
    if modifier_role and modifier_role in frappe.get_roles():
        return []

    return [
        PreflightFinding(
            severity="BLOCKING",
            code="PERIOD_FROZEN",
            field_path="posting_date",
            raw_value=str(posting_date),
            human_message=_(
                "The books in this system are closed up to {0}, so an entry "
                "dated {1} cannot be recorded."
            ).format(format_date(frozen_upto), format_date(posting_date)),
        )
    ]


def _preflight_transactional(doc: Any) -> tuple[list[PreflightFinding], bool]:
    """Run the DocType's real validate() inside an always-rolled-back savepoint.

    Verified prerequisite: none of the certified DocTypes calls frappe.db.commit
    on this path in v14 or v15, so the rollback genuinely undoes everything. A
    commit inside a controller would destroy the enclosing savepoint, which is
    why the certified list is a closed set rather than an allow-all.

    Note also that set_new_name() runs inside insert(), never inside validate(),
    so this consumes no naming-series number and leaves no voucher gap.
    """
    savepoint = "agent_preflight"
    findings: list[PreflightFinding] = []
    try:
        frappe.db.savepoint(savepoint)
        doc.run_method("validate")
    except frappe.PermissionError as exc:
        findings.append(
            PreflightFinding("BLOCKING", "PERMISSION_DENIED", "", None, _user_message("PERMISSION_DENIED", str(exc)))
        )
    except Exception as exc:
        findings.append(
            PreflightFinding(
                severity="BLOCKING",
                code=_classify_exception(exc),
                field_path="",
                raw_value=None,
                human_message=_user_message(_classify_exception(exc), str(exc)),
            )
        )
    finally:
        try:
            frappe.db.rollback(save_point=savepoint)
        except Exception:
            frappe.log_error(
                title="Agent preflight: savepoint rollback failed",
                message=frappe.get_traceback(),
            )
    return findings, True


# ─── Error classification ────────────────────────────────────────────────────

#: Typed Frappe exceptions -> stable codes the agent maps to accountant language.
#: All of these classes are present in both v14 and v15 (verified).
_EXCEPTION_CODES: tuple[tuple[str, str], ...] = (
    ("PermissionError", "PERMISSION_DENIED"),
    ("MandatoryError", "MANDATORY_MISSING"),
    ("CancelledLinkError", "LINK_CANCELLED"),
    ("LinkValidationError", "LINK_NOT_FOUND"),
    ("LinkExistsError", "HAS_DEPENDENTS"),
    ("TimestampMismatchError", "CONCURRENT_MODIFICATION"),
    ("UniqueValidationError", "DUPLICATE"),
    ("DuplicateEntryError", "DUPLICATE"),
)

#: ERPNext raises the closed-period error as a bare ValidationError with no
#: distinguishing type, so the only signal in the exception itself is its text.
#: Message matching is fragile, which is why _preflight_frozen_period checks
#: Accounts Settings directly and is the primary detection path; this is the
#: fallback for the case where the ERP refuses at posting time anyway.
_FROZEN_PERIOD_PATTERN = re.compile(
    r"not authorized to add or update entries before", re.IGNORECASE
)


#: Caught to detect an idempotency-key collision. Bound directly rather than via
#: getattr with an Exception fallback: a missing attribute would silently widen
#: the except clause to catch everything and turn every genuine write failure
#: into a bogus "already recorded". Both classes are verified present in v14 and
#: v15, so an AttributeError here is the correct, loud failure.
_DUPLICATE_KEY_ERRORS: tuple[type[BaseException], ...] = (
    frappe.UniqueValidationError,
    frappe.DuplicateEntryError,
)


def _classify_exception(exc: BaseException) -> str:
    """Map an ERP exception to a stable, user-mappable code."""
    for class_name, code in _EXCEPTION_CODES:
        klass = getattr(frappe, class_name, None)
        if klass is not None and isinstance(exc, klass):
            return code

    if _FROZEN_PERIOD_PATTERN.search(str(exc) or ""):
        return "PERIOD_FROZEN"

    if isinstance(exc, getattr(frappe, "ValidationError", Exception)):
        return "VALIDATION_FAILED"
    return "WRITE_REJECTED"


_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")

#: Fallbacks for the case where the ERP raises with an empty message. Observed
#: with frappe.PermissionError, which check_permission() raises bare - str(exc)
#: is "". A user-facing message must never be empty, and "something went wrong"
#: is not an acceptable answer when the actual cause is known and actionable.
_DEFAULT_MESSAGE_BY_CODE: dict[str, str] = {
    "PERMISSION_DENIED": "The agent is not authorised to perform this action in this system. "
                         "A System Manager can grant it on the Accountant Agent user.",
    "MANDATORY_MISSING": "A required field is missing from this entry.",
    "LINK_NOT_FOUND": "One of the records this entry refers to could not be found.",
    "LINK_CANCELLED": "This entry refers to a record that has been cancelled.",
    "HAS_DEPENDENTS": "This document cannot be changed while other documents are applied against it.",
    "CONCURRENT_MODIFICATION": "This document was changed by someone else while the agent was working on it.",
    "DUPLICATE": "This entry has already been recorded.",
    "PERIOD_FROZEN": "The accounting period for this date is closed.",
    "VALIDATION_FAILED": "This entry was refused by your system's accounting rules.",
    "WRITE_REJECTED": "This entry could not be recorded.",
}


def _user_message(code: str, raw: str) -> str:
    """A non-empty, accountant-readable message for every failure path."""
    cleaned = _clean_message(raw)
    if cleaned:
        return cleaned
    return _DEFAULT_MESSAGE_BY_CODE.get(code, _DEFAULT_MESSAGE_BY_CODE["WRITE_REJECTED"])


def _clean_message(message: str) -> str:
    """Strip markup and framework noise from an ERP message.

    Never returns a traceback, a table name or a Python type. project_rules.md
    section 5 (Zero Leakage) and section 6 (No Technical Talk) apply to anything
    that can reach a user.
    """
    text = _HTML_TAG_PATTERN.sub(" ", message or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def _payload_digest(payload: dict) -> str:
    """SHA-256 over a canonical rendering, so a replay is provably identical."""
    canonical = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _document_amount(doc: Any) -> Optional[float]:
    """Best-effort headline amount, for the write log and the run caps."""
    for fieldname in ("base_grand_total", "grand_total", "total_debit", "base_paid_amount", "paid_amount"):
        value = doc.get(fieldname)
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


# ─── Writes ──────────────────────────────────────────────────────────────────


def create_document(
    payload: dict,
    idempotency_key: str,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    approved_by: Optional[str] = None,
    savepoint_ordinal: int = 0,
) -> dict:
    """Create one document, idempotently, as the agent user.

    The protocol, and the reason for each step:

      1. RESERVE the idempotency key first. The UNIQUE index is the concurrency
         gate: two simultaneous replays cannot both reach step 2.
      2. WRITE the document through the Document API - permission-checked,
         link-validated, workflow-gated, server-scripts run.
      3. COMMIT the key -> docname mapping in the SAME transaction. Either both
         exist or neither does; there is no window in which a document exists
         with no record of which request created it.

    On a duplicate key the write is a REPLAY: roll back, read the prior result,
    and report it as already done rather than doing it again.
    """
    if not idempotency_key:
        raise MissingParameterError(_("Missing idempotency_key."))
    doctype = (payload or {}).get("doctype")
    if not doctype:
        raise MissingParameterError(_("Missing doctype in payload."))

    policy = load_write_policy()
    assert_write_policy_enabled(policy)
    assert_not_dry_run(policy)
    assert_doctype_allowed(policy, doctype, "create")
    assert_within_policy_caps(policy, payload, doctype)

    prior = find_write_log_by_key(idempotency_key)
    if prior and prior.get("status") == "COMMITTED":
        return {
            "outcome": "REPLAYED",
            "doctype": prior.get("target_doctype"),
            "docname": prior.get("target_docname"),
            "docstatus": prior.get("docstatus_written"),
            "idempotency_key": idempotency_key,
        }

    digest = _payload_digest(payload)
    savepoint = next_savepoint_name(savepoint_ordinal)

    try:
        frappe.db.savepoint(savepoint)

        log = reserve_write_log(
            idempotency_key=idempotency_key,
            action="create",
            target_doctype=doctype,
            request_digest=digest,
            run_id=run_id,
            session_id=session_id,
            approved_by=approved_by,
        )

        doc = insert_document(payload)

        commit_write_log(
            log=log,
            target_docname=doc.name,
            docstatus_written=int(doc.docstatus or 0),
            amount_written=_document_amount(doc),
            response_snapshot={"name": doc.name, "docstatus": int(doc.docstatus or 0)},
        )

        return {
            "outcome": "CREATED",
            "doctype": doctype,
            "docname": doc.name,
            "docstatus": int(doc.docstatus or 0),
            "idempotency_key": idempotency_key,
            "amount": _document_amount(doc),
        }

    except _DUPLICATE_KEY_ERRORS as exc:
        # UniqueValidationError is what actually fires for a unique FIELD index
        # (base_document.py, via db.is_unique_key_violation). DuplicateEntryError
        # is the duplicate NAME case and is caught alongside it for completeness.
        # Catching only the latter would make this whole replay path dead code.
        frappe.db.rollback(save_point=savepoint)
        replayed = find_write_log_by_key(idempotency_key)
        if replayed and replayed.get("target_docname"):
            return {
                "outcome": "REPLAYED",
                "doctype": replayed.get("target_doctype"),
                "docname": replayed.get("target_docname"),
                "docstatus": replayed.get("docstatus_written"),
                "idempotency_key": idempotency_key,
            }
        raise WriteRejectedError(_user_message("DUPLICATE", str(exc)), code="DUPLICATE")

    except Exception as exc:
        frappe.db.rollback(save_point=savepoint)
        code = _classify_exception(exc)
        record_failed_attempt(
            idempotency_key=idempotency_key,
            action="create",
            target_doctype=doctype,
            request_digest=digest,
            run_id=run_id,
            session_id=session_id,
            error_code=code,
            error_message=_user_message(code, str(exc)),
        )
        raise WriteRejectedError(_user_message(code, str(exc)), code=code)


def submit_existing_document(
    doctype: str,
    docname: str,
    idempotency_key: str,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    approved_by: Optional[str] = None,
    savepoint_ordinal: int = 0,
) -> dict:
    """Post a draft to the ledger. A separate action with a separate key.

    Submission is where GL entries become real and the document becomes
    immutable. It is gated independently of creation, in both the customer's
    write policy and the agent user's ERP permissions.
    """
    return _mutate_existing(
        action="submit",
        doctype=doctype,
        docname=docname,
        idempotency_key=idempotency_key,
        run_id=run_id,
        session_id=session_id,
        approved_by=approved_by,
        savepoint_ordinal=savepoint_ordinal,
        operation=lambda: submit_document(doctype, docname),
    )


def cancel_existing_document(
    doctype: str,
    docname: str,
    reason: str,
    idempotency_key: str,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    approved_by: Optional[str] = None,
    savepoint_ordinal: int = 0,
) -> dict:
    """Reverse a submitted document. Never edits it - immutability is the point."""
    if not reason:
        raise MissingParameterError(_("A reason is required to reverse a document."))
    return _mutate_existing(
        action="cancel",
        doctype=doctype,
        docname=docname,
        idempotency_key=idempotency_key,
        run_id=run_id,
        session_id=session_id,
        approved_by=approved_by,
        savepoint_ordinal=savepoint_ordinal,
        operation=lambda: cancel_document(doctype, docname, reason),
    )


def amend_existing_document(
    doctype: str,
    docname: str,
    payload: dict,
    idempotency_key: str,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    approved_by: Optional[str] = None,
    savepoint_ordinal: int = 0,
) -> dict:
    """Create a corrected successor to a cancelled document."""
    return _mutate_existing(
        action="amend",
        doctype=doctype,
        docname=docname,
        idempotency_key=idempotency_key,
        run_id=run_id,
        session_id=session_id,
        approved_by=approved_by,
        savepoint_ordinal=savepoint_ordinal,
        operation=lambda: amend_document(doctype, docname, payload),
    )


def _mutate_existing(
    action: str,
    doctype: str,
    docname: str,
    idempotency_key: str,
    run_id: Optional[str],
    session_id: Optional[str],
    approved_by: Optional[str],
    savepoint_ordinal: int,
    operation,
) -> dict:
    """Shared idempotent protocol for submit / cancel / amend."""
    if not doctype or not docname:
        raise MissingParameterError(_("Missing doctype or document name."))
    if not idempotency_key:
        raise MissingParameterError(_("Missing idempotency_key."))

    policy = load_write_policy()
    assert_write_policy_enabled(policy)
    assert_not_dry_run(policy)
    assert_doctype_allowed(policy, doctype, action)

    if not frappe.db.exists(doctype, docname):
        raise ResourceNotFoundError(_("{0} {1} was not found.").format(doctype, docname))

    prior = find_write_log_by_key(idempotency_key)
    if prior and prior.get("status") == "COMMITTED":
        return {
            "outcome": "REPLAYED",
            "doctype": prior.get("target_doctype"),
            "docname": prior.get("target_docname"),
            "docstatus": prior.get("docstatus_written"),
            "idempotency_key": idempotency_key,
        }

    digest = _payload_digest({"doctype": doctype, "docname": docname, "action": action})
    savepoint = next_savepoint_name(savepoint_ordinal)

    try:
        frappe.db.savepoint(savepoint)
        log = reserve_write_log(
            idempotency_key=idempotency_key,
            action=action,
            target_doctype=doctype,
            request_digest=digest,
            run_id=run_id,
            session_id=session_id,
            approved_by=approved_by,
        )
        doc = operation()
        commit_write_log(
            log=log,
            target_docname=doc.name,
            docstatus_written=int(doc.docstatus or 0),
            amount_written=_document_amount(doc),
            response_snapshot={"name": doc.name, "docstatus": int(doc.docstatus or 0)},
        )
        return {
            "outcome": "CREATED" if action == "amend" else "UPDATED",
            "doctype": doctype,
            "docname": doc.name,
            "docstatus": int(doc.docstatus or 0),
            "idempotency_key": idempotency_key,
        }

    except _DUPLICATE_KEY_ERRORS as exc:
        frappe.db.rollback(save_point=savepoint)
        replayed = find_write_log_by_key(idempotency_key)
        if replayed and replayed.get("target_docname"):
            return {
                "outcome": "REPLAYED",
                "doctype": replayed.get("target_doctype"),
                "docname": replayed.get("target_docname"),
                "docstatus": replayed.get("docstatus_written"),
                "idempotency_key": idempotency_key,
            }
        raise WriteRejectedError(_user_message("DUPLICATE", str(exc)), code="DUPLICATE")

    except Exception as exc:
        frappe.db.rollback(save_point=savepoint)
        code = _classify_exception(exc)
        record_failed_attempt(
            idempotency_key=idempotency_key,
            action=action,
            target_doctype=doctype,
            request_digest=digest,
            run_id=run_id,
            session_id=session_id,
            error_code=code,
            error_message=_user_message(code, str(exc)),
        )
        raise WriteRejectedError(_user_message(code, str(exc)), code=code)


def create_documents_batch(
    documents: Sequence[dict],
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    approved_by: Optional[str] = None,
) -> dict:
    """Create many documents in one transaction, one savepoint each.

    Atomicity policy, and the two halves are deliberately different questions:
      * WITHIN a document - all or nothing, always. A half-written journal entry
        is a corrupt ledger.
      * ACROSS documents - continue on error. A batch of 400 invoices that
        aborts on row 397 and discards 396 good ones is an unusable product.
    """
    if not documents:
        raise MissingParameterError(_("No documents supplied."))
    if len(documents) > MAX_BATCH_SIZE:
        raise MissingParameterError(
            _("At most {0} documents may be written in one request.").format(MAX_BATCH_SIZE)
        )

    policy = load_write_policy()
    assert_write_policy_enabled(policy)
    assert_not_dry_run(policy)

    results: list[dict] = []
    created = replayed = rejected = 0

    for ordinal, entry in enumerate(documents, start=1):
        payload = entry.get("payload") or {}
        key = entry.get("idempotency_key") or ""
        try:
            outcome = create_document(
                payload=payload,
                idempotency_key=key,
                run_id=run_id,
                session_id=session_id,
                approved_by=approved_by,
                savepoint_ordinal=ordinal,
            )
            outcome["ordinal"] = ordinal
            outcome["source_row_ordinal"] = entry.get("source_row_ordinal")
            results.append(outcome)
            if outcome["outcome"] == "CREATED":
                created += 1
            else:
                replayed += 1
        except AgentWriteError as exc:
            rejected += 1
            results.append(
                {
                    "outcome": "REJECTED",
                    "ordinal": ordinal,
                    "source_row_ordinal": entry.get("source_row_ordinal"),
                    "doctype": payload.get("doctype"),
                    "idempotency_key": key,
                    "error_code": exc.code,
                    "error_message": exc.message,
                }
            )

    return {
        "created": created,
        "replayed": replayed,
        "rejected": rejected,
        "results": results,
    }


def list_agent_documents(limit: int = 20, target_doctype: Optional[str] = None) -> dict:
    """Everything this agent has written, newest first.

    Backs "submit that entry" / "reverse the one you just made": the agent
    resolves a vague reference against its OWN write log, never against an
    arbitrary document name a language model produced.
    """
    rows = list_written_documents(limit=limit, target_doctype=target_doctype)

    documents: list[dict] = []
    for row in rows:
        if not row.get("target_docname"):
            continue
        state = get_document_state(row["target_doctype"], row["target_docname"])
        if state is None:
            # Written earlier but since deleted by a human. Not an error - just
            # no longer actionable, and the agent should not offer it.
            continue
        documents.append({**state, "written_at": str(row.get("creation") or ""),
                          "approved_by": row.get("approved_by")})
    return {"documents": documents}


def get_write_log(idempotency_key: str) -> dict:
    """Resolve the outcome of a request whose response was never received.

    This is the ONLY recovery path for an unknown outcome. A caller that timed
    out asks what happened to its key; it never retries the write and hopes.
    """
    if not idempotency_key:
        raise MissingParameterError(_("Missing idempotency_key."))
    record = find_write_log_by_key(idempotency_key)
    if not record:
        return {"found": False, "idempotency_key": idempotency_key}
    return {"found": True, **record}


def alert_on_stranded_in_flight() -> None:
    """Scheduled check for an invariant violation. Never deletes anything.

    A row can only be IN_FLIGHT inside an open transaction, because the
    reservation and its COMMITTED update share one. A surviving IN_FLIGHT row
    therefore means the invariant broke - which is worth an error log, not a
    quiet cleanup that destroys the only evidence.
    """
    from accountant_agent.agent_api.db.agent_write_repository import find_stranded_in_flight

    stranded = find_stranded_in_flight(older_than_minutes=60)
    if not stranded:
        return
    frappe.log_error(
        title="Agent Write Log: stranded IN_FLIGHT rows",
        message=(
            "These rows should be impossible: the reservation and its commit "
            "share one transaction.\n\n" + json.dumps(stranded, indent=2, default=str)
        ),
    )
