# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""Self-service onboarding: connect this ERP to the Accountant Agent platform.

THE PROBLEM THIS SOLVES
    The Creator Agent runs on the platform and writes into the customer's ERP.
    To do that the platform needs three things: where the ERP is, and an API key
    and secret belonging to the agent's own ERP user.

    Asking a customer to generate those in one screen and paste them into
    another is the single worst step in an onboarding flow. They paste a human's
    key by mistake, they paste the key into the secret box, they lose the secret
    because Frappe shows it exactly once, and every one of those mistakes
    surfaces much later as a permission error that reads like a bug.

    So the customer never sees a credential. They press Connect. This module
    provisions the agent's ERP user, mints its credentials, and posts them to the
    platform over the connection the customer is already authenticated on - the
    access token their Agent Settings record already holds from signing in.

WHY THE AGENT'S CREDENTIALS ARE REUSED RATHER THAN ROTATED ON EVERY CONNECT
    One ERP can be connected to more than one platform account: a firm's owner
    and their bookkeeper each have their own login. Both connections carry the
    SAME agent credentials, because there is one agent user in the ERP.

    Minting a fresh pair on every Connect would therefore silently break every
    connection made before this one - the previous holders keep a secret the ERP
    no longer accepts, and their next write fails with an authentication error
    they have no way to interpret. Connect is consequently idempotent, and
    rotation is a separate, explicitly labelled action.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    It does not grant the agent permission on a single accounting document, and
    it does not switch recording on. Those stay where they belong: with the
    customer, in their own ERP. ``apply_recommended_setup`` exists to make the
    safe starting configuration one click rather than twenty, but the customer
    has to ask for it, and what it configures is a dry-run.
"""

from __future__ import annotations

from typing import Any, Optional

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime

from accountant_agent.accountant_agent.doctype.agent_settings.agent_settings import (
    get_agent_server_url,
)
from accountant_agent.install import AGENT_ROLE, AGENT_USER, ensure_agent_user

#: The platform is a network hop away and the customer is watching a spinner.
#: Long enough for a verification handshake into their ERP and back, short
#: enough that a dead platform does not appear to hang the desk.
_PLATFORM_TIMEOUT_SECONDS: int = 45

#: Read permissions the agent needs before it can resolve "the bank account" to
#: a real account. Without these its candidate lookups return nothing and it
#: tells the customer their accounts do not exist.
_RECOMMENDED_READ_ONLY: tuple[str, ...] = (
    "Company", "Account", "Cost Center", "Customer", "Supplier",
    "Currency", "Fiscal Year", "Mode of Payment",
)

#: The one document type the recommended setup allows, and only as a draft.
#: A journal entry is the most general accounting instrument and the easiest for
#: an accountant to review before posting.
_RECOMMENDED_WRITABLE: str = "Journal Entry"


# ── Local state helpers ──────────────────────────────────────────────────────


def _settings_doc(agent_email: str) -> Any:
    """The caller's OWN Agent Settings record, or a refusal.

    ``agent_email`` reaches these endpoints from the browser's localStorage, so
    it is an identifier and never a credential. Ownership is proved before
    anything is done with it - otherwise any signed-in ERP user could name a
    colleague's platform account and connect this ERP's write credentials to it.
    """
    from accountant_agent.accountant_agent.page.agent_chat.agent_chat import (
        get_agent_settings_doc,
    )

    doc = get_agent_settings_doc(agent_email)
    if not doc:
        frappe.throw(
            _("Please sign in to the Accountant Agent from the chat page first."),
            frappe.DoesNotExistError,
        )
    return doc


def _require_admin() -> None:
    """Connecting an ERP to an external service is an administrator's decision."""
    frappe.only_for("System Manager")


def _public_site_url() -> str:
    """The address the platform should call this site on.

    ``frappe.utils.get_url()`` is right for almost every deployment. The
    override exists for the case it cannot know about: a site behind a proxy or
    tunnel whose public address differs from the one it sees itself on.
    """
    override = frappe.conf.get("accountant_agent_public_url")
    return (override or frappe.utils.get_url()).rstrip("/")


def _ensure_agent_credentials(force_new: bool = False) -> dict[str, str]:
    """The agent user's API key and secret, minting them only if needed.

    Returns both in plaintext because they are on their way to the platform in
    the same call. They are never returned to the browser, never logged, and
    never written to any document.
    """
    ensure_agent_user()
    user = frappe.get_doc("User", AGENT_USER)

    if not force_new and user.api_key:
        # The secret is stored encrypted; if it cannot be read back (an
        # encryption key rotated between then and now) a fresh pair is the only
        # way forward, and reusing an unreadable one would register credentials
        # that can never authenticate.
        secret = user.get_password("api_secret", raise_exception=False)
        if secret:
            return {"api_key": user.api_key, "api_secret": secret}

    api_key = frappe.generate_hash(length=15)
    api_secret = frappe.generate_hash(length=15)
    user.api_key = api_key
    user.api_secret = api_secret
    user.flags.ignore_permissions = True
    user.save(ignore_permissions=True)
    frappe.db.commit()
    return {"api_key": api_key, "api_secret": api_secret}


def _record_connection(
    doc: Any,
    erp_connection_id: Optional[str] = None,
    recording_enabled: Optional[bool] = None,
    last_error: Optional[str] = None,
) -> None:
    """Persist what the platform told us, so status never needs a network call.

    ``db.set_value`` rather than ``doc.save`` on purpose: saving an Agent
    Settings record round-trips its Password fields, and an unchanged
    ``api_key`` comes back as Frappe's masked placeholder. The controller's
    ``is_dummy_password`` check handles that correctly, but writing the columns
    directly avoids the whole class of problem for fields that are pure
    bookkeeping.
    """
    values: dict[str, Any] = {}
    if erp_connection_id is not None:
        values["erp_connection_id"] = erp_connection_id
        values["agent_erp_user"] = AGENT_USER
        values["write_connected_on"] = now_datetime()
    if recording_enabled is not None:
        values["write_recording_enabled"] = 1 if recording_enabled else 0
    values["write_last_error"] = (last_error or "")[:500]

    frappe.db.set_value("Agent Settings", doc.name, values, update_modified=False)
    frappe.db.commit()


# ── Platform transport ───────────────────────────────────────────────────────


def _platform_request(
    doc: Any, method: str, path: str, data: Optional[dict] = None
) -> dict:
    """Call the platform as this customer, refreshing the token once on 401.

    The platform's access tokens are short-lived; the refresh path mirrors
    ``agent_chat.py`` exactly so a customer never has to re-enter a password
    because a setup screen used a different rule from the chat page.

    Payloads are form-encoded. The platform's connection endpoints declare
    ``Form(...)`` parameters, and a JSON body is rejected with a validation
    error that reads like an authentication failure.
    """
    from accountant_agent.accountant_agent.page.agent_chat.agent_chat import (
        refresh_agent_token_on_server, save_agent_settings,
    )

    access_token = doc.get_password("access_token", raise_exception=False)
    if not access_token:
        frappe.throw(
            _("Please sign in to the Accountant Agent from the chat page first."),
            frappe.AuthenticationError,
        )

    url = f"{get_agent_server_url()}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}

    def _send(auth_headers: dict) -> requests.Response:
        return requests.request(
            method, url, data=data, headers=auth_headers,
            timeout=_PLATFORM_TIMEOUT_SECONDS,
        )

    try:
        response = _send(headers)
        if response.status_code == 401:
            refreshed = refresh_agent_token_on_server(access_token)
            if not refreshed:
                save_agent_settings(doc.email, access_token="")
                frappe.throw(
                    _("Your Accountant Agent session has expired. Please sign in "
                      "again from the chat page."),
                    frappe.AuthenticationError,
                )
            save_agent_settings(doc.email, access_token=refreshed)
            headers["Authorization"] = f"Bearer {refreshed}"
            response = _send(headers)
    except requests.exceptions.RequestException as exc:
        frappe.log_error(
            title="Accountant Agent: platform unreachable",
            message=f"{method} {path} failed: {exc}",
        )
        frappe.throw(
            _("Could not reach the Accountant Agent service. Please try again "
              "in a moment."),
            frappe.ValidationError,
        )

    if response.status_code >= 400:
        frappe.throw(_(_platform_error(response)), frappe.ValidationError)

    try:
        return response.json() or {}
    except ValueError:
        return {}


def _platform_error(response: requests.Response) -> str:
    """The platform's own words, never a status code or a body dump."""
    try:
        body = response.json()
    except ValueError:
        return "The Accountant Agent service could not complete that request."

    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list) and detail:
        first = detail[0]
        detail = first.get("msg") if isinstance(first, dict) else str(first)
    if isinstance(detail, str) and detail.strip():
        return detail
    return "The Accountant Agent service could not complete that request."


# ── Whitelisted endpoints ────────────────────────────────────────────────────


@frappe.whitelist()
def get_write_connection_status(agent_email: Optional[str] = None) -> dict:
    """Everything the setup screen needs, read from THIS site only.

    Deliberately makes no network call. If the platform were asked and happened
    to be unreachable, or the customer's token had lapsed, the screen would
    report "not connected" for a connection that is working perfectly - and the
    customer would press Connect again, which is the one action most likely to
    make a healthy setup worse.

    Everything below is local truth: what exists in this ERP, and what the
    platform last told us. Both are facts. Neither is a guess.
    """
    _require_admin()

    status: dict[str, Any] = {
        "agent_user": AGENT_USER,
        "agent_user_exists": bool(frappe.db.exists("User", AGENT_USER)),
        "agent_user_enabled": False,
        "has_credentials": False,
        "connected_to_platform": False,
        "recording_enabled": False,
        "connected_on": None,
        "last_error": None,
        "policy_enabled": False,
        "dry_run_only": False,
        "allowed_doctypes": [],
        "readable_doctypes": [],
        "missing": [],
    }

    if status["agent_user_exists"]:
        user = frappe.get_doc("User", AGENT_USER)
        status["agent_user_enabled"] = bool(user.enabled)
        status["has_credentials"] = bool(user.api_key)
        status["user_type"] = user.user_type

    if agent_email:
        doc = _settings_doc(agent_email)
        status["connected_to_platform"] = bool(doc.get("erp_connection_id"))
        status["recording_enabled"] = bool(doc.get("write_recording_enabled"))
        status["connected_on"] = doc.get("write_connected_on")
        status["last_error"] = doc.get("write_last_error") or None

    if frappe.db.exists("DocType", "Agent Write Policy"):
        policy = frappe.get_single("Agent Write Policy")
        status["policy_enabled"] = bool(policy.enabled)
        status["dry_run_only"] = bool(policy.dry_run_only)
        status["allowed_doctypes"] = [
            row.document_type for row in (policy.allowed_document_types or [])
        ]

    status["readable_doctypes"] = _granted_doctypes()
    status["missing"] = _missing_steps(status)
    return status


def _granted_doctypes() -> list[dict]:
    """Which DocTypes the customer has granted the agent role, and how.

    Read from the permission tables rather than by impersonating the agent: this
    endpoint runs on a settings screen, and switching the session user to answer
    a status question is a needless risk.
    """
    rows = frappe.get_all(
        "Custom DocPerm",
        filters={"role": AGENT_ROLE, "permlevel": 0},
        fields=["parent", "read", "create", "write", "submit", "cancel", "amend"],
    )
    owned = {"Agent Write Log", "Agent Write Policy", "Agent Chats", "Agent Chat History"}
    return [
        {
            "doctype": row.parent,
            "read": bool(row.read), "create": bool(row.create),
            "write": bool(row.write), "submit": bool(row.submit),
            "cancel": bool(row.cancel), "amend": bool(row.amend),
        }
        for row in rows
        if row.parent not in owned
    ]


def _missing_steps(status: dict) -> list[str]:
    """The remaining setup, phrased as instructions rather than as diagnostics."""
    missing: list[str] = []

    if not status["agent_user_exists"]:
        missing.append(
            "The agent's ERP user has not been created. Run a bench migrate on "
            "this site, or press Connect — it provisions the user for you."
        )
    elif not status["agent_user_enabled"]:
        missing.append(f"The user {AGENT_USER} is disabled. Enable it to let the agent work.")

    if not status["connected_to_platform"]:
        missing.append("Press Connect to link this ERP to your Accountant Agent account.")
    elif not status["recording_enabled"]:
        missing.append(
            "Recording is switched off. Turn on 'Allow the agent to record "
            "entries' when you are ready for it to write."
        )

    if not status["policy_enabled"]:
        missing.append(
            "The Agent Write Policy is disabled, so the agent will refuse every "
            "write. Open it and tick Enabled, then list the document types it "
            "may prepare."
        )
    elif not status["allowed_doctypes"]:
        missing.append(
            "The Agent Write Policy allows no document types yet. Add at least "
            "one — Journal Entry is the usual starting point."
        )

    writable = [row for row in status["readable_doctypes"] if row.get("create")]
    if not writable:
        missing.append(
            "The agent has not been granted permission on any accounting "
            "document. Grant the 'Accountant Agent' role Create and Write on the "
            "document types you want it to prepare."
        )

    return missing


@frappe.whitelist()
def connect_write_access(
    agent_email: str, enable_recording: int | str = 0
) -> dict:
    """Link this ERP to the caller's Accountant Agent account. Idempotent.

    Provisions the agent's ERP user if absent, reuses its credentials if it
    already has them, and registers the site with the platform, which verifies
    the pair by calling straight back into this site's write gateway.
    """
    _require_admin()
    doc = _settings_doc(agent_email)

    credentials = _ensure_agent_credentials()
    site_url = _public_site_url()

    result = _platform_request(
        doc, "POST", "/api/create/connections",
        data={
            "site_url": site_url,
            "api_key": credentials["api_key"],
            "api_secret": credentials["api_secret"],
            "agent_erp_user": AGENT_USER,
            "label": frappe.local.site,
        },
    )

    connection_id = result.get("erp_connection_id")
    verified = bool(result.get("verified"))
    # Mirror what the platform ACTUALLY reports, never a hardcoded False.
    # Connect is idempotent, so a customer who reconnects an already-recording
    # ERP keeps recording - and a local mirror that assumed otherwise would show
    # "recording off" on a connection that is very much on. A status screen that
    # contradicts reality is worse than no status screen.
    _record_connection(
        doc,
        erp_connection_id=connection_id,
        recording_enabled=bool(result.get("write_enabled")),
        last_error=None if verified else result.get("verification_error"),
    )

    if verified and _as_bool(enable_recording) and not result.get("write_enabled"):
        set_recording_enabled(agent_email, 1)
        result["write_enabled"] = True

    return {
        "connected": bool(connection_id),
        "verified": verified,
        "site_url": site_url,
        "agent_user": AGENT_USER,
        "verification_error": result.get("verification_error"),
        "recording_enabled": bool(result.get("write_enabled")),
        "message": (
            _("Connected. Your ERP and the Accountant Agent can now reach each other.")
            if verified
            else _("Saved, but the agent could not reach this site from the internet yet.")
        ),
    }


@frappe.whitelist()
def set_recording_enabled(agent_email: str, enabled: int | str) -> dict:
    """The platform-side switch that lets the agent write at all.

    One of two independent switches. The Agent Write Policy in this ERP is the
    other, and it is the one that holds even if the platform is compromised -
    which is why the agent refuses to write when either says no.
    """
    _require_admin()
    doc = _settings_doc(agent_email)

    connection_id = doc.get("erp_connection_id")
    if not connection_id:
        frappe.throw(
            _("Connect this ERP to your Accountant Agent account first."),
            frappe.ValidationError,
        )

    wanted = _as_bool(enabled)
    result = _platform_request(
        doc, "POST", f"/api/create/connections/{connection_id}/write-enabled",
        data={"enabled": "true" if wanted else "false"},
    )

    recording = bool(result.get("write_enabled"))
    _record_connection(doc, recording_enabled=recording)
    return {
        "recording_enabled": recording,
        "message": (
            _("The agent may now record entries, subject to your Agent Write Policy.")
            if recording
            else _("Recording is switched off. The agent can still read and prepare.")
        ),
    }


@frappe.whitelist()
def rotate_and_reconnect(agent_email: str) -> dict:
    """Issue fresh agent credentials and re-register them.

    THE WARNING THAT BELONGS ON THE BUTTON
        There is one agent user in this ERP, so there is one credential pair.
        Any OTHER platform account connected to this same site keeps the old
        secret and will start failing. This is the correct behaviour for a
        rotation - that is what rotating a credential means - but it must be a
        deliberate act, which is why it is not what Connect does.
    """
    _require_admin()
    doc = _settings_doc(agent_email)

    credentials = _ensure_agent_credentials(force_new=True)
    site_url = _public_site_url()

    result = _platform_request(
        doc, "POST", "/api/create/connections",
        data={
            "site_url": site_url,
            "api_key": credentials["api_key"],
            "api_secret": credentials["api_secret"],
            "agent_erp_user": AGENT_USER,
            "label": frappe.local.site,
        },
    )

    verified = bool(result.get("verified"))
    _record_connection(
        doc,
        erp_connection_id=result.get("erp_connection_id"),
        recording_enabled=bool(result.get("write_enabled")),
        last_error=None if verified else result.get("verification_error"),
    )
    return {
        "verified": verified,
        "verification_error": result.get("verification_error"),
        "message": _(
            "New credentials issued. Any other Accountant Agent account connected "
            "to this site must press Connect again."
        ),
    }


@frappe.whitelist()
def disconnect_write_access(agent_email: str) -> dict:
    """Remove this ERP from the platform, credentials included.

    The complete undo. The local agent user is left in place and disabled rather
    than deleted: it owns every Agent Write Log row, and deleting it would
    destroy the record of what the agent did. Disabling stops it acting; the
    audit trail survives, which is the correct trade for accounting software.
    """
    _require_admin()
    doc = _settings_doc(agent_email)

    connection_id = doc.get("erp_connection_id")
    if connection_id:
        _platform_request(
            doc, "DELETE", f"/api/create/connections/{connection_id}",
        )

    frappe.db.set_value(
        "Agent Settings", doc.name,
        {
            "erp_connection_id": "", "write_recording_enabled": 0,
            "write_connected_on": None, "write_last_error": "",
        },
        update_modified=False,
    )
    frappe.db.commit()
    return {
        "connected": False,
        "message": _(
            "Disconnected. The agent can no longer record anything in this ERP. "
            "Its history in Agent Write Log is kept."
        ),
    }


@frappe.whitelist()
def apply_recommended_setup() -> dict:
    """Configure the safe starting position, in one action.

    WHAT THIS GRANTS, AND WHY IT IS SAFE TO OFFER
        Read access on the master data the agent needs in order to recognise an
        account by name, and Create plus Write on Journal Entry - draft only.
        It grants NO submit, NO cancel and NO amend, and it enables the write
        policy in DRY RUN mode, so the agent validates entries against the real
        ledger and records nothing at all until the customer turns dry run off.

        Offered as an explicit button rather than done at install time, because
        an app that grants itself ledger permissions on installation is exactly
        what an accountant should refuse to install.
    """
    _require_admin()
    from frappe.permissions import add_permission, update_permission_property

    granted: list[str] = []
    for doctype in _RECOMMENDED_READ_ONLY:
        if not frappe.db.exists("DocType", doctype):
            continue
        _grant(add_permission, update_permission_property, doctype, {"read": 1})
        granted.append(doctype)

    if frappe.db.exists("DocType", _RECOMMENDED_WRITABLE):
        _grant(
            add_permission, update_permission_property, _RECOMMENDED_WRITABLE,
            {"read": 1, "create": 1, "write": 1, "submit": 0, "cancel": 0, "amend": 0},
        )
        granted.append(_RECOMMENDED_WRITABLE)

    policy = frappe.get_single("Agent Write Policy")
    # Dry run is forced ONLY when turning the policy on for the first time.
    #
    # A customer with a live, recording setup may press this button simply to
    # add one more read permission. Forcing dry run unconditionally would
    # silently stop them recording, and the next entry the agent "prepared"
    # would never reach their ledger - with nothing in the conversation to say
    # why. Switching a working system off is not a safe default; it is a
    # surprise.
    was_disabled = not policy.enabled
    policy.enabled = 1
    if was_disabled:
        policy.dry_run_only = 1
    policy.require_approval = 1
    if not any(
        row.document_type == _RECOMMENDED_WRITABLE
        for row in (policy.allowed_document_types or [])
    ):
        policy.append(
            "allowed_document_types",
            {
                "document_type": _RECOMMENDED_WRITABLE,
                "allow_create": 1, "allow_submit": 0,
                "allow_cancel": 0, "allow_amend": 0,
            },
        )
    policy.flags.ignore_permissions = True
    policy.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "granted": granted,
        "dry_run_only": bool(policy.dry_run_only),
        "message": _(
            "The agent can now read your chart of accounts and prepare journal "
            "entries. Dry run is on, so nothing is saved until you turn it off "
            "in the Agent Write Policy."
        ) if policy.dry_run_only else _(
            "The agent can now read your chart of accounts and prepare journal "
            "entries. Your existing recording settings were left unchanged."
        ),
    }


def _grant(add_permission, update_permission_property, doctype: str, perms: dict) -> None:
    """Give the agent role exactly these permissions on one DocType."""
    if not frappe.db.exists(
        "Custom DocPerm", {"parent": doctype, "role": AGENT_ROLE, "permlevel": 0}
    ):
        add_permission(doctype, AGENT_ROLE, 0)
    for ptype, value in perms.items():
        update_permission_property(doctype, AGENT_ROLE, 0, ptype, value)
    # Never delete. The agent reverses documents; it does not erase them.
    update_permission_property(doctype, AGENT_ROLE, 0, "delete", 0)


def _as_bool(value: Any) -> bool:
    """Frappe's whitelisted calls arrive as strings; "0" and "false" are False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
