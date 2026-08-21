# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""
Controller (Router) Layer — Agent API
--------------------------------------
Frappe whitelisted endpoints that handle HTTP request parsing,
input extraction, delegation to the service layer, and mapping
domain exceptions to HTTP status codes.

Prohibitions (per three-layer rules):
  ❌ NO database or ORM calls
  ❌ NO business validation or calculations
  ❌ NO transaction management
"""

import frappe
from frappe import _

from accountant_agent.agent_api.services.agent_api_service import (
	AuthenticationRequiredError,
	ClarificationProcessingError,
	FileTooLargeError,
	ForbiddenQueryError,
	InvalidApiKeyError,
	InvalidPayloadFormatError,
	MissingParameterError,
	QueryExecutionError,
	ResourceNotFoundError,
	authenticate_by_api_key,
	build_doctype_schema_summary,
	process_clarification_request,
	validate_and_execute_query,
	save_generated_file,
)


# ─── Input Extraction Helpers ───────────────────────────────────────────────


def _extract_api_key(explicit_value: str | None = None) -> str | None:
	"""
	Extract the API key from the explicit parameter, form data, or request headers.
	Returns the first non-empty value found, or None.
	"""
	if explicit_value:
		return explicit_value
	return (
		frappe.form_dict.get("api_key")
		or frappe.request.headers.get("X-API-Key")
	)


def _extract_param(explicit_value: list | str | None, param_name: str) -> list | str | None:
	"""
	Extract a parameter from the explicit value or fall back to form data.
	"""
	if explicit_value is not None:
		return explicit_value
	return frappe.form_dict.get(param_name)


# ─── Exception-to-HTTP Mapping ──────────────────────────────────────────────


def _set_error_response(status_code: int, error_message: str) -> dict:
	"""Set the HTTP status code on the response and return an error dict."""
	frappe.local.response.http_status_code = status_code
	return {"error": error_message}


# ─── Whitelisted Endpoints ──────────────────────────────────────────────────


@frappe.whitelist(allow_guest=True)
def execute_query(sql_query: str | None = None, api_key: str | None = None) -> dict:
	"""
	Execute a read-only SQL SELECT query on behalf of the agent,
	authenticated by the user's API Key (UUID).
	"""
	resolved_api_key = _extract_api_key(api_key)
	resolved_query = _extract_param(sql_query, "sql_query")

	try:
		settings_user = authenticate_by_api_key(resolved_api_key)
		return validate_and_execute_query(resolved_query, settings_user)

	except AuthenticationRequiredError:
		return _set_error_response(401, "Missing API Key. Authentication required.")
	except InvalidApiKeyError:
		return _set_error_response(403, "Invalid API Key. Authentication failed.")
	except MissingParameterError:
		return _set_error_response(400, "Missing SQL query.")
	except ForbiddenQueryError as exc:
		return _set_error_response(400, exc.reason)
	except QueryExecutionError as exc:
		# The agent needs enough to correct its own SQL (a bad column name, a
		# timeout), so the driver message is forwarded here by design — this
		# endpoint's caller is the agent, never an end user, and the agent's
		# own persona rules keep it off the customer's screen.
		return _set_error_response(500, str(exc))


@frappe.whitelist(allow_guest=True)
def get_doctype_schema(doctype: str | None = None, api_key: str | None = None) -> dict:
	"""
	Return a formatted and filtered schema summary for a given DocType,
	authenticated by the user's API Key.
	"""
	resolved_api_key = _extract_api_key(api_key)
	resolved_doctype = _extract_param(doctype, "doctype")

	try:
		authenticate_by_api_key(resolved_api_key)
		return build_doctype_schema_summary(resolved_doctype)

	except AuthenticationRequiredError:
		return _set_error_response(401, "Missing API Key. Authentication required.")
	except InvalidApiKeyError:
		return _set_error_response(403, "Invalid API Key. Authentication failed.")
	except MissingParameterError:
		return _set_error_response(400, "Missing DocType parameter.")
	except ResourceNotFoundError as exc:
		return _set_error_response(404, str(exc))
	except Exception as exc:
		return _set_error_response(500, f"Error retrieving DocType schema: {exc}")


@frappe.whitelist(allow_guest=True)
def request_clarification(
	session_id: str | None = None,
	questions: list | str | None = None,
	api_key: str | None = None,
) -> dict:
	"""
	Receive clarification questions from the agent server.
	Save the questions in the chat history and trigger a real-time event.
	"""
	resolved_api_key = _extract_api_key(api_key)
	resolved_session_id = _extract_param(session_id, "session_id")
	resolved_questions = _extract_param(questions, "questions")

	try:
		settings_user = authenticate_by_api_key(resolved_api_key)
		return process_clarification_request(
			resolved_session_id, resolved_questions, settings_user
		)

	except AuthenticationRequiredError:
		return _set_error_response(401, "Missing API Key. Authentication required.")
	except InvalidApiKeyError:
		return _set_error_response(403, "Invalid API Key. Authentication failed.")
	except MissingParameterError as exc:
		return _set_error_response(
			400, f"Missing {exc.parameter_name} parameter."
		)
	except ResourceNotFoundError as exc:
		return _set_error_response(404, str(exc))
	except InvalidPayloadFormatError as exc:
		return _set_error_response(400, exc.detail)
	except ClarificationProcessingError as exc:
		return _set_error_response(500, exc.detail)


@frappe.whitelist(allow_guest=True)
def upload_generated_file(
	session_id: str | None = None,
	api_key: str | None = None,
) -> dict:
	"""
	Whitelisted endpoint allowing the authenticated Agent to upload
	generated report files directly into Frappe.
	"""
	resolved_api_key = _extract_api_key(api_key)
	resolved_session_id = _extract_param(session_id, "session_id")

	try:
		settings_user = authenticate_by_api_key(resolved_api_key)
		uploaded_file = frappe.request.files.get("file")
		if not uploaded_file:
			return _set_error_response(400, "No file uploaded.")

		return save_generated_file(resolved_session_id, uploaded_file, settings_user)

	except AuthenticationRequiredError:
		return _set_error_response(401, "Missing API Key. Authentication required.")
	except InvalidApiKeyError:
		return _set_error_response(403, "Invalid API Key. Authentication failed.")
	except MissingParameterError as exc:
		return _set_error_response(400, f"Missing {exc.parameter_name} parameter.")
	except ResourceNotFoundError as exc:
		return _set_error_response(404, str(exc))
	except FileTooLargeError as exc:
		return _set_error_response(
			413,
			f"The generated report exceeds the {exc.limit_bytes // (1024 * 1024)} MB limit.",
		)
	except Exception:
		# Never surface the raw exception: it carries site paths and storage
		# details (project_rules.md §5, Zero Leakage). The detail goes to the
		# Error Log, where an administrator can see it and a caller cannot.
		frappe.log_error(
			title="Agent API: generated file upload",
			message=frappe.get_traceback(),
		)
		return _set_error_response(500, "The generated file could not be stored.")

