# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""Agent Settings — the per-user link between an ERP user and their agent account.

One record per connected user, named by the agent account e-mail. It holds two
secrets as Frappe Password fields (encrypted at rest in ``__Auth``): the agent
API key the platform authenticates the ERP with, and the access token the ERP
authenticates the platform with.

WHY THERE IS AN ``api_key_hash`` COLUMN NEXT TO AN ENCRYPTED ``api_key``
    Frappe encrypts Password fields with a randomised Fernet nonce, so the same
    key produces different ciphertext every time and the column cannot be
    indexed or searched. Authenticating by key therefore used to mean loading
    *every* Agent Settings record and decrypting each one — O(connected users)
    work with a decryption per record, on the hot path of every agent tool call.

    ``api_key_hash`` is a deterministic SHA-256 fingerprint of the same key. It
    is indexed, so the lookup is a single indexed read; the decrypted value is
    still compared afterwards with ``hmac.compare_digest`` so the hash is a
    lookup accelerator and never the authentication decision on its own.

    A plain SHA-256 is the right primitive here rather than a slow KDF: the
    input is a randomly generated UUID4 (122 bits of entropy), not a
    user-chosen password, so brute force is infeasible and there is nothing for
    a work factor to buy.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Optional

import frappe
import requests
from frappe import _
from frappe.model.document import Document

#: Where the platform's agent API lives. Read from ``site_config.json`` so a
#: deployment that does not run the agent server on the ERP host — which is
#: every real deployment — can point at it without editing source.
_DEFAULT_AGENT_SERVER_URL: str = "http://127.0.0.1:8010"
_USAGE_REQUEST_TIMEOUT_SECONDS: int = 10


def _load_env() -> None:
    try:
        import os
        app_root = os.path.abspath(os.path.join(frappe.get_app_path("accountant_agent"), ".."))
        env_path = os.path.join(app_root, ".env")
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip()
                        if val.startswith(('"', "'")) and val.endswith(val[0]):
                            val = val[1:-1]
                        os.environ.setdefault(key, val)
    except Exception:
        pass


def get_agent_server_url() -> str:
    """Base URL of the platform's agent API for this site."""
    import os
    _load_env()
    return (
        frappe.conf.get("accountant_agent_server_url")
        or os.environ.get("ACCOUNTANT_AGENT_SERVER_URL")
        or _DEFAULT_AGENT_SERVER_URL
    ).rstrip("/")


def hash_api_key(api_key: str) -> str:
    """Deterministic, indexable fingerprint of an agent API key.

    The single definition used by both the writer (``save_agent_settings``) and
    the reader (``find_settings_name_by_api_key``). Two definitions would drift
    and lock every customer out of their own agent.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def decode_jwt_payload(token: str) -> dict:
    """Decode a JWT payload WITHOUT verifying its signature.

    Used only to read the platform's own ``sub`` claim out of a token this site
    already holds, never to make a trust decision — the platform verifies its
    own signatures. Returns an empty dict on any malformed input.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
    except Exception as exc:
        frappe.log_error(
            title="Accountant Agent: JWT decode",
            message=f"Could not decode an access token payload: {exc}",
        )
        return {}


class AgentSettings(Document):
    """Keeps ``api_key_hash`` in lockstep with ``api_key``.

    Maintained by the controller rather than by callers so the two can never
    drift: any code path that sets a new key — the sign-up flow, a support
    fix-up, a backfill patch — gets the matching hash for free.

    ``is_dummy_password`` distinguishes a real key being set from the masked
    ``****`` placeholder Frappe substitutes when an unchanged document is
    loaded and re-saved. Without that check, an ordinary save of an untouched
    record would overwrite a valid hash with the hash of a row of asterisks.
    """

    def validate(self) -> None:
        self._assert_email_not_already_connected()

        current = self.get("api_key")
        if current and not self.is_dummy_password(current):
            self.api_key_hash = hash_api_key(current)

    def _assert_email_not_already_connected(self) -> None:
        """One agent account, one record.

        Enforced in the controller rather than as a DB UNIQUE constraint:
        adding a unique index to a live table fails `bench migrate` outright on
        any site that already holds a duplicate, which turns a hardening change
        into an outage. This covers every write path in the app instead.
        """
        if not self.email:
            return

        duplicate = frappe.db.get_value(
            "Agent Settings", {"email": self.email, "name": ("!=", self.name)}, "name"
        )
        if duplicate:
            frappe.throw(
                _("An Accountant Agent connection already exists for {0}.").format(self.email),
                frappe.DuplicateEntryError,
            )


def _get_own_settings_doc(email: str) -> Optional[Document]:
    """The caller's OWN Agent Settings record, or None.

    Ownership, not merely existence, is the check. ``email`` arrives from the
    browser (``localStorage``), so treating it as an identifier would let any
    authenticated ERP user name somebody else's agent account and act on it.
    """
    if not email:
        return None

    name = frappe.db.get_value("Agent Settings", {"email": email}, ["name", "owner"], as_dict=True)
    if not name:
        return None
    if name.owner != frappe.session.user and frappe.session.user != "Administrator":
        return None

    return frappe.get_doc("Agent Settings", name.name)


@frappe.whitelist()
def get_agent_settings_name(email: str) -> Optional[str]:
    """Document name of the caller's own Agent Settings record for this email."""
    doc = _get_own_settings_doc(email)
    return doc.name if doc else None


@frappe.whitelist()
def get_user_usage(email: str) -> dict:
    """Usage percentages for the caller's own agent account.

    Scoped to the caller's own record: usage is billing information, and an
    endpoint that accepted any e-mail would report one customer's consumption
    to another.
    """
    zero: dict = {"daily_usage_percentage": 0.0, "total_usage_percentage": 0.0}

    doc = _get_own_settings_doc(email)
    if not doc:
        return zero

    access_token = doc.get_password("access_token", raise_exception=False)
    user_id: Optional[str] = None
    if access_token:
        user_id = decode_jwt_payload(access_token).get("sub")

    # Fall back to the API key for accounts created before tokens carried a sub.
    if not user_id:
        user_id = doc.get_password("api_key", raise_exception=False)

    if not user_id:
        return zero

    headers: dict = {}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    try:
        response = requests.get(
            f"{get_agent_server_url()}/users/{user_id}/usage",
            headers=headers,
            timeout=_USAGE_REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            return zero

        data = response.json()
        return {
            "daily_usage_percentage": round(data.get("daily_usage_percentage", 0.0), 1),
            "total_usage_percentage": round(data.get("total_usage_percentage", 0.0), 1),
        }
    except Exception as exc:
        frappe.log_error(
            title="Accountant Agent: usage fetch",
            message=f"Could not read usage from the agent server: {exc}",
        )
        return zero
