# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""
Repository Layer — Agent API
-----------------------------
All database/ORM operations for the agent API endpoints.
Handles data access, query execution, and record persistence.

Prohibitions (per three-layer rules):
  ❌ NO business authorization or validation rules
  ❌ NO direct handling of API request schemas
"""

from __future__ import annotations

import hmac
from typing import Any, Optional

import frappe
from frappe.utils.password import get_decrypted_password

from razyyn.razyyn.doctype.agent_settings.agent_settings import (
	hash_api_key,
)


def find_settings_name_by_api_key(api_key: str) -> str | None:
	"""Resolve an API key to its Agent Settings record name, or None.

	Single indexed read on ``api_key_hash``, then a constant-time comparison
	against the decrypted secret. The hash narrows the search; the decrypted
	value makes the decision, so a hash collision or a tampered fingerprint
	column cannot authenticate anybody.

	This replaces a scan that loaded every Agent Settings document and
	decrypted each one. That cost O(connected users) queries plus a Fernet
	decryption per record on the hot path of every single agent tool call —
	the analysis agent alone issues dozens of them per run.
	"""
	if not api_key:
		return None

	name = frappe.db.get_value("Agent Settings", {"api_key_hash": hash_api_key(api_key)}, "name")
	if not name:
		return None

	stored = get_decrypted_password("Agent Settings", name, "api_key", raise_exception=False)
	if not stored or not hmac.compare_digest(str(stored), str(api_key)):
		return None

	return name


def backfill_api_key_hashes() -> int:
	"""Populate ``api_key_hash`` on records written before the column existed.

	Returns the number of records repaired. Idempotent — it only touches rows
	whose fingerprint is missing, so it is safe to run on every migrate, which
	is exactly what ``install.after_migrate`` does. Without it, an upgraded
	site would index-lookup a column that is NULL for every existing customer
	and lock all of them out of their own agent.
	"""
	pending = frappe.get_all(
		"Agent Settings",
		filters={"api_key_hash": ("in", (None, ""))},
		pluck="name",
	)

	repaired = 0
	for name in pending:
		plaintext = get_decrypted_password("Agent Settings", name, "api_key", raise_exception=False)
		if not plaintext:
			# A record with no recoverable key cannot authenticate anyway; it
			# is left alone so the condition stays visible rather than being
			# papered over with the fingerprint of an empty string.
			continue
		frappe.db.set_value(
			"Agent Settings",
			name,
			"api_key_hash",
			hash_api_key(plaintext),
			update_modified=False,
		)
		repaired += 1

	return repaired


def doctype_exists(doctype_name: str) -> bool:
	"""Check whether a DocType exists in the database."""
	return bool(frappe.db.exists("DocType", doctype_name))


def get_doctype_metadata(doctype_name: str) -> Any:
	"""
	Retrieve the Frappe Meta object for a given DocType.

	Returns:
	    frappe.model.meta.Meta instance.
	"""
	return frappe.get_meta(doctype_name)


def execute_select_query(query: str, max_rows: int, timeout_seconds: int) -> list[dict]:
	"""Execute a validated SELECT under a hard row cap and a hard time limit.

	This function trusts that the query has already been validated as a safe
	SELECT statement by the service layer. It does NOT trust that the query is
	*small* or *fast*, because a model wrote it.

	Both limits are applied by the database itself rather than checked after
	the fact. A cap enforced in Python after ``fetchall`` has already
	materialised the whole result set in the worker's memory, and a query that
	scans a ten-year ledger has already contended with the customer's own
	postings for however long it took. The limits have to bind before the rows
	exist.

	Cleanup is unconditional: these are session-scoped settings on a pooled
	connection, so leaking them would silently truncate or time out whatever
	request reuses that connection next.
	"""
	if frappe.db.db_type == "postgres":
		frappe.db.sql(f"SET LOCAL statement_timeout = {int(timeout_seconds) * 1000}")
		# Postgres has no SQL_SELECT_LIMIT equivalent; the cap is applied by the
		# service layer's LIMIT clause, and the slice below is the backstop.
		return frappe.db.sql(query, as_dict=True)[:max_rows]

	# MariaDB: SQL_SELECT_LIMIT bounds the rows the server will return for any
	# SELECT on this session, whatever the statement itself says, and
	# max_statement_time aborts server-side work that overruns.
	try:
		frappe.db.sql(f"SET SESSION SQL_SELECT_LIMIT = {int(max_rows)}")
		frappe.db.sql(f"SET SESSION max_statement_time = {int(timeout_seconds)}")
		return frappe.db.sql(query, as_dict=True)
	finally:
		try:
			frappe.db.sql("SET SESSION SQL_SELECT_LIMIT = DEFAULT")
			frappe.db.sql("SET SESSION max_statement_time = DEFAULT")
		except Exception as exc:
			frappe.log_error(
				title="Agent API: could not reset query session limits",
				message=(
					"SQL_SELECT_LIMIT / max_statement_time may still be set on " f"this connection: {exc}"
				),
			)


def chat_session_exists(session_id: str) -> bool:
	"""Check whether an Agent Chats record exists for the given session_id."""
	return bool(frappe.db.exists("Agent Chats", session_id))


def get_chat_session_owner(session_id: str) -> str | None:
	"""The ERP user who owns a chat session, or None if it does not exist."""
	return frappe.db.get_value("Agent Chats", session_id, "owner")


def insert_chat_history_record(
	session_id: str,
	sender: str,
	content: str,
) -> None:
	"""Insert a new message record into Agent Chat History."""
	doc = frappe.get_doc(
		{
			"doctype": "Agent Chat History",
			"creation1": frappe.utils.now_datetime(),
			"session_id": session_id,
			"sender": sender,
			"content": content,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()


def update_chat_last_timestamp(session_id: str) -> None:
	"""Update the last_update timestamp of a chat session."""
	if session_id and frappe.db.exists("Agent Chats", session_id):
		frappe.db.set_value(
			"Agent Chats",
			session_id,
			"last_update",
			frappe.utils.now_datetime(),
		)
		frappe.db.commit()
