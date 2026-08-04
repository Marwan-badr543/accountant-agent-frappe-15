# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""
Service Layer — Agent API
--------------------------
Pure business logic for the agent API endpoints.
Fully protocol-agnostic — zero awareness of HTTP/REST/gRPC.

Prohibitions (per three-layer rules):
  ❌ NO HTTP framework imports (HttpRequest, Response, status)
  ❌ NO direct HTTP exceptions (e.g., HTTPException)
"""

import json
import re
from typing import Optional

import frappe
from frappe import _

from accountant_agent.agent_api.db.agent_api_repository import (
	chat_session_exists,
	doctype_exists,
	execute_select_query,
	find_settings_name_by_api_key,
	get_doctype_metadata,
	insert_chat_history_record,
	update_chat_last_timestamp,
)


# ─── Domain Exceptions (protocol-agnostic) ──────────────────────────────────


class AuthenticationRequiredError(Exception):
	"""Raised when no API key is provided."""
	pass


class InvalidApiKeyError(Exception):
	"""Raised when the API key does not match any Agent Settings record."""
	pass


class MissingParameterError(Exception):
	"""Raised when a required parameter is missing."""

	def __init__(self, parameter_name: str) -> None:
		self.parameter_name = parameter_name
		super().__init__(f"Missing required parameter: {parameter_name}")


class ForbiddenQueryError(Exception):
	"""Raised when a SQL query contains forbidden keywords or is not a SELECT."""

	def __init__(self, reason: str) -> None:
		self.reason = reason
		super().__init__(reason)


class ResourceNotFoundError(Exception):
	"""Raised when a requested resource does not exist."""

	def __init__(self, resource_type: str, identifier: str) -> None:
		self.resource_type = resource_type
		self.identifier = identifier
		super().__init__(f"{resource_type} '{identifier}' not found.")


class QueryExecutionError(Exception):
	"""Raised when a SQL query fails during execution."""

	def __init__(self, detail: str) -> None:
		self.detail = detail
		super().__init__(f"SQL Execution Error: {detail}")


class ClarificationProcessingError(Exception):
	"""Raised when clarification request processing fails."""

	def __init__(self, detail: str) -> None:
		self.detail = detail
		super().__init__(detail)


class InvalidPayloadFormatError(Exception):
	"""Raised when a payload cannot be parsed into the expected format."""

	def __init__(self, detail: str) -> None:
		self.detail = detail
		super().__init__(detail)


# ─── Forbidden SQL Keywords ─────────────────────────────────────────────────

_FORBIDDEN_SQL_PATTERNS: list[re.Pattern] = [
	re.compile(pattern, re.IGNORECASE)
	for pattern in [
		r"\binsert\b", r"\bupdate\b", r"\bdelete\b", r"\bdrop\b",
		r"\balter\b", r"\bcreate\b", r"\btruncate\b", r"\breplace\b",
		r"\brename\b", r"\bgrant\b", r"\brevoke\b", r"\bexecute\b",
		r"\bload_file\b", r"\boutfile\b",
	]
]

_SELECT_START_PATTERN: re.Pattern = re.compile(
	r"^\s*(select|with)\b", re.IGNORECASE
)


# ─── Authentication Service ─────────────────────────────────────────────────


def authenticate_by_api_key(api_key: Optional[str]) -> str:
	"""
	Validate the given API key against stored Agent Settings records.

	Args:
		api_key: The API key string to validate.

	Returns:
		The matching settings document name (agent account email).

	Raises:
		AuthenticationRequiredError: If api_key is empty/None.
		InvalidApiKeyError: If no matching record is found.
	"""
	if not api_key:
		raise AuthenticationRequiredError()

	settings_user = find_settings_name_by_api_key(api_key)
	if not settings_user:
		raise InvalidApiKeyError()

	return settings_user


# ─── SQL Query Execution Service ─────────────────────────────────────────────


def validate_and_execute_query(sql_query: str, settings_user: str) -> dict:
	"""
	Validate a SQL query for read-only safety, then execute it.

	Args:
		sql_query: The raw SQL query string.
		settings_user: The authenticated user identifier.

	Returns:
		dict with keys: success, user, columns, data.

	Raises:
		MissingParameterError: If sql_query is empty.
		ForbiddenQueryError: If the query is not a SELECT or contains forbidden keywords.
		QueryExecutionError: If the query fails during execution.
	"""
	if not sql_query:
		raise MissingParameterError("sql_query")

	clean_query = sql_query.strip()

	# Guard: must start with SELECT or WITH (for CTEs)
	if not _SELECT_START_PATTERN.match(clean_query):
		raise ForbiddenQueryError(
			"Only SELECT queries are allowed for security reasons."
		)

	# Guard: scan for forbidden modification keywords anywhere in the query
	for pattern in _FORBIDDEN_SQL_PATTERNS:
		match = pattern.search(clean_query)
		if match:
			keyword = match.group(0)
			raise ForbiddenQueryError(
				f"Query contains forbidden keyword: {keyword}"
			)

	try:
		result = execute_select_query(clean_query)
		columns = list(result[0].keys()) if result else []
		return {
			"success": True,
			"user": settings_user,
			"columns": columns,
			"data": result,
		}
	except Exception as exc:
		raise QueryExecutionError(str(exc)) from exc


# ─── DocType Schema Service ─────────────────────────────────────────────────

# Layout field types that carry no data and should be excluded from schema summaries
_IGNORED_FIELD_TYPES: set[str] = {
	"Section Break", "Column Break", "Tab Break",
	"HTML", "Fold", "Table", "Heading",
}


def build_doctype_schema_summary(doctype: str) -> dict:
	"""
	Build a filtered schema summary for a DocType, optimized for LLM consumption.

	Args:
		doctype: The DocType name to summarize.

	Returns:
		dict with keys: success, doctype, table_name, fields.

	Raises:
		MissingParameterError: If doctype is empty.
		ResourceNotFoundError: If the DocType does not exist.
	"""
	if not doctype:
		raise MissingParameterError("doctype")

	if not doctype_exists(doctype):
		raise ResourceNotFoundError("DocType", doctype)

	meta = get_doctype_metadata(doctype)

	fields_summary: list[str] = [
		"name (Data/Primary Key)",
		"docstatus (Int: 0=Draft, 1=Submitted, 2=Cancelled)",
	]

	for df in meta.fields:
		if df.fieldtype in _IGNORED_FIELD_TYPES:
			continue

		info = f"{df.fieldname} ({df.fieldtype})"

		if df.label:
			info += f" - {df.label}"
		if df.reqd:
			info += " [Mandatory]"

		if df.fieldtype == "Link" and df.options:
			info += f" -> Link to {df.options}"
		elif df.fieldtype == "Select" and df.options:
			opts = [o.strip() for o in df.options.split("\n") if o.strip()]
			if len(opts) <= 10:
				info += f" [Options: {', '.join(opts)}]"

		fields_summary.append(info)

	return {
		"success": True,
		"doctype": doctype,
		"table_name": f"tab{doctype}",
		"fields": fields_summary,
	}


# ─── Clarification Request Service ──────────────────────────────────────────


def parse_questions_payload(questions_raw) -> list:
	"""
	Parse a raw questions payload into a validated list of question dicts.

	Args:
		questions_raw: Either a JSON string or a list of question dicts.

	Returns:
		Parsed list of question dicts.

	Raises:
		InvalidPayloadFormatError: If parsing fails or result is not a list.
	"""
	if isinstance(questions_raw, list):
		return questions_raw

	try:
		parsed = json.loads(questions_raw)
	except (json.JSONDecodeError, TypeError) as exc:
		raise InvalidPayloadFormatError(
			f"Invalid questions format. Must be a JSON array. Error: {exc}"
		) from exc

	if not isinstance(parsed, list):
		raise InvalidPayloadFormatError(
			"questions must be a list / JSON array."
		)

	return parsed


def process_clarification_request(
	session_id: str,
	questions_raw,
	settings_user: str,
) -> dict:
	"""
	Process a clarification request: parse questions, persist to chat history,
	and broadcast a real-time event.

	Args:
		session_id: The chat session UUID.
		questions_raw: Raw questions payload (string or list).
		settings_user: The authenticated user identifier.

	Returns:
		dict with keys: success, message.

	Raises:
		MissingParameterError: If session_id or questions_raw is missing.
		ResourceNotFoundError: If the chat session does not exist.
		InvalidPayloadFormatError: If questions cannot be parsed.
		ClarificationProcessingError: If saving or broadcasting fails.
	"""
	if not session_id:
		raise MissingParameterError("session_id")
	if not questions_raw:
		raise MissingParameterError("questions")

	parsed_questions = parse_questions_payload(questions_raw)

	if not chat_session_exists(session_id):
		raise ResourceNotFoundError("Chat session", session_id)

	# Build the JSON content payload for chat history
	content_payload = {
		"type": "clarification",
		"questions": parsed_questions,
	}
	content_json = json.dumps(content_payload, ensure_ascii=False)

	try:
		insert_chat_history_record(session_id, "ai", content_json)
		update_chat_last_timestamp(session_id)

		# Broadcast real-time notification to connected clients
		frappe.publish_realtime(
			event="agent_clarification_requested",
			message={
				"session_id": session_id,
				"questions": parsed_questions,
				"content": content_json,
			},
		)

		return {
			"success": True,
			"message": "Clarification request saved and broadcasted successfully.",
		}
	except Exception as exc:
		raise ClarificationProcessingError(
			f"Error saving clarification request: {exc}"
		) from exc
