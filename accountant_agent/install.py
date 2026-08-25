# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""Provisioning for the Accountant Agent's ERP identity.

Runs on ``after_install`` and ``after_migrate``. Every step is get-or-create, so
re-running repairs a partially provisioned site and changes nothing on a healthy
one.

THE DESIGN, IN ONE SENTENCE
    The agent is a real, named ERP user that this installer creates with
    **zero permission to do anything**, and every capability it ever gains is
    one the customer deliberately granted on the User form.

WHY THE ROLE HAS desk_access = 1
    It is tempting to set ``desk_access = 0`` on a robot role. That is a trap.
    ``User.set_system_user`` (frappe/core/doctype/user/user.py) runs on every
    save and executes:

        self.user_type = "System User" if self.has_desk_access() else "Website User"

    ...because "System User" is a *standard* User Type. So a role with
    ``desk_access = 0`` silently demotes the agent to a Website User on the next
    save, and every business permission the customer grants it stops working for
    reasons that are extremely hard to diagnose. Verified identical in Frappe
    v14 and v15.

    ``desk_access = 1`` grants **no** DocType permission whatsoever. The role
    still ships with zero DocPerm rows on every business DocType. Default-deny
    is preserved; the user is merely of the correct *type* for the permission
    engine to evaluate it.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import random_string

from accountant_agent.agent_api.db.agent_api_repository import backfill_api_key_hashes

# ── Constants ────────────────────────────────────────────────────────────────

AGENT_ROLE: str = "Accountant Agent"
AGENT_USER: str = "accountant-agent@agent.local"
AGENT_FIRST_NAME: str = "Accountant"
AGENT_LAST_NAME: str = "Agent"

#: DocTypes this app owns. The agent role is granted permission on these and
#: ONLY these. Business DocTypes (Journal Entry, Sales Invoice, ...) are never
#: touched here - that is the customer's decision, made on the User form.
APP_OWNED_PERMISSIONS: tuple[tuple[str, dict[str, int]], ...] = (
    # The agent must be able to reserve and commit its own idempotency records,
    # otherwise the write protocol cannot fail closed. It may never delete one.
    ("Agent Write Log", {"read": 1, "create": 1, "write": 1}),
    # Read-only: the policy is the customer's instrument, not the agent's.
    ("Agent Write Policy", {"read": 1}),
    ("Agent Chats", {"read": 1, "create": 1, "write": 1}),
    ("Agent Chat History", {"read": 1, "create": 1, "write": 1}),
)


# ── Hook entry points ────────────────────────────────────────────────────────


def after_install() -> None:
    """Provision on first install."""
    provision_agent_access()


def after_migrate() -> None:
    """Repair provisioning after an app upgrade.

    Deliberately the same routine: a site that was installed before this module
    existed, or that had the agent user removed, is brought to the correct state
    by the next ``bench migrate``.
    """
    provision_agent_access()


def provision_agent_access() -> None:
    """Create the agent role, user, policy and workspace entry. Idempotent."""
    ensure_agent_role()
    ensure_app_owned_permissions()
    ensure_agent_user()
    ensure_write_policy()
    ensure_workspace_shortcut()
    ensure_api_key_fingerprints()
    frappe.db.commit()
    frappe.logger("accountant_agent").info(
        "Accountant Agent access provisioned (user=%s, role=%s).", AGENT_USER, AGENT_ROLE
    )


def ensure_api_key_fingerprints() -> None:
    """Repair any Agent Settings record missing its ``api_key_hash``.

    The backfill patch is the primary mechanism; this is the safety net for the
    cases a patch cannot cover — a site whose patch log already recorded the
    entry, a record restored from an older backup, a support fix-up that wrote a
    key straight to ``__Auth``. Authentication depends on this column, so
    "usually populated" is not good enough: a record without it cannot
    authenticate at all, and the failure looks like an invalid API key.
    """
    if not frappe.db.exists("DocType", "Agent Settings"):
        return
    if not frappe.db.has_column("Agent Settings", "api_key_hash"):
        return

    repaired = backfill_api_key_hashes()
    if repaired:
        frappe.logger("accountant_agent").info(
            "Repaired the API key fingerprint on %s Agent Settings record(s).", repaired
        )


# ── Role ─────────────────────────────────────────────────────────────────────


def ensure_agent_role() -> str:
    """Create the agent role if absent. Returns the role name.

    The role is created with NO permissions. ``desk_access = 1`` is required -
    see the module docstring for why omitting it breaks the permission model.
    """
    if frappe.db.exists("Role", AGENT_ROLE):
        # Repair the one property whose absence silently breaks everything.
        if not frappe.db.get_value("Role", AGENT_ROLE, "desk_access"):
            frappe.db.set_value("Role", AGENT_ROLE, "desk_access", 1)
            frappe.logger("accountant_agent").warning(
                "Repaired desk_access on the %s role; without it the agent user "
                "is demoted to a Website User and granted permissions stop working.",
                AGENT_ROLE,
            )
        return AGENT_ROLE

    role = frappe.get_doc(
        {
            "doctype": "Role",
            "role_name": AGENT_ROLE,
            "desk_access": 1,
            "is_custom": 1,
            "disabled": 0,
        }
    )
    role.insert(ignore_permissions=True)
    return role.name


def ensure_app_owned_permissions() -> None:
    """Grant the agent role permission on this app's own DocTypes only.

    This is app-internal bookkeeping, not an accounting capability. The
    distinction is the whole design: the app grants itself the ability to keep
    its own audit trail; every capability that touches the customer's ledger
    comes from the customer.

    Fail-closed consequence, and it is the correct direction: if the customer
    removes this role, the Agent Write Log insert fails, the savepoint rolls
    back, and no document is written. Removing the role disables *writing*
    rather than disabling the *audit trail*.
    """
    from frappe.permissions import add_permission, update_permission_property

    for doctype, permissions in APP_OWNED_PERMISSIONS:
        if not frappe.db.exists("DocType", doctype):
            continue

        existing = frappe.db.exists(
            "Custom DocPerm", {"parent": doctype, "role": AGENT_ROLE, "permlevel": 0}
        )
        if not existing:
            add_permission(doctype, AGENT_ROLE, 0)

        for ptype, value in permissions.items():
            update_permission_property(doctype, AGENT_ROLE, 0, ptype, value)

        # Explicitly deny delete on every app-owned DocType. The audit trail is
        # append-only; see AgentWriteLog.on_trash.
        update_permission_property(doctype, AGENT_ROLE, 0, "delete", 0)


# ── User ─────────────────────────────────────────────────────────────────────


def ensure_agent_user() -> str:
    """Create the agent's ERP user if absent. Returns the user id.

    Created with exactly one role - the permission-less agent role - so the
    out-of-the-box posture is that the agent can do nothing at all. It is never
    granted System Manager, Accounts Manager, or any other role.
    """
    if frappe.db.exists("User", AGENT_USER):
        _repair_agent_user()
        return AGENT_USER

    user = frappe.get_doc(
        {
            "doctype": "User",
            "email": AGENT_USER,
            "first_name": AGENT_FIRST_NAME,
            "last_name": AGENT_LAST_NAME,
            "full_name": f"{AGENT_FIRST_NAME} {AGENT_LAST_NAME}",
            "user_type": "System User",
            "enabled": 1,
            "send_welcome_email": 0,
            # A long random password that is generated, never stored by us, and
            # never shown to anyone. The account authenticates by API key; this
            # exists only so the account is not left in a passwordless state.
            "new_password": random_string(40),
            "roles": [{"role": AGENT_ROLE}],
        }
    )
    user.flags.ignore_permissions = True
    user.flags.no_welcome_mail = True
    user.insert(ignore_permissions=True)
    return user.name


def _repair_agent_user() -> None:
    """Bring an existing agent user back to the intended baseline.

    Deliberately conservative: it restores the agent role and the System User
    type if they were lost, but it does NOT strip roles the customer added. The
    customer granting the agent extra roles is the entire point of the design -
    an installer that silently revoked them on every migrate would be fighting
    its own users.
    """
    user = frappe.get_doc("User", AGENT_USER)
    changed = False

    if user.user_type != "System User":
        user.user_type = "System User"
        changed = True

    if AGENT_ROLE not in {row.role for row in user.roles}:
        user.append("roles", {"role": AGENT_ROLE})
        changed = True

    if changed:
        user.flags.ignore_permissions = True
        user.save(ignore_permissions=True)


# ── Policy ───────────────────────────────────────────────────────────────────


def ensure_write_policy() -> None:
    """Create the Agent Write Policy singleton, disabled.

    The agent is provisioned but completely inert until the customer opens this
    document and turns it on. Nothing about installing this app grants the
    agent the ability to write.
    """
    if not frappe.db.exists("DocType", "Agent Write Policy"):
        return

    policy = frappe.get_single("Agent Write Policy")
    if policy.get("__islocal") or not frappe.db.exists("Singles", {"doctype": "Agent Write Policy"}):
        policy.enabled = 0
        policy.require_approval = 1
        policy.dry_run_only = 0
        # Every blast-radius limit ships OPEN, because 0 means unlimited and an
        # agent that silently refuses the 101st document of a real import is a
        # support ticket, not a safety feature. What actually keeps a fresh
        # install safe is `enabled = 0` above: the agent cannot write at all
        # until a human turns it on. The frozen-period check refuses entries in
        # a closed accounting period whatever the back-dating number says.
        policy.max_documents_per_run = 0
        policy.max_total_amount_per_run = 0
        policy.posting_date_max_days_back = 0
        policy.posting_date_max_days_forward = 0
        policy.flags.ignore_permissions = True
        policy.save(ignore_permissions=True)


def ensure_workspace_shortcut() -> None:
    """Surface the write log where an accountant will actually find it.

    A customer should be able to see everything the agent did in their system
    from day one, without being told where to look.
    """
    if not frappe.db.exists("DocType", "Workspace"):
        return
    if not frappe.db.exists("Workspace", "Accounting"):
        return

    workspace = frappe.get_doc("Workspace", "Accounting")
    existing = {row.link_to for row in workspace.get("shortcuts", [])}
    if "Agent Write Log" in existing:
        return

    workspace.append(
        "shortcuts",
        {"type": "DocType", "link_to": "Agent Write Log", "label": _("Agent Write Log")},
    )
    workspace.flags.ignore_permissions = True
    try:
        workspace.save(ignore_permissions=True)
    except Exception as exc:
        # A workspace layout conflict must never fail an install. The shortcut
        # is a convenience; the audit trail exists regardless.
        frappe.logger("accountant_agent").warning(
            "Could not add the Agent Write Log workspace shortcut: %s", exc
        )


# ── Credentials ──────────────────────────────────────────────────────────────


@frappe.whitelist()
def rotate_agent_credentials() -> dict[str, str]:
    """Generate a fresh API key/secret pair for the agent user.

    Returns the plaintext secret EXACTLY ONCE. It is never logged, never
    emailed, and cannot be retrieved again - a second call issues a new pair and
    invalidates the old one.

    Implemented here rather than by calling frappe.core.doctype.user.user
    .generate_keys because that function's return value differs between versions:
    v15 returns both api_key and api_secret, v14 returns only api_secret. Doing
    it directly keeps both benches byte-identical.
    """
    frappe.only_for("System Manager")

    ensure_agent_user()
    user = frappe.get_doc("User", AGENT_USER)

    api_key = frappe.generate_hash(length=15)
    api_secret = frappe.generate_hash(length=15)
    user.api_key = api_key
    user.api_secret = api_secret
    user.flags.ignore_permissions = True
    user.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "agent_user": AGENT_USER,
        "api_key": api_key,
        "api_secret": api_secret,
        "site_url": frappe.utils.get_url(),
    }


@frappe.whitelist()
def get_agent_status() -> dict[str, object]:
    """Report the agent's current provisioning and permission state.

    Backs the onboarding screen. Deliberately shows the customer exactly what
    the agent can and cannot do, in their own terms, rather than asking them to
    trust a claim in documentation.
    """
    frappe.only_for("System Manager")

    if not frappe.db.exists("User", AGENT_USER):
        return {"provisioned": False}

    user = frappe.get_doc("User", AGENT_USER)
    policy_enabled = bool(
        frappe.db.get_single_value("Agent Write Policy", "enabled")
    ) if frappe.db.exists("DocType", "Agent Write Policy") else False

    return {
        "provisioned": True,
        "agent_user": AGENT_USER,
        "enabled": bool(user.enabled),
        "user_type": user.user_type,
        "has_api_key": bool(user.api_key),
        "roles": sorted(row.role for row in user.roles),
        "write_policy_enabled": policy_enabled,
        "writable_doctypes": _writable_doctypes(),
    }


def _writable_doctypes() -> list[dict[str, object]]:
    """Which business DocTypes the agent can currently act on, and how.

    The intersection of what the customer granted the agent user in ERP
    permissions and what the write policy allows. Either one saying no means no.
    """
    if not frappe.db.exists("DocType", "Agent Write Policy"):
        return []

    policy = frappe.get_single("Agent Write Policy")
    if not policy.enabled:
        return []

    original_user = frappe.session.user
    results: list[dict[str, object]] = []
    try:
        frappe.set_user(AGENT_USER)
        for row in policy.allowed_document_types or []:
            results.append(
                {
                    "doctype": row.document_type,
                    "create": bool(
                        row.allow_create
                        and frappe.has_permission(row.document_type, "create")
                    ),
                    "submit": bool(
                        row.allow_submit
                        and frappe.has_permission(row.document_type, "submit")
                    ),
                    "cancel": bool(
                        row.allow_cancel
                        and frappe.has_permission(row.document_type, "cancel")
                    ),
                    "amend": bool(
                        row.allow_amend
                        and frappe.has_permission(row.document_type, "amend")
                    ),
                }
            )
    finally:
        frappe.set_user(original_user)

    return results
