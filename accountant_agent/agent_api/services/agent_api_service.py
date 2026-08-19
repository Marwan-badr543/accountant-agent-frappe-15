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
from datetime import timedelta
from typing import Any, Optional

import frappe
from frappe import _
from frappe.utils.file_manager import save_file

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


# ─── Query Safety Policy ────────────────────────────────────────────────────
#
# Everything this endpoint executes was written by a language model, and that
# model's context can contain text supplied by whoever sent the customer a
# document. The guards below therefore assume a hostile query author, not a
# careless one.

#: Statements that modify data or schema. Matched against a query whose string
#: literals have been masked first — otherwise an invoice whose description
#: contains the word "update" is refused, and the agent, told only that its
#: query was forbidden, retries the same thing forever.
_FORBIDDEN_SQL_PATTERNS: list[re.Pattern] = [
	re.compile(pattern, re.IGNORECASE)
	for pattern in [
		r"\binsert\b", r"\bupdate\b", r"\bdelete\b", r"\bdrop\b",
		r"\balter\b", r"\bcreate\b", r"\btruncate\b", r"\breplace\s+into\b",
		r"\brename\b", r"\bgrant\b", r"\brevoke\b", r"\bexecute\b",
		r"\bload_file\b", r"\boutfile\b", r"\bdumpfile\b", r"\binto\s+outfile\b",
		r"\bset\s+session\b", r"\bset\s+global\b",
	]
]

_SELECT_START_PATTERN: re.Pattern = re.compile(
	r"^\s*(select|with)\b", re.IGNORECASE
)

#: Tables that hold credentials, sessions or engine internals. None of them is
#: an accounting record, so refusing them costs the product nothing — while
#: allowing them would turn one prompt injection in an uploaded PDF into the
#: exfiltration of every stored secret on the customer's site. `__Auth` alone
#: holds the encrypted password of every ERP user.
_DENIED_IDENTIFIER_PATTERNS: list[re.Pattern] = [
	re.compile(pattern, re.IGNORECASE)
	for pattern in [
		# Frappe's own internal tables, all of which are `__`-prefixed.
		r"__auth", r"__global_search", r"__user_settings", r"__usersettings",
		# Database engine catalogues. information_schema is handled separately
		# below — the audit agent legitimately enumerates tables and columns
		# through it, so a blanket refusal here would break schema discovery.
		r"\bperformance_schema\b",
		r"\bmysql\s*\.", r"\bpg_catalog\b", r"\bpg_shadow\b", r"\bpg_authid\b",
		# DocTypes that exist to store secrets and integration credentials.
		r"tabAgent\s+Settings", r"tabOAuth\s+Bearer\s+Token",
		r"tabOAuth\s+Authorization\s+Code", r"tabToken\s+Cache",
		r"tabSocial\s+Login\s+Key", r"tabConnected\s+App",
		r"tabWebhook", r"tabIntegration\s+Request",
	]
]

#: Column names that carry secrets wherever they appear. `tabUser` is otherwise
#: a legitimate read — an accountant asks who posted a journal entry — so the
#: block is on the sensitive columns rather than on the table.
_DENIED_COLUMN_PATTERNS: list[re.Pattern] = [
	re.compile(pattern, re.IGNORECASE)
	for pattern in [
		r"\bapi_secret\b", r"\bencryption_key\b", r"\breset_password_key\b",
		r"\bsocial_login_userid\b",
	]
]

#: Rows returned to the agent. Matches the contract the agent already publishes
#: to the model in `agent/tools/tools.py` and `agent/agent_ask/prompts.py`
#: ("max 500 rows"); before this, that promise was made by the caller and kept
#: by nobody.
DEFAULT_MAX_RESULT_ROWS: int = 500

#: Seconds of server-side execution a single agent query may consume. Closes
#: companion issue E-2 (z_plan/analyse_plan.md §8.2): the agent's HTTP timeout
#: releases the *caller*, but the database keeps running the abandoned query and
#: keeps contending with the customer's own postings. Only the database can stop
#: it, so the limit has to be set here.
DEFAULT_QUERY_TIMEOUT_SECONDS: int = 30

#: The only two information_schema views the agent has a reason to read. The
#: audit agent discovers the customer's ledger tables and their columns through
#: them (agent/agent_audit/audit_nodes.py), and both are schema shape rather
#: than data — no credentials, no privileges, no live session list. Everything
#: else in that schema, notably the *_privileges views and processlist, stays
#: refused.
_ALLOWED_INFORMATION_SCHEMA_VIEWS: frozenset[str] = frozenset({"tables", "columns"})

_INFORMATION_SCHEMA_PATTERN: re.Pattern = re.compile(
	r"\binformation_schema\b(?:\s*\.\s*([a-zA-Z0-9_]+))?", re.IGNORECASE
)

_STRING_LITERAL_PATTERN: re.Pattern = re.compile(
	r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"", re.DOTALL
)


def _mask_string_literals(query: str) -> str:
	"""Blank out quoted literals so keyword scanning reads code, not data.

	`WHERE customer_name LIKE '%Drop Shipping%'` is an ordinary accounting
	query. Scanning it raw refuses it for containing "drop".
	"""
	return _STRING_LITERAL_PATTERN.sub("''", query)


def _max_result_rows() -> int:
	return int(frappe.conf.get("accountant_agent_max_query_rows") or DEFAULT_MAX_RESULT_ROWS)


def _query_timeout_seconds() -> int:
	return int(
		frappe.conf.get("accountant_agent_query_timeout_seconds")
		or DEFAULT_QUERY_TIMEOUT_SECONDS
	)


def assert_query_is_read_only(clean_query: str) -> None:
	"""Refuse anything that is not a single, self-contained, read-only SELECT.

	Raises:
		ForbiddenQueryError: with a reason the agent can act on.
	"""
	if not _SELECT_START_PATTERN.match(clean_query):
		raise ForbiddenQueryError(
			"Only SELECT queries are allowed for security reasons."
		)

	scannable = _mask_string_literals(clean_query)

	# Stacked statements. pymysql does not enable MULTI_STATEMENTS, but psycopg2
	# executes every statement in the string, so on a Postgres site the
	# SELECT-must-come-first guard alone would wave through `SELECT 1; ...`.
	if ";" in scannable.rstrip().rstrip(";"):
		raise ForbiddenQueryError(
			"Only a single statement may be executed per request."
		)

	for pattern in _FORBIDDEN_SQL_PATTERNS:
		match = pattern.search(scannable)
		if match:
			raise ForbiddenQueryError(
				f"Query contains forbidden keyword: {match.group(0)}"
			)

	for pattern in _DENIED_IDENTIFIER_PATTERNS:
		if pattern.search(scannable):
			raise ForbiddenQueryError(
				"This query reads a system or credential table, which is not "
				"permitted. Only business records are available."
			)

	for pattern in _DENIED_COLUMN_PATTERNS:
		if pattern.search(scannable):
			raise ForbiddenQueryError(
				"This query reads a credential column, which is not permitted."
			)

	for match in _INFORMATION_SCHEMA_PATTERN.finditer(scannable):
		view = (match.group(1) or "").lower()
		if view not in _ALLOWED_INFORMATION_SCHEMA_VIEWS:
			raise ForbiddenQueryError(
				"Only information_schema.tables and information_schema.columns "
				"may be read."
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


#: `FROM Sales Invoice WHERE ...` — the identifier run swallows the trailing
#: clause, so the rewrite has to try progressively shorter word prefixes.
_TABLE_REFERENCE_PATTERN: re.Pattern = re.compile(
	r'\b(from|join)\s+([`"]?)(tab)?([a-zA-Z0-9_\-\s]+)\2',
	re.IGNORECASE,
)


def _rewrite_query(query: str) -> str:
	"""Prefix bare DocType names with `tab` so the agent can write `FROM User`.

	Multi-word DocTypes are why this is not a one-line substitution. The
	identifier character class has to include spaces to match `Sales Invoice`,
	which means in `FROM Sales Invoice WHERE posting_date > ...` it also
	swallows `WHERE posting_date`. Testing only the full run therefore fails to
	resolve every multi-word DocType that is followed by a clause — which is
	almost all of them in real queries — and the agent gets a raw SQL error for
	a query that was correct.

	So: try the longest word prefix first and shorten until a DocType matches,
	leaving whatever follows untouched. Lookups are memoised per call because
	the same table is typically referenced several times in one query.
	"""
	resolved: dict[str, bool] = {}

	def is_doctype(candidate: str) -> bool:
		if candidate not in resolved:
			resolved[candidate] = doctype_exists(candidate)
		return resolved[candidate]

	def replace_match(match: re.Match) -> str:
		keyword, quote, has_tab, candidate = match.groups()
		if has_tab:
			return match.group(0)

		words = candidate.split()
		if not words:
			return match.group(0)

		for length in range(len(words), 0, -1):
			name = " ".join(words[:length])
			if not is_doctype(name):
				continue
			remainder = candidate[candidate.index(name) + len(name):]
			# The identifier run is greedy enough to have swallowed any
			# following JOIN — `FROM Sales Invoice a JOIN Account b` is one
			# match — so the tail is rewritten too. re.sub does not rescan its
			# own replacement, and without this the second table stays bare.
			return f"{keyword} {quote}tab{name}{quote}" + _TABLE_REFERENCE_PATTERN.sub(
				replace_match, remainder
			)

		return match.group(0)

	return _TABLE_REFERENCE_PATTERN.sub(replace_match, query)


def validate_and_execute_query(sql_query: str, settings_user: str) -> dict:
	"""Validate a SQL query for read-only safety, then execute it under limits.

	Args:
		sql_query: The raw SQL query string.
		settings_user: The authenticated user identifier.

	Returns:
		dict with keys: success, user, columns, data, row_count, truncated,
		max_rows.

		`truncated` exists so the agent can tell the user the truth. A silently
		capped result set is worse than no result at all here: the model would
		compute a total over 500 of 40,000 ledger rows and present it as the
		balance, which is precisely the fabricated-figure failure that
		project_rules.md §6 forbids.

	Raises:
		MissingParameterError: If sql_query is empty.
		ForbiddenQueryError: If the query is not a read-only single SELECT.
		QueryExecutionError: If the query fails during execution.
	"""
	if not sql_query:
		raise MissingParameterError("sql_query")

	clean_query = _rewrite_query(sql_query.strip())
	assert_query_is_read_only(clean_query)

	max_rows = _max_result_rows()

	try:
		result = execute_select_query(
			clean_query,
			max_rows=max_rows,
			timeout_seconds=_query_timeout_seconds(),
		)
	except Exception as exc:
		raise QueryExecutionError(str(exc)) from exc

	columns = list(result[0].keys()) if result else []
	return {
		"success": True,
		"user": settings_user,
		"columns": columns,
		"data": result,
		"row_count": len(result),
		"truncated": len(result) >= max_rows,
		# The cap itself, not just the fact that it bound. A caller that knows
		# the page size can walk a wide GROUP BY in cursor-sized pages and
		# report the whole population; a caller that only knows "truncated"
		# has to guess the page size, and a guess that is wrong either stops
		# early (a partial figure presented as a total) or asks for pages that
		# do not exist. The site owns this number — `accountant_agent_max_query_rows`
		# — so the site is what publishes it.
		"max_rows": max_rows,
	}


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
		# Whether `docstatus` means anything on this table. A submittable
		# DocType stores drafts (0), submitted (1) and cancelled (2) records
		# side by side, so an analysis of it that does not constrain docstatus
		# totals invoices that were never issued and invoices that were
		# withdrawn. A master such as Customer or Item is never submitted and
		# every row sits at 0, so the same constraint would return nothing.
		# The caller cannot tell the two apart from the field list alone.
		"is_submittable": bool(getattr(meta, "is_submittable", 0)),
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


#: Ceiling on a generated report the agent may push back into the ERP. Reports
#: are spreadsheets and PDFs, not datasets; anything larger is a bug in the
#: generator, and without a cap that bug becomes an unbounded write into the
#: customer's file store.
MAX_GENERATED_FILE_BYTES: int = 25 * 1024 * 1024

#: How long a generated report stays available for download before the hourly
#: sweep removes it. Long enough to open from the chat, short enough that the
#: customer's private file store does not accumulate financial extracts.
GENERATED_FILE_RETENTION_HOURS: int = 3

#: Deletions per sweep. Bounds one job's runtime and memory on a site whose
#: cleanup has not run for a while, rather than loading every stale row at once.
_CLEANUP_BATCH_SIZE: int = 500


class FileTooLargeError(Exception):
	"""Raised when an uploaded file exceeds the permitted size."""

	def __init__(self, size_bytes: int, limit_bytes: int) -> None:
		self.size_bytes = size_bytes
		self.limit_bytes = limit_bytes
		super().__init__(
			f"File of {size_bytes} bytes exceeds the {limit_bytes} byte limit."
		)


def save_generated_file(session_id: str, uploaded_file: Any, settings_user: str) -> dict:
	"""Store an agent-generated report as a private file linked to the session.

	Private, always. These are trial balances, ageing schedules and audit
	working papers; a public file in Frappe is served to anonymous callers by
	URL alone.

	Raises:
		MissingParameterError: If session_id is missing.
		ResourceNotFoundError: If the chat session does not exist.
		FileTooLargeError: If the payload exceeds MAX_GENERATED_FILE_BYTES.
	"""
	if not session_id:
		raise MissingParameterError("session_id")

	if not chat_session_exists(session_id):
		raise ResourceNotFoundError("Chat session", session_id)

	content = uploaded_file.read()
	if len(content) > MAX_GENERATED_FILE_BYTES:
		raise FileTooLargeError(len(content), MAX_GENERATED_FILE_BYTES)

	file_doc = save_file(
		fname=uploaded_file.filename,
		content=content,
		dt="Agent Chats",
		dn=session_id,
		is_private=1,
		df=None,
	)

	return {
		"success": True,
		"file_url": file_doc.file_url,
		"filename": file_doc.file_name,
	}


def cleanup_old_files() -> None:
	"""Hourly sweep of expired agent-generated reports.

	Scoped to files this app attached to an Agent Chats session, which is only
	ever a report the agent produced — user uploads never become File documents
	(see `upload_agent_file`). Nothing a customer uploaded is at risk here.

	The cutoff uses `frappe.utils.now_datetime`, not `datetime.now`. Frappe
	stores `creation` in the site's configured timezone; comparing it against
	the container's local clock silently shifts the window by the UTC offset,
	which either spares files forever or deletes them while they are still on
	screen.
	"""
	cutoff = frappe.utils.now_datetime() - timedelta(hours=GENERATED_FILE_RETENTION_HOURS)

	expired = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Agent Chats",
			"is_private": 1,
			"creation": ["<", cutoff],
		},
		pluck="name",
		limit=_CLEANUP_BATCH_SIZE,
	)

	for file_name in expired:
		try:
			frappe.delete_doc("File", file_name, ignore_permissions=True, delete_permanently=True)
			frappe.db.commit()
		except Exception as exc:
			# Commit per file, and roll back only the one that failed: a single
			# undeletable file must not discard the deletions that succeeded
			# before it, or the sweep can never make progress past it.
			frappe.db.rollback()
			frappe.log_error(
				title="Agent file cleanup",
				message=f"Could not delete expired file {file_name}: {exc}",
			)
