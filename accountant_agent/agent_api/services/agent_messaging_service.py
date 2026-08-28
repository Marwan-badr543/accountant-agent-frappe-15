# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""
Service Layer — Outbound Messaging
-----------------------------------
Sends email, Telegram, and Slack messages on the company's behalf, from
credentials that live in this site and never leave it.

WHY THE SENDING HAPPENS HERE AND NOT ON THE AGENT SERVER
	The agent could fetch this configuration over the API and make the call
	itself. That would put a customer's Google service-account key and Telegram
	bot token across the network and into another process's memory. Keeping the
	call here means the agent asks for an outcome ("send this to that address")
	and never holds the credential — the same trust boundary
	``agent_write_router`` already draws for writes.

WHY A MESSAGE IS TREATED LIKE A LEDGER WRITE
	An email to a client's auditor cannot be unsent. So it gets what a write
	gets: an idempotency key, so a retried request does not send twice; and a
	log row carrying the provider's OWN message id, because "no exception was
	raised" is not evidence a message was delivered. The agent may only tell a
	customer something was sent when there is an id in that row.

WHAT IS DELIBERATELY NOT HERE
	WhatsApp. Outside the 24-hour window opened by the customer, Meta permits
	only pre-approved template messages, which is a different product decision
	rather than another branch in this file — see
	``z_plan/THREE_FEATURES_UNDERSTANDING_AND_QUESTIONS.md``.
"""

import base64
import json
import mimetypes
import os
from email.message import EmailMessage
from typing import Any

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime

SETTINGS_DOCTYPE = "Agent Messaging Settings"
LOG_DOCTYPE = "Agent Message Log"

#: Gmail scope for send-only. Deliberately not `gmail.compose` or full access:
#: the delegation the customer's Workspace admin grants should let this app do
#: exactly one thing, so a compromise of the key cannot read their mail.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

SLACK_API = "https://slack.com/api/{method}"

#: Total bytes of attachments in one message. Gmail's own hard limit is 25 MB
#: for the whole encoded message and Telegram's is 50 MB per document (Slack
#: allows far more); 20 MB stays under all of them with room for base64
#: overhead, which inflates by a third.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

HTTP_TIMEOUT = 30
BODY_PREVIEW_CHARS = 500


# ─── Domain Exceptions ───────────────────────────────────────────────────────


class MessagingError(Exception):
	"""Base for everything this module refuses or fails to do."""

	def __init__(self, detail: str, code: str = "MESSAGING_ERROR") -> None:
		self.detail = detail
		self.code = code
		super().__init__(detail)


class ChannelNotConfiguredError(MessagingError):
	"""The channel is off, or its settings are incomplete.

	Separate from a send failure because the remedy is different and belongs to
	a different person: an administrator has to finish the setup, and telling
	the accountant "sending failed" would send them to the wrong place.
	"""

	def __init__(self, channel: str, detail: str) -> None:
		super().__init__(detail, code=f"{channel.upper()}_NOT_CONFIGURED")


class UnknownDestinationError(MessagingError):
	"""The named Telegram or Slack destination is not one the customer listed.

	A bot cannot start a conversation or post where it was never added, so the
	agent cannot be allowed to invent a chat or channel id: an unlisted
	destination is not a delivery failure, it is a destination that does not
	exist.
	"""

	def __init__(self, detail: str) -> None:
		super().__init__(detail, code="UNKNOWN_DESTINATION")


class AttachmentError(MessagingError):
	def __init__(self, detail: str) -> None:
		super().__init__(detail, code="ATTACHMENT_REJECTED")


class ProviderRefusedError(MessagingError):
	"""Google, Telegram, or Slack rejected the message."""

	def __init__(self, detail: str, code: str = "PROVIDER_REFUSED") -> None:
		super().__init__(detail, code=code)


# ─── Configuration ───────────────────────────────────────────────────────────


def _settings():
	return frappe.get_single(SETTINGS_DOCTYPE)


def get_messaging_config() -> dict:
	"""Which channels are usable, and where Telegram may send.

	Returns no secrets. The agent needs to know what it CAN do so it can tell
	the accountant honestly — "I can email that, but Telegram is not set up
	here" — and it needs the destination names so it can offer them. It never
	needs the bot token, so it is never sent one.
	"""
	settings = _settings()

	gmail_ready = bool(
		settings.gmail_enabled
		and settings.gmail_sender_email
		and settings.get_password("gmail_service_account_json", raise_exception=False)
	)

	destinations = [
		{
			"label": row.label,
			"is_default": bool(row.is_default),
			"notes": row.notes or "",
		}
		for row in (settings.telegram_destinations or [])
		if row.label and row.chat_id
	]
	telegram_ready = bool(
		settings.telegram_enabled
		and settings.get_password("telegram_bot_token", raise_exception=False)
		and destinations
	)

	slack_destinations = [
		{
			"label": row.label,
			"is_default": bool(row.is_default),
			"notes": row.notes or "",
		}
		for row in (settings.slack_destinations or [])
		if row.label and row.channel_id
	]
	slack_ready = bool(
		settings.slack_enabled
		and settings.get_password("slack_bot_token", raise_exception=False)
		and slack_destinations
	)

	return {
		"channels": {
			"gmail": {
				"enabled": gmail_ready,
				"sender": settings.gmail_sender_email if gmail_ready else None,
				"accepts_any_address": True,
				"unavailable_reason": None if gmail_ready else _gmail_gap(settings),
			},
			"telegram": {
				"enabled": telegram_ready,
				# Stated explicitly because it is the constraint that surprises
				# people: the agent must not offer to "send it to this number".
				"accepts_any_address": False,
				"destinations": destinations,
				"unavailable_reason": None if telegram_ready else _telegram_gap(settings, destinations),
			},
			"slack": {
				"enabled": slack_ready,
				# Same shape as Telegram: a bot posts only where it was invited,
				# so the agent may not offer to message an arbitrary person.
				"accepts_any_address": False,
				"destinations": slack_destinations,
				"unavailable_reason": None if slack_ready else _slack_gap(settings, slack_destinations),
			},
		}
	}


def _gmail_gap(settings) -> str:
	if not settings.gmail_enabled:
		return "Email sending is switched off in Agent Messaging Settings."
	if not settings.gmail_sender_email:
		return "No sending mailbox has been set in Agent Messaging Settings."
	return "The Google service account key has not been saved yet."


def _telegram_gap(settings, destinations) -> str:
	if not settings.telegram_enabled:
		return "Telegram sending is switched off in Agent Messaging Settings."
	if not settings.get_password("telegram_bot_token", raise_exception=False):
		return "No Telegram bot token has been saved yet."
	if not destinations:
		return (
			"No Telegram destinations have been added. A bot can only send to a "
			"chat that already exists, so each one has to be listed."
		)
	return "Telegram is not fully configured."


def _slack_gap(settings, destinations) -> str:
	if not settings.slack_enabled:
		return "Slack sending is switched off in Agent Messaging Settings."
	if not settings.get_password("slack_bot_token", raise_exception=False):
		return "No Slack bot token has been saved yet."
	if not destinations:
		return (
			"No Slack destinations have been added. A bot can only post into a "
			"channel it has been invited to, so each one has to be listed."
		)
	return "Slack is not fully configured."


# ─── Attachments ─────────────────────────────────────────────────────────────


def _load_attachments(file_urls: list[str]) -> list[dict]:
	"""Read the named files out of this site's own File records.

	Only files already attached to this site can be sent. The agent passes a
	``file_url``, never a filesystem path: accepting a path would make this
	endpoint an arbitrary file read on the customer's server, reachable by
	anything that can reach the agent.
	"""
	attachments: list[dict] = []
	total = 0

	for url in file_urls or []:
		if not url or not str(url).startswith("/"):
			raise AttachmentError(
				f"'{url}' is not a file on this site. Only files this site " "already holds can be attached."
			)

		name = frappe.db.get_value("File", {"file_url": url}, "name")
		if not name:
			raise AttachmentError(f"No file on this site has the address {url}.")

		file_doc = frappe.get_doc("File", name)
		try:
			content = file_doc.get_content()
		except Exception as exc:
			raise AttachmentError(f"{file_doc.file_name} could not be read: {exc}") from exc

		if isinstance(content, str):
			content = content.encode("utf-8")

		total += len(content)
		if total > MAX_ATTACHMENT_BYTES:
			raise AttachmentError(
				f"The attachments come to more than "
				f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB in total, which is "
				"more than email and Telegram accept."
			)

		attachments.append(
			{
				"filename": file_doc.file_name,
				"content": content,
				"mimetype": mimetypes.guess_type(file_doc.file_name)[0] or "application/octet-stream",
			}
		)

	return attachments


# ─── Gmail ───────────────────────────────────────────────────────────────────


def _gmail_credentials(settings):
	"""A send-only Gmail credential impersonating the configured mailbox.

	Domain-wide delegation is what makes ``with_subject`` legal: the Workspace
	administrator has authorised this service account's client id for the Gmail
	send scope across their domain, so it may act as any mailbox in it. Without
	that authorisation Google returns `unauthorized_client`, which is the error
	worth recognising because it means the admin step was never done rather
	than that the key is wrong.
	"""
	from google.oauth2 import service_account

	raw = settings.get_password("gmail_service_account_json", raise_exception=False)
	if not raw:
		raise ChannelNotConfiguredError("gmail", _gmail_gap(settings))

	try:
		info = json.loads(raw)
	except (TypeError, ValueError) as exc:
		raise ChannelNotConfiguredError(
			"gmail",
			"The Google service account key is not valid JSON. Paste the whole "
			"file, including the outermost { }.",
		) from exc

	try:
		credentials = service_account.Credentials.from_service_account_info(
			info,
			scopes=[GMAIL_SCOPE],
		)
	except Exception as exc:
		raise ChannelNotConfiguredError(
			"gmail",
			f"The Google service account key was not accepted: {exc}",
		) from exc

	return credentials.with_subject(settings.gmail_sender_email)


def _send_gmail(settings, to: str, subject: str, body: str, attachments: list[dict]) -> str:
	"""Send one email. Returns Gmail's own message id."""
	import google.auth.transport.requests as google_requests

	credentials = _gmail_credentials(settings)

	try:
		credentials.refresh(google_requests.Request())
	except Exception as exc:
		# The single most likely failure, and the one whose message must name
		# the fix: the admin never authorised the client id for this scope.
		raise ProviderRefusedError(
			"Google would not issue a token for this service account. The usual "
			"cause is that the Workspace administrator has not yet authorised "
			f"its client id for the gmail.send scope. Google said: {exc}",
			code="GMAIL_DELEGATION_NOT_AUTHORISED",
		) from exc

	message = EmailMessage()
	message["To"] = to
	message["Subject"] = subject or "(no subject)"
	message["From"] = (
		f"{settings.gmail_sender_name} <{settings.gmail_sender_email}>"
		if settings.gmail_sender_name
		else settings.gmail_sender_email
	)
	message.set_content(body or "")

	for attachment in attachments:
		maintype, _sep, subtype = attachment["mimetype"].partition("/")
		message.add_attachment(
			attachment["content"],
			maintype=maintype or "application",
			subtype=subtype or "octet-stream",
			filename=attachment["filename"],
		)

	encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

	response = requests.post(
		GMAIL_SEND_URL,
		headers={"Authorization": f"Bearer {credentials.token}"},
		json={"raw": encoded},
		timeout=HTTP_TIMEOUT,
	)

	if response.status_code >= 400:
		raise ProviderRefusedError(
			f"Gmail refused the message ({response.status_code}): " f"{_short_provider_error(response)}",
			code="GMAIL_REJECTED",
		)

	message_id = (response.json() or {}).get("id")
	if not message_id:
		# Gmail accepted the request but named no message. Without an id there
		# is no receipt, and without a receipt the agent must not claim a send.
		raise ProviderRefusedError(
			"Gmail accepted the request but returned no message id, so the send " "cannot be confirmed.",
			code="GMAIL_NO_RECEIPT",
		)
	return message_id


# ─── Telegram ────────────────────────────────────────────────────────────────


def _resolve_telegram_destination(settings, destination: str | None) -> tuple[str, str]:
	"""Turn a name the accountant used into a chat id the customer listed.

	Returns ``(chat_id, label)``. Never accepts a raw chat id from the agent:
	the listed destinations are the whole permitted set, so a request naming
	something else is refused rather than attempted.
	"""
	rows = [r for r in (settings.telegram_destinations or []) if r.label and r.chat_id]
	if not rows:
		raise ChannelNotConfiguredError("telegram", _telegram_gap(settings, []))

	if not destination:
		default = next((r for r in rows if r.is_default), None) or (rows[0] if len(rows) == 1 else None)
		if default is None:
			names = ", ".join(r.label for r in rows)
			raise UnknownDestinationError(
				f"There is more than one Telegram destination and none is marked "
				f"as the default, so I need to be told which one: {names}."
			)
		return default.chat_id, default.label

	wanted = str(destination).strip().casefold()
	for row in rows:
		if row.label.strip().casefold() == wanted or str(row.chat_id) == str(destination):
			return row.chat_id, row.label

	names = ", ".join(r.label for r in rows)
	raise UnknownDestinationError(
		f"'{destination}' is not one of the Telegram destinations set up here. "
		f"A Telegram bot cannot start a conversation, so it can only send to a "
		f"chat that has already been added: {names}."
	)


def _send_telegram(settings, chat_id: str, body: str, attachments: list[dict]) -> str:
	"""Send one Telegram message. Returns Telegram's own message id."""
	token = settings.get_password("telegram_bot_token", raise_exception=False)
	if not token:
		raise ChannelNotConfiguredError("telegram", _telegram_gap(settings, []))

	if not attachments:
		response = requests.post(
			TELEGRAM_API.format(token=token, method="sendMessage"),
			json={"chat_id": chat_id, "text": body or "", "disable_web_page_preview": True},
			timeout=HTTP_TIMEOUT,
		)
		return _telegram_receipt(response)

	# With attachments the text rides as the caption of the first document, so
	# a one-file message arrives as one notification rather than two.
	message_id = ""
	for index, attachment in enumerate(attachments):
		payload: dict[str, Any] = {"chat_id": chat_id}
		if index == 0 and body:
			# Telegram caps a caption at 1024 characters and rejects the whole
			# request if it is longer, so a long note is sent on its own first.
			if len(body) <= 1024:
				payload["caption"] = body
			else:
				text_response = requests.post(
					TELEGRAM_API.format(token=token, method="sendMessage"),
					json={"chat_id": chat_id, "text": body, "disable_web_page_preview": True},
					timeout=HTTP_TIMEOUT,
				)
				message_id = _telegram_receipt(text_response)

		response = requests.post(
			TELEGRAM_API.format(token=token, method="sendDocument"),
			data=payload,
			files={"document": (attachment["filename"], attachment["content"], attachment["mimetype"])},
			timeout=HTTP_TIMEOUT,
		)
		message_id = _telegram_receipt(response) or message_id

	return message_id


def _telegram_receipt(response) -> str:
	if response.status_code >= 400:
		raise ProviderRefusedError(
			f"Telegram refused the message ({response.status_code}): " f"{_short_provider_error(response)}",
			code="TELEGRAM_REJECTED",
		)
	payload = response.json() or {}
	if not payload.get("ok"):
		raise ProviderRefusedError(
			f"Telegram refused the message: {payload.get('description') or 'no reason given'}",
			code="TELEGRAM_REJECTED",
		)
	message_id = str((payload.get("result") or {}).get("message_id") or "")
	if not message_id:
		raise ProviderRefusedError(
			"Telegram accepted the request but returned no message id, so the " "send cannot be confirmed.",
			code="TELEGRAM_NO_RECEIPT",
		)
	return message_id


# ─── Slack ───────────────────────────────────────────────────────────────────


def _resolve_slack_destination(settings, destination: str | None) -> tuple[str, str]:
	"""Turn a name the accountant used into a channel id the customer listed.

	Returns ``(channel_id, label)``. Never accepts a raw channel id from the
	agent: the listed destinations are the whole permitted set, so a request
	naming something else is refused rather than attempted.
	"""
	rows = [r for r in (settings.slack_destinations or []) if r.label and r.channel_id]
	if not rows:
		raise ChannelNotConfiguredError("slack", _slack_gap(settings, []))

	if not destination:
		default = next((r for r in rows if r.is_default), None) or (rows[0] if len(rows) == 1 else None)
		if default is None:
			names = ", ".join(r.label for r in rows)
			raise UnknownDestinationError(
				f"There is more than one Slack destination and none is marked "
				f"as the default, so I need to be told which one: {names}."
			)
		return default.channel_id, default.label

	wanted = str(destination).strip().casefold()
	for row in rows:
		if row.label.strip().casefold() == wanted or str(row.channel_id) == str(destination):
			return row.channel_id, row.label

	names = ", ".join(r.label for r in rows)
	raise UnknownDestinationError(
		f"'{destination}' is not one of the Slack destinations set up here. "
		f"A Slack bot can only post into a channel it has been invited to, so "
		f"it can only send to one that has already been added: {names}."
	)


def _send_slack(settings, channel_id: str, body: str, attachments: list[dict]) -> str:
	"""Send one Slack message. Returns Slack's own message id.

	Files go through Slack's external-upload flow — ask for an upload URL, put
	the bytes there, then complete the upload into the channel. The body rides
	as the first file's comment so a one-file message arrives as one
	notification rather than two, the same shape ``_send_telegram`` gives.
	"""
	token = settings.get_password("slack_bot_token", raise_exception=False)
	if not token:
		raise ChannelNotConfiguredError("slack", _slack_gap(settings, []))

	headers = {"Authorization": f"Bearer {token}"}

	if not attachments:
		response = requests.post(
			SLACK_API.format(method="chat.postMessage"),
			headers=headers,
			json={"channel": channel_id, "text": body or "", "unfurl_links": False},
			timeout=HTTP_TIMEOUT,
		)
		payload = _slack_payload(response)
		message_id = str(payload.get("ts") or "")
		if not message_id:
			raise ProviderRefusedError(
				"Slack accepted the request but returned no message id, so the " "send cannot be confirmed.",
				code="SLACK_NO_RECEIPT",
			)
		return message_id

	message_id = ""
	for index, attachment in enumerate(attachments):
		ticket = _slack_payload(
			requests.post(
				SLACK_API.format(method="files.getUploadURLExternal"),
				headers=headers,
				# This endpoint takes form fields, not JSON — JSON comes back as
				# `invalid_arguments` with no hint that the encoding was the problem.
				data={"filename": attachment["filename"], "length": len(attachment["content"])},
				timeout=HTTP_TIMEOUT,
			)
		)

		put = requests.post(
			ticket["upload_url"],
			files={"file": (attachment["filename"], attachment["content"], attachment["mimetype"])},
			timeout=HTTP_TIMEOUT,
		)
		if put.status_code >= 400:
			raise ProviderRefusedError(
				f"Slack did not accept the file upload ({put.status_code}): " f"{_short_provider_error(put)}",
				code="SLACK_REJECTED",
			)

		complete: dict = {
			"files": [{"id": ticket["file_id"], "title": attachment["filename"]}],
			"channel_id": channel_id,
		}
		if index == 0 and body:
			complete["initial_comment"] = body
		_slack_payload(
			requests.post(
				SLACK_API.format(method="files.completeUploadExternal"),
				headers=headers,
				json=complete,
				timeout=HTTP_TIMEOUT,
			)
		)
		message_id = str(ticket["file_id"])

	return message_id


def _slack_payload(response) -> dict:
	"""The parsed body of a Slack reply, or a refusal that names the cause."""
	if response.status_code >= 400:
		raise ProviderRefusedError(
			f"Slack refused the message ({response.status_code}): " f"{_short_provider_error(response)}",
			code="SLACK_REJECTED",
		)
	payload = response.json() or {}
	if not payload.get("ok"):
		error = str(payload.get("error") or "no reason given")
		if error == "not_in_channel":
			# The one refusal a customer will actually hit, so it names the fix.
			raise ProviderRefusedError(
				"Slack refused the message: the bot has not been invited to that "
				"channel. Open the channel in Slack and /invite the bot, then "
				"try again.",
				code="SLACK_NOT_IN_CHANNEL",
			)
		raise ProviderRefusedError(
			f"Slack refused the message: {error}",
			code="SLACK_REJECTED",
		)
	return payload


def _short_provider_error(response) -> str:
	"""The provider's reason, trimmed, and never the whole body.

	A provider error body can carry the request back verbatim — which for Gmail
	includes the base64 of the message that was being sent.
	"""
	try:
		payload = response.json()
	except Exception:
		return (response.text or "")[:200]

	if isinstance(payload, dict):
		error = payload.get("error")
		if isinstance(error, dict):
			return str(error.get("message") or error.get("status") or "")[:300]
		if isinstance(error, str) and error:
			return error[:300]
		if payload.get("description"):
			return str(payload["description"])[:300]
	return str(payload)[:200]


# ─── The public operation ────────────────────────────────────────────────────


def send_message(
	channel: str,
	destination: str | None,
	subject: str | None,
	body: str | None,
	file_urls: list[str] | None,
	idempotency_key: str,
	requested_by: str,
	approved_by: str | None = None,
	run_id: str | None = None,
	session_id: str | None = None,
) -> dict:
	"""Send one message and return a receipt.

	Every outcome is written to ``Agent Message Log`` before this returns,
	including refusals — an outbound record that only holds successes cannot
	answer "did the agent try to email that?", which is the question an auditor
	actually asks.

	A repeat of an idempotency key that already succeeded returns the ORIGINAL
	receipt and sends nothing. The agent retries on network failures, and a
	retry that emails a client's auditor a second copy of their trial balance
	is not a small mistake.
	"""
	channel = (channel or "").strip().lower()
	if channel not in ("gmail", "telegram", "slack"):
		raise MessagingError(
			f"'{channel}' is not a channel this system can send through. "
			"Available: gmail, telegram, slack.",
			code="UNKNOWN_CHANNEL",
		)

	if not idempotency_key:
		raise MessagingError("An idempotency key is required.", code="MISSING_IDEMPOTENCY_KEY")

	replay = _existing_receipt(idempotency_key)
	if replay is not None:
		return replay

	settings = _settings()
	label = ""
	resolved_destination = destination or ""

	try:
		attachments = _load_attachments(file_urls or [])

		if channel == "gmail":
			if not settings.gmail_enabled:
				raise ChannelNotConfiguredError("gmail", _gmail_gap(settings))
			if not destination or "@" not in str(destination):
				raise UnknownDestinationError(f"'{destination}' is not an email address.")
			resolved_destination = str(destination).strip()
			provider_message_id = _send_gmail(
				settings,
				resolved_destination,
				subject or "",
				body or "",
				attachments,
			)
		elif channel == "telegram":
			if not settings.telegram_enabled:
				raise ChannelNotConfiguredError("telegram", _telegram_gap(settings, []))
			resolved_destination, label = _resolve_telegram_destination(settings, destination)
			provider_message_id = _send_telegram(
				settings,
				resolved_destination,
				_body_with_subject(subject, body),
				attachments,
			)
		else:
			if not settings.slack_enabled:
				raise ChannelNotConfiguredError("slack", _slack_gap(settings, []))
			resolved_destination, label = _resolve_slack_destination(settings, destination)
			provider_message_id = _send_slack(
				settings,
				resolved_destination,
				_body_with_subject(subject, body),
				attachments,
			)

	except MessagingError as exc:
		_record(
			idempotency_key,
			channel,
			"REFUSED" if _is_refusal(exc) else "FAILED",
			resolved_destination,
			label,
			subject,
			body,
			file_urls,
			requested_by,
			approved_by,
			run_id,
			session_id,
			provider_message_id="",
			error_code=exc.code,
			error_message=exc.detail,
		)
		_remember_channel_error(settings, channel, exc.detail)
		raise
	except Exception as exc:
		_record(
			idempotency_key,
			channel,
			"FAILED",
			resolved_destination,
			label,
			subject,
			body,
			file_urls,
			requested_by,
			approved_by,
			run_id,
			session_id,
			provider_message_id="",
			error_code="UNEXPECTED",
			error_message=str(exc)[:500],
		)
		raise

	_record(
		idempotency_key,
		channel,
		"SENT",
		resolved_destination,
		label,
		subject,
		body,
		file_urls,
		requested_by,
		approved_by,
		run_id,
		session_id,
		provider_message_id=provider_message_id,
		error_code="",
		error_message="",
	)
	_clear_channel_error(settings, channel)

	return {
		"status": "SENT",
		"channel": channel,
		"destination": resolved_destination,
		"destination_label": label,
		"provider_message_id": provider_message_id,
		"idempotency_key": idempotency_key,
	}


def _body_with_subject(subject: str | None, body: str | None) -> str:
	"""Telegram and Slack have no subject line, so a subject becomes the first line.

	Dropping it instead would lose the one-line summary the agent wrote for the
	reader, which on a report is the only thing naming what the file is.
	"""
	subject = (subject or "").strip()
	body = (body or "").strip()
	if subject and body:
		return f"{subject}\n\n{body}"
	return subject or body


def _is_refusal(exc: MessagingError) -> bool:
	"""Refused here, or failed at the provider.

	The distinction is what an operator reads first: a refusal is a
	configuration or request problem on this side, a failure is Google or
	Telegram saying no.
	"""
	return isinstance(exc, ChannelNotConfiguredError | UnknownDestinationError | AttachmentError)


def _existing_receipt(idempotency_key: str) -> dict | None:
	"""The receipt for a key already sent, or None to go ahead.

	Only a SENT row short-circuits. A previous REFUSED or FAILED attempt must
	be allowed to run again — the administrator may have fixed the setting the
	first attempt complained about.
	"""
	row = frappe.db.get_value(
		LOG_DOCTYPE,
		{"idempotency_key": idempotency_key, "status": "SENT"},
		["channel", "destination", "destination_label", "provider_message_id"],
		as_dict=True,
	)
	if not row:
		return None
	return {
		"status": "SENT",
		"channel": row.channel,
		"destination": row.destination,
		"destination_label": row.destination_label or "",
		"provider_message_id": row.provider_message_id,
		"idempotency_key": idempotency_key,
		"replayed": True,
	}


def _record(
	idempotency_key,
	channel,
	status,
	destination,
	destination_label,
	subject,
	body,
	file_urls,
	requested_by,
	approved_by,
	run_id,
	session_id,
	provider_message_id,
	error_code,
	error_message,
) -> None:
	"""Write the outbound record. Never raises.

	A logging failure must not turn a message that WAS sent into an exception
	the agent reports as a failure — the customer would then be told nothing
	went out while their client has the email. The row is best-effort; the send
	is not.
	"""
	try:
		frappe.get_doc(
			{
				"doctype": LOG_DOCTYPE,
				"idempotency_key": idempotency_key,
				"channel": channel,
				"status": status,
				"destination": destination,
				"destination_label": destination_label,
				"sent_at": now_datetime(),
				"subject": (subject or "")[:140],
				"body_preview": (body or "")[:BODY_PREVIEW_CHARS],
				"attachment_names": ", ".join(os.path.basename(u) for u in (file_urls or []))[:500],
				"requested_by": requested_by,
				"approved_by": approved_by or "",
				"run_id": run_id or "",
				"session_id": session_id or "",
				"provider_message_id": provider_message_id,
				"error_code": error_code,
				"error_message": (error_message or "")[:500],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(
			title="Agent Message Log write failed",
			message=frappe.get_traceback(),
		)


def _remember_channel_error(settings, channel: str, detail: str) -> None:
	"""Surface the last failure on the settings form.

	The person who can fix a delegation problem is an administrator looking at
	that form, not the accountant reading the chat.
	"""
	field = f"{channel}_last_error"
	try:
		frappe.db.set_single_value(SETTINGS_DOCTYPE, field, (detail or "")[:500])
		frappe.db.commit()
	except Exception:
		pass


def _clear_channel_error(settings, channel: str) -> None:
	field = f"{channel}_last_error"
	try:
		if settings.get(field):
			frappe.db.set_single_value(SETTINGS_DOCTYPE, field, "")
			frappe.db.commit()
	except Exception:
		pass
