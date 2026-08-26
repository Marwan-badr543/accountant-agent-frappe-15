# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""
Controller (Router) Layer — Outbound Messaging
-----------------------------------------------
Whitelisted endpoints the agent calls to learn what this site can send through,
and to send one message.

Prohibitions (per three-layer rules):
  ❌ NO database or ORM calls
  ❌ NO business validation or calculations
  ❌ NO transaction management
"""

import json

import frappe

from accountant_agent.agent_api.services.agent_api_service import (
	AuthenticationRequiredError,
	InvalidApiKeyError,
	MissingParameterError,
	authenticate_by_api_key,
)
from accountant_agent.agent_api.services.agent_messaging_service import (
	AttachmentError,
	ChannelNotConfiguredError,
	MessagingError,
	ProviderRefusedError,
	UnknownDestinationError,
	get_messaging_config as read_messaging_config,
	send_message as send_message_service,
)


def _extract_api_key(explicit_value: str | None = None) -> str | None:
	if explicit_value:
		return explicit_value
	return (
		frappe.form_dict.get("api_key")
		or frappe.get_request_header("X-Api-Key")
		or frappe.get_request_header("Authorization")
	)


def _extract_param(explicit_value, name: str):
	if explicit_value not in (None, ""):
		return explicit_value
	return frappe.form_dict.get(name)


def _set_error_response(status_code: int, message: str, code: str = "") -> dict:
	frappe.local.response["http_status_code"] = status_code
	payload = {"error": message}
	if code:
		payload["error_code"] = code
	return payload


def _as_list(value) -> list[str]:
	"""Accept a JSON array, a real list, or a single string.

	The agent posts form-encoded, so a list arrives as a JSON string. A caller
	that sends one bare url should not have to know that.
	"""
	if value in (None, ""):
		return []
	if isinstance(value, list):
		return [str(v) for v in value]
	text = str(value).strip()
	if text.startswith("["):
		try:
			parsed = json.loads(text)
			return [str(v) for v in parsed] if isinstance(parsed, list) else []
		except (TypeError, ValueError):
			return []
	return [text]


@frappe.whitelist(allow_guest=True)
def get_messaging_config(api_key: str | None = None) -> dict:
	"""What this site can send through, and where.

	Carries no secrets — see the service. The agent needs this to answer
	honestly when an accountant asks it to send something the site cannot send.
	"""
	try:
		authenticate_by_api_key(_extract_api_key(api_key))
		return read_messaging_config()
	except AuthenticationRequiredError:
		return _set_error_response(401, "Missing API Key. Authentication required.")
	except InvalidApiKeyError:
		return _set_error_response(403, "Invalid API Key. Authentication failed.")
	except Exception:
		frappe.log_error(
			title="Agent messaging config read failed", message=frappe.get_traceback(),
		)
		return _set_error_response(500, "The messaging settings could not be read.")



@frappe.whitelist(allow_guest=True)
def send_message(
	channel: str | None = None,
	destination: str | None = None,
	subject: str | None = None,
	body: str | None = None,
	file_urls=None,
	idempotency_key: str | None = None,
	approved_by: str | None = None,
	run_id: str | None = None,
	session_id: str | None = None,
	api_key: str | None = None,
) -> dict:
	"""Send one message on the company's behalf.

	Returns a receipt carrying the provider's own message id. The agent may
	only tell a customer something was sent when that id is present — every
	other outcome, including a refusal, comes back as an error the agent
	repeats rather than as a silent success.
	"""
	resolved_api_key = _extract_api_key(api_key)

	try:
		settings_user = authenticate_by_api_key(resolved_api_key)

		resolved_channel = _extract_param(channel, "channel")
		resolved_key = _extract_param(idempotency_key, "idempotency_key")
		if not resolved_channel:
			raise MissingParameterError("channel")
		if not resolved_key:
			raise MissingParameterError("idempotency_key")

		return send_message_service(
			channel=resolved_channel,
			destination=_extract_param(destination, "destination"),
			subject=_extract_param(subject, "subject"),
			body=_extract_param(body, "body"),
			file_urls=_as_list(_extract_param(file_urls, "file_urls")),
			idempotency_key=resolved_key,
			requested_by=settings_user,
			approved_by=_extract_param(approved_by, "approved_by"),
			run_id=_extract_param(run_id, "run_id"),
			session_id=_extract_param(session_id, "session_id"),
		)

	except AuthenticationRequiredError:
		return _set_error_response(401, "Missing API Key. Authentication required.")
	except InvalidApiKeyError:
		return _set_error_response(403, "Invalid API Key. Authentication failed.")
	except MissingParameterError as exc:
		return _set_error_response(400, f"Missing {exc.parameter_name} parameter.")
	except ChannelNotConfiguredError as exc:
		# 409, not 400: the request was well formed and the site is simply not
		# set up to honour it. The agent turns this into "email is not switched
		# on here", which sends the customer to their administrator rather than
		# making them rephrase a correct request.
		return _set_error_response(409, exc.detail, exc.code)
	except UnknownDestinationError as exc:
		return _set_error_response(400, exc.detail, exc.code)
	except AttachmentError as exc:
		return _set_error_response(400, exc.detail, exc.code)
	except ProviderRefusedError as exc:
		return _set_error_response(502, exc.detail, exc.code)
	except MessagingError as exc:
		return _set_error_response(400, exc.detail, exc.code)
	except Exception:
		# Never surface the raw exception: a provider error body can carry the
		# request back, and for Gmail that includes the message being sent.
		frappe.log_error(title="Agent message send failed", message=frappe.get_traceback())
		return _set_error_response(500, "The message could not be sent.")
