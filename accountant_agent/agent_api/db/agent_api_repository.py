# -*- coding: utf-8 -*-
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

from typing import Optional

import frappe


def find_settings_name_by_api_key(api_key: str) -> Optional[str]:
	"""
	Searches all Agent Settings records for one whose decrypted api_key
	matches the given value.

	Returns:
		The document name (agent account email) if found, else None.
	"""
	settings_records = frappe.get_all("Agent Settings", fields=["name"])

	for record in settings_records:
		try:
			doc = frappe.get_doc("Agent Settings", record.name)
			if doc.get_password("api_key") == api_key:
				return record.name
		except Exception:
			continue

	return None


def doctype_exists(doctype_name: str) -> bool:
	"""Check whether a DocType exists in the database."""
	return bool(frappe.db.exists("DocType", doctype_name))


def get_doctype_metadata(doctype_name: str):
	"""
	Retrieve the Frappe Meta object for a given DocType.

	Returns:
		frappe.model.meta.Meta instance.
	"""
	return frappe.get_meta(doctype_name)


def execute_select_query(query: str) -> list[dict]:
	"""
	Execute a raw SQL query and return results as a list of dicts.

	This function trusts that the query has already been validated
	as a safe SELECT statement by the service layer.
	"""
	return frappe.db.sql(query, as_dict=True)


def chat_session_exists(session_id: str) -> bool:
	"""Check whether an Agent Chats record exists for the given session_id."""
	return bool(frappe.db.exists("Agent Chats", session_id))


def insert_chat_history_record(
	session_id: str,
	sender: str,
	content: str,
) -> None:
	"""Insert a new message record into Agent Chat History."""
	doc = frappe.get_doc({
		"doctype": "Agent Chat History",
		"creation1": frappe.utils.now_datetime(),
		"session_id": session_id,
		"sender": sender,
		"content": content,
	})
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
